"""`python -m farm doctor`: is this host ready to produce? Slicer version, maker apps, licensed fonts.

Read-only. Never reads a font file's contents and never prints more than its name.
"""
import re
import subprocess

from . import config, net


def slicer_version() -> str | None:
    try:
        out = subprocess.run(config.slicer_cmd() + ["--help"], capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"PrusaSlicer-([\d.]+)", out)
    return m.group(1) if m else None


def production_ready() -> tuple[bool, str]:
    v = slicer_version()
    if v is None:
        return False, f"slicer not runnable: {' '.join(config.slicer_cmd())}"
    if v != config.PRODUCTION_SLICER:
        return False, f"PrusaSlicer {v}; production needs {config.PRODUCTION_SLICER} (Flathub)"
    return True, f"PrusaSlicer {v}"


def report() -> int:
    problems = 0
    ok, msg = production_ready()
    print(f"{'OK  ' if ok else 'WARN'} slicer: {msg}  [{' '.join(config.slicer_cmd())}]")
    problems += not ok
    for name, url in config.MAKER_URLS.items():
        try:
            net.get_json(f"{url}/health", timeout=5)
            print(f"OK   maker {name}: {url}")
        except Exception as e:
            print(f"FAIL maker {name}: {url} ({str(e)[:80]})")
            problems += 1
    fonts = sorted(p.name for p in config.FONTS_DIR.glob("*.[ot]tf")) if config.FONTS_DIR.is_dir() else []
    print(f"{'OK  ' if fonts else 'WARN'} fonts in {config.FONTS_DIR}: {', '.join(fonts) or 'none (licensed fonts are copied by hand)'}")
    problems += not fonts
    return problems
