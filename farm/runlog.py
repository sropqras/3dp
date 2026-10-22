"""One folder per run under runs/, with a run.log mirrored to the console and a manifest.json."""
import json
import re
import time
from pathlib import Path

from .printers import REPO

RUNS_DIR = REPO / "runs"


class Run:
    def __init__(self, job_name: str):
        slug = re.sub(r"[^A-Za-z0-9._-]+", "_", job_name).strip("_")
        self.dir = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{slug}"
        self.dir.mkdir(parents=True)
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
        self.manifest["steps"][name] = data
        self.save()

    def save(self) -> None:
        (self.dir / "manifest.json").write_text(json.dumps(self.manifest, indent=2, default=str), encoding="utf-8")

    def fail(self, msg: str):
        self.manifest["result"] = f"FAILED: {msg}"
        self.log(f"FAILED: {msg}")
        self.save()
        raise SystemExit(1)
