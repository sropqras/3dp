"""Spool inventory in the farm DB: each spool's remaining grams = its weight minus a ledger of uses.

The ledger is what makes deduction safe: a finished print writes one row per colour segment, keyed on
(build, attempt, spool, colour) so replaying the tracker never deducts twice. Hand edits in Farm Control are
ledger rows too ("manual"), and every edit carries the version the browser saw: a stale page gets a conflict
instead of silently overwriting what a print just deducted.
"""
import json
import shutil
import time
from pathlib import Path

from . import db

FIELDS = ("material", "brand", "colour", "hex", "location", "role", "weight_g")


class Conflict(Exception):
    pass


def migrate_json(con, path: Path) -> int:
    """One-time: data/inventory.json -> spools + an 'initial' ledger row per spool. Keeps a dated copy."""
    if con.execute("SELECT COUNT(*) FROM spools").fetchone()[0] or not path.is_file():
        return 0
    inv = json.loads(path.read_text(encoding="utf-8"))
    with db.tx(con):
        for s in inv.get("spools", []):
            weight = float(s.get("weight_g") or 1000)
            con.execute("INSERT INTO spools (id, material, brand, colour, hex, weight_g, location, role) "
                        "VALUES (?,?,?,?,?,?,?,?)", (s["id"], s.get("material"), s.get("brand"), s.get("colour"),
                                                     s.get("hex"), weight, s.get("location"), s.get("role")))
            used = weight - float(s.get("remaining_g", weight) or 0)
            if used:
                con.execute("INSERT INTO spool_ledger (spool_id, grams, reason, at) VALUES (?,?,?,?)",
                            (s["id"], used, "initial", db.now()))
    shutil.copy(path, path.with_name(f"inventory.json.migrated-{time.strftime('%Y%m%d-%H%M%S')}"))
    return len(inv.get("spools", []))


def all_spools(con) -> list[dict]:
    rows = con.execute("SELECT s.*, s.weight_g - COALESCE((SELECT SUM(grams) FROM spool_ledger l "
                       "WHERE l.spool_id = s.id), 0) AS remaining_g FROM spools s ORDER BY s.id").fetchall()
    return [dict(r, remaining_g=round(r["remaining_g"], 1)) for r in rows]


def get(con, spool_id: str) -> dict | None:
    return next((s for s in all_spools(con) if s["id"] == spool_id), None)


def at(con, location: str) -> list[dict]:
    return [s for s in all_spools(con) if s.get("location") == location]


def add(con, spool: dict) -> dict:
    sid = spool.get("id") or f"SP-{con.execute('SELECT COUNT(*) FROM spools').fetchone()[0] + 1:03d}"
    with db.tx(con):
        con.execute("INSERT INTO spools (id, material, brand, colour, hex, location, role, weight_g) "
                    "VALUES (?,?,?,?,?,?,?,?)", (sid, *(spool.get(k) for k in FIELDS[:-1]),
                                                 float(spool.get("weight_g") or 1000)))
    return get(con, sid)


def patch(con, spool_id: str, version: int, changes: dict) -> dict:
    """Edit fields and/or set remaining_g (as a 'manual' ledger row). Raises Conflict on a stale version."""
    with db.tx(con):
        cur = get(con, spool_id)
        if cur is None:
            raise LookupError(f"no spool {spool_id}")
        if int(version) != cur["version"]:
            raise Conflict(f"{spool_id} changed since you loaded it (a print may have used some); reload")
        sets = {k: changes[k] for k in FIELDS if k in changes}
        if sets:
            con.execute(f"UPDATE spools SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), spool_id))
        if "remaining_g" in changes:
            delta = cur["remaining_g"] - float(changes["remaining_g"])
            if abs(delta) > 1e-6:
                con.execute("INSERT INTO spool_ledger (spool_id, grams, reason, at) VALUES (?,?,?,?)",
                            (spool_id, delta, "manual", db.now()))
        con.execute("UPDATE spools SET version = version + 1 WHERE id=?", (spool_id,))
    return get(con, spool_id)


def deduct(con, build_id: int, attempt: int, assignments: list, segments: list, used_mm: float | None = None,
           reason: str = "print") -> list[tuple]:
    """Write the grams a print used, per colour segment, to the spool that fed it. used_mm (Moonraker history's
    filament_used) prorates a print that stopped early: segments are consumed in order up to that length.
    Idempotent on (build, attempt, spool, colour). Returns [(spool, colour, grams)] actually written."""
    written = []
    left = used_mm
    with db.tx(con):
        for seg in segments:
            i = seg["colour_idx"]
            grams = seg["grams"]
            if left is not None:
                take = max(0.0, min(seg["mm"], left))
                grams = round(seg["grams"] * (take / seg["mm"]) if seg["mm"] else 0.0, 2)
                left -= take
            spool = assignments[i] if i < len(assignments) else None
            if not spool or grams <= 0:
                continue
            cur = con.execute("INSERT OR IGNORE INTO spool_ledger (spool_id, build_id, attempt, colour_idx, grams, "
                              "reason, at) VALUES (?,?,?,?,?,?,?)",
                              (spool, build_id, attempt, i, grams, reason, db.now()))
            if cur.rowcount:
                con.execute("UPDATE spools SET version = version + 1 WHERE id=?", (spool,))
                written.append((spool, i, grams))
    return written
