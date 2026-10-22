import tempfile
import unittest
import zipfile
from pathlib import Path

from farm import printers, slicer
from tests.helpers import FakeRun, box_stl


class AlignFirstLayer(unittest.TestCase):
    def test_change_lands_on_a_layer_top(self):
        for layer_h, z in ((0.4, 4.0), (0.36, 4.0), (0.28, 4.0), (0.2, 3.0)):
            first = slicer.align_first_layer(layer_h, [z])
            self.assertTrue(0.25 - 1e-9 <= first <= 0.45 + 1e-9, first)
            steps = (z - first) / layer_h
            self.assertAlmostEqual(steps, round(steps), places=6, msg=(layer_h, z, first))

    def test_no_changes_clamps_to_window(self):
        self.assertEqual(slicer.align_first_layer(0.2, []), 0.25)
        self.assertEqual(slicer.align_first_layer(0.36, []), 0.36)
        self.assertEqual(slicer.align_first_layer(0.6, []), 0.45)

    def test_impossible_set_of_changes_refuses(self):
        with self.assertRaises(SystemExit):
            slicer.align_first_layer(0.4, [4.0, 4.3])


class InsertColourChanges(unittest.TestCase):
    def write(self, zs):
        f = Path(tempfile.mkdtemp()) / "x.gcode"
        f.write_text("".join(f";LAYER_CHANGE\n;Z:{z}\nG1 X1 Y1 E1\n" for z in zs))
        return f

    def test_m600_before_first_layer_above_change(self):
        f = self.write([3.6, 4.0, 4.4, 4.8])
        self.assertEqual(slicer.insert_colour_changes(f, [4.0]), [(4.0, 4.4)])
        lines = f.read_text().splitlines()
        i = lines.index("M600")
        self.assertEqual(lines[i + 2], ";Z:4.4")
        self.assertEqual(lines.count("M600"), 1)

    def test_no_changes_leaves_file_alone(self):
        f = self.write([0.4, 0.8])
        before = f.read_text()
        self.assertEqual(slicer.insert_colour_changes(f, []), [])
        self.assertEqual(f.read_text(), before)

    def test_change_above_print_refuses(self):
        with self.assertRaises(SystemExit):
            slicer.insert_colour_changes(self.write([0.4, 0.8]), [5.0])


class LayoutBed(unittest.TestCase):
    def test_plates_fit_and_stay_inside_bed(self):
        run, p = FakeRun(), printers.PRINTERS["Ender3MaxNeo"]
        a = box_stl(run.path("A.stl"), 0, 0, 0, 185, 103, 10)
        b = box_stl(run.path("B.stl"), 0, 0, 0, 185, 103, 10)
        bed = slicer.layout_bed(run, p, [a, b], 10)
        self.assertEqual(bed, [run.path("bed.3mf")])
        model = zipfile.ZipFile(bed[0]).read("3D/3dmodel.model").decode()
        self.assertEqual(model.count("<object "), 2)
        xs = [float(x) for x in __import__("re").findall(r'x="([-\d.]+)"', model)]
        self.assertGreaterEqual(min(xs), p.bed_min[0])
        self.assertLessEqual(max(xs), p.bed_max[0])

    def test_too_many_plates_refuses(self):
        run, p = FakeRun(), printers.PRINTERS["Ender3MaxNeo"]
        stls = [box_stl(run.path(f"{i}.stl"), 0, 0, 0, 185, 103, 10) for i in range(3)]
        with self.assertRaises(SystemExit):
            slicer.layout_bed(run, p, stls, 10)


if __name__ == "__main__":
    unittest.main()
