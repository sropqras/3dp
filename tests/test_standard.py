"""The farm standard: macro library text and the Fluidd layout (pure functions; the install itself is tested in
tests/test_baseline.py through the real stage/apply gate)."""
import sqlite3
import unittest
from unittest import mock

from farm import audit, db, standard


class Library(unittest.TestCase):
    def test_every_library_macro_has_a_grouped_description_and_a_good_name(self):
        text = standard.library_text()
        names = standard.library_macros(text)
        self.assertIn("FARM_INFO", names)
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            body = text.split(f"[gcode_macro {name}]")[1].split("\n[")[0]
            desc = next(l for l in body.splitlines() if l.startswith("description:"))
            self.assertIn(standard._group(desc[len("description:"):].strip()), standard.GROUPS, name)
        s = {f"gcode_macro {n.lower()}": {"gcode": "x"} for n in names}
        bad = [r for st, r, d in audit.rules(s, mock.Mock(has_m600_prompt=None))
               if r.startswith("no macro named letter+digits") and st != "PASS"]
        self.assertEqual(bad, [])                                    # M205/M900 are allowed G-code names

    def test_values_come_from_the_ancestral_macros(self):
        files = {"printer.cfg": "[include macros.cfg]\n",
                 "macros.cfg": "[gcode_macro HEAT]\ngcode:\n  SET_HEATER_TEMPERATURE HEATER=extruder TARGET=205\n\n"
                               "[gcode_macro _DISABLE_FS]\ngcode:\n    #SET_FILAMENT_SENSOR SENSOR=RunoutSensor ENABLE=0\n"}
        out = standard.install(files, "RunoutSensor")
        self.assertIn("variable_nozzle_temp: 205", out["macros.cfg"])
        self.assertIn("variable_sensor: ''", out["macros.cfg"])    # commented out on purpose: stays off
        self.assertEqual(standard.check({}), (False, standard.check({})[1]))
        v = standard.printer_values({"m.cfg": "[gcode_macro PID_BED]\ngcode:\n  {% set target = params.TARGET | default(60) | float %}\n"
                                               "  PID_CALIBRATE HEATER=heater_bed TARGET={target}\n"}, None)
        self.assertEqual(v["pid_bed"], 60)                                      # V3SE writes it as a default


class FluiddLayout(unittest.TestCase):
    def test_groups_keep_a_persons_look_and_presets(self):
        cur = {"macros": {"stored": [{"name": "heat", "alias": "Warm up", "color": "#f00", "visible": False}]},
               "uiSettings": {"theme": {"color": "#123"}, "dashboard": {"tempPresets": [{"id": "own", "name": "Mine"}]}}}
        macros = {"heat": "Utility | home, heat", "start_print": "", "_farm_printer": "x", "weird": ""}
        new = standard.fluidd_layout(cur, macros, [{"name": "Farm PETG 240/80", "nozzle": 240, "bed": 80}])
        by = {m["name"]: m for m in new["macros"]["stored"]}
        self.assertNotIn("_farm_printer", by)
        self.assertEqual((by["heat"]["alias"], by["heat"]["color"], by["heat"]["visible"]), ("Warm up", "#f00", False))
        self.assertEqual(by["heat"]["categoryId"], "farm-utility")
        self.assertEqual(by["start_print"]["categoryId"], "farm-print")       # ancestral name
        self.assertEqual(by["weird"]["categoryId"], "0")
        self.assertEqual(new["uiSettings"]["theme"], {"color": "#123"})        # each printer keeps its colour
        names = [p["name"] for p in new["uiSettings"]["dashboard"]["tempPresets"]]
        self.assertEqual(names, ["Farm PETG 240/80", "Mine"])
        again = standard.fluidd_layout({**cur, **new}, macros, [{"name": "Farm PETG 240/80", "nozzle": 240, "bed": 80}])
        self.assertEqual(again, new)                                            # applying twice changes nothing

    def test_apply_is_a_persons_and_undo_puts_it_back(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        db.migrate(con)
        store = {"macros": {"stored": []}, "uiSettings": {"dashboard": {"tempPresets": []}}}
        with mock.patch.object(standard, "_fluidd", lambda url: dict(store)), \
                mock.patch.object(standard, "_put", lambda url, k, v: store.__setitem__(k, v)):
            with self.assertRaises(PermissionError):
                standard.fluidd_apply(con, "p", "http://x", {"cool": ""}, "agent")
            r = standard.fluidd_apply(con, "p", "http://x", {"cool": ""}, "ui:Amir")
            self.assertEqual(store["macros"]["stored"][0]["categoryId"], "farm-utility")
            standard.fluidd_undo(con, r["id"], "ui:Amir")
            self.assertEqual(store["macros"], {"stored": []})
            with self.assertRaises(PermissionError):     # Klipper not running: nothing to lay out, nothing wiped
                standard.fluidd_apply(con, "p", "http://x", {"_farm_printer": ""}, "ui:Amir")


if __name__ == "__main__":
    unittest.main()
