"""`python -m farm worker`: runs the slow pipeline steps (maker calls up to 15 min, slicing) off the web server.

Tasks live in the farm DB with leases: a claimed task that isn't finished before its lease ends (the worker
died, the box rebooted) is claimed again. Steps are written to be re-run safely. One task at a time, so every
maker sees at most one request from the farm and only one slicer runs.

Chaining: generate (each job) -> plan (the order, once no job is still generating) -> slice (each build) ->
review. Nothing after review is automatic: approval and Start are people (jobs.TRANSITIONS, farm/gate.py).
"""
import threading
import time
import traceback

from . import db, jobs, makers, pipeline, printers, tracker

LEASE_S = {"plan": 300, "slice": 3600, "upload": 900}
MAX_ATTEMPTS = 5
IDLE_SLEEP_S = 3
TRACK_EVERY_S = 20


def enqueue(con, kind: str, ref_id: int, delay_s: float = 0) -> int | None:
    """Idempotent: a pending/running task of the same kind for the same row is not duplicated."""
    with db.tx(con):
        if con.execute("SELECT 1 FROM tasks WHERE kind=? AND ref_id=? AND state IN ('pending','running')",
                       (kind, ref_id)).fetchone():
            return None
        return con.execute("INSERT INTO tasks (kind, ref_id, run_after, created_at) VALUES (?,?,?,?)",
                           (kind, ref_id, db.now() + delay_s, db.now())).lastrowid


def lease_for(con, kind: str, ref_id: int) -> float:
    if kind == "generate":
        job = jobs.get(con, "job", ref_id)
        return makers.get(job["maker"]).spec.timeout_s + 300 if job else 600
    return LEASE_S.get(kind, 600)


def claim(con) -> dict | None:
    with db.tx(con):
        t = con.execute(
            "SELECT * FROM tasks WHERE (state='pending' AND run_after<=?) OR (state='running' AND lease_until<?) "
            "ORDER BY id LIMIT 1", (db.now(), db.now())).fetchone()
        if t is None:
            return None
        lease = db.now() + lease_for(con, t["kind"], t["ref_id"])
        con.execute("UPDATE tasks SET state='running', lease_until=?, attempts=attempts+1 WHERE id=?",
                    (lease, t["id"]))
        return dict(t, attempts=t["attempts"] + 1)


def _finish(con, task_id: int, state: str, error: str | None = None, retry_in: float | None = None):
    if retry_in is not None:
        con.execute("UPDATE tasks SET state='pending', run_after=?, last_error=?, lease_until=NULL WHERE id=?",
                    (db.now() + retry_in, error, task_id))
    else:
        con.execute("UPDATE tasks SET state=?, last_error=?, finished_at=? WHERE id=?",
                    (state, error, db.now(), task_id))


def _after_generate(con, job_id: int):
    order_id = jobs.get(con, "job", job_id)["order_id"]
    states = [r[0] for r in con.execute("SELECT state FROM jobs WHERE order_id=?", (order_id,))]
    if "generated" in states and not {"received", "generating"} & set(states):
        enqueue(con, "plan", order_id)


def run_task(con, t: dict) -> None:
    kind, ref = t["kind"], t["ref_id"]
    if kind == "generate":
        pipeline.generate_job(con, ref, actor="worker")
        _after_generate(con, ref)
    elif kind == "plan":
        for build_id in pipeline.plan_builds(con, ref, actor="worker"):
            enqueue(con, "slice", build_id)
    elif kind == "slice":
        pipeline.slice_build(con, ref, actor="worker")
    elif kind == "upload":
        pipeline.upload_build(con, ref, actor="worker")
    else:
        raise pipeline.StepFailed(f"unknown task kind {kind!r}")


def run_once(con, log=print) -> bool:
    """Claim and run one task. Returns False when there was nothing to do."""
    t = claim(con)
    if t is None:
        return False
    log(f"task {t['id']}: {t['kind']} {t['ref_id']} (attempt {t['attempts']})")
    try:
        run_task(con, t)
        _finish(con, t["id"], "done")
        log(f"task {t['id']}: done")
    except pipeline.StepFailed as e:
        if e.retryable and t["attempts"] < MAX_ATTEMPTS:
            wait = 60 * 2 ** (t["attempts"] - 1)     # 1, 2, 4, 8 min
            _finish(con, t["id"], "pending", str(e), retry_in=wait)
            log(f"task {t['id']}: retry in {wait}s: {e}")
        else:
            _finish(con, t["id"], "failed", str(e))
            log(f"task {t['id']}: FAILED: {e}")
            _gave_up(con, t, str(e), log)
    except BaseException as e:   # incl. SystemExit from a stage: never let one task kill the worker
        if isinstance(e, KeyboardInterrupt):
            raise
        _finish(con, t["id"], "failed", f"{type(e).__name__}: {e}")
        log(f"task {t['id']}: CRASHED: {type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
        _gave_up(con, t, f"{type(e).__name__}: {e}", log)
    return True


def _gave_up(con, t: dict, error: str, log) -> None:
    """A task that won't be retried: an upload's build is failed; a job's siblings are still planned (one failed
    item must not hold the rest of the order back)."""
    try:
        if t["kind"] == "upload":
            pipeline.give_up_upload(con, t["ref_id"], error)
        elif t["kind"] == "generate":
            _after_generate(con, t["ref_id"])
    except Exception as e:
        log(f"task {t['id']}: clean-up after failure: {type(e).__name__}: {e}")


def heartbeat(con, detail: str = "") -> None:
    con.execute("INSERT INTO heartbeat (name, at, detail) VALUES ('worker', ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET at=excluded.at, detail=excluded.detail", (db.now(), detail))


def alive(con, max_age_s: float = 120) -> bool:
    r = con.execute("SELECT at FROM heartbeat WHERE name='worker'").fetchone()
    return bool(r) and db.now() - r[0] < max_age_s


def _beat_forever(stop: threading.Event):
    """Own thread + connection: a 15-minute maker call must not look like a dead worker to the watchdog."""
    con = db.connect()
    while not stop.wait(30):
        heartbeat(con, "running")


def loop(once: bool = False, log=print) -> None:
    con = db.connect()
    log(f"farm worker on {db.DB_PATH}")
    # there is only ever one worker (worker_start.sh kills any other before starting): a task still marked
    # running now belonged to a worker that died, so don't make it wait out its lease (up to an hour)
    n = con.execute("UPDATE tasks SET state='pending', lease_until=NULL, run_after=0 WHERE state='running'").rowcount
    if n:
        log(f"requeued {n} task(s) left running by a previous worker")
    stop = threading.Event()
    threading.Thread(target=_beat_forever, args=(stop,), daemon=True).start()
    last_track = 0.0
    while True:
        heartbeat(con, "idle")
        printers.refresh()                  # an onboarding saved in Farm Control applies without a restart
        if time.time() - last_track >= TRACK_EVERY_S:
            last_track = time.time()
            try:
                tracker.poll(con, log)
            except Exception as e:   # tracking is best-effort; it must never stop the pipeline
                log(f"tracker: pass failed: {type(e).__name__}: {e}")
        did = run_once(con, log)
        if once and not did:
            return
        if not did:
            time.sleep(IDLE_SLEEP_S)
