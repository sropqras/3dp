"""Host-specific settings. Defaults suit this Windows workstation; override with env vars on other hosts
(e.g. aali-cli: FARM_PRUSASLICER="flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer")."""
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
