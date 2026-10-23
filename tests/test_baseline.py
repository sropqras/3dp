import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path

from farm import audit, baseline, cfgchange, configs, db, printers
from tests.fakes import FakeMoonraker, parse_klipper

# a printer that fails every fixable farm-baseline rule (the SBC-era S1 Pro, roughly)
BAD_PRINTER = b"""[include macros.cfg]
[printer]
kinematics: cartesian
max_velocity: 1000
max_accel: 4800

[pause_resume]

[idle_timeout]
timeout: 99999999

[filament_switch_sensor RunoutSensor]
pause_on_runout: True
insert_gcode:
  RESUME
switch_pin: !PC15

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# [bltouch]
#*# z_offset = 3.340
"""
BAD_MACROS = b"""[gcode_macro PRINT_END]
gcode:
    TURN_OFF_HEATERS
    BEEP

[gcode_macro LOAD_FILAMENT]
gcode:
    G91
    G1 E50 F300

[gcode_macro PURGE]
gcode:
    G1 E45 F300

[gcode_macro START_PRINT]
gcode:
    {% set BED_TEMP = params.BED_TEMP|default(60)|float %}
    {% set EXTRUDER_TEMP = params.EXTRUDER_TEMP|default(190)|float %}
    M190 S{BED_TEMP}
    G28

[gcode_macro M600]
gcode:
    PAUSE
    UNLOAD_FILAMENT
"""


