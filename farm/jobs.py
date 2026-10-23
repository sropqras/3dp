"""Orders, jobs and builds, and the only way their state changes: transition().

A job is one item to make (a plate "A01", a lithophane); a build is one bed = one G-code, holding one or more
jobs. Every state change is a compare-and-set (UPDATE ... WHERE state = <expected>) checked against TRANSITIONS
and the actor allowed to make it, and leaves an events row. Two processes racing for the same change: exactly
one wins, the other gets StateConflict.

Actors: cli, ui (a human in Farm Control), worker, tracker (reads Moonraker), gate (farm/gate.py, the only
starter of prints), import (backfill of old runs).
"""
import sqlite3

from .db import dumps, now, row, tx

HUMAN = {"ui", "cli"}
PIPELINE = {"worker", "cli"}

TRANSITIONS = {
    "job": {
        ("received", "generating"): PIPELINE,
        ("generating", "generated"): PIPELINE,
        ("generating", "failed"): PIPELINE,
        ("failed", "generating"): HUMAN | {"worker"},           # retry
        ("generated", "planned"): PIPELINE,                     # placed on a build
        ("planned", "generated"): HUMAN | {"worker"},           # its build was rejected: re-plan
        ("planned", "printing"): {"tracker", "gate"},
        ("printing", "done"): {"tracker"},
        ("printing", "planned"): {"tracker"},                   # print failed/cancelled: back to re-plan
        **{(s, "cancelled"): HUMAN for s in ("received", "generating", "generated", "planned", "failed")},
    },
    # printer config changes: anyone may stage; only farm/cfgchange.py's gate ("cfggate"), started by a person,
    # applies or rolls back
    "config_change": {
        ("staged", "applying"): {"cfggate"},
        ("applying", "verifying"): {"cfggate"},
        ("verifying", "applied"): {"cfggate"},
        ("applying", "failed"): {"cfggate"},
        ("verifying", "failed"): {"cfggate"},
        ("failed", "rolled_back"): {"cfggate"},
        ("applied", "rolled_back"): {"cfggate"},
        ("staged", "cancelled"): HUMAN | {"agent"},
        ("staged", "rejected"): HUMAN,
    },
    "build": {
        ("slicing", "review"): PIPELINE,
        ("slicing", "failed"): PIPELINE,
        ("review", "approved"): HUMAN,                          # the visual gate: only a person
        ("review", "rejected"): HUMAN,
        ("approved", "uploading"): PIPELINE,
        ("uploading", "queued"): PIPELINE,
        ("uploading", "failed"): PIPELINE,
        ("queued", "approved"): HUMAN,                          # moved to another printer: upload again
        ("queued", "starting"): {"gate"},                       # only farm/gate.py starts prints
        ("starting", "queued"): {"gate"},                       # start refused by the printer: back in line
        ("starting", "printing"): {"gate", "tracker"},
        ("printing", "done"): {"tracker"},
        ("printing", "failed"): {"tracker"},
        ("printing", "cancelled"): {"tracker"},
        ("failed", "slicing"): HUMAN,                           # retry
        ("review", "slicing"): HUMAN,                           # re-slice (another layer height): review again
        ("approved", "slicing"): HUMAN,
        **{(s, "cancelled"): HUMAN for s in ("slicing", "review", "approved", "queued", "failed")},
    },
}
TABLES = {"job": "jobs", "build": "builds", "order": "orders", "config_change": "config_changes"}
TERMINAL = {"job": {"done", "cancelled"}, "build": {"done", "rejected", "cancelled"}}


class StateConflict(Exception):
    """The row was not in the expected state (someone else moved it first), or the change isn't allowed."""


def event(con, entity, entity_id, frm, to, actor, **detail):
    con.execute("INSERT INTO events (entity, entity_id, from_state, to_state, actor, at, detail) "
                "VALUES (?,?,?,?,?,?,?)", (entity, entity_id, frm, to, actor, now(), dumps(detail)))


def transition(con: sqlite3.Connection, entity: str, entity_id: int, frm: str, to: str, actor: str,
               **detail) -> None:
    allowed = TRANSITIONS[entity].get((frm, to))
    if allowed is None:
        raise StateConflict(f"{entity} {entity_id}: {frm} -> {to} is not a transition")
    if actor not in allowed:
        raise StateConflict(f"{entity} {entity_id}: {frm} -> {to} is not allowed for {actor} (only {sorted(allowed)})")
    with tx(con):
        cur = con.execute(f"UPDATE {TABLES[entity]} SET state=?, updated_at=? WHERE id=? AND state=?",
                          (to, now(), entity_id, frm))
        if cur.rowcount != 1:
            got = con.execute(f"SELECT state FROM {TABLES[entity]} WHERE id=?", (entity_id,)).fetchone()
            raise StateConflict(f"{entity} {entity_id}: expected {frm}, is {got[0] if got else 'missing'}")
        event(con, entity, entity_id, frm, to, actor, **detail)


