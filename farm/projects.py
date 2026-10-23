"""Farm Projects: a large order as groups -> parts x qty, on one flexible grid, whatever makes each part.

A part is made by a maker (from a New-job template, its label in the maker's personalised parameter - the text of a
plate), re-prints an Inventory artefact, or is an item that isn't printed at all (inserts, hardware, packaging).
Every unit of a part is one grid cell; its status comes from the part's jobs (jobs.part_id), their builds and the
prints of their artefacts. Projects only prepare: generating and slicing queue for Review & queue, where a person
approves and the start gate starts - nothing here can start a print.

The Qabrastaan batch (jobs/qabrastaan/batch.json) imports as the first project. Its plates were made before projects
existed, so a batch-imported project also counts prints whose files carry the part's label (A01, CH02+A11, ...).
"""
import json
import re
import time
from pathlib import Path

from . import db, inventory, makers, pipeline, printers

ACTIVE = {"received": "generating", "generating": "generating", "generated": "generated", "planned": "sliced"}
BUILD = {"slicing": "sliced", "review": "sliced", "approved": "queued", "uploading": "queued", "queued": "queued",
         "starting": "printing", "printing": "printing"}
ORDER = ["todo", "generating", "generated", "sliced", "queued", "printing", "failed", "done"]


class Refused(Exception):
    pass


def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")):
        raise Refused("only a person changes what a project has delivered or removes parts")


# ---------------------------------------------------------------- building a project

def create(con, name: str, actor: str, client: str = "", note: str = "", hours_per_unit: float | None = None,
           units_per_bed: float | None = None, source: str | None = None) -> int:
    if not name.strip():
        raise ValueError("a project needs a name")
    return con.execute("INSERT INTO projects (name, client, note, hours_per_unit, units_per_bed, source, created_by, "
                       "created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (name.strip()[:120], client[:120], note[:2000], hours_per_unit, units_per_bed, source, actor,
                        db.now())).lastrowid


def add_group(con, project_id: int, name: str, meta: dict | None = None) -> int:
    if not con.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
        raise LookupError(f"no project {project_id}")
    pos = con.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM project_groups WHERE project_id=?", (project_id,)).fetchone()[0]
    return con.execute("INSERT INTO project_groups (project_id, name, pos, meta) VALUES (?,?,?,?)",
                       (project_id, name.strip()[:80] or f"Group {pos + 1}", pos, db.dumps(meta or {}))).lastrowid