class Baseline(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.saved = (configs.STORE, cfgchange.FARM_MD, dict(printers.PRINTERS),
                      (cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S))
        configs.STORE, cfgchange.FARM_MD = self.tmp / "snap", self.tmp / "FARM.md"
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S = 0, 0.05, 0.2
        self.m = FakeMoonraker({})
        self.m.settings_from_config = True
        self.m.config = {"printer.cfg": BAD_PRINTER, "macros.cfg": BAD_MACROS}
        printers.PRINTERS["Ender3MaxNeo"] = dataclasses.replace(printers.PRINTERS["Ender3MaxNeo"], moonraker=self.m.url)
        self.con = db.connect(self.tmp / "farm.db")

    def tearDown(self):
        configs.STORE, cfgchange.FARM_MD, saved, timing = self.saved
        printers.PRINTERS.clear()
        printers.PRINTERS.update(saved)
        cfgchange.SETTLE_S, cfgchange.POLL_S, cfgchange.ERROR_GRACE_S = timing
        self.m.close()

    def failing(self):
        return [r for st, r, _ in audit.rules(parse_klipper(self.m.config), printers.PRINTERS["Ender3MaxNeo"]) if st == "FAIL"]

    def test_every_generated_fix_applied_makes_the_audit_pass(self):
        before = self.failing()
        self.assertGreaterEqual(len(before), 9, before)
        self.assertIn("no macro calls an undefined BEEP", before)
        for _ in range(12):                 # one rule at a time, as a person would from the tab
            fails = self.failing()
            if not fails:
                break
            rule = fails[0]
            staged = baseline.stage_fix(self.con, "Ender3MaxNeo", self.m.url, rule, parse_klipper(self.m.config), "ui:test")
            for c in staged:
                out = cfgchange.apply(self.con, c["id"], "ui:test")
                self.assertIn(out["state"], ("applied", "failed"), (rule, out))
        self.assertEqual(self.failing(), [])
        text = self.m.config["printer.cfg"].decode()
        self.assertTrue(text.endswith("#*# z_offset = 3.340\n"), "SAVE_CONFIG must stay untouched")
        self.assertNotIn("RESUME", parse_klipper(self.m.config)["filament_switch_sensor runoutsensor"]["insert_gcode"])
        macros = self.m.config["macros.cfg"].decode()
        self.assertIn("SET_FILAMENT_SENSOR SENSOR=RunoutSensor ENABLE=0", macros)   # the printer's own sensor name
        self.assertNotIn("UNLOAD_FILAMENT\n", macros.split("[gcode_macro M600]")[1].split("[")[0])
        self.assertIn("[gcode_macro LOAD_FILAMENT]\ngcode:\n    _FARM_HEAT_FOR_FILAMENT\n", macros)
        self.assertIn("VARIABLE=temp VALUE={printer.extruder.target}", macros)
        self.assertIn("# BEEP", macros)

    def test_stage_all_fixes_one_change_per_file_then_the_audit_passes(self):
        fixable = [r for r in self.failing() if baseline.fixable(r)]
        staged = baseline.stage_fixes(self.con, "Ender3MaxNeo", self.m.url, fixable, parse_klipper(self.m.config), "agent")
        self.assertEqual(sorted(c["path"] for c in staged), ["macros.cfg", "printer.cfg"])   # not one per rule
        self.assertIn("; ", staged[0]["reason"])
        for c in staged:                                                # both apply: neither is stale
            self.assertEqual(cfgchange.apply(self.con, c["id"], "ui:test")["state"], "applied")
        self.assertEqual([r for r in self.failing() if baseline.fixable(r)], [])

    def test_macro_library_replaces_the_ancestral_copies(self):
        from farm import standard
        self.m.config["macros.cfg"] = BAD_MACROS + b"""
[gcode_macro HEAT]
gcode:
    SET_HEATER_TEMPERATURE HEATER=extruder TARGET=230
    G28

[gcode_macro PID_BED]
gcode:
    PID_CALIBRATE HEATER=heater_bed TARGET=65
    SAVE_CONFIG

[gcode_macro COOL]
gcode:
    TURN_OFF_HEATERS
"""
        warn = [r for st, r, _ in audit.rules(parse_klipper(self.m.config), printers.PRINTERS["Ender3MaxNeo"]) if st == "WARN"]
        self.assertIn("farm macro library is current", warn)
        staged = baseline.stage_fix(self.con, "Ender3MaxNeo", self.m.url, "farm macro library is current",
                                    parse_klipper(self.m.config), "agent")
        self.assertEqual([c["path"] for c in staged], ["farm_macros.cfg", "macros.cfg"])   # library first
        with self.assertRaises(cfgchange.Refused):          # the include can't go on before the file it includes
            cfgchange.apply(self.con, staged[1]["id"], "ui:test")
        for c in staged:
            self.assertEqual(cfgchange.apply(self.con, c["id"], "ui:test")["state"], "applied")
        s = parse_klipper(self.m.config)
        self.assertEqual(standard.check(s), (True, standard.version()))
        macros = self.m.config["macros.cfg"].decode()
        for name in ("HEAT", "PID_BED", "COOL"):                     # one definition: the library's
            self.assertNotIn(f"[gcode_macro {name}]", macros)
        self.assertIn("variable_nozzle_temp: 230", macros)          # the printer's numbers kept
        self.assertIn("variable_pid_bed: 65", macros)
        self.assertIn("variable_sensor: 'RunoutSensor'", macros)
        self.assertTrue(macros.startswith("[include farm_macros.cfg]"))
        self.assertIn("params.TEMP|default", s["gcode_macro heat"]["gcode"])
        self.assertEqual(standard.install({p: t.decode() for p, t in self.m.config.items()}, "RunoutSensor"), {})   # idempotent

    def test_rules_needing_judgement_have_no_generated_fix(self):
        s = parse_klipper(self.m.config)
        for rule in ("START_PRINT macro exists", "START_PRINT reads EXTRUDER_TEMP and BED_TEMP",
                     "no macro named letter+digits (Klipper parses them as classic commands)"):
            self.assertEqual(baseline.fixes_for(rule, s, {"printer.cfg": BAD_PRINTER.decode()}, "x"), [], rule)

    def test_no_sensor_means_no_sensor_commands(self):
        blocks = baseline.colour_change_blocks("p", None)
        self.assertNotIn("SET_FILAMENT_SENSOR", blocks["gcode_macro _DISABLE_FS"])


if __name__ == "__main__":
    unittest.main()


class Fixable(unittest.TestCase):
    def test_every_audit_rule_is_classified_and_fixable_ones_generate(self):
        from farm import audit
        from tests.fakes import parse_klipper
        s = parse_klipper({"printer.cfg": BAD_PRINTER, "macros.cfg": BAD_MACROS})
        files = {"printer.cfg": BAD_PRINTER.decode(), "macros.cfg": BAD_MACROS.decode()}
        p = printers.PRINTERS["Ender3MaxNeo"]
        for st, rule, _ in audit.rules(s, p):
            if st == "FAIL":
                self.assertEqual(bool(baseline.fixes_for(rule, s, files, "x")), baseline.fixable(rule), rule)


class Minimal(unittest.TestCase):
    """A fix never replaces a printer's tuned macros: the S1 Plus case of 2026-10-23."""
    TUNED = """[gcode_macro M600]
description: tuned
gcode:
    PAUSE_MACRO
    _COLOUR_CHANGE_PROMPT

[gcode_macro _COLOUR_CHANGE_PROMPT]
gcode:
    RESPOND TYPE=command MSG="action:prompt_begin Colour change"
    RESPOND TYPE=command MSG="action:prompt_text Hold the filament, press UNLOAD and clip the old end to its spool."
    RESPOND TYPE=command MSG="action:prompt_show"

[gcode_macro _COLOUR_CHANGE_RESUME]
gcode:
    M83
    G1 E-2 F1800
    RESUME
"""

    def test_tgalarm_fix_adds_one_line_and_nothing_else(self):
        out = baseline.fixes_for("colour change alerts Telegram (tgalarm)", {}, {"macros.cfg": self.TUNED}, "s1plus")
        self.assertEqual(len(out), 1)
        import difflib
        added = [l for l in difflib.ndiff(self.TUNED.splitlines(), out[0]["content"].splitlines()) if l[:2] in ("+ ", "- ")]
        self.assertEqual(len(added), 1, added)
        self.assertTrue(added[0].startswith("+     RESPOND PREFIX=tgalarm"), added)

    def test_auto_unload_fix_only_drops_that_line(self):
        bad = self.TUNED.replace("    PAUSE_MACRO\n", "    PAUSE_MACRO\n    UNLOAD_FILAMENT\n")
        out = baseline.fixes_for("M600 does not auto-unload", {}, {"macros.cfg": bad}, "x")
        self.assertEqual(out[0]["content"], self.TUNED)
