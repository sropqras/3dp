"""Printer registry. Hardware facts here must match FARM.md - don't add a printer without confirming its hardware."""
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PRUSA_DIR = REPO / "prusa"


@dataclass(frozen=True)
class Printer:
    name: str
    moonraker: str            # base URL of the instance's Moonraker
    bed_min: tuple            # usable XY area (matches the slicer bed_shape)
    bed_max: tuple
    max_z: float
    has_m600_prompt: bool     # guided colour change installed (FARM.md feature)
    bundle: Path              # PrusaSlicer ini holding the presets below
    printer_preset: str
    print_preset: str
    filament_presets: dict    # material key -> preset name
    readonly: bool = False    # watch-only: shown in Farm Control and audited, never sent jobs
    fluidd: str = ""          # Fluidd web UI for this printer (IP, not .local: phones on Tailscale can't resolve mDNS)
    fluidd_note: str = ""     # Fluidd can't pick an instance from the URL; say which one to choose when it's shared

    @property
    def bed_center(self):
        return ((self.bed_min[0] + self.bed_max[0]) / 2, (self.bed_min[1] + self.bed_max[1]) / 2)


PRINTERS = {
    "Ender3MaxNeo": Printer(
        name="Ender3MaxNeo",
        moonraker="http://192.168.1.184:7125",
        fluidd="http://192.168.1.184/",       # enders3dp's one Fluidd; with no saved choice it opens this instance
        bed_min=(5.0, 5.0),
        bed_max=(295.0, 295.0),
        max_z=300.0,
        has_m600_prompt=True,
        bundle=PRUSA_DIR / "MaxNeo_PETG_0.6_profiles.ini",
        printer_preset="Ender3MaxNeo (S1Plus HW, Sprite Pro 0.6) - PETG",
        print_preset="0.36 mm CHUNKY PETG (0.6 mm nozzle) @MaxNeo 13mm3",
        filament_presets={"petg": "Creality PETG @240_80 - Sprite 0.6 13mm3"},
    ),
    # Ender 3 S1 Plus on 3dpnas: same bed and Sprite Pro 0.6 hotend as the MaxNeo (user: G-code is shared
    # between them), so it uses the MaxNeo presets. Has a filament sensor; Klipper v0.13.
    "enders1plus": Printer(
        name="enders1plus",
        moonraker="http://3dpnas.local:7125",
        fluidd="http://192.168.1.68/",        # 3dpnas
        bed_min=(5.0, 5.0),
        bed_max=(295.0, 295.0),
        max_z=300.0,
        has_m600_prompt=True,    # guided M600 deployed 2026-10-22 (FARM.md)
        bundle=PRUSA_DIR / "MaxNeo_PETG_0.6_profiles.ini",
        printer_preset="Ender3MaxNeo (S1Plus HW, Sprite Pro 0.6) - PETG",
        print_preset="0.36 mm CHUNKY PETG (0.6 mm nozzle) @MaxNeo 13mm3",
        filament_presets={"petg": "Creality PETG @240_80 - Sprite 0.6 13mm3"},
    ),
    # Ender 3 V3 SE on enders3dp: the user's own production printer - WATCH ONLY (no jobs, no config changes)
    # until they hand it over. Hardware mostly unconfirmed (FARM.md); filament sensor + Meross plug confirmed.
    "EnderV3SE": Printer(
        name="EnderV3SE",
        moonraker="http://192.168.1.184:7128",
        fluidd="http://192.168.1.184/",
        fluidd_note="shared Fluidd: choose EnderV3SE (:7128) in its printer menu",
        bed_min=(5.0, 5.0),
        bed_max=(215.0, 215.0),
        max_z=250.0,
        has_m600_prompt=False,
        bundle=PRUSA_DIR / "PrusaSlicer_config_bundle.ini",
        printer_preset="Ender 3 V3 SE (Klipper)",
        print_preset="",
        filament_presets={},
        readonly=True,
    ),
}


def get(name: str) -> Printer:
    try:
        return PRINTERS[name]
    except KeyError:
        raise SystemExit(f"unknown printer {name!r}; known: {', '.join(PRINTERS)}")
