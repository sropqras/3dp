"""Printer config files: snapshots, the reviewed reference, and drift - all over Moonraker's file API, so every
host works the same way (enders3dp, 3dpnas, any new one) and nothing needs SSH.

A snapshot reads every config file except secrets and upstream code, stores each file once by its sha256 under
data/config_snapshots/<printer>/, and records {path: sha} in the DB. The reference is the newest snapshot a
person marked reviewed; drift is the live config compared with it.
"""
import difflib
import fnmatch
import hashlib
import json
import re
import urllib.parse
from pathlib import Path

from . import db, net
from .printers import REPO

STORE = REPO / "data" / "config_snapshots"
# never read into the farm: tokens and keys (CLAUDE.md), plus noise
SECRET = ("telegram.conf", "moonraker-obico.cfg", "octoeverywhere.conf", "*.secrets", "moonraker.secrets",
          ".moonraker.conf.bkp")
NOISE = ("printer-2*.cfg", "*.bak-*", "*.bak", "*.backup", "*.zip", "*.tar", "*.tar.gz", "*.tgz", "*.conf.20*", "*.log",
         "crowsnest.conf.2*")
# upstream code symlinked into every instance (recorded in SYMLINKS.txt, not ours to track)
UPSTREAM = ("KAMP/*", "fluidd.cfg", "mainsail.cfg", "timelapse.cfg", "moonraker_obico_macros.cfg")
LOUD = ("printer.cfg", "macros.cfg")


def secret(path: str) -> bool:
    """A secret file, or any copy of one (telegram.conf.bak-20261022 holds the same token)."""
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(name, p + ".*") or fnmatch.fnmatch(name, p + "-*")
               for p in SECRET)


def tracked(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if secret(path) or any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in NOISE):
        return False
    return not any(fnmatch.fnmatch(path, p) for p in UPSTREAM)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_files(url: str) -> list[dict]:
    files = net.get_json(f"{url}/server/files/list?root=config", timeout=10)["result"]
    return [f for f in files if tracked(f["path"])]


def list_history(url: str) -> list[dict]:
    """The config root's own history (host .bak copies, SAVE_CONFIG's printer-*.cfg, Fluidd zips): every file the
    snapshots leave out as noise, never a secret."""
    files = net.get_json(f"{url}/server/files/list?root=config", timeout=10)["result"]
    return [f for f in files if not secret(f["path"]) and not tracked(f["path"])
            and not any(fnmatch.fnmatch(f["path"], p) for p in UPSTREAM)]


def read_history(url: str, path: str) -> bytes:
    if secret(path) or tracked(path):
        raise PermissionError(f"{path} is not a history file")
    _, _, body = net.get_bytes(f"{url}/server/files/config/{urllib.parse.quote(path)}", timeout=30)
    return body


def read_file(url: str, path: str) -> bytes:
    if not tracked(path):
        raise PermissionError(f"{path} is not a file the farm reads (secret or upstream)")
    _, _, body = net.get_bytes(f"{url}/server/files/config/{urllib.parse.quote(path)}", timeout=15)
    return body


def live(url: str, mtimes: dict | None = None) -> dict:
    """{path: (sha, bytes)} of the live config; `mtimes` (if given) gets {path: modified}."""
    out = {}
    for f in list_files(url):
        b = read_file(url, f["path"])
        out[f["path"]] = (sha(b), b)
        if mtimes is not None and f.get("modified"):
            mtimes[f["path"]] = f["modified"]
    return out


def store(printer: str, data: bytes) -> str:
    """Keep one file's bytes in the snapshot store; returns its sha."""
    digest = sha(data)
    b = _blob(printer, digest)
    if not b.exists():
        b.parent.mkdir(parents=True, exist_ok=True)
        b.write_bytes(data)
    return digest


def _blob(printer: str, digest: str) -> Path:
    return STORE / re.sub(r"[^A-Za-z0-9_.:-]", "_", printer) / digest


def blob(printer: str, digest: str) -> bytes:
    return _blob(printer, digest).read_bytes()


def snapshot(con, printer: str, url: str, actor: str, note: str = "") -> dict:
    """The live config. Each file's modified time is kept too: it dates the config, so a print can be matched to
    the config it ran on (farm/restore.py)."""
    mt = {}
    files = live(url, mt)
    for path, (digest, data) in files.items():
        store(printer, data)
    manifest = {path: digest for path, (digest, _) in sorted(files.items())}
    prev = latest(con, printer)
    if prev and prev["files"] == manifest:
        # an older snapshot gets the file times it lacked, but only those older than it: a file re-uploaded with
        # the same bytes since (a rollback) has a newer time that would wrongly date the snapshot
        old = {p: t for p, t in mt.items() if t < prev["taken_at"]}
        if not prev.get("mtimes") and old:
            con.execute("UPDATE config_snapshots SET mtimes=? WHERE id=?", (json.dumps(old), prev["id"]))
        return {**prev, "unchanged": True}
    cur = con.execute("INSERT INTO config_snapshots (printer, url, taken_at, actor, files, note, mtimes) "
                      "VALUES (?,?,?,?,?,?,?)", (printer, url, db.now(), actor, json.dumps(manifest), note,
                                                 json.dumps(mt) if mt else None))
    return get(con, cur.lastrowid)


def get(con, snap_id: int) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE id=?", (snap_id,)).fetchone())


def latest(con, printer: str) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? AND kind='live' ORDER BY id DESC LIMIT 1",
                              (printer,)).fetchone())


def origin(con, printer: str) -> dict | None:
    """The printer's config as it was before the farm first changed it (farm/restore.py pins it once)."""
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? AND kind='origin' ORDER BY id LIMIT 1",
                              (printer,)).fetchone())


def reference(con, printer: str) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? AND kind='live' AND reviewed_at IS NOT NULL "
                              "ORDER BY id DESC LIMIT 1", (printer,)).fetchone())


def review(con, snap_id: int, actor: str) -> dict:
    """A person looked at this snapshot and accepts it as the printer's known-good config."""
    con.execute("UPDATE config_snapshots SET reviewed_by=?, reviewed_at=? WHERE id=?", (actor, db.now(), snap_id))
    return get(con, snap_id)


def drift(con, printer: str, current: dict) -> dict:
    """current: {path: sha} (live or a snapshot's files). Compared with the reviewed reference."""
    ref = reference(con, printer)
    if ref is None:
        return {"reference": None, "changed": [], "added": sorted(current), "removed": [], "loud": False}
    want = ref["files"]
    changed = sorted(p for p in current if p in want and current[p] != want[p])
    added = sorted(p for p in current if p not in want)
    removed = sorted(p for p in want if p not in current)
    loud = any(p.rsplit("/", 1)[-1] in LOUD for p in changed + added + removed)
    return {"reference": ref["id"], "changed": changed, "added": added, "removed": removed, "loud": loud}


def diff(printer: str, path: str, old_sha: str | None, new: bytes | None, new_label: str = "live") -> str:
    a = blob(printer, old_sha).decode("utf-8", "replace").splitlines(keepends=True) if old_sha else []
    b = (new or b"").decode("utf-8", "replace").splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, f"reviewed/{path}", f"{new_label}/{path}", n=3))


def to_repo(con, printer: str, dest: Path) -> list[str]:
    """Write the latest snapshot's files into printers/<name>/ (the git mirror); committing stays a person's call."""
    snap = latest(con, printer)
    if not snap:
        return []
    written = []
    for path, digest in snap["files"].items():
        out = dest / path
        data = blob(printer, digest)
        if not out.exists() or out.read_bytes() != data:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
            written.append(path)
    return written
