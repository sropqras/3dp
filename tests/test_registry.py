"""Phase 5: the registry is data (printers/registry.json) and a person onboards an instance from Farm Control."""
import copy
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from farm import cfgchange, db, fleet, hosts, printers, profiles
from tests.fakes import FakeMoonraker
from tests.test_audit_model3mf import GOOD

FARM_MD = "# Farm\n\n## Change log\n\n| Date | Printer | Change |\n|---|---|---|\n| 2026-10-22 | x | old |\n"
NEW = {"name": "newone", "bed_min": [0, 0], "bed_max": [300, 300], "max_z": 300, "profiles": ["neo-petg-036"]}


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        reg = self.tmp / "registry.json"
        shutil.copy(printers.REGISTRY, reg)
        (self.tmp / "FARM.md").write_text(FARM_MD)
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.config = {"printer.cfg": b"[mcu]\nserial: /dev/ttyUSB0\n\n[printer]\nkinematics: cartesian\n"}
        self.m.config_file = "/home/aali/printer_newone_data/config/printer.cfg"
        port = int(self.m.url.rsplit(":", 1)[1])
        patches = [mock.patch.object(printers, "REGISTRY", reg),
                   mock.patch.object(cfgchange, "FARM_MD", self.tmp / "FARM.md"),
                   mock.patch.object(db, "DB_PATH", self.tmp / "farm.db"),
                   mock.patch.object(hosts, "load_hosts",
                                     lambda *a: [{"name": "fakehost", "address": "127.0.0.1", "ports": [port]}])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: printers.refresh(force=True))   # back to the real registry for other tests
        printers.refresh(force=True)
        fleet._cache.clear()
        self.key = "fakehost:newone"

    def tearDown(self):
        self.m.close()

    def _onboard(self, **kw):
        return fleet.onboard(self.key, {**NEW, **kw}, "ui:Amir")

    def test_not_onboarded_until_saved_then_live_everywhere(self):
        v = fleet._instance(self.key)
        self.assertIsNone(v["registry"])
        self.assertNotIn("newone", profiles.PROFILES["neo-petg-036"].printers)
        e = self._onboard()
        self.assertEqual(e["moonraker"], self.m.url)
        self.assertIn("newone", printers.PRINTERS)                       # no restart needed
        self.assertIn("newone", profiles.PROFILES["neo-petg-036"].printers)
        self.assertEqual(printers.PRINTERS["newone"].bed_max, (300.0, 300.0))
        self.assertEqual(fleet._instance("newone")["registry"], "newone")   # the card is now the printer
        md = (self.tmp / "FARM.md").read_text()
        self.assertIn("| newone | Onboarded in Farm Control", md.splitlines()[6])   # newest row first
        self.assertTrue(profiles.PROFILES["neo-petg-036"].allows("newone"))

    def test_only_a_person_and_only_valid_facts(self):
        with self.assertRaises(cfgchange.Refused):
            fleet.onboard(self.key, NEW, "agent")
        for bad in ({"name": "1x"}, {"name": "Ender3MaxNeo"}, {"bed_max": [0, 300]}, {"max_z": 5},
                    {"profiles": ["no-such"]}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self._onboard(**bad)
        self.assertNotIn("newone", printers.PRINTERS)
        self.assertNotIn("newone", (self.tmp / "FARM.md").read_text())

    def test_profile_that_does_not_fit_the_bed_is_undone(self):
        with self.assertRaises(ValueError) as e:
            self._onboard(bed_max=[150, 150])                              # neo-petg-036's bed is larger
        self.assertIn("bed_shape", str(e.exception))
        self.assertNotIn("newone", printers.PRINTERS)
        self.assertNotIn("newone", [x["name"] for x in printers.registry_entries()])

    def test_onboarded_twice_refused(self):
        self._onboard()
        with self.assertRaises(ValueError):
            fleet.onboard("newone", {**NEW, "name": "again"}, "ui:Amir")

    def test_update_watch_only_and_profiles(self):
        self._onboard()
        r = fleet.update_registry("newone", {"readonly": True, "profiles": []}, "ui:Amir")
        self.assertEqual(len(r["changed"]), 2)
        self.assertTrue(printers.PRINTERS["newone"].readonly)
        self.assertNotIn("newone", profiles.PROFILES["neo-petg-036"].printers)
        self.assertIn("watch-only", (self.tmp / "FARM.md").read_text())
        self.assertEqual(fleet.update_registry("newone", {"readonly": True}, "ui:Amir")["changed"], [])
        with self.assertRaises(cfgchange.Refused):
            fleet.update_registry("newone", {"readonly": False}, "agent")
        with self.assertRaises(LookupError):
            fleet.update_registry("nobody", {}, "ui:Amir")

    def test_serial_is_a_staged_change(self):
        with self.assertRaises(ValueError):
            fleet.stage_serial(self.key, "/dev/ttyUSB1", "ui:Amir")
        port = "/dev/serial/by-path/pci-0000:00:14.0-usb-0:3.3:1.0-port0"
        r = fleet.stage_serial(self.key, port, "ui:Amir")
        self.assertIn("serial", r["summary"])
        self.assertEqual(self.m.config["printer.cfg"].count(b"ttyUSB0"), 1)   # staged, not applied
        con = db.connect()
        try:
            row = con.execute("SELECT state, new_content FROM config_changes WHERE id=?", (r["id"],)).fetchone()
        finally:
            con.close()
        self.assertEqual(row["state"], "staged")
        self.assertIn(f"serial: {port}", row["new_content"])

    def test_camera_registered_on_the_instance(self):
        with self.assertRaises(ValueError):
            fleet.register_camera(self.key, {"path": "../etc"}, "ui:Amir")
        with self.assertRaises(cfgchange.Refused):
            fleet.register_camera(self.key, {"path": "webcam3"}, "agent")
        r = fleet.register_camera(self.key, {"name": "newone", "path": "/webcam3/"}, "ui:Amir")
        self.assertEqual(r["stream"], "/webcam3/?action=stream")
        self.assertEqual(self.m.webcams[0]["snapshot_url"], "/webcam3/?action=snapshot")


if __name__ == "__main__":
    unittest.main()
