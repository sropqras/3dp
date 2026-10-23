import dataclasses
import os
import sys
import tempfile
import unittest
from pathlib import Path

from farm import db, jobs, makers, moonraker, pipeline, runlog, worker
from tests.fakes import FakeMaker

FAKE_SLICER = f"{sys.executable} {Path(__file__).resolve().parent / 'fake_slicer.py'}"
JOB = {"name": "two plates", "printer": "Ender3MaxNeo", "material": "petg", "layer_height": 0.4,
       "colour_changes": ["base_top"], "colours": ["black", "white"], "colour_hex": ["#111111", "#EEEEEE"],
       "likhan": {"mode": "text", "base_thickness_mm": 4, "font_size_mm": 90},
       "plates": [{"text": "T02", "font_size_mm": 66}, "T11"]}


class Pipeline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (runlog.RUNS_DIR, os.environ.get("FARM_PRUSASLICER"))
        runlog.RUNS_DIR = self.tmp / "runs"
        os.environ["FARM_PRUSASLICER"] = FAKE_SLICER
        os.environ["FARM_ALLOW_ANY_SLICER"] = "1"   # the fake slicer isn't PrusaSlicer 2.9.6
        self.con = db.connect(self.tmp / "farm.db")
        self.fake = FakeMaker(self.tmp)
        self.maker = makers.get("likhan")
        self.maker.spec = dataclasses.replace(type(self.maker).spec, url=self.fake.url)
        self.uploads = []
        self._upload = moonraker.upload
        moonraker.upload = lambda run, printer, g, folder: self.uploads.append((printer.name, g.name)) or f"{folder}/{g.name}"

    def tearDown(self):
        runlog.RUNS_DIR = self.saved[0]
        if self.saved[1] is None:
            os.environ.pop("FARM_PRUSASLICER", None)
        else:
            os.environ["FARM_PRUSASLICER"] = self.saved[1]
        os.environ.pop("FARM_ALLOW_ANY_SLICER", None)
        del self.maker.spec
        moonraker.upload = self._upload
        self.fake.close()

    def drain(self):
        while worker.run_once(self.con, log=lambda m: None):
            pass

    def submit(self, job=JOB):
        oid, run = pipeline.submit_job_file(self.con, job)
        for (jid,) in self.con.execute("SELECT id FROM jobs WHERE order_id=?", (oid,)).fetchall():
            worker.enqueue(self.con, "generate", jid)
        return oid

    def test_worker_takes_an_order_to_review_and_stops_there(self):
        oid = self.submit()
        self.drain()
        bs = jobs.builds(self.con)
        self.assertEqual(len(bs), 1)                                    # two plates, one bed
        b = bs[0]
        self.assertEqual((b["state"], b["labels"], b["change_zs"]), ("review", "T02 T11", [4.0]))
        self.assertTrue(Path(b["gcode"]).is_file() and Path(b["project_3mf"]).is_file())
        self.assertEqual(b["grams"], 21.5)
        self.assertEqual({r[0] for r in self.con.execute("SELECT state FROM jobs")}, {"planned"})
        self.assertEqual(len(self.fake.requests), 2)
        self.assertIn(b'name="bed_width_mm"\r\n\r\n290', self.fake.requests[0][1])   # bed from the printer
        self.assertEqual(self.uploads, [])                               # nothing goes to a printer unapproved

    def test_approval_is_a_person_and_upload_follows(self):
        self.submit()
        self.drain()
        bid = jobs.builds(self.con)[0]["id"]
        with self.assertRaises(jobs.StateConflict):
            pipeline.approve(self.con, bid, "worker")
        pipeline.approve(self.con, bid, "ui", note="looked at the 3D view")
        worker.enqueue(self.con, "upload", bid)
        self.drain()
        b = jobs.get(self.con, "build", bid)
        self.assertEqual(b["state"], "queued")
        self.assertEqual(self.uploads, [("Ender3MaxNeo", Path(b["gcode"]).name)])
        appr = self.con.execute("SELECT actor, gcode_sha256 FROM approvals WHERE build_id=?", (bid,)).fetchone()
        self.assertEqual(tuple(appr), ("ui", b["gcode_sha256"]))

    def test_unconfirmed_profile_slices_but_cannot_be_approved(self):
        job = dict(JOB, profile="neo-hyperpla-028-litho", layer_height=0.28, colour_changes=[])
        self.submit(job)
        self.drain()
        b = jobs.builds(self.con)[0]
        self.assertEqual(b["state"], "review")
        self.assertIn("EXTRUDER_TEMP=210 BED_TEMP=70", Path(b["gcode"]).read_text())
        with self.assertRaises(pipeline.StepFailed):
            pipeline.approve(self.con, b["id"], "ui")

    def test_maker_down_is_retried_later_bad_request_is_not(self):
        self.fake.fail_with = 503
        self.submit(dict(JOB, plates=["T11"]))
        self.drain()
        t = self.con.execute("SELECT state, attempts, run_after, last_error FROM tasks").fetchone()
        self.assertEqual(t["state"], "pending")
        self.assertGreater(t["run_after"], db.now() + 30)
        self.assertIn("503", t["last_error"])
        self.assertEqual(self.con.execute("SELECT state FROM jobs").fetchone()[0], "failed")
        # maker back: the retry (forced due now) generates and the order goes on to review
        self.fake.fail_with = None
        self.con.execute("UPDATE tasks SET run_after=0")
        self.drain()
        self.assertEqual(jobs.builds(self.con)[0]["state"], "review")

        self.fake.fail_with = 400
        self.submit(dict(JOB, name="bad", plates=["X"]))
        self.drain()
        states = [r[0] for r in self.con.execute("SELECT state FROM tasks ORDER BY id")]
        self.assertEqual(states[-1], "failed")

    def test_only_production_slicer_output_can_be_approved(self):
        self.submit()
        self.drain()
        bid = jobs.builds(self.con)[0]["id"]
        os.environ.pop("FARM_ALLOW_ANY_SLICER")
        with self.assertRaises(pipeline.StepFailed) as e:
            pipeline.approve(self.con, bid, "ui")
        self.assertIn("production needs 2.9.6", str(e.exception))
        os.environ["FARM_ALLOW_ANY_SLICER"] = "1"

    def test_expired_lease_is_reclaimed(self):
        self.submit(dict(JOB, plates=["T11"]))
        t = worker.claim(self.con)
        self.assertIsNone(worker.claim(self.con))                     # leased: nobody else gets it
        self.con.execute("UPDATE tasks SET lease_until=? WHERE id=?", (db.now() - 1, t["id"]))
        t2 = worker.claim(self.con)
        self.assertEqual((t2["id"], t2["attempts"]), (t["id"], 2))

    def test_enqueue_is_idempotent(self):
        self.assertIsNotNone(worker.enqueue(self.con, "slice", 99))
        self.assertIsNone(worker.enqueue(self.con, "slice", 99))

    def test_beds_must_agree(self):
        oid = self.submit(dict(JOB, plates=["T11", "T12"]))
        self.con.execute("UPDATE jobs SET layer_height=0.28 WHERE label='T12'")
        while worker.run_once(self.con, log=lambda m: None):
            pass
        failed = self.con.execute("SELECT last_error FROM tasks WHERE kind='plan'").fetchone()[0]
        self.assertIn("share a bed but differ", failed)


if __name__ == "__main__":
    unittest.main()
