# Handoff: run the farm pipeline + control room on aali-cli (192.168.1.107)

Written 2026-10-22 by the Claude session that built it on the user's Windows workstation. For the Claude/operator on
aali-cli. Branch: `farm-config-backup` of `sropqras/3dp`.

## What's here

| Path | What |
|---|---|
| `farm/` | `python -m farm` - production pipeline (likhan -> PrusaSlicer -> M600 -> checks -> Moonraker), `watch`, `audit`, `serve` |
| `farm/web.py`, `farm/static/index.html` | **Farm Control** web UI: live printers + cameras, spool inventory, jobs, batch progress grid |
| `jobs/qabrastaan/` | Qabrastaan grave-plate jobs and `batch.json` (rows A-R + children, 635 plates) |
| `prusa/MaxNeo_PETG_0.6_profiles.ini` | PrusaSlicer presets the pipeline slices with (MaxNeo + S1 Plus, Sprite 0.6, PETG 13 mm3/s) |
| `printers/<Name>/` | Klipper config backups per printer (secrets excluded) |
| `FARM.md`, `PIPELINE.md` | Printer list, feature matrix, change log; pipeline stages and quirks |

Python 3.10+, standard library only.

## Setup on aali-cli

```bash
git clone https://github.com/sropqras/3dp.git && cd 3dp && git checkout farm-config-backup
# PrusaSlicer 2.9.6 CLI (Linux = Flathub)
flatpak install -y flathub com.prusa3d.PrusaSlicer
export FARM_PRUSASLICER="flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer"
export FARM_LIKHAN_URL="http://127.0.0.1:4111"          # likhan-maker on this host
# licensed font, NOT in git: copy from the workstation D:\repos\sropqras\3dp\assets\fonts\CharlesWright-Bold.otf
mkdir -p assets/fonts && cp /path/to/CharlesWright-Bold.otf assets/fonts/
python3 -m farm status && python3 -m farm audit           # both printers reachable, see baseline gaps
```

likhan-maker must run branch `add-charles-wright-font` (fixed plate size + font option) - see that repo's HANDOFF.md.
Until then, jobs with `base_width_mm` fail at likhan with a 400.

## Control room as a service

```ini
# ~/.config/systemd/user/farm-control.service
[Unit]
Description=Farm Control (python -m farm serve)
After=network-online.target

[Service]
WorkingDirectory=%h/3dp
Environment=FARM_PRUSASLICER=flatpak run --command=PrusaSlicer com.prusa3d.PrusaSlicer
Environment=FARM_LIKHAN_URL=http://127.0.0.1:4111
ExecStart=/usr/bin/python3 -m farm serve --host 0.0.0.0 --port 8090
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now farm-control
loginctl enable-linger "$USER"     # keep it running without a login session
# open http://192.168.1.107:8090
```

LAN only, no authentication: don't expose port 8090 outside the network. The spool inventory is stored in
`data/inventory.json` (gitignored; seeded from `data/inventory.example.json` on first start).

## Printers it talks to

| Printer | Moonraker |
|---|---|
| Ender3MaxNeo | http://192.168.1.184:7125 (host enders3dp) |
| enders1plus | http://3dpnas.local:7125 (192.168.1.68) |

`scripts/backup-configs.sh` needs SSH to enders3dp (key `~/.ssh/enders3dp`, alias in `~/.ssh/config`); set that up
on aali-cli the same way if backups should run from there.

## Rules (from FARM.md / the session)

- Never restart Klipper or edit configs on a printer that is printing or paused.
- Run `python -m farm audit` before onboarding or switching a printer; add a rule to `farm/audit.py` for every new lesson.
- `farm start` only after the human visual gate (open the run's `.3mf`, bed clear, first colour loaded).

## State at handoff (2026-10-22 23:15) and what's next

Printing when the workstation session closed (printers run independently; nothing below needs the workstation):

| Printer | Job | Next event |
|---|---|---|
| Ender3MaxNeo | CH02+A11 (black SP-003), started 23:06, ~3 h 40 m | colour change ~01:00: Fluidd prompt + Telegram tgalarm_photo |
| enders1plus | R50+Q52 (black SP-001), started 22:25, ~3 h 56 m | colour change ~00:30: Fluidd prompt only (its Telegram alerts are staged, not live) |
| EnderV3SE | user's own job | watch-only, never touched |

Letter-colour spool SP-002 is on the shelf, shared by both colour changes. A pause is safe overnight on both printers:
nozzle off after 30 min, bed + motors hold, 12 h cap, RESUME reheats.

The run folders for these jobs (`runs/...`) exist only on the workstation, so a Farm Control started on aali-cli shows
the printers live but cannot link these two jobs to their runs (no colour-change countdown for them). New runs made
on aali-cli link normally. `data/inventory.example.json` holds the real spool stock as of the handoff.

### To do (in order)

1. **enders1plus after its print**: deploy the staged config (FARM.md "Staged, not yet on the printer") via Moonraker's
   file API (no SSH to 3dpnas), FIRMWARE_RESTART, check the console shows the prompt, then `python -m farm audit enders1plus`
   must pass. Then apply `farm/telegram_pack.conf` to its bot (telegram.conf via the file API, keep [bot]).
2. **likhan-maker on aali-cli**: deploy branch `add-charles-wright-font` + the licensed font (its HANDOFF.md). Until then
   jobs with fixed plates / monospace fail at likhan.
3. **Confirm with the user**: plate count (635 listed vs 580 expected), letter colour, colours of sealed spool SP-004.
4. **Telegram tokens** for EnderV3SE / Ender3Pro / EnderCR10SE bots on enders3dp (rejected by Telegram; crash-looping).
5. **Filament**: the full batch needs ~40 kg plate colour + ~16 kg letter colour (+5-10 %); 3.8 kg on hand.
6. **Spool auto-deduction** in Farm Control (split grams at the M600: plate colour before, letter colour after).
