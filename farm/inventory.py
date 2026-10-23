"""Inventory: every artefact the farm has made or holds, and where it printed - the start of the product portfolio.

An artefact is one deliverable thing: a maker job's models (kind "model"), a G-code file that lives on a printer
(kind "gcode": sliced by hand on the Windows box, migrated from an SBC, ...), a file imported from the NAS, or a
bought/non-printed item. A print is one attempt of an artefact on a printer (Moonraker history, the tracker).

Read-only towards the printers: backfill() lists files, metadata and history over Moonraker and never writes.
Artefacts are grouped into designs by design_key(), so the five re-slices of one model are one card, and a
printer's "Al-Quddus_180mm_12h16m_0.28mm_220C_PLA_ENDER3S1PLUS.gcode" meets the NAS's "Al-Quddus_180mm.3mf".
"""
import json
import re
import time
import urllib.parse
from pathlib import Path

from . import db, net, printers

STATUS = {"completed": "completed", "cancelled": "cancelled", "in_progress": "printing", "error": "failed",
          "klippy_shutdown": "failed", "klippy_disconnect": "failed", "server_exit": "failed", "interrupted": "failed"}
META_KEYS = ("slicer", "slicer_version", "estimated_time", "layer_height", "first_layer_height", "nozzle_diameter",
             "object_height", "filament_total", "filament_weight_total", "filament_type", "filament_name")
# PrusaSlicer's default output name: <input>_<print time>_<layer>mm_<temp>C_<material>_<printer>
_SLICER_TAIL = re.compile(r"_(\d+d)?(\d+h)?\d+m_[\d.]+mm_\d+C_[A-Za-z0-9+.-]+_[A-Za-z0-9]*$")
_FARM_TAIL = re.compile(r"_[\d.]+mm_[a-z]+_[A-Za-z0-9]+$")          # farm runs: A01_0.4mm_petg_Ender3MaxNeo
_COPY = re.compile(r"(\s*\(\d+\)|\s*-\s*copy|_copy)+$", re.I)
LABEL = re.compile(r"^[A-Z]{1,2}\d{2}$")


class Refused(Exception):
    pass


def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")):
        raise Refused("only a person marks prints or makes products")


def title_of(name: str) -> str:
    """The design's own name: no folder, extension, slicer suffix or '(2)' copy marker."""
    stem = Path(name).name
    stem = stem[: -len(Path(stem).suffix)] if Path(stem).suffix.lower() in (
        ".gcode", ".stl", ".3mf", ".obj", ".step", ".stp", ".scad", ".bgcode", ".f3d", ".fcstd", ".cxprj") else stem
    stem = _FARM_TAIL.sub("", _SLICER_TAIL.sub("", stem))
    return _COPY.sub("", stem).strip() or stem


def design_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title_of(name).lower()).strip()


def labels_in(title: str) -> list[str]:
    parts = title.split("+")
    return parts if all(LABEL.match(p) for p in parts) else []


# ---------------------------------------------------------------- writing artefacts and prints

def artefact_for_job(con, job: dict) -> int:
    r = con.execute("SELECT id FROM artefacts WHERE job_id=?", (job["id"],)).fetchone()
    if r:
        return r[0]
    order = con.execute("SELECT title FROM orders WHERE id=?", (job["order_id"],)).fetchone()
    m = con.execute("SELECT path, sha256 FROM models WHERE job_id=? ORDER BY id LIMIT 1", (job["id"],)).fetchone()
    design = job["label"] if job["maker"] in ("upload", "nas") else (order[0] if order else job["label"])
    return con.execute(
        "INSERT INTO artefacts (kind, title, design, maker, job_id, path, sha256, meta, created_at) "
        "VALUES ('model',?,?,?,?,?,?,?,?)",
        (job["label"], design_key(design) or design, job["maker"], job["id"], m and m[0], m and m[1],
         db.dumps({"labels": labels_in(job["label"]), "order": order and order[0]}), db.now())).lastrowid


def job_artefacts(con) -> int:
    """One artefact per generated job (pipeline.generate_job calls this too). Returns how many were new."""
    n = 0
    for j in con.execute("SELECT j.* FROM jobs j WHERE EXISTS (SELECT 1 FROM models m WHERE m.job_id=j.id) "
                         "AND NOT EXISTS (SELECT 1 FROM artefacts a WHERE a.job_id=j.id)").fetchall():
        artefact_for_job(con, db.row(j))
        n += 1
    return n


