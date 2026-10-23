"""Host-specific settings. Defaults suit aali-cli, where the farm runs (the Flathub PrusaSlicer 2.9.6, the makers
on this host); the Windows workstation keeps its own via os.name. Every value is overridable with an env var."""
import os
import shlex
from pathlib import Path

from .printers import REPO

_DEFAULT_SLICER = (r"C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe" if os.name == "nt"
                   else "flatpak run --command=prusa-slicer com.prusa3d.PrusaSlicer")


def slicer_cmd() -> list[str]:
    raw = os.environ.get("FARM_PRUSASLICER")
    if raw:
        return shlex.split(raw, posix=os.name != "nt")
    return [_DEFAULT_SLICER] if os.name == "nt" else shlex.split(_DEFAULT_SLICER)


LIKHAN_URL = os.environ.get("FARM_LIKHAN_URL", "http://192.168.1.107:4111" if os.name == "nt" else "http://127.0.0.1:4111")
FONTS_DIR = Path(os.environ.get("FARM_FONTS_DIR", REPO / "assets" / "fonts"))
# who Farm Control records with an action when the page hasn't been told otherwise (the "You" link changes it per
# browser). A name the farm uses, never a personal detail.
OPERATOR = os.environ.get("FARM_OPERATOR", "Ironman").strip() or "Ironman"
# PrintGuard: local print-failure detection (a vision model in its own container on 3dpnas). Empty = not used.
# Must stay on the LAN/Tailscale (tests/test_no_ai.py). The token is only needed once one is issued in PrintGuard:
# from then on every read wants one. It's a secret: env only, never in farm.conf or git.
PRINTGUARD_URL = os.environ.get("FARM_PRINTGUARD_URL", "").strip()
PRINTGUARD_TOKEN = os.environ.get("FARM_PRINTGUARD_TOKEN", "").strip()

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