def parse_labels(text: str) -> list[tuple[str, int]]:
    """Paste: one part per line, 'label' or 'label, qty'; a range 'A01-A50' (same prefix, numbered) expands."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        label, _, qty = line.partition(",")
        label, qty = label.strip(), int(qty.strip() or 1) if qty.strip().isdigit() else 1
        m = re.fullmatch(r"([A-Za-z]*)(\d+)\s*-\s*\1(\d+)", label)
        if m and int(m.group(3)) >= int(m.group(2)):
            w = len(m.group(2))
            out += [(f"{m.group(1)}{i:0{w}d}", qty) for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append((label[:80], max(1, qty)))
    return out


def add_parts(con, group_id: int, parts: list[dict]) -> list[int]:
    """parts: [{label, qty, source: maker|artefact|item, template, maker, params, artefact_id}]"""
    g = con.execute("SELECT * FROM project_groups WHERE id=?", (group_id,)).fetchone()
    if not g:
        raise LookupError(f"no group {group_id}")
    pos = con.execute("SELECT COALESCE(MAX(pos), -1) + 1 FROM project_parts WHERE group_id=?", (group_id,)).fetchone()[0]
    ids = []
    with db.tx(con):
        for i, p in enumerate(parts):
            src = p.get("source") or ("artefact" if p.get("artefact_id") else "item" if not (p.get("template") or p.get("maker")) else "maker")
            if src not in ("maker", "artefact", "item"):
                raise ValueError(f"source {src!r}: maker, artefact or item")
            if src == "artefact" and not con.execute("SELECT 1 FROM artefacts WHERE id=?", (p.get("artefact_id"),)).fetchone():
                raise ValueError(f"{p.get('label')}: no such Inventory artefact")
            if src == "maker" and not (p.get("template") or p.get("maker")):
                raise ValueError(f"{p.get('label')}: a made part needs a template or a maker")
            ids.append(con.execute(
                "INSERT INTO project_parts (group_id, label, qty, source, maker, params, template, artefact_id, pos) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (group_id, str(p["label"])[:80], max(1, int(p.get("qty") or 1)), src, p.get("maker"),
                 db.dumps(p.get("params") or {}), p.get("template"), p.get("artefact_id"), pos + i)).lastrowid)
    return ids


def import_batch(con, path: Path, actor: str, templates: list[dict]) -> int:
    """A jobs/*/batch.json grid as a project (idempotent: the same file imports once)."""
    b = json.loads(Path(path).read_text(encoding="utf-8"))
    src = f"batch:{Path(path).as_posix().split('/jobs/', 1)[-1]}"
    old = con.execute("SELECT id FROM projects WHERE source=?", (src,)).fetchone()
    if old:
        return old[0]
    per_bed = b.get("plates_per_bed") or 1
    tpl = {t["id"] for t in templates}
    main = next((t for t in ("qabrastaan-plate-neo",) if t in tpl), None)
    children = next((t for t in ("qabrastaan-children",) if t in tpl), main)
    with db.tx(con):
        pid = create(con, b["name"], actor, note=b.get("note", ""), source=src,
                     hours_per_unit=(b.get("hours_per_bed") or 0) / per_bed or None, units_per_bed=per_bed)
        for row in b["rows"]:
            small = row.get("font_size_mm") and row["font_size_mm"] < 90
            gid = add_group(con, pid, row.get("name") or row["prefix"], {"prefix": row["prefix"], **(
                {"font_size_mm": row["font_size_mm"]} if row.get("font_size_mm") else {})})
            con.execute("UPDATE project_groups SET pos=? WHERE id=?", (b["rows"].index(row), gid))
            for i in range(1, row["count"] + 1):
                con.execute("INSERT INTO project_parts (group_id, label, qty, source, template, pos) "
                            "VALUES (?,?,1,'maker',?,?)",
                            (gid, f"{row['prefix']}{i:02d}", children if small else main, i))
    return pid


# ---------------------------------------------------------------- the grid

def _parts(con, project_id: int) -> list[dict]:
    return [db.row(r) for r in con.execute(
        "SELECT pp.*, g.name AS group_name, g.pos AS gpos FROM project_parts pp JOIN project_groups g ON g.id = pp.group_id "
        "WHERE g.project_id=? AND pp.removed=0 ORDER BY g.pos, g.id, pp.pos, pp.id", (project_id,))]


def _label_prints(con) -> dict:
    """label -> successful prints of artefacts that carry it (legacy files: A01_..., CH02+A11_...)."""
    out = {}
    for meta, n in con.execute("SELECT a.meta, COUNT(*) FROM prints p JOIN artefacts a ON a.id = p.artefact_id "
                               "WHERE p.status='completed' AND COALESCE(p.quality,'') <> 'bad' GROUP BY a.id"):
        for lab in json.loads(meta or "{}").get("labels") or []:
            out[lab] = out.get(lab, 0) + n
    return out


def grid(con, project_id: int) -> dict:
    pr = con.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not pr:
        raise LookupError(f"no project {project_id}")
    pr = db.row(pr)
    legacy = _label_prints(con) if (pr["source"] or "").startswith("batch:") else {}
    groups, totals = {}, {"cells": 0, "done": 0}
    est_left = 0.0
    for p in _parts(con, project_id):
        jobs = [db.row(j) for j in con.execute("SELECT * FROM jobs WHERE part_id=? ORDER BY id", (p["id"],))]
        if legacy and not jobs:   # batch-imported: the label's jobs from before projects existed
            jobs = [db.row(j) for j in con.execute("SELECT * FROM jobs WHERE label=? AND part_id IS NULL AND "
                                                   "state <> 'cancelled' ORDER BY id", (p["label"],))]
        arts = [r[0] for r in con.execute("SELECT id FROM artefacts WHERE job_id IN (%s)" %
                                          ",".join(str(j["id"]) for j in jobs))] if jobs else []
        if p["source"] == "artefact" and p["artefact_id"]:
            arts.append(p["artefact_id"])
        done = 0
        if p["source"] == "item":
            done = p["items_done"]
        else:
            if arts:
                done = con.execute("SELECT COUNT(*) FROM prints WHERE artefact_id IN (%s) AND status='completed' AND "
                                   "COALESCE(quality,'') <> 'bad'" % ",".join(map(str, arts))).fetchone()[0]
            done = max(done, legacy.get(p["label"], 0), sum(1 for j in jobs if j["state"] == "done"))
        active = []
        for j in jobs:
            if j["state"] in ("done", "cancelled"):
                continue
            if j["state"] == "failed":
                active.append(("failed", j))
                continue
            b = con.execute("SELECT b.state, b.estimate_s FROM build_jobs bj JOIN builds b ON b.id = bj.build_id WHERE "
                            "bj.job_id=? ORDER BY b.id DESC LIMIT 1", (j["id"],)).fetchone()
            st = BUILD.get(b[0]) if b and b[0] in BUILD else ACTIVE.get(j["state"], "generating")
            if j["state"] == "printing":
                st = "printing"
            active.append((st, j))
        cells = []
        for u in range(p["qty"]):
            if p["source"] == "item":
                st = "item-done" if u < done else "item-todo"
            elif u < done:
                st = "done"
            elif u - done < len(active):
                st = active[u - done][0]
            else:
                st = "todo"
            cells.append({"unit": u, "status": st,
                          "job": active[u - done][1]["id"] if p["source"] != "item" and done <= u < done + len(active) else None})
        totals["cells"] += len(cells)
        totals["done"] += sum(c["status"] in ("done", "item-done") for c in cells)
        if p["source"] != "item":
            left = sum(c["status"] not in ("done",) for c in cells)
            est_left += left * (pr["hours_per_unit"] or 0)
        g = groups.setdefault(p["group_id"], {"id": p["group_id"], "name": p["group_name"], "parts": []})
        g["parts"].append({"id": p["id"], "label": p["label"], "qty": p["qty"], "source": p["source"],
                           "template": p["template"], "maker": p["maker"], "artefact_id": p["artefact_id"],
                           "generated": any(j["state"] in ("generated", "planned", "done", "printing") for j in jobs),
                           "cells": cells})
    for g in con.execute("SELECT id, name FROM project_groups WHERE project_id=? ORDER BY pos, id", (project_id,)):
        groups.setdefault(g[0], {"id": g[0], "name": g[1], "parts": []})
    order = [r[0] for r in con.execute("SELECT id FROM project_groups WHERE project_id=? ORDER BY pos, id", (project_id,))]
    return {**pr, "groups": [groups[i] for i in order], "total": totals["cells"], "done": totals["done"],
            "hours_left": round(est_left, 1)}


def summaries(con) -> list[dict]:
    out = []
    for p in con.execute("SELECT id FROM projects WHERE state <> 'archived' ORDER BY id DESC"):
        g = grid(con, p[0])
        status = {}
        for gr in g["groups"]:
            for part in gr["parts"]:
                for c in part["cells"]:
                    status[c["status"]] = status.get(c["status"], 0) + 1
        out.append({k: g[k] for k in ("id", "name", "client", "note", "state", "total", "done", "hours_left", "source",
                                      "created_at")} | {"status": status, "groups": len(g["groups"])})
    return out


# ---------------------------------------------------------------- acting on cells

def _selected(con, project_id: int, part_ids: list[int]) -> list[dict]:
    parts = {p["id"]: p for p in _parts(con, project_id)}
    missing = [i for i in part_ids if i not in parts]
    if missing:
        raise LookupError(f"parts {missing} are not in project {project_id}")
    return [parts[i] for i in part_ids]


def generate(con, project_id: int, part_ids: list[int], actor: str, templates: list[dict], resolve,
             override: dict | None = None) -> list[dict]:
    """Make the selected maker parts' missing units: one order per template/maker+profile+printer, one job per
    unit (personalised with the part's label). Units already made, in work or done are not made again."""
    override = override or {}
    tpl = {t["id"]: t for t in templates}
    g = grid(con, project_id)
    todo = {}
    for gr in g["groups"]:
        for part in gr["parts"]:
            if part["id"] in part_ids and part["source"] == "maker":
                todo[part["id"]] = sum(c["status"] == "todo" for c in part["cells"])   # failed: Retry its job
    batches = {}
    for p in _selected(con, project_id, part_ids):
        if p["source"] != "maker" or todo.get(p["id"], 0) <= 0:
            continue
        t = dict(tpl.get(p["template"]) or {})
        if p["template"] and not t:
            raise ValueError(f"{p['label']}: template {p['template']!r} no longer exists")
        maker = p["maker"] or t.get("maker")
        m = makers.get(maker)
        params = {**(t.get("params") or {}), **json.loads(p["params"] or "{}")}
        pers = next((k for k, v in m.spec.params.items() if v.get("personalise")), None)
        if pers and not json.loads(p["params"] or "{}").get(pers):
            params[pers] = p["label"]
        profile = override.get("profile") or t.get("profile")
        printer = override.get("printer") or t.get("printer")
        layer = float(override.get("layer_height") or t.get("layer_height") or 0)
        if not (profile and printer and layer):
            raise ValueError(f"{p['label']}: choose a printer, profile and layer height (its template has none)")
        key = (maker, p["template"] or "", profile, printer, layer)
        b = batches.setdefault(key, {"t": t, "items": [], "parts": []})
        for _ in range(todo[p["id"]]):
            b["items"].append({"label": p["label"], "params": params,
                               "inputs": {k: resolve(v) for k, v in (t.get("files") or {}).items() if v}})
            b["parts"].append(p["id"])
    pr = con.execute("SELECT name FROM projects WHERE id=?", (project_id,)).fetchone()
    out = []
    for (maker, template, profile, printer, layer), b in batches.items():
        t = b["t"]
        oid, run = pipeline.submit_order(
            con, source="project", title=f"{pr[0]}: {', '.join(dict.fromkeys(i['label'] for i in b['items']))}"[:120],
            maker=maker, items=b["items"], profile=profile, layer_height=layer, printer=printer,
            colours=t.get("colours") or [], colour_hex=t.get("colour_hex") or [],
            colour_changes=t.get("colour_changes") or [], same_bed=bool(t.get("same_bed")))
        jids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE order_id=? ORDER BY id", (oid,))]
        with db.tx(con):
            con.execute("UPDATE orders SET project_id=? WHERE id=?", (project_id, oid))
            for jid, part in zip(jids, b["parts"]):
                con.execute("UPDATE jobs SET part_id=? WHERE id=?", (part, jid))
        out.append({"order": oid, "jobs": jids, "printer": printer, "profile": profile, "layer_height": layer})
    if not out:
        raise ValueError("nothing to make: the selected parts are items, already made, in work or done")
    return out


def send(con, project_id: int, part_ids: list[int], printer: str, profile: str, layer_height: float,
         send_artefact) -> dict:
    """Generated parts (their newest generated jobs) and artefact parts onto new builds for this printer."""
    jids, arts = [], []
    for p in _selected(con, project_id, part_ids):
        if p["source"] == "artefact" and p["artefact_id"]:
            arts.append(p["artefact_id"])
            continue
        j = con.execute("SELECT j.id FROM jobs j WHERE j.part_id=? AND EXISTS (SELECT 1 FROM models m WHERE "
                        "m.job_id=j.id) ORDER BY j.id DESC LIMIT 1", (p["id"],)).fetchone()
        if j:
            jids.append(j[0])
    if not (jids or arts):
        raise ValueError("nothing generated to send yet: Generate first")
    builds = pipeline.send_jobs(con, jids, printer, profile, layer_height, "ui", same_bed=True) if jids else []
    sent = [send_artefact(con, a, {"printer": printer, "profile": profile, "layer_height": layer_height}) for a in arts]
    return {"builds": builds, "artefacts": sent}


def items(con, project_id: int, part_ids: list[int], delta: int, actor: str) -> int:
    _person(actor)
    n = 0
    for p in _selected(con, project_id, part_ids):
        if p["source"] == "item":
            con.execute("UPDATE project_parts SET items_done=? WHERE id=?",
                        (min(p["qty"], max(0, p["items_done"] + delta)), p["id"]))
            n += 1
    return n


def remove(con, project_id: int, part_ids: list[int], actor: str) -> int:
    _person(actor)
    _selected(con, project_id, part_ids)
    return con.execute("UPDATE project_parts SET removed=1 WHERE id IN (%s)" % ",".join("?" * len(part_ids)),
                       part_ids).rowcount


def set_state(con, project_id: int, state: str, actor: str) -> None:
    _person(actor)
    if state not in ("open", "delivered", "archived"):
        raise ValueError("state: open, delivered or archived")
    con.execute("UPDATE projects SET state=? WHERE id=?", (state, project_id))
