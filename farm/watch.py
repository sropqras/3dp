"""Follow a started run on its printer and stop at the next milestone, logging to <run>/monitor.log.

Milestones are found from the run's own G-code (byte offsets of layer 2 and of each colour change),
compared with Moonraker's virtual_sdcard.file_position, so homing/meshing moves can't fool them.
"""
import time
from pathlib import Path

from . import net

QUERY = ("print_stats=state,print_duration&virtual_sdcard=file_position&pause_resume=is_paused"
         "&webhooks=state&gcode_move=gcode_position&extruder=temperature&heater_bed=temperature")


def milestones(gcode: Path) -> dict:
    data = gcode.read_bytes()
    marks = {"size": len(data)}
    second = data.find(b";LAYER_CHANGE\n", data.find(b";LAYER_CHANGE\n") + 1)
    marks["first_layer_done"] = second
    marks["colour_changes"] = []
    i = data.find(b";COLOR_CHANGE")
    while i != -1:
        marks["colour_changes"].append(i)
        i = data.find(b";COLOR_CHANGE", i + 1)
    return marks


def watch(run, printer, gcode: Path, interval: float = 20, until: str = "next") -> str:
    marks = milestones(gcode)
    log = open(run.path("monitor.log"), "a", encoding="utf-8")
    seen_first = False
    while True:
        try:
            s = net.get_json(f"{printer.moonraker}/printer/objects/query?{QUERY}")["result"]["status"]
        except Exception as e:  # network blip: keep watching
            log.write(f"{time.strftime('%H:%M:%S')} query failed: {e}\n")
            time.sleep(interval)
            continue
        pos = s["virtual_sdcard"]["file_position"]
        state = s["print_stats"]["state"]
        line = (f"klipper={s['webhooks']['state']} job={state} paused={s['pause_resume']['is_paused']} "
                f"z={s['gcode_move']['gcode_position'][2]:.2f} file={100 * pos / marks['size']:.1f}% "
                f"T={s['extruder']['temperature']:.0f} B={s['heater_bed']['temperature']:.0f} "
                f"t={s['print_stats']['print_duration'] / 60:.1f}min")
        log.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        log.flush()

        if s["webhooks"]["state"] != "ready":
            return f"KLIPPER {s['webhooks']['state'].upper()}: {line}"
        if state in ("complete", "cancelled", "error"):
            return f"JOB {state.upper()}: {line}"
        if s["pause_resume"]["is_paused"]:
            at_change = any(abs(pos - c) < 4096 for c in marks["colour_changes"])
            return f"{'COLOUR CHANGE' if at_change else 'PAUSED (not at a colour change!)'}: {line}"
        if not seen_first and state == "printing" and pos > marks["first_layer_done"] > 0:
            seen_first = True
            if until == "next":
                return f"FIRST LAYER DONE: {line}"
        time.sleep(interval)
