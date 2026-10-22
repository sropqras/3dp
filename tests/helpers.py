"""Shared test fixtures: a run that records instead of writing, binary STL boxes, small G-code builders."""
import struct
import tempfile
from pathlib import Path


class FakeRun:
    """Stands in for runlog.Run: keeps steps and log lines in memory, fail() raises SystemExit like the real one."""

    def __init__(self, tmp: Path | None = None):
        self.dir = Path(tmp or tempfile.mkdtemp())
        self.steps, self.lines, self.manifest = {}, [], {"steps": {}}

    def log(self, msg):
        self.lines.append(msg)

    def step(self, name, **data):
        self.steps[name] = data

    def path(self, name):
        return self.dir / name

    def fail(self, msg):
        raise SystemExit(msg)


def box_stl(path: Path, x0, y0, z0, x1, y1, z1) -> Path:
    """Binary STL of an axis-aligned box (12 triangles)."""
    v = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1),
             (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    data = b"\0" * 80 + struct.pack("<I", len(faces))
    for a, b, c in faces:
        data += struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0)
    path.write_bytes(data)
    return path


def gcode(*, start="START_PRINT EXTRUDER_TEMP=245 BED_TEMP=80", before_start=(), m600=1, cap="13",
          end=True, moves=((100, 100), (200, 200)), zs=(0.4, 0.8)) -> str:
    out = [";generated for tests", *before_start]
    if start:
        out.append(start)
    for z in zs:
        out += [";LAYER_CHANGE", f";Z:{z}"]
        out += [f"G1 X{x} Y{y} E0.5" for x, y in moves]
    out += ["M600"] * m600
    if end:
        out.append("PRINT_END")
    if cap is not None:
        out.append(f"; filament_max_volumetric_speed = {cap}")
    out += ["; filament used [g] = 12.34", "; estimated printing time (normal mode) = 1h 2m 3s"]
    return "\n".join(out) + "\n"
