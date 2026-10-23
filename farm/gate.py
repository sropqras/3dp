"""The Start gate: the ONLY code that starts a print (moonraker.start needs the token made here).

start_build() refuses unless every check passes, and records who started what with which checklist:
  1  the build is queued, and this call wins the queued -> starting compare-and-set (a double click loses)
  2  a person approved exactly this G-code (review approval's sha256 == the file's sha256 now)
  3  the checklist is ticked (viewed 3D, bed clear, colour loaded) AND Farm Control served this build's 3D view
     (CLI starts confirm the view interactively instead)
  4  the printer isn't watch-only and is one the build's profile allows
  5  the printer's live config passes the farm baseline audit (zero FAIL) - blocking, unlike the old CLI
  6  Klipper is ready, not printing, not paused, and no other build is starting/printing on that printer
  7  the uploaded file is on the printer with the local G-code's size
  8  a spool is chosen for the first colour, and its material matches the profile
Agents never call this (CLAUDE.md): starting a print is a person's decision at the machine.
"""
import json
import secrets
import threading
import time
import urllib.parse
from pathlib import Path

from . import audit, db, jobs, moonraker, net, pipeline, printers, profiles, spools
from .printers import REPO

CHECKLIST = ("viewed_3d", "bed_clear", "colour_loaded")
NONCE_TTL_S = 120


class GateRefused(Exception):
    pass


class _Token:
    """Proof for moonraker.start that the call comes from the gate. Only this module makes one."""
    __slots__ = ()


_TOKEN = _Token()
_nonces: dict[str, tuple[int, float]] = {}
_nonce_lock = threading.Lock()


def issue_nonce(build_id: int) -> str:
    """The web Start dialog gets one when it opens; a Start POST without a fresh one is refused."""
    n = secrets.token_urlsafe(16)
    with _nonce_lock:
        now = time.time()
        for k in [k for k, (_, exp) in _nonces.items() if exp < now]:
            del _nonces[k]
        _nonces[n] = (build_id, now + NONCE_TTL_S)
    return n


def _use_nonce(nonce: str, build_id: int) -> bool:
    with _nonce_lock:
        hit = _nonces.pop(nonce, None)
    return bool(hit) and hit[0] == build_id and hit[1] >= time.time()


def spools_for(con, printer_name: str) -> list[dict]:
    return spools.at(con, printer_name)


def preflight(con, build_id: int, viewed_required: bool = True) -> dict:
    """Everything the Start dialog shows, and the reasons Start would be refused right now (no side effects)."""
    b = jobs.get(con, "build", build_id)
    if b is None:
        raise GateRefused(f"no build {build_id}")
    reasons, info = [], {"build": b}
    if b["state"] != "queued":
        reasons.append(f"build is {b['state']}, not queued")
    printer = printers.PRINTERS.get(b["printer"])
    profile = profiles.PROFILES.get(b["profile"])
    if printer is None or profile is None:
        reasons.append(f"unknown printer/profile {b['printer']}/{b['profile']}")
        return {**info, "reasons": reasons}
    if printer.readonly:
        reasons.append(f"{printer.name} is watch-only")
    if not profile.allows(printer.name):
        reasons.append(f"profile {profile.id} is not for {printer.name}")
    if not profile.confirmed:
        reasons.append(f"profile {profile.id} is unconfirmed")
    gpath = Path(b["gcode"] or "")
    sha = pipeline.sha256(gpath) if gpath.is_file() else None
    appr = con.execute("SELECT gcode_sha256, actor, at FROM approvals WHERE build_id=? AND kind='review' "
                       "ORDER BY id DESC LIMIT 1", (build_id,)).fetchone()
    if not appr:
        reasons.append("not approved at review")
    elif appr["gcode_sha256"] != sha:
        reasons.append("the G-code changed after it was approved; review it again")
    viewed = con.execute("SELECT 1 FROM approvals WHERE build_id=? AND kind='viewed'", (build_id,)).fetchone()
    info["viewed"] = bool(viewed)
    if viewed_required and not viewed:
        reasons.append("the 3D view of this build hasn't been opened in Farm Control")
    busy = con.execute("SELECT id FROM builds WHERE printer=? AND state IN ('starting','printing') AND id<>?",
                       (printer.name, build_id)).fetchall()
    if busy:
        reasons.append(f"{printer.name} already has build {busy[0][0]} starting/printing")
    try:
        st = moonraker.status(printer)
        info["printer_status"] = st
        if st["klipper"] != "ready" or st["job"] not in moonraker.STARTABLE or st["paused"]:
            reasons.append(f"{printer.name} is not idle: klipper {st['klipper']}, job {st['job']}, "
                           f"paused {st['paused']}")
    except Exception as e:
        reasons.append(f"{printer.name} unreachable: {str(e)[:120]}")
    try:
        fails = [r for stt, r, _ in audit.rules(audit._settings(printer), printer) if stt == "FAIL"]
        info["audit_fails"] = fails
        if fails:
            reasons.append(f"farm baseline audit fails on {printer.name}: {'; '.join(fails)}")
    except Exception as e:
        reasons.append(f"audit could not read {printer.name}: {str(e)[:120]}")
    if b["remote_path"] and gpath.is_file():
        try:
            meta = net.get_json(f"{printer.moonraker}/server/files/metadata?filename="
                                f"{urllib.parse.quote(b['remote_path'])}", timeout=5)
            if int(meta["result"].get("size", -1)) != gpath.stat().st_size:
                reasons.append("the file on the printer differs from the approved G-code (size)")
        except Exception as e:
            reasons.append(f"uploaded file not found on {printer.name}: {str(e)[:120]}")
    elif not b["remote_path"]:
        reasons.append("not uploaded")
    info["spools"] = spools_for(con, printer.name)
    info["all_spools"] = spools.all_spools(con)
    info["material"] = profile.material
    return {**info, "reasons": reasons}


