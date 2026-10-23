"""Headless check of the Printers tab against a fake Moonraker only: snapshot, stage a change, apply, roll back.
    python3 tests/ui_printers.py   -> prints READY <url>; tests/ui_printers.cjs drives it."""
import copy, json, os, subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.fakes import FakeMoonraker  # noqa: E402
from tests.test_audit_model3mf import GOOD  # noqa: E402
from tests.test_cfgchange import PRINTER_CFG  # noqa: E402

tmp = Path(tempfile.mkdtemp())
m = FakeMoonraker(copy.deepcopy(GOOD))
m.config = {"printer.cfg": PRINTER_CFG, "macros.cfg": b"[gcode_macro M600]\ngcode:\n  PAUSE\n"}
m.settings_from_config = True   # the audit reads the config text, so a generated fix really changes it
port = m.server.server_address[1]
(tmp / "hosts.json").write_text(json.dumps({"hosts": [{"name": "fakehost", "address": "127.0.0.1", "ports": [port]}]}))
env = dict(os.environ, FARM_DB=str(tmp / "farm.db"), FARM_RUNS_DIR=str(tmp / "runs"), FARM_HOSTS_FILE=str(tmp / "hosts.json"),
           FARM_MOONRAKER_OVERRIDE=json.dumps({"Ender3MaxNeo": m.url}))
code = ("import sys; from farm import configs, cfgchange; from pathlib import Path; configs.STORE = Path(sys.argv[1]); "
        "cfgchange.FARM_MD = Path(sys.argv[2]); cfgchange.SETTLE_S = 0; cfgchange.POLL_S = 0.1; "
        "from farm import web; web.serve('127.0.0.1', 10197)")
(tmp / "FARM.md").write_text("| Date | Printer | Change |\n|---|---|---|\n")
srv = subprocess.Popen([sys.executable, "-c", code, str(tmp / "snap"), str(tmp / "FARM.md")],
                       cwd=Path(__file__).resolve().parent.parent, env=env)
time.sleep(1.5)
print(f"READY http://127.0.0.1:10197", flush=True)
try:
    while True:
        time.sleep(1)
        Path(tmp / "state.json").write_text(json.dumps({"restarts": m.restarts, "printer_cfg": m.config["printer.cfg"].decode()}))
        print("STATE " + json.dumps({"restarts": m.restarts, "max_velocity_line": [l for l in m.config["printer.cfg"].decode().splitlines() if l.startswith("max_velocity")]}), flush=True)
except KeyboardInterrupt:
    pass
finally:
    srv.terminate()
