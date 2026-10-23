"""The farm's state: one sqlite file (stdlib), shared by the web server, the worker and the CLI.

Large artefacts (STL, G-code, 3MF, uploads) stay on disk under runs/ and data/uploads/; rows hold their paths.
Schema changes are numbered migrations applied on connect (PRAGMA user_version), never edits to old ones.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .printers import REPO

DB_PATH = Path(os.environ.get("FARM_DB", REPO / "data" / "farm.db"))

MIGRATIONS = [
    # 1: orders -> jobs (items) -> models; builds (one bed = one G-code) hold jobs; the audit trail and queues
    """
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_ref TEXT, title TEXT NOT NULL,
        notes TEXT DEFAULT '', state TEXT NOT NULL DEFAULT 'open', created_at REAL NOT NULL,
        UNIQUE (source, external_ref));
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), maker TEXT NOT NULL,
        label TEXT NOT NULL, params TEXT NOT NULL DEFAULT '{}', inputs TEXT NOT NULL DEFAULT '{}',
        qty INTEGER NOT NULL DEFAULT 1, profile TEXT, layer_height REAL, printer TEXT,
        colours TEXT NOT NULL DEFAULT '[]', colour_hex TEXT NOT NULL DEFAULT '[]',
        colour_changes TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, error TEXT, maker_version TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE TABLE models (
        id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id), path TEXT NOT NULL,
        kind TEXT NOT NULL, label TEXT, bbox TEXT, triangles INTEGER, sha256 TEXT);
    CREATE TABLE builds (
        id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id), profile TEXT,
        printer TEXT NOT NULL, state TEXT NOT NULL, run_dir TEXT, gcode TEXT, gcode_sha256 TEXT,
        project_3mf TEXT, remote_path TEXT, estimate_s REAL, grams REAL, segments TEXT,
        queue_pos REAL, moonraker_job_id TEXT, attempt INTEGER NOT NULL DEFAULT 1, paused INTEGER DEFAULT 0,
        started_at REAL, finished_at REAL, error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE UNIQUE INDEX builds_run_gcode ON builds(run_dir, gcode);
    CREATE TABLE build_jobs (
        build_id INTEGER NOT NULL REFERENCES builds(id), job_id INTEGER NOT NULL REFERENCES jobs(id),
        PRIMARY KEY (build_id, job_id));
    CREATE TABLE approvals (
        id INTEGER PRIMARY KEY, build_id INTEGER NOT NULL REFERENCES builds(id), kind TEXT NOT NULL,
        gcode_sha256 TEXT, actor TEXT NOT NULL, at REAL NOT NULL, checklist TEXT);
    CREATE TABLE events (
        id INTEGER PRIMARY KEY, entity TEXT NOT NULL, entity_id INTEGER NOT NULL, from_state TEXT,
        to_state TEXT, actor TEXT NOT NULL, at REAL NOT NULL, detail TEXT);
    CREATE INDEX events_entity ON events(entity, entity_id);
    CREATE TABLE tasks (
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref_id INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
        lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, run_after REAL NOT NULL DEFAULT 0,
        created_at REAL NOT NULL);
    CREATE INDEX tasks_pending ON tasks(state, run_after);
    CREATE TABLE spools (
        id TEXT PRIMARY KEY, material TEXT, brand TEXT, colour TEXT, hex TEXT, weight_g REAL NOT NULL,
        location TEXT, role TEXT, version INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE spool_ledger (
        id INTEGER PRIMARY KEY, spool_id TEXT NOT NULL REFERENCES spools(id), build_id INTEGER, attempt INTEGER,
        colour_idx INTEGER, grams REAL NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL,
        UNIQUE (build_id, attempt, spool_id, colour_idx));
    CREATE TABLE outbox (
        id INTEGER PRIMARY KEY, target TEXT NOT NULL, payload TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE,
        attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0, sent_at REAL, last_error TEXT);
    CREATE TABLE heartbeat (name TEXT PRIMARY KEY, at REAL NOT NULL, detail TEXT);
    """,
    # 2: what the pipeline needs to run from rows alone (worker), not from a job.json in memory
    """
    ALTER TABLE orders ADD COLUMN run_dir TEXT;
    ALTER TABLE orders ADD COLUMN upload_folder TEXT;
    ALTER TABLE jobs ADD COLUMN bed_group TEXT;
    ALTER TABLE jobs ADD COLUMN colour_zs TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE builds ADD COLUMN layer_height REAL;
    ALTER TABLE builds ADD COLUMN change_zs TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE builds ADD COLUMN colours TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE builds ADD COLUMN colour_hex TEXT NOT NULL DEFAULT '[]';
    ALTER TABLE tasks ADD COLUMN finished_at REAL
    """,
    # 3: which models (pieces) are on which bed, in bed order
    """
    CREATE TABLE build_models (
        build_id INTEGER NOT NULL REFERENCES builds(id), model_id INTEGER NOT NULL REFERENCES models(id),
        pos INTEGER NOT NULL, PRIMARY KEY (build_id, model_id))
    """,
    # 4: printer config snapshots (Printers tab): files {path: sha} per snapshot, content-addressed on disk
    """
    CREATE TABLE config_snapshots (
        id INTEGER PRIMARY KEY, printer TEXT NOT NULL, url TEXT NOT NULL, taken_at REAL NOT NULL,
        actor TEXT NOT NULL, files TEXT NOT NULL, reviewed_by TEXT, reviewed_at REAL, note TEXT);
    CREATE INDEX config_snapshots_printer ON config_snapshots(printer, id)
    """,
    # 5: staged printer config changes and their gated apply (farm/cfgchange.py)
    """
    CREATE TABLE config_changes (
        id INTEGER PRIMARY KEY, printer TEXT NOT NULL, url TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
        summary TEXT NOT NULL, reason TEXT NOT NULL, author TEXT NOT NULL, base_sha TEXT, new_content TEXT NOT NULL,
        state TEXT NOT NULL, error TEXT, result TEXT, before_snapshot INTEGER, applied_by TEXT, applied_at REAL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL);
    CREATE INDEX config_changes_printer ON config_changes(printer, state)
    """,
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    """One connection per thread. WAL lets the web server read while the worker writes; busy_timeout makes a
    writer wait for another instead of failing."""
    path = Path(path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10, isolation_level=None)   # autocommit; explicit BEGIN where needed
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA foreign_keys=ON")
    migrate(con)
    return con


def migrate(con: sqlite3.Connection) -> int:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for n, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        con.execute("BEGIN IMMEDIATE")
        try:
            # re-check under the write lock: another process may have migrated meanwhile
            if con.execute("PRAGMA user_version").fetchone()[0] >= n:
                con.execute("ROLLBACK")
                continue
            for stmt in (s.strip() for s in sql.split(";")):
                if stmt:
                    con.execute(stmt)
            con.execute(f"PRAGMA user_version = {n}")
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return con.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def tx(con: sqlite3.Connection):
    """All-or-nothing block; joins the caller's transaction if one is already open."""
    if con.in_transaction:
        yield con
        return
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def now() -> float:
    return time.time()


def dumps(v) -> str:
    return json.dumps(v, default=str)


def row(r: sqlite3.Row | None) -> dict | None:
    """Row -> dict with JSON columns decoded."""
    if r is None:
        return None
    d = dict(r)
    for k in ("params", "inputs", "colours", "colour_hex", "colour_changes", "colour_zs", "change_zs", "bbox",
              "segments", "detail", "checklist", "payload", "files"):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
