"""Restore points: what each printer's config was before the farm first changed it (its **origin**), every snapshot
since, and the evidence for each one: the prints that completed while that config was the one on the printer.

These printers ran for years before the farm. Their configs aren't tidy, but they printed, so losing that state
would mean rebuilding a printer from scratch. Therefore:

- **The origin is pinned once and never re-taken.** It comes from the oldest farm snapshot. Any file that was
  edited before that snapshot comes from the host's own `<file>.bak-YYYYMMDD-HHMM` copy instead: the first editing
  sessions made one before every edit, and kept the file's original modified time. It sits in the DB as a
  `config_snapshots` row of kind `origin`, and in git under `printers/_archive/<printer>/origin/`.
- **The host's own history is archived into git:** `.bak` copies, SAVE_CONFIG's `printer-*.cfg`, Fluidd zips and,
  over read-only SSH, KIAUH's `printer-data-backups`. Secrets never are, by name or by content.
- **Each config is dated by its files' modified times** (`config_snapshots.mtimes`). It was live from its newest
  file's time until the next change: a gate Apply, or a newer file time in the next snapshot. A print that
  started and finished inside that window ran on it. That is "exact" when both ends are known; otherwise it is
  "likely", for example when a file changed outside the farm and its time wasn't recorded.
- **The last known good config** is the newest one with completed prints and none a person marked bad.
- **A restore stages one change per differing file as a single batch.** A person applies it through the same gate
  as every change: it refuses while printing, takes a snapshot, uploads all the files, does one FIRMWARE_RESTART,
  and puts everything back if Klipper doesn't load. Files the old config doesn't name are left in place.
"""
import io
import json
import re
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path

from . import cfgchange, configs, db, net, printers
from .printers import REPO

ARCHIVE = REPO / "printers" / "_archive"
BAK = re.compile(r"^(?P<base>.+)\.bak-(?P<stamp>\d{8}-\d{4}(?:\d{2})?)$")
# a token in a file that isn't named like a secret (moonraker.conf [power] tokens, a pasted key) keeps it out of git
SECRET_TEXT = re.compile(r"(?i)(bot_token|api_key|apikey|auth_token|access_token|password|passwd|github_pat_|"
                         r"\bghp_[A-Za-z0-9]{20}|\bsk-[A-Za-z0-9]{20}|bearer\s+[A-Za-z0-9._-]{20})")
HISTORY_MAX = 2_000_000
SSH_KEY = Path.home() / ".ssh" / "enders3dp"


def _stamp(s: str) -> float:
    return datetime.strptime(s if len(s) == 15 else s + "00", "%Y%m%d-%H%M%S").timestamp()


def _day(t: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def folder(key: str) -> str:
    """printers/<folder>/ for a fleet key ('Ender3MaxNeo', 'enders3dp:Ender3Pro' -> 'Ender3Pro')."""
    return key if key in printers.PRINTERS else key.rsplit(":", 1)[-1]


def has_secret(data: bytes) -> bool:
    return bool(SECRET_TEXT.search(data.decode("utf-8", "replace")))


def _write(dest: Path, data: bytes) -> bool:
    if dest.exists() and dest.read_bytes() == data:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True


def _first_live(con, key: str) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_snapshots WHERE printer=? AND kind='live' ORDER BY id LIMIT 1",
                              (key,)).fetchone())


def _first_change(con, key: str) -> float | None:
    r = con.execute("SELECT MIN(applied_at) FROM config_changes WHERE printer=? AND applied_at IS NOT NULL",
                    (key,)).fetchone()
    return r[0] if r else None


# ---------------------------------------------------------------- the origin

