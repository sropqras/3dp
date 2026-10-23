"""The Printers tab's data: fleet health (farm/hosts.py), audit per instance, config snapshots and drift
(farm/configs.py). Read-only towards the printers; the only writes are snapshots and reviews in the farm DB."""
import posixpath
import re
import threading
import time
import urllib.parse
from types import SimpleNamespace

from . import audit, baseline, cfgchange, configs, db, hosts, jobs, maintenance, net, ops, printers

_cache, _lock = {}, threading.Lock()


def _cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    value = fn()
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def _audit(v: dict) -> list[dict] | None:
    if v.get("klippy_state") != "ready":
        return None
    p = printers.PRINTERS.get(v.get("registry") or "") or SimpleNamespace(has_m600_prompt=None)
    settings = audit.net.get_json(f"{v['url']}/printer/objects/query?configfile=settings", timeout=8)
    s = settings["result"]["status"]["configfile"]["settings"]
    return [{"status": st, "rule": r, "detail": d, "fixable": baseline.fixable(r)} for st, r, d in audit.rules(s, p)]


def overview() -> dict:
    if printers.refresh():
        _cache.pop("fleet", None)
    fl = _cached("fleet", 20, hosts.fleet)
    con = db.connect()
    try:
        out = []
        for v in fl:
            v = dict(v)
            if v.get("reachable"):
                try:
                    v["audit"] = _cached(f"audit:{v['key']}", 120, lambda v=v: _audit(v))
                except Exception as e:
                    v["audit_error"] = str(e)[:120]
                latest = configs.latest(con, v["key"])
                ref = configs.reference(con, v["key"])
                v["snapshot"] = {"latest": latest and {k: latest[k] for k in ("id", "taken_at", "actor")},
                                 "reference": ref and {k: ref[k] for k in ("id", "reviewed_by", "reviewed_at")},
                                 "files": len(latest["files"]) if latest else 0,
                                 "drift": configs.drift(con, v["key"], latest["files"]) if latest else None}
                if v.get("registry"):
                    v["maintenance"] = [s for s in maintenance.status(con, v["registry"], v.get("print_hours"))]
                v["changes"] = [{k: c[k] for k in ("id", "state", "path", "summary", "reason", "author", "error",
                                                   "applied_by", "created_at")}
                                for c in cfgchange.listing(con, v["key"], 8)]
            out.append(v)
    finally:
        con.close()
    by_host = {}
    for v in out:   # services are a host's, not an instance's: merge what each Moonraker may see
        by_host.setdefault(v["host"], {}).update(v.get("services") or {})
    from . import profiles as _profiles
    return {"time": time.time(), "instances": out, "host_services": by_host,
            "hosts": hosts.load_hosts(), "registry": printers.registry_entries(),
            "profiles": [{"id": x.id, "name": x.name, "nozzle": x.nozzle, "confirmed": x.confirmed}
                         for x in _profiles.PROFILES.values()]}


def _instance(key: str) -> dict:
    for v in _cached("fleet", 20, hosts.fleet):
        if v.get("key") == key and v.get("reachable"):
            return v
    raise LookupError(f"no reachable instance {key}")


def files(key: str) -> dict:
    con = db.connect()
    try:
        latest = configs.latest(con, key)
        ref = configs.reference(con, key)
        d = configs.drift(con, key, latest["files"]) if latest else None
        names = sorted(set((latest or {}).get("files", {})) | set((ref or {}).get("files", {})))
        rows = [{"path": n, "latest": (latest or {}).get("files", {}).get(n), "reviewed": (ref or {}).get("files", {}).get(n),
                 "state": ("added" if d and n in d["added"] else "removed" if d and n in d["removed"]
                           else "changed" if d and n in d["changed"] else "same" if ref else "unreviewed")}
                for n in names]
        return {"key": key, "latest": latest and latest["id"], "reference": ref and ref["id"], "files": rows}
    finally:
        con.close()


def file_text(key: str, path: str) -> str:
    con = db.connect()
    try:
        latest = configs.latest(con, key)
        digest = (latest or {}).get("files", {}).get(path)
        if not digest:
            raise LookupError(f"{path} not in {key}'s latest snapshot")
        return configs.blob(key, digest).decode("utf-8", "replace")
    finally:
        con.close()


def diff(key: str, path: str) -> str:
    """Live file on the printer against the reviewed reference (reads the printer, never writes)."""
    v = _instance(key)
    con = db.connect()
    try:
        ref = configs.reference(con, key)
        old = (ref or {}).get("files", {}).get(path)
    finally:
        con.close()
    try:
        new = configs.read_file(v["url"], path)
    except Exception:
        new = None
    return configs.diff(key, path, old, new) or "(no difference)"


