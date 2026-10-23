"""The OMV NAS's project files as a catalogue: what exists, which were sent to and printed on the farm's printers,
and a one-file Import into farm work. Nothing is copied in bulk.

scan() lists \\\\OMV\\3dprinting\\3dprinting\\Project Files over the OMV rsync daemon (anonymous, read-only,
`rsync -r --list-only`: names, sizes and dates only). Each file gets inventory.design_key(), the same key a
printer's G-code gets, so a NAS "Al-Quddus_180mm.3mf" meets "Al-Quddus_180mm_12h16m_0.28mm_..._ENDER3S1PLUS.gcode".
import_file() fetches ONE file with rsync into data/nas/ and makes it an Inventory artefact (maker "nas"); from
there it is sent to a printer through the upload maker and the normal gates.
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import zlib
from datetime import datetime
from pathlib import Path

from . import db, inventory
from .printers import REPO

NAS_URL = os.environ.get("FARM_NAS_URL", "rsync://192.168.1.121/3dprinting/3dprinting/Project Files/")
PHOTOS_URL = os.environ.get("FARM_NAS_PHOTOS_URL", "rsync://192.168.1.121/3dprinting/3dprinting/ProductImages/")
PHOTOS = "@ProductImages/"          # catalogue prefix for the photos share (portfolio pictures)
IMAGES = {"jpg", "jpeg", "png", "webp"}
NAS_DIR = Path(os.environ.get("FARM_NAS_DIR", REPO / "data" / "nas"))
MODELS = {"stl", "3mf", "obj", "step", "stp", "scad", "gcode", "bgcode", "f3d", "fcstd", "cxprj", "amf"}
PRINTABLE = {"stl", "3mf"}
_LINE = re.compile(r"^(\S+)\s+([\d,]+)\s+(\d{4}/\d\d/\d\d \d\d:\d\d:\d\d)\s(.+)$")


def _rsync(*args, timeout=600) -> str:
    return _rsync_rc(*args, timeout=timeout)[0]


def _rsync_rc(*args, timeout=600) -> tuple[str, bool]:
    """(stdout, partial). 23/24: some files vanished or were unreadable - the rest still counts, but a partial
    listing must not mark anything gone."""
    r = subprocess.run(["rsync", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode not in (0, 23, 24):
        raise RuntimeError(f"rsync exit {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout, r.returncode != 0


def _kv(con, key: str, value=None):
    if value is None:
        r = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r and r[0] else None
    con.execute("INSERT INTO kv (key, value, at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "at=excluded.at", (key, json.dumps(value), time.time()))


def status(con) -> dict:
    """OMV online/offline as last seen by a scan. Offline is a state: the catalogue, thumbnails and previews keep
    working from the cache; fetching files (Import, full-resolution 3D) waits for it."""
    st = _kv(con, "nas_status") or {}
    return {"online": st.get("online", False), "last_ok": st.get("last_ok"), "last_error": st.get("last_error"),
            "error_at": st.get("error_at"), "source": NAS_URL}


def _url(path: str) -> str:
    """The rsync source for a catalogued path, wildcards escaped (a NAS name like 'box [v2].stl' is literal)."""
    root, rel = (PHOTOS_URL, path[len(PHOTOS):]) if path.startswith(PHOTOS) else (NAS_URL, path)
    return root + re.sub(r"([*?\[\]\\])", r"\\\1", rel)


def parse_listing(text: str, prefix: str = "") -> list[dict]:
    out = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m or m.group(1).startswith("d"):
            continue
        path = m.group(4)
        ext = path.rsplit(".", 1)[-1].lower() if "." in Path(path).name else ""
        if ext not in MODELS | IMAGES or "/." in "/" + path:
            continue
        path = prefix + path
        out.append({"path": path, "size": int(m.group(2).replace(",", "")), "ext": ext,
                    "mtime": datetime.strptime(m.group(3), "%Y/%m/%d %H:%M:%S").timestamp(),
                    "folder": path.split("/")[0] if "/" in path else "",
                    "design": "" if ext in IMAGES else inventory.design_key(path)})
    return out


def scan(con, log=print, listing: str | None = None, partial: bool = False) -> dict:
    """Refresh nas_files from the NAS (listing: tests pass rsync's output). Files no longer there are marked gone -
    only after a complete listing of both shares; a failed or partial one changes nothing (OMV down is not "all
    files deleted"). An unchanged listing (same hash) writes nothing but the status."""
    from . import nasindex
    st = _kv(con, "nas_status") or {}
    try:
        text, part = (listing, partial) if listing is not None else _rsync_rc("-r", "--list-only", NAS_URL)
    except Exception as e:
        _kv(con, "nas_status", {**st, "online": False, "last_error": str(e)[:200], "error_at": time.time()})
        raise
    files = parse_listing(text)
    if listing is None:
        try:
            ptext, ppart = _rsync_rc("-r", "--list-only", PHOTOS_URL, timeout=120)
            files += parse_listing(ptext, PHOTOS)
            part = part or ppart
        except Exception as e:      # the photos share is a nice-to-have, but then nothing of it may go "gone"
            log(f"nas: photos not listed ({str(e)[:80]})")
            part = True
    digest = hashlib.sha1("\n".join(sorted(f"{f['path']}|{f['size']}|{f['mtime']}" for f in files)).encode()).hexdigest()
    _kv(con, "nas_status", {**st, "online": True, "last_ok": time.time(), "partial": part})
    if digest == (_kv(con, "nas_listing") or {}).get("hash") and not part:
        nasindex.resolve(con)       # a rule may have changed since; cheap when nothing did
        log(f"nas: unchanged ({len(files)} files)")
        return {"files": sum(f["ext"] in MODELS for f in files), "images": sum(f["ext"] in IMAGES for f in files),
                "gone": 0, "new": [], "unchanged": True}
    t = time.time()
    first_scan = con.execute("SELECT COUNT(*) FROM nas_files").fetchone()[0] == 0
    before = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, size, mtime FROM nas_files")}
    with db.tx(con):
        known = set(before)
        # files catalogued before first_seen existed count as old, not new
        con.execute("UPDATE nas_files SET first_seen=0 WHERE first_seen IS NULL")
        for f in files:
            con.execute("INSERT INTO nas_files (path, size, mtime, ext, folder, design, seen_at, gone, first_seen) "
                        "VALUES (?,?,?,?,?,?,?,0,?) ON CONFLICT (path) DO UPDATE SET size=excluded.size, "
                        "mtime=excluded.mtime, design=excluded.design, seen_at=excluded.seen_at, gone=0",
                        (f["path"], f["size"], f["mtime"], f["ext"], f["folder"], f["design"], t, 0 if first_scan else t))
        gone = 0 if part else con.execute("UPDATE nas_files SET gone=1 WHERE seen_at < ? AND gone=0", (t,)).rowcount
    if not part:
        _kv(con, "nas_listing", {"hash": digest, "at": t})
    untagged = con.execute("SELECT COUNT(*) FROM nas_tags").fetchone()[0] == 0
    nasindex.retag(con, [f for f in files if untagged or before.get(f["path"]) != (f["size"], f["mtime"])])
    nasindex.resolve(con)
    models = sum(f["ext"] in MODELS for f in files)
    new = [] if first_scan else [f["path"] for f in files if f["path"] not in known and f["ext"] in MODELS]
    log(f"nas: {models} model files and {len(files) - models} images catalogued, {len(new)} new, {gone} gone")
    return {"files": models, "images": len(files) - models, "gone": gone, "new": new}


NEW_DAYS = 14


def matrix(con, q: str = "", matched_only: bool = False, folder: str = "", limit: int = 3000,
           new_only: bool = False) -> dict:
    """One row per NAS model file: which onboarded printers have it (sent) and how it printed there."""
    from . import printers
    designs = {d["design"]: d for d in inventory.designs(con)}
    rows = []
    for r in con.execute("SELECT * FROM nas_files WHERE gone=0 AND design <> '' ORDER BY folder, path"):
        r = db.row(r)
        d = designs.get(r["design"])
        if matched_only and not (d and d["printers"]):
            continue
        if folder and r["folder"] != folder:
            continue
        if q and q.lower() not in r["path"].lower():
            continue
        is_new = bool(r.get("first_seen")) and r["first_seen"] > time.time() - NEW_DAYS * 86400
        if new_only and not is_new:
            continue
        rows.append({**r, "printers": d["printers"] if d else {}, "ok": d["ok"] if d else 0,
                     "printable": r["ext"] in PRINTABLE, "new": is_new})
    folders = [f for (f,) in con.execute("SELECT DISTINCT folder FROM nas_files WHERE gone=0 AND design <> '' "
                                         "ORDER BY folder")]
    stats = con.execute("SELECT COUNT(*), MAX(seen_at) FROM nas_files WHERE gone=0 AND design <> ''").fetchone()
    return {"printers": list(printers.PRINTERS), "folders": folders, "total": stats[0], "scanned_at": stats[1],
            "matched": sum(1 for r in rows if r["printers"]), "rows": rows[:limit], "truncated": len(rows) > limit,
            "new": con.execute("SELECT COUNT(*) FROM nas_files WHERE gone=0 AND design <> '' AND first_seen > ?",
                               (time.time() - NEW_DAYS * 86400,)).fetchone()[0], "new_days": NEW_DAYS,
            "source": NAS_URL}


def import_file(con, path: str, actor: str) -> dict:
    """Fetch one catalogued STL/3MF from the NAS and make it an artefact ready for farm work."""
    r = con.execute("SELECT * FROM nas_files WHERE path=? AND gone=0", (path,)).fetchone()
    if not r:
        raise LookupError("not in the NAS catalogue (scan again?)")
    r = db.row(r)
    if r["ext"] not in PRINTABLE:
        raise ValueError(f".{r['ext']} can't be printed as is - export an STL or 3MF on the NAS first")
    dest = (NAS_DIR / path).resolve()
    if not str(dest).startswith(str(NAS_DIR.resolve()) + os.sep):
        raise ValueError("bad path")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _rsync("-t", _url(path), str(dest), timeout=300)
    title = inventory.title_of(path)
    with db.tx(con):
        aid = r["artefact_id"]
        if aid and con.execute("SELECT 1 FROM artefacts WHERE id=?", (aid,)).fetchone():
            con.execute("UPDATE artefacts SET path=?, modified=? WHERE id=?", (str(dest), r["mtime"], aid))
        else:
            aid = con.execute(
                "INSERT INTO artefacts (kind, title, design, maker, path, meta, modified, created_at) "
                "VALUES ('model',?,?,'nas',?,?,?,?)",
                (title, r["design"] or title, str(dest), db.dumps({"nas_path": path, "imported_by": actor}),
                 r["mtime"], db.now())).lastrowid
            con.execute("UPDATE nas_files SET artefact_id=? WHERE path=?", (aid, path))
    return {"artefact": aid, "title": title, "path": str(dest), "bytes": dest.stat().st_size}


def photos_for(con, design: str, limit: int = 60) -> list[dict]:
    """Pictures that may show this design: images in the folders holding its NAS models, then the photos share."""
    dirs = {str(Path(r[0]).parent) for r in con.execute("SELECT path FROM nas_files WHERE design=? AND gone=0", (design,))}
    near = [db.row(r) for r in con.execute("SELECT path, size FROM nas_files WHERE design='' AND gone=0 AND "
                                           "path NOT LIKE ? ORDER BY path", (PHOTOS + "%",))
            if str(Path(r[0]).parent) in dirs]
    share = [db.row(r) for r in con.execute("SELECT path, size FROM nas_files WHERE design='' AND gone=0 AND path LIKE ? "
                                            "ORDER BY mtime DESC", (PHOTOS + "%",))]
    return [{**x, "near": True} for x in near][:limit] + [{**x, "near": False} for x in share][:max(0, limit - len(near))]


def fetch_image(con, path: str, dest: Path) -> Path:
    """One catalogued picture from the NAS (a product photo)."""
    r = con.execute("SELECT ext FROM nas_files WHERE path=? AND gone=0", (path,)).fetchone()
    if not r or r[0] not in IMAGES:
        raise LookupError("not a catalogued picture")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _rsync("-t", _url(path), str(dest), timeout=120)
    return dest


# ---------------------------------------------------------------- previews (farm/meshprev.py)
# Kept: a thumbnail + a light preview mesh per model (~100 KB), under data/nas/_thumbs and _preview, keyed by the
# NAS path and valid while its size/mtime match. Originals are only held in the bounded _cache (full-resolution 3D)
# or as Import artefacts; the warmer deletes what it fetched.

THUMB_DIR, PREVIEW_DIR, CACHE_DIR = NAS_DIR / "_thumbs", NAS_DIR / "_preview", NAS_DIR / "_cache"
CACHE_MAX = int(os.environ.get("FARM_NAS_CACHE_GB", "10")) * 1024 ** 3
SYNC_MAX = 6 * 1024 ** 2          # a thumbnail asked for in the UI is built on the spot up to this size...
_BUILDING = threading.BoundedSemaphore(2)   # ...by at most two requests at once; the rest queue for the builder
WARM_MAX = 80 * 1024 ** 2


def _key(path: str) -> str:
    return hashlib.sha1(path.encode()).hexdigest()


def _file(con, path: str) -> dict:
    r = con.execute("SELECT * FROM nas_files WHERE path=? AND gone=0", (path,)).fetchone()
    if not r:
        raise LookupError("not in the NAS catalogue")
    return db.row(r)


def meta(con, path: str) -> dict | None:
    """The cached preview facts for a path, if still valid for the file on the NAS."""
    f = con.execute("SELECT size, mtime FROM nas_files WHERE path=?", (path,)).fetchone()
    m = con.execute("SELECT * FROM nas_meta WHERE path=?", (path,)).fetchone()
    if not f or not m or (m["size"], m["mtime"]) != (f[0], f[1]):
        return None
    return db.row(m)


def _local_copy(con, f: dict) -> Path | None:
    """A copy we already have: an Import, or the full-resolution cache."""
    if f["artefact_id"]:
        r = con.execute("SELECT path FROM artefacts WHERE id=?", (f["artefact_id"],)).fetchone()
        if r and r[0] and Path(r[0]).is_file() and Path(r[0]).stat().st_size == f["size"]:
            return Path(r[0])
    c = CACHE_DIR / (_key(f["path"]) + "." + f["ext"])
    return c if c.is_file() and c.stat().st_size == f["size"] else None


def build_preview(con, path: str, keep: bool = False) -> dict:
    """Fetch (unless we have a copy), build thumbnail + preview, store them, drop the fetched original unless
    `keep` (then it goes to the bounded cache)."""
    from . import meshprev
    f = _file(con, path)
    if f["ext"] not in PRINTABLE:
        raise ValueError(f".{f['ext']} has no preview (STL and 3MF only)")
    src, fetched = _local_copy(con, f), None
    if src is None:
        tmp = REPO / "data" / "tmp" / "nasprev"
        tmp.mkdir(parents=True, exist_ok=True)
        fetched = src = (CACHE_DIR if keep else tmp) / (_key(path) + "." + f["ext"])
        src.parent.mkdir(parents=True, exist_ok=True)
        _rsync("-t", _url(path), str(src), timeout=900)
    try:
        out = meshprev.build(src, Path(path).name)
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
        k = _key(path)
        (THUMB_DIR / f"{k}.png").write_bytes(out["thumb"])
        (PREVIEW_DIR / f"{k}.bin").write_bytes(zlib.compress(out["blob"], 6))
        con.execute("INSERT INTO nas_meta (path, size, mtime, thumb, preview, triangles, bbox, thumb_from, warmed_at, "
                    "error) VALUES (?,?,?,?,?,?,?,?,?,NULL) ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                    "mtime=excluded.mtime, thumb=excluded.thumb, preview=excluded.preview, triangles=excluded.triangles, "
                    "bbox=excluded.bbox, thumb_from=excluded.thumb_from, warmed_at=excluded.warmed_at, error=NULL",
                    (path, f["size"], f["mtime"], f"{k}.png", f"{k}.bin", out["triangles"], json.dumps(out["bbox"]),
                     out["thumb_from"], time.time()))
        return {"path": path, "triangles": out["triangles"], "thumb_from": out["thumb_from"]}
    except Exception as e:
        con.execute("INSERT INTO nas_meta (path, size, mtime, error, warmed_at) VALUES (?,?,?,?,?) ON CONFLICT(path) "
                    "DO UPDATE SET size=excluded.size, mtime=excluded.mtime, error=excluded.error, warmed_at=excluded.warmed_at",
                    (path, f["size"], f["mtime"], f"{type(e).__name__}: {str(e)[:200]}", time.time()))
        raise
    finally:
        if fetched is not None and not keep:
            fetched.unlink(missing_ok=True)
        if keep:
            trim_cache()


def trim_cache(limit: int = None) -> int:
    """Keep the full-resolution cache under its cap, oldest-used first."""
    limit = CACHE_MAX if limit is None else limit
    files = sorted((p for p in CACHE_DIR.glob("*") if p.is_file()), key=lambda p: p.stat().st_atime) if CACHE_DIR.is_dir() else []
    total, removed = sum(p.stat().st_size for p in files), 0
    while files and total > limit:
        p = files.pop(0)
        total -= p.stat().st_size
        p.unlink(missing_ok=True)
        removed += 1
    return removed


def thumb(con, path: str, build: bool = True) -> tuple[bytes, str] | None:
    """A model's thumbnail: cached; else built now (small files, OMV online); else a picture from its folder; else
    None (the page shows a type icon). A miss marks the file wanted, so the warmer does it first."""
    m = meta(con, path)
    if m and m["thumb"] and (THUMB_DIR / m["thumb"]).is_file():
        return (THUMB_DIR / m["thumb"]).read_bytes(), "image/png"
    f = _file(con, path)
    if build and f["ext"] in PRINTABLE and f["size"] <= SYNC_MAX and status(con)["online"] and not (m and m.get("error")) \
            and _BUILDING.acquire(blocking=False):
        try:
            build_preview(con, path)
            return thumb(con, path, build=False)
        except Exception:
            pass
        finally:
            _BUILDING.release()
    con.execute("INSERT INTO nas_meta (path, wanted) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET wanted=excluded.wanted",
                (path, time.time()))
    return None


def preview_blob(con, path: str, full: bool = False) -> bytes:
    """The viewer blob: the stored preview, or (full) the whole mesh from the bounded cache."""
    from . import meshprev
    if full:
        f = _file(con, path)
        src = _local_copy(con, f)
        if src is None:
            if not status(con)["online"]:
                raise RuntimeError("OMV is offline - the full model can't be fetched now (the preview still works)")
            src = CACHE_DIR / (_key(path) + "." + f["ext"])
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            _rsync("-t", _url(path), str(src), timeout=900)
            trim_cache()
        pos, idx = meshprev.read(src)
        return meshprev.to_blob(*meshprev.decimate(pos, idx, 400000), Path(path).name)
    m = meta(con, path)
    if not (m and m["preview"] and (PREVIEW_DIR / m["preview"]).is_file()):
        if not status(con)["online"]:
            raise RuntimeError("no preview cached yet and OMV is offline")
        build_preview(con, path)
        m = meta(con, path)
    return zlib.decompress((PREVIEW_DIR / m["preview"]).read_bytes())


def build_wanted(con, max_bytes: int = WARM_MAX) -> str | None:
    """One preview the UI asked for (newest request first), if OMV is up. The web server's builder thread calls
    this in a loop, so thumbnails a person is looking at appear within seconds without overloading the host."""
    if not status(con)["online"]:
        return None
    r = con.execute("SELECT m.path FROM nas_meta m JOIN nas_files f ON f.path = m.path WHERE m.wanted IS NOT NULL "
                    "AND m.thumb IS NULL AND m.error IS NULL AND f.gone=0 AND f.ext IN ('stl','3mf') AND f.size <= ? "
                    "ORDER BY m.wanted DESC LIMIT 1", (max_bytes,)).fetchone()
    if not r:
        return None
    try:
        build_preview(con, r[0])
    except Exception:
        pass                              # recorded in nas_meta.error; not retried until the nightly warm says so
    return r[0]


def warm(con, minutes: float = 30, max_bytes: int = WARM_MAX, log=print) -> dict:
    """Previews for models that have none (or whose file changed): wanted first, then new, then small. Stops at the
    time budget; the next run carries on. Files over max_bytes are left to on-demand."""
    t0, done, failed = time.time(), 0, 0
    todo = con.execute(
        "SELECT f.path FROM nas_files f LEFT JOIN nas_meta m ON m.path = f.path WHERE f.gone=0 AND f.ext IN ('stl','3mf') "
        "AND f.size <= ? AND (m.path IS NULL OR m.size IS NOT f.size OR m.mtime IS NOT f.mtime OR "
        "(m.thumb IS NULL AND (m.error IS NULL OR m.warmed_at < ?))) "
        "ORDER BY COALESCE(m.wanted, 0) DESC, COALESCE(f.first_seen, 0) DESC, f.size",
        (max_bytes, time.time() - 7 * 86400)).fetchall()
    for (path,) in todo:
        if time.time() - t0 > minutes * 60:
            break
        try:
            build_preview(con, path)
            done += 1
        except Exception as e:
            failed += 1
            log(f"nas warm: {path}: {type(e).__name__}: {str(e)[:100]}")
    left = len(todo) - done - failed
    log(f"nas warm: {done} previews built, {failed} failed, {left} left, {round(time.time() - t0)} s")
    return {"built": done, "failed": failed, "left": left}
