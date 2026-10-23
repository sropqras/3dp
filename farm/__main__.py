"""python -m farm <command>

  run <job.json> [--upload]   generate (likhan) -> slice -> validate [-> upload]; writes runs/<stamp>_<job>/
  submit <job.json>           queue a job for the worker (generate -> plan -> slice -> review); returns at once
  builds [--state S]          list builds (id, state, printer, labels, profile, file)
  approve <build|run-dir>     visual gate at a terminal: approve a build in review, then upload it (a person only)
  start <build|run-dir>       start a queued build through the gate (checklist, audit, spool; a person only)
  watch <run-dir>             follow a started run; stops at first layer / colour change / finish / error
  status [printer]            show printer state
  audit [printer]             check live Klipper configs against the farm baseline (read-only)
  worker [--once | --alive]   run queued pipeline tasks (generate, plan, slice, upload); --alive: heartbeat check
  db init | import-runs       create/migrate data/farm.db; backfill runs/ made before it existed
  presets check               static check of every print profile's PrusaSlicer presets (farm/profiles.py)
  config list|show|stage|apply|rollback|cancel   printer config changes (staged by anyone; applied by a person
                              at a terminal or in Farm Control's Printers tab; see farm/cfgchange.py)
  backup [printer] [--to-repo] snapshot printer configs over Moonraker (secrets excluded); --to-repo mirrors to printers/
  doctor                      is this host ready to produce: slicer version, maker apps, fonts (read-only)
  monitor [--state FILE]      one pass of the serious-error monitor; prints new alerts as '<loud|silent>\t<text>'
  serve [--port 10111]        farm control room: live printers, cameras, spools, jobs, batch progress
"""
import argparse
import json
import sys
from pathlib import Path

from . import audit, db, jobs, monitor, moonraker, pipeline, printers, profiles, watch
from .runlog import Run


def cmd_run(args):
    """The whole pipeline inline (no worker): submit -> generate -> plan -> slice [-> approve + upload]."""
    job_path = Path(args.job)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    printer = printers.get(job["printer"])
    if printer.readonly:
        sys.exit(f"{printer.name} is watch-only (readonly in printers/registry.json); no jobs are sent to it")
    con = db.connect()
    order_id, run = pipeline.submit_job_file(con, job, job_path)
    with run.guard():
        try:
            for (job_id,) in con.execute("SELECT id FROM jobs WHERE order_id=? ORDER BY id", (order_id,)).fetchall():
                pipeline.generate_job(con, job_id, actor="cli")
            builds = pipeline.plan_builds(con, order_id, actor="cli")
            for build_id in builds:
                pipeline.slice_build(con, build_id, actor="cli")
            if args.upload:
                for build_id in builds:
                    # the legacy CLI has no review step of its own: the visual gate is the human at `farm start`
                    pipeline.approve(con, build_id, "cli", note="legacy run --upload (visual gate at start)")
                    pipeline.upload_build(con, build_id, actor="cli")
        except pipeline.StepFailed as e:
            run.fail(str(e))
        run.manifest["result"] = "ok"
        run.save()
        run.log(f"run: done (order {order_id}, builds {builds}). Next gate: load "
                f"'{(job.get('colours') or ['?'])[0]}', clear the bed, then python -m farm start {run.dir}")


def cmd_submit(args):
    from . import worker
    job_path = Path(args.job)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if printers.get(job["printer"]).readonly:
        sys.exit(f"{job['printer']} is watch-only; no jobs are sent to it")
    con = db.connect()
    order_id, run = pipeline.submit_job_file(con, job, job_path)
    ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE order_id=? ORDER BY id", (order_id,))]
    for job_id in ids:
        worker.enqueue(con, "generate", job_id)
    print(f"order {order_id}: {len(ids)} job(s) queued for the worker; run folder {run.dir}")


