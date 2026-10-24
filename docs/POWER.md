# Printer power: the Meross MSS426 strip (built 2026-10-24)

The design research of 2026-10-23 (a separate bridge, one smart plug per printer) is superseded. What runs is below;
the code is `farm/power.py`, the tests `tests/test_power.py`, the rules in CLAUDE.md ("Printer power is a person's action").

## Hardware

- **One Meross MSS426 surge strip** at 192.168.1.63 (fw 4.1.1), switched over its **local** HTTP API:
  `POST http://<ip>/config`, `sign = md5(messageId + key + timestamp)`, `Appliance.Control.ToggleX`
  `{togglex: {channel, onoff}}`. The device key is the account's, fetched once by Ironman with
  `~/agent_workspace/infrastructure/meross/get_key.sh` into `farm_control/meross.env` (chmod 600). No farm code
  talks to Meross's cloud (`tests/test_no_ai.py`).
- **The app's outlet numbers are not the API's channels:**

| App outlet | Channel | Feeds |
|---|---|---|
| 1 | 6 | EnderV3SE |
| 2 | 3 | s1pro |
| 3 | 2 | Ender3MaxNeo (its board stays up on USB power, so "ready" with the outlet off) |
| 4 | 5 | enders1plus |
| 5 | 1 | NebulaV3SE (standalone) |
| 6 | 4 | CR-10 SE (stock Creality, read-only status) |
| USB | 7 | "Farm lights" (`printers/power.json`) |
| - | 0 | the whole strip: never mapped, never sent |

- **No outlet feeds a print host** (enders3dp, 3dpnas): one Off would take down every instance on it.

## Who can switch what

- **A person** maps outlets (Printers tab → Farm settings, registry `power`; named outlets with `power.map_aux`), and
  switches them: the card's ⏻ / **Power on**, the 💡 header button, `python3 -m farm power on|off`, and the printer's
  own button (below). Agents only run `farm power scan` (read-only).
- **The monitor** switches exactly one outlet, Farm lights: on while any farm printer prints, off 10 min after the last
  print, only lights it switched on itself, and only after a person's own 💡 switch read back clean (`proven`).

## The gates (`set_power` → `_toggle`)

1. **No Off anywhere while anything prints.** `farm_busy()` reads, in parallel, every registry printer, every Moonraker
   in `printers/hosts.json`, and the stock-Creality hosts in `FARM_CREALITY_HOSTS` (`farm/creality.py` listens to the
   :9999 WebSocket; unknown state codes count as printing). This also covers an outlet mapped to the wrong printer.
2. **The printer being switched off is idle:** not printing/paused, no heater target, hotend under 50 °C, Moonraker
   readable. A board that's gone counts as off only if its last print isn't printing/paused/error.
3. **One choke point:** channel ≥ 1, the strip identified by uuid **before** the SET, every outlet read before and
   after. If any other outlet changed, a **trip latch** stops all switching (loud monitor alert) until a person runs
   `farm power reset`.

## The printer's own power button (Telegram ⏻, Fluidd's toggle)

Telegram/Fluidd → the printer's Moonraker `[power printer]` (`type: http`) → `GET/POST
http://192.168.1.107:10111/api/power/device/<printer>` → `set_power(..., reconnect=False)`.

- Two locks: Moonraker's `locked_while_printing` and every farm gate above. Moonraker's
  `restart_klipper_when_powered` (delay 8 s) does the FIRMWARE_RESTART after On.
- The section comes from `power.moonraker_section(name)`, staged by `farm power stage-device <printer>` as an ordinary
  config change on `moonraker.conf`. A person applies it **with the printer switched on and idle**: applying restarts
  Moonraker and the gate waits for Klipper "ready" (change 30 rolled back because it was applied with the board off).
- The endpoint needs `X-Farm: 1` (the template sends it), logs the actor as `ui:Ironman via <printer> (Telegram/Fluidd)`,
  and answers a refusal with 409, logged as event `power_refused`; farm-monitor relays each one once, silent, on the
  ops bot (Moonraker's error text may never reach the printer's chat).
- The Telegram pack sets `[bot] power_device: printer` (only keys in `ops.BOT_KEYS`; token and chat untouched) and adds
  the `power` button only where the device exists; `power` is in `require_confirmation`.
- **Not wired on standalone printers** (NebulaV3SE: its Moonraker runs on the pad its own outlet powers). Use the card's ⏻.
- State 2026-10-24: staged as changes 35 (Ender3MaxNeo), 36 (s1pro), 38 (enders1plus), 39 (EnderV3SE, which also
  drops the Obico updater include). Live test pending Ironman's Apply.

## What the garage costs to run (measured, 2026-10-23)

Ironman's wall-socket meter, upstream of the whole garage, read **£658 over 925 days** (13 Jan 2024 to 27 Jul 2026)
at **27p/kWh**. That is **2,437 kWh, 110 W on average, about £260 a year**. Downstream of it: enders3dp, 3dpnas, the
always-on SBCs, nas.local (the Synology backup target), the house-to-garage network link, 2-3 small LEDs, and all 8
printers on the Meross 6-in-1 strip. The strip switches the printers by hand; it doesn't measure anything.

Prints in the same window (Moonraker history: 6 of 8 printers; the Nebula at .39 and one more aren't readable):
**~1,645 jobs, ~5,400 print-hours, ~68 kg of filament.**

| | kWh | Share | Notes |
|---|---|---|---|
| Printing | ~550-800 | 25-35 % | 0.10-0.15 kWh per print-hour, Ender-class beds |
| Always on | ~1,650-1,900 | 65-75 % | 75-85 W continuous: print hosts, SBCs, NAS, link, idle printers |

- **The always-on load is the bill, not printing.** Each always-on watt costs **£2.37 a year**. Electricity per
  print-hour is about **£0.12**, of which printing itself is about 3p.
- **Savings in order:**
  1. retired SBCs off (s1propad since 2026-10-23, about £7 a year);
  2. one print host instead of two (3dpnas or enders3dp, about £35-60 a year);
  3. printers switched off when idle through this design (5-10 W each while idle);
  4. LEDs on a schedule.
- **beast** (T7810, about 110-140 W idle) is **not** on this meter and is on only when needed. As a 24/7 server it
  would add about £260-330 a year. It stays an on-demand GPU machine; the July plan to make it the primary was
  revised accordingly (agent_workspace `infrastructure/beast/PROJECT.md`).
- The split between devices is estimated; a plug-in meter moved for a day per device would give each one's figure.
