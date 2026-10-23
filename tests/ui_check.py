"""Headless end-to-end check of Review & queue + the Start dialog, against fakes only (no real printer).

    python3 tests/ui_check.py      # prepares a throwaway DB, serves on :10199, waits; drive it with a browser
Prints READY <url> <fake-moonraker-url>; Ctrl+C / kill to stop. Used by tests/ui_check.cjs (Playwright).
"""
import copy
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farm import db, gate, jobs, makers, pipeline, printers, runlog, worker  # noqa: E402
from tests.fakes import FakeMaker, FakeMoonraker  # noqa: E402
from tests.test_audit_model3mf import GOOD  # noqa: E402
from tests.test_pipeline_worker import FAKE_SLICER, JOB  # noqa: E402

tmp = Path(tempfile.mkdtemp())
os.environ.update(FARM_PRUSASLICER=FAKE_SLICER, FARM_ALLOW_ANY_SLICER="1")
runlog.RUNS_DIR = tmp / "runs"
con = db.connect(tmp / "farm.db")
maker_srv, moon = FakeMaker(tmp), FakeMoonraker(copy.deepcopy(GOOD))
m = makers.get("likhan")
m.spec = dataclasses.replace(type(m).spec, url=maker_srv.url)
override = {n: moon.url for n in printers.PRINTERS}     # every registry printer is the fake: nothing real is contacted
for n, u in override.items():
    printers.PRINTERS[n] = dataclasses.replace(printers.PRINTERS[n], moonraker=u)
for name in ("Review me", "Start me"):
    pipeline.submit_job_file(con, dict(JOB, name=name))
for (jid,) in con.execute("SELECT id FROM jobs").fetchall():
    worker.enqueue(con, "generate", jid)
while worker.run_once(con, log=lambda x: None):
    pass
b_review, b_start = sorted(b["id"] for b in jobs.builds(con))
gate.record_view(con, Path(jobs.get(con, "build", b_start)["project_3mf"]))
pipeline.approve(con, b_start, "ui")
worker.enqueue(con, "upload", b_start)
while worker.run_once(con, log=lambda x: None):
    pass
from farm import spools  # noqa: E402
spools.add(con, {"id": "SP-T1", "material": "PETG", "colour": "Black", "location": "Ender3MaxNeo", "weight_g": 1000})
spools.add(con, {"id": "SP-T2", "material": "PETG", "colour": "White", "location": "shelf", "weight_g": 1000})
env = dict(os.environ, FARM_DB=str(tmp / "farm.db"), FARM_RUNS_DIR=str(tmp / "runs"),
           FARM_MOONRAKER_OVERRIDE=json.dumps(override), FARM_INVENTORY=str(tmp / "inventory.json"), FARM_TEMPLATES=str(tmp / "templates.json"))
srv = subprocess.Popen([sys.executable, "-m", "farm", "serve", "--host", "127.0.0.1", "--port", "10199"],
                       cwd=Path(__file__).resolve().parent.parent, env=env)
time.sleep(1.5)
print(f"READY http://127.0.0.1:10199 {moon.url} review={b_review} queued={b_start}", flush=True)
try:
    while True:
        time.sleep(1)
        while worker.run_once(con, log=lambda x: None):   # the harness is also the worker (fake likhan, fake slicer)
            pass
        Path(tmp / "starts.json").write_text(json.dumps(moon.starts))
        print(f"STARTS {json.dumps(moon.starts)}", flush=True) if moon.starts else None
except KeyboardInterrupt:
    pass
finally:
    srv.terminate()
