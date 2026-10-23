import copy
import dataclasses
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from farm import db, gate, jobs, makers, pipeline, printers, runlog, spools, worker
from tests.fakes import FakeMaker, FakeMoonraker
from tests.test_audit_model3mf import GOOD
from tests.test_pipeline_worker import FAKE_SLICER, JOB

OK = {"viewed_3d": True, "bed_clear": True, "colour_loaded": True}


class Gate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (runlog.RUNS_DIR, dict(printers.PRINTERS))
        runlog.RUNS_DIR = self.tmp / "runs"
        os.environ["FARM_PRUSASLICER"] = FAKE_SLICER
        os.environ["FARM_ALLOW_ANY_SLICER"] = "1"
        self.con = db.connect(self.tmp / "farm.db")
        self.maker_srv = FakeMaker(self.tmp)
        self.maker = makers.get("likhan")
        self.maker.spec = dataclasses.replace(type(self.maker).spec, url=self.maker_srv.url)
        self.moon = FakeMoonraker(copy.deepcopy(GOOD))
        for name in ("Ender3MaxNeo", "enders1plus", "EnderV3SE"):
            printers.PRINTERS[name] = dataclasses.replace(printers.PRINTERS[name], moonraker=self.moon.url)
        spools.add(self.con, {"id": "SP-1", "material": "PETG", "location": "Ender3MaxNeo", "weight_g": 1000})
        spools.add(self.con, {"id": "SP-9", "material": "PLA", "location": "Ender3MaxNeo", "weight_g": 1000})
        # an order all the way to queued, viewed in the UI
        pipeline.submit_job_file(self.con, JOB)
        for (jid,) in self.con.execute("SELECT id FROM jobs").fetchall():
            worker.enqueue(self.con, "generate", jid)
        self.drain()
        self.bid = jobs.builds(self.con)[0]["id"]
        pipeline.approve(self.con, self.bid, "ui")
        worker.enqueue(self.con, "upload", self.bid)
        self.drain()
        self.b = jobs.get(self.con, "build", self.bid)
        gate.record_view(self.con, Path(self.b["project_3mf"]))

    def tearDown(self):
        runlog.RUNS_DIR, saved_printers = self.saved
        printers.PRINTERS.clear()
        printers.PRINTERS.update(saved_printers)
        os.environ.pop("FARM_ALLOW_ANY_SLICER", None)
        del self.maker.spec
        self.maker_srv.close()
        self.moon.close()

    def drain(self):
        while worker.run_once(self.con, log=lambda m: None):
            pass

    def start(self, **kw):
        args = dict(operator="Amir", checklist=OK, spool_id="SP-1", via="ui", nonce=gate.issue_nonce(self.bid))
        args.update(kw)
        return gate.start_build(self.con, self.bid, args.pop("operator"), args.pop("checklist"),
                                args.pop("spool_id"), **args)

    def assertRefused(self, needle, **kw):
        with self.assertRaises(gate.GateRefused) as e:
            self.start(**kw)
        self.assertIn(needle, str(e.exception))
        self.assertEqual(self.moon.starts, [])
        self.assertEqual(jobs.get(self.con, "build", self.bid)["state"], "queued")

    def test_happy_path_starts_once_and_records_who(self):
        self.assertEqual(self.b["state"], "queued")
        self.assertEqual(self.start(), self.b["remote_path"])
        self.assertEqual(self.moon.starts, [self.b["remote_path"]])
        self.assertEqual(jobs.get(self.con, "build", self.bid)["state"], "printing")
        self.assertEqual({r[0] for r in self.con.execute("SELECT state FROM jobs")}, {"printing"})
        who = self.con.execute("SELECT actor, checklist FROM approvals WHERE kind='start'").fetchone()
        self.assertEqual(who["actor"], "ui:Amir")
        self.assertEqual(json.loads(who["checklist"])["spool"], "SP-1")

    def test_nonce_required_single_use_and_per_build(self):
        self.assertRefused("Start dialog expired", nonce=None)
        self.assertRefused("Start dialog expired", nonce="made-up")
        n = gate.issue_nonce(self.bid + 1)
        self.assertRefused("Start dialog expired", nonce=n)

    def test_checklist_operator_and_spool(self):
        self.assertRefused("checklist not complete: bed_clear", checklist={**OK, "bed_clear": False})
        self.assertRefused("operator", operator="")
        self.assertRefused("choose the spool", spool_id="SP-404")
        self.assertRefused("spool SP-9 is PLA", spool_id="SP-9")

    def test_gcode_changed_after_approval(self):
        Path(self.b["gcode"]).write_text(Path(self.b["gcode"]).read_text() + "; edited\n")
        self.assertRefused("changed after it was approved")

    def test_3d_view_must_have_been_opened_in_the_ui(self):
        self.con.execute("DELETE FROM approvals WHERE kind='viewed'")
        self.assertRefused("3D view of this build hasn't been opened")

    def test_printer_state_and_audit(self):
        self.moon.state = "printing"
        self.assertRefused("is not idle")
        self.moon.state, self.moon.paused = "standby", True
        self.assertRefused("is not idle")
        self.moon.paused = False
        self.moon.settings["printer"]["max_velocity"] = 300
        self.assertRefused("farm baseline audit fails")

    def test_file_on_printer_must_match(self):
        self.moon.files[self.b["remote_path"]] += 1
        self.assertRefused("differs from the approved G-code")
        del self.moon.files[self.b["remote_path"]]
        self.assertRefused("uploaded file not found")

    def test_watch_only_and_busy_printer(self):
        printers.PRINTERS["Ender3MaxNeo"] = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], readonly=True)
        self.assertRefused("watch-only")
        printers.PRINTERS["Ender3MaxNeo"] = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], readonly=False)
        other = jobs.create_build(self.con, self.b["order_id"], "Ender3MaxNeo", [], state="slicing")
        self.con.execute("UPDATE builds SET state='printing' WHERE id=?", (other,))
        self.assertRefused(f"already has build {other}")

    def test_two_starts_race_one_wins(self):
        results, path = [], Path(self.con.execute("PRAGMA database_list").fetchone()[2])
        nonces = [gate.issue_nonce(self.bid) for _ in range(4)]

        def go(n):
            c = db.connect(path)
            try:
                gate.start_build(c, self.bid, "Amir", OK, "SP-1", via="ui", nonce=n)
                results.append("started")
            except gate.GateRefused as e:
                results.append(str(e))

        ts = [threading.Thread(target=go, args=(n,)) for n in nonces]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("started"), 1, results)
        self.assertEqual(len(self.moon.starts), 1)

    def test_moonraker_start_needs_the_gate(self):
        from farm import moonraker
        with self.assertRaises(PermissionError):
            moonraker.start(None, printers.PRINTERS["Ender3MaxNeo"], self.b["remote_path"])
        self.assertEqual(self.moon.starts, [])


if __name__ == "__main__":
    unittest.main()
