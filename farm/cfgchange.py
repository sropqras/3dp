"""Printer config changes: staged, reviewed, then applied through a gate - so the farm needs no AI session to
change a printer, and every change is recorded with who, why and what it did.

stage()    anyone (Farm Control, the CLI, an agent): a whole new file or one [section] option, with a reason.
           Only farm-tracked files (configs.tracked: never secrets or upstream code).
apply()    a person only (Farm Control button, or `farm config apply` at a terminal). Refuses unless the printer
           is not watch-only, not printing or paused, the live file is still the one the change was staged
           against, and no other change is being applied there. Then: snapshot, upload over Moonraker, FIRMWARE_RESTART,
           wait for Klipper, verify (ready, no new audit FAIL, no "!!"/"Unknown command" in the console since the
           restart). Klipper not coming back -> automatic rollback to the snapshot.
rollback() a person only: put back the file as it was before an applied (or failed) change, same gate.
"""
import json
import posixpath
import re
import tempfile
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from . import audit, configs, db, jobs, net, printers
from .printers import REPO

READY_TIMEOUT_S = 90
SETTLE_S, POLL_S, ERROR_GRACE_S = 2, 2, 20   # tests set these to ~0
FARM_MD = REPO / "FARM.md"


class Refused(Exception):
    pass


# ---------------------------------------------------------------- printer access

def _settings(url):
    return net.get_json(f"{url}/printer/objects/query?configfile=settings", timeout=10)["result"]["status"]["configfile"]["settings"]


def _state(url) -> dict:
    info = net.get_json(f"{url}/printer/info", timeout=5)["result"]
    st = {"klippy": info.get("state"), "message": (info.get("state_message") or "").strip()}
    if st["klippy"] == "ready":
        s = net.get_json(f"{url}/printer/objects/query?print_stats=state&pause_resume=is_paused", timeout=5)["result"]["status"]
        st["job"] = s["print_stats"]["state"]
        st["paused"] = bool(s["pause_resume"]["is_paused"])
    return st


def _board_off(st: dict) -> bool:
    """Klipper parsed its config but can't reach the board: the printer is switched off at the wall. A config
    error reads differently ("Option ... not valid", "Unable to open config file"), so this is a real check."""
    from .monitor import powered_off
    return powered_off(st.get("klippy"), st.get("message"), st.get("job"))


def _fails(url, printer_name) -> set:
    p = printers.PRINTERS.get(printer_name) or SimpleNamespace(has_m600_prompt=None)
    return {r for st, r, _ in audit.rules(_settings(url), p) if st == "FAIL"}


def _console_since(url, t0: float) -> list[str]:
    store = net.get_json(f"{url}/server/gcode_store?count=200", timeout=5)["result"]["gcode_store"]
    return [x["message"].strip() for x in store if x["time"] >= t0
            and (x["message"].startswith("!!") or "Unknown command" in x["message"])]


def _upload(url, path: str, content: bytes):
    folder, _, name = path.rpartition("/")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / name
        f.write_bytes(content)
        net.post_multipart(f"{url}/server/files/upload", {"root": "config", "path": folder}, {"file": f}, timeout=30)


def _restart_and_wait(url, timeout=READY_TIMEOUT_S) -> dict:
    t0 = time.time()
    net.post_json(f"{url}/printer/firmware_restart", timeout=10)
    time.sleep(SETTLE_S)
    last = {}
    while time.time() - t0 < timeout:
        try:
            last = _state(url)
            if last["klippy"] in ("ready", "error", "shutdown"):
                # "error" can be the moment before ready while the MCU resets; give it a few more seconds
                if last["klippy"] == "ready" or time.time() - t0 > ERROR_GRACE_S:
                    return last
        except Exception as e:
            last = {"klippy": "unreachable", "message": str(e)[:200]}
        time.sleep(POLL_S)
    return last


# ---------------------------------------------------------------- staging