def start_build(con, build_id: int, operator: str, checklist: dict, spool_id: str | None, via: str,
                nonce: str | None = None, later_spools: list | None = None) -> str:
    """spool_id: loaded now (first colour, must be on this printer); later_spools: for colours 2.. (optional,
    any spool - they come off the shelf at the colour change); together they decide where the grams go."""
    """Start an approved, queued, uploaded build. Returns the remote file started. Raises GateRefused."""
    if via == "ui" and not (nonce and _use_nonce(nonce, build_id)):
        raise GateRefused("the Start dialog expired or was not opened for this build; open it again")
    missing = [c for c in CHECKLIST if not checklist.get(c)]
    if missing:
        raise GateRefused(f"checklist not complete: {', '.join(missing)}")
    if not operator:
        raise GateRefused("who is starting it? (operator name)")
    pf = preflight(con, build_id, viewed_required=(via == "ui"))
    spool = next((s for s in pf.get("spools", []) if s.get("id") == spool_id), None)
    if spool is None:
        pf["reasons"].append(f"choose the spool loaded on {pf['build']['printer']} (none chosen or not there)")
    elif str(spool.get("material", "")).lower() != pf.get("material"):
        pf["reasons"].append(f"spool {spool_id} is {spool.get('material')}, the profile prints {pf.get('material')}")
    known = {s["id"] for s in pf.get("all_spools", [])}
    later = [s or None for s in (later_spools or [])]
    unknown = [s for s in later if s and s not in known]
    if unknown:
        pf["reasons"].append(f"unknown spool(s) for later colours: {', '.join(unknown)}")
    if pf["reasons"]:
        raise GateRefused("; ".join(pf["reasons"]))
    b = pf["build"]
    try:
        jobs.transition(con, "build", build_id, "queued", "starting", "gate", operator=operator, via=via)
    except jobs.StateConflict as e:
        raise GateRefused(f"someone else is starting it ({e})") from None
    printer = printers.PRINTERS[b["printer"]]
    run = pipeline.run_for(con, b["order_id"])
    try:
        with db.tx(con):
            con.execute("INSERT INTO approvals (build_id, kind, gcode_sha256, actor, at, checklist) "
                        "VALUES (?,?,?,?,?,?)", (build_id, "start", b["gcode_sha256"], f"{via}:{operator}",
                                                  db.now(), db.dumps({**checklist, "spool": spool_id,
                                                                      "spools": [spool_id] + later})))
        moonraker.start(run, printer, b["remote_path"], token=_TOKEN)
    except BaseException as e:
        jobs.transition(con, "build", build_id, "starting", "queued", "gate", error=str(e)[:300])
        raise GateRefused(f"printer refused the start: {e}") from None
    with db.tx(con):
        jobs.update_build(con, build_id, started_at=db.now())
        jobs.transition(con, "build", build_id, "starting", "printing", "gate")
        for (jid,) in con.execute("SELECT job_id FROM build_jobs WHERE build_id=?", (build_id,)).fetchall():
            if jobs.get(con, "job", jid)["state"] == "planned":
                jobs.transition(con, "job", jid, "planned", "printing", "gate", build=build_id)
    return b["remote_path"]


def record_view(con, project_3mf: Path) -> None:
    """Farm Control served this .3mf: remember it per build (gate check 3)."""
    for (bid,) in con.execute("SELECT id FROM builds WHERE project_3mf=? AND state IN ('review','approved',"
                              "'uploading','queued')", (str(project_3mf),)).fetchall():
        con.execute("INSERT INTO approvals (build_id, kind, gcode_sha256, actor, at) VALUES (?,?,?,?,?)",
                    (bid, "viewed", None, "ui", db.now()))
