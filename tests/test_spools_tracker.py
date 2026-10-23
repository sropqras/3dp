import json
import tempfile
import unittest
from pathlib import Path

from farm import db, gcode, jobs, spools, tracker
from tests import helpers


class Spools(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")

    def test_migrate_json_once_keeps_remaining_and_a_copy(self):
        d = Path(tempfile.mkdtemp())
        f = d / "inventory.json"
        f.write_text(json.dumps({"spools": [{"id": "SP-1", "material": "PETG", "weight_g": 1000, "remaining_g": 900,
                                             "location": "Ender3MaxNeo"}]}))
        self.assertEqual(spools.migrate_json(self.con, f), 1)
        self.assertEqual(spools.migrate_json(self.con, f), 0)
        self.assertEqual(spools.get(self.con, "SP-1")["remaining_g"], 900)
        self.assertTrue(list(d.glob("inventory.json.migrated-*")))

    def test_stale_edit_is_refused(self):
        s = spools.add(self.con, {"id": "SP-1", "material": "PETG", "weight_g": 1000})
        spools.patch(self.con, "SP-1", s["version"], {"remaining_g": 800})
        with self.assertRaises(spools.Conflict):
            spools.patch(self.con, "SP-1", s["version"], {"remaining_g": 700})
        self.assertEqual(spools.get(self.con, "SP-1")["remaining_g"], 800)

    def test_deduct_is_idempotent_and_prorates_a_stopped_print(self):
        spools.add(self.con, {"id": "A", "weight_g": 1000})
        spools.add(self.con, {"id": "B", "weight_g": 1000})
        segs = [{"colour_idx": 0, "mm": 100.0, "grams": 30.0}, {"colour_idx": 1, "mm": 50.0, "grams": 15.0}]
        self.assertEqual(spools.deduct(self.con, 7, 1, ["A", "B"], segs), [("A", 0, 30.0), ("B", 1, 15.0)])
        self.assertEqual(spools.deduct(self.con, 7, 1, ["A", "B"], segs), [])          # replay: nothing new
        self.assertEqual((spools.get(self.con, "A")["remaining_g"], spools.get(self.con, "B")["remaining_g"]),
                         (970.0, 985.0))
        # attempt 2 stopped after 125 mm: all of colour 1, half of colour 2
        self.assertEqual(spools.deduct(self.con, 7, 2, ["A", "B"], segs, used_mm=125),
                         [("A", 0, 30.0), ("B", 1, 7.5)])


class Segments(unittest.TestCase):
    def test_split_at_m600_matches_totals(self):
        f = Path(tempfile.mkdtemp()) / "g.gcode"
        f.write_text("M83\nG1 X1 Y1 E10\nG1 E-1\nG1 E1\n;COLOR_CHANGE\nM600\nG1 X2 E5.5\n"
                     "; filament_diameter = 1.75\n; filament_density = 1.23\n")
        segs = gcode.segments(f)
        self.assertEqual([s["mm"] for s in segs], [10.0, 5.5])
        self.assertAlmostEqual(segs[0]["grams"], 10.0 * 3.14159265 * 0.875 ** 2 * 1.23 / 1000, places=2)
        self.assertGreater(segs[1]["start_byte"], 0)


class Tracker(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        oid = jobs.create_order(self.con, "ui", "o")
        self.jid = jobs.create_job(self.con, oid, "likhan", "A01", state="planned")
        self.bid = jobs.create_build(self.con, oid, "Ender3MaxNeo", [self.jid], state="printing",
                                     remote_path="farm/x.gcode", started_at=1000.0,
                                     segments=[{"colour_idx": 0, "mm": 100.0, "grams": 30.0},
                                               {"colour_idx": 1, "mm": 50.0, "grams": 15.0}])
        self.con.execute("UPDATE jobs SET state='printing' WHERE id=?", (self.jid,))
        self.con.execute("INSERT INTO approvals (build_id, kind, actor, at, checklist) VALUES (?,?,?,?,?)",
                         (self.bid, "start", "ui:x", 1000.0, json.dumps({"spools": ["A", "B"]})))
        spools.add(self.con, {"id": "A", "weight_g": 1000})
        spools.add(self.con, {"id": "B", "weight_g": 1000})
        self.hist = []
        tracker._history = lambda printer, since: self.hist

    def test_still_printing_then_completed(self):
        self.hist = [{"filename": "farm/x.gcode", "start_time": 1001, "status": "in_progress"}]
        self.assertEqual(tracker.poll(self.con, log=lambda m: None), [])
        self.hist = [{"filename": "farm/old.gcode", "start_time": 1001, "status": "completed"},
                     {"filename": "farm/x.gcode", "start_time": 500, "status": "completed"},     # an older print
                     {"filename": "farm/x.gcode", "start_time": 1001, "status": "completed", "job_id": "0001"}]
        self.assertEqual(tracker.poll(self.con, log=lambda m: None), [(self.bid, "done")])
        self.assertEqual(jobs.get(self.con, "job", self.jid)["state"], "done")
        self.assertEqual(spools.get(self.con, "B")["remaining_g"], 985.0)
        self.assertEqual(tracker.poll(self.con, log=lambda m: None), [])                   # nothing printing now

    def test_cancelled_prorates_and_requeues_the_job(self):
        self.hist = [{"filename": "farm/x.gcode", "start_time": 1001, "status": "cancelled", "filament_used": 40}]
        self.assertEqual(tracker.poll(self.con, log=lambda m: None), [(self.bid, "cancelled")])
        self.assertEqual(jobs.get(self.con, "job", self.jid)["state"], "planned")
        self.assertEqual(spools.get(self.con, "A")["remaining_g"], 988.0)
        self.assertEqual(spools.get(self.con, "B")["remaining_g"], 1000.0)


if __name__ == "__main__":
    unittest.main()