def capture_origin(con, key: str, url: str, actor: str) -> dict:
    """Pin the printer's original config (once; a pinned origin is never replaced). Reads the printer, never writes."""
    have = configs.origin(con, key)
    if have:
        return {**have, "unchanged": True}
    first = _first_live(con, key) or configs.snapshot(con, key, url, actor="origin capture")
    hist = configs.list_history(url)
    baks = {}                                   # base path -> (stamp, bak path, its modified time)
    for f in hist:
        m = BAK.match(f["path"])
        if not m or not configs.tracked(m["base"]):
            continue
        t = _stamp(m["stamp"])
        if t < first["taken_at"] and (m["base"] not in baks or t < baks[m["base"]][0]):
            baks[m["base"]] = (t, f["path"], f.get("modified"))
    now_files = {f["path"]: f for f in configs.list_files(url)}
    files, mt, src, edits, likely = {}, {}, {}, [], {}
    for path, digest in first["files"].items():
        if path in baks:
            t, bp, mod = baks[path]
            files[path] = configs.store(key, configs.read_history(url, bp))
            if mod:
                mt[path] = mod
            src[path] = f"{bp}: the host's copy made before the first edit ({_day(t)})"
            edits.append(t)
            continue
        files[path] = digest
        src[path] = f"farm snapshot {first['id']} ({_day(first['taken_at'])})"
        f = now_files.get(path)
        if (first.get("mtimes") or {}).get(path, 9e18) < first["taken_at"]:
            mt[path] = first["mtimes"][path]
        elif f and f.get("modified") and f["modified"] < first["taken_at"] and \
                configs.sha(configs.read_file(url, path)) == digest:
            mt[path] = f["modified"]                     # untouched since: today's file time is its time
        else:   # touched since (a rollback re-uploads the same bytes): the oldest identical archived copy dates it
            seen = _archived_since(key, path, digest)
            if seen:
                mt[path], likely[path] = seen
    ends = edits + [t for t in [_first_change(con, key)] if t]
    until = min(ends) if ends else None
    meta = {"since": max(mt.values()) if mt and len(mt) == len(files) else None, "until": until,
            "undated": sorted(set(files) - set(mt)), "dated_by_copy": likely, "sources": src, "first_snapshot": first["id"],
            "edited_before_first_snapshot": sorted(baks)}
    cur = con.execute("INSERT INTO config_snapshots (printer, url, taken_at, actor, files, note, kind, mtimes, meta) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (key, url, db.now(), actor, json.dumps(dict(sorted(files.items()))),
                       "original state, before the farm's first change", "origin", json.dumps(mt), json.dumps(meta)))
    o = configs.get(con, cur.lastrowid)
    export_origin(con, key)
    return o


def _archived_since(key: str, path: str, digest: str):
    """(time, where) of the oldest archived copy of this exact file (KIAUH backups, host history), or None."""
    best = None
    for d in sorted((ARCHIVE / folder(key) / "kiauh").glob("*")):
        f = d / path
        if f.is_file() and configs.sha(f.read_bytes()) == digest:
            try:
                t = datetime.strptime(d.name, "%Y%m%d-%H%M%S").timestamp()
            except ValueError:
                continue
            if best is None or t < best[0]:
                best = (t, f"identical in KIAUH backup {d.name}")
    return best


def export_origin(con, key: str) -> Path | None:
    """printers/_archive/<printer>/origin/: the files plus ORIGIN.md saying where each came from (git = off this host)."""
    o = configs.origin(con, key)
    if not o:
        return None
    dest = ARCHIVE / folder(key) / "origin"
    meta, mt = o.get("meta") or {}, o.get("mtimes") or {}
    for path, digest in o["files"].items():
        _write(dest / path, configs.blob(key, digest))
    lines = [f"# {folder(key)}: original config", "",
             f"Pinned {_day(o['taken_at'])} by {o['actor']} (farm DB snapshot {o['id']}, kind `origin`).",
             "This is the config the printer ran before the farm first changed it. Restore it from the Printers tab",
             "(Restore points -> Stage restore), never by copying these files over by hand.", "",
             f"- live from: {_day(meta.get('since'))} (newest file time)",
             f"- replaced: {_day(meta.get('until')) if meta.get('until') else 'not yet (still the live config)'}", "",
             "| File | Modified | sha256 | Source |", "|---|---|---|---|"]
    for path, digest in o["files"].items():
        lines.append(f"| {path} | {_day(mt.get(path))} | {digest[:12]} | {meta.get('sources', {}).get(path, '')} |")
    _write(dest.parent / "ORIGIN.md", ("\n".join(lines) + "\n").encode())
    return dest


