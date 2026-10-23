# Switching printers on and off: Meross smart plugs (research, 2026-10-23)

Nothing is built yet. This is the design and what is needed from Ironman first.

## Findings

- **No Moonraker support.** Moonraker's `[power]` has no Meross type. Its types are gpio, klipper_device, rf,
  tplink_smartplug, tasmota, shelly, homeseer, homeassistant, loxonev1, smartthings, mqtt, hue, http and uhubctl
  (https://moonraker.readthedocs.io/en/latest/configuration/).
- **LAN control works without the cloud.** Send `POST http://<plug>/config` with
  `sign = md5(messageId + key + timestamp)`. Toggle with `Appliance.Control.ToggleX` (older firmware uses
  `Toggle`); read power with `Appliance.Control.Electricity` (MSS310/315 only, in mW).
- **The key is the account's** and the same for every plug. You get it once by logging in with `meross_iot`; no cloud
  is needed after that. It belongs in a chmod-600 env file, never in git or Telegram.
- **Some newer firmware encrypts** the local API. These plugs advertise `Appliance.Encrypt.ECDHE` in
  `Appliance.System.Ability`: AES-256-CBC, which the standard library can't do. One Ability GET per plug tells.

## Design

1. **A small stdlib bridge on aali-cli** holds the key and exposes `GET /plug/<name>/on|off|status`. It is the only
   place encryption would ever be added.
2. **Moonraker `[power <name>]` with `type: http`** points at the bridge, one section per printer instance, with:
   - `locked_while_printing: True`: no power-off mid-print, even from Fluidd;
   - `restart_klipper_when_powered: True`: replaces the manual Reconnect;
   - `initial_state` left unset.

   These are printer config changes, so they are staged and applied through the gate like any other.
3. **Farm Control** switches through Moonraker's `/machine/device_power/device`, so it never sees the key. It refuses
   OFF while printing or paused, or while the hotend is above about 50 C (the heat-break fan has to keep running).
4. **No plug may feed a host** (enders3dp or 3dpnas). One OFF would take down every instance on it.

## Needed from Ironman

- Each plug's model and hardware/firmware version (Meross app → device → settings), and which printer it feeds.
- A DHCP reservation per plug.
- The account key, extracted once in a throwaway venv (`meross_iot`), into the bridge's env file.
- Optionally, block the plugs' internet access at the router once local control works.

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