# ---------------------------------------------------------------- creation (initial states only)

def create_order(con, source: str, title: str, external_ref: str | None = None, notes: str = "") -> int:
    """Idempotent on (source, external_ref): the same invtool request or run folder never makes two orders."""
    with tx(con):
        if external_ref is not None:
            hit = con.execute("SELECT id FROM orders WHERE source=? AND external_ref=?",
                              (source, external_ref)).fetchone()
            if hit:
                return hit[0]
        cur = con.execute("INSERT INTO orders (source, external_ref, title, notes, created_at) VALUES (?,?,?,?,?)",
                          (source, external_ref, title, notes, now()))
        event(con, "order", cur.lastrowid, None, "open", source)
        return cur.lastrowid


def create_job(con, order_id: int, maker: str, label: str, *, params=None, inputs=None, qty=1, profile=None,
               layer_height=None, printer=None, colours=(), colour_hex=(), colour_changes=(),
               state="received", actor="cli") -> int:
    t = now()
    with tx(con):
        return _insert_job(con, t, order_id, maker, label, params, inputs, qty, profile, layer_height, printer,
                           colours, colour_hex, colour_changes, state, actor)


def _insert_job(con, t, order_id, maker, label, params, inputs, qty, profile, layer_height, printer, colours,
                colour_hex, colour_changes, state, actor) -> int:
    cur = con.execute(
        "INSERT INTO jobs (order_id, maker, label, params, inputs, qty, profile, layer_height, printer, colours, "
        "colour_hex, colour_changes, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, maker, label, dumps(params or {}), dumps(inputs or {}), qty, profile, layer_height, printer,
         dumps(list(colours)), dumps(list(colour_hex)), dumps(list(colour_changes)), state, t, t))
    event(con, "job", cur.lastrowid, None, state, actor)
    return cur.lastrowid


def create_build(con, order_id: int, printer: str, job_ids: list[int], *, profile=None, run_dir=None,
                 state="slicing", actor="cli", **fields) -> int:
    t = now()
    cols = {"order_id": order_id, "printer": printer, "profile": profile, "run_dir": run_dir, "state": state,
            "created_at": t, "updated_at": t, **fields}
    with tx(con):
        cur = con.execute(f"INSERT INTO builds ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          tuple(dumps(v) if isinstance(v, (list, dict)) else v for v in cols.values()))
        for j in job_ids:
            con.execute("INSERT INTO build_jobs (build_id, job_id) VALUES (?,?)", (cur.lastrowid, j))
        event(con, "build", cur.lastrowid, None, state, actor)
        return cur.lastrowid


def update_build(con, build_id: int, **fields) -> None:
    """Facts about a build (paths, hashes, estimates); never its state - that is transition()'s job."""
    assert "state" not in fields
    sets = ", ".join(f"{k}=?" for k in fields)
    con.execute(f"UPDATE builds SET {sets}, updated_at=? WHERE id=?",
                (*(dumps(v) if isinstance(v, (list, dict)) else v for v in fields.values()), now(), build_id))


# ---------------------------------------------------------------- reads

def builds(con, state: str | None = None) -> list[dict]:
    q = ("SELECT b.*, o.title AS order_title, o.source AS order_source, "
         "(SELECT group_concat(j.label, ' ') FROM build_jobs bj JOIN jobs j ON j.id = bj.job_id "
         " WHERE bj.build_id = b.id) AS labels FROM builds b JOIN orders o ON o.id = b.order_id")
    args = ()
    if state:
        q, args = q + " WHERE b.state = ?", (state,)
    return [row(r) for r in con.execute(q + " ORDER BY b.id DESC", args)]


def get(con, entity: str, entity_id: int) -> dict | None:
    return row(con.execute(f"SELECT * FROM {TABLES[entity]} WHERE id=?", (entity_id,)).fetchone())


def history(con, entity: str, entity_id: int) -> list[dict]:
    return [row(r) for r in con.execute("SELECT * FROM events WHERE entity=? AND entity_id=? ORDER BY id",
                                        (entity, entity_id))]
