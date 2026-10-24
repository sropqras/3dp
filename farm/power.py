"""Printer power at the wall: a Meross smart surge strip (MSS426) switched over its LOCAL HTTP API, never the cloud.

The strip answers POST http://<ip>/config with JSON signed by the Meross account's device key:
sign = md5(messageId + key + timestamp). The key is fetched once by Ironman (~/agent_workspace/infrastructure/meross/)
into a chmod-600 env file as FARM_MEROSS_KEY; it is never logged, shown or committed.

A printer gets power control only when a person maps it to one outlet in the Printers tab (registry "power":
{"type": "meross", "ip", "uuid", "channel"}). Rules (CLAUDE.md):
  - only a person switches (ui:/cli: actors), never a watch-only printer, never channel 0 (the whole strip);
  - Off is refused while printing or paused, with any heater target set, with the hotend at 50 C or more (its fan needs
    power to cool it), or when Moonraker can't be read - unless the board is already gone (monitor.powered_off);
  - every reply must come from the mapped device's uuid, so a DHCP change can't switch something else.

device(ip)         the strip: type, firmware, uuid, channels [{channel, onoff}] (cached 5 s)
state(printer)     {mapped, on, error} for one registry printer
set_power(...)     switch a mapped printer on (then FIRMWARE_RESTART to find the board) or off
devices()          every known strip (FARM_MEROSS_DEVICES + mapped ones) with its outlets and who is on them
"""
import hashlib
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid as _uuid

from . import jobs, net, printers

HOT_C = 50.0                # hotend at or above this: its fan still has to run, so no power off
_cache, _lock = {}, threading.Lock()


class Refused(Exception):
    pass


def key() -> str:
    return os.environ.get("FARM_MEROSS_KEY", "").strip()


def known_ips() -> list[str]:
    """Strips Farm Control should show: FARM_MEROSS_DEVICES (comma separated) plus every mapped one."""
    ips = [x.strip() for x in os.environ.get("FARM_MEROSS_DEVICES", "").split(",") if x.strip()]
    for p in printers.PRINTERS.values():
        ip = (p.power or {}).get("ip")
        if ip and ip not in ips:
            ips.append(ip)
    return ips


