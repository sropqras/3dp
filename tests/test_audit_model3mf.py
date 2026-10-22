import copy
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from farm import audit, model3mf, printers, slicer

NEO = printers.PRINTERS["Ender3MaxNeo"]

# shaped like Moonraker's configfile.settings for a printer that meets the farm baseline
GOOD = {
    "printer": {"max_velocity": 200, "max_accel": 3000},
    "pause_resume": {"recover_velocity": 150.0},
    "gcode_macro start_print": {"gcode": "{% set BED_TEMP = params.BED_TEMP|default(70)|float %}\n"
                                         "{% set EXTRUDER_TEMP = params.EXTRUDER_TEMP|default(220)|float %}\n"
                                         "CLEAR_PAUSE\nBED_MESH_CALIBRATE\nLINE_PURGE"},
    "gcode_macro m600": {"gcode": "PAUSE\n_COLOUR_CHANGE_PROMPT"},
    "gcode_macro _colour_change_prompt": {"gcode": "RESPOND TYPE=command MSG=action:prompt_begin\n"
                                                    "TELEGRAM PREFIX=tgalarm_photo"},
    "filament_switch_sensor runout": {"insert_gcode": "M117 inserted"},
    "idle_timeout": {"timeout": 1800, "gcode": "{% if printer.pause_resume.is_paused %}M104 S0{% endif %}"},
    "delayed_gcode _pause_hard_stop": {},
}


def fails(settings):
    return [r for st, r, d in audit.rules(settings, NEO) if st == "FAIL"]


class AuditRules(unittest.TestCase):
    def test_baseline_passes(self):
        self.assertEqual(fails(GOOD), [])

    def mutate(self, fn):
        s = copy.deepcopy(GOOD)
        fn(s)
        return fails(s)

    def test_each_lesson_is_caught(self):
        cases = {
            "max_velocity": lambda s: s["printer"].update(max_velocity=300),
            "max_accel": lambda s: s["printer"].update(max_accel=5000),
            "recover_velocity": lambda s: s["pause_resume"].update(recover_velocity=25),
            "CLEAR_PAUSE": lambda s: s["gcode_macro start_print"].update(
                gcode="params.BED_TEMP params.EXTRUDER_TEMP BED_MESH_CALIBRATE"),
            "reads EXTRUDER_TEMP": lambda s: s["gcode_macro start_print"].update(gcode="CLEAR_PAUSE\nM190 S70"),
            "auto-unload": lambda s: s["gcode_macro m600"].update(gcode="PAUSE\nUNLOAD_FILAMENT\n_COLOUR_CHANGE_PROMPT"),
            "tgalarm": lambda s: s["gcode_macro _colour_change_prompt"].update(gcode="RESPOND MSG=x"),
            "letter+digits": lambda s: s.update({"gcode_macro _m600_prompt": {"gcode": ""}}),
            "insert does not resume": lambda s: s["filament_switch_sensor runout"].update(insert_gcode="RESUME"),
            "idle_timeout is finite": lambda s: s["idle_timeout"].update(timeout=99999),
            "pause-aware": lambda s: s["idle_timeout"].update(gcode="TURN_OFF_HEATERS"),
            "12 h": lambda s: s.pop("delayed_gcode _pause_hard_stop"),
        }
        for needle, fn in cases.items():
            got = self.mutate(fn)
            self.assertTrue(any(needle.lower() in r.lower() for r in got), f"{needle}: {got}")

    def test_g_and_m_codes_are_not_flagged_as_letter_digit_macros(self):
        s = copy.deepcopy(GOOD)
        s["gcode_macro m600"]["gcode"] += ""
        s["gcode_macro g29"] = {"gcode": ""}
        self.assertEqual(fails(s), [])


class Model3mf(unittest.TestCase):
    def test_transforms_names_colours_and_layers(self):
        tmp = Path(tempfile.mkdtemp())
        tri = (0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 10, 4)
        src = tmp / "geom.3mf"
        slicer._write_3mf(src, [("A01", [tri])])
        model = zipfile.ZipFile(src).read("3D/3dmodel.model").decode()
        model = model.replace('<item objectid="1"/>', '<item objectid="1" transform="1 0 0 0 1 0 0 0 1 100 50 0"/>')
        out = tmp / "p.3mf"
        with zipfile.ZipFile(src) as a, zipfile.ZipFile(out, "w") as b:
            for i in a.infolist():
                b.writestr(i, model if i.filename == "3D/3dmodel.model" else a.read(i.filename))
            b.writestr("Metadata/Slic3r_PE.config", "; bed_shape = 5x5,295x5,295x295,5x295\n; layer_height = 0.4\n"
                                                    "; first_layer_height = 0.4\n; extruder_colour = \"#1c1c1c\"\n")
            b.writestr("Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml",
                       '<custom_gcodes_per_print_z><code print_z="4.4" type="0" color="#F2F2F2" gcode="M600"/>'
                       '</custom_gcodes_per_print_z>')
        blob = model3mf.load(out)
        n = struct.unpack("<I", blob[:4])[0]
        h = json.loads(blob[4:4 + n])
        self.assertEqual(h["objects"], ["A01"])
        self.assertEqual(h["bbox"], [[100.0, 50.0, 0.0], [110.0, 60.0, 4.0]])
        self.assertEqual(h["first_colour"], "#1c1c1c")
        self.assertEqual(h["colour_changes"], [{"print_z": 4.4, "colour": "#F2F2F2"}])
        self.assertEqual(h["layer_height"], 0.4)
        self.assertEqual(h["bed_shape"][0], [5.0, 5.0])
        self.assertEqual(len(blob), 4 + n + 3 * 3 * 4 + 3 * 4)


if __name__ == "__main__":
    unittest.main()
