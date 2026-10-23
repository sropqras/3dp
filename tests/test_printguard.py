"""PrintGuard as the farm reads it: matching printers, alerts told once, never stopping the print-error watch."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from farm import config, monitor, printers, printguard, web

URL = "http://192.168.1.68:11111"
NEO = printers.PRINTERS["Ender3MaxNeo"]


def fake(alert=None, events=(), status="printing", camera_online=True):
    """PrintGuard's /api/v1 answers: the MaxNeo (matched by URL, named differently there) and a printer not in the farm."""
    data = {
        "/printers": [{"id": "p1", "name": "Neo in PrintGuard", "config": {"base_url": NEO.moonraker + "/"},
                       "device_state": {"status": status}},
                      {"id": "p2", "name": "Ender3Pro", "config": {"base_url": "http://192.168.1.184:7126"},
                       "device_state": {"status": "idle"}}],
        "/monitors": [{"id": "m1", "printer_id": "p1", "camera_id": "c1", "enabled": True, "watching": True,
                       "on_defect": "none", "threshold": 0.75, "result": {"score": 0.12, "ts": 100.0}, "alert": alert},
                      {"id": "m2", "printer_id": "p2", "camera_id": "c2", "enabled": True, "watching": False,
                       "result": None, "alert": None}],
        "/cameras": [{"id": "c1", "online": camera_online, "reason": None if camera_online else "no frames"},
                     {"id": "c2", "online": True}],
        "/events": list(events),
    }
    return lambda path, timeout=5: data[path]


class PrintGuardTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(config, "PRINTGUARD_URL", URL)
        p.start()
        self.addCleanup(p.stop)

    def test_status_matches_farm_printers_by_moonraker_url(self):
        with mock.patch.object(printguard, "_get", fake()):
            st = printguard.status()
        self.assertEqual(set(st["printers"]), {"Ender3MaxNeo", "Ender3Pro"})   # unknown to the farm: its own name
        neo = st["printers"]["Ender3MaxNeo"]
        self.assertEqual((neo["score"], neo["watching"], neo["status"], neo["on_defect"]), (0.12, True, "printing", "none"))

    def test_not_configured_reads_nothing(self):
        with mock.patch.object(config, "PRINTGUARD_URL", ""), mock.patch.object(printguard, "_get", side_effect=AssertionError):
            self.assertEqual(printguard.status(), {})

    def test_errors_are_reported_not_raised_and_401_names_the_token(self):
        with mock.patch.object(printguard, "_get", side_effect=RuntimeError("HTTP 401 from x: missing or invalid token")):
            st = printguard.status()
        self.assertIn("FARM_PRINTGUARD_TOKEN", st["error"])
        self.assertEqual(st["ui"], URL)

    def test_alert_told_once_including_one_already_cleared(self):
        ev = [{"event": "alert", "monitor_id": "m1", "score": 0.91, "action": "none", "ts": 200.0}]
        with mock.patch.object(printguard, "_get", fake(events=ev)):     # cleared on the monitor, still in the events
            st = printguard.status()
        new, told = printguard.alerts_since(st, {})
        self.assertEqual([(a["printer"], a["score"]) for a in new], [("Ender3MaxNeo", 0.91)])
        self.assertEqual(printguard.alerts_since(st, told)[0], [])                         # same alert: not again
        self.assertEqual(printguard.alerts_since(st, {"m1": 300.0})[0], [])                # older than told: ignored

    def test_monitor_pass_alerts_loud_once_and_skips_first_pass_history(self):
        state = {}
        old = {"score": 0.8, "action": "none", "ts": 50.0}
        with mock.patch.object(printguard, "_get", fake(alert=old)):
            self.assertEqual(monitor.printguard_pass(state), [])                           # already showing: not news
        new = {"score": 0.93, "action": "none", "ts": 400.0}
        with mock.patch.object(printguard, "_get", fake(alert=new)):
            out = monitor.printguard_pass(state)
            self.assertEqual(len(out), 1)
            level, text = out[0]
            self.assertEqual(level, "loud")
            self.assertIn("Ender3MaxNeo", text)
            self.assertIn("0.93", text)
            self.assertIn("Not paused", text)
            self.assertEqual(monitor.printguard_pass(state), [])                           # told once

    def test_unreachable_and_blind_camera_are_quiet_and_debounced(self):
        state = {}
        with mock.patch.object(printguard, "_get", side_effect=RuntimeError("timed out")), \
                mock.patch.object(monitor.time, "time", return_value=1000.0):
            self.assertEqual(monitor.printguard_pass(state), [])                           # not yet confirmed
        with mock.patch.object(printguard, "_get", side_effect=RuntimeError("timed out")), \
                mock.patch.object(monitor.time, "time", return_value=1000.0 + 1600):
            out = monitor.printguard_pass(state)
        self.assertEqual([lv for lv, _ in out], ["silent"])
        self.assertIn("PrintGuard unreachable", out[0][1])
        state = {}
        for t in (0.0, 1600.0):
            with mock.patch.object(printguard, "_get", fake(camera_online=False)), \
                    mock.patch.object(monitor.time, "time", return_value=t):
                out = monitor.printguard_pass(state)
        self.assertEqual(len(out), 1)
        self.assertIn("no frames", out[0][1])
        self.assertEqual(out[0][0], "silent")

    def test_idle_printer_without_frames_is_not_a_problem(self):
        state = {}
        for t in (0.0, 1600.0):
            with mock.patch.object(printguard, "_get", fake(status="idle", camera_online=False)), \
                    mock.patch.object(monitor.time, "time", return_value=t):
                self.assertEqual(monitor.printguard_pass(state), [])

    def test_overview_cards_carry_printguard_only_when_configured(self):
        with mock.patch.object(printguard, "_get", fake()):
            st = printguard.status()
        live = [{"name": "Ender3MaxNeo"}, {"name": "s1pro"}]
        web._add_printguard(live, st)
        self.assertEqual((live[0]["printguard"]["score"], live[0]["printguard"]["ui"]), (0.12, URL))
        self.assertTrue(live[1]["printguard"]["missing"])                                  # no monitor for it
        live = [{"name": "Ender3MaxNeo"}]
        web._add_printguard(live, {"error": "timed out", "ui": URL})
        self.assertEqual(live[0]["printguard"], {"error": "timed out", "ui": URL})
        live = [{"name": "Ender3MaxNeo"}]
        web._add_printguard(live, {})                                                      # not configured
        self.assertNotIn("printguard", live[0])

    def test_run_survives_a_broken_printguard_pass(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(monitor, "check", lambda p, prev: ({}, [])), \
                mock.patch.object(monitor, "printguard_pass", side_effect=ValueError("boom")):
            out = monitor.run(Path(d) / "state.json", health=False)
            self.assertEqual(out, [("silent", "farm monitor: PrintGuard pass failed: ValueError: boom")])
            json.loads((Path(d) / "state.json").read_text())


if __name__ == "__main__":
    unittest.main()