# ---------------------------------------------------------------- the host's own history

def archive_history(key: str, url: str) -> dict:
    """Every history file in the printer's config folder, plus the members of its Fluidd zips, into
    printers/_archive/<printer>/host/. Secrets are left out by name and by content."""
    dest = ARCHIVE / folder(key) / "host"
    out = {"written": 0, "kept": 0, "skipped": []}
    for f in configs.list_history(url):
        if f["path"].endswith((".tar", ".tar.gz", ".tgz")):
            out["skipped"].append(f"{f['path']} (tarball: can't be checked for secrets)")
            continue
        if f.get("size", 0) > HISTORY_MAX * (10 if f["path"].endswith(".zip") else 1):
            out["skipped"].append(f"{f['path']} (too big)")
            continue
        data = configs.read_history(url, f["path"])
        items = [(f["path"], data)]
        if f["path"].endswith(".zip"):
            items = []
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for n in z.namelist():
                        if not n.endswith("/") and z.getinfo(n).file_size <= HISTORY_MAX:
                            items.append((f"{f['path'][:-4]}/{n}", z.read(n)))
            except zipfile.BadZipFile:
                out["skipped"].append(f"{f['path']} (not a zip)")
        for path, b in items:
            if configs.secret(path) or has_secret(b) or ".." in Path(path).parts:
                out["skipped"].append(f"{path} (secret)")
                continue
            out["written" if _write(dest / path, b) else "kept"] += 1
    return out


def archive_kiauh(ssh: str = "aali@enders3dp.local") -> dict:
    """KIAUH's printer-data-backups on enders3dp (2024-25 copies of every instance's config), over read-only SSH,
    into printers/_archive/<printer>/kiauh/<backup>/. Only that folder; secrets never leave the host."""
    tmp = REPO / "data" / "tmp" / "kiauh"
    tmp.mkdir(parents=True, exist_ok=True)
    excl = [a for p in configs.SECRET for a in ("--exclude", p, "--exclude", p + ".*", "--exclude", p + "-*")]
    r = subprocess.run(["rsync", "-a", "--delete", "-e", f"ssh -i {SSH_KEY} -o BatchMode=yes -o ConnectTimeout=10",
                        *excl, "--exclude", "*.zip", f"{ssh}:kiauh-backups/printer-data-backups/", f"{tmp}/"],
                       capture_output=True, text=True, timeout=600)
    if r.returncode not in (0, 23, 24):
        raise RuntimeError(f"rsync {r.returncode}: {r.stderr.strip()[:300]}")
    names = {p.lower(): p for p in [*printers.PRINTERS, *(d.name for d in (REPO / "printers").iterdir() if d.is_dir())]}
    out = {"written": 0, "kept": 0, "skipped": []}
    for d in sorted(x for x in tmp.iterdir() if x.is_dir()):
        inst, _, stamp = d.name.rpartition("_data-")
        name = names.get(inst.lower())
        if not name:
            out["skipped"].append(f"{d.name} (no printer called {inst})")
            continue
        for f in sorted(x for x in d.rglob("*") if x.is_file()):
            rel = f.relative_to(d).as_posix()
            b = f.read_bytes()
            if configs.secret(rel) or len(b) > HISTORY_MAX or has_secret(b):
                out["skipped"].append(f"{d.name}/{rel}")
                continue
            out["written" if _write(ARCHIVE / name / "kiauh" / stamp / rel, b) else "kept"] += 1
    return out


