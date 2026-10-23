import copy
import dataclasses
import tempfile
import threading
import unittest
from pathlib import Path

from farm import cfgchange, configs, db, jobs, printers
from tests.fakes import FakeMoonraker
from tests.test_audit_model3mf import GOOD

PRINTER_CFG = b"[printer]\nkinematics: cartesian\nmax_velocity: 200\nmax_accel: 3000\n\n[stepper_x]\nstep_pin: PC2\n" \
              b"\n#*# <---------------------- SAVE_CONFIG ---------------------->\n#*# [bltouch]\n#*# z_offset = 3.3\n"
FARM_MD = "# Farm\n\n## Change log\n\n| Date | Printer | Change |\n|---|---|---|\n| old | x | y |\n"


class KeyEdit(unittest.TestCase):
    def test_replace_insert_and_save_config_untouched(self):
        t = PRINTER_CFG.decode()
        out = cfgchange.key_edit(t, "printer", "max_velocity", "180")
        self.assertIn("max_velocity: 180\n", out)
        self.assertNotIn("max_velocity: 200", out)
        out = cfgchange.key_edit(t, "stepper_x", "microsteps", "16")
        self.assertIn("[stepper_x]\nmicrosteps: 16\nstep_pin: PC2", out)
        self.assertTrue(out.endswith("#*# z_offset = 3.3\n"))
        with self.assertRaises(cfgchange.Refused):
            cfgchange.key_edit(t, "bltouch", "z_offset", "1")      # only in SAVE_CONFIG: refused, not edited


class Changes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (configs.STORE, cfgchange.FARM_MD, dict(printers.PRINTERS),
                      (cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S))
        configs.STORE = self.tmp / "snap"
        cfgchange.FARM_MD = self.tmp / "FARM.md"
        cfgchange.FARM_MD.write_text(FARM_MD)
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S = 0, 0.05, 0.2, 2
        self.m = FakeMoonraker(copy.deepcopy(GOOD))
        self.m.config = {"printer.cfg": PRINTER_CFG, "telegram.conf": b"[bot]\nbot_token: SECRET\n"}
        printers.PRINTERS["Ender3MaxNeo"] = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], moonraker=self.m.url)
        self.con = db.connect(self.tmp / "farm.db")

    def tearDown(self):
        configs.STORE, cfgchange.FARM_MD, saved, timing = self.saved
        printers.PRINTERS.clear()
        printers.PRINTERS.update(saved)
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S, cfgchange.READY_TIMEOUT_S = timing
        self.m.close()

    def stage(self, **kw):
        args = dict(section="printer", option="max_velocity", value="180", reason="slower travel", author="ui:Amir")
        args.update(kw)
        return cfgchange.stage(self.con, "Ender3MaxNeo", self.m.url, "printer.cfg", **args)

    def test_stage_refusals(self):
        with self.assertRaises(cfgchange.Refused):
            cfgchange.stage(self.con, "Ender3MaxNeo", self.m.url, "telegram.conf", new_content="x", reason="r", author="ui:a")
        with self.assertRaises(cfgchange.Refused):
            self.stage(reason="  ")
        with self.assertRaises(cfgchange.Refused):
            self.stage(value="200")                                   # already live
        with self.assertRaises(cfgchange.Refused):
            cfgchange.stage(self.con, "EnderV3SE", self.m.url, "printer.cfg", section="printer", option="max_velocity",
                            value="1", reason="r", author="ui:a")      # watch-only

    def test_happy_apply_records_everything(self):
        c = self.stage()
        self.assertIn("+max_velocity: 180", cfgchange.preview(self.con, c["id"]))
        out = cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.assertEqual(out["state"], "applied", out["error"])
        self.assertIn(b"max_velocity: 180", self.m.config["printer.cfg"])
        self.assertEqual(self.m.restarts, 1)
        self.assertTrue(out["before_snapshot"])
        self.assertIn("change 1 |", cfgchange.FARM_MD.read_text())
        trail = [e["to_state"] for e in jobs.history(self.con, "config_change", c["id"])]
        self.assertEqual(trail, ["staged", "applying", "verifying", "applied"])

    def test_apply_refusals(self):
        c = self.stage()
        for actor in ("agent", "worker", "cfggate"):
            with self.assertRaises(cfgchange.Refused, msg=actor):
                cfgchange.apply(self.con, c["id"], actor)
        self.m.state = "printing"
        with self.assertRaises(cfgchange.Refused):
            cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.m.state, self.m.paused = "standby", True
        with self.assertRaises(cfgchange.Refused):
            cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.m.paused = False
        self.m.config["printer.cfg"] += b"\n# edited in Fluidd meanwhile\n"
        with self.assertRaises(cfgchange.Refused) as e:
            cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.assertIn("changed on the printer", str(e.exception))
        self.assertEqual(self.m.restarts, 0)
        self.assertEqual(cfgchange.get(self.con, c["id"])["state"], "staged")

    def test_new_audit_fail_or_console_error_fails_verification(self):
        c = self.stage(value="190")
        self.m.on_restart = lambda m: m.settings["printer"].update(max_velocity=300)   # the change broke a rule
        out = cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.assertEqual(out["state"], "failed")
        self.assertIn("max_velocity", out["error"])
        self.m.on_restart = lambda m: m.console.append({"message": '// Unknown command:"_X1"', "time": 9e9, "type": "response"})
        self.m.settings["printer"]["max_velocity"] = 200
        c2 = self.stage(value="170")
        self.assertEqual(cfgchange.apply(self.con, c2["id"], "ui:Amir")["state"], "failed")

    def test_klipper_not_coming_back_rolls_back_automatically(self):
        c = self.stage()
        self.m.after_restart = "error"
        out = cfgchange.apply(self.con, c["id"], "ui:Amir")
        self.assertEqual(out["state"], "rolled_back")
        self.assertEqual(self.m.config["printer.cfg"], PRINTER_CFG)
        self.assertEqual(self.m.restarts, 2)

    def test_manual_rollback(self):
        c = self.stage()
        cfgchange.apply(self.con, c["id"], "ui:Amir")
        out = cfgchange.rollback(self.con, c["id"], "ui:Amir")
        self.assertEqual(out["state"], "rolled_back")
        self.assertEqual(self.m.config["printer.cfg"], PRINTER_CFG)
        with self.assertRaises(cfgchange.Refused):
            cfgchange.rollback(self.con, c["id"], "ui:Amir")

    def test_two_applies_race_one_wins(self):
        c = self.stage()
        path = Path(self.con.execute("PRAGMA database_list").fetchone()[2])
        results = []

        def go():
            con = db.connect(path)
            try:
                results.append(cfgchange.apply(con, c["id"], "ui:Amir")["state"])
            except (cfgchange.Refused, jobs.StateConflict) as e:
                results.append(type(e).__name__)

        ts = [threading.Thread(target=go) for _ in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(results.count("applied"), 1, results)
        self.assertEqual(self.m.restarts, 1)


if __name__ == "__main__":
    unittest.main()
