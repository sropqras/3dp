# The farm standard: one macro library, one Fluidd layout

Ironman asked on 2026-10-23 for "ancestral" G-code macros and Fluidd front ends, so that functions and tuning macros
look and behave the same on every printer.

## What was there

All six Klipper instances share macros from one ancestor file, but each copy had its numbers written in, and the
copies have drifted:

| Macro | Variants | What differs |
|---|---|---|
| HEAT | 3 | nozzle temperature: 220 / 210 / 200 |
| PID_EXTRUDER | 4 | target temperature hard-coded (one takes a parameter) |
| PID_BED, BED_MESH | 2–3 | bed temperature: 70 / 60 |
| NOZZLE_MID, CALIBRATE_Z_UI | 1 | X110 Y110 / X150 Y150, even on the 300 mm MaxNeo |
| _DISABLE_FS / _ENABLE_FS | 3 | sensor name; the MaxNeo's are commented out on purpose (no sensor fitted) |
| M900, M205 | 1 | present on only two printers, so PrusaSlicer's M205 is an `Unknown command` elsewhere |

## The library

**The shared file.** `printers/standard/farm_macros.cfg` is the same bytes on every printer. It holds:
- the shared utility macros: FARM_INFO, COOL, HEAT, NOZZLE_MID, DISABLE_STEPPERS;
- the bed macros: BED_MESH, BED_TRAMMING_2, CALIBRATE_Z_UI;
- tuning: PID_EXTRUDER, PID_BED, and new TUNE_PRESSURE_ADVANCE, TUNE_TEMPERATURE and TUNE_INPUT_SHAPER;
- the filament-sensor guards;
- M205 and M900.

**Per-printer values.** These live in `[gcode_macro _FARM_PRINTER]` in the printer's own `macros.cfg`:
`nozzle_temp`, `bed_temp`, `pid_nozzle`, `pid_bed` and `sensor`. They are read from the printer's current macros
when the library goes on, so nothing changes behaviour except the "middle of the bed" moves, which now use the real
bed centre.

**Formatting.** Every description starts with its group: `Print |`, `Filament |`, `Bed |`, `Tune |` or `Utility |`.
The Fluidd layout uses these groups.

**Printer-specific macros stay in each printer's own files.** These are START_PRINT, PRINT_END, M600,
LOAD/UNLOAD/PURGE and PAUSE/RESUME/CANCEL, and the farm audit rules check them.

**Version.** `FARM_INFO`'s `variable_version` is the library version.

## Installing and upgrading

The audit rule **"farm macro library is current"** is a WARN, so `farm audit` still passes without the library. It
has a generated fix (`farm/standard.py` `install`, through `farm/baseline.py`). Stage fix produces two changes:
1. the library file, as a new file;
2. `macros.cfg`, which gets the include, the values block, and every same-named old copy removed from the files
   Klipper loads.

**Order.** The gate refuses to apply change 2 before change 1, because Klipper would fail on the missing file.

**Upgrading.** Edit the library in the repo, bump `variable_version`, and Stage fix again. The printer's existing
`_FARM_PRINTER` values are kept.

**Other fixes stay out of the way.** Once the library is included, the colour-change fixes no longer write their own
`_DISABLE_FS`/`_ENABLE_FS`.

## Fluidd layout

Fluidd keeps its settings per instance in Moonraker's database (namespace `fluidd`). The farm layout:
- **Macro panel:** categories Print / Filament / Bed / Tune / Utility, assigned by description group or by the
  ancestral name. Bed, Tune, Utility and Filament macros are disabled while printing. A person's alias, colour and
  hidden flags are kept.
- **Temperature presets:** one per farm filament ("Farm PETG 240/80", "Farm PLA 210/70", from the profiles) go in
  front of the printer's own presets.
- **Left alone:** each printer's own theme, colour and logo, so they stay recognisable.

**Applying it.** This is a person's action: Printers tab → card → *Fluidd layout (farm standard)* → Apply. It changes
no printer config and needs no restart.

**Undo.** What it replaced is kept in `ui_pushes` (migration 9), and Undo puts it back.

**Watch-only printers are refused**, so EnderV3SE keeps its own layout.

## Status, 2026-10-23

The library is staged, not applied:

| Printer | Changes |
|---|---|
| Ender3MaxNeo | 9 → 10 |
| s1pro | 11 → 12 |

enders1plus gets it from **Stage all fixes** once it is back on and its changes 1 and 3 are applied. The Fluidd
layout hasn't been pushed anywhere yet.
