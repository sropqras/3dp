"""The OMV NAS's project files as a catalogue: what exists, which were sent to and printed on the farm's printers,
and a one-file Import into farm work. Nothing is copied in bulk.

scan() lists \\\\OMV\\3dprinting\\3dprinting\\Project Files over the OMV rsync daemon (anonymous, read-only,
`rsync -r --list-only`: names, sizes and dates only). Each file gets inventory.design_key(), the same key a
printer's G-code gets, so a NAS "Al-Quddus_180mm.3mf" meets "Al-Quddus_180mm_12h16m_0.28mm_..._ENDER3S1PLUS.gcode".
import_file() fetches ONE file with rsync into data/nas/ and makes it an Inventory artefact (maker "nas"); from
there it is sent to a printer through the upload maker and the normal gates.
"""
import os
import re
import subprocess
import time
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
    r = subprocess.run(["rsync", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode not in (0, 23, 24):   # 23/24: some files vanished or were unreadable - the rest still counts
        raise RuntimeError(f"rsync exit {r.returncode}: {r.stderr.strip()[:200]}")
    return r.stdout


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


def scan(con, log=print, listing: str | None = None) -> dict:
    """Refresh nas_files from the NAS (listing: tests pass rsync's output). Files no longer there are marked gone."""
    files = parse_listing(listing if listing is not None else _rsync("-r", "--list-only", NAS_URL))
    if listing is None:
        try:
            files += parse_listing(_rsync("-r", "--list-only", PHOTOS_URL, timeout=120), PHOTOS)
        except Exception as e:      # the photos share is a nice-to-have
            log(f"nas: photos not listed ({str(e)[:80]})")
    t = time.time()
    with db.tx(con):
        for f in files:
            con.execute("INSERT INTO nas_files (path, size, mtime, ext, folder, design, seen_at, gone) "
                        "VALUES (?,?,?,?,?,?,?,0) ON CONFLICT (path) DO UPDATE SET size=excluded.size, "
                        "mtime=excluded.mtime, design=excluded.design, seen_at=excluded.seen_at, gone=0",
                        (f["path"], f["size"], f["mtime"], f["ext"], f["folder"], f["design"], t))
        gone = con.execute("UPDATE nas_files SET gone=1 WHERE seen_at < ? AND gone=0", (t,)).rowcount
    models = sum(f["ext"] in MODELS for f in files)
    log(f"nas: {models} model files and {len(files) - models} images catalogued, {gone} gone")
    return {"files": models, "images": len(files) - models, "gone": gone}


def matrix(con, q: str = "", matched_only: bool = False, folder: str = "", limit: int = 3000) -> dict:
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
        rows.append({**r, "printers": d["printers"] if d else {}, "ok": d["ok"] if d else 0,
                     "printable": r["ext"] in PRINTABLE})
    folders = [f for (f,) in con.execute("SELECT DISTINCT folder FROM nas_files WHERE gone=0 AND design <> '' "
                                         "ORDER BY folder")]
    stats = con.execute("SELECT COUNT(*), MAX(seen_at) FROM nas_files WHERE gone=0 AND design <> ''").fetchone()
    return {"printers": list(printers.PRINTERS), "folders": folders, "total": stats[0], "scanned_at": stats[1],
            "matched": sum(1 for r in rows if r["printers"]), "rows": rows[:limit], "truncated": len(rows) > limit,
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