def _person_at_terminal(what: str):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        sys.exit(f"farm {what} is for a person at a terminal; agents never {what} prints (CLAUDE.md)")


def _builds_for(con, target: str, state: str) -> list[dict]:
    if target.isdigit():
        b = jobs.get(con, "build", int(target))
        return [b] if b else []
    run_dir = str(Path(target).resolve())
    return [b for b in jobs.builds(con, state) if b["run_dir"] and str(Path(b["run_dir"]).resolve()) == run_dir]


def cmd_approve(args):
    """The visual gate at a terminal: look at the .3mf, then approve this exact G-code and upload it."""
    _person_at_terminal("approve")
    con = db.connect()
    builds = _builds_for(con, args.target, "review")
    if not builds:
        sys.exit(f"no build in review for {args.target} (python3 -m farm builds to list them)")
    for b in builds:
        print(f"build {b['id']}: {b['labels']} on {b['printer']} ({b['profile']})\n  3D: {b['project_3mf']}")
        if input("Opened that .3mf (or Farm Control's 3D view) and it looks right? [y/N] ").strip().lower() != "y":
            print("  not approved")
            continue
        who = input("Your name: ").strip()
        try:
            pipeline.approve(con, b["id"], "cli", note=f"approved at terminal by {who}")
            print(f"  uploading to {b['printer']} ...")
            print(f"  queued: {pipeline.upload_build(con, b['id'], actor='cli')}")
        except pipeline.StepFailed as e:
            print(f"  refused: {e}")


def cmd_start(args):
    """Start a queued build through the gate (farm/gate.py): the same checks as Farm Control's Start button."""
    from . import gate
    _person_at_terminal("start")
    con = db.connect()
    builds = _builds_for(con, args.target, "queued")
    if len(builds) != 1:
        sys.exit(f"{len(builds)} queued build(s) for {args.target}; give one build id (python3 -m farm builds)")
    b = builds[0]
    print(f"build {b['id']}: {b['labels']} -> {b['printer']}  ({b['remote_path']})")
    if input(f"Type the printer name to start it on ({b['printer']}): ").strip() != b["printer"]:
        sys.exit("printer name didn't match; not starting")
    ask = lambda q: input(q + " [y/N] ").strip().lower() == "y"
    checklist = {"viewed_3d": ask("Checked the 3D view / .3mf of THIS build?"),
                 "bed_clear": ask("Bed clear and clean?"),
                 "colour_loaded": ask(f"First colour loaded ({', '.join(b['colours'][:1]) or 'as planned'})?")}
    spools = gate.spools_for(con, b["printer"])
    for sp in spools:
        print(f"  spool {sp['id']}: {sp.get('colour')} {sp.get('material')} ({sp.get('remaining_g')} g)")
    spool = input("Spool id loaded now: ").strip()
    who = input("Your name: ").strip()
    try:
        remote = gate.start_build(con, b["id"], who, checklist, spool, via="cli")
    except gate.GateRefused as e:
        sys.exit(f"not started: {e}")
    print(f"started {remote} on {b['printer']}")


def cmd_builds(args):
    con = db.connect()
    for b in jobs.builds(con, args.state):
        print(f"{b['id']:>4} {b['state']:<10} {b['printer']:<13} {b['labels'] or '':<24} {b['profile'] or '':<24} "
              f"{(b['remote_path'] or b['gcode'] or '')[-60:]}")


def cmd_watch(args):
    run_dir = Path(args.run_dir)
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    printer = printers.get(job["printer"])
    gcodes = sorted(run_dir.glob("*.gcode"))
    run = Run.resume(run_dir)
    result = watch.watch(run, printer, gcodes[0], until=args.until)
    run.log(f"watch: {result}")


def cmd_audit(args):
    names = [args.printer] if args.printer else list(printers.PRINTERS)
    fails = audit.audit([printers.get(n) for n in names])
    print(f"\n{fails} failing rule(s)" if fails else "\nall printers meet the farm baseline")
    sys.exit(1 if fails else 0)


