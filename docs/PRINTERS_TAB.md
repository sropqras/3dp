# Farm Control: a Printers tab, so the farm can run without an AI session

## Context

Tonight every printer operation was done by hand from an AI session over SSH, the Moonraker API, or a script: audits, config backups, macro and baseline edits, the s1pro instance, the history and G-code migration, camera and service setup, and explaining timeouts.

Amir wants all of that **inside Farm Control**, so day-to-day running needs no AI. Sessions like this one are then only for developing the tooling.

His decisions (2026-10-23):
- **Config changes are staged, then applied through a gate.**
- The first version covers:
  - a **maintenance log**;
  - a **health monitor**;
  - **scheduled config backups**;
  - **non-onboarded instances**.
- The **main dashboard stays as it is**. Everything new goes in a separate **Printers** tab.

What's available to build on:
- **Every host's Moonraker API** (v0.9.3 on enders3dp and 3dpnas) exposes:
  - component versions (`/machine/update/status`);
  - failed components and warnings (`/server/info`);
  - controllable services and their state (`/machine/system_info`);
  - host CPU and memory (`/machine/proc_stats`);
  - every config file, readable and writable (`/server/files/…?root=config`);
  - `/printer/firmware_restart`;
  - history and its totals.
- **SSH from aali-cli:**
  - enders3dp works (`~/.ssh/enders3dp`);
  - 3dpnas has SSH, but aali-cli's key isn't authorised there yet (Amir: `ssh-copy-id`).
  - Neither host has passwordless sudo, so anything needing root (systemd units) stays a generated script for Amir to run.
- **Already in place:**
  - `farm/audit.py` (rules);
  - `farm/monitor.py` (alerts);
  - `farm/db.py` with migrations;
  - `jobs.transition`-style compare-and-set;
  - the gate pattern (`farm/gate.py`);
  - `web.py`'s route table;
  - `tests/ui_check.*` and the fakes (`tests/fakes.py` FakeMoonraker).

## Design

### The Printers tab (`farm/static/printers.html` + `printers.js`, linked from the header; the dashboard is untouched)

- **The host list** comes from data, not code: `printers/hosts.json` lists each host, its Moonraker ports and SSH alias. An instance found on a host but missing from `farm/printers.py` shows as **not onboarded**, with a checklist of what's missing (registry entry, farm baseline, profile, camera, Telegram).
- **One card per instance, with:**
  - its state;
  - versions (Klipper, Moonraker, Fluidd, crowsnest, Telegram bot, KAMP), plus which are behind;
  - failed components and warnings;
  - services (a Telegram bot crash-looping shows red);
  - host CPU temperature, memory and disk;
  - whether the camera snapshot works;
  - the audit table (every rule with its reason, pass/fail);
  - config drift;
  - pending changes;
  - maintenance due.
- **Detail view:**
  - config files, each with a diff against the last reviewed backup;
  - change history (who changed what, when, and why);
  - service controls;
  - maintenance log;
  - the print history totals.

### Config backups and drift (`farm/configs.py`)

- **Snapshot:** read every config file over Moonraker, never the secret files (`telegram.conf`, `moonraker-obico.cfg`, `octoeverywhere.conf`, `*.bkp`), and store it content-addressed under `data/config_snapshots/<printer>/<sha>` with a `config_snapshots` row.
- **Schedule:** nightly through a jobctl `farm-backup` job, plus on demand.
- **Drift:**
  - **Reference** = the last snapshot a person marked reviewed.
  - **Drift** = live files that differ from it, or new or removed files.
  - Drift raises a silent alert, or a loud one for `printer.cfg`/`macros.cfg`.
  - `farm backup --to-repo` keeps `printers/<name>/` in step for git; committing stays a development action.

### Staged changes and the gated Apply (`farm/configs.py`, using `gate.py`'s pattern)

