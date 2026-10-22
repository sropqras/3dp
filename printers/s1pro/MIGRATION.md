# s1pro: migrating from its own SBC (s1propad, 192.168.1.114) to an instance on enders3dp

Configs in this folder were pulled from s1propad over Moonraker's file API on 2026-10-23 (secrets and Klipper
auto-backups excluded). Status: **backed up only - nothing changed on either box yet.**

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

## Decisions (Amir, 2026-10-23)
- **Own Klipper for s1pro**: a second checkout on enders3dp pinned to v0.13.0-770-gce7002bed with its own
  Python env, used only by the s1pro instance. No reflash; the running instances' shared Klipper is untouched.
- **All G-code files** (135, 1.9 GB) are copied over Moonraker's API, keeping folders.

## Steps (each needs the printers it touches idle; nothing restarts the existing instances)
1. SSH from aali-cli: `ssh-copy-id -i ~/.ssh/enders3dp.pub aali@enders3dp.local` (Amir, once).
2. On enders3dp: inspect the orphan `s1pro` service and `~/s1pro_data` (if any) before reusing the slot.
3. `git clone` Klipper to `~/klipper-s1pro`, checkout `ce7002bed`; venv `~/klippy-env-s1pro`.
4. `~/s1pro_data/{config,gcodes,logs,comms}`; configs from this folder with the SBC-only sections removed,
   serial left as a placeholder until the board is plugged into enders3dp.
5. Moonraker instance on the next free port (enders3dp uses 7125-7128 -> 7129), systemd units (sudo, Amir).
6. Cut-over (printer idle, no filament): stop klipper on s1propad, move the USB cable, set the by-path serial,
   start the instance, `farm audit s1pro` read-only, then farm-baseline the config (idle timeout, CLEAR_PAUSE,
   guided M600, paused-safe idle).
7. Copy the G-code files; add s1pro to farm/printers.py + FARM.md; Fluidd link on enders3dp.
