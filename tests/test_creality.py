"""Stock-Creality printers (the CR-10 SE) are read, never driven: their status feed counts them in the power strip's
'is anything printing?' check, cautiously (anything not provably idle is printing)."""
import os
import unittest
from unittest import mock

from farm import creality, power
from tests.fakes import FakeCreality

IDLE = {"state": 4, "deviceState": 0, "printFileName": "", "printProgress": 0, "nozzleTemp": "13.1",
        "targetNozzleTemp": 0, "bedTemp0": "11.8", "targetBedTemp0": 0, "model": "F003"}


class CrealityTest(unittest.TestCase):
    def feed(self, **kw):
        f = FakeCreality({**IDLE, **kw})
        self.addCleanup(f.close)
        return f

    def read(self, f):
        return creality.status("127.0.0.1", timeout=2, port=f.port)

    def test_idle_is_idle_and_nothing_is_ever_sent(self):
        f = self.feed()
        st = self.read(f)
        self.assertEqual((st["state"], st["nozzle"], st["model"]), (4, 13.1, "F003"))
        self.assertFalse(creality.busy(st))
        self.assertEqual(f.received, [], "only the handshake: no command after it")

    def test_anything_not_provably_idle_counts_as_printing(self):
        for kw in ({"state": 1}, {"state": 5}, {"state": 7}, {"state": "x"},
                   {"targetNozzleTemp": 210}, {"targetBedTemp0": 60}):
            self.assertTrue(creality.busy(self.read(self.feed(**kw))), kw)

    def test_a_feed_without_a_state_is_an_error(self):
        f = FakeCreality({"model": "F003"})
        self.addCleanup(f.close)
        with self.assertRaises(ConnectionError):
            self.read(f)

    def test_the_power_strip_sees_a_printing_creality(self):
        f = self.feed(state=1, printFileName="part.gcode")
        with mock.patch.dict(os.environ, {"FARM_CREALITY_HOSTS": "127.0.0.1"}), \
                mock.patch.object(creality, "PORT", f.port), \
                mock.patch.dict(power.printers.PRINTERS, {}, clear=True), \
                mock.patch("farm.hosts.load_hosts", return_value=[]):
            self.assertEqual(power.farm_busy(timeout=2), ["127.0.0.1 (Creality)"])
            f.fields["state"] = 4
            self.assertEqual(power.farm_busy(timeout=2), [])
        with mock.patch.dict(os.environ, {"FARM_CREALITY_HOSTS": "127.0.0.1"}), \
                mock.patch.object(creality, "PORT", 9), \
                mock.patch.dict(power.printers.PRINTERS, {}, clear=True), \
                mock.patch("farm.hosts.load_hosts", return_value=[]):
            self.assertEqual(power.farm_busy(timeout=1), [], "unreadable (switched off): not counted")


if __name__ == "__main__":
    unittest.main()