# ---------------------------------------------------------------- evidence

def _prints(con, key: str, url: str | None) -> list[dict]:
    if key in printers.PRINTERS:
        return [{"start": r[0], "end": r[1], "status": r[2], "file": r[3], "quality": r[4]} for r in con.execute(
            "SELECT started_at, finished_at, status, file, quality FROM prints WHERE printer=? ORDER BY started_at", (key,))]
    if not url:
        return []
    out, start = [], 0
    while True:
        jobs = net.get_json(f"{url}/server/history/list?limit=100&start={start}&order=asc", timeout=15)["result"].get("jobs") or []
        out += [{"start": j.get("start_time"), "end": j.get("end_time"), "status": j.get("status"),
                 "file": j.get("filename"), "quality": None} for j in jobs]
        if len(jobs) < 100:
            return out
        start += len(jobs)


def points(con, key: str, url: str | None = None) -> list[dict]:
    """Every restore point of a printer, newest first, each with the window it was live and the prints in it."""
    o = configs.origin(con, key)
    snaps = [db.row(r) for r in con.execute("SELECT * FROM config_snapshots WHERE printer=? AND kind='live' ORDER BY id",
                                            (key,))]
    changes = [db.row(r) for r in con.execute("SELECT id, applied_at, before_snapshot, state FROM config_changes "
                                              "WHERE printer=? AND applied_at IS NOT NULL ORDER BY applied_at", (key,))]
    now = db.now()
    edited = {}                       # path -> the times a session edited it (its .bak-<stamp> copies on the host)
    try:
        for f in configs.list_history(url) if url else []:
            m = BAK.match(f["path"])
            if m:
                edited.setdefault(m["base"], []).append(_stamp(m["stamp"]))
    except Exception:
        pass
    pts = []
    if o:
        m = o.get("meta") or {}
        pts.append({**o, "since": m.get("since"), "until": m.get("until") or now, "current": not m.get("until"),
                    "exact": bool(m.get("since")) and not m.get("undated") and not m.get("dated_by_copy")})
    for i, s in enumerate(snaps):
        mt, nxt = s.get("mtimes") or {}, snaps[i + 1] if i + 1 < len(snaps) else None
        dated = bool(mt) and set(mt) >= set(s["files"]) and bool(s["files"])
        if dated:
            since = max(mt[p] for p in s["files"])
        else:   # not dated: no earlier than the snapshot before it, or the last session edit of one of its files
            lows = [snaps[i - 1]["taken_at"]] if i else []
            lows += [t for p in s["files"] for t in edited.get(p, []) if t < s["taken_at"]]
            since = max(lows) if lows else None
        # the next change through the gate ends it (its "before" snapshot can be this one, reused unchanged)
        gate_end = next((c["applied_at"] for c in changes if c["applied_at"] >= s["taken_at"] - 5), None)
        ends = [gate_end] if gate_end else []
        if nxt:
            moved = [p for p in nxt["files"] if s["files"].get(p) != nxt["files"][p]]
            nmt = nxt.get("mtimes") or {}
            ends.append(min(nmt[p] for p in moved) if moved and all(p in nmt for p in moved) else nxt["taken_at"])
        until = min(ends) if ends else None
        current = until is None
        pts.append({**s, "since": since, "until": now if current else until, "current": current,
                    "exact": dated and (bool(gate_end) or current)})
    prints = _prints(con, key, url)
    for p in pts:
        lo, hi = p["since"], p["until"] or now
        inside = [x for x in prints if lo and x["start"] and x["end"] and lo <= x["start"] and x["end"] <= hi]
        done = [x for x in inside if x["status"] == "completed"]
        p["prints"] = {"completed": len(done), "failed": sum(x["status"] in ("error", "failed") for x in inside),
                       "cancelled": sum(x["status"] == "cancelled" for x in inside),
                       "good": sum(x["quality"] == "good" for x in done), "bad": sum(x["quality"] == "bad" for x in done),
                       "last": done and {"file": done[-1]["file"], "finished_at": done[-1]["end"]}}
        p["evidence"] = ("none" if not done else "exact" if p["exact"] else "likely")
        p["labels"] = (["origin"] if p["kind"] == "origin" else []) + (["current"] if p.get("current") else [])
    good = [p for p in pts if p["prints"]["completed"] and not p["prints"]["bad"]]
    if good:
        best = max(good, key=lambda p: (p["prints"]["last"]["finished_at"], p["kind"] == "live"))
        best["labels"].append("last known good")
    out = []
    for p in sorted(pts, key=lambda p: (p["since"] or 0, p["kind"] == "live", p["id"]), reverse=True):
        out.append({k: p.get(k) for k in ("id", "kind", "actor", "taken_at", "since", "until", "current", "labels",
                                          "prints", "evidence", "note")} |
                   {"files": len(p["files"]), "sources": (p.get("meta") or {}).get("sources") if p["kind"] == "origin" else None})
    return out


