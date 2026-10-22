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
