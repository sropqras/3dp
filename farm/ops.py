"""Printer-farm operations the Printers tab runs, so nothing needs an AI session over SSH:

telegram_pack()   merge farm/telegram_pack.conf into a printer's telegram.conf (keeping [bot]: token, chat), restart
                  only the bot; a copy of the old file stays on the printer and comes back if the bot won't start.
                  The token is never stored, shown or logged by the farm.
restart_service() restart one service through that instance's Moonraker, refused when it would touch a print:
                  Klipper, Moonraker or crowsnest while a printer on that host is printing or paused, anything of a
                  watch-only printer.
usb_devices()     a host's USB serial ports and cameras (read-only, over SSH), each matched to the printer or camera
                  config that uses it - a new board or camera is the one nobody uses.
host_script()     the root-only steps (new Klipper instance, separate camera) as a script for a person to run with sudo.
"""
import configparser
import io
import json
import re
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from . import configs, db, jobs, net, printers
from .printers import REPO

PACK = REPO / "farm" / "telegram_pack.conf"
SSH_KEY = Path.home() / ".ssh" / "enders3dp"


class Refused(Exception):
    pass


def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")):
        raise Refused("only a person runs printer operations (Farm Control or a terminal)")


def _watch_only_names() -> list[str]:
    return [p.name for p in printers.PRINTERS.values() if p.readonly]


def _log(con, kind: str, detail: dict, actor: str):
    jobs.event(con, "printer_op", 0, None, kind, actor, **detail)


# ---------------------------------------------------------------- Telegram pack

def merge_pack(current: str, pack: str) -> tuple[str, list, list]:
    """Pack sections replace the printer's sections of the same name; everything else ([bot] first) is kept."""
    cur = configparser.ConfigParser(interpolation=None, strict=False)
    cur.optionxform = str
    cur.read_string(current)
    pk = configparser.ConfigParser(interpolation=None, strict=False)
    pk.optionxform = str
    pk.read_string(pack)
    for sec in pk.sections():
        if sec == "bot":
            continue                                   # never: token and chat live here
        if cur.has_section(sec):
            cur.remove_section(sec)
        cur.add_section(sec)
        for k, v in pk.items(sec):
            cur.set(sec, k, v)
    buf = io.StringIO()
    cur.write(buf)
    return buf.getvalue(), [s for s in pk.sections() if s != "bot"], [s for s in cur.sections() if s not in pk.sections()]


def _raw(url, path) -> bytes:
    """Read a config file WITHOUT configs.tracked's guard - only for telegram.conf, inside this module."""
    _, _, body = net.get_bytes(f"{url}/server/files/config/{urllib.parse.quote(path)}", timeout=15)
    return body


def _upload(url, path, content: bytes):
    folder, _, name = path.rpartition("/")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / name
        f.write_bytes(content)
        net.post_multipart(f"{url}/server/files/upload", {"root": "config", "path": folder}, {"file": f}, timeout=30)


def _services(url) -> dict:
    si = net.get_json(f"{url}/machine/system_info", timeout=8)["result"]["system_info"]
    return {n: f"{s.get('active_state')}/{s.get('sub_state')}" for n, s in (si.get("service_state") or {}).items()}


def bot_service(url, instance: str) -> str | None:
    names = [n for n in _services(url) if "telegram" in n]
    exact = [n for n in names if n.lower().endswith("-" + instance.lower())]
    return (exact or ([n for n in names if n == "moonraker-telegram-bot"]) or [None])[0]


def telegram_pack(con, key: str, url: str, instance: str, actor: str, wait_s: float = 8) -> dict:
    _person(actor)
    if key in _watch_only_names():
        raise Refused(f"{key} is watch-only")
    svc = bot_service(url, instance)
    if not svc:
        raise Refused(f"no Telegram bot service visible to {key}'s Moonraker")
    try:
        current = _raw(url, "telegram.conf")
    except Exception:
        raise Refused(f"{key} has no telegram.conf") from None
    new, replaced, kept = merge_pack(current.decode("utf-8", "replace"), PACK.read_text(encoding="utf-8"))
    backup = f"telegram.conf.bak-farm-{time.strftime('%Y%m%d-%H%M%S')}"
    _upload(url, backup, current)                    # stays on the printer, next to the original
    _upload(url, "telegram.conf", new.encode())
    net.post_json(f"{url}/machine/services/restart?service={urllib.parse.quote(svc)}", timeout=20)
    time.sleep(wait_s)
    state = _services(url).get(svc, "?")
    result = {"printer": key, "service": svc, "replaced": replaced, "kept": kept, "backup": backup, "state": state}
    if not state.startswith("active/running"):
        _upload(url, "telegram.conf", current)
        net.post_json(f"{url}/machine/services/restart?service={urllib.parse.quote(svc)}", timeout=20)
        result["rolled_back"] = True
    _log(con, "telegram_pack", result, actor)
    return result