def snapshot(key: str, actor: str) -> dict:
    v = _instance(key)
    con = db.connect()
    try:
        snap = configs.snapshot(con, key, v["url"], actor=actor)
        return {"id": snap["id"], "unchanged": bool(snap.get("unchanged")), "files": len(snap["files"]),
                "drift": configs.drift(con, key, snap["files"])}
    finally:
        con.close()


def review(key: str, snap_id: int, actor: str) -> dict:
    con = db.connect()
    try:
        snap = configs.get(con, snap_id)
        if not snap or snap["printer"] != key:
            raise LookupError(f"no snapshot {snap_id} for {key}")
        configs.review(con, snap_id, actor)
        return {"reviewed": snap_id}
    finally:
        con.close()


def changes(key: str | None = None) -> list[dict]:
    con = db.connect()
    try:
        return [{k: v for k, v in c.items() if k != "new_content"} for c in cfgchange.listing(con, key)]
    finally:
        con.close()


def stage_change(key: str, body: dict, actor: str) -> dict:
    v = _instance(key)
    con = db.connect()
    try:
        kw = {"new_content": body["content"]} if body.get("content") is not None else \
             {"section": body.get("section"), "option": body.get("option"), "value": body.get("value")}
        c = cfgchange.stage(con, key, v["url"], body.get("path") or "printer.cfg", reason=str(body.get("reason") or ""),
                            author=actor, **kw)
        return {"id": c["id"], "summary": c["summary"]}
    finally:
        con.close()


def change_action(cid: int, action: str, actor: str) -> dict:
    con = db.connect()
    try:
        if action == "preview":
            return {"diff": cfgchange.preview(con, cid)}
        if action == "apply":
            c = cfgchange.apply(con, cid, actor)
        elif action == "rollback":
            c = cfgchange.rollback(con, cid, actor)
        elif action in ("cancel", "reject"):
            c = cfgchange.get(con, cid)
            jobs.transition(con, "config_change", cid, c["state"], "cancelled" if action == "cancel" else "rejected", "ui")
            c = cfgchange.get(con, cid)
        else:
            raise LookupError(action)
        _cache.pop(f"audit:{c['printer']}", None)
        _cache.pop("fleet", None)
        return {k: v for k, v in c.items() if k != "new_content"}
    finally:
        con.close()


def stage_fix(key: str, rule: str, actor: str) -> list[dict]:
    v = _instance(key)
    con = db.connect()
    try:
        staged = baseline.stage_fix(con, key, v["url"], rule, cfgchange._settings(v["url"]), actor)
        return [{"id": c["id"], "path": c["path"], "summary": c["summary"]} for c in staged]
    finally:
        con.close()


def stage_all_fixes(key: str, actor: str) -> list[dict]:
    """Every failing audit rule that has a generated fix, one staged change per file."""
    v = _instance(key)
    rules = [a["rule"] for a in (_audit(v) or []) if a["status"] in ("FAIL", "WARN") and a["fixable"]]
    if not rules:
        raise cfgchange.Refused("no failing rule with a generated fix")
    con = db.connect()
    try:
        staged = baseline.stage_fixes(con, key, v["url"], rules, cfgchange._settings(v["url"]), actor)
        _cache.pop(f"audit:{key}", None)
        return [{"id": c["id"], "path": c["path"], "summary": c["summary"], "reason": c["reason"]} for c in staged]
    finally:
        con.close()


def telegram_pack(key: str, actor: str) -> dict:
    v = _instance(key)
    con = db.connect()
    try:
        return ops.telegram_pack(con, key, v["url"], v["name"], actor)
    finally:
        con.close()


def restart_service(key: str, service: str, actor: str) -> dict:
    v = _instance(key)
    same_host = [x for x in _cached("fleet", 20, hosts.fleet) if x["host"] == v["host"] and x.get("reachable")]
    con = db.connect()
    try:
        out = ops.restart_service(con, key, v["url"], service, same_host, actor)
        _cache.pop("fleet", None)
        return out
    finally:
        con.close()


def usb(host_name: str) -> dict:
    host = next((h for h in hosts.load_hosts() if h["name"] == host_name), None)
    if host is None:
        raise LookupError(f"no host {host_name}")
    keys = [v["key"] for v in _cached("fleet", 20, hosts.fleet) if v["host"] == host_name and v.get("key")]
    con = db.connect()
    try:
        serial, cams = ops.used_devices(con, keys)
    finally:
        con.close()
    return ops.usb_devices(host, serial, cams)


