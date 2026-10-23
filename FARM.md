# 3D Print Farm

Klipper farm host `enders3dp` (192.168.1.184). One Klipper + Moonraker instance per printer.
Configs are pulled into `printers/<Name>/` with `scripts/backup-configs.sh` (secrets excluded).
Slicer profiles (PrusaSlicer 2.9.6) live in `prusa/`.

## Printers

| Printer | Moonraker | USB | Status |
|---|---|---|---|
| Ender3MaxNeo | :7125 | connected | ready |
| EnderV3SE | :7128 | connected | in production; handed over to the farm 2026-10-23 (changes only while idle) |
| Ender3Pro | :7126 | not connected | idle instance |
| ~~EnderCR10SE~~ | ~~:7127~~ | – | **not a printer**: the S1 Plus's old instance (bed 316×304×300, Jan 2025 calibration runs only). The S1 Plus runs on 3dpnas (enders1plus). Retired 2026-10-23: no longer watched; configs and history in `printers/_archive/EnderCR10SE/`; services removed by Ironman with KIAUH |
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
| enders1plus | changes **18** (farm_macros.cfg) → **19** (printer.cfg max_velocity 200) → **20** (macros.cfg: library include + values, tgalarm, cold-nozzle reheat, BEEP commented); 1, 3 and the duplicate 15 folded in | when idle (on or off); then the Telegram pack |
| EnderV3SE | changes **24** → **25** (printer.cfg: recover_velocity 150, paused-safe idle keeping DELAYED_PRINTER_OFF) → **26** (macros.cfg: CLEAR_PAUSE in START_PRINT, guided M600, reheat, 12 h cap, library) | only while idle; Ironman reviews first (production printer) |

## Change log

| Date | Printer | Change |
|---|---|---|
| 2026-10-23 | all | PrintGuard (local print-failure detection) on 3dpnas:11111, alert-only trial: monitors for enders1plus, MaxNeo, V3SE and s1pro (2 fps, threshold 0.75, 3 frames, no pause). Alerts come through farm-monitor and the ops bot; Farm Control cards show a badge. Not yet: camera crops (garage dark at setup), Ender3Pro (shares the MaxNeo's `/webcam2/` stream, no camera of its own). No printer config changed |
| 2026-10-23 | EnderV3SE | Telegram fixed: the old bot token was rejected (401) and `server:` pointed at a dead address (10.0.0.123). New bot @AZ3dpe3v3seBot, `server: localhost:7128`, then the farm Telegram pack (buttons, macros, pinned status). Ender3Pro's token is cleared until it gets its own bot (its `chat_id` is wrong too) |
| 2026-10-23 | EnderV3SE | printer.cfg: edited (printer.cfg): farm baseline: pause_resume recover_velocity >= 100; idle timeout is pause-aware - applied by Ironman via Farm Control, change 25 |
| 2026-10-23 | EnderV3SE | macros.cfg: edited (macros.cfg): farm baseline: START_PRINT runs CLEAR_PAUSE; M600 does not auto-unload; M600 shows the colour-change prompt; LOAD/UNLOAD/PURGE heat a cold nozzle; 12 h pause safety cap exists; farm macro library is current - applied by Ironman via Farm Control, change 26 |
| 2026-10-23 | EnderV3SE | farm_macros.cfg: new file (farm_macros.cfg): farm baseline: farm macro library is current - applied by Ironman via Farm Control, change 24 |
| 2026-10-23 | EnderV3SE | moonraker.conf: edited (moonraker.conf): put back the Obico updater include that KIAUH's OctoEverywhere removal deleted (2026-10-23 11:50); Obico stays, only OctoEverywhere was meant to go. Takes effect at Moonraker's next restart - applied by Ironman via Farm Control, change 29 |
| 2026-10-23 | Ender3MaxNeo, Ender3Pro, EnderV3SE | OctoEverywhere removed from enders3dp with KIAUH (Ironman: drop the external dependency). KIAUH also deleted `[include moonraker-obico-update.cfg]` from their moonraker.conf (left out: 27/28 cancelled; 29 was applied on V3SE at 13:32 and is to be rolled back) and `[include moonraker_obico_macros.cfg]` + a commented shell_command include from Ender3Pro's printer.cfg (11:49:45), which is what change 8 (applied, then rolled back) had tried. Originals stay in the restore points |
| 2026-10-23 | EnderCR10SE (enders3dp:7127) | Identified as the S1 Plus's old instance, not a separate printer. Nothing migrated: all its history and G-code were calibration runs (Ironman: noise), the rest was already on 3dpnas. Staged 13/14 cancelled, port dropped from `printers/hosts.json`, configs + history archived. Services removed with KIAUH (Ironman) |
| 2026-10-23 | all | Restore points: each printer's original config pinned (`farm config origin`), host + KIAUH history archived in git (`printers/_archive/`) |
| 2026-10-23 | EnderV3SE | Farm settings: no longer watch-only; note (Farm Control, Ironman) |
| 2026-10-23 | s1pro | macros.cfg: edited (macros.cfg): farm baseline: farm macro library is current - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 12; audit pass, console clean |
| 2026-10-23 | s1pro | farm_macros.cfg: new file (farm_macros.cfg): farm baseline: farm macro library is current - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 11; audit pass, console clean |
| 2026-10-23 | Ender3MaxNeo | macros.cfg: edited (macros.cfg): farm baseline: farm macro library is current - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 10; audit pass, console clean |
| 2026-10-23 | Ender3MaxNeo | farm_macros.cfg: new file (farm_macros.cfg): farm baseline: farm macro library is current - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 9; audit pass, console clean |
| 2026-10-23 | s1pro | macros.cfg: edited (macros.cfg): farm baseline: LOAD/UNLOAD/PURGE heat a cold nozzle; no macro calls an undefined BEEP - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 7; audit pass, console clean |
| 2026-10-23 | Ender3MaxNeo | macros.cfg: edited (macros.cfg): farm baseline: LOAD/UNLOAD/PURGE heat a cold nozzle - applied through the gate by a Claude session on Ironman's instruction of 2026-10-23 (actor cli:Ironman), change 4; audit pass, console clean |
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
