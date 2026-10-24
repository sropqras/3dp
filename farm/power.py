"""Printer power at the wall: a Meross smart surge strip (MSS426) switched over its LOCAL HTTP API, never the cloud.

The strip answers POST http://<ip>/config with JSON signed by the Meross account's device key:
sign = md5(messageId + key + timestamp). The key is fetched once by Ironman (~/agent_workspace/infrastructure/meross/)
into a chmod-600 env file as FARM_MEROSS_KEY; it is never logged, shown or committed.

A printer gets power control only when a person maps it to one outlet in the Printers tab (registry "power":
{"type": "meross", "ip", "uuid", "channel"}). Rules (CLAUDE.md):
  - only a person switches (ui:/cli: actors), never a watch-only printer, never channel 0 (the whole strip);
  - Off is refused while printing or paused, with any heater target set, with the hotend at 50 C or more (its fan needs
    power to cool it), or when Moonraker can't be read - unless the board is already gone (monitor.powered_off);
  - every reply must come from the mapped device's uuid, so a DHCP change can't switch something else;
  - no flicker (Ironman, 2026-10-24: quick off/on cycles can fry a board): an outlet already in the asked state gets
    nothing sent, an outlet switched less than MIN_DWELL_S ago is refused, and two switches on one strip are at least
    STRIP_GAP_S apart. A file lock makes that hold across Farm Control, the monitor and the CLI.

device(ip)         the strip: type, firmware, uuid, channels [{channel, onoff}] (cached 5 s)
state(printer)     {mapped, on, error} for one registry printer
set_power(...)     switch a mapped printer on (then FIRMWARE_RESTART to find the board) or off
devices()          every known strip (FARM_MEROSS_DEVICES + mapped ones) with its outlets and who is on them
aux outlets        named outlets that are not printers (e.g. "Farm lights": USB LED lights for when the garage is dark),
                   kept in printers/power.json; switched by a person with no print checks (nothing to protect)
"""
import contextlib
import fcntl
import hashlib
import ipaddress
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import uuid as _uuid
from pathlib import Path

from . import jobs, net, printers

HOT_C = 50.0                # hotend at or above this: its fan still has to run, so no power off
MIN_DWELL_S = 60            # an outlet keeps its state at least this long: no quick off/on cycles on a board's PSU
STRIP_GAP_S = 5             # between any two switches on one strip: two printers never power up at the same instant
AUX_FILE = Path(os.environ.get("FARM_POWER_FILE", printers.REPO / "printers" / "power.json"))
_cache, _lock = {}, threading.Lock()


class Refused(Exception):
    pass


def key() -> str:
    return os.environ.get("FARM_MEROSS_KEY", "").strip()


def aux() -> list[dict]:
    """Named non-printer outlets: [{"name", "ip", "uuid", "channel"}]."""
    try:
        return list(json.loads(AUX_FILE.read_text(encoding="utf-8")).get("aux") or [])
    except (OSError, ValueError):
        return []


def known_ips() -> list[str]:
    """Strips Farm Control should show: FARM_MEROSS_DEVICES (comma separated) plus every mapped one."""
    ips = [x.strip() for x in os.environ.get("FARM_MEROSS_DEVICES", "").split(",") if x.strip()]
    for m in [p.power or {} for p in printers.PRINTERS.values()] + aux():
        ip = m.get("ip")
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
            c["aux"] = next((a["name"] for a in aux() if a.get("ip") == ip and a.get("channel") == c["channel"]), None)
        out.append(d)
    return out


# ---------------------------------------------------------------- switching

def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")) or actor.split(":", 1)[1].strip() in ("", "unknown"):
        raise Refused("only a person switches printer power (put your name in the 'You' box first)")