def maint(key: str) -> dict:
    v = _instance(key)
    con = db.connect()
    try:
        return {"status": maintenance.status(con, key, v.get("print_hours")), "entries": maintenance.entries(con, key),
                "intervals": maintenance.intervals(con, key), "kinds": list(maintenance.KINDS),
                "print_hours": v.get("print_hours")}
    finally:
        con.close()


def maint_log(key: str, body: dict, actor: str) -> dict:
    v = _instance(key)
    con = db.connect()
    try:
        if body.get("interval") is not None:
            maintenance.set_interval(con, str(body["kind"]), float(body["interval"]),
                                     printer=key if body.get("this_printer") else "*")
            return {"ok": True}
        return maintenance.log(con, key, str(body["kind"]), str(body.get("note") or ""), actor, v.get("print_hours"))
    finally:
        con.close()


# ---------------------------------------------------------------- registry + onboarding (phase 5)

NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,31}$")


def _person(actor: str):
    if not actor.startswith(("ui:", "cli:")):
        raise cfgchange.Refused("only a person changes the farm's printer registry")


def _farm_md_row(printer: str, change: str):
    """Registry edits land in FARM.md's change log, like applied config changes."""
    try:
        t = cfgchange.FARM_MD.read_text(encoding="utf-8")
        marker = "| Date | Printer | Change |\n|---|---|---|\n"
        if marker in t:
            clean = lambda x: re.sub(r"[|\r\n]+", " ", str(x)).strip()
            i = t.index(marker) + len(marker)
            row = f"| {time.strftime('%Y-%m-%d')} | {clean(printer)} | {clean(change)} |\n"
            cfgchange.FARM_MD.write_text(t[:i] + row + t[i:], encoding="utf-8")
    except OSError:
        pass


def onboard(key: str, body: dict, actor: str) -> dict:
    """Add an instance (a not-onboarded card) to printers/registry.json: name, usable bed, max Z, the profiles it may
    use, watch-only or not. The hardware facts are the person's to confirm - the form says so."""
    from . import profiles as _profiles
    _person(actor)
    v = _instance(key)
    name = str(body.get("name") or "").strip()
    if not NAME_RE.match(name):
        raise ValueError("name: a letter, then letters/digits/_/- (2-32)")
    entries = printers.registry_entries()
    if any(e["name"] == name for e in entries):
        raise ValueError(f"{name} is already in the registry")
    if v.get("registry"):
        raise ValueError(f"that Moonraker is already in the registry as {v['registry']}")
    bed_min = [float(x) for x in body["bed_min"]]
    bed_max = [float(x) for x in body["bed_max"]]
    if not (bed_max[0] > bed_min[0] >= 0 and bed_max[1] > bed_min[1] >= 0 and 50 <= float(body["max_z"]) <= 1000):
        raise ValueError("bed: min < max (mm), max Z 50..1000")
    profs = [str(p) for p in body.get("profiles") or []]
    unknown = [p for p in profs if p not in _profiles.PROFILES]
    if unknown:
        raise ValueError(f"unknown profile(s): {', '.join(unknown)}")
    first = _profiles.PROFILES[profs[0]] if profs else None
    entry = {"name": name, "note": str(body.get("note") or "")[:300], "moonraker": v["url"], "fluidd": v.get("fluidd", ""),
             "fluidd_note": str(body.get("fluidd_note") or "")[:200], "bed_min": bed_min, "bed_max": bed_max,
             "max_z": float(body["max_z"]), "has_m600_prompt": bool(body.get("has_m600_prompt")),
             "readonly": bool(body.get("readonly")), "profiles": profs,
             "legacy_presets": {"bundle": first.bundle.name, "printer": first.printer_preset, "print": first.print_preset,
                                "filament": {first.material: first.filament_preset}} if first else {}}
    printers.save_registry(entries + [entry])
    bad = {p: _profiles.check(_profiles.PROFILES[p]) for p in profs}
    bad = {p: x for p, x in bad.items() if x}
    if bad:   # e.g. a profile's bed doesn't fit this printer: undo, nothing half-saved
        printers.save_registry(entries)
        raise ValueError("; ".join(f"{p}: {', '.join(x)}" for p, x in bad.items()))
    _farm_md_row(name, f"Onboarded in Farm Control ({v['host']}:{v['port']}, bed {bed_min}-{bed_max}, Z {entry['max_z']}, "
                       f"profiles {', '.join(profs) or 'none'}{', watch-only' if entry['readonly'] else ''}) "
                       f"by {actor.split(':', 1)[-1]}")
    _cache.pop("fleet", None)
    return entry


