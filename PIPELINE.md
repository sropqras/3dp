# Production pipeline

`job.json` → **likhan** `/generate` → STL → **PrusaSlicer CLI** (printer presets) → colour changes → **checks** → **Moonraker** upload → visual gate → start

```bash
python -m farm status                         # printer state
python -m farm run jobs/qabrastaan/A01.json   # generate + slice + check (no printer contact)
python -m farm run jobs/qabrastaan/A01.json --upload
python -m farm start runs/<stamp>_<job>       # only after the visual gate
python -m farm watch runs/<stamp>_<job>       # follow it: first layer / colour change / finish / error (monitor.log)
```

Python 3.10+, standard library only. Every run writes `runs/<stamp>_<job>/`:
`run.log` (what happened), `manifest.json` (machine-readable steps), `job.json`, the STL, `presets/` (exact
slicer inputs), `slicer.log`, and the G-code.

## Stages

| Stage | Module | What it guarantees |
|---|---|---|
| Generate | `farm/likhan.py` | Model sized for the target printer's bed (job bed settings are overridden from `farm/printers.py`) |
| Slice | `farm/slicer.py` | Presets extracted from `prusa/*.ini`; first layer adjusted so every colour change lands exactly on a layer top; `M600` inserted before the first layer above it |
| Project | `farm/project.py` | `<job>.3mf` for the visual gate: same geometry and resolved config as the G-code, colour changes on the layer slider |
| Check | `farm/gcode.py` | One `START_PRINT` with no slicer temps before it, `PRINT_END` present, M600 count matches the job, volumetric cap set and ≤ job max, extrusion inside the bed, height ≤ printer max |
| Upload / start | `farm/moonraker.py` | Upload to `gcodes/<upload_folder>/`; start refuses unless Klipper is ready, not printing, not paused |

## Visual gates (human)

1. **Open the run's `.3mf` in PrusaSlicer** (Preview tab) and check the model, colours and layer slider; then **before start**: bed clear, first colour loaded, nozzle clean.
2. **At the colour change**: Fluidd shows the LOAD / PURGE / RESUME prompt (`M600` on printers with `has_m600_prompt`).
3. **First layer**: watch the first layer go down; cancel from Fluidd if it isn't sticking.

## Host config (env vars)

| Variable | Default | Example on aali-cli |
|---|---|---|
| `FARM_PRUSASLICER` | `C:\Program Files\Prusa3D\PrusaSlicer\prusa-slicer-console.exe` | `flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer` |
| `FARM_LIKHAN_URL` | `http://192.168.1.107:4111` | `http://127.0.0.1:4111` |
| `FARM_FONTS_DIR` | `assets/fonts/` (gitignored, licensed fonts) | same |

## Known quirks

- PrusaSlicer 2.9.6 CLI segfaults on `*_extruder = 0` keys (e.g. `bed_temperature_extruder`, `wipe_tower_extruder`); `slicer.extract_preset` strips them.
- Licensed fonts (Charles Wright) are never committed: put the `.otf` in `assets/fonts/`.
