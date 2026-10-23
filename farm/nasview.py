"""The NAS catalogue as a browsable dataset: one query engine behind the grid, table and matrix views.

A spec (all optional) filters, sorts and pages the catalogued models:
    folder   OMV folder; the subtree under it          category  farm category id
    ext      ["stl", "3mf", ...]                       tags      ["thingiverse", "mat:PETG", ...] (all must match)
    printed  any | ok (printed OK somewhere) | never | failed
    printer  printed (or sent) on this printer         new / imported / pictures / dup   true = only those
    q        free text, with tokens: ext:3mf cat:orders printer:s1pro folder:Allah tag:thingiverse
    sort     name | folder | size | modified | first_seen | prints      desc  bool
    offset, limit (<= 500)
Facet counts are computed the usual way: each facet over the rows matching every OTHER filter, so a chip shows how
many you'd get by adding it. ~2k files: everything is done in memory from one read.
"""
import json
import re
import time
from pathlib import PurePosixPath

from . import db, inventory, nas, nasindex

SORTS = {"name": lambda r: r["name"].lower(), "folder": lambda r: (r["folder"].lower(), r["name"].lower()),
         "size": lambda r: r["size"] or 0, "modified": lambda r: r["mtime"] or 0,
         "first_seen": lambda r: r["first_seen"] or 0, "prints": lambda r: r["ok"]}
TOKEN = re.compile(r"\b(ext|cat|printer|folder|tag):(\"[^\"]+\"|\S+)", re.I)
_cache = {}


def _designs(con) -> dict:
    """inventory.designs by design key, cached briefly (the grid pages through the same rows)."""
    hit = _cache.get("designs")
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    out = {d["design"]: d for d in inventory.designs(con)}
    _cache["designs"] = (time.time(), out)
    return out


def rows(con) -> list[dict]:
    """Every catalogued model (not pictures), joined with its farm facts."""
    designs = _designs(con)
    tags = {}
    for path, tag in con.execute("SELECT path, tag FROM nas_tags"):
        tags.setdefault(path, []).append(tag)
    thumbs = {p: (t, pv) for p, t, pv in con.execute("SELECT path, thumb, preview FROM nas_meta WHERE thumb IS NOT NULL "
                                                     "OR preview IS NOT NULL")}
    files = [db.row(r) for r in con.execute("SELECT * FROM nas_files WHERE gone=0")]
    pic_dirs, by_design, by_name = set(), {}, {}
    for f in files:
        if f["design"] == "":
            pic_dirs.add(str(PurePosixPath(f["path"]).parent))
        else:
            by_design.setdefault(f["design"], []).append(f["path"])
            by_name.setdefault((PurePosixPath(f["path"]).name.lower(), f["size"]), []).append(f["path"])
    new_since = time.time() - nas.NEW_DAYS * 86400
    out = []
    for f in files:
        if f["design"] == "":
            continue
        d = designs.get(f["design"])
        p = PurePosixPath(f["path"])
        printers = d["printers"] if d else {}
        failed = sum(c.get("failed", 0) for c in printers.values())
        r = {**f, "name": p.name, "stem": p.stem, "folder": str(p.parent) if str(p.parent) != "." else "",
             "tags": sorted(tags.get(f["path"], [])), "printers": printers, "ok": d["ok"] if d else 0,
             "failed": failed, "printable": f["ext"] in nas.PRINTABLE, "imported": bool(f["artefact_id"]),
             "new": bool(f["first_seen"]) and f["first_seen"] > new_since,
             "pictures": str(p.parent) in pic_dirs,
             "dup": len(by_design.get(f["design"], [])) > 1 or len(by_name.get((p.name.lower(), f["size"]), [])) > 1,
             "thumb": bool(thumbs.get(f["path"], (None,))[0]), "preview": bool(thumbs.get(f["path"], (None, None))[1])}
        out.append(r)
    return out


