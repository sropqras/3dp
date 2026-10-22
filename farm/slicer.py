"""Stage 2: slice with PrusaSlicer's CLI using the printer's presets, then insert colour changes.

Colour changes are placed on exact layer boundaries: the first layer height is adjusted so that every
requested change height (e.g. the top of the base plate) lands on a layer top, then M600 is inserted
right before the first layer above it.
"""
import re
import subprocess
from pathlib import Path

from . import config

EPS = 1e-4

# The 2.9.6 CLI segfaults on "<something>_extruder = 0" (GUI meaning: "current extruder"), e.g.
# bed_temperature_extruder, wipe_tower_extruder (bisected 2026-10-22). Dropping them falls back to
# extruder 1, which is the same thing on single-extruder printers.
CLI_UNSAFE = re.compile(r"^\w+_extruder = 0$")


def extract_preset(bundle: Path, kind: str, name: str, out: Path) -> Path:
    text = bundle.read_text(encoding="utf-8")
    m = re.search(r"^\[" + re.escape(f"{kind}:{name}") + r"\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    if not m:
        raise SystemExit(f"preset [{kind}:{name}] not found in {bundle}")
    body = "\n".join(l for l in m.group(1).splitlines() if not CLI_UNSAFE.match(l))
    out.write_text(body + "\n", encoding="utf-8")
    return out


def preset_value(ini: Path, key: str) -> str | None:
    m = re.search(r"^" + re.escape(key) + r" = (.*)$", ini.read_text(encoding="utf-8"), re.M)
    return m.group(1).strip() if m else None


def align_first_layer(layer_h: float, change_zs: list[float], lo: float = 0.25, hi: float = 0.45) -> float:
    """First layer height in [lo, hi] such that every change z sits on a layer top, else SystemExit."""
    if not change_zs:
        return min(max(layer_h, lo), hi)
    z0 = change_zs[0]
    for n in range(int(z0 / layer_h) + 1, -1, -1):
        first = round(z0 - n * layer_h, 4)
        if lo - EPS <= first <= hi + EPS and all(
                abs(((z - first) / layer_h) - round((z - first) / layer_h)) < EPS for z in change_zs):
            return first
    raise SystemExit(f"no first layer in [{lo},{hi}] puts {change_zs} on {layer_h} mm layer boundaries; "
                     f"pick a layer height that divides the gaps")


def slice_model(run, printer, stl: Path, material: str, layer_h: float, change_zs: list[float]) -> Path:
    presets = run.path("presets")
    presets.mkdir(exist_ok=True)
    p_ini = extract_preset(printer.bundle, "printer", printer.printer_preset, presets / "printer.ini")
    f_ini = extract_preset(printer.bundle, "filament", printer.filament_presets[material], presets / "filament.ini")
    s_ini = extract_preset(printer.bundle, "print", printer.print_preset, presets / "print.ini")

    first = align_first_layer(layer_h, change_zs)
    cx, cy = printer.bed_center
    out = run.path(f"{stl.stem}_{layer_h}mm_{material}_{printer.name}.gcode")
    cmd = config.slicer_cmd() + [
        "--load", str(p_ini), "--load", str(f_ini), "--load", str(s_ini),
        "--layer-height", str(layer_h), "--first-layer-height", str(first),
        "--center", f"{cx},{cy}", "--ensure-on-bed",
        "--export-gcode", "--output", str(out), "--loglevel", "2", str(stl),
    ]
    run.log(f"slice: layer {layer_h} mm, first layer {first} mm (aligned for colour changes at {change_zs})")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    run.path("slicer.log").write_text(" ".join(cmd) + "\n\n" + proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not out.is_file():
        run.fail(f"PrusaSlicer exited {proc.returncode}; see slicer.log")

    inserted = insert_colour_changes(out, change_zs)
    for z, at in inserted:
        run.log(f"slice: M600 inserted after layer top z={z} (before layer z={at})")
    run.step("slice", cmd=cmd, first_layer=first, layer_height=layer_h, gcode=str(out),
             colour_changes=[{"after_z": z, "before_layer_z": at} for z, at in inserted])
    return out


def insert_colour_changes(gcode: Path, change_zs: list[float]) -> list[tuple]:
    lines = gcode.read_text(encoding="utf-8").splitlines(keepends=True)
    pending = sorted(change_zs)
    inserted = []
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if pending and line.startswith(";LAYER_CHANGE") and i + 1 < len(lines) and lines[i + 1].startswith(";Z:"):
            z = float(lines[i + 1][3:])
            if z > pending[0] + EPS:
                out.append(f";COLOR_CHANGE after z={pending[0]} (farm pipeline)\nM600\n")
                inserted.append((pending.pop(0), z))
        out.append(line)
        i += 1
    if pending:
        raise SystemExit(f"colour change heights {pending} are above the top of the print")
    gcode.write_text("".join(out), encoding="utf-8", newline="\n")
    return inserted
