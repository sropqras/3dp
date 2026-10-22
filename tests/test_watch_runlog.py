import json
import os
import tempfile
import unittest
from pathlib import Path


class Milestones(unittest.TestCase):
    def test_offsets(self):
        from farm import watch
        f = Path(tempfile.mkdtemp()) / "m.gcode"
        f.write_bytes(b"START\n;LAYER_CHANGE\nA\n;LAYER_CHANGE\nB\n;COLOR_CHANGE after z=4\nM600\n;COLOR_CHANGE x\n")
        data = f.read_bytes()
        m = watch.milestones(f)
        self.assertEqual(m["size"], len(data))
        self.assertEqual(m["first_layer_done"], data.index(b";LAYER_CHANGE\nB"))
        self.assertEqual(len(m["colour_changes"]), 2)


class RunGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["FARM_RUNS_DIR"] = self.tmp
        import importlib
        from farm import runlog
        self.runlog = importlib.reload(runlog)

    def tearDown(self):
        os.environ.pop("FARM_RUNS_DIR")

    def manifest(self, run):
        return json.loads((run.dir / "manifest.json").read_text())

    def test_bare_systemexit_is_recorded(self):
        run = self.runlog.Run("job")
        with self.assertRaises(SystemExit):
            with run.guard():
                raise SystemExit("preset not found")
        self.assertEqual(self.manifest(run)["result"], "FAILED: preset not found")

    def test_exception_is_recorded_and_fail_not_overwritten(self):
        run = self.runlog.Run("job")
        with self.assertRaises(ValueError):
            with run.guard():
                raise ValueError("boom")
        self.assertEqual(self.manifest(run)["result"], "FAILED: boom")
        run2 = self.runlog.Run("job")
        with self.assertRaises(SystemExit):
            with run2.guard():
                run2.fail("specific reason")
        self.assertEqual(self.manifest(run2)["result"], "FAILED: specific reason")

    def test_same_second_runs_get_distinct_folders(self):
        dirs = {self.runlog.Run("same").dir for _ in range(3)}
        self.assertEqual(len(dirs), 3)

    def test_step_log_keeps_every_bed(self):
        run = self.runlog.Run("job")
        run.step("validate", bed=1)
        run.step("validate", bed=2)
        m = self.manifest(run)
        self.assertEqual(m["steps"]["validate"], {"bed": 2})
        self.assertEqual([s["bed"] for s in m["step_log"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
