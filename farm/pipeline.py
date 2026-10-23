"""The production pipeline as steps over DB rows, so the CLI (inline) and the worker (queued tasks) share it:

  submit   order + jobs from a job file / the New-job form / invtool; one run folder per order
  generate one job -> its maker plugin -> models (STL) in the run folder
  plan     generated jobs -> builds (beds): a bed_group shares one bed, otherwise every piece is its own bed
  slice    one build -> PrusaSlicer with the job's profile -> colour changes -> checks -> project .3mf -> review
  upload   an APPROVED build -> its printer's Moonraker -> queued (starting it is farm/gate.py's job, not this)

Each step moves state with jobs.transition(); a failure records why on the row and raises.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from . import config, db, gcode, jobs, makers, moonraker, printers, profiles, project, slicer
from .runlog import Run


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def duration_s(text: str | None) -> float | None:
    """PrusaSlicer's '2h 44m 7s' / '1d 2h 3m 4s' -> seconds."""
    if not text:
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)([dhms])", text)
    return float(sum(int(n) * units[u] for n, u in parts)) if parts else None


def labels_of(job: dict) -> list[str]:
    if job.get("plates"):
        return [pl["text"] if isinstance(pl, dict) else pl for pl in job["plates"]]
    return [job.get("likhan", {}).get("text") or job["name"]]


class StepFailed(Exception):
    """A step failed; retryable: worth retrying later (maker down, timeout) vs a problem with the job itself."""

    def __init__(self, msg: str, retryable: bool = False):
        super().__init__(msg)
        self.retryable = retryable


# ---------------------------------------------------------------- submit

def submit_job_file(con, job: dict, job_path: Path | None = None, source: str = "cli",
                    external_ref: str | None = None) -> tuple[int, Run]:
    """A farm job file (jobs/*/*.json): one order; one job per plate (all on one bed) or one job."""
    printer = printers.get(job["printer"])
    profile = profiles.get(job["profile"]) if job.get("profile") else profiles.legacy(printer, job["material"])
    run = Run(job["name"])
    if job_path:
        run.path("job.json").write_text(Path(job_path).read_text(encoding="utf-8"), encoding="utf-8")
    else:
        run.path("job.json").write_text(json.dumps(job, indent=2), encoding="utf-8")
    run.manifest["colours"] = job.get("colours", [])
    run.save()
    maker = job.get("maker", "likhan")
    base = dict(job.get(maker) or {})
    inputs = {"font": str(config.FONTS_DIR / job["font"])} if job.get("font") else {}
    with db.tx(con):
        oid = jobs.create_order(con, source, job["name"], external_ref=external_ref or run.dir.name)
        con.execute("UPDATE orders SET run_dir=?, upload_folder=? WHERE id=?",
                    (str(run.dir), job.get("upload_folder", "farm"), oid))
        if job.get("plates"):
            items = [({**base, **pl} if isinstance(pl, dict) else {**base, "text": pl}) for pl in job["plates"]]
            group = "bed1"
        else:
            items, group = [base], None
        for params in items:
            jobs_id = jobs.create_job(
                con, oid, maker, params.get("text") or job["name"], params=params, inputs=inputs,
                profile=profile.id, layer_height=job["layer_height"], printer=printer.name,
                colours=job.get("colours", []), colour_hex=job.get("colour_hex", []),
                colour_changes=job.get("colour_changes", []), actor=source)
            con.execute("UPDATE jobs SET bed_group=? WHERE id=?", (group, jobs_id))
    run.log(f"run: {job['name']} on {printer.name} with profile {profile.id} -> {run.dir}")
    if not profile.confirmed:
        run.log(f"run: profile {profile.id} is UNCONFIRMED ({profile.note}): review only, not for production")
    return oid, run


def run_for(con, order_id: int) -> Run:
    order = jobs.get(con, "order", order_id)
    return Run.resume(Path(order["run_dir"]))