def update_registry(name: str, body: dict, actor: str) -> dict:
    """Change an onboarded printer's farm settings: watch-only, profiles, Fluidd note, note."""
    from . import profiles as _profiles
    _person(actor)
    entries = printers.registry_entries()
    e = next((x for x in entries if x["name"] == name), None)
    if e is None:
        raise LookupError(f"{name} is not in the registry")
    changed = []
    if "readonly" in body and bool(body["readonly"]) != bool(e.get("readonly")):
        e["readonly"] = bool(body["readonly"])
        changed.append("watch-only" if e["readonly"] else "no longer watch-only")
    if "profiles" in body:
        profs = [str(p) for p in body["profiles"]]
        unknown = [p for p in profs if p not in _profiles.PROFILES]
        if unknown:
            raise ValueError(f"unknown profile(s): {', '.join(unknown)}")
        if profs != e.get("profiles"):
            e["profiles"] = profs
            changed.append(f"profiles {', '.join(profs) or 'none'}")
    for k in ("fluidd_note", "note"):
        if k in body and body[k] != e.get(k):
            e[k] = str(body[k])[:300]
            changed.append(k)
    if changed:
        printers.save_registry(entries)
        _farm_md_row(name, f"Farm settings: {'; '.join(changed)} (Farm Control, {actor.split(':', 1)[-1]})")
        _cache.pop("fleet", None)
    return {"name": name, "changed": changed}


def stage_serial(key: str, port: str, actor: str) -> dict:
    """Set [mcu] serial to a USB port read from the host (a staged change: a person Applies it like any other)."""
    if not port.startswith("/dev/serial/by-path/"):
        raise ValueError("use the board's /dev/serial/by-path/... port (stable across reboots, unlike by-id for CH340s)")
    v = _instance(key)
    con = db.connect()
    try:
        files = {f["path"]: configs.read_file(v["url"], f["path"]).decode("utf-8", "replace") for f in configs.list_files(v["url"])}
        path = next((p for p, t in files.items() if baseline.section_span(t, "mcu")), "printer.cfg")
        c = cfgchange.stage(con, key, v["url"], path, section="mcu", option="serial", value=port,
                            reason=f"onboarding: the board is on {port}", author=actor)
        return {"id": c["id"], "summary": c["summary"]}
    finally:
        con.close()


MISSING_INCLUDE = re.compile(r"Unable to open config file (\S+)")
INCLUDE_LINE = re.compile(r"^\s*\[include\s+([^\]]+?)\s*\]", re.I)


def stage_missing_include(key: str, actor: str) -> list[dict]:
    """Klipper won't start on an [include] whose file isn't on the host (an uninstalled plugin's macros, e.g.
    moonraker_obico_macros.cfg): stage commenting that include out, in every file printer.cfg reaches. A staged
    change like any other - a person Applies it, and the gate's restart shows whether Klipper comes up."""
    v = _instance(key)
    if v.get("readonly") or (printers.PRINTERS.get(v.get("registry") or "") or SimpleNamespace(readonly=False)).readonly:
        raise cfgchange.Refused(f"{key} is watch-only: no config changes")
    info = net.get_json(f"{v['url']}/printer/info", timeout=5)["result"]
    m = MISSING_INCLUDE.search(info.get("state_message") or "")
    if not m:
        raise cfgchange.Refused(f"{key} is not failing on a missing config file ({(info.get('state_message') or '')[:120]})")
    missing = m.group(1).rsplit("/", 1)[-1]
    todo, seen, out = ["printer.cfg"], set(), []
    con = db.connect()
    try:
        while todo:
            path = todo.pop()
            if path in seen:
                continue
            seen.add(path)
            try:
                text = configs.read_file(v["url"], path).decode("utf-8", "replace")
            except Exception:
                continue
            lines, hit = text.splitlines(keepends=True), False
            for i, line in enumerate(lines):
                inc = INCLUDE_LINE.match(line)
                if not inc:
                    continue
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), inc.group(1).strip()))
                if "*" not in target and not target.startswith(".."):
                    todo.append(target)
                if target.rsplit("/", 1)[-1] == missing:
                    lines[i] = f"# {line.rstrip()}   # farm: {missing} is not on the host (onboarding)\n"
                    hit = True
            if hit:
                c = cfgchange.stage(con, key, v["url"], path, new_content="".join(lines),
                                    reason=f"onboarding: Klipper can't start - {missing} is included but missing on the host",
                                    author=actor)
                out.append({"id": c["id"], "path": path, "summary": c["summary"]})
    finally:
        con.close()
    if not out:
        raise cfgchange.Refused(f"no [include] of {missing} found in the files printer.cfg reaches")
    _cache.pop("fleet", None)
    return out


