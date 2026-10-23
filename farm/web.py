"""Farm control room: `python -m farm serve` - live printers, spool inventory, jobs and batch progress.

Stdlib only (runs as-is on aali-cli). The page is static (farm/static/index.html) and polls the JSON API below;
the server is the only thing that talks to the printers, so the browser never needs CORS or printer access.

  GET  /health                liveness for the aali-cli watchdog (never touches a printer)
  GET  /api/overview          printers + jobs + batch + inventory in one payload (what the page polls)
  GET  /api/cam/<printer>     webcam snapshot proxied from the printer host
  GET  /api/model/<run>/<3mf> a run's project .3mf as viewer geometry (farm/model3mf.py)
  GET  /api/builds[?state=]   builds from the farm DB
  POST /api/builds/<id>/<act> approve | reject | cancel | retry | move {printer} | start-dialog | start
                              (start needs the dialog's nonce + checklist + spool + operator: farm/gate.py)
  GET  /printers               the Printers tab (fleet health, audit, config snapshots/drift) - farm/fleet.py
  GET  /api/fleet[/<key>/files|file?path=|diff?path=]   fleet overview / a printer's config files
  POST /api/fleet/<key>/snapshot | review {snapshot, operator} | changes {path, section/option/value | content, reason}
  POST /api/fleet/<key>/fix {rule} | telegram-pack | restart {service}    (farm/baseline.py, farm/ops.py)
  POST /api/fleet/<key>/onboard | registry | serial {port} | camera {name, path}   registry + onboarding (phase 5)
  POST /api/fleet/<key>/reconnect   FIRMWARE_RESTART of a printer that was switched off (board back on)
  GET  /api/hosts/<name>/usb  USB serial ports + cameras on a host and who uses them (read-only, ssh)
  POST /api/scripts {kind: camera|instance, ...}   root-only host steps as a script to run with sudo
  POST /api/changes/<id>/preview | apply | rollback | cancel | reject {operator}   (apply/rollback: farm/cfgchange.py gate)
  GET  /api/makers            the maker plugins (spec, health), print profiles, printers, licensed font NAMES
  POST /api/uploads?name=     a file for a new job (raw body) -> {"ref"}; kept under data/uploads/ (gitignored)
  GET  /api/templates         New-job templates (jobs/templates.json: proven + starter; data/templates.json: saved)
  POST /api/templates         save the New-job form as a template
  GET  /api/inventory?by=printer|project|maker&q=&printed=1&products=1   artefacts folded into designs (farm/inventory.py)
  GET  /api/inventory/design?d=   a design's prints     GET /api/artefacts/<id>/thumb   G-code thumbnail from its printer
  GET  /api/nas?q=&matched=1&folder=   the OMV project-files catalogue vs the printers (farm/nas.py)
  POST /api/inventory/refresh | /api/nas/scan | /api/nas/import {path} | /api/prints/<id>/mark {quality, note}
  POST /api/artefacts/<id>/send {printer, profile, layer_height}   onto a printer via Review & queue
  POST /api/artefacts/<id>/product {name, description, printer, profile, layer_height, photo}   the portfolio (a person)
  POST /api/products/<id>/delete   GET /api/products/<id>/photo   GET /api/inventory/photos?d=   GET /api/nas/image?path=
  GET  /api/projects[/<id>]   Farm Projects: summaries / one project's grid (farm/projects.py)
  POST /api/projects {name, client, note, hours_per_unit}   a new project
  POST /api/projects/<id>/groups {name} | parts {group_id|group, text, source, template, artefact_id}
       | generate {parts, printer?, profile?, layer_height?} | send {parts, printer, profile, layer_height}
       | item-done | item-undo | remove {parts} | state {state}   (marks, removal, state: a person only)
  GET  /api/library           orders -> generated jobs/models -> builds
  POST /api/jobs/send {job_ids, printer, profile, layer_height, same_bed}   re-use generated models on new builds
  POST /api/orders            a new order from the New-job page -> jobs queued for the worker
  POST /api/spools[/<id>]     add a spool / edit one ({"version": n, ...fields, "remaining_g"}); 409 if stale
"""
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit, cfgchange, config, db, fleet, gate, inventory, nas, ops, jobs, makers, model3mf, monitor, net, pipeline, printers, profiles, projects, spools, watch, worker
from .printers import REPO
from .runlog import RUNS_DIR

STATIC = Path(__file__).resolve().parent / "static"
DATA = REPO / "data"
import os as _os0
INVENTORY = Path(_os0.environ.get("FARM_INVENTORY", DATA / "inventory.json"))   # tests point elsewhere
BATCHES = sorted((REPO / "jobs").glob("*/batch.json"))

_cache, _lock = {}, threading.Lock()


