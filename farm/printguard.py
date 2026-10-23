"""PrintGuard as the farm sees it: print-failure detection by a local vision model, read-only from here.

PrintGuard runs in its own container on 3dpnas (`~/agent_workspace/infrastructure/printguard/`). CLAUDE.md allows it as
a local vision model that sends nothing off the LAN; `tests/test_no_ai.py` checks FARM_PRINTGUARD_URL stays local.
Its monitors are alert-only. The farm only READS its REST API (`/api/v1`, the read scope): it never adds printers,
changes monitors or sends a printer command through it, and prints start only through farm/gate.py.

status()       one read of PrintGuard, grouped per farm printer (matched by Moonraker URL), plus its recent alerts
alerts_since() the alert events newer than a timestamp, for the monitor to tell once
"""
import json
import urllib.parse
import urllib.request

from . import config, net, printers

ACTIVE = ("printing",)      # PrintGuard's device status while a print runs (paused/idle/error put a monitor on standby)


def _get(path: str, timeout: float = 5):
    req = urllib.request.Request(config.PRINTGUARD_URL.rstrip("/") + "/api/v1" + path)
    if config.PRINTGUARD_TOKEN:        # once any token is issued in PrintGuard, every read needs one
        req.add_header("Authorization", f"Bearer {config.PRINTGUARD_TOKEN}")
    _, _, body = net._send(req, timeout)
    return json.loads(body)


def _key(url: str) -> str:
    u = urllib.parse.urlparse(url or "")
    return f"{(u.hostname or '').lower()}:{u.port or 80}"


def status() -> dict:
    """{} when PrintGuard isn't configured; {"error", "ui"} when it can't be read; otherwise
    {"ui", "printers": {name: {monitor, enabled, watching, status, score, score_ts, alert, camera_online, camera_reason}},
     "events": [...]}. Names are farm registry names where the Moonraker URL matches, else PrintGuard's own."""
    if not config.PRINTGUARD_URL:
        return {}
    ui = config.PRINTGUARD_URL.rstrip("/")
    try:
        pg_printers, monitors, cameras, events = _get("/printers"), _get("/monitors"), _get("/cameras"), _get("/events")
    except Exception as e:
        msg = str(e)
        if "HTTP 401" in msg:
            msg = "PrintGuard wants a token now: put a read token in FARM_PRINTGUARD_TOKEN"
        return {"error": msg[:200], "ui": ui}
    farm = {_key(p.moonraker): p.name for p in printers.PRINTERS.values()}
    named = {p["id"]: farm.get(_key(p.get("config", {}).get("base_url", ""))) or p["name"] for p in pg_printers}
    state = {p["id"]: (p.get("device_state") or {}).get("status") for p in pg_printers}
    cams = {c["id"]: c for c in cameras}
    out = {}
    for m in monitors:
        name = named.get(m.get("printer_id"))
        if not name:
            continue
        cam, result = cams.get(m.get("camera_id")) or {}, m.get("result") or {}
        out[name] = {"monitor": m["id"], "enabled": m.get("enabled"), "watching": m.get("watching"),
                     "status": state.get(m.get("printer_id")), "on_defect": m.get("on_defect"),
                     "score": result.get("score"), "score_ts": result.get("ts"), "alert": m.get("alert"),
                     "threshold": m.get("threshold"),
                     "camera_online": cam.get("online"), "camera_reason": cam.get("reason")}
    by_monitor = {m["id"]: named.get(m.get("printer_id")) or m.get("name") for m in monitors}
    evs = [{**e, "printer": by_monitor.get(e.get("monitor_id"))} for e in events if isinstance(e, dict)]
    return {"ui": ui, "printers": out, "events": evs}


def alerts_since(st: dict, last: dict) -> tuple[list[dict], dict]:
    """The alerts newer than what was told, per monitor. `last` is {monitor_id: ts told}; returns (new, updated last).
    An alert stays on its monitor until a clean frame, so the monitor's own alert counts too: one that cleared between
    two passes is still in the events, one the events tail has dropped is still on the monitor."""
    seen = dict(last)
    found = {}
    for e in st.get("events", []):
        if e.get("event") == "alert" and e.get("monitor_id") and e.get("ts"):
            found.setdefault(e["monitor_id"], []).append({"printer": e.get("printer"), "score": e.get("score"),
                                                          "action": e.get("action"), "ts": e["ts"]})
    for name, v in st.get("printers", {}).items():
        a = v.get("alert")
        if a and a.get("ts"):
            found.setdefault(v["monitor"], []).append({"printer": name, **a})
    new = []
    for mid, items in found.items():
        newest = max(items, key=lambda a: a["ts"])
        if newest["ts"] > seen.get(mid, 0):
            new.append({**newest, "monitor": mid})
            seen[mid] = newest["ts"]
    return sorted(new, key=lambda a: a["ts"]), seen