def cmd_monitor(args):
    for level, text in monitor.run(Path(args.state)):
        # one alert per line for the shell job: newlines inside an alert travel as \n
        print(f"{level}\t{text.replace(chr(10), chr(92) + 'n')}")


def cmd_worker(args):
    from . import worker
    if args.alive:
        ok = worker.alive(db.connect())
        print("worker alive" if ok else "worker heartbeat stale or missing")
        sys.exit(0 if ok else 1)
    worker.loop(once=args.once)


def cmd_db(args):
    from . import db
    from .runlog import RUNS_DIR
    con = db.connect()
    print(f"{db.DB_PATH}: schema v{con.execute('PRAGMA user_version').fetchone()[0]}")
    if args.action == "import-runs":
        done = pipeline.import_runs(con, RUNS_DIR)
        print(f"imported {len(done)} run(s)" + (": " + ", ".join(done) if done else ""))


def cmd_presets(args):
    bad = 0
    for p in profiles.PROFILES.values():
        problems = profiles.check(p)
        bad += bool(problems)
        flag = "" if p.confirmed else f"  [UNCONFIRMED: {p.note}]"
        print(f"{'OK  ' if not problems else 'FAIL'} {p.id}: {p.name} on {', '.join(p.printers)}{flag}")
        for pr in problems:
            print(f"     - {pr}")
    sys.exit(1 if bad else 0)


def cmd_config(args):
    from . import cfgchange, fleet
    con = db.connect()
    if args.action == "list":
        for c in cfgchange.listing(con, args.printer):
            print(f"{c['id']:>4} {c['state']:<11} {c['printer']:<14} {c['summary'][:50]:<50} {c['author']} - {c['reason'][:60]}")
        return
    if args.action == "show":
        c = cfgchange.get(con, args.id)
        print(f"change {c['id']} {c['state']} on {c['printer']} {c['path']}: {c['summary']}\nreason: {c['reason']}\n")
        print(cfgchange.preview(con, args.id) if c["state"] == "staged" else (c["result"] or c["error"] or ""))
        return
    if args.action == "stage":
        v = fleet._instance(args.printer)
        kw = {"new_content": Path(args.file).read_text(encoding="utf-8")} if args.file else \
             {"section": args.section, "option": args.option, "value": args.value}
        who = args.author or "cli"
        try:
            c = cfgchange.stage(con, v["key"], v["url"], args.path, reason=args.reason, author=who, **kw)
        except cfgchange.Refused as e:
            sys.exit(f"not staged: {e}")
        print(f"staged change {c['id']}: {c['summary']} - review and apply it in Farm Control -> Printers")
        return
    if args.action == "cancel":
        c = cfgchange.get(con, args.id)
        jobs.transition(con, "config_change", args.id, c["state"], "cancelled", "agent" if args.author == "agent" else "cli")
        print(f"change {args.id} cancelled")
        return
    _person_at_terminal(args.action)
    who = "cli:" + input("Your name: ").strip()
    print(cfgchange.preview(con, args.id) if args.action == "apply" else "")
    if input(f"{args.action} change {args.id}? [y/N] ").strip().lower() != "y":
        sys.exit("not done")
    try:
        c = (cfgchange.apply if args.action == "apply" else cfgchange.rollback)(con, args.id, who)
    except cfgchange.Refused as e:
        sys.exit(f"refused: {e}")
    print(f"change {c['id']}: {c['state']} {c['error'] or ''}")


