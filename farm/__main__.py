"""python -m farm <command>

  run <job.json> [--upload]   generate (likhan) -> slice -> validate [-> upload]; writes runs/<stamp>_<job>/
  start <run-dir>             start an uploaded run on its printer (visual gate: bed clear, base colour loaded)
  status [printer]            show printer state
"""
import argparse
import json
import shutil
import sys
from pathlib import Path

from . import config, gcode, likhan, moonraker, printers, project, slicer
from .runlog import Run


def cmd_run(args):
    job_path = Path(args.job)
    job = json.loads(job_path.read_text(encoding="utf-8"))
    printer = printers.get(job["printer"])
    run = Run(job["name"])
    shutil.copy(job_path, run.path("job.json"))
    run.log(f"run: {job['name']} on {printer.name} -> {run.dir}")

    params = dict(job["likhan"])
    # size pieces for the printer it will actually print on, not the form default
    params["bed_width_mm"] = printer.bed_max[0] - printer.bed_min[0]
    params["bed_depth_mm"] = printer.bed_max[1] - printer.bed_min[1]
    font = config.FONTS_DIR / job["font"] if job.get("font") else None
    stls = likhan.generate(run, config.LIKHAN_URL, params, str(font) if font else None)

    change_zs = [params["base_thickness_mm"] if z == "base_top" else float(z) for z in job.get("colour_changes", [])]
    outputs = []
    for stl in stls:
        out, cmd, colour_layers = slicer.slice_model(run, printer, stl, job["material"], job["layer_height"], change_zs)
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
    run = Run.resume(run_dir)
    moonraker.start(run, printer, remotes[0])


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
    args = ap.parse_args()
    {"run": cmd_run, "start": cmd_start, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
