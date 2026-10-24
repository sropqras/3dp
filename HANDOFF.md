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
| Hourly Inventory + NAS sync (new NAS models → one silent Telegram note) | jobctl `farm-inventory`, :25 |
| NAS previews (thumbnail + light 3D per model, originals not kept) | jobctl `farm-nas-thumbs`, 01:30, 50 min |
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
  - The **NAS catalogue** (2.0, 2026-10-23): folder tree / farm categories, facet chips, Grid with thumbnails /
    Table / Matrix, saved views, a pack drawer with 3D preview, Import / Send / Add to project. Previews warm nightly
    (jobctl `farm-nas-thumbs`); works from the cache when OMV is off.
- **Spools**: spool stock. Prints deduct from it automatically.
- **Every page** (2026-10-24): ⚙ in the header picks the theme (System / Light / Dark), density, text and card size,
  and **Edit layout** moves, hides and collapses sections and printer cards, like Fluidd. Kept per browser.
- **Printers** (`/printers`): fleet health, audit with **Stage fix / Stage all fixes**, gated Apply and rollback.
  - Also: Telegram pack, service restarts, maintenance, onboarding (`docs/PRINTERS_TAB.md`).
  - **Farm standard** (`docs/FARM_STANDARD.md`): the shared macro library (audit WARN "farm macro library is
    current" → Stage fix) and the **Fluidd layout** panel on each card (Apply / Undo).
  - Onboarding a printer whose Klipper won't start on a missing `[include]` offers **Stage: comment out the missing
    include**.

## Printers

| Printer | Moonraker | Notes |
|---|---|---|
| Ender3MaxNeo | 192.168.1.184:7125 (enders3dp) | 0.6 Sprite, PETG plates |
| s1pro | 192.168.1.184:7129 (enders3dp) | own Klipper v0.13 instance, 0.4 nozzle (moved off its SBC 2026-10-23) |
| enders1plus | 192.168.1.68:7125 (3dpnas) | 0.6 Sprite, shares G-code with the MaxNeo |
| EnderV3SE | 192.168.1.184:7128 (enders3dp) | Ironman's own production printer, **handed over to the farm 2026-10-23** (changes only while idle; no print profile yet) |

The registry is `printers/registry.json`, edited from the Printers tab only.

## To do (in order)

0. **Printer power (Meross MSS426, 2026-10-24): built, waiting for the key and the outlet map.**
   1. In your own terminal on aali-cli: `~/agent_workspace/infrastructure/meross/get_key.sh`. It asks for your Meross login,
      saves only the device key (chmod 600), then shows the strip's outlets.
   2. Restart Farm Control: `~/agent_workspace/infrastructure/farm_control/stop.sh && …/start.sh`.
   3. Printers tab → each printer's **Farm settings** → **Power outlet** → Save.
      Outlets found 2026-10-24 (Ironman switched each on in the Meross app; the board or host that appeared was read):
      app 1 = channel 6 EnderV3SE · app 2 = ch 3 s1pro · app 3 = ch 2 Ender3MaxNeo (its board stays on through USB
      power, so it shows "ready" even with the outlet off) · app 4 = ch 5 enders1plus · app 5 = ch 1 Ender 3 V3 SE on a
      Nebula Pad (192.168.1.200, hosts.json `printer-200`, not onboarded) · app 6 = ch 4 CR-10 SE on stock Creality
      firmware (a new printer, not onboarded) · ch 7 = probably the USB ports · ch 0 = the whole strip (never mapped). The **Power strips** panel shows what
      each outlet feeds.
   4. Try it on an idle, cold printer: ⏻ off, then **Power on** (it reconnects Klipper).
1. **Apply the staged changes** (Printers tab → Changes; `FARM.md` "Staged" table). Put your name in the **You**
   box first: without it Apply stops at "Put your name in the 'You' box first". That is probably why 1/3/4/7
   didn't go on the first time.
   - 4 (MaxNeo) and 7 (s1pro) were applied 2026-10-23 by a Claude session on Ironman's explicit instruction
     (actor `cli:Ironman`). Both verified: no audit FAIL, console clean.
   - 9-12 (the macro library on MaxNeo and s1pro) were applied 2026-10-23 on Ironman's instruction: `FARM_INFO`
     answers on both and both audits pass.
   - **enders1plus 18 → 19 → 20** (library, max_velocity, macros incl. tgalarm/reheat/BEEP; 1 and 3 were folded
     in and cancelled), then the **Telegram pack**.
   - **EnderV3SE 24 → 25 → 26** (handed over 2026-10-23): library; printer.cfg recover_velocity 150 + paused-safe
     idle that keeps its own `DELAYED_PRINTER_OFF` (which calls `POWER_OFF_PRINTER`, an Unknown command today);
     macros: CLEAR_PAUSE, guided M600 prompt with tgalarm, reheat, 12 h cap, `_FARM_PRINTER` 220/70, PID 220/60,
     FilamentSensor. Review the diffs - it is your production printer.
   - (older note) **enders1plus**: no need to switch it on. The gate now applies with the board off: Klipper on 3dpnas loads the
     config, the audit runs, and a config error still rolls back. Apply **1** and **3**, then **Stage all fixes**
     (cold-nozzle reheat, BEEP, macro library) and apply those, then the **Telegram pack**.
   - **Ender3Pro**: KIAUH removed the missing `moonraker_obico_macros.cfg` include (what change 8 tried; it had been rolled back). Its Klipper needs a restart (Reconnect) to start, then the onboarding checklist.
   - ~~EnderCR10SE~~: not a printer, the S1 Plus's old instance. 13/14 cancelled. **Remove it with KIAUH** on enders3dp (see FARM.md change log), then unlink it in Obico. OctoEverywhere is removed from the farm altogether (2026-10-23).
2. **Fluidd layout**: pushed 2026-10-23 to MaxNeo, s1pro, enders1plus and EnderCR10SE (Undo on each card).
   EnderV3SE: push it after its staged changes are on. Not pushed: Ender3Pro (Klipper not running; a push now refuses when it can't read
   any macros).
   - **Power plugs**: Meross research and design in `docs/POWER.md`. It needs your plug models, IPs and the
     account key first.
3. **PrintGuard** (3dpnas:11111) is installed and set up (2026-10-23), alert-only; alerts reach you via farm-monitor.
   Left for you (`~/agent_workspace/infrastructure/printguard/PROJECT.md`):
   - camera crops, from a lit frame (the garage was dark at setup);
   - Ender3Pro has no camera (`/webcam2/` faces the MaxNeo): delete its Fluidd camera entry "XWF Ender 3 Pro", and
     give it a camera of its own before it gets a monitor;
   - optionally a dedicated Telegram bot inside PrintGuard for photo alerts;
   - after 1-2 weeks of alerts, decide on pause per printer.
4. **Confirm values**:
   - the Hyper PLA volumetric cap (15 proposed; `neo-hyperpla-028-litho` and `neo-pla-020-supports` stay unconfirmed until then);
   - lithophane t_min/t_max at 0.6;
   - the Qabrastaan plate count (635 listed vs 580 expected).
5. **First supported print** (holder): `neo-pla-020-supports` slices for review but refuses upload until a person has
   printed one and confirmed the profile.
6. **Next features** (ranked in `docs/FEATURE_GAPS.md`): cost per print/product, product SKUs with recipes, proven
   settings as the send default, suggested printer, Spoolman sync. Phase 7 (invtool) and phase 8 (EnderV3SE) still
   wait for Ironman's go-ahead.
7. **Telegram bots:** EnderV3SE fixed 2026-10-23 (new bot @AZ3dpe3v3seBot, `server: localhost:7128`, Telegram pack). Ender3Pro's token is cleared, so its bot restart-loops until a `/newbot` token goes in with `server: localhost:7126` and the right `chat_id` (copy MaxNeo's). Each printer needs its own bot; two copies of one token fight (Telegram "Conflict"). Old note: (tokens rejected by Telegram).
   The monitor now reports each one once instead of every 2 minutes.
8. **Filament** for the full Qabrastaan batch: ~40 kg plate colour + ~16 kg letter colour (+5-10 %).

## Rules

`CLAUDE.md` is the authority. In short:
- agents never approve or start prints;
- printer configs change only through the Printers tab's gated Apply (agents may stage);
- EnderV3SE: changes only while it is idle (Ironman's production printer);
- the Charles Wright font is never committed or served;
- read `gcode_store` after any macro change.
