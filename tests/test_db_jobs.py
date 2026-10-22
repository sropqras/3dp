import json
import tempfile
import threading
import unittest
from pathlib import Path

from farm import db, jobs, pipeline, printers
from tests.helpers import FakeRun


def fresh():
    return db.connect(Path(tempfile.mkdtemp()) / "farm.db")


class Migrations(unittest.TestCase):
    def test_idempotent_and_versioned(self):
        path = Path(tempfile.mkdtemp()) / "farm.db"
        a = db.connect(path)
        self.assertEqual(a.execute("PRAGMA user_version").fetchone()[0], len(db.MIGRATIONS))
        b = db.connect(path)   # second connect: nothing re-applied, no error
        self.assertEqual(db.migrate(b), len(db.MIGRATIONS))
        self.assertEqual(a.execute("PRAGMA journal_mode").fetchone()[0], "wal")


class Transitions(unittest.TestCase):
    def setUp(self):
        self.con = fresh()
        self.oid = jobs.create_order(self.con, "ui", "test")
        self.jid = jobs.create_job(self.con, self.oid, "likhan", "A01")
        self.bid = jobs.create_build(self.con, self.oid, "Ender3MaxNeo", [self.jid])

    def test_happy_path_writes_events(self):
        jobs.transition(self.con, "build", self.bid, "slicing", "review", "worker")
        jobs.transition(self.con, "build", self.bid, "review", "approved", "ui")
        self.assertEqual(jobs.get(self.con, "build", self.bid)["state"], "approved")
        trail = [(e["from_state"], e["to_state"], e["actor"]) for e in jobs.history(self.con, "build", self.bid)]
        self.assertEqual(trail, [(None, "slicing", "cli"), ("slicing", "review", "worker"),
                                 ("review", "approved", "ui")])

    def test_unknown_transition_refused(self):
        with self.assertRaises(jobs.StateConflict):
            jobs.transition(self.con, "build", self.bid, "slicing", "printing", "worker")

    def test_only_a_person_approves_and_only_the_gate_starts(self):
        jobs.transition(self.con, "build", self.bid, "slicing", "review", "worker")
        with self.assertRaises(jobs.StateConflict):
            jobs.transition(self.con, "build", self.bid, "review", "approved", "worker")
        for frm, to in (("review", "approved"), ("approved", "uploading"), ("uploading", "queued")):
            jobs.transition(self.con, "build", self.bid, frm, to, "ui" if to == "approved" else "worker")
        for actor in ("ui", "cli", "worker", "tracker"):
            with self.assertRaises(jobs.StateConflict, msg=actor):
                jobs.transition(self.con, "build", self.bid, "queued", "starting", actor)
        jobs.transition(self.con, "build", self.bid, "queued", "starting", "gate")

    def test_wrong_expected_state_is_a_conflict_and_changes_nothing(self):
        with self.assertRaises(jobs.StateConflict):
            jobs.transition(self.con, "build", self.bid, "review", "approved", "ui")
        self.assertEqual(jobs.get(self.con, "build", self.bid)["state"], "slicing")
        self.assertEqual(len(jobs.history(self.con, "build", self.bid)), 1)

    def test_race_exactly_one_wins(self):
        path = Path(self.con.execute("PRAGMA database_list").fetchone()[2])
        jobs.transition(self.con, "build", self.bid, "slicing", "review", "worker")
        wins, conflicts = [], []

        def approve():
            c = db.connect(path)
            try:
                jobs.transition(c, "build", self.bid, "review", "approved", "ui")
                wins.append(1)
            except jobs.StateConflict:
                conflicts.append(1)

        ts = [threading.Thread(target=approve) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual((len(wins), len(conflicts)), (1, 7))

    def test_order_is_idempotent_on_external_ref(self):
        a = jobs.create_order(self.con, "invtool", "x", external_ref="PR-1")
        b = jobs.create_order(self.con, "invtool", "x again", external_ref="PR-1")
        self.assertEqual(a, b)


class Shadow(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        db.DB_PATH = self.tmp / "farm.db"   # Recorder connects to the default path

    def test_recorder_mirrors_a_cli_run(self):
        run = FakeRun(self.tmp / "run")
        run.dir.mkdir()
        job = {"name": "two plates", "plates": ["A01", "A02"], "layer_height": 0.4, "likhan": {"text": "x"}}
        rec = pipeline.Recorder(run, job, printers.PRINTERS["Ender3MaxNeo"])
        rec.generated()
        g = run.path("A01+A02.gcode")
        g.write_text("G1\n")
        rec.build_sliced(g, {"estimated printing time (normal mode)": "2h 44m 7s", "filament used [g]": "117.5"},
                         run.path("A01+A02.3mf"))
        rec.uploaded(["farm/A01+A02.gcode"])
        con = db.connect()
        b = jobs.builds(con)[0]
        self.assertEqual((b["state"], b["labels"], b["estimate_s"], b["grams"]), ("queued", "A01 A02", 9847.0, 117.5))
        self.assertEqual(b["gcode_sha256"], pipeline.sha256(g))
        self.assertEqual({j["state"] for j in map(dict, con.execute("SELECT state FROM jobs"))}, {"planned"})
        notes = [e["detail"].get("note") for e in jobs.history(con, "build", b["id"])]
        self.assertIn("legacy run --upload", notes)
        self.assertFalse([l for l in run.lines if l.startswith("db:")], run.lines)

    def test_db_failure_never_stops_production(self):
        db.DB_PATH = Path("/proc/forbidden/farm.db")
        run = FakeRun(self.tmp / "run2")
        rec = pipeline.Recorder(run, {"name": "x", "likhan": {}}, printers.PRINTERS["Ender3MaxNeo"])
        rec.generated()
        rec.failed("whatever")
        self.assertTrue(any(l.startswith("db: _open skipped") for l in run.lines))

    def test_import_runs_is_idempotent(self):
        runs = self.tmp / "runs"
        d = runs / "20261022-230600_Qabrastaan_CH02_A11"
        d.mkdir(parents=True)
        (d / "job.json").write_text(json.dumps({"name": "CH02+A11", "printer": "Ender3MaxNeo",
                                                "plates": ["CH02", "A11"], "likhan": {}}))
        (d / "manifest.json").write_text(json.dumps({"result": "ok", "uploaded": ["farm/q/CH02+A11.gcode"]}))
        (d / "CH02+A11.gcode").write_text("G1\n")
        con = db.connect()
        self.assertEqual(pipeline.import_runs(con, runs), [d.name])
        self.assertEqual(pipeline.import_runs(con, runs), [])
        b = jobs.builds(con)[0]
        self.assertEqual((b["state"], b["remote_path"], b["labels"]), ("queued", "farm/q/CH02+A11.gcode", "CH02 A11"))

    def test_duration(self):
        self.assertEqual(pipeline.duration_s("1d 2h 3m 4s"), 93784.0)
        self.assertIsNone(pipeline.duration_s(None))


if __name__ == "__main__":
    unittest.main()
