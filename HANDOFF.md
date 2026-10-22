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
