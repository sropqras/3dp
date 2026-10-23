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
NOISE = ("printer-2*.cfg", "*.bak-*", "*.bak", "*.backup", "*.zip", "*.conf.20*", "*.log", "crowsnest.conf.2*")
# upstream code symlinked into every instance (recorded in SYMLINKS.txt, not ours to track)
UPSTREAM = ("KAMP/*", "fluidd.cfg", "mainsail.cfg", "timelapse.cfg", "moonraker_obico_macros.cfg")
LOUD = ("printer.cfg", "macros.cfg")


def tracked(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in SECRET + NOISE):
        return False
    return not any(fnmatch.fnmatch(path, p) for p in UPSTREAM)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def list_files(url: str) -> list[dict]:
    files = net.get_json(f"{url}/server/files/list?root=config", timeout=10)["result"]
    return [f for f in files if tracked(f["path"])]


def read_file(url: str, path: str) -> bytes:
    if not tracked(path):
        raise PermissionError(f"{path} is not a file the farm reads (secret or upstream)")
    _, _, body = net.get_bytes(f"{url}/server/files/config/{urllib.parse.quote(path)}", timeout=15)
    return body


def live(url: str) -> dict:
    """{path: (sha, bytes)} of the live config."""
    return {f["path"]: (sha(b), b) for f in list_files(url) for b in [read_file(url, f["path"])]}


def _blob(printer: str, digest: str) -> Path:
    return STORE / re.sub(r"[^A-Za-z0-9_.:-]", "_", printer) / digest


def blob(printer: str, digest: str) -> bytes:
    return _blob(printer, digest).read_bytes()


def snapshot(con, printer: str, url: str, actor: str, note: str = "") -> dict:
    files = live(url)
    for path, (digest, data) in files.items():
        b = _blob(printer, digest)
        if not b.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            b.write_bytes(data)
    manifest = {path: digest for path, (digest, _) in sorted(files.items())}
    prev = latest(con, printer)
    if prev and prev["files"] == manifest:
        return {**prev, "unchanged": True}
    cur = con.execute("INSERT INTO config_snapshots (printer, url, taken_at, actor, files, note) VALUES (?,?,?,?,?,?)",
                      (printer, url, db.now(), actor, json.dumps(manifest), note))
    return get(con, cur.lastrowid)


def get(con, snap_id: int) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE id=?", (snap_id,)).fetchone())


def latest(con, printer: str) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? ORDER BY id DESC LIMIT 1",
                              (printer,)).fetchone())


def reference(con, printer: str) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? AND reviewed_at IS NOT NULL "
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
