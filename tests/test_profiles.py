import dataclasses
import unittest

from farm import printers, profiles


class Profiles(unittest.TestCase):
    def test_every_profile_passes_the_static_checks(self):
        for p in profiles.PROFILES.values():
            self.assertEqual(profiles.check(p), [], p.id)

    def test_legacy_jobs_map_to_todays_presets(self):
        neo = printers.PRINTERS["Ender3MaxNeo"]
        p = profiles.legacy(neo, "petg")
        self.assertEqual(p.id, "neo-petg-036")
        self.assertEqual((p.bundle, p.print_preset), (neo.bundle, neo.print_preset))
        self.assertIs(profiles.legacy(printers.PRINTERS["enders1plus"], "petg"), p)

    def test_check_catches_known_bad_presets(self):
        base = profiles.PROFILES["neo-hyperpla-028-litho"]
        bundle_user = printers.PRUSA_DIR / "PrusaSlicer_config_bundle.ini"
        stock = dataclasses.replace(base, bundle=bundle_user, printer_preset="Creality Ender-3 S1 Plus (0.6 mm nozzle) - PLA220",
                                    filament_preset="Creality Hyper PLA 210_70",
                                    print_preset="0.28 mm SPEED (0.6 mm nozzle) @CREALITY - Litho Profile")
        problems = " | ".join(profiles.check(stock))
        for needle in ("auto-emits temperatures", "{first_layer_temperature[0]}", "volumetric cap 0.0"):
            self.assertIn(needle, problems)
        wrong_nozzle = dataclasses.replace(base, nozzle=0.4)
        self.assertTrue(any("nozzle" in x for x in profiles.check(wrong_nozzle)))
        missing = dataclasses.replace(base, print_preset="nope")
        self.assertTrue(any("[print:nope] not in" in x for x in profiles.check(missing)))

    def test_unconfirmed_profiles_say_why(self):
        for p in profiles.PROFILES.values():
            if not p.confirmed:
                self.assertTrue(p.note, p.id)


if __name__ == "__main__":
    unittest.main()