def register_camera(key: str, body: dict, actor: str) -> dict:
    """A webcam entry on the instance's own Moonraker (what Fluidd, Telegram and Farm Control read)."""
    _person(actor)
    v = _instance(key)
    _not_watch_only(key, v)
    path = str(body.get("path") or "").strip("/")
    if not path.startswith("webcam"):
        raise ValueError("path: the host's web path for the stream, e.g. webcam3")
    item = {"name": str(body.get("name") or v.get("registry") or v["name"])[:40], "service": "mjpegstreamer",
            "stream_url": f"/{path}/?action=stream", "snapshot_url": f"/{path}/?action=snapshot",
            "target_fps": 15, "target_fps_idle": 5}
    _, _, out = net.post_json_body(f"{v['url']}/server/webcams/item", item, timeout=10)
    _cache.pop("fleet", None)
    return {"registered": item["name"], "stream": item["stream_url"]}


def _not_watch_only(key: str, v: dict):
    if v.get("readonly") or (printers.PRINTERS.get(v.get("registry") or "") or SimpleNamespace(readonly=False)).readonly:
        raise cfgchange.Refused(f"{key} is watch-only: use its own Fluidd")


def fluidd(key: str) -> dict:
    """What the farm Fluidd layout would make of this printer's macro panel and presets, and the last push."""
    from . import standard
    v = _instance(key)
    out = standard.fluidd_preview(v["url"], standard.live_macros(v["url"]))
    con = db.connect()
    try:
        last = con.execute("SELECT id, actor, at, undone_at FROM ui_pushes WHERE url=? ORDER BY id DESC LIMIT 1",
                           (v["url"],)).fetchone()
    finally:
        con.close()
    return {**out, "last": db.row(last) if last else None, "watch_only": bool(v.get("readonly"))}


def fluidd_apply(key: str, actor: str) -> dict:
    from . import standard
    _person(actor)
    v = _instance(key)
    _not_watch_only(key, v)
    con = db.connect()
    try:
        return standard.fluidd_apply(con, v.get("registry") or key, v["url"], standard.live_macros(v["url"]), actor)
    finally:
        con.close()


def fluidd_undo(key: str, push_id: int, actor: str) -> dict:
    from . import standard
    _person(actor)
    v = _instance(key)
    _not_watch_only(key, v)
    con = db.connect()
    try:
        r = con.execute("SELECT url FROM ui_pushes WHERE id=?", (push_id,)).fetchone()
        if r is None or r[0] != v["url"]:
            raise LookupError(f"no Fluidd push {push_id} on {key}")
        return standard.fluidd_undo(con, push_id, actor)
    finally:
        con.close()


def reconnect(key: str, actor: str, wait_s: float = 20) -> dict:
    """A printer switched off at the wall leaves Klipper in "Unable to connect"; after it is switched on, Klipper
    needs a FIRMWARE_RESTART to find the board. Only for that state - a config error or a lost-mid-print board is
    not fixed by a restart and stays for a person to read."""
    from .monitor import powered_off
    _person(actor)
    v = _instance(key)
    if v.get("readonly") or (printers.PRINTERS.get(v.get("registry") or "") or SimpleNamespace(readonly=False)).readonly:
        raise cfgchange.Refused(f"{key} is watch-only: use its own Fluidd")
    info = net.get_json(f"{v['url']}/printer/info", timeout=5)["result"]
    job = ((net.get_json(f"{v['url']}/printer/objects/query?print_stats=state", timeout=5)["result"]["status"]
            .get("print_stats") or {}).get("state"))
    if not powered_off(info.get("state"), info.get("state_message"), job):
        raise cfgchange.Refused(f"{key} is {info.get('state')}, not waiting for its board - nothing to reconnect")
    net.post_json(f"{v['url']}/printer/firmware_restart", timeout=10)
    deadline = time.time() + wait_s
    state = info.get("state")
    while time.time() < deadline:
        time.sleep(1)
        try:
            state = net.get_json(f"{v['url']}/printer/info", timeout=5)["result"].get("state")
        except Exception:
            continue
        if state == "ready":
            break
    _cache.pop("fleet", None)
    return {"key": key, "state": state,
            "hint": "" if state == "ready" else "still not connected: is the printer switched on and its USB in?"}


def unquote_key(part: str) -> str:
    return urllib.parse.unquote(part)
