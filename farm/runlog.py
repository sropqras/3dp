"""One folder per run under runs/, with a run.log mirrored to the console and a manifest.json."""
import json
import os
from contextlib import contextmanager
import re
import time
from pathlib import Path

from .printers import REPO

# tests and the golden harness point this elsewhere so they never mix with real runs
RUNS_DIR = Path(os.environ.get("FARM_RUNS_DIR", REPO / "runs"))


class Run:
    def __init__(self, job_name: str):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", job_name).strip("_")
        base = f"{time.strftime('%Y%m%d-%H%M%S')}_{slug}"
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        for n in range(1, 100):   # two runs of one job in the same second get -2, -3, ...
            self.dir = RUNS_DIR / (base if n == 1 else f"{base}-{n}")
            try:
                self.dir.mkdir()
                break
            except FileExistsError:
                continue
        self.manifest = {"job": job_name, "started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}
        self._log = open(self.dir / "run.log", "a", encoding="utf-8")

    @classmethod
    def resume(cls, run_dir: Path) -> "Run":
        run = cls.__new__(cls)
        run.dir = Path(run_dir)
        run.manifest = json.loads((run.dir / "manifest.json").read_text(encoding="utf-8"))
        run._log = open(run.dir / "run.log", "a", encoding="utf-8")
        return run

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        self._log.write(line + "\n")
        self._log.flush()

    def path(self, name: str) -> Path:
        return self.dir / name

    def step(self, name: str, **data) -> None:
        """steps[name] keeps the latest (what readers use); step_log keeps every call, so a multi-bed run
        doesn't lose the earlier beds' slice/validate/project facts."""
        self.manifest["steps"][name] = data
        self.manifest.setdefault("step_log", []).append({"step": name, **data})
        self.save()

    def save(self) -> None:
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest, indent=2, default=str), encoding="utf-8")

    @contextmanager
    def guard(self):
        """Every way out of a run leaves a result: an exception or a bare SystemExit from a stage (preset not
        found, colour change above the print, ...) is recorded as FAILED instead of 'running' forever."""
        try:
            yield self
        except BaseException as e:
            if not str(self.manifest.get("result", "")).startswith("FAILED"):
                msg = str(e) or type(e).__name__
                self.manifest["result"] = f"FAILED: {msg}"
                self.log(f"FAILED: {msg}")
                self.save()
            raise

    def fail(self, msg: str):
        self.manifest["result"] = f"FAILED: {msg}"
        self.log(f"FAILED: {msg}")
        self.save()
        raise SystemExit(1)