# ---------------------------------------------------------------- restore

def compare(con, key: str, url: str, snap_id: int) -> dict:
    """What restoring this snapshot would change on the printer now."""
    snap = configs.get(con, snap_id)
    if not snap or snap["printer"] != key:
        raise LookupError(f"no snapshot {snap_id} for {key}")
    live = configs.live(url)
    changed = [p for p, d in snap["files"].items() if live.get(p, (None,))[0] != d]
    return {"snapshot": snap, "changed": changed, "created": [p for p in changed if p not in live],
            "left_in_place": sorted(p for p in live if p not in snap["files"])}


def diff(con, key: str, url: str, snap_id: int) -> str:
    c = compare(con, key, url, snap_id)
    out = []
    for p in c["changed"]:
        try:
            now = configs.read_file(url, p)
        except Exception:
            now = None
        import difflib
        a = (now or b"").decode("utf-8", "replace").splitlines(keepends=True)
        b = configs.blob(key, c["snapshot"]["files"][p]).decode("utf-8", "replace").splitlines(keepends=True)
        out.append("".join(difflib.unified_diff(a, b, f"live/{p}", f"snapshot-{snap_id}/{p}", n=2)))
    if c["left_in_place"]:
        out.append("# left in place (the restored config doesn't use them unless it [include]s them): "
                   + ", ".join(c["left_in_place"]) + "\n")
    return "\n".join(out) or "(the printer already runs this config)"


def stage_restore(con, key: str, url: str, snap_id: int, author: str, reason: str = "") -> dict:
    """Stage one change per differing file as one batch; applied together by a person through the gate."""
    c = compare(con, key, url, snap_id)
    if not c["changed"]:
        raise cfgchange.Refused("the printer already runs this config - nothing to restore")
    snap = c["snapshot"]
    label = "the original config" if snap["kind"] == "origin" else f"snapshot {snap_id} ({_day(snap['taken_at'])})"
    why = (reason.strip() or f"restore {label}")[:400]
    order = sorted(c["changed"], key=lambda p: (p not in c["created"], p == "printer.cfg", p))
    tag = f"restore-{snap_id}-{int(time.time())}"
    ids = []
    with db.tx(con):
        for p in order:
            try:
                text = configs.blob(key, snap["files"][p]).decode("utf-8")
            except UnicodeDecodeError:
                raise cfgchange.Refused(f"{p} in snapshot {snap_id} isn't text - restore it by hand")
            ch = cfgchange.stage(con, key, url, p, new_content=text, reason=why, author=author)
            con.execute("UPDATE config_changes SET batch=?, kind='restore', summary=? WHERE id=?",
                        (tag, f"{p}: restore {label}", ch["id"]))
            ids.append(ch["id"])
    return {"batch": tag, "changes": ids, "files": order, "left_in_place": c["left_in_place"]}