def _gcode_artefact(con, printer: str, path: str, modified: float | None, meta: dict | None) -> int:
    r = con.execute("SELECT id, modified FROM artefacts WHERE printer=? AND remote_path=? AND job_id IS NULL",
                    (printer, path)).fetchone()
    title = title_of(path)
    folder = path.split("/")[1] if path.startswith("farm/") and path.count("/") >= 2 else None
    design = folder or design_key(path) or title
    if r:
        if meta is not None:
            con.execute("UPDATE artefacts SET meta=?, modified=? WHERE id=?", (db.dumps(meta), modified, r[0]))
        return r[0]
    return con.execute(
        "INSERT INTO artefacts (kind, title, design, maker, printer, remote_path, meta, modified, created_at) "
        "VALUES ('gcode',?,?,?,?,?,?,?,?)",
        (title, design, "farm" if folder else "printer-file", printer, path,
         db.dumps({**(meta or {}), "labels": labels_in(title)}), modified, db.now())).lastrowid


def _build_artefacts(con, printer: str, remote_path: str, started: float) -> tuple[int | None, list[int]]:
    """A farm build uploaded under this name: (build id, its jobs' artefacts)."""
    b = con.execute("SELECT id FROM builds WHERE printer=? AND remote_path=? AND (started_at IS NULL OR "
                    "started_at <= ?) ORDER BY id DESC LIMIT 1", (printer, remote_path, started + 300)).fetchone()
    if not b:
        return None, []
    jids = [r[0] for r in con.execute("SELECT job_id FROM build_jobs WHERE build_id=?", (b[0],))]
    return b[0], [artefact_for_job(con, db.row(con.execute("SELECT * FROM jobs WHERE id=?", (j,)).fetchone()))
                  for j in jids if con.execute("SELECT 1 FROM models WHERE job_id=?", (j,)).fetchone()]


def record(con, artefact_id: int, printer: str, entry: dict, source: str, build_id: int | None = None) -> None:
    """Upsert one Moonraker history entry as a print (a person's quality mark survives)."""
    status = STATUS.get(entry.get("status"), entry.get("status") or "unknown")
    con.execute(
        "INSERT INTO prints (artefact_id, build_id, printer, file, started_at, finished_at, status, duration_s, "
        "filament_mm, source) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (artefact_id, printer, file, started_at) "
        "DO UPDATE SET finished_at=excluded.finished_at, status=excluded.status, duration_s=excluded.duration_s, "
        "filament_mm=excluded.filament_mm, build_id=COALESCE(prints.build_id, excluded.build_id)",
        (artefact_id, build_id, printer, entry.get("filename", ""), float(entry.get("start_time") or 0),
         entry.get("end_time"), status, entry.get("print_duration"), entry.get("filament_used"), source))


def record_build(con, build: dict, entry: dict) -> None:
    """The tracker saw a farm build end: a print for each artefact on it."""
    for j in con.execute("SELECT job_id FROM build_jobs WHERE build_id=?", (build["id"],)).fetchall():
        job = db.row(con.execute("SELECT * FROM jobs WHERE id=?", (j[0],)).fetchone())
        if con.execute("SELECT 1 FROM models WHERE job_id=?", (job["id"],)).fetchone():
            record(con, artefact_for_job(con, job), build["printer"],
                   {**entry, "filename": build["remote_path"] or entry.get("filename", "")}, "tracker", build["id"])


# ---------------------------------------------------------------- backfill from the printers (read-only)

def _history_all(p, since: float):
    start = 0
    while True:
        r = net.get_json(f"{p.moonraker}/server/history/list?limit=100&start={start}&order=asc&since={since:.0f}",
                         timeout=15)["result"]
        jobs = r.get("jobs") or []
        yield from jobs
        if len(jobs) < 100:
            return
        start += len(jobs)


def _meta(p, path: str) -> dict:
    try:
        m = net.get_json(f"{p.moonraker}/server/files/metadata?filename={urllib.parse.quote(path)}", timeout=10)["result"]
    except Exception:
        return {}
    out = {k: m[k] for k in META_KEYS if m.get(k) is not None}
    thumbs = sorted(m.get("thumbnails") or [], key=lambda t: t.get("width", 0) * t.get("height", 0))
    if thumbs:
        d = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        out["thumb"] = d + thumbs[-1]["relative_path"]
    return out


