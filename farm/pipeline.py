"""The production pipeline's bookkeeping: mirrors what a run does into the farm DB (orders, jobs, builds).

Phase 1 (shadow): the CLI pipeline still decides everything; Recorder writes what happened so the queue,
review and tracker that come next have real data. A DB problem is logged, never allowed to stop production.
"""
import hashlib
import json
import re
from pathlib import Path

from . import db, jobs


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


class Recorder:
    """Shadow writes for one CLI run. Every method swallows and logs its own errors (shadow mode)."""

    def __init__(self, run, job: dict, printer, maker: str = "likhan"):
        self.run, self.job, self.printer, self.maker = run, job, printer, maker
        self.con = self.order_id = None
        self.job_ids, self.build_ids = [], []
        self._safe(self._open)

    def _safe(self, fn, *a):
        if self.con is None and fn != self._open:   # bound methods: compare with ==, not is
            return None
        try:
            return fn(*a)
        except Exception as e:  # shadow mode: production goes on
            self.run.log(f"db: {fn.__name__} skipped ({type(e).__name__}: {e})")
            return None

    def _open(self):
        self.con = db.connect()
        self.order_id = jobs.create_order(self.con, "cli", self.job["name"], external_ref=self.run.dir.name)
        for label in labels_of(self.job):
            jid = jobs.create_job(
                self.con, self.order_id, self.maker, label, params=self.job.get(self.maker, {}),
                profile=self.job.get("profile"), layer_height=self.job.get("layer_height"),
                printer=self.printer.name, colours=self.job.get("colours", []),
                colour_hex=self.job.get("colour_hex", []), colour_changes=self.job.get("colour_changes", []))
            jobs.transition(self.con, "job", jid, "received", "generating", "cli")
            self.job_ids.append(jid)

    def generated(self):
        self._safe(self._generated)

    def _generated(self):
        for jid in self.job_ids:
            jobs.transition(self.con, "job", jid, "generating", "generated", "cli")

    def build_sliced(self, gcode: Path, facts: dict, project: Path) -> None:
        self._safe(self._build_sliced, gcode, facts, project)

    def _build_sliced(self, gcode, facts, project):
        bid = jobs.create_build(
            self.con, self.order_id, self.printer.name, self.job_ids, profile=self.job.get("profile"),
            run_dir=str(self.run.dir), gcode=str(gcode), gcode_sha256=sha256(gcode), project_3mf=str(project),
            estimate_s=duration_s(facts.get("estimated printing time (normal mode)")),
            grams=float(facts.get("filament used [g]", 0) or 0))
        jobs.transition(self.con, "build", bid, "slicing", "review", "cli")
        for jid in self.job_ids:
            if jobs.get(self.con, "job", jid)["state"] == "generated":
                jobs.transition(self.con, "job", jid, "generated", "planned", "cli")
        self.build_ids.append(bid)

    def uploaded(self, remotes: list[str]) -> None:
        self._safe(self._uploaded, remotes)

    def _uploaded(self, remotes):
        # the legacy CLI uploads without the review gate; say so in the trail instead of pretending
        for bid, remote in zip(self.build_ids, remotes):
            jobs.transition(self.con, "build", bid, "review", "approved", "cli", note="legacy run --upload")
            jobs.transition(self.con, "build", bid, "approved", "uploading", "cli")
            jobs.update_build(self.con, bid, remote_path=remote)
            jobs.transition(self.con, "build", bid, "uploading", "queued", "cli")

    def failed(self, msg: str) -> None:
        self._safe(self._failed, msg)

    def _failed(self, msg):
        for jid in self.job_ids:
            if jobs.get(self.con, "job", jid)["state"] == "generating":
                jobs.transition(self.con, "job", jid, "generating", "failed", "cli", error=msg)


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
