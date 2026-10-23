"""Follow started builds on their printers to the end (read-only towards Moonraker; it never sends G-code).

A build the gate started (printing) ends in Moonraker's print history under its remote file name, after its
start time: completed -> done, cancelled -> cancelled, error/shutdown -> failed. At the end it writes the
filament used to the spools that fed each colour (spools.deduct, idempotent): the slicer's per-colour grams
for a finished print, Moonraker's measured filament_used, prorated over the colours, for a stopped one.
A failed or cancelled build's jobs go back to "planned" so they can be printed again.
"""
import json

from . import db, inventory, jobs, net, printers, spools

DONE = {"completed": "done", "cancelled": "cancelled", "error": "failed", "klippy_shutdown": "failed",
        "klippy_disconnect": "failed", "server_exit": "failed", "interrupted": "failed"}


def _history(printer, since: float) -> list[dict]:
    r = net.get_json(f"{printer.moonraker}/server/history/list?limit=50&since={since - 120:.0f}", timeout=5)
    return r["result"]["jobs"]


def _assignments(con, build_id: int) -> list:
    r = con.execute("SELECT checklist FROM approvals WHERE build_id=? AND kind='start' ORDER BY id DESC LIMIT 1",
                    (build_id,)).fetchone()
    if not r:
        return []
    c = json.loads(r[0] or "{}")
    return c.get("spools") or ([c["spool"]] if c.get("spool") else [])


def finish(con, b: dict, status: str, entry: dict, log=print, actor: str = "tracker") -> str:
    to = DONE[status]
    used_mm = None if to == "done" else float(entry.get("filament_used") or 0)
    with db.tx(con):
        jobs.update_build(con, b["id"], finished_at=entry.get("end_time") or db.now(),
                          moonraker_job_id=str(entry.get("job_id", "")))
        jobs.transition(con, "build", b["id"], "printing", to, actor, moonraker_status=status,
                        filament_used_mm=entry.get("filament_used"))
        for (jid,) in con.execute("SELECT job_id FROM build_jobs WHERE build_id=?", (b["id"],)).fetchall():
            if jobs.get(con, "job", jid)["state"] == "printing":
                jobs.transition(con, "job", jid, "printing", "done" if to == "done" else "planned", actor,
                                build=b["id"])
        inventory.record_build(con, b, entry)
        written = spools.deduct(con, b["id"], b["attempt"], _assignments(con, b["id"]), b["segments"] or [],
                                used_mm=used_mm, reason=f"build {b['id']} {to}")
    log(f"tracker: build {b['id']} {b['labels'] or ''} on {b['printer']} -> {to} ({status}); spools {written}")
    return to


def poll(con, log=print) -> list[tuple]:
    """One pass over every printing build. Returns [(build id, new state)] for the ones that ended."""
    ended = []
    for b in jobs.builds(con, "printing"):
        printer = printers.PRINTERS.get(b["printer"])
        if printer is None or not b["remote_path"]:
            continue
        try:
            hist = _history(printer, b["started_at"] or 0)
        except Exception as e:
            log(f"tracker: {b['printer']} unreachable ({str(e)[:80]}); will look again")
            continue
        mine = [h for h in hist if h.get("filename") == b["remote_path"]
                and h.get("start_time", 0) >= (b["started_at"] or 0) - 120]
        if not mine:
            continue
        entry = max(mine, key=lambda h: h.get("start_time", 0))
        if entry.get("status") in DONE:
            ended.append((b["id"], finish(con, b, entry["status"], entry, log)))
    return ended
