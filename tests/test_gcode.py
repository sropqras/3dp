import tempfile
import unittest
from pathlib import Path

from farm import gcode, printers
from tests import helpers
from tests.helpers import FakeRun

NEO = printers.PRINTERS["Ender3MaxNeo"]
V3SE = printers.PRINTERS["EnderV3SE"]


def check(text, printer=NEO, changes=(4.0,), max_flow=13):
    f = Path(tempfile.mkdtemp()) / "t.gcode"
    f.write_text(text)
    return gcode.validate(FakeRun(), f, printer, list(changes), max_flow)


class Validate(unittest.TestCase):
    def test_good_gcode_passes(self):
        facts = check(helpers.gcode())
        self.assertEqual(facts["m600_count"], 1)
        self.assertEqual(facts["extents"], [100.0, 100.0, 200.0, 200.0])
        self.assertEqual(facts["filament used [g]"], "12.34")
        self.assertEqual(facts["problems"], [])

    def assertRefused(self, text, needle, **kw):
        with self.assertRaises(SystemExit) as e:
            check(text, **kw)
        self.assertIn(needle, str(e.exception))

    def test_temperature_before_start_print(self):
        self.assertRefused(helpers.gcode(before_start=("M140 S80",)), "temperature commands before START_PRINT")

    def test_start_print_missing_or_twice(self):
        self.assertRefused(helpers.gcode(start=None), "expected one START_PRINT")

    def test_start_print_needs_numeric_temperatures(self):
        self.assertRefused(helpers.gcode(start="START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP=80"),
                           "no numeric EXTRUDER_TEMP")
        self.assertRefused(helpers.gcode(start="START_PRINT"), "no numeric BED_TEMP")

    def test_print_end_missing(self):
        self.assertRefused(helpers.gcode(end=False), "PRINT_END missing")

    def test_m600_count_must_match_job(self):
        self.assertRefused(helpers.gcode(m600=0), "expected 1 M600, found 0")
        self.assertRefused(helpers.gcode(m600=2), "expected 1 M600, found 2")

    def test_m600_needs_guided_prompt(self):
        self.assertRefused(helpers.gcode(moves=((100, 100),)), "no guided M600", printer=V3SE)

    def test_volumetric_cap_required_and_bounded(self):
        self.assertRefused(helpers.gcode(cap="0"), "filament_max_volumetric_speed")
        self.assertRefused(helpers.gcode(cap=None), "filament_max_volumetric_speed")
        self.assertRefused(helpers.gcode(cap="20"), "filament_max_volumetric_speed")

    def test_extrusion_outside_bed(self):
        self.assertRefused(helpers.gcode(moves=((2, 100), (200, 200))), "leaves bed")

    def test_height_limit(self):
        self.assertRefused(helpers.gcode(zs=(0.4, 301.0)), "exceeds")


if __name__ == "__main__":
    unittest.main()
