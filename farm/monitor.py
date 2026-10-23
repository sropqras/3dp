"""Serious-error monitor: one read-only pass over every printer in the registry, compared with the previous pass
(a JSON state file), reporting only what is NEW and what has RECOVERED, so a cron job can run it every few minutes
without repeating itself. Never sends G-code or touches a printer's state.

Serious = Klipper in error/shutdown (thermal runaway, MCU lost, ...), a print in the error state, Moonraker
unreachable on two passes running, or a new console error line: Klipper's "!!" errors and "Unknown command"
(the API answers ok for an unknown macro, so the console is the only place that shows it).
"""
import re
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
        if powered_off(wh["state"], wh.get("state_message"), ps["state"]):
            now["off"] = True                 # switched off at the wall: a note, not an alarm
        elif wh["state"] in ("error", "shutdown"):
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
    if now.get("off") and not prev.get("off"):
        out.append(("silent", f"⚪ {p.name}: powered off (board not connected) - Reconnect in Farm Control after switching it on"))
    elif prev.get("off") and not now.get("off") and "unreachable" not in now["problems"]:
        out.append(("silent", f"🟢 {p.name}: board connected again"))
    old = prev.get("problems", {})
    for key, text in now["problems"].items():
        if old.get(key) != text:
            out.append(("loud", f"🔴 {p.name}: {text}{link}"))
    for key in old.keys() - now["problems"].keys():
        out.append(("silent", f"✅ {p.name}: recovered ({old[key][:80]})"))
    return now, out


def powered_off(klippy: str | None, message: str | None, job: str | None = None) -> bool:
    """Klipper couldn't reach the board at start-up and nothing was printing: the printer is switched off (or its
    USB is out), which the farm does to idle printers. Losing the board mid-print is NOT this - that stays loud."""
    return klippy == "error" and job not in ("printing", "paused") and bool(
        re.search(r"mcu '[^']*': Unable to connect", message or ""))


# ---------------------------------------------------------------- fleet health (Printers tab, phase 4)

DISK_PCT, CPU_TEMP_C = 90, 80
QUIET_SERVICES = ("backup-on-boot", "backup-filewatch")   # one-shot by design: "inactive" is normal


def health_problems(fleet: list[dict], maint_due: dict | None = None) -> dict:
    """{scope: {problem key: (level, text)}} from one fleet read (farm/hosts.py). Hosts: services, disk, CPU.
    Registry printers: failed components, Moonraker warnings, camera, maintenance due."""
    out = {}
    for v in fleet:
        if not v.get("reachable"):
            continue
        host = out.setdefault(f"host:{v['host']}", {})
        for name, st in (v.get("services") or {}).items():
            if st.startswith("active/running") or any(q in name for q in QUIET_SERVICES):
                continue
            core = name.startswith(("klipper", "moonraker")) and not any(x in name for x in ("telegram", "obico", "backup"))
            host[f"svc:{name}"] = ("loud" if core else "silent", f"{v['host']}: service {name} is {st}")
        if (v.get("disk_used_pct") or 0) > DISK_PCT:
            host["disk"] = ("loud", f"{v['host']}: disk {v['disk_used_pct']}% full ({v.get('disk_free_gb')} GB free)")
        if (v.get("cpu_temp") or 0) > CPU_TEMP_C:
            host["cpu_temp"] = ("loud", f"{v['host']}: CPU at {round(v['cpu_temp'])} C")
        if v.get("throttled"):
            host["throttled"] = ("loud", f"{v['host']}: throttled ({', '.join(v['throttled'])})")
        if not v.get("registry"):
            continue
        mine = out.setdefault(f"printer:{v['registry']}", {})
        for comp in v.get("failed_components") or []:
            mine[f"failed:{comp}"] = ("loud", f"{v['registry']}: Moonraker component {comp} failed")
        for i, w in enumerate(v.get("warnings") or []):
            mine[f"warning:{w[:60]}"] = ("silent", f"{v['registry']}: Moonraker warning: {w[:200]}")
        for c in v.get("cameras") or []:
            if c.get("ok") is False:
                mine[f"camera:{c.get('name')}"] = ("silent", f"{v['registry']}: camera {c.get('name')} gives no snapshot")
        for kind in (maint_due or {}).get(v["registry"], []):
            mine[f"maint:{kind}"] = ("silent", f"{v['registry']}: maintenance due - {kind}")
    return out


def diff_problems(prev: dict, now: dict) -> list[tuple[str, str]]:
    alerts = []
    for scope, probs in now.items():
        old = prev.get(scope, {})
        for key, (level, text) in probs.items():
            if key not in old:
                alerts.append((level, ("🔴 " if level == "loud" else "🟡 ") + text))
    for scope, old in prev.items():
        for key, (level, text) in old.items():
            if key not in now.get(scope, {}):
                alerts.append(("silent", f"✅ recovered: {text[:120]}"))
    return alerts


def health_pass(state: dict) -> list[tuple[str, str]]:
    from . import db, hosts, maintenance
    fl = hosts.fleet(cameras=True)
    due = {}
    try:
        con = db.connect()
        for v in fl:
            if v.get("registry") and v.get("print_hours") is not None:
                due[v["registry"]] = [s["kind"] for s in maintenance.status(con, v["registry"], v["print_hours"]) if s["due"]]
        con.close()
    except Exception:
        pass
    retired = {h["name"] for h in hosts.load_hosts() if h.get("retired")}
    now = health_problems([v for v in fl if v["host"] not in retired], due)
    prev = {k: {kk: tuple(vv) for kk, vv in v.items()} for k, v in state.get("_health", {}).items()}
    alerts = diff_problems(prev, now)
    state["_health"] = now
    return alerts


def run(state_file: Path, health: bool = True) -> list[tuple[str, str]]:
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    alerts = []
    for p in printers.PRINTERS.values():
        state[p.name], found = check(p, state.get(p.name, {}))
        alerts += found
    if health:
        try:
            alerts += health_pass(state)
        except Exception as e:   # the health pass is extra: it must never stop the print-error watch
            alerts.append(("silent", f"farm monitor: health pass failed: {type(e).__name__}: {str(e)[:120]}"))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2))
    return alerts