- **A change** is a full new file content, or a key edit to `[section] option`, plus a reason and an author (ui, cli, or an agent through `farm config stage`). It's stored as a `config_changes` row: `staged → applying → verifying → applied | failed → rolled_back`, plus `rejected` and `cancelled`. Every transition is a compare-and-set with an event row, like builds.
- **Apply** is a person's button, or `farm config apply` at a terminal. Agents can only stage. It **refuses** unless:
  - the printer isn't watch-only;
  - Klipper isn't printing or paused;
  - the live file still has the hash it had when the change was staged (otherwise: stale, re-stage);
  - no other change is applying on that printer.
- **It then:**
  1. Snapshots first.
  2. Uploads through the Moonraker file API (the same code path for enders3dp and 3dpnas).
  3. Runs `FIRMWARE_RESTART`.
  4. Waits for `ready`, up to 90 s.
- **Then it verifies:**
  - Klipper is `ready`;
  - there are no new audit FAILs compared with before;
  - `gcode_store` has no `Unknown command` or `!!` since the restart.
  - If Klipper isn't `ready`: **automatic rollback** (upload the snapshot, restart again) and a loud alert. Other failures leave the change `failed`, with a one-click Rollback.
- **Change log:** every applied change is appended to FARM.md's change log table automatically (printer, change, reason, who), so the doc can't drift.

### Remediation instead of AI: baseline fixes (`farm/baseline.py`)

- Each `audit.py` rule gets an optional **fix**: a generated staged change taken from the reference implementation that's proven on the farm (the MaxNeo and S1 Plus macros):
  - `max_velocity`/`max_accel`/`recover_velocity`: key edits;
  - `idle_timeout`: the paused-safe block;
  - `_PAUSE_HARD_STOP` and `_PAUSE_RESUME_HOOK`;
  - guided `M600` with the prompt and no auto-unload;
  - `CLEAR_PAUSE` in `START_PRINT`;
  - the `START_PRINT` parameters.

  Templates are parameterised per printer: name, sensor name, the Telegram prefixes.
- **The audit table gets a "Stage fix" button per failing rule.** The user reviews the diff, then Applies through the gate above. This replaces the hand-copying I did for s1pro.
- **Telegram pack:** "Apply farm Telegram pack" stages a merge of `farm/telegram_pack.conf` into `telegram.conf`, keeping `[bot]` (token and chat). This is the only place a secret file is touched: it's read for the merge, never shown, logged or snapshotted. The bot is then restarted through Moonraker's service API (the bot only, never Klipper).

### Services and hosts

- **Restart:** a Restart button for each service in Moonraker's allowed list (Telegram bot, crowsnest, the ustreamer, Moonraker itself). Klipper and crowsnest restarts refuse while that host has a printer printing or paused. Crowsnest affects every camera on its host, so it warns and names them.
- **Host-level jobs that need root** produce a **script to copy and run** (the `s1pro-install.sh` / `s1pro-camera.sh` pattern), filled in from the tab, and the tab verifies the result afterwards:
  - new instance;
  - separate camera;
  - unit drop-ins.
- **SSH (read-only)** is used only for what Moonraker can't do: listing USB serial/camera devices for onboarding, and host disk/logs. It never makes changes.

### Health monitor (extend `farm/monitor.py`; the same new/recovered dedupe)

- **New checks:**
  - a failed component or Moonraker warning;
  - a service that should be running and is failed or crash-looping;
  - host disk over 90 %;
  - CPU throttling or temperature over 80 °C;
  - the camera snapshot failing;
  - Klipper or Moonraker versions newer than the farm's tested ones (silent);
  - config drift;
  - maintenance due.
- **Alerts** go loud or silent on the ops bot, as now.

### Maintenance log (`maintenance` table + tab)

- **Log entries:** kind (nozzle, belts, PTFE, lubrication, bed, other), note, who, when, and the printer's print hours at that time, taken from Moonraker history totals.
- **Reminders:** per kind, a reminder interval in print hours (defaults editable in the tab, e.g. nozzle 300 h, belts 500 h, lubrication 200 h). "Due" shows on the card and the monitor alerts once.