# ---------------------------------------------------------------- services

def restart_service(con, key: str, url: str, service: str, host_instances: list[dict], actor: str) -> dict:
    _person(actor)
    allowed = _services(url)
    if service not in allowed:
        raise Refused(f"{service} is not a service {key}'s Moonraker may control")
    lowered = service.lower()
    for name in _watch_only_names():
        if name.lower() in lowered:
            raise Refused(f"{service} belongs to watch-only {name}")
    busy = [v for v in host_instances if v.get("job") in ("printing", "paused") or v.get("paused")]
    touches_prints = lowered.startswith(("klipper", "moonraker")) and "telegram" not in lowered \
        and "obico" not in lowered or lowered.startswith("crowsnest")
    if touches_prints and busy:
        raise Refused(f"{service} would disturb {', '.join(v.get('registry') or v.get('name') for v in busy)} "
                      f"(printing/paused) - restart it when they're idle")
    if lowered.startswith("crowsnest"):
        ro = [v for v in host_instances if v.get("readonly")]
        if ro:
            raise Refused(f"crowsnest also serves watch-only {ro[0].get('registry')}'s camera")
    net.post_json(f"{url}/machine/services/restart?service={urllib.parse.quote(service)}", timeout=30)
    time.sleep(5)
    state = _services(url).get(service, "?")
    result = {"printer": key, "service": service, "state": state}
    _log(con, "service_restart", result, actor)
    return result


# ---------------------------------------------------------------- USB devices (read-only, SSH)

def usb_devices(host: dict, used_serial: dict, used_cameras: dict) -> dict:
    """{serial: [{path, target, used_by}], cameras: [...]} - used_*: {device path or by-id name: who uses it}."""
    if not host.get("ssh"):
        return {"error": "no ssh account for this host in printers/hosts.json"}
    cmd = ("for d in /dev/serial/by-path/* /dev/v4l/by-id/*index0; do [ -e \"$d\" ] && echo \"$d $(readlink -f $d)\"; "
           "done 2>/dev/null; pgrep -af '^[^ ]*ustreamer[.]bin ' | sed 's/^/STREAM /'")   # anchored: not this shell
    try:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-i", str(SSH_KEY), host["ssh"], cmd],
                             capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return {"error": "ssh timed out"}
    if out.returncode != 0:
        return {"error": (out.stderr.strip().splitlines() or ["ssh failed"])[-1][:200]}
    serial, cams, streams = [], [], {}
    lines = out.stdout.splitlines()
    for line in lines:   # streams first: they're listed after the devices
        if line.startswith("STREAM "):
            port = re.search(r" -p (\d+)", line)
            dev = re.search(r" -d (\S+)", line)
            if port and dev:
                streams[dev.group(1)] = f"streamed on :{port.group(1)}"
    for line in lines:
        if line.startswith("STREAM "):
            continue
        path, _, target = line.partition(" ")
        if "/serial/" in path:
            serial.append({"path": path, "target": target, "used_by": used_serial.get(path)})
        else:
            name = path.rsplit("/", 1)[-1]
            who = used_cameras.get(path) or used_cameras.get(name)
            live = streams.get(path) or next((v for d, v in streams.items() if d.endswith(target)), None)
            cams.append({"path": path, "target": target, "used_by": " · ".join(x for x in (who, live) if x) or None})
    return {"serial": serial, "cameras": cams}


def used_devices(con, keys: list[str]) -> tuple[dict, dict]:
    """Which printer's config uses which serial port / camera device (from the latest config snapshots)."""
    serial, cams = {}, {}
    for key in keys:
        snap = configs.latest(con, key)
        if not snap:
            continue
        for path, digest in snap["files"].items():
            text = configs.blob(key, digest).decode("utf-8", "replace")
            for m in re.finditer(r"^serial:\s*(\S+)", text, re.M):
                serial[m.group(1)] = key
            for m in re.finditer(r"^device:\s*(\S+)", text, re.M):
                cams[m.group(1)] = "crowsnest"      # crowsnest.conf lives in one instance's folder but serves the host
            for m in re.finditer(r"-d (/dev/v4l/by-id/\S+)", text):
                cams[m.group(1)] = key
    return serial, cams


# ---------------------------------------------------------------- root-only host steps, as scripts