def key_edit(content: str, section: str, option: str, value: str) -> str:
    """Set `option: value` in [section] (replacing an existing line, else adding it under the header).
    Leaves SAVE_CONFIG's auto-generated block alone."""
    head, sep, tail = content.partition("\n#*# <---------------------- SAVE_CONFIG")
    m = re.search(r"^\[" + re.escape(section) + r"\][ \t]*\n", head, re.M)
    if not m:
        raise Refused(f"[{section}] is not in this file")
    end = re.search(r"^\[", head[m.end():], re.M)
    body_end = m.end() + (end.start() if end else len(head) - m.end())
    body = head[m.end():body_end]
    line = re.compile(r"^" + re.escape(option) + r"[ \t]*[:=].*$", re.M)
    if line.search(body):
        body = line.sub(f"{option}: {value}", body, count=1)
    else:
        body = f"{option}: {value}\n" + body
    return head[:m.end()] + body + head[body_end:] + sep + tail


def stage(con, printer: str, url: str, path: str, *, new_content: str | None = None, section: str = None,
          option: str = None, value: str = None, reason: str, author: str) -> dict:
    if not configs.tracked(path):
        raise Refused(f"{path} is not a farm-managed file (secret, upstream or noise)")
    if not reason.strip():
        raise Refused("say why (the reason goes into the change log)")
    p = printers.PRINTERS.get(printer)
    if (p and p.readonly) or printers.watch_only_url(url):
        raise Refused(f"{printer} is watch-only: no config changes")
    try:
        live = configs.read_file(url, path)
    except Exception:
        live = None
    base = configs.sha(live) if live is not None else None
    if section:
        if live is None:
            raise Refused(f"{path} does not exist on {printer}")
        new = key_edit(live.decode(), section, option, str(value))
        kind, summary = "key", f"[{section}] {option}: {value}"
    else:
        new, kind = new_content, "file"
        summary = f"{path}: {'new file' if live is None else 'edited'}"
    if live is not None and new.encode() == live:
        raise Refused("that is already the live content - nothing to change")
    t = db.now()
    cur = con.execute("INSERT INTO config_changes (printer, url, path, kind, summary, reason, author, base_sha, "
                      "new_content, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (printer, url, path, kind, summary, reason.strip()[:500], author, base, new, "staged", t, t))
    jobs.event(con, "config_change", cur.lastrowid, None, "staged", author, path=path, summary=summary)
    return get(con, cur.lastrowid)


def get(con, cid: int) -> dict | None:
    return db.row(con.execute("SELECT * FROM config_changes WHERE id=?", (cid,)).fetchone())


def listing(con, printer: str | None = None, limit: int = 50) -> list[dict]:
    q, a = "SELECT * FROM config_changes", ()
    if printer:
        q, a = q + " WHERE printer=?", (printer,)
    return [db.row(r) for r in con.execute(q + " ORDER BY id DESC LIMIT ?", (*a, limit))]


def preview(con, cid: int) -> str:
    c = get(con, cid)
    try:
        live = configs.read_file(c["url"], c["path"])
    except Exception:
        live = b""
    import difflib
    return "".join(difflib.unified_diff(live.decode("utf-8", "replace").splitlines(keepends=True),
                                        c["new_content"].splitlines(keepends=True),
                                        f"live/{c['path']}", f"staged/{c['path']}", n=3)) or "(no difference)"


# ---------------------------------------------------------------- the gate

def _gate(con, c: dict, actor: str):
    if not actor.startswith(("ui:", "cli:")):
        raise Refused("only a person applies or rolls back printer config changes (Farm Control or a terminal)")
    if actor.split(":", 1)[1] in ("", "unknown"):
        raise Refused("put your name in the 'You' box first (it's recorded with the change)")
    p = printers.PRINTERS.get(c["printer"])
    if (p and p.readonly) or printers.watch_only_url(c["url"]):
        raise Refused(f"{c['printer']} is watch-only")
    st = _state(c["url"])
    if st.get("job") in ("printing", "paused") or st.get("paused"):
        raise Refused(f"{c['printer']} is {st.get('job')}{' (paused)' if st.get('paused') else ''}: "
                      f"never change a printer's config mid-print - stage it and apply when it's idle")
    busy = con.execute("SELECT id FROM config_changes WHERE printer=? AND state IN ('applying','verifying') AND id<>?",
                       (c["printer"], c["id"])).fetchone()
    if busy:
        raise Refused(f"change {busy[0]} is being applied to {c['printer']} right now")
    return st


def _farm_md(c: dict, actor: str):
    """Every applied change lands in FARM.md's change log - the doc can't drift from the printers."""
    try:
        text = FARM_MD.read_text(encoding="utf-8")
        marker = "| Date | Printer | Change |\n|---|---|---|\n"
        if marker not in text:
            return
        clean = lambda s: re.sub(r"[|\r\n]+", " ", str(s)).strip()
        row = (f"| {time.strftime('%Y-%m-%d')} | {clean(c['printer'])} | {clean(c['summary'])} ({clean(c['path'])}): "
               f"{clean(c['reason'])} - applied by {clean(actor.split(':', 1)[-1])} via Farm Control, change {c['id']} |\n")
        i = text.index(marker) + len(marker)
        FARM_MD.write_text(text[:i] + row + text[i:], encoding="utf-8")
    except OSError:
        pass


def batch(con, cid: int) -> list[dict]:
    """The changes applied together with this one (a restore stages one change per file as a set): staged ones of
    the same batch, this one first. A change outside a batch is a set of one."""
    c = get(con, cid)
    if not c or not c.get("batch"):
        return [c] if c else []
    rest = [db.row(r) for r in con.execute("SELECT * FROM config_changes WHERE batch=? AND state='staged' AND id<>? "
                                           "ORDER BY id", (c["batch"], cid))]
    return [c] + rest


def apply(con, cid: int, actor: str) -> dict:
    c = get(con, cid)
    if c is None:
        raise Refused(f"no change {cid}")
    if c["state"] != "staged":
        raise Refused(f"change {cid} is {c['state']}, not staged")
    group = batch(con, cid)
    st = _gate(con, c, actor)
    lives = {}
    for m in group:
        try:
            live = configs.read_file(m["url"], m["path"])
        except Exception:
            live = None
        if (configs.sha(live) if live is not None else None) != m["base_sha"]:
            raise Refused(f"{m['path']} changed on the printer since this was staged - stage it again")
        lives[m["id"]] = live
    # a file this change includes that another staged change creates (the farm macro library): that one goes on
    # first, or Klipper fails on the missing file and the gate has to roll back. A file the same set creates is fine.
    ours = {m["path"] for m in group}
    incs = [(m, inc) for m in group
            for inc in re.findall(r"^\s*\[include\s+([^\]*]+?)\s*\]", m["new_content"] or "", re.M | re.I)]
    present = {f["path"] for f in configs.list_files(c["url"])} if incs else set()
    for m, inc in incs:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(m["path"]), inc.strip()))
        if target in present or target in ours:
            continue
        first = con.execute("SELECT id FROM config_changes WHERE printer=? AND path=? AND state='staged' AND id NOT IN "
                            f"({','.join('?' * len(group))})", (c["printer"], target, *[g["id"] for g in group])).fetchone()
        if first:
            raise Refused(f"apply change {first[0]} ({target}) first - this one includes it")
    for m in group:      # the clicked change first: of two people pressing Apply, one wins here
        jobs.transition(con, "config_change", m["id"], "staged", "applying", "cfggate", by=actor,
                        **({"batch": c["batch"]} if c.get("batch") else {}))
        con.execute("UPDATE config_changes SET applied_by=?, applied_at=? WHERE id=?", (actor, db.now(), m["id"]))
    try:
        _apply(con, group, st, lives, actor)
        return get(con, cid)
    except Exception as e:
        # anything unexpected (printer gone mid-verify, ...) must not leave the change "applying"/"verifying": that
        # would block every later change to this printer. Failed = a person looks (the snapshot is there to restore)
        for m in group:
            cur = get(con, m["id"])
            if cur["state"] in ("applying", "verifying"):
                con.execute("UPDATE config_changes SET error=? WHERE id=?", (f"{type(e).__name__}: {str(e)[:500]}", m["id"]))
                jobs.transition(con, "config_change", m["id"], cur["state"], "failed", "cfggate", error=str(e)[:300])
        raise