### Registry as data, and onboarding

- `farm/printers.py` reads `printers/registry.json` (git-tracked; today's entries move into it unchanged). The tab's **Onboard** checklist edits it with confirmations: hardware facts, the profiles the printer is allowed, and readonly on/off.
- The checklist steps, each verified live:
  1. instance reachable;
  2. serial port detected (SSH device listing);
  3. audit passes, or "Stage fix" for each failing rule;
  4. camera registered;
  5. profile chosen;
  6. registry saved;
  7. monitor picks it up.

  This is the s1pro cut-over, made repeatable.

## Phases (each shippable; the dashboard, the pipeline and printing behaviour are untouched)

1. **Read-only tab:** host discovery, cards (versions, services, health, audit table), config file list, snapshots + drift, nightly `farm-backup` job, non-onboarded instances. New: `farm/hosts.py`, `farm/configs.py` (snapshot/drift), `printers/hosts.json`, migration 4 (`config_snapshots`), `printers.html`/`printers.js`.
2. **Staged changes + gated Apply + rollback:** `config_changes` (migration 5); `farm config stage|apply|rollback` CLI (apply/rollback refuse without a terminal for non-UI actors); the editor and diff UI; the FARM.md change-log writer.
3. **Remediation and operations:** `farm/baseline.py` fix templates per audit rule; the Telegram pack via the API; service restarts with gates; root-script generators; SSH device listing.
4. **Health monitor + maintenance:** monitor checks; `maintenance` table (migration 6) and reminders.
5. **Registry as data + onboarding checklist.** Once this phase lands, first use: finish s1pro through it.

## Tests

- **Offline unittest** (extending `tests/fakes.py` FakeMoonraker with the config file API, `firmware_restart`, `system_info`, `update/status`, `proc_stats`, and gcode_store control):
  - snapshot and drift excluding secret files;
  - Apply refused when printing, paused, watch-only, stale hash, or another change is applying;
  - happy Apply: snapshot → upload → restart → verify;
  - automatic rollback when Klipper won't come ready;
  - verify failing on a new audit FAIL or an `Unknown command`;
  - a two-apply race where one wins;
  - Telegram pack merge keeping `[bot]` and never logging the token;
  - baseline fixes making the audit pass on a fake printer that fails every rule;
  - maintenance due computed from fake history totals.
- **UI check** (`tests/ui_check.*` pattern, fakes only): open the Printers tab, stage a fix, apply, see the change verified, then roll back.
- **Live, read-only:** the tab against the real farm with **no changes applied**: versions, audit and drift match `farm audit` / `farm status`.
- **The first real Apply** is an s1pro baseline item (no print risk), with Amir watching.

## Rules carried over (enforced in code, not just docs)

- EnderV3SE stays hands-off: watch-only, so Apply and service restarts refuse on it.
- No restart or config change while printing or paused (the gate).
- Secret config files are never snapshotted, shown or logged.
- Agents may stage but never apply or start; that rule is added to CLAUDE.md.
- Root-only host steps are scripts for Amir.

## Critical files
- **New:** `farm/hosts.py`, `farm/configs.py`, `farm/baseline.py`, `farm/maintenance.py`, `farm/static/printers.html`, `farm/static/printers.js`, `printers/hosts.json`, `printers/registry.json`, `tests/test_configs.py`, `tests/test_baseline.py`
- **Modified:** `farm/web.py` (routes), `farm/db.py` (migrations 4–6), `farm/monitor.py`, `farm/audit.py` (fix hooks), `farm/printers.py` (registry from JSON), `farm/__main__.py` (`config`/`backup`/`maint` commands), `farm/static/index.html` (header link only), `tests/fakes.py`, CLAUDE.md, FARM.md
- **Ops:** jobctl `farm-backup` (nightly), SERVER.md, `farm_control/PROJECT.md`
