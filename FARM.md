# 3D Print Farm

Klipper farm host `enders3dp` (192.168.1.184). One Klipper + Moonraker instance per printer.
Configs are pulled into `printers/<Name>/` with `scripts/backup-configs.sh` (secrets excluded).
Slicer profiles (PrusaSlicer 2.9.6) live in `prusa/`.

## Printers

| Printer | Moonraker | USB | Status |
|---|---|---|---|
| Ender3MaxNeo | :7125 | connected | ready |
| EnderV3SE | :7128 | connected | in production - read-only until scheduled |
| Ender3Pro | :7126 | not connected | idle instance |
| EnderCR10SE | :7127 | not connected | idle instance |
| **s1pro** (Ender 3 S1 Pro) | :7129 | connected (USB 3.3) | ready; moved off its SBC 2026-10-23, own Klipper v0.13.0-770, 0.4 nozzle, RunoutSensor, profile `s1pro-petg-020` |
| **enders1plus** (host 3dpnas.local, 192.168.1.68) | :7125 | connected | ready; Ender 3 S1 Plus, same bed + Sprite Pro 0.6 as MaxNeo, filament sensor |

Other hosts (not on enders3dp, not yet onboarded): e3propad.local, powerpad.local, s1propad.local,
192.168.1.39:4408, 192.168.1.200:4408.

## Feature matrix

✅ has · ❌ doesn't have · ? unconfirmed (don't assume from config - configs contain copy-pasted sections)

| Feature | Ender3MaxNeo | EnderV3SE | Ender3Pro | EnderCR10SE |
|---|---|---|---|---|
| Mainboard | Creality V4.2.7 (S1 Plus-like) | ? | ? | ? |
| Hotend / nozzle | Sprite Pro, V6-thread 0.6 mm | ? | ? | ? |
| Filament runout sensor | ❌ | ✅ switch (PC15) | ? | ? |
| Bed probe | ✅ CR Touch | ? | ? | ? |
| KAMP mesh + purge | ✅ | ✅ | ? | ? |
| Smart plug | ❌ | ✅ Meross MSS426 (not wired to Moonraker yet) | ? | ? |
| Accelerometer | ❌ (shaper values hardcoded) | ? | ? | ? |
| Sensorless homing | ❌ | ✅ | ? | ? |
| Colour change (M600 + prompt) | ✅ | ? | ? | ? |
| Paused-safe idle timeout | ✅ | ❌ | ❌ | ❌ |
| Telegram bot | ✅ | ❌ token rejected | ❌ token rejected | ❌ token rejected |

enders1plus (3dpnas) has its own working Telegram bot (user confirmed 2026-10-22).

## Farm-wide features (rolled out per printer, tracked above)

- **Paused-safe idle timeout** - while paused, the nozzle cools after 30 min (the pause sets SET_IDLE_TIMEOUT 1800; otherwise idle_timeout is 3600 s); bed and motors stay on so the print can resume (overnight is fine). 12 h safety cap turns the bed off too; RESUME reheats nozzle and bed.
- **Guided colour change** - `M600` parks and shows a Fluidd prompt with UNLOAD / LOAD / PURGE / RESUME buttons. No auto-unload: on printers without a runout sensor an unattended eject lets the filament end tangle on the spool.
- **CLEAR_PAUSE in START_PRINT** - a stale pause can never make PAUSE answer "Print already paused".

## Staged, not yet on the printer

| Printer | Staged in repo | Apply when |
|---|---|---|
| enders1plus | max_velocity 300 -> 200 (KAMP move); Telegram tgalarm on colour change; then the Telegram pack. Now from Farm Control → Printers: **Stage fix** on its two failing rules, Apply while idle, then **Telegram pack** | after the R50+Q52 print (~02:20 on 2026-10-23); then `python -m farm audit enders1plus` must pass |

## Change log

| Date | Printer | Change |
|---|---|---|
| 2026-10-23 | s1pro | Moved from its own SBC (s1propad) to an instance on enders3dp: own Klipper v0.13.0-770 (matches the board), configs + 135 G-codes + 297-job history + Fluidd settings migrated; farm baseline (paused-safe idle, 12 h cap, guided M600, CLEAR_PAUSE, recover 150, 200/3000 motion limits); audit 15/15 |
| 2026-10-22 | Ender3MaxNeo | Removed phantom filament sensors (btt_sfs include), CLEAR_PAUSE in START_PRINT, fixed FC_RESUME, removed duplicate screws_tilt_adjust |
| 2026-10-22 | Ender3MaxNeo | Paused-safe idle timeout, guided M600 colour change |
| 2026-10-22 | Ender3MaxNeo | 12 h pause safety cap (_PAUSE_HARD_STOP), RESUME reheats the bed if it was hit |
| 2026-10-22 | Ender3MaxNeo | M600 no longer auto-unloads; UNLOAD button in the prompt |
| 2026-10-22 | Ender3MaxNeo | Telegram farm pack (farm/telegram_pack.conf): pinned single status, silent progress, farm buttons, confirmations, risky macros hidden |
| 2026-10-22 | Ender3MaxNeo | max_velocity 200 / max_accel 3000, recover_velocity 150, KAMP purge flow 12, colour-change prompt fixed (macro naming) + anti-ooze resume; passes farm audit |
| 2026-10-22 | enders1plus | Sensor insert no longer RESUMEs; guided M600 (no auto-unload, sensor off during swap); paused-safe idle timeout + 12 h cap (was never); CLEAR_PAUSE in START_PRINT; recover_velocity 25 -> 150; anti-ooze resume |
| 2026-10-22 | farm | Multi-plate beds (one 3MF object per plate), fixed-size plates via likhan |
| 2026-10-22 | farm | Production pipeline `python -m farm` (likhan -> slice -> M600 -> checks -> Moonraker), see PIPELINE.md |
| 2026-10-22 | Ender3MaxNeo | PrusaSlicer PETG 0.6 presets with 13 mm³/s cap (`prusa/MaxNeo_PETG_0.6_profiles.ini`) |