def farm_busy(timeout: float = 3) -> list[str]:
    """Every printer the farm can see that is printing or paused: the registry printers, every Moonraker in
    printers/hosts.json (printers not onboarded yet, like the Nebula Pad's V3 SE, share the strip), and the stock-Creality
    printers in FARM_CREALITY_HOSTS (the CR-10 SE, read-only over its status feed). One that can't be read is not
    counted (a switched-off or retired host would otherwise block every Off for ever)."""
    from . import hosts
    urls = {p.moonraker.rstrip("/"): p.name for p in printers.PRINTERS.values()}
    try:
        for h in hosts.load_hosts():
            for port in h.get("ports") or [7125]:
                urls.setdefault(f"http://{h['address']}:{port}", f"{h['name']}:{port}")
    except Exception:
        pass
    def one(item):
        url, name = item
        try:
            q = net.get_json(f"{url}/printer/objects/query?print_stats=state&pause_resume=is_paused", timeout=timeout)["result"]["status"]
        except Exception:
            return None
        printing = (q.get("print_stats") or {}).get("state") in ("printing", "paused") or (q.get("pause_resume") or {}).get("is_paused")
        return name if printing else None
    def creality_one(host):          # stock Creality firmware (farm/creality.py): read-only status feed, no Moonraker
        from . import creality
        try:
            return f"{host} (Creality)" if creality.busy(creality.status(host, timeout=timeout, port=creality.PORT)) else None
        except Exception:
            return None
    from concurrent.futures import ThreadPoolExecutor
    from . import creality
    with ThreadPoolExecutor(max_workers=8) as ex:      # in parallel: retired hosts that don't answer cost one timeout, not one each
        found = list(ex.map(one, urls.items())) + list(ex.map(creality_one, creality.hosts()))
    return sorted(n for n in found if n)


def _off_check(p):
    """Refuses unless the printer is provably idle and cool, or its board is already gone."""
    from .monitor import powered_off
    try:
        info = net.get_json(f"{p.moonraker}/printer/info", timeout=5)["result"]
    except Exception:
        raise Refused(f"can't read {p.name}'s Moonraker, so it can't be shown to be idle - check it, or switch off at the strip by hand")
    st, msg = info.get("state"), info.get("state_message")
    if st != "ready":
        try:          # Moonraker keeps print_stats while Klipper is down: a board lost MID-PRINT is not "already off"
            job = (net.get_json(f"{p.moonraker}/printer/objects/query?print_stats=state", timeout=5)["result"]
                   ["status"].get("print_stats") or {}).get("state")
        except Exception:
            job = None
        if job in ("printing", "paused", "error"):
            raise Refused(f"{p.name}'s print is {job}: switch it off by hand once you've looked at it")
        if powered_off(st, msg, job):
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