def _apply(con, group: list[dict], st: dict, lives: dict, actor: str):
    c = group[0]
    ids = [m["id"] for m in group]
    snap = configs.snapshot(con, c["printer"], c["url"], actor=f"before change {', '.join(map(str, ids))}")
    for m in group:
        con.execute("UPDATE config_changes SET before_snapshot=? WHERE id=?", (snap["id"], m["id"]))
    # switched off at the wall: the host is up, Klipper loads the config and only the board is missing - the
    # change is verified as far as it can be (config loads, audit) and the console is read when it's next on
    off = _board_off(st)
    comparable = st["klippy"] == "ready" or off
    before_fails = _fails(c["url"], c["printer"]) if comparable else set()
    t0 = time.time() - 1
    try:
        for m in group:
            _upload(m["url"], m["path"], m["new_content"].encode())
        after = _restart_and_wait(c["url"])
    except Exception as e:
        after = {"klippy": "unreachable", "message": str(e)[:300]}
    loaded = after.get("klippy") == "ready" or (off and _board_off(after))
    if not loaded:
        # Klipper didn't come back with the new config: put the old files back straight away
        result = {"klipper": after, "auto_rollback": True}
        try:
            for m in group:
                if lives[m["id"]] is not None:
                    _upload(m["url"], m["path"], lives[m["id"]])
            result["after_rollback"] = _restart_and_wait(c["url"])
        except Exception as e:
            result["rollback_error"] = str(e)[:300]
        for m in group:
            con.execute("UPDATE config_changes SET result=?, error=? WHERE id=?",
                        (json.dumps(result), f"Klipper {after.get('klippy')}: {after.get('message', '')[:300]}", m["id"]))
            jobs.transition(con, "config_change", m["id"], "applying", "failed", "cfggate", **result)
            jobs.transition(con, "config_change", m["id"], "failed", "rolled_back", "cfggate", automatic=True)
        return
    for m in group:
        jobs.transition(con, "config_change", m["id"], "applying", "verifying", "cfggate")
    after_fails = _fails(c["url"], c["printer"])
    # a printer that wasn't ready before (being onboarded, a broken config) has no "before" to compare with:
    # only its console counts then. A restore puts an older config back on purpose: the audit FAILs that config
    # had come back with it and are reported, not counted against it
    restoring = all(m["kind"] == "restore" for m in group)
    new_fails = sorted(after_fails - before_fails) if comparable else []
    console = _console_since(c["url"], t0)
    result = {"klipper": after, "new_audit_fails": new_fails, "console_errors": console,
              **({"board_off": True, "note": "applied with the printer switched off: the config loads and the audit "
                  "ran; macros first run when it is on - read its console after Reconnect"} if off else {}),
              **({"files": [m["path"] for m in group]} if len(group) > 1 else {}),
              "fixed_audit_fails": sorted(before_fails - after_fails), "audit_fails_now": sorted(after_fails)}
    bad = ([] if restoring else [f"new audit FAIL: {f}" for f in new_fails]) + console
    if not bad:   # the new config dated from now on: prints on it count as its evidence (farm/restore.py)
        try:
            configs.snapshot(con, c["printer"], c["url"], actor=f"after change {', '.join(map(str, ids))}")
        except Exception:
            pass
    for m in group:
        con.execute("UPDATE config_changes SET result=? WHERE id=?", (json.dumps(result), m["id"]))
        if bad:
            con.execute("UPDATE config_changes SET error=? WHERE id=?", ("; ".join(bad)[:1000], m["id"]))
            jobs.transition(con, "config_change", m["id"], "verifying", "failed", "cfggate", **result)
        else:
            jobs.transition(con, "config_change", m["id"], "verifying", "applied", "cfggate", **result)
            _farm_md(get(con, m["id"]), actor)


