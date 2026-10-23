"""Print profiles: a job picks a profile + layer height + printer, instead of every printer having one preset.

A profile is a reviewed combination of PrusaSlicer printer/print/filament presets that is known to pass the
farm checks (START_PRINT placeholders, no auto-emitted temperatures, a real volumetric cap). `printers` lists
every printer that can run the SAME G-code, so a queued build can move between them without re-slicing.

`confirmed=False` marks a profile whose values are still a proposal: it slices for review, but production
(approve/start) refuses it until the user confirms and the flag is flipped here.
"""
from dataclasses import dataclass, field
from pathlib import Path

from . import printers
from .printers import PRUSA_DIR

FARM_BUNDLE = PRUSA_DIR / "farm_profiles.ini"


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    base_printers: tuple      # printers named in code; the registry adds more (printers/registry.json "profiles")
    nozzle: float
    bundle: Path
    printer_preset: str
    print_preset: str
    filament_preset: str
    material: str
    layer_heights: tuple            # choices offered for this profile
    max_volumetric: float           # validate refuses a filament cap above this
    first_layer: tuple = (0.25, 0.45)   # window for align_first_layer (0.6 nozzle)
    supports: bool = False
    confirmed: bool = True
    note: str = ""
    tags: tuple = field(default_factory=tuple)

    @property
    def printers(self) -> tuple:
        """Every printer that may use this profile: named here, or listing it in printers/registry.json."""
        reg = tuple(n for n, p in printers.PRINTERS.items() if self.id in p.profiles and n not in self.base_printers)
        return tuple(self.base_printers) + reg

    def allows(self, printer_name: str) -> bool:
        return printer_name in self.printers


_NEO = printers.PRINTERS["Ender3MaxNeo"]

PROFILES = {p.id: p for p in (
    # Today's production profile (grave plates). Must stay byte-identical: tests/golden.py proves it.
    Profile(
        id="neo-petg-036", name="PETG 0.6 chunky (13 mm³/s)", base_printers=("Ender3MaxNeo", "enders1plus"),
        nozzle=0.6, bundle=_NEO.bundle, printer_preset=_NEO.printer_preset, print_preset=_NEO.print_preset,
        filament_preset=_NEO.filament_presets["petg"], material="petg",
        layer_heights=(0.28, 0.32, 0.36, 0.4, 0.44), max_volumetric=13, tags=("plates", "parts")),
    # S1 Pro (0.4 nozzle): the user's own S1 Pro 0.4 preset with START_PRINT placeholders + autoemit off, the farm's
    # proven Sprite PETG (13 mm³/s), the user's 0.20 NORMAL SPEEDY GON print preset.
    Profile(
        id="s1pro-petg-020", name="PETG 0.4 (S1 Pro)", base_printers=("s1pro",), nozzle=0.4,
        bundle=FARM_BUNDLE, printer_preset="Farm S1 Pro 0.4", print_preset="Farm 0.20 PETG 0.4 (S1 Pro)",
        filament_preset="Farm PETG 0.4 (S1 Pro)", material="petg",
        layer_heights=(0.16, 0.2, 0.24, 0.28), max_volumetric=13, first_layer=(0.15, 0.3),
        tags=("plates", "parts")),
    # Large lithophanes / lamp shades in Hyper PLA on the 0.6 Sprites (the user has printed these).
    Profile(
        id="neo-hyperpla-028-litho", name="Hyper PLA 0.6 lithophane", base_printers=("Ender3MaxNeo", "enders1plus"),
        nozzle=0.6, bundle=FARM_BUNDLE, printer_preset="Farm Sprite 0.6 (MaxNeo + S1 Plus)",
        print_preset="Farm 0.28 Litho 0.6", filament_preset="Farm Hyper PLA 0.6", material="pla",
        layer_heights=(0.2, 0.24, 0.28, 0.32), max_volumetric=15, confirmed=False,
        note="Hyper PLA volumetric cap 15 mm³/s is a proposal; confirm before production",
        tags=("lithophane", "lamp", "decor")),
)}


def check(profile: Profile) -> list[str]:
    """Static checks of a profile's presets, so a new profile can't reach a printer with a known-bad setting."""
    from .slicer import CLI_UNSAFE   # local: slicer imports this module
    import re
    problems = []
    text = profile.bundle.read_text(encoding="utf-8") if profile.bundle.is_file() else ""
    sections = {}
    for kind, name in (("printer", profile.printer_preset), ("print", profile.print_preset),
                       ("filament", profile.filament_preset)):
        m = re.search(r"^\[" + re.escape(f"{kind}:{name}") + r"\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
        if not m:
            problems.append(f"[{kind}:{name}] not in {profile.bundle.name}")
        sections[kind] = dict(re.findall(r"^(\w+) = (.*)$", m.group(1), re.M)) if m else {}
    pr, fi = sections["printer"], sections["filament"]
    if pr:
        if pr.get("autoemit_temperature_commands") != "0":
            problems.append("printer preset auto-emits temperatures (M104/M140 before START_PRINT)")
        start = pr.get("start_gcode", "")
        for ph in ("{first_layer_temperature[0]}", "{first_layer_bed_temperature[0]}"):
            if ph not in start:
                problems.append(f"START_PRINT doesn't pass {ph} (literal temperatures ignore the filament)")
        nz = [float(x) for x in pr.get("nozzle_diameter", "0").split(",")]
        if nz[0] != profile.nozzle:
            problems.append(f"preset nozzle {nz[0]} != profile nozzle {profile.nozzle}")
        bed = [tuple(float(v) for v in p.split("x")) for p in pr.get("bed_shape", "").split(",") if "x" in p]
        for name in profile.printers:
            hw = printers.PRINTERS.get(name)
            if hw is None:
                problems.append(f"printer {name} is not in printers/registry.json")
                continue
            (x0, y0), (x1, y1) = hw.bed_min, hw.bed_max
            if not bed or any(not (x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6) for x, y in bed):
                problems.append(f"bed_shape {pr.get('bed_shape')} is outside {name}'s usable bed {hw.bed_min}-{hw.bed_max}")
    if fi:
        cap = float(fi.get("filament_max_volumetric_speed", "0") or 0)
        if not 0 < cap <= profile.max_volumetric:
            problems.append(f"filament volumetric cap {cap} not in (0, {profile.max_volumetric}]")
    return problems


def get(profile_id: str) -> Profile:
    try:
        return PROFILES[profile_id]
    except KeyError:
        raise SystemExit(f"unknown profile {profile_id!r}; known: {', '.join(PROFILES)}")


def for_printer(printer_name: str) -> list[Profile]:
    return [p for p in PROFILES.values() if p.allows(printer_name)]


def legacy(printer, material: str) -> Profile:
    """Jobs written before profiles existed name a printer + material; map them to the profile that slices
    exactly as before (the printer's own presets)."""
    for p in PROFILES.values():
        if (p.allows(printer.name) and p.material == material and p.bundle == printer.bundle
                and p.print_preset == printer.print_preset
                and p.filament_preset == printer.filament_presets.get(material)):
            return p
    raise SystemExit(f"no profile for {printer.name} + {material}; add one to farm/profiles.py")