def host_script(kind: str, **p) -> str:
    """What a person runs with sudo on the printer host. Generated from the scripts proven on s1pro (2026-10-23)."""
    if kind == "camera":
        dev, port, name = p["device"], int(p["port"]), re.sub(r"[^a-z0-9-]", "", p["name"].lower())
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+-video-index0", dev):
            raise Refused("device: the name in /dev/v4l/by-id/ ending in -video-index0")
        return f"""#!/usr/bin/env bash
# Camera for {name} OUTSIDE crowsnest: its own ustreamer on 127.0.0.1:{port}; crowsnest and the other cameras untouched.
# Run on the printer host: sudo bash farm-camera-{name}.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || {{ echo "run with sudo"; exit 1; }}
DEV=/dev/v4l/by-id/{dev}
[[ -e "$DEV" ]] || {{ echo "no such camera: $DEV"; exit 1; }}
cat > /etc/systemd/system/ustreamer-{name}.service <<UNIT
[Unit]
Description={name} camera (ustreamer on 127.0.0.1:{port}), separate from crowsnest
After=network-online.target
[Service]
User=aali
SupplementaryGroups=video
ExecStart=/home/aali/crowsnest/bin/ustreamer/src/ustreamer.bin --host 127.0.0.1 -p {port} -d $DEV --device-timeout=2 -m MJPEG --encoder=HW -r 1280x720 -f 15 --allow-origin=* --static /home/aali/crowsnest/ustreamer-www
Restart=always
RestartSec=10
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now ustreamer-{name}.service
sleep 4
systemctl --no-pager --lines=3 status ustreamer-{name}.service | grep -E "Active" || true
curl -s -o /dev/null -w "snapshot on :{port}: %{{http_code}} %{{size_download}} bytes\\n" "http://127.0.0.1:{port}/?action=snapshot"
"""
    if kind == "instance":
        name, port = re.sub(r"[^A-Za-z0-9_-]", "", p["name"]), int(p["port"])
        own = p.get("klipper_commit")
        klipper_lines = ""
        if own:
            klipper_lines = f"""mkdir -p /etc/systemd/system/klipper-{name}.service.d
cat > /etc/systemd/system/klipper-{name}.service.d/farm.conf <<UNIT
[Service]
WorkingDirectory=$H/klipper-{name}
ExecStart=
ExecStart=$H/klippy-env-{name}/bin/python \\$KLIPPER_ARGS
UNIT
"""
        return f"""#!/usr/bin/env bash
# New farm Klipper instance '{name}' with Moonraker on :{port}. Prepare first (no sudo, as aali):
#   ~/printer_{name}_data/{{config,gcodes,logs,comms,systemd,database}}, config files, systemd/klipper.env + moonraker.env
#   {"own Klipper: git clone to ~/klipper-" + name + " at " + own + " + venv ~/klippy-env-" + name if own else "shared ~/klipper"}
# Then: sudo bash farm-instance-{name}.sh   (touches only this instance)
set -euo pipefail
[[ $EUID -eq 0 ]] || {{ echo "run with sudo"; exit 1; }}
H=/home/aali
D=$H/printer_{name}_data
for f in $D/systemd/klipper.env $D/systemd/moonraker.env $D/config/printer.cfg $D/config/moonraker.conf; do
  [[ -e $f ]] || {{ echo "missing $f - prepare the instance first"; exit 1; }}
done
grep -q "port: {port}" $D/config/moonraker.conf || {{ echo "moonraker.conf is not on port {port}"; exit 1; }}
cat > /etc/systemd/system/klipper-{name}.service <<UNIT
[Unit]
Description=Klipper ({name})
After=network-online.target
[Install]
WantedBy=multi-user.target
[Service]
Type=simple
User=aali
WorkingDirectory=$H/klipper
EnvironmentFile=$D/systemd/klipper.env
ExecStart=$H/klippy-env/bin/python \\$KLIPPER_ARGS
Restart=always
RestartSec=10
UNIT
{klipper_lines}cat > /etc/systemd/system/moonraker-{name}.service <<UNIT
[Unit]
Description=Moonraker for {name}, port {port}
Requires=network-online.target
After=network-online.target klipper-{name}.service
[Install]
WantedBy=multi-user.target
[Service]
Type=simple
User=aali
SupplementaryGroups=moonraker-admin
WorkingDirectory=$H/moonraker
EnvironmentFile=$D/systemd/moonraker.env
ExecStart=$H/moonraker-env/bin/python \\$MOONRAKER_ARGS
Restart=always
RestartSec=10
UNIT
getent group moonraker-admin >/dev/null || sed -i "/SupplementaryGroups/d" /etc/systemd/system/moonraker-{name}.service
systemctl daemon-reload
systemctl enable --now klipper-{name}.service moonraker-{name}.service
sleep 5
systemctl --no-pager --lines=0 status klipper-{name}.service moonraker-{name}.service | grep -E "Loaded|Active"
"""
    raise Refused(f"unknown script {kind}")
