"""Stage 3: check the G-code against what the printer and job expect before it goes anywhere near a printer."""
import math
import re
from pathlib import Path

MOVE = re.compile(r"^G1 (?:[^;]*?X(?P<x>-?[\d.]+))?(?:[^;]*?Y(?P<y>-?[\d.]+))?[^;]*?E(?P<e>-?[\d.]+)")


def validate(run, gcode: Path, printer, change_zs: list[float], max_flow: float) -> dict:
    text = gcode.read_text(encoding="utf-8")
    lines = text.splitlines()
    problems, facts = [], {}

    start = [i for i, l in enumerate(lines) if l.startswith("START_PRINT")]
    if len(start) != 1:
        problems.append(f"expected one START_PRINT, found {len(start)}")
    else:
        facts["start_print"] = lines[start[0]]
        early = [l for l in lines[:start[0]] if re.match(r"^M(104|109|140|190) ", l)]
        if early:
            problems.append(f"temperature commands before START_PRINT: {early}")
    if not any(l.startswith("PRINT_END") for l in lines):
        problems.append("PRINT_END missing")

    m600 = [i for i, l in enumerate(lines) if l.strip() == "M600"]
    facts["m600_count"] = len(m600)
    if len(m600) != len(change_zs):
        problems.append(f"expected {len(change_zs)} M600, found {len(m600)}")
    if m600 and not printer.has_m600_prompt:
        problems.append(f"{printer.name} has no guided M600 installed (FARM.md)")

    cap = re.search(r"^; filament_max_volumetric_speed = ([\d.]+)", text, re.M)
    facts["volumetric_cap"] = float(cap.group(1)) if cap else 0.0
    if not cap or not 0 < float(cap.group(1)) <= max_flow:
        problems.append(f"filament_max_volumetric_speed must be in (0, {max_flow}], got {facts['volumetric_cap']}")

    # extent of extruding moves (absolute XY, relative E)
    xs, ys = [], []
    for l in lines:
        m = MOVE.match(l)
        if m and float(m.group("e")) > 0:
            if m.group("x"):
                xs.append(float(m.group("x")))
            if m.group("y"):
                ys.append(float(m.group("y")))
    if xs and ys:
        facts["extents"] = [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]
        (x0, y0), (x1, y1) = printer.bed_min, printer.bed_max
        if min(xs) < x0 or min(ys) < y0 or max(xs) > x1 or max(ys) > y1:
            problems.append(f"extrusion {facts['extents']} leaves bed {printer.bed_min}-{printer.bed_max}")

    zs = [float(l[3:]) for l in lines if l.startswith(";Z:")]
    facts["layers"] = len(zs)
    facts["top_z"] = max(zs) if zs else 0
    if zs and max(zs) > printer.max_z:
        problems.append(f"print height {max(zs)} exceeds {printer.max_z}")

    for key in ("estimated printing time (normal mode)", "filament used [g]", "filament used [mm]"):
        m = re.search(r"^; " + re.escape(key) + r" = (.*)$", text, re.M)
        if m:
            facts[key] = m.group(1).strip()

    facts["problems"] = problems
    run.step("validate", **facts)
    for k, v in facts.items():
        if k != "problems":
            run.log(f"check: {k} = {v}")
    if problems:
        run.fail("; ".join(problems))
    run.log("check: all good")
    return facts


def flow_at(width: float, height: float, speed: float) -> float:
    """PrusaSlicer's rounded-rectangle extrusion cross-section times speed, in mm^3/s."""
    return ((width - height) * height + math.pi * (height / 2) ** 2) * speed