def cmd_backup(args):
    from . import configs, hosts
    con = db.connect()
    bad = 0
    for v in hosts.fleet(cameras=False):
        if not v["reachable"] or (args.printer and args.printer not in (v.get("key"), v.get("registry"), v.get("name"))):
            continue
        try:
            snap = configs.snapshot(con, v["key"], v["url"], actor="backup")
        except Exception as e:
            bad += 1
            print(f"FAIL {v['key']}: {str(e)[:120]}")
            continue
        d = configs.drift(con, v["key"], snap["files"])
        what = "unchanged" if snap.get("unchanged") else f"snapshot {snap['id']}"
        drift = ("no reviewed reference yet" if d["reference"] is None else
                 "no drift" if not (d["changed"] or d["added"] or d["removed"]) else
                 f"DRIFT changed={d['changed']} added={d['added']} removed={d['removed']}")
        print(f"OK   {v['key']}: {len(snap['files'])} files, {what}; {drift}")
        if args.to_repo:
            dest = Path(__file__).resolve().parent.parent / "printers" / (v.get("registry") or v["name"])
            written = configs.to_repo(con, v["key"], dest)
            print(f"     -> {dest.relative_to(dest.parent.parent)}: {len(written)} file(s) updated")
    sys.exit(1 if bad else 0)


def cmd_doctor(args):
    from . import doctor
    n = doctor.report()
    print(f"\n{n} problem(s)" if n else "\nready to produce")
    sys.exit(1 if n else 0)


def cmd_status(args):
    for name in [args.printer] if args.printer else printers.PRINTERS:
        print(name, moonraker.status(printers.get(name)))


def main():
    ap = argparse.ArgumentParser(prog="farm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("job")
    r.add_argument("--upload", action="store_true")
    sb = sub.add_parser("submit")
    sb.add_argument("job")
    s = sub.add_parser("start")
    s.add_argument("target")
    ap_ = sub.add_parser("approve")
    ap_.add_argument("target")
    bl = sub.add_parser("builds")
    bl.add_argument("--state")
    t = sub.add_parser("status")
    t.add_argument("printer", nargs="?")
    w = sub.add_parser("watch")
    w.add_argument("run_dir")
    w.add_argument("--until", choices=["next", "event"], default="next",
                   help="next: stop at first layer done too; event: only pause/finish/error")
    a = sub.add_parser("audit")
    a.add_argument("printer", nargs="?")
    sub.add_parser("doctor")
    cf = sub.add_parser("config")
    cf.add_argument("action", choices=["list", "show", "stage", "apply", "rollback", "cancel"])
    cf.add_argument("id", nargs="?", type=int)
    cf.add_argument("--printer")
    cf.add_argument("--path", default="printer.cfg")
    cf.add_argument("--section")
    cf.add_argument("--option")
    cf.add_argument("--value")
    cf.add_argument("--file", help="stage this whole file as the new content")
    cf.add_argument("--reason", default="")
    cf.add_argument("--author", help="who stages it (an AI session: --author agent)")
    bk = sub.add_parser("backup")
    bk.add_argument("printer", nargs="?")
    bk.add_argument("--to-repo", action="store_true")
    pc = sub.add_parser("presets")
    pc.add_argument("action", choices=["check"])
    wk = sub.add_parser("worker")
    wk.add_argument("--once", action="store_true", help="run pending tasks, then exit")
    wk.add_argument("--alive", action="store_true", help="exit 0 if the worker's heartbeat is fresh")
    d = sub.add_parser("db")
    d.add_argument("action", choices=["init", "import-runs"])
    m = sub.add_parser("monitor")
    m.add_argument("--state", default=str(Path.home() / ".cache" / "farm-monitor.json"))
    v = sub.add_parser("serve")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=10111)
    args = ap.parse_args()
    if args.cmd == "serve":
        from . import web
        return web.serve(args.host, args.port)
    {"run": cmd_run, "start": cmd_start, "status": cmd_status, "watch": cmd_watch, "audit": cmd_audit, "monitor": cmd_monitor, "doctor": cmd_doctor, "db": cmd_db, "presets": cmd_presets, "worker": cmd_worker, "submit": cmd_submit, "approve": cmd_approve, "builds": cmd_builds, "backup": cmd_backup, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    main()
