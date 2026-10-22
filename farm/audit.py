"""Farm baseline audit: check each printer's LIVE Klipper config (via Moonraker) against the rules learned on the
farm, so a lesson from one printer is checked on every printer instead of rediscovered. Read-only.

Add a rule whenever a problem is found on any printer; every rule says why it exists.
"""
import re

from . import net

# Motion limits that apply because of the farm's hardware (heavy 300 mm beds on V-wheels)
MAX_VELOCITY = 200          # KAMP's purge travel runs at max_velocity; 300 slammed the bed wheels (MaxNeo + S1 Plus)
MAX_ACCEL = 3000            # ~80 % of the Y shaper limit on the 35 Hz Y axes
MIN_RECOVER_VELOCITY = 100  # slow returns from the colour-change park ooze rings across the plate


def _settings(printer) -> dict:
    q = net.get_json(f"{printer.moonraker}/printer/objects/query?configfile=settings")
    return q["result"]["status"]["configfile"]["settings"]


def _gcode(s: dict, macro: str) -> str:
    return (s.get(f"gcode_macro {macro.lower()}") or {}).get("gcode", "")


def _code_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def rules(s: dict, printer) -> list[tuple]:
    """(status, rule, detail) for each check; status is PASS, FAIL or WARN."""
    out = []

    def check(ok, rule, detail, warn=False):
        out.append(("PASS" if ok else ("WARN" if warn else "FAIL"), rule, detail))

    pr = s.get("printer", {})
    check(pr.get("max_velocity", 999) <= MAX_VELOCITY, f"max_velocity <= {MAX_VELOCITY}",
          f"{pr.get('max_velocity')} (KAMP purge travel uses it)")
    check(pr.get("max_accel", 99999) <= MAX_ACCEL, f"max_accel <= {MAX_ACCEL}", f"{pr.get('max_accel')}")

    rv = s.get("pause_resume", {}).get("recover_velocity", 50.0)
    check(rv >= MIN_RECOVER_VELOCITY, f"pause_resume recover_velocity >= {MIN_RECOVER_VELOCITY}",
          f"{rv} (slow return oozes across the plate)")

    start = _code_lines(_gcode(s, "START_PRINT"))
    check(bool(start), "START_PRINT macro exists", "pipeline G-code calls START_PRINT EXTRUDER_TEMP= BED_TEMP=")
    raw_start = _gcode(s, "START_PRINT")
    check("params.EXTRUDER_TEMP" in raw_start and "params.BED_TEMP" in raw_start,
          "START_PRINT reads EXTRUDER_TEMP and BED_TEMP",
          "farm profiles pass the filament's temperatures as START_PRINT parameters")
    check("CLEAR_PAUSE" in start, "START_PRINT runs CLEAR_PAUSE",
          "stale pause made PAUSE answer 'already paused' and the print snapped back (MaxNeo)")

    m600 = _code_lines(_gcode(s, "M600"))
    check(bool(m600), "M600 macro exists", "slicer colour changes emit M600")
    check(not any(l.startswith("UNLOAD_FILAMENT") for l in m600), "M600 does not auto-unload",
          "an unattended eject lets the filament end tangle on the spool")
    prompt = [l for l in m600 if "PROMPT" in l]
    check(bool(prompt), "M600 shows the colour-change prompt", ", ".join(prompt) or "no prompt macro called")

    prompt_code = " ".join(_gcode(s, l.split()[0]) for l in prompt)
    check("PREFIX=tgalarm" in prompt_code, "colour change alerts Telegram (tgalarm)",
          "a waiting colour change must reach a phone, not just the console")

    bad_names = [k[len("gcode_macro "):] for k in s if k.startswith("gcode_macro ")
                 and re.match(r"^_?[a-z]\d", k[len("gcode_macro "):])
                 and not re.match(r"^[gm]\d+(\.\d+)?$", k[len("gcode_macro "):])]
    check(not bad_names, "no macro named letter+digits (Klipper parses them as classic commands)",
          ", ".join(bad_names) or "none")

    for key, sec in s.items():
        if key.startswith(("filament_switch_sensor", "filament_motion_sensor")):
            ins = _code_lines(sec.get("insert_gcode", "") or "")
            check(not any(l.startswith(("RESUME", "M24")) for l in ins), f"{key}: insert does not resume",
                  "inserting filament mid-swap resumed before load/purge (S1 Plus)")

    idle = s.get("idle_timeout", {})
    t = idle.get("timeout", 600)
    check(t <= 4 * 3600, "idle_timeout is finite (<= 4 h)", f"{t:.0f} s (never = heaters can stay on)")
    check("is_paused" in idle.get("gcode", ""), "idle timeout is pause-aware",
          "while paused only the nozzle may cool; bed + motors must hold the print")
    check("delayed_gcode _pause_hard_stop" in s, "12 h pause safety cap exists", "_PAUSE_HARD_STOP")

    if printer.has_m600_prompt is False and m600:
        check(False, "pipeline registry matches", "printer has an M600 but has_m600_prompt=False", warn=True)
    return out


def audit(printers) -> int:
    fails = 0
    for p in printers:
        print(f"== {p.name} ({p.moonraker})")
        try:
            s = _settings(p)
        except Exception as e:
            print(f"   UNREACHABLE: {e}")
            fails += 1
            continue
        for status, rule, detail in rules(s, p):
            if status != "PASS":
                fails += status == "FAIL"
            print(f"   {status:4} {rule} -- {detail}")
    return fails
