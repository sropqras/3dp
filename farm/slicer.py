"""Stage 2: slice with PrusaSlicer's CLI using the printer's presets, then insert colour changes.

Colour changes are placed on exact layer boundaries: the first layer height is adjusted so that every
requested change height (e.g. the top of the base plate) lands on a layer top, then M600 is inserted
right before the first layer above it.
"""
import re
import struct
import subprocess
import zipfile
from pathlib import Path

from . import config, profiles

SLICE_TIMEOUT_S = 1800   # a big multi-plate bed takes a few minutes; 30 is a hang

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


def slice_model(run, printer, stls: list[Path], material: str, layer_h: float, change_zs: list[float],
                spacing_mm: float = 10.0, profile=None, avoid: set | None = None, tag: str = "") -> Path:
    """Slice one or more STLs onto one bed (PrusaSlicer arranges several, spacing_mm apart) with the job's
    profile; jobs without one get the printer's own presets (profiles.legacy), exactly as before profiles."""
    profile = profile or profiles.legacy(printer, material)
    if not profile.allows(printer.name):
        run.fail(f"profile {profile.id} is not for {printer.name} (only {', '.join(profile.printers)})")
    if layer_h not in profile.layer_heights:
        run.log(f"slice: note: layer {layer_h} mm is not one of {profile.id}'s choices {profile.layer_heights}")
    stls = [stls] if isinstance(stls, Path) else list(stls)
    presets = run.path("presets")
    presets.mkdir(exist_ok=True)
    p_ini = extract_preset(profile.bundle, "printer", profile.printer_preset, presets / "printer.ini")
    f_ini = extract_preset(profile.bundle, "filament", profile.filament_preset, presets / "filament.ini")
    s_ini = extract_preset(profile.bundle, "print", profile.print_preset, presets / "print.ini")

    first = align_first_layer(layer_h, change_zs, *profile.first_layer)
    cx, cy = printer.bed_center
    stem = "+".join(s.stem for s in stls)  # computed before layout_bed swaps the STLs for bed.3mf
    out = run.path(f"{stem}_{layer_h}mm_{profile.material}_{printer.name}.gcode")
    if avoid and out.name in avoid:   # another build already has this name (qty > 1, a re-send): keep both files
        out = run.path(f"{stem}_{layer_h}mm_{profile.material}_{printer.name}_{tag}.gcode")
    if len(stls) == 1:
        placement = ["--center", f"{cx},{cy}"]
    else:
        # deterministic layout, each plate stays its own object (cancellable via EXCLUDE_OBJECT)
        stls = layout_bed(run, printer, stls, spacing_mm)
        placement = ["--dont-arrange"]
    cmd = config.slicer_cmd() + [
        "--load", str(p_ini), "--load", str(f_ini), "--load", str(s_ini),
        "--layer-height", str(layer_h), "--first-layer-height", str(first),
        *placement, "--ensure-on-bed",
        "--export-gcode", "--output", str(out), "--loglevel", "2", *map(str, stls),
    ]
    run.log(f"slice: layer {layer_h} mm, first layer {first} mm (aligned for colour changes at {change_zs})")
    try:   # a hung slicer must not stop every queue while the worker's heartbeat says it's alive
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=SLICE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        run.fail(f"PrusaSlicer did not finish in {SLICE_TIMEOUT_S // 60} min; killed")
    run.path("slicer.log").write_text(" ".join(cmd) + "\n\n" + proc.stdout + proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not out.is_file():
        run.fail(f"PrusaSlicer exited {proc.returncode}; see slicer.log")

    inserted = insert_colour_changes(out, change_zs)
    for z, at in inserted:
        run.log(f"slice: M600 inserted after layer top z={z} (before layer z={at})")
    run.step("slice", cmd=cmd, profile=profile.id, first_layer=first, layer_height=layer_h, gcode=str(out),
             colour_changes=[{"after_z": z, "before_layer_z": at} for z, at in inserted])
    return out, cmd, [at for _z, at in inserted]


def _read_stl(path: Path):
    data = path.read_bytes()
    n = struct.unpack("<I", data[80:84])[0]
    return data[:80], [struct.unpack("<12fH", data[84 + i * 50:134 + i * 50]) for i in range(n)]



def layout_bed(run, printer, stls: list[Path], spacing_mm: float) -> list[Path]:
    """Stack the plates front to back, each centred in X, the whole block centred on the bed.
    Writes translated copies to <run>/bed/ and returns them; fails if the block doesn't fit."""
    (bx0, by0), (bx1, by1) = printer.bed_min, printer.bed_max
    meshes = []
    for s in stls:
        header, tris = _read_stl(s)
        xs = [v for t in tris for v in (t[3], t[6], t[9])]
        ys = [v for t in tris for v in (t[4], t[7], t[10])]
        meshes.append((s, header, tris, min(xs), max(xs), min(ys), max(ys)))
    widest = max(m[4] - m[3] for m in meshes)
    depth = sum(m[6] - m[5] for m in meshes) + spacing_mm * (len(meshes) - 1)
    if widest > bx1 - bx0 or depth > by1 - by0:
        run.fail(f"{len(stls)} plates need {widest:.0f} x {depth:.0f} mm; bed is {bx1 - bx0:.0f} x {by1 - by0:.0f} mm")
    cx, cy = printer.bed_center
    y = cy + depth / 2   # first plate at the back
    placed = []
    for s, header, tris, x0, x1, y0, y1 in meshes:
        dx = cx - (x0 + x1) / 2
        dy = (y - (y1 - y0)) - y0
        moved = [t[:3] + tuple(c + (dx if k % 3 == 0 else dy if k % 3 == 1 else 0.0)
                               for k, c in enumerate(t[3:12])) + t[12:] for t in tris]
        run.log(f"bed: {s.stem} at X {x0 + dx:.1f}-{x1 + dx:.1f}, Y {y0 + dy:.1f}-{y1 + dy:.1f}")
        placed.append((s.stem, moved))
        y -= (y1 - y0) + spacing_mm
    # one 3MF, one object per plate: the CLI slices several STL arguments as separate jobs (last one wins),
    # and --merge would fuse the plates into a single object
    bed = run.path("bed.3mf")
    _write_3mf(bed, placed)
    return [bed]


def _write_3mf(path: Path, objects: list) -> None:
    """Minimal 3MF: each (name, triangles) becomes its own object and build item at the identity transform."""
    res, items = [], []
    for oid, (name, tris) in enumerate(objects, start=1):
        index, verts, faces = {}, [], []
        for t in tris:
            ids = []
            for v in (t[3:6], t[6:9], t[9:12]):
                key = tuple(round(c, 5) for c in v)
                if key not in index:
                    index[key] = len(verts)
                    verts.append(key)
                ids.append(index[key])
            faces.append(ids)
        vx = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in verts)
        tx = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
        res.append(f'<object id="{oid}" name="{name}" type="model"><mesh><vertices>{vx}</vertices>'
                   f'<triangles>{tx}</triangles></mesh></object>')
        items.append(f'<item objectid="{oid}"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
             'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<resources>{"".join(res)}</resources><build>{"".join(items)}</build></model>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="rels" ContentType="application/'
                   'vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" '
                   'ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)


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
                # :g so 4 and 4.0 print the same (heights arrive as int from a job file, float from a maker)
                out.append(f";COLOR_CHANGE after z={pending[0]:g} (farm pipeline)\nM600\n")
                inserted.append((pending.pop(0), z))
        out.append(line)
        i += 1
    if pending:
        raise SystemExit(f"colour change heights {pending} are above the top of the print")
    gcode.write_text("".join(out), encoding="utf-8", newline="\n")
    return inserted
