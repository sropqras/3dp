"""Printer registry: printers/registry.json (edited from Farm Control's Printers tab, with a person's confirmation).
Hardware facts must match FARM.md - don't add a printer without confirming its hardware."""
import dataclasses
import json
import os
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
    profiles: tuple = ()      # print profiles (farm/profiles.py) this printer may use
    note: str = ""

    @property
    def bed_center(self):
        return ((self.bed_min[0] + self.bed_max[0]) / 2, (self.bed_min[1] + self.bed_max[1]) / 2)


REGISTRY = Path(os.environ.get("FARM_REGISTRY", REPO / "printers" / "registry.json"))
PRINTERS: dict = {}
_loaded = {"mtime": None}


def _from_json(e: dict) -> "Printer":
    lp = e.get("legacy_presets") or {}
    return Printer(
        name=e["name"], moonraker=e["moonraker"], bed_min=tuple(e["bed_min"]), bed_max=tuple(e["bed_max"]),
        max_z=float(e["max_z"]), has_m600_prompt=bool(e.get("has_m600_prompt")),
        bundle=PRUSA_DIR / lp.get("bundle", "farm_profiles.ini"), printer_preset=lp.get("printer", ""),
        print_preset=lp.get("print", ""), filament_presets=dict(lp.get("filament") or {}),
        readonly=bool(e.get("readonly")), fluidd=e.get("fluidd", ""), fluidd_note=e.get("fluidd_note", ""),
        profiles=tuple(e.get("profiles") or ()), note=e.get("note", ""))


def refresh(force: bool = False) -> bool:
    """(Re)load printers/registry.json when it changed - in place, so every `printers.PRINTERS` user sees it.
    The web server and the worker call this, so an onboarding saved in Farm Control takes effect without a restart."""
    try:
        mtime = REGISTRY.stat().st_mtime
    except OSError:
        return False
    if not force and mtime == _loaded["mtime"]:
        return False
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    fresh = {e["name"]: _from_json(e) for e in data["printers"]}
    for name, url in json.loads(os.environ.get("FARM_MOONRAKER_OVERRIDE", "{}")).items():
        if name in fresh:
            fresh[name] = dataclasses.replace(fresh[name], moonraker=url)
    PRINTERS.clear()
    PRINTERS.update(fresh)
    _loaded["mtime"] = mtime
    return True


def registry_entries() -> list[dict]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["printers"]


def save_registry(entries: list[dict]) -> None:
    data = json.loads(REGISTRY.read_text(encoding="utf-8"))
    data["printers"] = entries
    tmp = REGISTRY.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(REGISTRY)
    refresh(force=True)


refresh(force=True)


# Tests and the headless UI check point printers at a fake Moonraker with FARM_MOONRAKER_OVERRIDE
# ({"Ender3MaxNeo": "http://127.0.0.1:9999"}), applied in refresh(). Never set in production.


def get(name: str) -> Printer:
    try:
        return PRINTERS[name]
    except KeyError:
        raise SystemExit(f"unknown printer {name!r}; known: {', '.join(PRINTERS)}")