def _file() -> dict:
    try:
        return json.loads(AUX_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write(data: dict):
    tmp = AUX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(AUX_FILE)


def tripped() -> dict | None:
    """Set when a switch changed an outlet it wasn't sent to: ALL switching stops until a person clears it."""
    return _file().get("tripped") or None


def clear_trip(actor: str) -> dict:
    _person(actor)
    data = _file()
    was = data.pop("tripped", None)
    _write(data)
    return {"cleared": was}


def _switched_file() -> Path:
    """When each outlet (and each strip) was last switched: runtime state beside power.json, not in git."""
    return AUX_FILE.with_name(AUX_FILE.stem + ".switched.json")


def _switched() -> dict:
    try:
        return json.loads(_switched_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def _switch_lock():
    """One switch at a time across every process (web threads, the monitor, the CLI)."""
    with open(AUX_FILE.with_name(AUX_FILE.stem + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def wait_left(m: dict) -> float:
    """Seconds until this outlet may change again (0 = now)."""
    last = _switched().get(f"{m['ip']}#{int(m.get('channel', 0))}", 0)
    return max(0.0, MIN_DWELL_S - (time.time() - last))


def _toggle(m: dict, on: bool) -> bool:
    """The ONE place an outlet is switched. Never channel 0; the device is identified first; never a flicker (an
    outlet already so is left alone, one switched under MIN_DWELL_S ago is refused); every outlet is read before and
    after, and if any outlet other than this one changed, the trip latch stops all further switching."""
    ch_no = int(m.get("channel", 0))
    if ch_no < 1:
        raise Refused("channel 0 switches the whole strip: never")
    with _switch_lock():
        return _toggle_locked(m, ch_no, on)


def _toggle_locked(m: dict, ch_no: int, on: bool) -> bool:
    t = tripped()
    if t:
        raise Refused(f"power switching is stopped: {t.get('why')} - check the strip, then `python3 -m farm power reset`")
    before = device(m["ip"], fresh=True)
    # identify the device BEFORE switching: checking the reply's uuid afterwards would already have switched it
    if str(before.get("uuid", "")).lower() != m["uuid"].lower():
        raise RuntimeError(f"the device at {m['ip']} is not the mapped strip (uuid changed): check its DHCP reservation")
    cur = next((c for c in before["channels"] if c["channel"] == ch_no), None)
    if cur is not None and bool(cur["onoff"]) == on:
        return on            # already so (a double press, Moonraker's retry): nothing is sent
    left = wait_left(m)
    if left > 0:
        raise Refused(f"outlet {ch_no} was switched {int(MIN_DWELL_S - left)} s ago: wait {int(left) + 1} s "
                      f"(quick off/on cycles stress the printer's power supply and board)")
    log = _switched()
    gap = time.time() - log.get(m["ip"], 0)
    if gap < STRIP_GAP_S:
        time.sleep(STRIP_GAP_S - gap)
    _call(m["ip"], "SET", "Appliance.Control.ToggleX", {"togglex": {"channel": ch_no, "onoff": 1 if on else 0}},
          want_uuid=m["uuid"], port=_port(m["ip"]))
    log[f"{m['ip']}#{ch_no}"] = log[m["ip"]] = time.time()     # recorded at once: even a failed read-back counts
    tmp = _switched_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(log, indent=2) + "\n", encoding="utf-8")
    tmp.replace(_switched_file())
    after = device(m["ip"], fresh=True)
    was = {c["channel"]: c["onoff"] for c in before["channels"]}
    others = [c["channel"] for c in after["channels"] if c["channel"] != ch_no and was.get(c["channel"]) != c["onoff"]]
    if others:
        why = (f"switching outlet {ch_no} {'on' if on else 'off'} also changed outlet(s) {', '.join(map(str, others))} "
               f"at {time.strftime('%Y-%m-%d %H:%M')}")
        data = _file()
        data["tripped"] = {"why": why, "at": time.time()}
        _write(data)
        raise RuntimeError(f"STOPPED: {why}. All power switching is off until a person checks the strip.")
    ch = next((c for c in after["channels"] if c["channel"] == ch_no), None)
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


def set_power(con, name: str, on: bool, actor: str, boot_s: float = 8, wait_s: float = 25, reconnect: bool = True) -> dict:
    """reconnect=False: the printer's own Moonraker asked (its [power printer] device), and it restarts Klipper itself
    (restart_klipper_when_powered) - so switch and answer at once, well inside its 20 s request timeout."""
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
        result = {"printer": name, "outlet": m["channel"], "on": True,
                  **(_after_on(p, boot_s, wait_s) if reconnect else {"state": "switched on", "hint": "its Moonraker restarts Klipper"})}
    else:
        why = _off_check(p)
        # any printer printing anywhere on the farm blocks every Off: if this outlet were ever mapped to the wrong
        # printer, switching "off" an idle one must not be able to cut a running print
        busy = farm_busy()
        if busy:
            raise Refused(f"{', '.join(busy)} {'is' if len(busy) == 1 else 'are'} printing: no power off anywhere "
                          "until nothing is printing")
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
    for a in aux():
        if a.get("ip") == ip and a.get("channel") == ch and a.get("name") != name:
            raise ValueError(f"outlet {ch} is the {a['name']} outlet")
    return {"type": "meross", "ip": ip, "uuid": d["uuid"], "channel": ch, "model": d["type"]}


# ---------------------------------------------------------------- named non-printer outlets (farm lights)

def map_aux(name: str, ip: str | None, channel: int | None, actor: str) -> dict:
    """A person names an outlet (or clears it with ip=None). Checked against the live strip like a printer mapping."""
    from .fleet import _farm_md_row
    _person(actor)
    name = str(name).strip()[:40]
    if not name or name in printers.PRINTERS:
        raise ValueError("an outlet name that is not a printer's, e.g. 'Farm lights'")
    rest = [a for a in aux() if a.get("name") != name]
    if ip is None:
        entry, change = None, f"power outlet '{name}' removed"
    else:
        m = validate_mapping(name, {"ip": ip, "channel": channel})
        entry = {"name": name, "ip": m["ip"], "uuid": m["uuid"], "channel": m["channel"], "model": m["model"]}
        change = f"power outlet {m['channel']} on the {m['model']} at {m['ip']} is '{name}'"
    data = _file()
    data["aux"] = rest + ([entry] if entry else [])
    _write(data)
    _farm_md_row("farm", f"{change} ({actor.split(':', 1)[-1]})")
    return {"name": name, "outlet": entry}


def aux_state() -> list[dict]:
    """[{name, on, error}] for the named outlets (cheap: the 5 s device cache)."""
    out = []
    for a in aux():
        try:
            d = device(a["ip"])
            if a["uuid"].lower() != str(d.get("uuid", "")).lower():
                out.append({"name": a["name"], "error": "the strip at that address is a different device"})
                continue
            ch = next((c for c in d["channels"] if c["channel"] == a["channel"]), None)
            out.append({"name": a["name"], "on": bool(ch and ch["onoff"]), "channel": a["channel"]}
                       if ch else {"name": a["name"], "error": f"outlet {a['channel']} not on the strip"})
        except Exception as e:
            out.append({"name": a["name"], "error": str(e)[:160]})
    return out


LIGHTS = "Farm lights"
LIGHTS_OFF_AFTER_S = 600        # nothing printing for this long: lights off (covers a pause between prints)


def auto_lights(st: dict, busy: list[str], now: float) -> list[tuple[str, str]]:
    """The farm lights follow the prints (Ironman, 2026-10-24): on when a farm printer is printing or paused (for the
    cameras and PrintGuard), off once nothing has printed for LIGHTS_OFF_AFTER_S. The ONLY outlet the monitor may switch.
    It only switches off lights it switched on itself; lights a person switched on stay on. A person switching them
    off during a print is respected until that print ends. st persists between passes. Returns no alerts (just logs)."""
    a = next((x for x in aux() if x.get("name") == LIGHTS), None)
    if a is None or not key() or not a.get("proven") or tripped():
        return []        # never automatic until a person's own switch of this outlet read back clean
    if wait_left(a) > 0:
        return []        # switched under a minute ago: the next pass decides (no flicker)
    cur = next((x for x in aux_state() if x["name"] == LIGHTS), {})
    if "error" in cur or "on" not in cur:
        return []                       # power_pass reports an unreadable strip
    on = cur["on"]
    if busy:
        st["idle_since"] = None
        if not on and st.get("auto_on") and not st.get("held_off"):
            st["held_off"] = True       # it was ours and is off now: a person switched it off, respect that
        if not on and not st.get("held_off"):
            _auto_switch(a, True, f"printing: {', '.join(busy)}")
            st["auto_on"] = True
        return []
    st["held_off"] = False
    if not on:
        st["auto_on"], st["idle_since"] = False, None
        return []
    if not st.get("auto_on"):
        return []                       # a person switched them on: theirs to switch off
    st["idle_since"] = st.get("idle_since") or now
    if now - st["idle_since"] >= LIGHTS_OFF_AFTER_S:
        if farm_busy():                 # a printer the monitor doesn't watch (not onboarded) is printing: keep them on
            return []
        _auto_switch(a, False, f"nothing printing for {int((now - st['idle_since']) // 60)} min")
        st["auto_on"], st["idle_since"] = False, None
    return []


def _auto_switch(a: dict, on: bool, why: str):
    from . import db
    if a.get("name") != LIGHTS or int(a.get("channel", 0)) < 1:
        raise Refused("the monitor may only switch the farm lights")
    lan_ip(a["ip"])
    _toggle(a, on)
    con = db.connect()
    try:
        jobs.event(con, "printer_op", 0, None, "aux_on" if on else "aux_off", "monitor:auto-lights",
                   outlet=LIGHTS, channel=a["channel"], on=on, why=why)
    finally:
        con.close()


def switch_aux(con, name: str, on: bool, actor: str) -> dict:
    """A person switches a named outlet (lights): no print checks, there is nothing to protect."""
    _person(actor)
    a = next((x for x in aux() if x.get("name") == name), None)
    if a is None:
        raise LookupError(f"no outlet named {name!r}")
    if int(a.get("channel", 0)) < 1:
        raise Refused("channel 0 switches the whole strip")
    lan_ip(a["ip"])
    now = _toggle(a, on)
    if now != on:
        raise RuntimeError(f"the strip didn't switch outlet {a['channel']} {'on' if on else 'off'}")
    if not a.get("proven"):          # this person's switch read back clean: only this outlet changed
        data = _file()
        for x in data.get("aux", []):
            if x.get("name") == name:
                x["proven"] = time.strftime("%Y-%m-%d %H:%M") + f" by {actor.split(':', 1)[-1]}"
        _write(data)
    result = {"outlet": name, "channel": a["channel"], "on": on}
    jobs.event(con, "printer_op", 0, None, "aux_on" if on else "aux_off", actor, **result)
    return result


# ---------------------------------------------------------------- the printer's own power button (Telegram, Fluidd)

FARM_URL = os.environ.get("FARM_PUBLIC_URL", "http://192.168.1.107:10111")


def moonraker_section(name: str) -> str:
    """[power printer] for a printer's moonraker.conf: an http power device that asks Farm Control, so Telegram's
    power button and Fluidd's toggle go through the same gates as the card's ⏻. Moonraker also refuses while
    printing (locked_while_printing) and restarts Klipper once the board has power."""
    url = f"{FARM_URL.rstrip('/')}/api/power/device/{urllib.parse.quote(name)}"
    return f"""[power printer]
# Farm Control power (farm/power.py): the Meross outlet, switched only through the farm's gates (never while anything
# on the farm prints, hotend under 50 C, nothing heating). Telegram's power button and Fluidd's toggle use this.
type: http
on_url: {url}
off_url: {url}
status_url: {url}
request_template:
  {{% if command in ["on", "off"] %}}
    {{% do http_request.set_method("POST") %}}
    {{% do http_request.add_header("Content-Type", "application/json") %}}
    {{% do http_request.add_header("X-Farm", "1") %}}
    {{% do http_request.set_body({{"action": command}}) %}}
  {{% endif %}}
  {{% do http_request.send() %}}
response_template:
  {{http_request.last_response().json()["state"]}}
locked_while_printing: True
restart_klipper_when_powered: True
restart_delay: 8
"""


def stage_device(con, name: str, author: str) -> dict:
    """Stage [power printer] into the printer's moonraker.conf as a normal config change: a person Applies it through
    the gate when the printer is idle (Moonraker restarts). Never for a standalone or watch-only printer."""
    from . import baseline, cfgchange, configs
    p = printers.PRINTERS.get(name)
    if p is None:
        raise LookupError(f"{name} is not a farm printer")
    if p.readonly or p.standalone:
        raise Refused(f"{name} is {'watch-only' if p.readonly else 'standalone'}: its own power button isn't wired to the farm")
    _mapping(p)
    current = configs.read_file(p.moonraker, "moonraker.conf").decode("utf-8", "replace")
    crlf = "\r\n" in current
    text = current.replace("\r\n", "\n")
    new = baseline.replace_section(text, "power printer", moonraker_section(name))
    if new == text:
        raise Refused(f"{name}'s moonraker.conf already has this [power printer]")
    return cfgchange.stage(con, name, p.moonraker, "moonraker.conf", new_content=new.replace("\n", "\r\n") if crlf else new,
                           reason="power button in Telegram/Fluidd: [power printer] asks Farm Control, which switches the "
                                  "outlet through its gates (never while anything prints)", author=author)

