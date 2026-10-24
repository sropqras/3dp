"""Printer power at the wall (farm/power.py) against a fake Meross strip and a fake Moonraker: a person switches a
mapped outlet; Off is refused unless the printer is provably idle and cool; nothing else is ever switched."""
import copy
import dataclasses
import json
import os
import tempfile
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
        aux = mock.patch.object(power, "AUX_FILE", Path(tempfile.mkdtemp()) / "power.json")   # never the real strip's map
        aux.start()
        self.addCleanup(aux.stop)
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