def backfill_printer(con, p, log=print) -> dict:
    """One printer: its G-code files (+ slicer metadata) and its whole print history."""
    files = net.get_json(f"{p.moonraker}/server/files/list?root=gcodes", timeout=15)["result"]
    builds = {r[0] for r in con.execute("SELECT remote_path FROM builds WHERE printer=? AND remote_path IS NOT NULL",
                                        (p.name,))}
    new_files = 0
    for f in files:
        if f["path"] in builds or not f["path"].lower().endswith((".gcode", ".bgcode")):
            continue
        known = con.execute("SELECT modified FROM artefacts WHERE printer=? AND remote_path=? AND job_id IS NULL",
                            (p.name, f["path"])).fetchone()
        if known and known[0] == f.get("modified"):
            continue
        with db.tx(con):
            _gcode_artefact(con, p.name, f["path"], f.get("modified"), _meta(p, f["path"]))
        new_files += 1 if not known else 0
    last = con.execute("SELECT MAX(started_at) FROM prints WHERE printer=? AND status <> 'printing'",
                       (p.name,)).fetchone()[0]
    n = 0
    with db.tx(con):
        for e in _history_all(p, max(0, (last or 0) - 86400)):
            fn = e.get("filename") or ""
            bid, arts = _build_artefacts(con, p.name, fn, float(e.get("start_time") or 0))
            if not arts:
                meta = {k: v for k, v in (e.get("metadata") or {}).items() if k in META_KEYS}
                arts = [_gcode_artefact(con, p.name, fn, None, None if e.get("exists", True) else
                                        {**meta, "deleted": True})]
            for a in arts:
                record(con, a, p.name, e, "tracker" if bid else "history", bid)
            n += 1
    log(f"inventory: {p.name}: {len(files)} files ({new_files} new), {n} history entries")
    return {"files": len(files), "new_files": new_files, "history": n}


def backfill(con, log=print) -> dict:
    """Idempotent: job artefacts, then every registry printer (watch-only ones too: this only reads)."""
    out = {"job_artefacts": job_artefacts(con)}
    printers.refresh()
    for p in printers.PRINTERS.values():
        try:
            out[p.name] = backfill_printer(con, p, log)
        except Exception as e:
            log(f"inventory: {p.name} skipped ({type(e).__name__}: {str(e)[:100]})")
            out[p.name] = {"error": str(e)[:100]}
    return out


def thumb(con, artefact_id: int) -> tuple[bytes, str] | None:
    a = con.execute("SELECT printer, meta FROM artefacts WHERE id=?", (artefact_id,)).fetchone()
    if not a or not a[0] or a[0] not in printers.PRINTERS:
        return None
    t = json.loads(a[1] or "{}").get("thumb")
    if not t:
        return None
    status, headers, body = net.get_bytes(
        f"{printers.PRINTERS[a[0]].moonraker}/server/files/gcodes/{urllib.parse.quote(t)}", timeout=8)
    return (body, headers.get("Content-Type", "image/png")) if status == 200 else None


# ---------------------------------------------------------------- people: quality marks

def mark(con, print_id: int, quality: str | None, actor: str, note: str = "") -> dict:
    _person(actor)
    if quality not in ("good", "bad", None, ""):
        raise ValueError("quality: good, bad or empty")
    if not con.execute("SELECT 1 FROM prints WHERE id=?", (print_id,)).fetchone():
        raise LookupError(f"no print {print_id}")
    con.execute("UPDATE prints SET quality=?, marked_by=?, marked_at=?, note=? WHERE id=?",
                (quality or None, actor, time.time(), note[:300], print_id))
    return db.row(con.execute("SELECT * FROM prints WHERE id=?", (print_id,)).fetchone())


# ---------------------------------------------------------------- the Inventory tab

