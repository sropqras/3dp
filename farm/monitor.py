"""Serious-error monitor: one read-only pass over every printer in the registry, compared with the previous pass
(a JSON state file), reporting only what is NEW and what has RECOVERED, so a cron job can run it every few minutes
without repeating itself. Never sends G-code or touches a printer's state.

Serious = Klipper in error/shutdown (thermal runaway, MCU lost, ...), a print in the error state, Moonraker
unreachable on two passes running, or a new console error line: Klipper's "!!" errors and "Unknown command"
(the API answers ok for an unknown macro, so the console is the only place that shows it).
"""
import json
from pathlib import Path

from . import net, printers

Q = "webhooks=state,state_message&print_stats=state,filename,message&pause_resume=is_paused"
UNREACHABLE_PASSES = 2      # one missed poll is a Wi-Fi blip, two is a problem


def _console_errors(p, since: float | None) -> tuple[list[str], float]:
    store = net.get_json(f"{p.moonraker}/server/gcode_store?count=200", timeout=5)["result"]["gcode_store"]
    latest = max([since or 0.0] + [x["time"] for x in store])
    if since is None:
        return [], latest
    bad = [x["message"].strip() for x in store if x["time"] > since
           and (x["message"].startswith("!!") or "Unknown command" in x["message"])]
    return bad, latest


def check(p, prev: dict) -> tuple[dict, list[tuple[str, str]]]:
    """Returns (new state for this printer, [(level, message)]) with level 'loud' or 'silent'."""
    now = {"problems": {}, "misses": 0, "console_t": prev.get("console_t")}
    out = []
    link = f"\nFluidd: {p.fluidd}" + (f" ({p.fluidd_note})" if p.fluidd_note else "") if p.fluidd else ""
    try:
        s = net.get_json(f"{p.moonraker}/printer/objects/query?{Q}", timeout=5)["result"]["status"]
    except Exception as e:
        now["misses"] = prev.get("misses", 0) + 1
        now["problems"] = dict(prev.get("problems", {}))
        if now["misses"] >= UNREACHABLE_PASSES:
            now["problems"]["unreachable"] = f"Moonraker unreachable: {str(e)[:120]}"
    else:
        wh, ps = s["webhooks"], s["print_stats"]
        if wh["state"] in ("error", "shutdown"):
            now["problems"]["klipper"] = f"Klipper {wh['state']}: {wh['state_message'].strip().splitlines()[0][:200]}"
        if ps["state"] == "error":
            now["problems"]["print"] = f"print error ({ps['filename']}): {ps.get('message', '')[:200]}"
        try:
            # first pass starts at the newest stored line (printer-host clock, not ours), so old history is never
            # reported as new
            lines, now["console_t"] = _console_errors(p, now["console_t"])
            if lines:
                more = f"\n(+{len(lines) - 5} more)" if len(lines) > 5 else ""
                out.append(("loud", f"🟠 {p.name}: console error\n" + "\n".join(l[:200] for l in lines[:5]) + more + link))
        except Exception:
            pass
    old = prev.get("problems", {})
    for key, text in now["problems"].items():
        if old.get(key) != text:
            out.append(("loud", f"🔴 {p.name}: {text}{link}"))
    for key in old.keys() - now["problems"].keys():
        out.append(("silent", f"✅ {p.name}: recovered ({old[key][:80]})"))
    return now, out


def run(state_file: Path) -> list[tuple[str, str]]:
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    alerts = []
    for p in printers.PRINTERS.values():
        state[p.name], found = check(p, state.get(p.name, {}))
        alerts += found
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2))
    return alerts
