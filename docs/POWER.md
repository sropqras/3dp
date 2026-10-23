# Switching printers on and off: Meross smart plugs (research, 2026-10-23)

Nothing is built yet. This is the design and what is needed from Amir first.

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

## Needed from Amir

- Each plug's model and hardware/firmware version (Meross app → device → settings), and which printer it feeds.
- A DHCP reservation per plug.
- The account key, extracted once in a throwaway venv (`meross_iot`), into the bridge's env file.
- Optionally, block the plugs' internet access at the router once local control works.