def _projects_of(con) -> dict:
    """artefact id -> project names (a job made for a project part, or an order filed under a project)."""
    out = {}
    for aid, name in con.execute(
            "SELECT a.id, p.name FROM artefacts a JOIN jobs j ON j.id = a.job_id "
            "LEFT JOIN project_parts pp ON pp.id = j.part_id LEFT JOIN project_groups g ON g.id = pp.group_id "
            "JOIN orders o ON o.id = j.order_id "
            "JOIN projects p ON p.id = COALESCE(g.project_id, o.project_id)"):
        out.setdefault(aid, set()).add(name)
    for aid, name in con.execute(
            "SELECT pp.artefact_id, p.name FROM project_parts pp JOIN project_groups g ON g.id = pp.group_id "
            "JOIN projects p ON p.id = g.project_id WHERE pp.artefact_id IS NOT NULL AND pp.removed = 0"):
        out.setdefault(aid, set()).add(name)
    return out


def designs(con) -> list[dict]:
    """Artefacts folded into designs, with per-printer print counts. Newest print first."""
    projs = _projects_of(con)
    counts = {}
    for aid, printer, status, quality, n, last in con.execute(
            "SELECT artefact_id, printer, status, quality, COUNT(*), MAX(started_at) FROM prints "
            "GROUP BY artefact_id, printer, status, quality"):
        counts.setdefault(aid, []).append((printer, status, quality, n, last))
    products = {r["artefact_id"]: db.row(r) for r in con.execute("SELECT * FROM products ORDER BY id")}
    nas = {}
    for path, design in con.execute("SELECT path, design FROM nas_files WHERE gone = 0"):
        nas.setdefault(design, []).append(path)
    out = {}
    for a in con.execute("SELECT * FROM artefacts ORDER BY id"):
        a = db.row(a)
        name = a["design"].capitalize() if a["maker"] == "farm" else a["title"]   # farm/<order>/ files: the order
        d = out.setdefault(a["design"], {"design": a["design"], "title": name, "makers": set(), "kinds": set(),
                                        "artefacts": [], "printers": {}, "projects": set(), "products": [],
                                        "last": 0, "nas": nas.get(a["design"], [])})
        meta = a["meta"] if isinstance(a["meta"], dict) else json.loads(a["meta"] or "{}")
        d["makers"].add(a["maker"])
        d["kinds"].add(a["kind"])
        d["projects"] |= projs.get(a["id"], set())
        if a["id"] in products:
            d["products"].append(products[a["id"]])
        d["artefacts"].append({"id": a["id"], "kind": a["kind"], "title": a["title"], "maker": a["maker"],
                               "printer": a["printer"], "remote_path": a["remote_path"], "job_id": a["job_id"],
                               "thumb": bool(meta.get("thumb")), "deleted": bool(meta.get("deleted")),
                               "layer_height": meta.get("layer_height"), "estimated_time": meta.get("estimated_time"),
                               "grams": meta.get("filament_weight_total"), "labels": meta.get("labels") or []})
        if a["printer"]:
            d["printers"].setdefault(a["printer"], {"files": 0, "completed": 0, "failed": 0, "cancelled": 0,
                                                    "good": 0, "bad": 0})["files"] += 1
        for printer, status, quality, n, last in counts.get(a["id"], []):
            c = d["printers"].setdefault(printer, {"files": 0, "completed": 0, "failed": 0, "cancelled": 0,
                                                   "good": 0, "bad": 0})
            if status in c:
                c[status] += n
            if quality in ("good", "bad"):
                c[quality] += n
            d["last"] = max(d["last"], last or 0)
    res = []
    for d in out.values():
        d["makers"], d["kinds"], d["projects"] = sorted(d["makers"]), sorted(d["kinds"]), sorted(d["projects"])
        d["ok"] = sum(c["completed"] - c["bad"] for c in d["printers"].values())
        res.append(d)
    return sorted(res, key=lambda d: (-d["last"], d["title"].lower()))


def listing(con, by: str = "printer", q: str = "", printed_only: bool = False, products_only: bool = False) -> dict:
    ds = designs(con)
    if q:
        ql = q.lower()
        ds = [d for d in ds if ql in d["title"].lower() or ql in d["design"] or
              any(ql in (a["remote_path"] or "").lower() for a in d["artefacts"])]
    if printed_only:
        ds = [d for d in ds if d["ok"] > 0]
    if products_only:
        ds = [d for d in ds if d["products"]]
    groups = {}
    for d in ds:
        keys = (list(d["printers"]) if by == "printer" else d["projects"] if by == "project"
                else d["makers"]) or ["(none)"]
        for k in keys:
            groups.setdefault(k, []).append(d)
    order = sorted(groups, key=lambda k: (k == "(none)", -len(groups[k]), k))
    return {"by": by, "total": len(ds), "groups": [{"key": k, "designs": groups[k]} for k in order]}


