"""Standalone printers (Creality Nebula Pad, the CR-10 SE's own screen) keep their maker's setup: the farm watches,
powers and sends files, but its Klipper standards are reported as INFO and never staged as fixes."""
import copy
import dataclasses
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from farm import audit, baseline, cfgchange, db, printers
from tests.test_audit_model3mf import GOOD


def statuses(settings, standalone):
    p = SimpleNamespace(name="x", has_m600_prompt=False, standalone=standalone)
    return {r: st for st, r, d in audit.rules(settings, p)}


class Standalone(unittest.TestCase):
    def test_farm_standards_become_info_but_compatibility_stays_checked(self):
        s = copy.deepcopy(GOOD)
        s["printer"]["max_velocity"] = 500.0
        farm = statuses(s, False)
        alone = statuses(s, True)
        self.assertEqual(farm["max_velocity <= 200"], "FAIL")
        self.assertEqual(alone["max_velocity <= 200"], "INFO")
        self.assertNotIn("FAIL", alone.values(), "a standalone printer has no farm-standard FAILs")
        s2 = copy.deepcopy(GOOD)
        for k in [k for k in s2 if k.startswith("gcode_macro start_print")]:
            del s2[k]
        self.assertEqual(statuses(s2, True)["START_PRINT macro exists"], "FAIL", "farm G-code would not run: still a FAIL")

    def test_generated_fixes_are_refused_for_a_standalone_printer(self):
        name = next(iter(printers.PRINTERS))
        alone = dataclasses.replace(printers.PRINTERS[name], standalone=True)
        con = db.connect(Path(tempfile.mkdtemp()) / "farm.db")
        with mock.patch.dict(printers.PRINTERS, {name: alone}):
            for call in (lambda: baseline.stage_fix(con, name, "http://127.0.0.1:9", "max_velocity <= 200", {}, "agent"),
                         lambda: baseline.stage_fixes(con, name, "http://127.0.0.1:9", ["max_velocity <= 200"], {}, "agent")):
                with self.assertRaises(cfgchange.Refused) as cm:
                    call()
                self.assertIn("standalone", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