def rollback(con, cid: int, actor: str) -> dict:
    """Put back the files as they were before an applied (or failed) change - every file of its set together."""
    c = get(con, cid)
    if c is None or c["state"] not in ("applied", "failed"):
        raise Refused(f"change {cid} is {c and c['state']}: only an applied or failed change can be rolled back")
    _gate(con, c, actor)
    group = [c] + ([db.row(r) for r in con.execute(
        "SELECT * FROM config_changes WHERE batch=? AND id<>? AND state IN ('applied','failed') ORDER BY id",
        (c["batch"], cid))] if c.get("batch") else [])
    snap = configs.get(con, c["before_snapshot"]) if c["before_snapshot"] else None
    put = [(m, (snap or {}).get("files", {}).get(m["path"])) for m in group]
    if not any(d for _, d in put):
        raise Refused(f"no copy of {c['path']} from before change {cid} (it was a new file?)")
    for m, digest in put:
        if digest:        # a file the change created stays; nothing the old config loads refers to it
            _upload(m["url"], m["path"], configs.blob(m["printer"], digest))
    after = _restart_and_wait(c["url"])
    for m, _ in put:
        jobs.transition(con, "config_change", m["id"], m["state"], "rolled_back", "cfggate", by=actor, klipper=after)
    return get(con, cid)