def _fail(con, entity, row, actor, msg, retryable=False):
    with db.tx(con):
        con.execute(f"UPDATE {jobs.TABLES[entity]} SET error=? WHERE id=?", (msg[:2000], row["id"]))
        if (row["state"], "failed") in jobs.TRANSITIONS[entity]:
            jobs.transition(con, entity, row["id"], row["state"], "failed", actor, error=msg[:500])
    raise StepFailed(msg, retryable)


# ---------------------------------------------------------------- generate

def generate_job(con, job_id: int, actor: str = "worker") -> list[int]:
    job = jobs.get(con, "job", job_id)
    if job["state"] in ("received", "failed"):
        jobs.transition(con, "job", job_id, job["state"], "generating", actor)
        job["state"] = "generating"
    elif job["state"] != "generating":
        raise StepFailed(f"job {job_id} is {job['state']}, not ready to generate")
    run = run_for(con, job["order_id"])
    maker = makers.get(job["maker"])
    printer = printers.get(job["printer"])
    profile = profiles.get(job["profile"])
    ctx = makers.GenContext(
        params=job["params"], files={k: Path(v) for k, v in job["inputs"].items()},
        bed_mm=(printer.bed_max[0] - printer.bed_min[0], printer.bed_max[1] - printer.bed_min[1], printer.max_z),
        nozzle=profile.nozzle, layer_height=job["layer_height"], out_dir=run.dir, log=run.log, label=job["label"])
    for name, path in ctx.files.items():
        if not path.is_file():
            run.log(f"generate: input {name} missing: {path}")
            _fail(con, "job", job, actor, f"{name} file not found: {path}")
    problems = maker.validate(job["params"], ctx.files, ctx)
    if problems:
        _fail(con, "job", job, actor, "invalid parameters: " + "; ".join(problems))
    try:
        res = maker.generate(ctx)
    except makers.MakerError as e:
        run.log(f"generate: {job['label']} FAILED: {e}")
        _fail(con, "job", job, actor, str(e), e.retryable)
    for w in res.warnings:
        run.log(f"generate: warning: {w}")
    run.step(maker.spec.id, params=job["params"], response=res.stats, stls=[str(m.path) for m in res.models])
    with db.tx(con):
        ids = [con.execute("INSERT INTO models (job_id, path, kind, label, bbox, triangles, sha256) "
                           "VALUES (?,?,?,?,?,?,?)",
                           (job_id, str(m.path), m.path.suffix.lstrip(".").lower(), m.label, db.dumps(m.bbox),
                            m.triangles, sha256(m.path))).lastrowid for m in res.models]
        con.execute("UPDATE jobs SET colour_zs=?, maker_version=?, error=NULL WHERE id=?",
                    (db.dumps(res.colour_zs), res.maker_version, job_id))
        jobs.transition(con, "job", job_id, "generating", "generated", actor, models=len(ids))
    return ids


# ---------------------------------------------------------------- plan

def _change_zs(job: dict) -> list[float]:
    out = []
    for z in job["colour_changes"]:
        if isinstance(z, str) and not _is_number(z):
            if z not in job["colour_zs"]:
                raise StepFailed(f"job {job['label']}: colour change {z!r} unknown to maker {job['maker']} "
                                 f"(it reports {sorted(job['colour_zs'])})")
            out.append(float(job["colour_zs"][z]))
        else:
            out.append(float(z))
    return out


