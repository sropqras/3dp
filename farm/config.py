"""Host-specific settings. Defaults suit this Windows workstation; override with env vars on other hosts
(e.g. aali-cli: FARM_PRUSASLICER="flatpak run --command=prusa-slicer com.prusa3d.PrusaSlicer")."""
import os
import shlex
from pathlib import Path

from .printers import REPO

_DEFAULT_SLICER = r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe"


def slicer_cmd() -> list[str]:
    raw = os.environ.get("FARM_PRUSASLICER")
    if raw:
        return shlex.split(raw, posix=os.name != "nt")
    return [_DEFAULT_SLICER]


LIKHAN_URL = os.environ.get("FARM_LIKHAN_URL", "http://192.168.1.107:4111")
FONTS_DIR = Path(os.environ.get("FARM_FONTS_DIR", REPO / "assets" / "fonts"))

# The maker apps on aali-cli (x111 ports, SERVER.md). One env var each, so another host can point elsewhere.
MAKER_URLS = {
    name: os.environ.get(f"FARM_MAKER_{name.upper()}_URL", default)
    for name, default in {
        "likhan": LIKHAN_URL,
        "lithophane": "http://127.0.0.1:5111",
        "namakdani": "http://127.0.0.1:6111",
        "lamp": "http://127.0.0.1:8111",
        "holder": "http://127.0.0.1:9111",
    }.items()
}
# production G-code is only ever sliced with this PrusaSlicer (presets, CLI quirks and goldens are tied to it)
PRODUCTION_SLICER = "2.9.6"
