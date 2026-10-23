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
        maintenance.log(self.con, "neo", "nozzle", "new 0.6 Sprite nozzle", "ui:Ironman", 1000.0)
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


class Debounce(unittest.TestCase):
    def test_a_crash_looping_service_is_told_once_and_recovers_once(self):
        from farm import monitor
        seen, prev, told = {}, {}, []
        bot = {"host:h": {"svc:moonraker-telegram-bot-X": ("silent", "h: service bot is activating/auto-restart")}}
        disk = {"host:h": {"disk": ("loud", "h: disk 95% full")}}
        t = 0
        for i in range(40):                        # 80 min: the bot is down on every other 2-min pass
            now = monitor.settle(seen, bot if i % 2 == 0 else {}, t)
            told += monitor.diff_problems(prev, now)
            prev, t = now, t + 120
        self.assertEqual(len(told), 1)             # one alert, after it had lasted ~25 min; no recoveries
        self.assertIn("auto-restart", told[0][1])
        for i in range(15):                        # running steadily for 30 min -> recovered, once
            now = monitor.settle(seen, {}, t)
            told += monitor.diff_problems(prev, now)
            prev, t = now, t + 120
        self.assertEqual([("recovered" in m) for _, m in told], [False, True])
        told.clear()                               # a loud problem is told on its second pass
        for i in range(3):
            now = monitor.settle(seen, disk, t)
            told += monitor.diff_problems(prev, now)
            prev, t = now, t + 120
        self.assertEqual(len(told), 1)
        self.assertEqual(told[0][0], "loud")
        now = monitor.settle(seen, {}, t)          # one blip of a short problem is never told at all
        self.assertEqual(monitor.settle(seen, {"host:h": {"cpu_temp": ("loud", "hot")}}, t + 120).get("host:h", {}).get("cpu_temp"), None)