def _is_number(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def plan_builds(con, order_id: int, actor: str = "worker") -> list[int]:
    """Generated jobs of an order -> builds. Jobs sharing a bed_group go on one bed (in job order); any other
    job puts each of its pieces on its own bed. A bed needs one printer, profile, layer and colour plan."""
    rows = [db.row(r) for r in con.execute(
        "SELECT * FROM jobs WHERE order_id=? AND state='generated' ORDER BY id", (order_id,))]
    beds = []   # (jobs on the bed, model rows)
    groups = {}
    for j in rows:
        models_ = [db.row(r) for r in con.execute("SELECT * FROM models WHERE job_id=? ORDER BY id", (j["id"],))]
        if j["bed_group"]:
            g = groups.setdefault(j["bed_group"], ([], []))
            g[0].append(j)
            g[1].extend(models_)
        else:
            beds += [([j], [m]) for m in models_]
    beds = list(groups.values()) + beds if groups else beds
    built = []
    for bed_jobs, bed_models in beds:
        first = bed_jobs[0]
        plan = {(j["printer"], j["profile"], j["layer_height"], tuple(_change_zs(j))) for j in bed_jobs}
        if len(plan) != 1:
            raise StepFailed(f"jobs {[j['label'] for j in bed_jobs]} share a bed but differ in printer/profile/"
                             f"layer/colour changes: {sorted(plan)}")
        with db.tx(con):
            bid = jobs.create_build(
                con, order_id, first["printer"], [j["id"] for j in bed_jobs], profile=first["profile"],
                run_dir=jobs.get(con, "order", order_id)["run_dir"], layer_height=first["layer_height"],
                change_zs=_change_zs(first), colours=first["colours"], colour_hex=first["colour_hex"],
                state="slicing", actor=actor)
            for pos, m in enumerate(bed_models):
                con.execute("INSERT INTO build_models (build_id, model_id, pos) VALUES (?,?,?)", (bid, m["id"], pos))
            for j in bed_jobs:
                if jobs.get(con, "job", j["id"])["state"] == "generated":
                    jobs.transition(con, "job", j["id"], "generated", "planned", actor, build=bid)
        built.append(bid)
    return built


def build_models(con, build_id: int) -> list[dict]:
    return [db.row(r) for r in con.execute(
        "SELECT m.* FROM build_models bm JOIN models m ON m.id = bm.model_id WHERE bm.build_id=? ORDER BY bm.pos",
        (build_id,))]


# ---------------------------------------------------------------- slice

def slice_build(con, build_id: int, actor: str = "worker") -> dict:
    b = jobs.get(con, "build", build_id)
    if b["state"] != "slicing":
        raise StepFailed(f"build {build_id} is {b['state']}, not slicing")
    run = run_for(con, b["order_id"])
    printer = printers.get(b["printer"])
    profile = profiles.get(b["profile"])
    stls = [Path(m["path"]) for m in build_models(con, build_id)]
    try:
        out, cmd, colour_layers = slicer.slice_model(run, printer, stls, profile.material, b["layer_height"],
                                                     b["change_zs"], profile=profile)
        facts = gcode.validate(run, out, printer, b["change_zs"], max_flow=profile.max_volumetric)
        proj = project.export_project(run, cmd, out, colour_layers, b["colour_hex"])
    except SystemExit as e:   # the stages report problems with run.fail
        _fail(con, "build", b, actor, str(e) or "slice failed")
    with db.tx(con):
        jobs.update_build(con, build_id, gcode=str(out), gcode_sha256=sha256(out), project_3mf=str(proj),
                          estimate_s=duration_s(facts.get("estimated printing time (normal mode)")),
                          grams=float(facts.get("filament used [g]", 0) or 0), error=None)
        jobs.transition(con, "build", build_id, "slicing", "review", actor)
    run.manifest["result"] = "ok"
    run.save()
    return facts


# ---------------------------------------------------------------- upload

def upload_build(con, build_id: int, actor: str = "worker") -> str:
    """Only an approved build is uploaded: approval is a person's decision (jobs.TRANSITIONS)."""
    b = jobs.get(con, "build", build_id)
    if b["state"] not in ("approved", "uploading"):   # uploading: an interrupted upload, safe to redo
        raise StepFailed(f"build {build_id} is {b['state']}, only approved builds are uploaded")
    profile = profiles.get(b["profile"])
    if not profile.confirmed:
        raise StepFailed(f"profile {profile.id} is unconfirmed ({profile.note}); not uploading")
    if b["state"] == "approved":
        jobs.transition(con, "build", build_id, "approved", "uploading", actor)
    run = run_for(con, b["order_id"])
    order = jobs.get(con, "order", b["order_id"])
    printer = printers.get(b["printer"])
    try:
        remote = moonraker.upload(run, printer, Path(b["gcode"]), order["upload_folder"] or "farm")
    except Exception as e:
        _fail(con, "build", {**b, "state": "uploading"}, actor, f"upload: {e}", retryable=True)
    with db.tx(con):
        jobs.update_build(con, build_id, remote_path=remote)
        jobs.transition(con, "build", build_id, "uploading", "queued", actor, remote=remote)
    run.manifest.setdefault("uploaded", [])
    if remote not in run.manifest["uploaded"]:
        run.manifest["uploaded"].append(remote)
    run.save()
    return remote


def slicer_version_of(gcode_path: Path) -> str | None:
    """'; generated by PrusaSlicer 2.9.6+linux-x64-... on ...' -> '2.9.6' (first lines of the G-code)."""
    with open(gcode_path, encoding="utf-8", errors="replace") as f:
        for _ in range(20):
            m = re.match(r"; generated by PrusaSlicer[ -]([\d.]+)", f.readline())
            if m:
                return m.group(1)
    return None


def approve(con, build_id: int, actor: str, note: str = "") -> None:
    """The visual gate passed for this G-code (recorded with its hash, so a re-slice needs a new approval)."""
    b = jobs.get(con, "build", build_id)
    profile = profiles.get(b["profile"])
    if not profile.confirmed:
        raise StepFailed(f"profile {profile.id} is unconfirmed ({profile.note}); can't approve for production")
    version = slicer_version_of(Path(b["gcode"]))
    if version != config.PRODUCTION_SLICER and not os.environ.get("FARM_ALLOW_ANY_SLICER"):
        raise StepFailed(f"G-code was sliced by PrusaSlicer {version}; production needs {config.PRODUCTION_SLICER} "
                         f"(install it, then re-slice)")
    with db.tx(con):
        con.execute("INSERT INTO approvals (build_id, kind, gcode_sha256, actor, at, checklist) VALUES (?,?,?,?,?,?)",
                    (build_id, "review", b["gcode_sha256"], actor, db.now(), db.dumps({"note": note})))
        jobs.transition(con, "build", build_id, "review", "approved", actor, note=note)


def import_runs(con, runs_dir: Path) -> list[str]:
    """Backfill runs made before the DB existed (or on another host) as orders/jobs/builds. Idempotent: a run
    folder already known (any source) is skipped. States are a best guess - the tracker corrects them later."""
    done = []
    for m in sorted(Path(runs_dir).glob("*/manifest.json")):
        d = m.parent
        if con.execute("SELECT 1 FROM orders WHERE external_ref=?", (d.name,)).fetchone():
            continue
        try:
            man = json.loads(m.read_text(encoding="utf-8"))
            job = json.loads((d / "job.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        result = str(man.get("result", "running"))
        failed = result.startswith("FAILED") or result == "running"
        with db.tx(con):
            oid = jobs.create_order(con, "import", job.get("name", d.name), external_ref=d.name, notes=result)
            jids = [jobs.create_job(con, oid, "likhan", label, params=job.get("likhan", {}),
                                    layer_height=job.get("layer_height"), printer=job.get("printer"),
                                    colours=job.get("colours", []), colour_hex=job.get("colour_hex", []),
                                    colour_changes=job.get("colour_changes", []),
                                    state="failed" if failed else "planned", actor="import")
                    for label in labels_of(job)]
            uploaded = man.get("uploaded") or []
            for i, g in enumerate(sorted(d.glob("*.gcode"))):
                if failed:
                    break
                remote = uploaded[i] if i < len(uploaded) else None
                jobs.create_build(con, oid, job.get("printer", "?"), jids, run_dir=str(d), gcode=str(g),
                                  gcode_sha256=sha256(g), remote_path=remote,
                                  project_3mf=str(g.with_suffix(".3mf")) if g.with_suffix(".3mf").exists() else None,
                                  state="queued" if remote else "review", actor="import")
        done.append(d.name)
    return done
