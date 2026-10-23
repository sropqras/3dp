import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from farm import makers
from tests.fakes import FakeMakers
from tests.helpers import box_stl

TYPES = {"number", "integer", "string", "boolean", "enum", "json"}


class Contract(unittest.TestCase):
    """Every adapter honours farm-maker/1: a well-formed spec, valid defaults, STL out, 4xx final, 5xx retryable."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.fake = FakeMakers(cls.tmp)
        cls.all = makers.all_makers()
        for m in cls.all.values():
            m.spec = dataclasses.replace(type(m).spec, url=cls.fake.url)
        img = cls.tmp / "p.png"
        img.write_bytes(b"\x89PNG fake")
        cls.files = {"likhan": {}, "lithophane": {"image": img}, "lamp": {"fileToUpload1": img}, "plaque": {},
                     "namakdani": {"file": box_stl(cls.tmp / "s.stl", 0, 0, 0, 10, 10, 10)}, "holder": {"image1": img}}
        cls.params = {"likhan": {"text": "A1"}, "plaque": {"text": "A1"}}

    @classmethod
    def tearDownClass(cls):
        for m in cls.all.values():
            del m.spec
        cls.fake.close()

    def ctx(self, mid):
        return makers.GenContext(params=self.params.get(mid, {}), files=self.files[mid], bed_mm=(290, 290, 300),
                                 nozzle=0.6, layer_height=0.28, out_dir=Path(tempfile.mkdtemp()), log=lambda m: None,
                                 label="X1")

    def test_all_five_makers_are_registered(self):
        self.assertEqual(set(self.all), {"likhan", "lithophane", "lamp", "plaque", "namakdani", "holder"})

    def test_specs_are_well_formed_and_defaults_validate(self):
        for mid, m in self.all.items():
            s = m.spec
            self.assertEqual(s.id, mid)
            self.assertIn(s.mode, ("sync", "async"))
            self.assertGreater(s.timeout_s, 0)
            for name, p in s.params.items():
                self.assertIn(p.get("type"), TYPES, f"{mid}.{name}")
                if p.get("type") == "enum":
                    self.assertTrue(p.get("enum"), f"{mid}.{name}")
            defaults = {k: v for k, v in makers.Maker.defaults(m).items()}
            self.assertEqual(makers.Maker.validate(m, defaults, self.files[mid]), [], mid)
            json.dumps(m.manifest())
            for h in s.colour_heights:
                self.assertEqual(s.colour_changes, "farm", mid)

    def test_generate_returns_stl_models(self):
        for mid, m in self.all.items():
            res = m.generate(self.ctx(mid))
            self.assertTrue(res.models, mid)
            for mo in res.models:
                self.assertTrue(mo.path.is_file() and mo.triangles == 12, mid)

    def test_4xx_is_final_5xx_is_retryable(self):
        for code, retry in ((400, False), (503, True)):
            self.fake.status = code
            try:
                for mid, m in self.all.items():
                    with self.assertRaises(makers.MakerError, msg=f"{mid} {code}") as e:
                        m.generate(self.ctx(mid))
                    self.assertEqual(e.exception.retryable, retry, f"{mid} {code}")
            finally:
                self.fake.status = None

    def test_validate_catches_bad_input(self):
        m = self.all["lithophane"]
        self.assertTrue(any("doesn't fit" in p for p in m.validate({"width_mm": 500, "height_mm": 400}, {"image": 1}, self.ctx("lithophane"))))
        self.assertTrue(any("outside" in p for p in m.validate({"width_mm": 5000}, {"image": 1})))
        self.assertTrue(any("unknown parameter" in p for p in m.validate({"nope": 1}, {"image": 1})))
        self.assertTrue(any("photo, some text" in p for p in m.validate({}, {})))


if __name__ == "__main__":
    unittest.main()