def lan_ip(ip: str) -> str:
    """The strip must be a private LAN address: power control never leaves the LAN."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError(f"power strip address must be a LAN IP, not {ip!r}") from None
    if not a.is_private or (a.is_loopback and not os.environ.get("FARM_POWER_TEST")):   # loopback: the tests' fake only
        raise ValueError(f"power strip address must be a private LAN IP, not {ip}")
    return ip


def sign(message_id: str, k: str, ts: int) -> str:
    return hashlib.md5(f"{message_id}{k}{ts}".encode()).hexdigest()


def _call(ip: str, method: str, namespace: str, payload: dict, want_uuid: str = "", port: int = 80) -> dict:
    """One signed request to the strip's /config. Raises RuntimeError with a plain reason; never includes the key."""
    k = key()
    if not k:
        raise RuntimeError("no Meross key: run ~/agent_workspace/infrastructure/meross/get_key.sh once")
    mid, ts = _uuid.uuid4().hex, int(time.time())
    msg = {"header": {"messageId": mid, "method": method, "from": f"http://{ip}/config", "namespace": namespace,
                      "timestamp": ts, "sign": sign(mid, k, ts), "payloadVersion": 1, "triggerSrc": "FarmControl"},
           "payload": payload}
    host = ip if port == 80 else f"{ip}:{port}"
    req = urllib.request.Request(f"http://{host}/config", data=json.dumps(msg).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        _, _, body = net._send(req, 6)
    except Exception as e:
        raise RuntimeError(f"power strip {ip} unreachable ({type(e).__name__})") from None
    try:
        out = json.loads(body)
    except ValueError:
        raise RuntimeError(f"power strip {ip} sent something that isn't Meross JSON") from None
    h = out.get("header") or {}
    if h.get("method") == "ERROR":
        err = ((out.get("payload") or {}).get("error") or {})
        if err.get("code") == 5001:
            raise RuntimeError("the strip refused the signature: the Meross key in meross.env is wrong or missing")
        raise RuntimeError(f"power strip {ip} said error {err.get('code')}: {str(err.get('detail'))[:80]}")
    if want_uuid and want_uuid.lower() not in str(h.get("from", "")).lower():
        raise RuntimeError(f"the device at {ip} is not the mapped strip (uuid changed): check its DHCP reservation")
    return out


def _port(ip: str) -> int:
    """Tests point a strip at a fake on 127.0.0.1:<port> (FARM_POWER_PORTS={"127.0.0.1": port})."""
    return int(json.loads(os.environ.get("FARM_POWER_PORTS", "{}")).get(ip, 80))


def device(ip: str, fresh: bool = False) -> dict:
    lan_ip(ip)
    with _lock:
        hit = _cache.get(ip)
        if hit and not fresh and time.time() - hit[0] < 5:
            return hit[1]
    out = _call(ip, "GET", "Appliance.System.All", {}, port=_port(ip))
    allp = (out.get("payload") or {}).get("all") or {}
    hw, fw = (allp.get("system") or {}).get("hardware") or {}, (allp.get("system") or {}).get("firmware") or {}
    tog = (allp.get("digest") or {}).get("togglex") or []
    tog = tog if isinstance(tog, list) else [tog]
    from_ = str((out.get("header") or {}).get("from", ""))
    d = {"ip": ip, "type": hw.get("type", "?"), "hardware": hw.get("version", ""), "firmware": fw.get("version", ""),
         "uuid": hw.get("uuid") or (from_.split("/")[2] if from_.count("/") >= 3 else ""),   # /appliance/<uuid>/publish
         "channels": sorted(({"channel": int(t.get("channel", 0)), "onoff": int(t.get("onoff", 0))} for t in tog
                             if isinstance(t, dict)), key=lambda c: c["channel"])}
    with _lock:
        _cache[ip] = (time.time(), d)
    return d


def _mapping(p) -> dict:
    m = p.power or {}
    if not m.get("ip") or not m.get("uuid") or not isinstance(m.get("channel"), int):
        raise Refused(f"{p.name} has no power outlet mapped: map it in Farm Control -> Printers -> Farm settings")
    if m["channel"] < 1:
        raise Refused("channel 0 switches the whole strip: map one outlet")
    return m


def state(p) -> dict:
    """{mapped, on, error} for a registry printer (cheap: uses the 5 s device cache)."""
    m = p.power or {}
    if not m.get("ip"):
        return {"mapped": False}
    try:
        d = device(m["ip"])
        if m.get("uuid") and m["uuid"].lower() != str(d.get("uuid", "")).lower():
            return {"mapped": True, "error": "the strip at that address is a different device (uuid changed)"}
        ch = next((c for c in d["channels"] if c["channel"] == m.get("channel")), None)
        if ch is None:
            return {"mapped": True, "error": f"outlet {m.get('channel')} not on the strip"}
        return {"mapped": True, "on": bool(ch["onoff"]), "channel": m["channel"]}
    except Exception as e:
        return {"mapped": True, "error": str(e)[:160]}


def devices() -> list[dict]:
    """Every known strip with its outlets and the printer mapped to each (for the Printers tab)."""
    out = []
    for ip in known_ips():
        try:
            d = dict(device(ip))
        except Exception as e:
            out.append({"ip": ip, "error": str(e)[:160]})
            continue
        for c in d["channels"]:
            c["printer"] = next((p.name for p in printers.PRINTERS.values()
                                 if (p.power or {}).get("ip") == ip and (p.power or {}).get("channel") == c["channel"]), None)
        out.append(d)
    return out


# ---------------------------------------------------------------- switching

def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")) or actor.split(":", 1)[1].strip() in ("", "unknown"):
        raise Refused("only a person switches printer power (put your name in the 'You' box first)")


def _off_check(p):
    """Refuses unless the printer is provably idle and cool, or its board is already gone."""
    from .monitor import powered_off
    try:
        info = net.get_json(f"{p.moonraker}/printer/info", timeout=5)["result"]
    except Exception:
        raise Refused(f"can't read {p.name}'s Moonraker, so it can't be shown to be idle - check it, or switch off at the strip by hand")
    st, msg = info.get("state"), info.get("state_message")
    if st != "ready":
        if powered_off(st, msg, None):
            return "board already off"
        raise Refused(f"Klipper is {st}: the hotend temperature can't be read - fix Klipper first, or switch off by hand")
    q = net.get_json(f"{p.moonraker}/printer/objects/query?print_stats=state&pause_resume=is_paused"
                     "&extruder=temperature,target&heater_bed=temperature,target", timeout=5)["result"]["status"]
    job = (q.get("print_stats") or {}).get("state")
    if job in ("printing", "paused") or (q.get("pause_resume") or {}).get("is_paused"):
        raise Refused(f"{p.name} is {job}: power off would ruin the print")
    for h in ("extruder", "heater_bed"):
        if float((q.get(h) or {}).get("target") or 0) > 0:
            raise Refused(f"{p.name}'s {h.replace('_', ' ')} is set to {float(q[h]['target']):.0f} C: turn it off first")
    t = float((q.get("extruder") or {}).get("temperature") or 0)
    if t >= HOT_C:
        raise Refused(f"hotend {t:.0f} C: wait until it's below {HOT_C:.0f} C (its fan needs power while it cools)")
    return f"idle, hotend {t:.0f} C"


def _toggle(m: dict, on: bool) -> bool:
    # identify the device BEFORE switching: checking the reply's uuid afterwards would already have switched it
    if str(device(m["ip"], fresh=True).get("uuid", "")).lower() != m["uuid"].lower():
        raise RuntimeError(f"the device at {m['ip']} is not the mapped strip (uuid changed): check its DHCP reservation")
    _call(m["ip"], "SET", "Appliance.Control.ToggleX", {"togglex": {"channel": m["channel"], "onoff": 1 if on else 0}},
          want_uuid=m["uuid"], port=_port(m["ip"]))
    ch = next((c for c in device(m["ip"], fresh=True)["channels"] if c["channel"] == m["channel"]), None)
    return bool(ch and ch["onoff"])


def _after_on(p, boot_s: float, wait_s: float) -> dict:
    """The board is powered: give it time to enumerate, then FIRMWARE_RESTART if Klipper is waiting for it."""
    from .monitor import powered_off
    time.sleep(boot_s)
    try:
        info = net.get_json(f"{p.moonraker}/printer/info", timeout=5)["result"]
    except Exception:
        return {"state": "unknown", "hint": "powered on, but its Moonraker didn't answer - is the printer host up?"}
    if info.get("state") == "ready":
        return {"state": "ready", "hint": ""}
    if not powered_off(info.get("state"), info.get("state_message"), None):
        return {"state": info.get("state"), "hint": f"powered on; Klipper is {info.get('state')}: {str(info.get('state_message'))[:120]}"}
    net.post_json(f"{p.moonraker}/printer/firmware_restart", timeout=10)
    state, deadline = info.get("state"), time.time() + wait_s
    while time.time() < deadline:
        time.sleep(1)
        try:
            state = net.get_json(f"{p.moonraker}/printer/info", timeout=5)["result"].get("state")
        except Exception:
            continue
        if state == "ready":
            break
    return {"state": state, "hint": "" if state == "ready" else "powered on, but Klipper didn't find the board: is its USB in?"}


def set_power(con, name: str, on: bool, actor: str, boot_s: float = 8, wait_s: float = 25) -> dict:
    _person(actor)
    p = printers.PRINTERS.get(name)
    if p is None:
        raise LookupError(f"{name} is not a farm printer")
    if p.readonly:
        raise Refused(f"{name} is watch-only: no power control from the farm")
    m = _mapping(p)
    lan_ip(m["ip"])
    if on:
        now = _toggle(m, True)
        if not now:
            raise RuntimeError(f"the strip didn't switch outlet {m['channel']} on")
        result = {"printer": name, "outlet": m["channel"], "on": True, **_after_on(p, boot_s, wait_s)}
    else:
        why = _off_check(p)
        now = _toggle(m, False)
        if now:
            raise RuntimeError(f"the strip didn't switch outlet {m['channel']} off")
        result = {"printer": name, "outlet": m["channel"], "on": False, "checked": why}
    jobs.event(con, "printer_op", 0, None, "power_on" if on else "power_off", actor, **result)
    return result


def validate_mapping(name: str, m: dict | None) -> dict | None:
    """A person's mapping from the Printers tab, checked against the live strip (needs the key)."""
    if not m:
        return None
    ip, ch = lan_ip(str(m.get("ip", "")).strip()), m.get("channel")
    try:
        ch = int(ch)
    except (TypeError, ValueError):
        raise ValueError("outlet: a number") from None
    if ch < 1:
        raise ValueError("channel 0 switches the whole strip: pick one outlet")
    d = device(ip, fresh=True)
    if ch not in [c["channel"] for c in d["channels"]]:
        raise ValueError(f"the strip at {ip} has no outlet {ch} (it has {', '.join(str(c['channel']) for c in d['channels'])})")
    for p in printers.PRINTERS.values():
        pm = p.power or {}
        if p.name != name and pm.get("ip") == ip and pm.get("channel") == ch:
            raise ValueError(f"outlet {ch} is already mapped to {p.name}")
    return {"type": "meross", "ip": ip, "uuid": d["uuid"], "channel": ch, "model": d["type"]}