def _parse_q(spec: dict) -> dict:
    """Pull ext:/cat:/printer:/folder:/tag: tokens out of the free text into the spec."""
    spec = dict(spec)
    q = spec.get("q") or ""
    for key, val in TOKEN.findall(q):
        val = val.strip('"')
        key = key.lower()
        if key == "ext":
            spec["ext"] = list(spec.get("ext") or []) + [val.lower().lstrip(".")]
        elif key == "tag":
            spec["tags"] = list(spec.get("tags") or []) + [val]
        elif key == "cat":
            spec["cat_name"] = val.lower()
        elif key == "folder":
            spec["folder_q"] = val.lower()           # typed: any folder whose path contains it
        else:
            spec[key] = val
    spec["text"] = TOKEN.sub("", q).strip().lower()
    return spec


def _match(r: dict, s: dict, skip: str = "", cats: dict | None = None) -> bool:
    if s.get("folder") and skip != "folder":
        f = s["folder"].strip("/")
        if not (r["folder"] == f or r["folder"].startswith(f + "/")):
            return False
    if s.get("folder_q") and skip != "folder" and s["folder_q"] not in r["folder"].lower():
        return False
    if s.get("category") not in (None, "") and skip != "category" and r["category_id"] != int(s["category"]):
        return False
    if s.get("cat_name") and skip != "category" and s["cat_name"] not in (cats or {}).get(r["category_id"], "").lower():
        return False
    if s.get("ext") and skip != "ext" and r["ext"] not in s["ext"]:
        return False
    if s.get("tags") and skip != "tags" and not set(s["tags"]) <= set(r["tags"]):
        return False
    pr = s.get("printed") or "any"
    if skip != "printed" and pr != "any":
        if pr == "ok" and r["ok"] <= 0 or pr == "never" and r["printers"] or pr == "failed" and not r["failed"]:
            return False
    if s.get("printer") and skip != "printer" and not any(s["printer"].lower() == p.lower() for p in r["printers"]):
        return False
    for flag in ("new", "imported", "pictures", "dup"):
        if s.get(flag) and skip != flag and not r[flag]:
            return False
    if s.get("text") and s["text"] not in r["path"].lower() and not any(s["text"] in t.lower() for t in r["tags"]):
        return False
    return True


def categories(con) -> list[dict]:
    counts = dict(con.execute("SELECT category_id, COUNT(*) FROM nas_files WHERE gone=0 AND design <> '' "
                              "GROUP BY category_id").fetchall())
    return [{**db.row(r), "count": counts.get(r["id"], 0)}
            for r in con.execute("SELECT * FROM nas_categories ORDER BY pos, name")]


def query(con, spec: dict) -> dict:
    s = _parse_q(spec)
    cats = {c["id"]: c["name"] for c in categories(con)}
    all_rows = rows(con)
    hit = [r for r in all_rows if _match(r, s, cats=cats)]
    key = SORTS.get(s.get("sort") or "name", SORTS["name"])
    hit.sort(key=key, reverse=bool(s.get("desc")))
    off, lim = max(0, int(s.get("offset") or 0)), min(500, max(1, int(s.get("limit") or 120)))

    def facet(name, values):
        sub = [r for r in all_rows if _match(r, s, skip=name, cats=cats)]
        out = {}
        for r in sub:
            for v in values(r):
                out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: (-kv[1], str(kv[0]))))

    facets = {
        "ext": facet("ext", lambda r: [r["ext"]]),
        "category": facet("category", lambda r: [r["category_id"]]),
        "printed": facet("printed", lambda r: ["ok"] * (r["ok"] > 0) + ["never"] * (not r["printers"])
                         + ["failed"] * (r["failed"] > 0)),
        "printer": facet("printer", lambda r: list(r["printers"])),
        "tags": dict(list(facet("tags", lambda r: r["tags"]).items())[:40]),
        **{f: sum(1 for r in all_rows if r[f] and _match(r, s, skip=f, cats=cats))
           for f in ("new", "imported", "pictures", "dup")},
    }
    return {"total": len(hit), "offset": off, "rows": hit[off:off + lim], "facets": facets,
            "categories": categories(con), "status": nas.status(con), "printers": sorted({p for r in all_rows
                                                                                             for p in r["printers"]})}


