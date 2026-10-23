"""Farm Projects: groups -> parts x qty, the cell grid, generate/send through the real pipeline (fake maker and slicer,
nothing reaches a printer), items, removal, the Qabrastaan batch import."""
import dataclasses
import json
import os
import tempfile
import unittest
from pathlib import Path

from farm import db, inventory, jobs, makers, moonraker, pipeline, projects, runlog, worker
from tests.fakes import FakeMaker
from tests.helpers import box_stl
from tests.test_pipeline_worker import FAKE_SLICER

TEMPLATES = [{"id": "plate", "maker": "likhan", "params": {"mode": "text", "base_thickness_mm": 4, "font_size_mm": 90},
              "profile": "neo-petg-036", "layer_height": 0.4, "printer": "Ender3MaxNeo", "same_bed": True,
              "colour_changes": ["base_top"], "colours": ["black", "white"], "colour_hex": ["#111", "#eee"]},
             {"id": "qabrastaan-plate-neo", "maker": "likhan", "params": {}}, {"id": "qabrastaan-children", "maker": "likhan"}]


class Projects(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (runlog.RUNS_DIR, os.environ.get("FARM_PRUSASLICER"))
        runlog.RUNS_DIR = self.tmp / "runs"
        os.environ["FARM_PRUSASLICER"] = FAKE_SLICER
        os.environ["FARM_ALLOW_ANY_SLICER"] = "1"
        self.con = db.connect(self.tmp / "farm.db")
        self.fake = FakeMaker(self.tmp)
        self.maker = makers.get("likhan")
        self.maker.spec = dataclasses.replace(type(self.maker).spec, url=self.fake.url)
        self.uploads = []
        self._upload = moonraker.upload
        moonraker.upload = lambda *a: self.uploads.append(a) or "x"
        self.pid = projects.create(self.con, "Order 42", "ui:Amir", hours_per_unit=2)
        self.gid = projects.add_group(self.con, self.pid, "Row A")

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

    def cells(self):
        g = projects.grid(self.con, self.pid)
        return {p["label"]: [c["status"] for c in p["cells"]] for gr in g["groups"] for p in gr["parts"]}

    def test_paste_ranges_and_qty(self):
        self.assertEqual(projects.parse_labels("A01-A03\nBox insert, 4\n\nCH9"),
                         [("A01", 1), ("A02", 1), ("A03", 1), ("Box insert", 4), ("CH9", 1)])
        self.assertEqual(len(projects.parse_labels("A08-A12")), 5)

    def test_generate_to_review_and_the_grid_follows(self):
        ids = projects.add_parts(self.con, self.gid, [{"label": "T01", "template": "plate"},
                                                      {"label": "T02", "template": "plate", "qty": 2},
                                                      {"label": "Box", "source": "item", "qty": 3}])
        self.assertEqual(self.cells(), {"T01": ["todo"], "T02": ["todo", "todo"], "Box": ["item-todo"] * 3})
        out = projects.generate(self.con, self.pid, ids, "ui:Amir", TEMPLATES, str)
        self.assertEqual(len(out), 1)                                   # one order: same template/printer/profile
        self.assertEqual(len(out[0]["jobs"]), 3)                        # one job per unit, items not made
        texts = sorted(json.loads(r[0])["text"] for r in self.con.execute("SELECT params FROM jobs"))
        self.assertEqual(texts, ["T01", "T02", "T02"])                  # the label personalises the template
        self.assertEqual(self.con.execute("SELECT project_id FROM orders").fetchone()[0], self.pid)
        self.assertEqual(self.cells()["T02"], ["generating", "generating"])
        with self.assertRaises(ValueError):                             # nothing left to make
            projects.generate(self.con, self.pid, ids, "ui:Amir", TEMPLATES, str)
        for jid in out[0]["jobs"]:
            worker.enqueue(self.con, "generate", jid)
        self.drain()
        self.assertEqual(self.cells()["T01"], ["sliced"])               # waiting in Review & queue
        self.assertEqual(self.uploads, [])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM artefacts").fetchone()[0], 3)   # in the Inventory
        # a print of T01's artefact finishing makes its cell done
        aid = self.con.execute("SELECT a.id FROM artefacts a JOIN jobs j ON j.id=a.job_id WHERE j.label='T01'").fetchone()[0]
        inventory.record(self.con, aid, "Ender3MaxNeo", {"filename": "x.gcode", "status": "completed", "start_time": 1}, "tracker")
        self.assertEqual(self.cells()["T01"], ["done"])
        g = projects.grid(self.con, self.pid)
        self.assertEqual((g["done"], g["total"], g["hours_left"]), (1, 6, 4.0))

    def test_qty_on_separate_beds_gets_one_build_each(self):
        t = [dict(TEMPLATES[0], id="solo", same_bed=False)]
        ids = projects.add_parts(self.con, self.gid, [{"label": "T02", "template": "solo", "qty": 2}])
        for jid in projects.generate(self.con, self.pid, ids, "ui:Amir", t, str)[0]["jobs"]:
            worker.enqueue(self.con, "generate", jid)
        self.drain()
        bs = jobs.builds(self.con)
        self.assertEqual([b["state"] for b in bs], ["review", "review"])
        self.assertEqual(len({b["gcode"] for b in bs}), 2)                     # two files, neither overwritten
        self.assertEqual(self.cells()["T02"], ["sliced", "sliced"])

    def test_send_generated_parts_and_artefact_reprints(self):
        ids = projects.add_parts(self.con, self.gid, [{"label": "T01", "template": "plate"}])
        for jid in projects.generate(self.con, self.pid, ids, "ui:Amir", TEMPLATES, str)[0]["jobs"]:
            worker.enqueue(self.con, "generate", jid)
        self.drain()
        stl = box_stl(self.tmp / "own.stl", 0, 0, 0, 20, 20, 10)
        art = self.con.execute("INSERT INTO artefacts (kind, title, design, maker, path, meta, created_at) "
                               "VALUES ('model','own','own','nas',?,'{}',0)", (str(stl),)).lastrowid
        rp = projects.add_parts(self.con, self.gid, [{"label": "Own", "source": "artefact", "artefact_id": art, "qty": 2}])

        def send_artefact(con, a, body):
            oid, _ = pipeline.submit_order(con, source="inventory", title="own", maker="upload",
                                           items=[{"label": "own", "params": {}, "inputs": {"file": str(stl)}}],
                                           profile=body["profile"], layer_height=body["layer_height"], printer=body["printer"])
            return {"order": oid}
        out = projects.send(self.con, self.pid, ids + rp, "enders1plus", "neo-petg-036", 0.4, send_artefact)
        self.assertEqual(len(out["builds"]), 1)
        self.assertEqual(len(out["artefacts"]), 1)
        self.assertEqual(jobs.get(self.con, "build", out["builds"][0])["printer"], "enders1plus")

    def test_items_and_removal_are_a_persons(self):
        ids = projects.add_parts(self.con, self.gid, [{"label": "Box", "source": "item", "qty": 2},
                                                      {"label": "T09", "template": "plate"}])
        with self.assertRaises(projects.Refused):
            projects.items(self.con, self.pid, ids[:1], 1, "agent")
        projects.items(self.con, self.pid, ids[:1], 1, "ui:Amir")
        projects.items(self.con, self.pid, ids[:1], 5, "ui:Amir")      # capped at qty
        self.assertEqual(self.cells()["Box"], ["item-done", "item-done"])
        with self.assertRaises(projects.Refused):
            projects.remove(self.con, self.pid, ids[1:], "agent")
        projects.remove(self.con, self.pid, ids[1:], "ui:Amir")
        self.assertNotIn("T09", self.cells())
        other = projects.create(self.con, "Other", "ui:Amir")
        with self.assertRaises(LookupError):                            # parts belong to their own project
            projects.remove(self.con, other, ids[:1], "ui:Amir")

    def test_bad_parts_refused(self):
        with self.assertRaises(ValueError):
            projects.add_parts(self.con, self.gid, [{"label": "X", "source": "artefact", "artefact_id": 999}])
        with self.assertRaises(ValueError):
            projects.add_parts(self.con, self.gid, [{"label": "X", "source": "maker"}])
        ids = projects.add_parts(self.con, self.gid, [{"label": "X", "template": "gone"}])
        with self.assertRaises(ValueError):
            projects.generate(self.con, self.pid, ids, "ui:Amir", TEMPLATES, str)

    def test_batch_import_counts_legacy_prints_by_label(self):
        batch = self.tmp / "jobs" / "q" / "batch.json"
        batch.parent.mkdir(parents=True)
        batch.write_text(json.dumps({"name": "Plates", "plates_per_bed": 2, "hours_per_bed": 4, "note": "n",
                                     "rows": [{"prefix": "A", "count": 3}, {"prefix": "CH", "count": 2, "name": "Children",
                                                                           "font_size_mm": 66}]}))
        pid = projects.import_batch(self.con, batch, "import", TEMPLATES)
        self.assertEqual(projects.import_batch(self.con, batch, "import", TEMPLATES), pid)     # once
        aid = self.con.execute("INSERT INTO artefacts (kind, title, design, maker, printer, remote_path, meta, created_at) "
                               "VALUES ('gcode','CH02+A01','q','farm','Ender3MaxNeo','farm/q/CH02+A01.gcode',?,0)",
                               (json.dumps({"labels": ["CH02", "A01"]}),)).lastrowid
        inventory.record(self.con, aid, "Ender3MaxNeo", {"filename": "f", "status": "completed", "start_time": 5}, "history")
        g = projects.grid(self.con, pid)
        self.assertEqual([x["name"] for x in g["groups"]], ["A", "Children"])
        cells = {p["label"]: p["cells"][0]["status"] for gr in g["groups"] for p in gr["parts"]}
        self.assertEqual(cells, {"A01": "done", "A02": "todo", "A03": "todo", "CH01": "todo", "CH02": "done"})
        self.assertEqual({p["template"] for gr in g["groups"] for p in gr["parts"]},
                         {"qabrastaan-plate-neo", "qabrastaan-children"})
        self.assertEqual((g["done"], g["total"], g["hours_left"]), (2, 5, 6.0))


if __name__ == "__main__":
    unittest.main()
