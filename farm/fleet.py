"""The Printers tab's data: fleet health (farm/hosts.py), audit per instance, config snapshots and drift
(farm/configs.py). Read-only towards the printers; the only writes are snapshots and reviews in the farm DB."""
import threading
import time
import urllib.parse
from types import SimpleNamespace

from . import audit, configs, db, hosts, printers

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
    return [{"status": st, "rule": r, "detail": d} for st, r, d in audit.rules(s, p)]


def overview() -> dict:
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
            out.append(v)
    finally:
        con.close()
    by_host = {}
    for v in out:   # services are a host's, not an instance's: merge what each Moonraker may see
        by_host.setdefault(v["host"], {}).update(v.get("services") or {})
    return {"time": time.time(), "instances": out, "host_services": by_host,
            "hosts": hosts.load_hosts()}


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


def unquote_key(part: str) -> str:
    return urllib.parse.unquote(part)