def tree(con) -> dict:
    """The OMV folder hierarchy with model counts (total / printed OK / new), pictures-only folders included."""
    root = {"name": "", "path": "", "count": 0, "ok": 0, "new": 0, "children": {}}
    for r in rows(con):
        node = root
        node["count"] += 1
        node["ok"] += r["ok"] > 0
        node["new"] += r["new"]
        acc = []
        for part in r["folder"].split("/") if r["folder"] else []:
            acc.append(part)
            node = node["children"].setdefault(part, {"name": part, "path": "/".join(acc), "count": 0, "ok": 0,
                                                      "new": 0, "children": {}})
            node["count"] += 1
            node["ok"] += r["ok"] > 0
            node["new"] += r["new"]

    def flat(n):
        return {**{k: v for k, v in n.items() if k != "children"},
                "children": [flat(c) for c in sorted(n["children"].values(), key=lambda c: c["name"].lower())]}
    return flat(root)


def pack(con, folder: str) -> dict:
    """A design pack: everything in one OMV folder - models, CAD sources, pictures - and how its designs printed."""
    folder = folder.strip("/")
    files = [db.row(r) for r in con.execute("SELECT * FROM nas_files WHERE gone=0 ORDER BY path")]
    mine = [f for f in files if str(PurePosixPath(f["path"]).parent).strip(".") == folder]
    designs = sorted({f["design"] for f in mine if f["design"]})
    prints = [p for d in designs for p in inventory.prints_of(con, d)][:100]
    tags = {}
    for path, tag in con.execute("SELECT path, tag FROM nas_tags WHERE path LIKE ?", (folder + "/%" if folder else "%",)):
        tags.setdefault(path, []).append(tag)
    links = sorted({nasindex.thing_url(t) for f in mine for t in tags.get(f["path"], []) if t.startswith("thing:")})
    return {"folder": folder, "models": [f for f in mine if f["design"]], "pictures": [f for f in mine if not f["design"]],
            "prints": prints, "links": links, "status": nas.status(con)}


# ---------------------------------------------------------------- a person's edits (never written to OMV)

def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")) or actor.split(":", 1)[1] in ("", "unknown"):
        raise PermissionError("put your name in the 'You' box first (catalogue edits are a person's)")


def category_edit(con, body: dict, actor: str) -> dict:
    """create {name, colour} | rename {id, name} | colour {id, colour} | move {id, pos} | delete {id}"""
    _person(actor)
    act = body.get("action")
    with db.tx(con):
        if act == "create":
            name = str(body.get("name") or "").strip()[:60]
            if not name:
                raise ValueError("name the category")
            pos = con.execute("SELECT COALESCE(MAX(pos), 0) + 1 FROM nas_categories").fetchone()[0]
            cid = con.execute("INSERT INTO nas_categories (name, colour, pos) VALUES (?,?,?)",
                              (name, str(body.get("colour") or "#607d8b")[:20], pos)).lastrowid
            return {"id": cid}
        cid = int(body["id"])
        row = con.execute("SELECT name FROM nas_categories WHERE id=?", (cid,)).fetchone()
        if not row:
            raise LookupError(f"no category {cid}")
        if act == "rename":
            con.execute("UPDATE nas_categories SET name=? WHERE id=?", (str(body["name"]).strip()[:60], cid))
        elif act == "colour":
            con.execute("UPDATE nas_categories SET colour=? WHERE id=?", (str(body["colour"])[:20], cid))
        elif act == "move":
            con.execute("UPDATE nas_categories SET pos=? WHERE id=?", (int(body["pos"]), cid))
        elif act == "delete":
            if row[0] == nasindex.UNCATEGORISED:
                raise ValueError("Uncategorised stays")
            con.execute("DELETE FROM nas_rules WHERE category_id=?", (cid,))
            con.execute("DELETE FROM nas_categories WHERE id=?", (cid,))
        else:
            raise ValueError("action: create, rename, colour, move or delete")
    nasindex.resolve(con)
    return {"id": cid, "action": act}


