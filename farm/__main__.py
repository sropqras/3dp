"""python -m farm <command>

  run <job.json> [--upload]   generate (likhan) -> slice -> validate [-> upload]; writes runs/<stamp>_<job>/
  start <run-dir>             start an uploaded run on its printer (visual gate: bed clear, base colour loaded)
  watch <run-dir>             follow a started run; stops at first layer / colour change / finish / error
  status [printer]            show printer state
  audit [printer]             check live Klipper configs against the farm baseline (read-only)
  doctor                      is this host ready to produce: slicer version, maker apps, fonts (read-only)
  monitor [--state FILE]      one pass of the serious-error monitor; prints new alerts as '<loud|silent>\t<text>'
  serve [--port 10111]        farm control room: live printers, cameras, spools, jobs, batch progress
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from . import audit, config, gcode, likhan, monitor, moonraker, printers, project, slicer, watch
from .runlog import Run


def cmd_run(args):
    job_path = Path(args.job)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    printer = printers.get(job["printer"])
    if printer.readonly:
        sys.exit(f"{printer.name} is watch-only (readonly in farm/printers.py); no jobs are sent to it")
    run = Run(job["name"])
    with run.guard():
        shutil.copy(job_path, run.path("job.json"))
        run.log(f"run: {job['name']} on {printer.name} -> {run.dir}")

        params = dict(job["likhan"])
        # size pieces for the printer it will actually print on, not the form default
        params["bed_width_mm"] = printer.bed_max[0] - printer.bed_min[0]
        params["bed_depth_mm"] = printer.bed_max[1] - printer.bed_min[1]
        font = config.FONTS_DIR / job["font"] if job.get("font") else None
        change_zs = [params["base_thickness_mm"] if z == "base_top" else float(z) for z in job.get("colour_changes", [])]

        # "plates": several labels on ONE bed (one STL each, arranged together); else one design, one or more
        # auto-split pieces, each its own bed
        if job.get("plates"):
            # a plate is a label, or {"text": ..., <likhan overrides>} e.g. a smaller font_size_mm for the CH row
            plates = [pl if isinstance(pl, dict) else {"text": pl} for pl in job["plates"]]
            stls = [s for pl in plates
                    for s in likhan.generate(run, config.LIKHAN_URL, {**params, **pl}, str(font) if font else None)]
            beds = [stls]
        else:
            beds = [[s] for s in likhan.generate(run, config.LIKHAN_URL, params, str(font) if font else None)]

        outputs = []
        for bed in beds:
            out, cmd, colour_layers = slicer.slice_model(run, printer, bed, job["material"], job["layer_height"], change_zs)
            gcode.validate(run, out, printer, change_zs, max_flow=job.get("max_volumetric_speed", 13))
            project.export_project(run, cmd, out, colour_layers, job.get("colour_hex", []))
            outputs.append(out)

        if args.upload:
            remotes = [moonraker.upload(run, printer, out, job.get("upload_folder", "farm")) for out in outputs]
            run.manifest["uploaded"] = remotes
        run.manifest["result"] = "ok"
        run.manifest["colours"] = job.get("colours", [])
        run.save()
        run.log(f"run: done. Next gate: load '{(job.get('colours') or ['?'])[0]}', clear the bed, then "
                f"python -m farm start {run.dir}")


def cmd_start(args):
    run_dir = Path(args.run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    job = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
    remotes = manifest.get("uploaded") or sys.exit("run was not uploaded (use run --upload)")
    if len(remotes) != 1:
        sys.exit(f"{len(remotes)} pieces uploaded; start them one at a time from Fluidd")
    printer = printers.get(job["printer"])
    if printer.readonly:
        sys.exit(f"{printer.name} is watch-only; not starting")
    run = Run.resume(run_dir)
    # farm baseline: log every failing rule so a known lesson is never silently missing on this printer
    fails = [(r, d) for st, r, d in audit.rules(audit._settings(printer), printer) if st != "PASS"]
    for rule, detail in fails:
        run.log(f"audit: FAIL {rule} -- {detail}")
    run.step("audit", fails=fails)
    moonraker.start(run, printer, remotes[0])


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
    s = sub.add_parser("start")
    s.add_argument("run_dir")
    t = sub.add_parser("status")
    t.add_argument("printer", nargs="?")
    w = sub.add_parser("watch")
    w.add_argument("run_dir")
    w.add_argument("--until", choices=["next", "event"], default="next",
                   help="next: stop at first layer done too; event: only pause/finish/error")
    a = sub.add_parser("audit")
    a.add_argument("printer", nargs="?")
    sub.add_parser("doctor")
    m = sub.add_parser("monitor")
    m.add_argument("--state", default=str(Path.home() / ".cache" / "farm-monitor.json"))
    v = sub.add_parser("serve")
    v.add_argument("--host", default="0.0.0.0")
    v.add_argument("--port", type=int, default=10111)
    args = ap.parse_args()
    if args.cmd == "serve":
        from . import web
        return web.serve(args.host, args.port)
    {"run": cmd_run, "start": cmd_start, "status": cmd_status, "watch": cmd_watch, "audit": cmd_audit, "monitor": cmd_monitor, "doctor": cmd_doctor}[args.cmd](args)


if __name__ == "__main__":
    main()
