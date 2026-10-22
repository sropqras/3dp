"""Compare fill strategies on one bed: slice the same bed (a pipeline bed.3mf, or one STL) with each variant and report PrusaSlicer's time and
filament estimates plus a per-feature time split (G-code moves / feedrate, so relative, not absolute).

Usage: python scripts/infill_study.py <run-dir-with-presets> <out-dir> <stl> [<stl> ...]
"""
import math
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from farm import config  # noqa: E402

VARIANTS = {
    # name: CLI overrides on top of the MaxNeo PETG presets
    "gyroid 10% (current)": ["--fill-pattern", "gyroid", "--fill-density", "10%"],
    "gyroid 15%": ["--fill-pattern", "gyroid", "--fill-density", "15%"],
    "gyroid 20%": ["--fill-pattern", "gyroid", "--fill-density", "20%"],
    "grid 15%": ["--fill-pattern", "grid", "--fill-density", "15%"],
    "rectilinear 15%": ["--fill-pattern", "rectilinear", "--fill-density", "15%"],
    "cubic 15%": ["--fill-pattern", "cubic", "--fill-density", "15%"],
    "gyroid 15% + 4 walls": ["--fill-pattern", "gyroid", "--fill-density", "15%", "--perimeters", "4"],
    "solid 100%": ["--fill-pattern", "rectilinear", "--fill-density", "100%"],
}

# Round 2: the infill pattern barely moves the time; solid skins and the bridge layer over the infill do.
ROUND2 = {
    "baseline (gyroid 15%, 3 bottom / 4 top)": [],
    "top 3": ["--top-solid-layers", "3"],
    "bridge 45 mm/s": ["--bridge-speed", "45"],
    "top 3 + bridge 45": ["--top-solid-layers", "3", "--bridge-speed", "45"],
    "top 3 + bridge 45 + top surface 50": ["--top-solid-layers", "3", "--bridge-speed", "45",
                                           "--top-solid-infill-speed", "50"],
}
if "--round2" in sys.argv:
    sys.argv.remove("--round2")
    VARIANTS = ROUND2


def feature_times(gcode: Path) -> dict:
    """Seconds per ;TYPE: role from distance / feedrate (ignores acceleration)."""
    t, f, role = defaultdict(float), 0.0, "Other"
    x = y = 0.0
    for line in gcode.read_text(encoding="utf-8").splitlines():
        if line.startswith(";TYPE:"):
            role = line[6:].strip()
        elif line.startswith(("G1 ", "G0 ")):
            m = dict(re.findall(r"([XYZEF])(-?[\d.]+)", line.split(";")[0]))
            if "F" in m:
                f = float(m["F"])
            nx, ny = float(m.get("X", x)), float(m.get("Y", y))
            d = math.hypot(nx - x, ny - y)
            if d and f:
                extruding = "E" in m and float(m["E"]) > 0
                t[role if extruding else "Travel"] += d / (f / 60)
            x, y = nx, ny
    return dict(t)


def main():
    run_dir, out_dir, stls = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
    out_dir.mkdir(parents=True, exist_ok=True)
    presets = [str(run_dir / "presets" / n) for n in ("printer.ini", "filament.ini", "print.ini")]
    rows = []
    for name, extra in VARIANTS.items():
        out = out_dir / (re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") + ".gcode")
        cmd = config.slicer_cmd() + sum([["--load", p] for p in presets], []) + [
            "--layer-height", "0.4", "--first-layer-height", "0.4",
            *(["--dont-arrange"] if stls[0].endswith(".3mf") else ["--center", "150,150"]), *extra, "--export-gcode", "--output", str(out), *stls]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode or not out.is_file():
            print(f"{name}: slice failed ({r.returncode})"); continue
        g = out.read_text(encoding="utf-8")
        est = re.search(r"^; estimated printing time \(normal mode\) = (.*)$", g, re.M).group(1)
        grams = float(re.search(r"^; filament used \[g\] = ([\d.]+)", g, re.M).group(1))
        ft = feature_times(out)
        total = sum(ft.values())
        top = sorted(ft.items(), key=lambda kv: -kv[1])[:4]
        rows.append((name, est, grams, ", ".join(f"{k} {100 * v / total:.0f}%" for k, v in top)))
    w = max(len(r[0]) for r in rows)
    print(f"{'variant':{w}} | estimate     | grams | where the time goes (move time share)")
    for name, est, grams, split in rows:
        print(f"{name:{w}} | {est:12} | {grams:5.1f} | {split}")


if __name__ == "__main__":
    main()
