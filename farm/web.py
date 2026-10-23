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
  POST /api/inventory         replace the spool list (JSON body {"spools": [...]})
"""
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit, db, gate, jobs, model3mf, net, pipeline, printers, profiles, watch, worker
from .printers import REPO
from .runlog import RUNS_DIR

STATIC = Path(__file__).resolve().parent / "static"
DATA = REPO / "data"
INVENTORY = DATA / "inventory.json"
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
    if v["klipper"] != "ready":
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


# ---------------------------------------------------------------- batch progress

def batch_view(runs, live, hist_by_file) -> list[dict]:
    out = []
    for path in BATCHES:
        b = json.loads(path.read_text(encoding="utf-8"))
        status = {}
        for r in reversed(runs):  # oldest first, newer runs override
            st = "sliced" if r["result"] == "ok" else None
            if r["uploaded"]:
                st = "queued"
                h = hist_by_file.get(r["uploaded"][0])
                if h:
                    st = {"completed": "done", "cancelled": "failed", "error": "failed",
                          "klippy_shutdown": "failed"}.get(h["status"], st)
            for lab in r["labels"]:
                if st and (status.get(lab) != "done"):
                    status[lab] = st
        for pv in live:
            if pv.get("run") and pv.get("job") in ("printing", "paused"):
                for lab in pv["run"]["labels"]:
                    status[lab] = "printing"
        rows = []
        for row in b["rows"]:
            labels = [f"{row['prefix']}{i:02d}" for i in range(1, row["count"] + 1)]
            rows.append({"name": row.get("name", row["prefix"]),
                         "cells": [{"label": l, "status": status.get(l, "planned")} for l in labels]})
        total = sum(len(r["cells"]) for r in rows)
        done = sum(c["status"] == "done" for r in rows for c in r["cells"])
        beds_left = (total - done) / b.get("plates_per_bed", 1)
        out.append({"name": b["name"], "rows": rows, "total": total, "done": done,
                    "hours_left": beds_left * b.get("hours_per_bed", 0), "note": b.get("note", "")})
    return out


# ---------------------------------------------------------------- builds (review, queue, start)

ACTIVE = ("slicing", "review", "approved", "uploading", "queued", "starting", "printing", "failed")


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
                    "material": pf.get("material"), "viewed": pf.get("viewed"),
                    "printer_status": pf.get("printer_status"), "audit_fails": pf.get("audit_fails", [])}
        elif action == "start":
            remote = gate.start_build(con, build_id, str(body.get("operator", "")).strip()[:80],
                                      body.get("checklist") or {}, body.get("spool_id"), via="ui",
                                      nonce=body.get("nonce"))
            return {"started": remote}
        else:
            raise LookupError(f"unknown action {action}")
        return {"ok": True, "state": jobs.get(con, "build", build_id)["state"]}
    finally:
        con.close()


def overview() -> dict:
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
    inv = json.loads(INVENTORY.read_text(encoding="utf-8")) if INVENTORY.exists() else {"spools": []}
    return {"time": time.time(), "printers": live, "runs": shown[:25], "batches": batch_view(runs, live, hist),
            "builds": builds_view(),
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
        if path == "/api/overview":
            return self._send(200, json.dumps(overview()).encode())
        if path.startswith("/api/cam/"):
            p = printers.PRINTERS.get(path.rsplit("/", 1)[-1])
            if not p:
                return self._send(404, b"{}")
            try:
                cams = net.get_json(f"{p.moonraker}/server/webcams/list", timeout=4)["result"]["webcams"]
                host = urllib.parse.urlparse(p.moonraker).hostname
                with urllib.request.urlopen(f"http://{host}{cams[0]['snapshot_url']}", timeout=5) as r:
                    return self._send(200, r.read(), r.headers.get("Content-Type", "image/jpeg"))
            except Exception:
                return self._send(502, b"")
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
        if path != "/api/inventory":
            return self._send(404, b"{}")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            assert isinstance(body.get("spools"), list)
        except Exception:
            return self._send(400, b'{"error":"expected {\\"spools\\": [...]}"}')
        DATA.mkdir(exist_ok=True)
        INVENTORY.write_text(json.dumps(body, indent=2), encoding="utf-8")
        return self._send(200, b'{"ok":true}')

    def log_message(self, fmt, *args):  # keep the console quiet; the page polls every few seconds
        pass


def serve(host: str, port: int):
    if not INVENTORY.exists() and (DATA / "inventory.example.json").exists():
        INVENTORY.write_text((DATA / "inventory.example.json").read_text(encoding="utf-8"), encoding="utf-8")
    print(f"farm control room on http://{host}:{port}  (Ctrl+C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