def cached(key: str, ttl: float, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


# ---------------------------------------------------------------- runs (local pipeline history)

def project_files(run_dir: Path) -> list[str]:
    """The visual-gate .3mf of each sliced bed (project.export_project writes it beside its G-code)."""
    return [g.with_suffix(".3mf").name for g in sorted(run_dir.glob("*.gcode")) if g.with_suffix(".3mf").is_file()]


def model_blob(run_name: str, file: str) -> bytes | None:
    run_dir = RUNS_DIR / run_name
    if run_dir.parent != RUNS_DIR or not run_dir.is_dir() or file not in project_files(run_dir):
        return None
    path = run_dir / file
    try:   # the gate checks that the 3D view of a build was actually opened before it can be started
        con = db.connect()
        try:
            gate.record_view(con, path)
        finally:
            con.close()
    except Exception:
        pass
    return cached(f"model:{path}:{path.stat().st_mtime}", 3600, lambda: model3mf.load(path))


def load_runs() -> list[dict]:
    runs = []
    for m in sorted(RUNS_DIR.glob("*/manifest.json"), reverse=True):
        try:
            man = json.loads(m.read_text(encoding="utf-8"))
            job = json.loads((m.parent / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        val = man.get("steps", {}).get("validate", {})
        labels = [pl["text"] if isinstance(pl, dict) else pl for pl in job.get("plates", [])] or ([job["likhan"]["text"]] if job.get("likhan", {}).get("text") else [])
        gcodes = sorted(m.parent.glob("*.gcode"))
        runs.append({
            "dir": m.parent.name, "job": man.get("job"), "printer": job.get("printer"),
            "started": man.get("started"), "result": man.get("result", "running"),
            "uploaded": man.get("uploaded", []), "labels": labels, "colours": job.get("colours", []),
            "estimate": val.get("estimated printing time (normal mode)"),
            "grams": float(val.get("filament used [g]", 0) or 0),
            "gcode": str(gcodes[0]) if gcodes else None,
            "models": project_files(m.parent),
        })
    return runs


# ---------------------------------------------------------------- printers (live via Moonraker)

Q = ("print_stats=state,filename,print_duration,message&virtual_sdcard=progress,file_position"
     "&extruder=temperature,target&heater_bed=temperature,target&pause_resume=is_paused&webhooks=state,state_message"
     "&gcode_move=speed_factor,extrude_factor&display_status=message")


def _estimate(p, filename: str) -> float | None:
    try:
        meta = net.get_json(f"{p.moonraker}/server/files/metadata?filename={urllib.parse.quote(filename)}", timeout=4)
        return float(meta["result"].get("estimated_time") or 0) or None
    except Exception:
        return None


def printer_view(p, runs_by_remote: dict) -> dict:
    v = {"name": p.name, "host": urllib.parse.urlparse(p.moonraker).hostname, "online": False, "readonly": p.readonly,
         "fluidd": p.fluidd, "fluidd_note": p.fluidd_note}
    try:
        s = net.get_json(f"{p.moonraker}/printer/objects/query?{Q}", timeout=4)["result"]["status"]
    except Exception as e:
        v["error"] = str(e)[:120]
        return v
    ps, sd = s["print_stats"], s["virtual_sdcard"]
    v.update(online=True, klipper=s["webhooks"]["state"], klipper_msg=s["webhooks"]["state_message"].splitlines()[0],
             job=ps["state"], file=ps["filename"], elapsed=ps["print_duration"], progress=sd["progress"],
             paused=s["pause_resume"]["is_paused"], speed=s["gcode_move"]["speed_factor"],
             flow=s["gcode_move"]["extrude_factor"],
             nozzle=[s["extruder"]["temperature"], s["extruder"]["target"]],
             bed=[s["heater_bed"]["temperature"], s["heater_bed"]["target"]])
    if v["job"] in ("printing", "paused"):
        # file progress is bytes, and these plates are badly non-linear (sparse base layers: long time, few bytes),
        # so time comes from the slicer's estimate like Fluidd does; bytes only when there is no estimate
        est = cached(f"est:{p.name}:{v['file']}", 3600, lambda: _estimate(p, v["file"]))
        if est:
            v["eta_s"] = max(0.0, est - v["elapsed"])
            v["progress"] = min(0.99, v["elapsed"] / est)
        elif v["progress"] > 0.01:
            v["eta_s"] = v["elapsed"] / v["progress"] - v["elapsed"]
    run = runs_by_remote.get(v["file"])
    if run:
        v["run"] = {"dir": run["dir"], "job": run["job"], "labels": run["labels"], "colours": run["colours"],
                    "models": run["models"]}
        if run.get("gcode"):
            marks = cached("marks:" + run["gcode"], 3600, lambda: watch.milestones(Path(run["gcode"])))
            cc = [c / marks["size"] for c in marks["colour_changes"]]
            if v["paused"] and any(abs(sd["file_position"] - c) < 4096 for c in marks["colour_changes"]):
                v["alert"] = f"Colour change: load {run['colours'][1] if len(run['colours']) > 1 else 'next colour'}"
            # byte positions against byte progress; no time is given, because the G-code has no M73 remaining-time marks
            # (preset remaining_times = 0) and a byte-rate guess was off by ~10x on these plates
            if v["job"] == "printing" and any(c > sd["progress"] for c in cc):
                v["next"] = {"what": "colour change", "in_s": None}
    elif v["paused"]:
        v["alert"] = "Paused"
    v["off"] = monitor.powered_off(v["klipper"], s["webhooks"]["state_message"], v["job"])
    if v["klipper"] != "ready" and not v["off"]:
        v["alert"] = v["klipper_msg"]
    cams = cached(f"cams:{p.name}", 600, lambda: net.get_json(f"{p.moonraker}/server/webcams/list", timeout=4)["result"]["webcams"])
    v["has_cam"] = bool(cams)

    def audit_now():
        return [{"rule": r, "detail": d} for st, r, d in audit.rules(audit._settings(p), p) if st == "FAIL"]
    try:
        v["audit_fails"] = cached(f"audit:{p.name}", 300, audit_now)
    except Exception:
        v["audit_fails"] = None
    return v


def _safe_view(p, by_remote) -> dict:
    """One printer answering oddly (a Moonraker hiccup, a missing object) must not take down the whole page."""
    try:
        return printer_view(p, by_remote)
    except Exception as e:
        return {"name": p.name, "host": urllib.parse.urlparse(p.moonraker).hostname, "online": False,
                "readonly": p.readonly, "fluidd": p.fluidd, "fluidd_note": p.fluidd_note,
                "error": f"unexpected answer: {type(e).__name__}: {str(e)[:100]}"}


def history(p) -> list[dict]:
    try:
        return net.get_json(f"{p.moonraker}/server/history/list?limit=100", timeout=4)["result"]["jobs"]
    except Exception:
        return []


# ---------------------------------------------------------------- Farm Projects (farm/projects.py)

def projects_view() -> list[dict]:
    """Open projects for the KPIs and the Projects tab. A jobs/*/batch.json not yet imported becomes a project."""
    con = db.connect()
    try:
        for path in BATCHES:
            projects.import_batch(con, path, "import", templates())
        return projects.summaries(con)
    finally:
        con.close()


# ---------------------------------------------------------------- new jobs (makers, uploads, orders)

UPLOADS = DATA / "uploads"
MAX_UPLOAD = 200 * 1024 * 1024


def makers_view() -> dict:
    ms = []
    for m in makers.all_makers().values():
        s = m.spec
        ms.append({**m.manifest(), "healthy": cached(f"health:{s.id}", 30, m.health),
                   "defaults": makers.Maker.defaults(m)})
    profs = [{"id": p.id, "name": p.name, "printers": [x for x in p.printers if not printers.PRINTERS[x].readonly],
              "layer_heights": list(p.layer_heights), "material": p.material, "confirmed": p.confirmed,
              "supports": p.supports, "note": p.note} for p in profiles.PROFILES.values()]
    fonts = sorted(f.name for f in config.FONTS_DIR.glob("*.[ot]tf")) if config.FONTS_DIR.is_dir() else []
    return {"makers": ms, "profiles": profs, "fonts": fonts,
            "printers": [n for n, p in printers.PRINTERS.items() if not p.readonly]}


def save_upload(name: str, body: bytes) -> dict:
    import re as _re
    import uuid as _uuid
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "file").name).strip("._") or "file"
    d = UPLOADS / _uuid.uuid4().hex
    d.mkdir(parents=True)
    (d / safe).write_bytes(body)
    return {"ref": f"{d.name}/{safe}", "bytes": len(body)}


def _resolve_input(value: str) -> str:
    """'upload:<ref>' -> a file under data/uploads; 'font:<name>' -> a licensed font by NAME from FONTS_DIR.
    Nothing else: a page can't point the farm at an arbitrary path on this machine."""
    kind, _, ref = str(value).partition(":")
    if kind == "upload":
        path = (UPLOADS / ref).resolve()
        if UPLOADS.resolve() not in path.parents or not path.is_file():
            raise ValueError(f"unknown upload {ref}")
        return str(path)
    if kind == "font":
        path = (config.FONTS_DIR / Path(ref).name)
        if not path.is_file():
            raise ValueError(f"font {ref} is not installed on this server")
        return str(path)
    raise ValueError(f"bad file reference {value!r}")


TEMPLATES = REPO / "jobs" / "templates.json"          # proven + starter, in git
import os as _os
MY_TEMPLATES = Path(_os.environ.get("FARM_TEMPLATES", DATA / "templates.json"))   # saved from the page (tests point elsewhere)


def templates() -> list[dict]:
    out = json.loads(TEMPLATES.read_text(encoding="utf-8"))["templates"] if TEMPLATES.exists() else []
    if MY_TEMPLATES.exists():
        out += [{**t, "status": "saved"} for t in json.loads(MY_TEMPLATES.read_text(encoding="utf-8")).get("templates", [])]
    return out


def save_template(body: dict) -> dict:
    t = {k: body.get(k) for k in ("name", "maker", "title", "params", "files", "profile", "layer_height", "printer",
                                  "colours", "colour_hex", "colour_changes", "same_bed", "items")}
    if not (t["name"] and t["maker"]):
        raise ValueError("a template needs a name and a maker")
    if any(str(v).startswith("upload:") for v in (t.get("files") or {}).values()):
        t["files"] = {k: v for k, v in t["files"].items() if not str(v).startswith("upload:")}   # uploads are one-off
    import re as _re
    t["id"] = "my-" + _re.sub(r"[^a-z0-9]+", "-", t["name"].lower()).strip("-")[:50]
    mine = json.loads(MY_TEMPLATES.read_text(encoding="utf-8")).get("templates", []) if MY_TEMPLATES.exists() else []
    mine = [m for m in mine if m["id"] != t["id"]] + [t]
    DATA.mkdir(exist_ok=True)
    MY_TEMPLATES.write_text(json.dumps({"templates": mine}, indent=2), encoding="utf-8")
    return t


def create_order(body: dict) -> dict:
    items = []
    for it in body.get("items") or []:
        inputs = {k: _resolve_input(v) for k, v in (it.get("files") or {}).items() if v}
        items.append({"label": str(it.get("label") or "")[:80], "qty": int(it.get("qty") or 1),
                      "params": it.get("params") or {}, "inputs": inputs})
    con = db.connect()
    try:
        oid, run = pipeline.submit_order(
            con, source="ui", title=str(body.get("title") or body.get("maker") or "order")[:120],
            maker=str(body.get("maker")), items=items, profile=str(body.get("profile")),
            layer_height=float(body.get("layer_height") or 0), printer=str(body.get("printer")),
            colours=body.get("colours") or [], colour_hex=body.get("colour_hex") or [],
            colour_changes=body.get("colour_changes") or [], same_bed=bool(body.get("same_bed")),
            notes=str(body.get("notes") or "")[:500])
        ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE order_id=? ORDER BY id", (oid,))]
        for jid in ids:
            worker.enqueue(con, "generate", jid)
        return {"order": oid, "jobs": ids, "run": run.dir.name}
    finally:
        con.close()


def project_action(con, pid: int | None, action: str, body: dict, who: str):
    ids = [int(i) for i in body.get("parts") or []]
    if action == "create":
        new = projects.create(con, str(body.get("name") or ""), who, str(body.get("client") or ""),
                              str(body.get("note") or ""), float(body["hours_per_unit"]) if body.get("hours_per_unit") else None)
        projects.add_group(con, new, str(body.get("group") or "Parts"))
        return {"id": new}
    if action == "groups":
        return {"id": projects.add_group(con, pid, str(body.get("name") or ""))}
    if action == "parts":
        labels = projects.parse_labels(str(body.get("text") or ""))
        if not labels:
            raise ValueError("no parts: one label per line (or a range like A01-A50)")
        src = body.get("source") or "maker"
        common = {"source": src, "template": body.get("template") if src == "maker" else None,
                  "artefact_id": int(body["artefact_id"]) if src == "artefact" and body.get("artefact_id") else None}
        gid = int(body["group_id"]) if body.get("group_id") else projects.add_group(con, pid, str(body.get("group") or ""))
        return {"parts": projects.add_parts(con, gid, [{"label": l, "qty": q, **common} for l, q in labels])}
    if action == "generate":
        out = projects.generate(con, pid, ids, who, templates(), _resolve_input,
                                {k: body.get(k) for k in ("printer", "profile", "layer_height") if body.get(k)})
        for o in out:
            for jid in o["jobs"]:
                worker.enqueue(con, "generate", jid)
        return {"orders": out}
    if action == "send":
        out = projects.send(con, pid, ids, str(body["printer"]), str(body["profile"]), float(body["layer_height"]),
                            send_artefact)
        for bid in out["builds"]:
            worker.enqueue(con, "slice", bid)
        return out
    if action in ("item-done", "item-undo"):
        return {"changed": projects.items(con, pid, ids, 1 if action == "item-done" else -1, who)}
    if action == "remove":
        return {"removed": projects.remove(con, pid, ids, who)}
    if action == "state":
        projects.set_state(con, pid, str(body.get("state")), who)
        return {"state": body.get("state")}
    raise LookupError(f"no project action {action}")


def cam_snapshot(p) -> tuple[bytes, str]:
    cams = net.get_json(f"{p.moonraker}/server/webcams/list", timeout=4)["result"]["webcams"]
    host = urllib.parse.urlparse(p.moonraker).hostname
    with urllib.request.urlopen(f"http://{host}{cams[0]['snapshot_url']}", timeout=5) as r:
        return r.read(), r.headers.get("Content-Type", "image/jpeg")


def product_photo(con, src: str) -> tuple[bytes, str] | None:
    """camera:<printer> (a snapshot now), nas:<catalogued picture>, upload:<ref>; empty keeps the old photo."""
    kind, _, ref = src.partition(":")
    if not src:
        return None
    if kind == "camera":
        data, ctype = cam_snapshot(printers.get(ref))
        return data, "png" if "png" in ctype else "jpg"
    if kind == "nas":
        dest = nas.fetch_image(con, ref, nas.NAS_DIR / "_images" / ref.replace("@", ""))
        return dest.read_bytes(), dest.suffix
    if kind == "upload":
        path = Path(_resolve_input(src))
        return path.read_bytes(), path.suffix
    raise ValueError("photo: camera:<printer>, nas:<path> or upload:<ref>")


def send_artefact(con, artefact_id: int, body: dict) -> dict:
    """An Inventory artefact onto a printer: a maker job's models are re-used (pipeline.send_jobs); an imported or
    uploaded model file becomes an order for the upload maker. Either way it is sliced for Review & queue."""
    a = con.execute("SELECT * FROM artefacts WHERE id=?", (artefact_id,)).fetchone()
    if not a:
        raise LookupError(f"no artefact {artefact_id}")
    printer, profile, lh = str(body["printer"]), str(body["profile"]), float(body["layer_height"])
    if a["job_id"]:
        ids = pipeline.send_jobs(con, [a["job_id"]], printer, profile, lh, "ui")
        for bid in ids:
            worker.enqueue(con, "slice", bid)
        return {"builds": ids}
    if a["kind"] != "model" or not a["path"] or not Path(a["path"]).is_file():
        raise ValueError("this artefact is G-code sliced elsewhere (it can't be re-sliced) - import its model "
                         "from the NAS, or upload the STL/3MF")
    oid, run = pipeline.submit_order(con, source="inventory", title=a["title"][:120], maker="upload",
                                     items=[{"label": a["title"][:80], "params": {}, "inputs": {"file": a["path"]}}],
                                     profile=profile, layer_height=lh, printer=printer)
    ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE order_id=? ORDER BY id", (oid,))]
    for jid in ids:
        worker.enqueue(con, "generate", jid)
    return {"order": oid, "jobs": ids}


def jobs_in_progress() -> list[dict]:
    """Jobs not yet on a bed (being generated, or failed) - the Review table shows builds, this shows the rest."""
    con = db.connect()
    try:
        rows = con.execute("SELECT j.id, j.label, j.maker, j.state, j.error, j.printer, o.title, "
                           "EXISTS(SELECT 1 FROM tasks t WHERE t.kind='plan' AND t.ref_id=j.order_id AND "
                           "t.state IN ('pending','running')) AS planning FROM jobs j "
                           "JOIN orders o ON o.id = j.order_id WHERE j.state IN ('received','generating','generated',"
                           "'failed') ORDER BY j.id DESC LIMIT 40").fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- builds (review, queue, start)

ACTIVE = ("slicing", "review", "approved", "uploading", "queued", "starting", "printing", "failed")


def workable_layers(prof, change_zs) -> list[float]:
    """The profile's layer heights that can put every colour change exactly on a layer top (slicer.align_first_layer):
    the picker never offers one that would fail."""
    from . import slicer
    out = []
    for lh in prof.layer_heights:
        try:
            slicer.align_first_layer(lh, list(change_zs or []), *prof.first_layer)
            out.append(lh)
        except SystemExit:
            pass
    return out


def builds_view() -> list[dict]:
    con = db.connect()
    try:
        rows = [b for b in jobs.builds(con) if b["state"] in ACTIVE][:60]
        viewed = {r[0] for r in con.execute("SELECT DISTINCT build_id FROM approvals WHERE kind='viewed'")}
    finally:
        con.close()
    out = []
    for b in rows:
        prof = profiles.PROFILES.get(b["profile"])
        g = Path(b["gcode"]) if b["gcode"] else None
        out.append({
            "id": b["id"], "state": b["state"], "printer": b["printer"], "labels": b["labels"] or "",
            "order": b["order_title"], "profile": b["profile"], "profile_name": prof.name if prof else b["profile"],
            "profile_confirmed": bool(prof and prof.confirmed),
            "layer_height": b["layer_height"], "layer_heights": workable_layers(prof, b["change_zs"]) if prof else [],
            "printers": list(prof.printers) if prof else [b["printer"]],
            "estimate_s": b["estimate_s"], "grams": b["grams"], "error": b["error"], "remote": b["remote_path"],
            "colours": b["colours"], "viewed": b["id"] in viewed,
            "run": Path(b["run_dir"]).name if b["run_dir"] else None,
            "model": g.with_suffix(".3mf").name if g and g.with_suffix(".3mf").is_file() else None,
        })
    return out


def build_action(build_id: int, action: str, body: dict) -> dict:
    con = db.connect()
    try:
        b = jobs.get(con, "build", build_id)
        if b is None:
            raise LookupError(f"no build {build_id}")
        if action == "approve":
            # the visual gate is looking at it: the 3D view of this build must have been opened first
            if not con.execute("SELECT 1 FROM approvals WHERE build_id=? AND kind='viewed'", (build_id,)).fetchone():
                raise ValueError("open the 3D view of this build before approving it")
            pipeline.approve(con, build_id, "ui", note=str(body.get("note", ""))[:500])
            worker.enqueue(con, "upload", build_id)
        elif action == "reject":
            with db.tx(con):
                jobs.transition(con, "build", build_id, "review", "rejected", "ui", note=str(body.get("note", ""))[:500])
                for (jid,) in con.execute("SELECT job_id FROM build_jobs WHERE build_id=?", (build_id,)).fetchall():
                    if jobs.get(con, "job", jid)["state"] == "planned":
                        jobs.transition(con, "job", jid, "planned", "generated", "ui", rejected_build=build_id)
        elif action == "cancel":
            jobs.transition(con, "build", build_id, b["state"], "cancelled", "ui")
        elif action == "reslice":
            pipeline.reslice(con, build_id, float(body["layer_height"]), "ui")
            worker.enqueue(con, "slice", build_id)
        elif action == "retry":
            jobs.transition(con, "build", build_id, "failed", "slicing", "ui")
            worker.enqueue(con, "slice", build_id)
        elif action == "move":
            target = str(body.get("printer", ""))
            prof = profiles.get(b["profile"])
            p = printers.PRINTERS.get(target)
            if p is None or p.readonly or not prof.allows(target):
                raise ValueError(f"{target or '?'} can't run this build (profile {prof.id}: {', '.join(prof.printers)})")
            with db.tx(con):
                if b["state"] == "queued":
                    jobs.transition(con, "build", build_id, "queued", "approved", "ui", moved_to=target)
                elif b["state"] != "approved":
                    raise ValueError(f"only approved or queued builds can move (this one is {b['state']})")
                jobs.update_build(con, build_id, printer=target, remote_path=None)
            worker.enqueue(con, "upload", build_id)
        elif action == "start-dialog":
            pf = gate.preflight(con, build_id)
            return {"nonce": gate.issue_nonce(build_id), "reasons": pf["reasons"], "spools": pf.get("spools", []),
                    "all_spools": pf.get("all_spools", []), "colours": b["colours"],
                    "material": pf.get("material"), "viewed": pf.get("viewed"),
                    "printer_status": pf.get("printer_status"), "audit_fails": pf.get("audit_fails", [])}
        elif action == "start":
            remote = gate.start_build(con, build_id, str(body.get("operator", "")).strip()[:80],
                                      body.get("checklist") or {}, body.get("spool_id"), via="ui",
                                      nonce=body.get("nonce"), later_spools=body.get("later_spools") or [])
            return {"started": remote}
        else:
            raise LookupError(f"unknown action {action}")
        return {"ok": True, "state": jobs.get(con, "build", build_id)["state"]}
    finally:
        con.close()


def overview() -> dict:
    printers.refresh()
    runs = load_runs()
    by_remote = {rem: r for r in reversed(runs) for rem in r["uploaded"]}
    plist = list(printers.PRINTERS.values())
    live = [_safe_view(p, by_remote) for p in plist]
    hist = {}
    for p in plist:
        for h in sorted(cached(f"hist:{p.name}", 30, lambda p=p: history(p)), key=lambda h: h.get("start_time", 0)):
            hist[h["filename"]] = h
    # one row per uploaded file and per job: older runs of the same job (dry runs, re-slices) are superseded
    shown, seen_remote, seen_job = [], set(), set()
    for r in runs:  # newest first
        remote = r["uploaded"][0] if r["uploaded"] else None
        if (remote and remote in seen_remote) or (not remote and r["job"] in seen_job):
            continue
        if remote:
            seen_remote.add(remote)
        seen_job.add(r["job"])
        h = hist.get(remote) if remote else None
        r["status"] = h["status"] if h else ("uploaded" if remote else r["result"])
        r.pop("gcode", None)
        shown.append(r)
    con = db.connect()
    try:
        inv = {"spools": spools.all_spools(con)}
    finally:
        con.close()
    return {"time": time.time(), "printers": live, "runs": shown[:25],
            "projects": [x for x in cached("projects", 15, projects_view) if x["state"] == "open"],
            "builds": builds_view(), "jobs_in_progress": jobs_in_progress(),
            "inventory": inv, "petg_used_g": round(sum(r["grams"] for r in shown if r["status"] == "completed"), 1)}


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/health":
            return self._send(200, b'{"ok":true}')
        if path == "/api/builds":
            state = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("state", [None])[0]
            con = db.connect()
            try:
                return self._send(200, json.dumps(jobs.builds(con, state), default=str).encode())
            finally:
                con.close()
        if path in ("/printers", "/printers.html"):
            return self._send(200, (STATIC / "printers.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/static/printers.js":
            return self._send(200, (STATIC / "printers.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/api/fleet":
            return self._send(200, json.dumps(fleet.overview(), default=str).encode())
        if path.startswith("/api/hosts/") and path.endswith("/usb"):
            try:
                return self._send(200, json.dumps(fleet.usb(urllib.parse.unquote(path.split("/")[3]))).encode())
            except LookupError as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
        if path.startswith("/api/fleet/"):
            parts = path[len("/api/fleet/"):].split("/")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                key = fleet.unquote_key(parts[0])
                if parts[1:] == ["files"]:
                    return self._send(200, json.dumps(fleet.files(key)).encode())
                if parts[1:] == ["file"]:
                    return self._send(200, fleet.file_text(key, q["path"][0]).encode(), "text/plain; charset=utf-8")
                if parts[1:] == ["changes"]:
                    return self._send(200, json.dumps(fleet.changes(key), default=str).encode())
                if parts[1:] == ["maintenance"]:
                    return self._send(200, json.dumps(fleet.maint(key), default=str).encode())
                if parts[1:] == ["diff"]:
                    return self._send(200, fleet.diff(key, q["path"][0]).encode(), "text/plain; charset=utf-8")
            except (LookupError, KeyError, IndexError) as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
            except PermissionError as e:
                return self._send(403, json.dumps({"error": str(e)}).encode())
            return self._send(404, b"{}")
        if path == "/api/templates":
            return self._send(200, json.dumps(templates()).encode())
        if path in ("/api/inventory", "/api/inventory/design", "/api/nas"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            con = db.connect()
            try:
                if path == "/api/inventory":
                    out = inventory.listing(con, q.get("by", "printer"), q.get("q", ""), q.get("printed") == "1",
                                            q.get("products") == "1")
                elif path == "/api/inventory/design":
                    out = {"prints": inventory.prints_of(con, q.get("d", ""))}
                else:
                    out = nas.matrix(con, q.get("q", ""), q.get("matched") == "1", q.get("folder", ""))
                return self._send(200, json.dumps(out, default=str).encode())
            finally:
                con.close()
        if path.startswith("/api/artefacts/") and path.endswith("/thumb"):
            def fetch(aid=path.split("/")[3]):
                con = db.connect()
                try:
                    return inventory.thumb(con, int(aid))
                except (ValueError, OSError):
                    return None
                finally:
                    con.close()
            t = cached(f"thumb:{path}", 3600, fetch)
            return self._send(200, t[0], t[1]) if t else self._send(404, b"{}")
        if path == "/api/projects":
            return self._send(200, json.dumps(projects_view(), default=str).encode())
        if path.startswith("/api/projects/") and path.split("/")[3].isdigit():
            con = db.connect()
            try:
                return self._send(200, json.dumps(projects.grid(con, int(path.split("/")[3])), default=str).encode())
            except LookupError as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
            finally:
                con.close()
        if path == "/api/library":
            con = db.connect()
            try:
                return self._send(200, json.dumps(pipeline.library(con), default=str).encode())
            finally:
                con.close()
        if path == "/api/makers":
            return self._send(200, json.dumps(makers_view(), default=str).encode())
        if path == "/api/overview":
            return self._send(200, json.dumps(overview()).encode())
        if path.startswith("/api/cam/"):
            p = printers.PRINTERS.get(path.rsplit("/", 1)[-1])
            if not p:
                return self._send(404, b"{}")
            try:
                return self._send(200, *cam_snapshot(p))
            except Exception:
                return self._send(502, b"")
        if path.startswith("/api/products/") and path.endswith("/photo"):
            con = db.connect()
            try:
                ph = inventory.product_photo(con, int(path.split("/")[3]))
            except ValueError:
                ph = None
            finally:
                con.close()
            return self._send(200, *ph) if ph else self._send(404, b"{}")
        if path in ("/api/inventory/photos", "/api/nas/image"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
            con = db.connect()
            try:
                if path == "/api/inventory/photos":
                    out = {"nas": nas.photos_for(con, q.get("d", "")),
                           "cameras": [x.name for x in printers.PRINTERS.values()], "proven": inventory.proven(con, q.get("d", ""))}
                    return self._send(200, json.dumps(out, default=str).encode())
                rel = q.get("path", "")
                dest = (nas.NAS_DIR / "_images" / rel.replace("@", "")).resolve()
                if not str(dest).startswith(str((nas.NAS_DIR / "_images").resolve())):
                    return self._send(400, b"{}")
                if not dest.is_file():
                    nas.fetch_image(con, rel, dest)
                ext = dest.suffix.lower().lstrip(".")
                return self._send(200, dest.read_bytes(), "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}")
            except (LookupError, RuntimeError, OSError) as e:
                return self._send(404, json.dumps({"error": str(e)[:200]}).encode())
            finally:
                con.close()
        if path.startswith("/api/model/"):
            parts = [urllib.parse.unquote(x) for x in path[len("/api/model/"):].split("/")]
            blob = model_blob(*parts) if len(parts) == 2 else None
            return self._send(200, blob, "application/octet-stream") if blob else self._send(404, b"{}")
        return self._send(404, b"{}")

    def _json_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        if n > 1_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "builds"] and parts[2].isdigit():
            try:
                out = build_action(int(parts[2]), parts[3], self._json_body())
                return self._send(200, json.dumps(out, default=str).encode())
            except LookupError as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
            except (gate.GateRefused, pipeline.StepFailed, jobs.StateConflict, ValueError, SystemExit) as e:
                return self._send(409, json.dumps({"error": str(e)}).encode())
        if len(parts) == 4 and parts[:2] == ["api", "fleet"]:
            try:
                key, body = fleet.unquote_key(parts[2]), self._json_body()
                who = "ui:" + (str(body.get("operator") or "").strip()[:40] or "unknown")
                if parts[3] == "snapshot":
                    return self._send(200, json.dumps(fleet.snapshot(key, who)).encode())
                if parts[3] == "review":
                    return self._send(200, json.dumps(fleet.review(key, int(body["snapshot"]), who)).encode())
                if parts[3] == "changes":
                    return self._send(200, json.dumps(fleet.stage_change(key, body, who)).encode())
                if parts[3] == "onboard":
                    return self._send(200, json.dumps(fleet.onboard(key, body, who)).encode())
                if parts[3] == "serial":
                    return self._send(200, json.dumps(fleet.stage_serial(key, str(body["port"]), who)).encode())
                if parts[3] == "camera":
                    return self._send(200, json.dumps(fleet.register_camera(key, body, who)).encode())
                if parts[3] == "registry":
                    return self._send(200, json.dumps(fleet.update_registry(key, body, who)).encode())
                if parts[3] == "reconnect":
                    return self._send(200, json.dumps(fleet.reconnect(key, who)).encode())
                if parts[3] == "maintenance":
                    return self._send(200, json.dumps(fleet.maint_log(key, body, who), default=str).encode())
                if parts[3] == "fix":
                    return self._send(200, json.dumps(fleet.stage_fix(key, str(body["rule"]), who)).encode())
                if parts[3] == "telegram-pack":
                    return self._send(200, json.dumps(fleet.telegram_pack(key, who)).encode())
                if parts[3] == "restart":
                    return self._send(200, json.dumps(fleet.restart_service(key, str(body["service"]), who)).encode())
            except (LookupError, KeyError, ValueError, cfgchange.Refused, ops.Refused) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            return self._send(404, b"{}")
        if path == "/api/scripts":
            try:
                body = self._json_body()
                kind = body.pop("kind", "")
                body.pop("operator", None)
                return self._send(200, ops.host_script(kind, **body).encode(), "text/plain; charset=utf-8")
            except (ops.Refused, KeyError, ValueError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
        if len(parts) == 4 and parts[:2] == ["api", "changes"] and parts[2].isdigit():
            try:
                body = self._json_body()
                who = "ui:" + (str(body.get("operator") or "").strip()[:40] or "unknown")
                return self._send(200, json.dumps(fleet.change_action(int(parts[2]), parts[3], who), default=str).encode())
            except (cfgchange.Refused, jobs.StateConflict, LookupError) as e:
                return self._send(409, json.dumps({"error": str(e)}).encode())
        if path == "/api/uploads":
            n = int(self.headers.get("Content-Length", 0) or 0)
            if not 0 < n <= MAX_UPLOAD:
                return self._send(413, json.dumps({"error": f"upload must be 1 byte..{MAX_UPLOAD // 2**20} MB"}).encode())
            name = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("name", ["file"])[0]
            return self._send(200, json.dumps(save_upload(name, self.rfile.read(n))).encode())
        if path == "/api/templates":
            try:
                return self._send(200, json.dumps(save_template(self._json_body())).encode())
            except (ValueError, KeyError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
        if path == "/api/jobs/send":
            try:
                body = self._json_body()
                con = db.connect()
                try:
                    ids = pipeline.send_jobs(con, [int(j) for j in body["job_ids"]], str(body["printer"]),
                                             str(body["profile"]), float(body["layer_height"]), "ui",
                                             same_bed=bool(body.get("same_bed", True)))
                    for bid in ids:
                        worker.enqueue(con, "slice", bid)
                finally:
                    con.close()
                return self._send(200, json.dumps({"builds": ids}).encode())
            except (pipeline.StepFailed, KeyError, ValueError, SystemExit) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
        if path == "/api/orders":
            try:
                return self._send(200, json.dumps(create_order(self._json_body())).encode())
            except (pipeline.StepFailed, ValueError, SystemExit, KeyError, TypeError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
        if len(parts) in (2, 3) and parts[:2] == ["api", "spools"]:
            con = db.connect()
            try:
                body = self._json_body()
                if len(parts) == 2:
                    out = spools.add(con, body)
                else:
                    out = spools.patch(con, parts[2], body.pop("version", -1), body)
                return self._send(200, json.dumps(out).encode())
            except spools.Conflict as e:
                return self._send(409, json.dumps({"error": str(e)}).encode())
            except (LookupError, ValueError, KeyError) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            finally:
                con.close()
        if path in ("/api/inventory/refresh", "/api/nas/scan", "/api/nas/import") or (
                len(parts) == 4 and parts[:2] in (["api", "prints"], ["api", "artefacts"], ["api", "products"]) and parts[2].isdigit()):
            con = db.connect()
            try:
                body = self._json_body()
                who = "ui:" + (str(body.get("operator") or "").strip()[:40] or "unknown")
                if path == "/api/inventory/refresh":
                    out = inventory.backfill(con, log=lambda m: None)
                elif path == "/api/nas/scan":
                    out = nas.scan(con, log=lambda m: None)
                elif path == "/api/nas/import":
                    out = nas.import_file(con, str(body["path"]), who)
                elif parts[1] == "prints" and parts[3] == "mark":
                    out = inventory.mark(con, int(parts[2]), body.get("quality"), who, str(body.get("note") or ""))
                elif parts[1] == "artefacts" and parts[3] == "send":
                    out = send_artefact(con, int(parts[2]), body)
                elif parts[1] == "artefacts" and parts[3] == "product":
                    out = inventory.promote(con, int(parts[2]), body, who, product_photo(con, str(body.get("photo") or "")))
                elif parts[1] == "products" and parts[3] == "delete":
                    inventory.unpromote(con, int(parts[2]), who)
                    out = {"deleted": int(parts[2])}
                else:
                    return self._send(404, b"{}")
                return self._send(200, json.dumps(out, default=str).encode())
            except LookupError as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
            except (inventory.Refused, pipeline.StepFailed, ValueError, KeyError, RuntimeError, SystemExit) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            finally:
                con.close()
        if parts[:2] == ["api", "projects"] and (len(parts) == 2 or len(parts) == 4 and parts[2].isdigit()):
            con = db.connect()
            try:
                body = self._json_body()
                who = "ui:" + (str(body.get("operator") or "").strip()[:40] or "unknown")
                out = project_action(con, int(parts[2]) if len(parts) == 4 else None, parts[3] if len(parts) == 4
                                     else "create", body, who)
                _cache.pop("projects", None)
                return self._send(200, json.dumps(out, default=str).encode())
            except LookupError as e:
                return self._send(404, json.dumps({"error": str(e)}).encode())
            except (projects.Refused, pipeline.StepFailed, ValueError, KeyError, TypeError, SystemExit) as e:
                return self._send(400, json.dumps({"error": str(e)}).encode())
            finally:
                con.close()
        if path == "/api/inventory":   # the old whole-file save could overwrite a print's deduction
            return self._send(410, b'{"error":"the inventory moved into the farm DB: POST /api/spools, PATCH-style POST /api/spools/<id> with version"}')
        return self._send(404, b"{}")

    def log_message(self, fmt, *args):  # keep the console quiet; the page polls every few seconds
        pass


def serve(host: str, port: int):
    if not INVENTORY.exists() and (DATA / "inventory.example.json").exists():
        INVENTORY.write_text((DATA / "inventory.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    con = db.connect()
    n = spools.migrate_json(con, INVENTORY)   # once: the inventory now lives in the farm DB (a dated copy is kept)
    if n:
        print(f"moved {n} spool(s) from {INVENTORY.name} into the farm DB")
    con.close()
    print(f"farm control room on http://{host}:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
