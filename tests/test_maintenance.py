import tempfile
import unittest
from pathlib import Path

from farm import db, maintenance


class Maintenance(unittest.TestCase):
    def setUp(self):
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")

    def st(self, hours):
        return {s["kind"]: s for s in maintenance.status(self.con, "neo", hours)}

    def test_due_from_print_hours_since_last_entry(self):
        self.assertTrue(self.st(1000)["nozzle"]["no_record"])
        self.assertFalse(self.st(1000)["nozzle"]["due"])          # never logged: no guess
        maintenance.log(self.con, "neo", "nozzle", "new 0.6 Sprite nozzle", "ui:Amir", 1000.0)
        self.assertEqual(self.st(1100)["nozzle"]["since_hours"], 100.0)
        self.assertFalse(self.st(1269)["nozzle"]["due"])
        self.assertTrue(self.st(1271)["nozzle"]["soon"])
        self.assertTrue(self.st(1300)["nozzle"]["due"])

    def test_intervals_default_global_and_per_printer(self):
        maintenance.log(self.con, "neo", "belts", "", "ui:a", 0.0)
        self.assertFalse(self.st(450)["belts"]["due"])            # default 500
        maintenance.set_interval(self.con, "belts", 400)          # farm-wide
        self.assertTrue(self.st(450)["belts"]["due"])
        maintenance.set_interval(self.con, "belts", 600, printer="neo")
        self.assertFalse(self.st(450)["belts"]["due"])
        with self.assertRaises(ValueError):
            maintenance.log(self.con, "neo", "teleport", "", "ui:a", 0.0)


if __name__ == "__main__":
    unittest.main()


class HealthAlerts(unittest.TestCase):
    def test_new_problems_alert_once_and_recover(self):
        from farm import monitor
        fleet = [{"reachable": True, "host": "h", "registry": "Ender3MaxNeo",
                  "services": {"moonraker-telegram-bot-X": "activating/auto-restart", "klipper-Y": "failed/failed",
                               "klipper-backup-on-boot": "inactive/dead", "crowsnest": "active/running"},
                  "disk_used_pct": 95, "disk_free_gb": 3, "cpu_temp": 50, "throttled": [], "failed_components": ["timelapse"],
                  "warnings": [], "cameras": [{"name": "cam", "ok": False}]}]
        now = monitor.health_problems(fleet, {"Ender3MaxNeo": ["nozzle"]})
        levels = {k: lv for scope in now.values() for k, (lv, _) in scope.items()}
        self.assertEqual(levels, {"svc:moonraker-telegram-bot-X": "silent", "svc:klipper-Y": "loud", "disk": "loud",
                                  "failed:timelapse": "loud", "camera:cam": "silent", "maint:nozzle": "silent"})
        first = monitor.diff_problems({}, now)
        self.assertEqual(len(first), 6)
        self.assertEqual(monitor.diff_problems(now, now), [])                  # no repeats
        fleet[0]["disk_used_pct"] = 40
        later = monitor.health_problems(fleet, {"Ender3MaxNeo": ["nozzle"]})
        rec = monitor.diff_problems(now, later)
        self.assertEqual([lv for lv, _ in rec], ["silent"])
        self.assertIn("recovered", rec[0][1])
