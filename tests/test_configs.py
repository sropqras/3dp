import copy
import tempfile
import unittest
from pathlib import Path

from farm import configs, db
from tests.fakes import FakeMoonraker
from tests.test_audit_model3mf import GOOD


class Configs(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = configs.STORE
        configs.STORE = self.tmp / "snapshots"
        self.con = db.connect(self.tmp / "farm.db")
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.config = {"printer.cfg": b"[printer]\nmax_velocity: 200\n", "macros.cfg": b"[gcode_macro M600]\n",
                         "telegram.conf": b"[bot]\nbot_token: SECRET123\n", "KAMP/Line_Purge.cfg": b"upstream",
                         "printer-20250101_000000.cfg": b"klipper autobackup", "moonraker.conf": b"[server]\n"}

    def tearDown(self):
        configs.STORE = self.saved
        self.m.close()

    def test_secrets_upstream_and_noise_are_never_read(self):
        self.assertEqual(sorted(f["path"] for f in configs.list_files(self.m.url)),
                         ["macros.cfg", "moonraker.conf", "printer.cfg"])
        with self.assertRaises(PermissionError):
            configs.read_file(self.m.url, "telegram.conf")
        configs.snapshot(self.con, "neo", self.m.url, "test")
        stored = b"".join(p.read_bytes() for p in (self.tmp / "snapshots").rglob("*") if p.is_file())
        self.assertNotIn(b"SECRET123", stored)

    def test_snapshot_dedupes_and_drift_needs_a_reviewed_reference(self):
        s1 = configs.snapshot(self.con, "neo", self.m.url, "test")
        self.assertTrue(configs.snapshot(self.con, "neo", self.m.url, "test").get("unchanged"))
        self.assertIsNone(configs.drift(self.con, "neo", s1["files"])["reference"])
        configs.review(self.con, s1["id"], "ui:Ironman")
        self.assertEqual(configs.drift(self.con, "neo", s1["files"])["changed"], [])
        self.m.config["printer.cfg"] = b"[printer]\nmax_velocity: 300\n"
        self.m.config["new.cfg"] = b"x"
        s2 = configs.snapshot(self.con, "neo", self.m.url, "test")
        d = configs.drift(self.con, "neo", s2["files"])
        self.assertEqual((d["changed"], d["added"], d["loud"]), (["printer.cfg"], ["new.cfg"], True))
        diff = configs.diff("neo", "printer.cfg", s1["files"]["printer.cfg"], self.m.config["printer.cfg"])
        self.assertIn("-max_velocity: 200", diff)
        self.assertIn("+max_velocity: 300", diff)

    def test_to_repo_writes_only_changes(self):
        configs.snapshot(self.con, "neo", self.m.url, "test")
        dest = self.tmp / "repo" / "neo"
        self.assertEqual(sorted(configs.to_repo(self.con, "neo", dest)), ["macros.cfg", "moonraker.conf", "printer.cfg"])
        self.assertEqual(configs.to_repo(self.con, "neo", dest), [])
        self.assertFalse((dest / "telegram.conf").exists())


class InstanceName(unittest.TestCase):
    def test_names(self):
        from farm.hosts import instance_name
        self.assertEqual(instance_name("/home/aali/Ender3MaxNeo_data/config/printer.cfg", "h"), "Ender3MaxNeo")
        self.assertEqual(instance_name("/home/aali/printer_s1pro_data/config/printer.cfg", "h"), "s1pro")
        self.assertEqual(instance_name("/home/aali/printer_data/config/printer.cfg", "3dpnas"), "3dpnas")


if __name__ == "__main__":
    unittest.main()
