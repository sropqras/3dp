# s1pro: migrating from its own SBC (s1propad, 192.168.1.114) to an instance on enders3dp

Configs in this folder were pulled from s1propad over Moonraker's file API on 2026-10-23 (secrets and Klipper
auto-backups excluded). Status (2026-10-23): **staged on enders3dp; waiting for Ironman's sudo step, then the cable cut-over.** The SBC's files are in `from-s1propad/`; this folder is now the enders3dp instance (backup-configs.sh mirrors `~/printer_s1pro_data` here).

Facts from s1propad:
- Klipper **v0.13.0-770-gce7002bed** on Armbian (bookworm); MCU STM32F401 flashed with the same version.
  enders3dp runs **v0.12.0-418**: the board won't talk to it as-is (host/MCU versions must match).
- `[mcu]` serial is a generic CH340 (`usb-1a86_USB_Serial-if00-port0`): on enders3dp use the
  `/dev/serial/by-path/...` of the USB port it ends up on, like the other instances.
- SBC-only, dropped on enders3dp: `[mcu host]` + `[temperature_fan raspberry_pi]` (the SBC's own fan),
  `[temperature_sensor S1ProPad]`, KlipperScreen, klipper-mcu.
- ADXL is a USB RP2040 (`adxl.cfg`, include commented out): moves as is.
- Not farm-baseline yet: `idle_timeout: 99999999`, no CLEAR_PAUSE/guided M600/paused-safe idle (FARM.md).
- enders3dp already has an orphan `s1pro` Klipper service with no config (FARM.md) - the natural slot.

## Decisions (Ironman, 2026-10-23)
- **Own Klipper for s1pro**: a second checkout on enders3dp pinned to v0.13.0-770-gce7002bed with its own
  Python env, used only by the s1pro instance. No reflash; the running instances' shared Klipper is untouched.
- **All G-code files** (135, 1.9 GB) are copied over Moonraker's API, keeping folders.

## Steps (each needs the printers it touches idle; nothing restarts the existing instances)
1. SSH from aali-cli: `ssh-copy-id -i ~/.ssh/enders3dp.pub aali@enders3dp.local` (Ironman, once).
2. On enders3dp: inspect the orphan `s1pro` service and `~/s1pro_data` (if any) before reusing the slot.
3. `git clone` Klipper to `~/klipper-s1pro`, checkout `ce7002bed`; venv `~/klippy-env-s1pro`.
4. `~/s1pro_data/{config,gcodes,logs,comms}`; configs from this folder with the SBC-only sections removed,
   serial left as a placeholder until the board is plugged into enders3dp.
5. Moonraker instance on the next free port (enders3dp uses 7125-7128 -> 7129), systemd units (sudo, Ironman).
6. Cut-over (printer idle, no filament): stop klipper on s1propad, move the USB cable, set the by-path serial,
   start the instance, `farm audit s1pro` read-only, then farm-baseline the config (idle timeout, CLEAR_PAUSE,
   guided M600, paused-safe idle).
7. Copy the G-code files; add s1pro to farm/printers.py + FARM.md; Fluidd link on enders3dp.

## Done so far (2026-10-23)
- enders3dp: `~/klipper-s1pro` pinned at ce7002bed (upstream mainline, = the SBC's), `~/klippy-env-s1pro` from its
  klippy-requirements (adds msgspec); chelper compiled. Shared `~/klipper`/`~/klippy-env` untouched.
- `~/printer_s1pro_data/config`: printer.cfg = the SBC's with the SBC-only sections removed, by-path serial
  placeholder, gcodes path, farm baseline (idle timeout paused-safe 30 min + 12 h cap, recover_velocity 150,
  max_velocity 200 / max_accel 3000 instead of 1000 / 4800 - audit limits; raise later per printer if wanted).
  SAVE_CONFIG block (PID, input shaper mzv 61.2/40.2, mesh, z_offset 3.340) kept verbatim.
  macros.cfg = enders1plus's farm-baseline macros (guided M600, CLEAR_PAUSE, _PAUSE_HARD_STOP, RESUME reheat);
  its [screws_tilt_adjust] dropped (S1 Plus positions). KAMP + fluidd.cfg symlinked like the other instances.
  moonraker.conf from the MaxNeo template on port 7129.
- `~/s1pro-install.sh` (sudo, Ironman): drop-in so klipper-s1pro runs the new checkout/venv, moonraker-s1pro unit,
  enable + start. Touches only s1pro.

## Migrated 2026-10-23 (SBC cooled, Klipper stopped on s1propad at Ironman's request; Moonraker left up for the copy)
- Ironman ran `~/s1pro-install.sh`: klipper-s1pro runs ~/klipper-s1pro (v0.13.0-770) via its drop-in; moonraker-s1pro on :7129.
- **G-code: all 135 files (1.92 GB, folder `PETG/`)** copied over Moonraker's API into `~/printer_s1pro_data/gcodes`,
  modification times kept, every size verified on enders3dp.
- **Print history: 297 jobs (2024-12-28 .. 2026-09-24) + totals** (295 jobs, 1000 h printing, 3.7 km filament,
  longest 28.4 h) inserted into s1pro's Moonraker DB (job ids kept). Export kept in `from-s1propad/history.json`.
- **Fluidd settings** (layout, macros, sensors, charts, console, uiSettings "Ender S1 Pro") and the timelapse config
  copied through the database API. Webcam entries skipped (they point at the SBC's camera).
- **Timelapse videos** (44, 1.03 GB) copied to `~/printer_s1pro_data/timelapse`.
- Lesson: enders3dp's units (KIAUH template) have `RemainAfterExit=yes`, so a stopped Klipper/Moonraker is NOT
  restarted by systemd despite Restart=always - restart them with `sudo systemctl restart <unit>`, never by killing.

## Left: the cut-over (Ironman at the printer)
USB cable SBC -> enders3dp; set `serial:` to the new /dev/serial/by-path port; restart klipper-s1pro (sudo);
check no MCU version error; `farm audit s1pro`; add s1pro + profile `s1pro-petg-020` (presets staged in
prusa/farm_profiles.ini) to the farm; sampling print through review + Start.

## Cut-over done 2026-10-23 02:50
USB cable on enders3dp hub port 3.3 (`/dev/serial/by-path/pci-0000:00:14.0-usb-0:3.3:1.0-port0`). First
FIRMWARE_RESTART: "Failed automated reset" (the board was still configured from the SBC session); the second one
connected: MCU v0.13.0-770-gce7002bed = host. Ready, no config warnings, no console errors, farm audit 15/15,
RunoutSensor sees filament. Added to farm/printers.py + profile `s1pro-petg-020`; a sample plate dry-sliced and
validated. The MaxNeo (hub port 3.2, paused) and V3SE were checked before and after: undisturbed.
Camera: not detected on enders3dp yet; `~/s1pro-camera.sh <by-id>` (sudo) is staged for it.
