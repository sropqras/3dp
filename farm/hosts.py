"""The fleet as Moonraker sees it: every instance on every host in printers/hosts.json, onboarded or not.

Read-only. Each instance gets one health record: Klipper state, versions, failed components and warnings,
services, host CPU/memory/disk, camera, history totals, and which registry printer it is (if any).
"""
import json
import socket
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import net, printers
from .printers import REPO

import os
HOSTS_FILE = Path(os.environ.get("FARM_HOSTS_FILE", REPO / "printers" / "hosts.json"))   # tests point elsewhere
VERSION_KEYS = ("klipper", "moonraker", "fluidd", "mainsail", "crowsnest", "moonraker-telegram-bot",
                "Klipper-Adaptive-Meshing-Purging", "timelapse")


def load_hosts(path: Path = HOSTS_FILE) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["hosts"]


def _ip(host: str) -> str:
    try:
        return socket.gethostbyname(host)
    except OSError:
        return host


def registry_by_endpoint() -> dict:
    """(ip, port) -> registry printer, so 3dpnas.local and 192.168.1.68 are the same instance."""
    out = {}
    for p in printers.PRINTERS.values():
        u = urllib.parse.urlparse(p.moonraker)
        out[(_ip(u.hostname), u.port or 80)] = p
    return out


def instance_name(config_file: str, host: str) -> str:
    """/home/aali/Ender3MaxNeo_data/config/printer.cfg -> Ender3MaxNeo; printer_s1pro_data -> s1pro;
    printer_data (single-instance host) -> the host's name."""
    parts = Path(config_file or "").parts
    data = next((p for p in parts if p.endswith("_data")), "")
    name = data[: -len("_data")]
    if name.startswith("printer_"):
        name = name[len("printer_"):]
    return name if name and name != "printer" else host


def _get(url: str, timeout: float = 5):
    try:
        return net.get_json(url, timeout=timeout).get("result")
    except Exception as e:
        return {"_error": str(e)[:160]}


def health(host: dict, port: int, reg: dict) -> dict:
    base = f"http://{host['address']}:{port}"
    v = {"host": host["name"], "port": port, "url": base, "reachable": False}
    info = _get(f"{base}/server/info", 4)
    if "_error" in info:
        v["error"] = info["_error"]
        return v
    v["reachable"] = True
    v["klippy_state"] = info.get("klippy_state")
    v["failed_components"] = info.get("failed_components") or []
    v["warnings"] = info.get("warnings") or []
    v["moonraker_version"] = info.get("moonraker_version")
    pinfo = _get(f"{base}/printer/info")
    v["state"] = pinfo.get("state")
    v["state_message"] = (pinfo.get("state_message") or "").strip().splitlines()[0][:200] if pinfo.get("state_message") else ""
    v["klipper_version"] = pinfo.get("software_version")
    v["name"] = instance_name(pinfo.get("config_file", ""), host["name"])
    p = reg.get((_ip(host["address"]), port))
    v["registry"] = p.name if p else None
    v["key"] = p.name if p else f"{host['name']}:{v['name']}"
    v["readonly"] = bool(p and p.readonly)
    v["fluidd"] = p.fluidd if p else f"http://{host['address']}/"
    st = _get(f"{base}/printer/objects/query?print_stats=state,filename&pause_resume=is_paused")
    s = (st or {}).get("status", {})
    v["job"] = (s.get("print_stats") or {}).get("state")
    v["file"] = (s.get("print_stats") or {}).get("filename")
    v["paused"] = bool((s.get("pause_resume") or {}).get("is_paused"))
    upd = _get(f"{base}/machine/update/status")
    vi = (upd or {}).get("version_info", {}) if isinstance(upd, dict) else {}
    v["versions"] = {k: (vi[k].get("version") or vi[k].get("full_version_string") or "?")
                     for k in vi if k in VERSION_KEYS or k.startswith(("moonraker-obico", "octoeverywhere"))
                     if isinstance(vi[k], dict)}
    v["updates"] = sorted(k for k, x in vi.items() if isinstance(x, dict) and x.get("version") and
                          x.get("remote_version") and x["version"] != x["remote_version"])
    si = _get(f"{base}/machine/system_info")
    sysi = (si or {}).get("system_info", {}) if isinstance(si, dict) else {}
    v["services"] = {name: f"{st.get('active_state')}/{st.get('sub_state')}"
                     for name, st in (sysi.get("service_state") or {}).items()}
    v["host_cpu"] = (sysi.get("cpu_info") or {}).get("model") or (sysi.get("cpu_info") or {}).get("processor")
    ps = _get(f"{base}/machine/proc_stats")
    if isinstance(ps, dict) and "_error" not in ps:
        mem = ps.get("system_memory") or {}
        v["cpu_temp"] = ps.get("cpu_temp")
        v["throttled"] = (ps.get("throttled_state") or {}).get("flags") or []
        v["mem_used_pct"] = round(100 * (1 - mem["available"] / mem["total"]), 1) if mem.get("total") else None
    du = _get(f"{base}/server/files/directory?path=gcodes&extended=false")
    if isinstance(du, dict) and du.get("disk_usage"):
        d = du["disk_usage"]
        v["disk_used_pct"] = round(100 * d["used"] / d["total"], 1) if d.get("total") else None
        v["disk_free_gb"] = round(d.get("free", 0) / 1e9, 1)
    cams = _get(f"{base}/server/webcams/list")
    v["cameras"] = [{"name": c.get("name"), "snapshot": c.get("snapshot_url")}
                    for c in (cams or {}).get("webcams", [])] if isinstance(cams, dict) else []
    tot = _get(f"{base}/server/history/totals")
    jt = (tot or {}).get("job_totals", {}) if isinstance(tot, dict) else {}
    v["print_hours"] = round(jt.get("total_print_time", 0) / 3600, 1) if jt else None
    v["total_jobs"] = int(jt.get("total_jobs", 0)) if jt else None
    return v


def camera_ok(v: dict) -> list[dict]:
    """Snapshot each registered camera through the host's web server (what Fluidd and Farm Control use)."""
    out = []
    host = urllib.parse.urlparse(v["url"]).hostname
    for c in v.get("cameras", []):
        url = c["snapshot"] if (c["snapshot"] or "").startswith("http") else f"http://{host}{c['snapshot']}"
        try:
            status, headers, body = net.get_bytes(url, timeout=5)
            out.append({**c, "ok": status == 200 and len(body) > 1000, "bytes": len(body)})
        except Exception as e:
            out.append({**c, "ok": False, "error": str(e)[:80]})
    return out


def fleet(cameras: bool = True) -> list[dict]:
    reg = registry_by_endpoint()
    targets = [(h, port) for h in load_hosts() for port in h.get("ports", [7125])]
    with ThreadPoolExecutor(max_workers=12) as ex:
        out = list(ex.map(lambda t: health(t[0], t[1], reg), targets))
        if cameras:
            cams = list(ex.map(lambda v: camera_ok(v) if v.get("reachable") else [], out))
            for v, c in zip(out, cams):
                if v.get("reachable"):
                    v["cameras"] = c
    for h in load_hosts():   # a host that answered on no port is one row, not one per port
        mine = [v for v in out if v["host"] == h["name"]]
        if mine and not any(v["reachable"] for v in mine):
            for v in mine[1:]:
                out.remove(v)
    return out