def prints_of(con, design: str) -> list[dict]:
    return [db.row(r) for r in con.execute(
        "SELECT p.*, a.title, a.kind FROM prints p JOIN artefacts a ON a.id = p.artefact_id WHERE a.design=? "
        "ORDER BY p.started_at DESC LIMIT 200", (design,))]


# ---------------------------------------------------------------- the portfolio: products a person promoted

PRODUCTS_DIR = Path(__import__("os").environ.get("FARM_PRODUCTS_DIR", printers.REPO / "data" / "products"))


def proven(con, design: str) -> dict:
    """What it was last printed well with: the newest good print, else the newest completed one."""
    r = con.execute(
        "SELECT p.printer, a.meta, b.profile, b.layer_height FROM prints p JOIN artefacts a ON a.id = p.artefact_id "
        "LEFT JOIN builds b ON b.id = p.build_id WHERE a.design=? AND p.status='completed' AND "
        "COALESCE(p.quality,'') <> 'bad' ORDER BY (p.quality = 'good') DESC, p.started_at DESC LIMIT 1", (design,)).fetchone()
    if not r:
        return {}
    meta = json.loads(r[1] or "{}")
    return {"printer": r[0], "profile": r[2], "layer_height": r[3] or meta.get("layer_height")}


def promote(con, artefact_id: int, body: dict, actor: str, photo: tuple[bytes, str] | None = None) -> dict:
    """Make (or update) the product for an artefact. photo: (bytes, extension)."""
    _person(actor)
    a = con.execute("SELECT design FROM artefacts WHERE id=?", (artefact_id,)).fetchone()
    if not a:
        raise LookupError(f"no artefact {artefact_id}")
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("a product needs a name")
    pv = proven(con, a[0])
    row = {"name": name[:120], "description": str(body.get("description") or "")[:2000],
           "printer": body.get("printer") or pv.get("printer"), "profile": body.get("profile") or pv.get("profile"),
           "layer_height": float(body["layer_height"]) if body.get("layer_height") else pv.get("layer_height")}
    old = con.execute("SELECT id, photo FROM products WHERE artefact_id=?", (artefact_id,)).fetchone()
    with db.tx(con):
        if old:
            pid = old[0]
            con.execute("UPDATE products SET name=?, description=?, printer=?, profile=?, layer_height=? WHERE id=?",
                        (row["name"], row["description"], row["printer"], row["profile"], row["layer_height"], pid))
        else:
            pid = con.execute("INSERT INTO products (artefact_id, name, description, printer, profile, layer_height, "
                              "created_by, created_at) VALUES (?,?,?,?,?,?,?,?)",
                              (artefact_id, row["name"], row["description"], row["printer"], row["profile"],
                               row["layer_height"], actor, db.now())).lastrowid
        if photo:
            data, ext = photo
            ext = ext.lower().lstrip(".")
            if ext not in ("jpg", "jpeg", "png", "webp") or len(data) < 100:
                raise ValueError("photo: a jpg, png or webp image")
            PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
            dest = PRODUCTS_DIR / f"{pid}.{ext}"
            for f in PRODUCTS_DIR.glob(f"{pid}.*"):
                f.unlink()
            dest.write_bytes(data)
            con.execute("UPDATE products SET photo=? WHERE id=?", (dest.name, pid))
    return db.row(con.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone())


def unpromote(con, product_id: int, actor: str) -> None:
    _person(actor)
    r = con.execute("SELECT photo FROM products WHERE id=?", (product_id,)).fetchone()
    if not r:
        raise LookupError(f"no product {product_id}")
    if r[0]:
        (PRODUCTS_DIR / r[0]).unlink(missing_ok=True)
    con.execute("DELETE FROM products WHERE id=?", (product_id,))


def product_photo(con, product_id: int) -> tuple[bytes, str] | None:
    r = con.execute("SELECT photo FROM products WHERE id=?", (product_id,)).fetchone()
    if not r or not r[0] or not (PRODUCTS_DIR / r[0]).is_file():
        return None
    ext = r[0].rsplit(".", 1)[-1]
    return (PRODUCTS_DIR / r[0]).read_bytes(), {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, f"image/{ext}")
