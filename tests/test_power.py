"""Printer power at the wall (farm/power.py) against a fake Meross strip and a fake Moonraker: a person switches a
mapped outlet; Off is refused unless the printer is provably idle and cool; nothing else is ever switched."""
import copy
import dataclasses
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from farm import db, monitor, power, printers
from tests.fakes import FakeMeross, FakeMoonraker
from tests.test_audit_model3mf import GOOD

KEY = "test-device-key-0123"
NAME = "Ender3MaxNeo"


class PowerTest(unittest.TestCase):
    def setUp(self):
        self.strip = FakeMeross(KEY)
        self.moon = FakeMoonraker(copy.deepcopy(GOOD))
        env = mock.patch.dict(os.environ, {"FARM_MEROSS_KEY": KEY, "FARM_POWER_TEST": "1", "FARM_MEROSS_DEVICES": "",
                                            "FARM_POWER_PORTS": json.dumps({"127.0.0.1": self.strip.port})})
        env.start()
        self.addCleanup(env.stop)
        tmp = Path(tempfile.mkdtemp())
        self._real_busy = power.farm_busy
        # the live registry maps the real printers to the real strip: tests see none of those mappings
        unmapped = {n: dataclasses.replace(pr, power=None) for n, pr in printers.PRINTERS.items()}
        for patch in (mock.patch.dict(printers.PRINTERS, unmapped),mock.patch.object(power, "AUX_FILE", tmp / "power.json"),     # never the real strip's map
                      mock.patch.object(db, "DB_PATH", tmp / "farm.db"),            # never the real farm DB
                      mock.patch.object(power, "farm_busy", return_value=[]),       # never the real printers
                      mock.patch.object(power, "MIN_DWELL_S", 0), mock.patch.object(power, "STRIP_GAP_S", 0)):
            patch.start()
            self.addCleanup(patch.stop)
        self.mapping = {"type": "meross", "ip": "127.0.0.1", "uuid": self.strip.uuid, "channel": 2, "model": "mss426"}
        self.set_printer(power=self.mapping)
        power._cache.clear()
        self.con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        self.addCleanup(self.strip.close)

    def set_printer(self, **kw):
        old = printers.PRINTERS[NAME]
        printers.PRINTERS[NAME] = dataclasses.replace(old, **{"moonraker": self.moon.url, **kw})
        self.addCleanup(printers.PRINTERS.__setitem__, NAME, old)

    def switch(self, on, actor="ui:Ironman"):
        return power.set_power(self.con, NAME, on, actor, boot_s=0, wait_s=3)

    def refused(self, on, text, actor="ui:Ironman"):
        with self.assertRaises(power.Refused) as cm:
            self.switch(on, actor)
        self.assertIn(text, str(cm.exception))
        self.assertEqual(self.strip.onoff[2], 1, "a refusal must not touch the outlet")
        return cm.exception

    # -------------------------------------------------------------- the strip
    def test_sign_is_md5_of_id_key_and_time(self):
        self.assertEqual(power.sign("abc", "k", 123), "4c1c07cb496d351efeb2586d414f0826")

    def test_device_reads_type_uuid_and_outlets(self):
        d = power.device("127.0.0.1")
        self.assertEqual((d["type"], d["uuid"], [c["channel"] for c in d["channels"]]),
                         ("mss426", self.strip.uuid, [0, 1, 2, 3, 4, 5]))

    def test_wrong_key_says_so_and_never_shows_the_key(self):
        with mock.patch.dict(os.environ, {"FARM_MEROSS_KEY": "nope-wrong"}):
            with self.assertRaises(RuntimeError) as cm:
                power.device("127.0.0.1", fresh=True)
        self.assertIn("refused the signature", str(cm.exception))
        self.assertNotIn("nope-wrong", str(cm.exception))

    def test_no_key_and_off_lan_addresses_are_refused(self):
        with mock.patch.dict(os.environ, {"FARM_MEROSS_KEY": ""}):
            with self.assertRaises(RuntimeError) as cm:
                power.device("127.0.0.1", fresh=True)
            self.assertIn("get_key.sh", str(cm.exception))
        for ip in ("8.8.8.8", "meross.example.com"):
            with self.assertRaises(ValueError):
                power.lan_ip(ip)
        with mock.patch.dict(os.environ, {"FARM_POWER_TEST": ""}):
            with self.assertRaises(ValueError):
                power.lan_ip("127.0.0.1")       # loopback only for the tests' fake

    # -------------------------------------------------------------- switching off
    def test_off_when_idle_and_cool_switches_only_its_outlet_and_logs_it(self):
        out = self.switch(False)
        self.assertFalse(out["on"])
        self.assertEqual(self.strip.onoff, {0: 1, 1: 1, 2: 0, 3: 1, 4: 1, 5: 1})
        ev = self.con.execute("SELECT to_state, actor, detail FROM events WHERE entity='printer_op'").fetchall()
        self.assertEqual([(e["to_state"], e["actor"]) for e in ev], [("power_off", "ui:Ironman")])
        self.assertNotIn(KEY, ev[0]["detail"])

    def test_off_is_refused_while_printing_paused_heating_or_hot(self):
        self.moon.state = "printing"
        self.refused(False, "ruin the print")
        self.moon.state, self.moon.paused = "standby", True
        self.refused(False, "ruin the print")
        self.moon.paused = False
        self.moon.heater_bed = {"temperature": 30.0, "target": 60.0}
        self.refused(False, "heater bed is set to 60")
        self.moon.heater_bed = {"temperature": 30.0, "target": 0.0}
        self.moon.extruder = {"temperature": 60.0, "target": 0.0}
        self.refused(False, "hotend 60 C")

    def test_off_is_refused_when_moonraker_cannot_be_read(self):
        self.set_printer(power=self.mapping, moonraker="http://127.0.0.1:9")    # nothing listens
        self.refused(False, "can't read")

    def test_off_is_refused_when_klipper_is_in_another_error(self):
        self.moon.klipper, self.moon.klipper_msg = "error", "Option 'x' is not valid in section 'printer'"
        self.refused(False, "Klipper is error")

    def test_off_is_allowed_when_the_board_is_already_gone(self):
        self.moon.klipper, self.moon.klipper_msg = "error", "mcu 'mcu': Unable to connect"
        self.assertEqual(self.switch(False)["checked"], "board already off")
        self.assertEqual(self.strip.onoff[2], 0)

    # -------------------------------------------------------------- switching on
    def test_on_switches_the_outlet_and_reconnects_klipper(self):
        self.strip.onoff[2] = 0
        self.moon.klipper, self.moon.klipper_msg = "error", "mcu 'mcu': Unable to connect"
        out = self.switch(True)
        self.assertEqual((out["on"], out["state"], self.moon.restarts, self.strip.onoff[2]), (True, "ready", 1, 1))

    def test_on_does_not_restart_a_ready_klipper(self):
        self.assertEqual(self.switch(True)["state"], "ready")
        self.assertEqual(self.moon.restarts, 0)

    # -------------------------------------------------------------- what may never be switched
    def test_only_a_person_a_mapped_outlet_and_never_watch_only(self):
        self.refused(False, "only a person", actor="agent")
        self.refused(False, "only a person", actor="ui:unknown")
        self.set_printer(power=self.mapping, readonly=True)
        self.refused(False, "watch-only")
        self.set_printer(power=None, readonly=False)
        self.refused(False, "no power outlet mapped")
        self.set_printer(power={**self.mapping, "channel": 0})
        self.refused(False, "whole strip")

    def test_a_different_device_at_the_address_is_never_switched(self):
        self.strip.uuid = "ffffffffffffffffffffffffffffffff"
        with self.assertRaises(RuntimeError) as cm:
            self.switch(False)
        self.assertIn("not the mapped strip", str(cm.exception))
        self.assertEqual(self.strip.onoff[2], 1)
        self.assertEqual([c for c in self.strip.calls if c[0] == "SET"], [], "nothing is sent to a device it can't identify")

    def test_mapping_is_checked_against_the_strip(self):
        m = power.validate_mapping(NAME, {"ip": "127.0.0.1", "channel": 3})
        self.assertEqual((m["uuid"], m["channel"], m["model"]), (self.strip.uuid, 3, "mss426"))
        for bad, why in (({"ip": "127.0.0.1", "channel": 0}, "whole strip"), ({"ip": "127.0.0.1", "channel": 9}, "no outlet 9"),
                         ({"ip": "8.8.8.8", "channel": 1}, "LAN")):
            with self.assertRaises(ValueError) as cm:
                power.validate_mapping(NAME, bad)
            self.assertIn(why, str(cm.exception))
        other = next(n for n in printers.PRINTERS if n != NAME)
        with self.assertRaises(ValueError) as cm:
            power.validate_mapping(other, {"ip": "127.0.0.1", "channel": 2})    # NAME has outlet 2
        self.assertIn(f"already mapped to {NAME}", str(cm.exception))
        self.assertIsNone(power.validate_mapping(NAME, None))

    # -------------------------------------------------------------- named outlets (the farm lights)
    def _aux_files(self):
        from farm import cfgchange
        tmp = Path(tempfile.mkdtemp())
        (tmp / "FARM.md").write_text("| Date | Printer | Change |\n|---|---|---|\n")
        for patch in (mock.patch.object(power, "AUX_FILE", tmp / "power.json"), mock.patch.object(cfgchange, "FARM_MD", tmp / "FARM.md")):
            patch.start()
            self.addCleanup(patch.stop)
        return tmp

    def test_farm_lights_are_mapped_switched_by_a_person_and_kept_apart_from_printers(self):
        tmp = self._aux_files()
        r = power.map_aux("Farm lights", "127.0.0.1", 5, "cli:Ironman")
        self.assertEqual(r["outlet"]["channel"], 5)
        self.assertIn("'Farm lights'", (tmp / "FARM.md").read_text())
        self.assertEqual(power.aux_state(), [{"name": "Farm lights", "on": True, "channel": 5}])
        out = power.switch_aux(self.con, "Farm lights", False, "ui:Ironman")
        self.assertEqual((out["on"], self.strip.onoff[5], self.strip.onoff[2]), (False, 0, 1))   # only its outlet
        self.moon.state = "printing"                                                           # lights: no print checks
        self.assertTrue(power.switch_aux(self.con, "Farm lights", True, "ui:Ironman")["on"])
        with self.assertRaises(power.Refused):
            power.switch_aux(self.con, "Farm lights", False, "agent")
        with self.assertRaises(ValueError):                                                    # a printer's outlet
            power.map_aux("Farm lights", "127.0.0.1", 2, "cli:Ironman")
        with self.assertRaises(ValueError):                                                    # the lights' outlet
            power.validate_mapping("s1pro", {"ip": "127.0.0.1", "channel": 5})
        with self.assertRaises(ValueError):
            power.map_aux(NAME, "127.0.0.1", 4, "cli:Ironman")                                 # a printer's name
        self.strip.uuid = "f" * 32
        with self.assertRaises(RuntimeError):
            power.switch_aux(self.con, "Farm lights", False, "ui:Ironman")
        self.assertEqual(self.strip.onoff[5], 1)
        power.map_aux("Farm lights", None, None, "cli:Ironman")
        self.assertEqual(power.aux(), [])

    def test_farm_lights_follow_the_prints_and_respect_a_person(self):
        self._aux_files()
        power.map_aux(power.LIGHTS, "127.0.0.1", 5, "cli:Ironman")
        lights = lambda: self.strip.onoff[5]
        self.strip.onoff[5] = 0
        power.auto_lights({}, ["s1pro"], 0)
        self.assertEqual(lights(), 0, "not automatic until a person's own switch of the outlet read back clean")
        power.switch_aux(self.con, power.LIGHTS, True, "ui:Ironman")
        self.assertTrue(power.aux()[0]["proven"])
        def run(busy, t):
            power._cache.clear()
            power.auto_lights(st, busy, t)
        self.strip.onoff[5] = 0
        st = {}
        run([], 0)
        self.assertEqual(lights(), 0)                       # nothing printing, off: stays off
        run(["s1pro"], 10)
        self.assertEqual(lights(), 1)                       # a print: on
        run([], 100)
        run([], 100 + power.LIGHTS_OFF_AFTER_S - 1)
        self.assertEqual(lights(), 1)                       # not yet: a pause between prints keeps them on
        run([], 100 + power.LIGHTS_OFF_AFTER_S)
        self.assertEqual(lights(), 0)                       # idle long enough: off
        # a person switched them on: never switched off by the automation
        self.strip.onoff[5] = 1
        run([], 2000)
        run([], 9000)
        self.assertEqual(lights(), 1)
        # a person switches them off during a print: respected until the print ends
        self.strip.onoff[5], st = 0, {}
        run(["s1pro"], 10000)
        self.assertEqual(lights(), 1)
        self.strip.onoff[5] = 0                             # person: off
        run(["s1pro"], 10100)
        self.assertEqual(lights(), 0)
        run([], 10200)
        run(["s1pro"], 10300)                               # the next print: on again
        self.assertEqual(lights(), 1)
        # only the lights: no printer outlet was ever touched
        self.assertEqual({c: v for c, v in self.strip.onoff.items() if c != 5}, {0: 1, 1: 1, 2: 1, 3: 1, 4: 1})
        ev = db.connect().execute("SELECT to_state FROM events WHERE actor='monitor:auto-lights' ORDER BY id").fetchall()
        self.assertEqual([e[0] for e in ev], ["aux_on", "aux_off", "aux_on", "aux_on"])   # in the (test) farm DB

    # -------------------------------------------------------------- what keeps a print from being cut
    def test_no_off_anywhere_while_any_printer_prints(self):
        with mock.patch.object(power, "farm_busy", return_value=["s1pro"]):
            self.refused(False, "s1pro is printing: no power off anywhere")
        self._aux_files()
        power.map_aux(power.LIGHTS, "127.0.0.1", 5, "cli:Ironman")
        power.switch_aux(self.con, power.LIGHTS, True, "ui:Ironman")                 # proven
        st = {"auto_on": True, "idle_since": 0}
        with mock.patch.object(power, "farm_busy", return_value=["printer-200:7125"]):
            power._cache.clear()
            power.auto_lights(st, [], power.LIGHTS_OFF_AFTER_S + 1)
        self.assertEqual(self.strip.onoff[5], 1, "lights stay on while an unwatched printer prints")

    def test_farm_busy_reads_every_known_moonraker(self):
        from farm import hosts
        other = FakeMoonraker(copy.deepcopy(GOOD))
        other.state = "printing"
        self.addCleanup(other.server.shutdown)
        fake_hosts = [{"name": "printer-200", "address": "127.0.0.1", "ports": [other.server.server_address[1]]},
                      {"name": "gone", "address": "127.0.0.1", "ports": [9]}]          # unreadable: not counted
        with mock.patch.object(power, "farm_busy", self._real_busy), \
                mock.patch.object(hosts, "load_hosts", return_value=fake_hosts), \
                mock.patch.dict(printers.PRINTERS, {NAME: printers.PRINTERS[NAME]}, clear=True):
            self.assertEqual(power.farm_busy(timeout=2), [f"printer-200:{other.server.server_address[1]}"])

    def test_a_board_lost_mid_print_is_not_already_off(self):
        self.moon.klipper, self.moon.klipper_msg = "shutdown", "Lost communication with MCU 'mcu'"
        for job in ("printing", "paused", "error"):
            self.moon.state = job
            self.refused(False, f"print is {job}")

    def test_a_switch_that_changes_another_outlet_stops_all_switching(self):
        self.strip.crosstalk = 4
        with self.assertRaises(RuntimeError) as cm:
            self.switch(False)
        self.assertIn("also changed outlet(s) 4", str(cm.exception))
        self.strip.crosstalk = None
        sets = len([c for c in self.strip.calls if c[0] == "SET"])
        with self.assertRaises(power.Refused) as cm:                                   # latched: nothing more is sent
            self.switch(True)
        self.assertIn("power switching is stopped", str(cm.exception))
        self.assertEqual(len([c for c in self.strip.calls if c[0] == "SET"]), sets)
        state = {}
        for t in (0.0, 1600.0):
            with mock.patch.object(monitor.time, "time", return_value=t):
                out = monitor.power_pass(state)
        self.assertTrue(any(lv == "loud" and "stopped" in msg for lv, msg in out))
        with self.assertRaises(power.Refused):
            power.clear_trip("agent")
        power.clear_trip("cli:Ironman")
        self.assertIsNone(power.tripped())

    def test_no_flicker(self):
        """Quick off/on cycles can fry a board (Ironman, 2026-10-24): already-so sends nothing, a quick reversal is
        refused, and the lights automation waits instead of failing."""
        sets = lambda: len([c for c in self.strip.calls if c[0] == "SET"])
        self.switch(False)
        n = sets()
        self.switch(False)                                  # already off: a double press or a retry sends nothing
        self.assertEqual(sets(), n)
        with mock.patch.object(power, "MIN_DWELL_S", 60):
            with self.assertRaises(power.Refused) as cm:
                self.switch(True)
            self.assertIn("wait", str(cm.exception))
            self.assertEqual(sets(), n)
            self.assertGreater(power.wait_left(self.mapping), 50)
            with mock.patch.object(power.time, "time", return_value=time.time() + 61):
                self.assertEqual(power.wait_left(self.mapping), 0)
        self.switch(True)                                   # the dwell is over (0 in tests): it switches
        self.assertEqual(sets(), n + 1)

    def test_switches_on_one_strip_are_spaced(self):
        with mock.patch.object(power, "STRIP_GAP_S", 0.4):
            t0 = time.time()
            self.switch(False)
            power._toggle({**self.mapping, "channel": 3}, False)
            self.assertGreaterEqual(time.time() - t0, 0.39)

    def test_channel_zero_is_refused_at_the_switch_itself(self):
        with self.assertRaises(power.Refused):
            power._toggle({"ip": "127.0.0.1", "uuid": self.strip.uuid, "channel": 0}, False)
        self.assertEqual([c for c in self.strip.calls if c[0] == "SET"], [])

    # -------------------------------------------------------------- the printer's own power button (Telegram, Fluidd)
    def _serve(self):
        import threading
        from http.server import ThreadingHTTPServer
        from farm import web
        srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def _post(self, base, body, farm_header=True):
        import urllib.error
        import urllib.request
        h = {"Content-Type": "application/json", **({"X-Farm": "1"} if farm_header else {})}
        req = urllib.request.Request(f"{base}/api/power/device/{NAME}", data=json.dumps(body).encode(), headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_moonraker_device_endpoint_switches_through_the_gates(self):
        import urllib.request
        base = self._serve()
        with urllib.request.urlopen(f"{base}/api/power/device/{NAME}", timeout=10) as r:
            self.assertEqual(json.loads(r.read()), {"state": "on"})
        self.assertEqual(self._post(base, {"action": "off"}), (200, {"state": "off"}))
        self.assertEqual(self.strip.onoff[2], 0)
        self.moon.klipper, self.moon.klipper_msg = "error", "mcu 'mcu': Unable to connect"
        self.assertEqual(self._post(base, {"action": "on"}), (200, {"state": "on"}))
        self.assertEqual((self.strip.onoff[2], self.moon.restarts), (1, 0), "Moonraker restarts Klipper itself")
        ev = db.connect().execute("SELECT actor FROM events WHERE to_state IN ('power_on','power_off')").fetchall()   # the web server's DB
        self.assertTrue(all(e["actor"].endswith(f"via {NAME} (Telegram/Fluidd)") for e in ev) and ev)

    def test_moonraker_device_refusals_are_409_logged_and_relayed_once(self):
        base = self._serve()
        state = {}
        monitor.power_refusals(state)                                              # first pass: start of the log
        self.moon.extruder = {"temperature": 120.0, "target": 0.0}
        code, out = self._post(base, {"action": "off"})
        self.assertEqual(code, 409)
        self.assertIn("hotend 120 C", out["error"])
        self.assertEqual(self.strip.onoff[2], 1)
        with mock.patch.object(power, "farm_busy", return_value=["s1pro"]):
            self.moon.extruder = {"temperature": 25.0, "target": 0.0}
            self.assertEqual(self._post(base, {"action": "off"})[0], 409)
        told = monitor.power_refusals(state)
        self.assertEqual(len(told), 2)
        self.assertIn("hotend 120 C", told[0][1])
        self.assertIn("s1pro is printing", told[1][1])
        self.assertEqual(monitor.power_refusals(state), [], "told once")

    def test_moonraker_device_needs_the_farm_header_and_a_real_action(self):
        base = self._serve()
        self.assertEqual(self._post(base, {"action": "off"}, farm_header=False)[0], 403)
        self.assertEqual(self._post(base, {"action": "explode"})[0], 400)
        self.set_printer(power=self.mapping, standalone=True)
        self.assertEqual(self._post(base, {"action": "off"})[0], 409)
        self.assertEqual(self.strip.onoff[2], 1)

    def test_stage_device_puts_the_section_in_moonraker_conf(self):
        import configparser
        from farm import cfgchange
        self.moon.config["moonraker.conf"] = b"[server]\nhost: 0.0.0.0\n"
        with mock.patch.object(cfgchange, "_settings", return_value={}, create=True):
            c = power.stage_device(self.con, NAME, "agent")
        new = self.con.execute("SELECT new_content FROM config_changes WHERE id=?", (c["id"],)).fetchone()[0]
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_string(new if isinstance(new, str) else new.decode())
        self.assertEqual(cp["power printer"]["type"], "http")
        self.assertTrue(cp["power printer"]["on_url"].endswith(f"/api/power/device/{NAME}"))
        self.assertEqual(cp["power printer"]["locked_while_printing"], "True")
        self.assertEqual(cp["server"]["host"], "0.0.0.0", "the rest of moonraker.conf is kept")
        self.set_printer(power=self.mapping, standalone=True)
        with self.assertRaises(power.Refused):
            power.stage_device(self.con, NAME, "agent")

    # -------------------------------------------------------------- the monitor and the card
    def test_unreadable_strip_is_a_quiet_debounced_note(self):
        state = {}
        self.strip.down = True
        for t in (0.0, 1600.0):
            with mock.patch.object(monitor.time, "time", return_value=t):
                out = monitor.power_pass(state)
        self.assertEqual([lv for lv, _ in out], ["silent"])
        self.assertIn("power strip 127.0.0.1 unreadable", out[0][1])

    def test_card_state(self):
        self.assertEqual(power.state(printers.PRINTERS[NAME]), {"mapped": True, "on": True, "channel": 2})
        self.assertEqual(power.state(dataclasses.replace(printers.PRINTERS[NAME], power=None)), {"mapped": False})


if __name__ == "__main__":
    unittest.main()
