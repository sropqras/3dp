# Handoff: the farm on aali-cli (192.168.1.107)

Current as of 2026-10-23. aali-cli is the only place work happens: the Windows workstation no longer pushes and must
`git pull` before any use. Branch `main` of `sropqras/3dp`.

## What runs

| What | Where |
|---|---|
| **Farm Control** (web UI) | http://192.168.1.107:10111 (Tailscale http://100.75.3.107:10111). LAN only, no auth. jobctl `farm-web` keeps it up |
| Worker (generate, plan, slice, upload after approval, tracker) | jobctl `farm-worker` |
| Serious-error + health monitor (ops Telegram bot) | jobctl `farm-monitor`, every 2 min |
| Nightly config snapshots + Inventory refresh + NAS catalogue | jobctl `farm-backup`, 03:10 |
| Maker apps | likhan :4111, lithophane :5111, namakdani :6111, lamp/plaque :8111, holder :9111 |
| Slicer | Flathub PrusaSlicer 2.9.6 (the default on Linux; `python3 -m farm doctor` says "ready to produce") |

After a `git pull`: `~/agent_workspace/infrastructure/farm_control/stop.sh && worker_stop.sh`, then
`jobctl run farm-web` and `jobctl run farm-worker` (or wait 5 min for the keep-alive jobs).

## The tabs

- **Dashboard**: live printers and cameras, Review & queue (3D view → Approve → Start checklist), jobs.
  - A switched-off printer shows "switched off", not an error. **Reconnect** after switching it on.
- **Projects**: large orders as groups → parts × qty (`docs/FARM_PROJECTS.md`).
  - The Qabrastaan batch is the first project.
  - Select parts, then **Generate** (from a template) or **Send to printer**. Both only prepare: every bed waits in Review & queue.
- **Inventory**: every design the farm has made or holds, grouped by printer, project or maker.
  - Shows each design's prints (with Good/Bad marks) and its portfolio product.
  - The **NAS catalogue** matrix lists `\\OMV\3dprinting\3dprinting\Project Files` against the printers, with a one-file **Import**.
- **Spools**: spool stock. Prints deduct from it automatically.
- **Printers** (`/printers`): fleet health, audit with **Stage fix / Stage all fixes**, gated Apply and rollback.
  - Also: Telegram pack, service restarts, maintenance, onboarding (`docs/PRINTERS_TAB.md`).

## Printers

| Printer | Moonraker | Notes |
|---|---|---|
| Ender3MaxNeo | 192.168.1.184:7125 (enders3dp) | 0.6 Sprite, PETG plates |
| s1pro | 192.168.1.184:7129 (enders3dp) | own Klipper v0.13 instance, 0.4 nozzle (moved off its SBC 2026-10-23) |
| enders1plus | 192.168.1.68:7125 (3dpnas) | 0.6 Sprite, shares G-code with the MaxNeo |
| EnderV3SE | 192.168.1.184:7128 (enders3dp) | Amir's own production printer: **watch-only, hands-off** |

The registry is `printers/registry.json`, edited from the Printers tab only.

## To do (in order)

1. **Apply the staged printer changes** (Printers tab → Changes → Apply, each while that printer is idle and switched on):
   - enders1plus 1 (`max_velocity` 200) and 3 (tgalarm line), then **Telegram pack**;
   - Ender3MaxNeo 4 (cold-nozzle heating for LOAD/UNLOAD/PURGE);
   - s1pro 7 (cold-nozzle heating + the undefined BEEP).

   Each Apply verifies itself and rolls back if Klipper doesn't come back.
2. **3dpnas SSH** for the USB device listing: `ssh-copy-id -i ~/.ssh/enders3dp.pub aali@3dpnas.local`.
3. **Confirm values**:
   - the Hyper PLA volumetric cap (15 proposed; `neo-hyperpla-028-litho` and `neo-pla-020-supports` stay unconfirmed until then);
   - lithophane t_min/t_max at 0.6;
   - the Qabrastaan plate count (635 listed vs 580 expected).
4. **First supported print** (holder): `neo-pla-020-supports` slices for review but refuses upload until a person has
   printed one and confirmed the profile.
5. **Phase 7 (invtool intake)** and **phase 8 (EnderV3SE onboarding)** in `docs/ARCHITECTURE.md` wait for Amir's
   go-ahead.
6. **Telegram bots** for EnderV3SE / Ender3Pro / EnderCR10SE on enders3dp crash-loop (tokens rejected by Telegram).
7. **Filament** for the full Qabrastaan batch: ~40 kg plate colour + ~16 kg letter colour (+5-10 %).

## Rules

`CLAUDE.md` is the authority. In short:
- agents never approve or start prints;
- printer configs change only through the Printers tab's gated Apply (agents may stage);
- EnderV3SE is hands-off;
- the Charles Wright font is never committed or served;
- read `gcode_store` after any macro change.
