"""Stage 4: upload to the printer's Moonraker and (only when asked) start the print."""
import urllib.parse
from pathlib import Path

from . import net

STARTABLE = {"standby", "complete", "cancelled", "error"}


def status(printer) -> dict:
    q = "print_stats=state,filename&webhooks=state&pause_resume=is_paused&extruder=temperature&heater_bed=temperature"
    s = net.get_json(f"{printer.moonraker}/printer/objects/query?{q}")["result"]["status"]
    return {"klipper": s["webhooks"]["state"], "job": s["print_stats"]["state"], "file": s["print_stats"]["filename"],
            "paused": s["pause_resume"]["is_paused"], "nozzle": round(s["extruder"]["temperature"], 1),
            "bed": round(s["heater_bed"]["temperature"], 1)}


def upload(run, printer, gcode: Path, folder: str) -> str:
    run.log(f"upload: {gcode.name} -> {printer.name} gcodes/{folder}/")
    _, _, body = net.post_multipart(f"{printer.moonraker}/server/files/upload",
                                    {"root": "gcodes", "path": folder}, {"file": gcode})
    remote = f"{folder}/{gcode.name}"
    run.step("upload", printer=printer.name, remote=remote, response=body.decode(errors="replace")[:500])
    return remote


def start(run, printer, remote: str, *, token=None) -> None:
    """Only farm/gate.py may call this (it passes its token after the full Start checklist)."""
    from .gate import _Token
    if not isinstance(token, _Token):
        raise PermissionError("prints are started through farm/gate.py (the Start button / farm start), not directly")
    if printer.readonly:
        raise PermissionError(f"{printer.name} is watch-only")
    st = status(printer)
    run.log(f"printer: {st}")
    if st["klipper"] != "ready" or st["job"] not in STARTABLE or st["paused"]:
        run.fail(f"{printer.name} is not idle ({st}); not starting")
    net.post_json(f"{printer.moonraker}/printer/print/start?filename={urllib.parse.quote(remote)}")
    run.log(f"start: {remote} started on {printer.name}")
    run.step("start", printer=printer.name, remote=remote, status_before=st)