def assign(con, body: dict, actor: str) -> dict:
    """Put folders (their whole subtree) and/or single files into a category. A folder rule replaces any earlier
    rule for the same folder; a file rule is a per-file override."""
    _person(actor)
    cid = int(body["category"])
    if not con.execute("SELECT 1 FROM nas_categories WHERE id=?", (cid,)).fetchone():
        raise LookupError(f"no category {cid}")
    folders = [str(f).strip("/") for f in body.get("folders") or [] if str(f).strip("/")]
    paths = [str(p) for p in body.get("paths") or []]
    known = {r[0] for r in con.execute("SELECT path FROM nas_files WHERE gone=0")}
    if any(p not in known for p in paths):
        raise LookupError("not in the NAS catalogue")
    who = actor.split(":", 1)[1]
    with db.tx(con):
        for kind, items in (("folder", folders), ("file", paths)):
            for x in items:
                con.execute("INSERT INTO nas_rules (category_id, kind, pattern, created_by) VALUES (?,?,?,?) "
                            "ON CONFLICT(kind, pattern) DO UPDATE SET category_id=excluded.category_id, "
                            "created_by=excluded.created_by", (cid, kind, x, who))
    return {"changed": nasindex.resolve(con), "folders": len(folders), "files": len(paths)}


def tag(con, body: dict, actor: str) -> dict:
    _person(actor)
    paths = [str(p) for p in body.get("paths") or []]
    add = [str(t).strip()[:40] for t in body.get("add") or [] if str(t).strip()]
    rem = [str(t).strip() for t in body.get("remove") or []]
    with db.tx(con):
        for p in paths:
            con.executemany("INSERT INTO nas_tags (path, tag, source) VALUES (?,?, 'person') "
                            "ON CONFLICT(path, tag) DO UPDATE SET source='person'", [(p, t) for t in add])
            con.executemany("DELETE FROM nas_tags WHERE path=? AND tag=?", [(p, t) for t in rem])
    return {"paths": len(paths), "added": add, "removed": rem}


def views(con) -> list[dict]:
    return [{**db.row(r), "spec": json.loads(r["spec"])}
            for r in con.execute("SELECT * FROM nas_views ORDER BY pinned DESC, pos, id")]


def view_edit(con, body: dict, actor: str) -> dict:
    """save {name, spec[, id]} | pin {id, pinned} | delete {id}"""
    _person(actor)
    act = body.get("action")
    if act == "save":
        name = str(body.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("name the view")
        spec = {k: v for k, v in dict(body.get("spec") or {}).items() if k not in ("offset", "limit")}
        if body.get("id"):
            con.execute("UPDATE nas_views SET name=?, spec=? WHERE id=?", (name, json.dumps(spec), int(body["id"])))
            return {"id": int(body["id"])}
        vid = con.execute("INSERT INTO nas_views (name, spec, pinned, pos, created_by, created_at) VALUES (?,?,1,?,?,?)",
                          (name, json.dumps(spec), int(time.time()), actor.split(":", 1)[1], time.time())).lastrowid
        return {"id": vid}
    vid = int(body["id"])
    if act == "pin":
        con.execute("UPDATE nas_views SET pinned=? WHERE id=?", (1 if body.get("pinned") else 0, vid))
    elif act == "delete":
        con.execute("DELETE FROM nas_views WHERE id=?", (vid,))
    else:
        raise ValueError("action: save, pin or delete")
    return {"id": vid, "action": act}
