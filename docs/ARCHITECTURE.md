# Farm Control as the shop's central workflow: maker plugins → slice → gate → queue → print → back to the order

## Context

Today `~/repos/3dp` drives one product end to end. That product is likhan grave plates in PETG on the 0.6 printers, run from the CLI. Farm Control (:10111) displays and monitors, but it doesn't orchestrate.

Amir's goal is a central app around the five maker apps on aali-cli:
- likhan :4111
- lithophane :5111
- namakdani :6111
- lamp + plaque :8111
- holder :9111 (WIP)

Each of these has one job: generate a model to print. The central app should then send it to print. Orders arrive from **Farm Control's New-job page** and from **invtool** (:3111). The original written requirement isn't on aali-cli; it was probably in the Windows-workstation session. This plan is built from Amir's answers in this session.

Decisions from Amir:
- Makers are **plugins behind one shared contract**.
- Every **job** chooses its printer, print preset, filament preset and layer height. Coarse or large models, including lithophanes in 0.6 Hyper PLA, can go to the big 0.6 printers.
- EnderV3SE can be onboarded, as its own gated phase.
- Prints start **only from a human Start button after a checklist**.

Research found the following (four survey agents plus a design agent):
- **The makers.** None knows about the farm or emits colour changes, and their APIs all differ: likhan and lithophane are one synchronous multipart call, lamp takes a JSON `parameters` field and can take minutes, namakdani needs an upload followed by a build call, and holder is an asynchronous job API. All five have `/health`.
- **Only the PETG MaxNeo preset can pass the pipeline today:**
  - every PLA preset, including `Creality Hyper PLA 210_70`, has volumetric cap 0, which `gcode.validate` rejects;
  - every other printer preset auto-emits temperatures and has literal temperatures in `START_PRINT`;
  - lamp-maker's embedded Ender 3 V3 SE profile fits no farm printer;
  - EnderV3SE's `M600` calls a missing `PAUSE_MACRO`.
- **The pipeline.** Plates are hard-wired:
  - `job["likhan"]`, `base_top`, `plates`, label matching in the batch grid, and the "Plates"/"PETG" UI;
  - runs can stay "running" forever, because several `SystemExit` paths skip `Run.fail`;
  - there is no job state machine and no queue;
  - `cmd_start` logs audit failures but starts anyway, and `moonraker.start` has no readonly, approval or lock check;
  - inventory saves can be overwritten by a stale browser;
  - there are no tests.
- **invtool:**
  - Prisma/Postgres; `Sale` is one order line;
  - `Product.kind` includes `MADE_TO_ORDER`, and `maker_meta` (empty in practice) was meant to hold the generator and its params;
  - no auth, no production status, no personalisation field;
  - buyer PII is deliberately never stored, and a test enforces that;
  - `lib/record-sale.ts` is the single path every sale goes through;
  - a `MADE_TO_ORDER` product fails the stock guard on its second sale;
  - "private" assets sit in public `data/uploads/`.

## Architecture

### Persistence and processes
- **Persistence: stdlib `sqlite3`** in `data/farm.db` (gitignored), with WAL and `busy_timeout`. Schema migrations go through `PRAGMA user_version`.
  - It holds Orders, Jobs (items), Models, Builds (one bed = one G-code), Approvals, Events (append-only), Tasks (worker queue with leases), Spools plus a SpoolLedger, and an Outbox.
  - Large files (STL, G-code, 3MF, uploads) stay on disk.
  - `runs/<stamp>_<slug>/` stays as each Build's artifact folder, so `manifest.json`, `run.log` and the 3D viewer keep working.
  - Why sqlite: two processes write state, and it gives atomic compare-and-set transitions and per-printer locks. It also replaces the whole-file inventory POST with optimistic locking.
- **State machines**, all moved through one `transition()` in `farm/jobs.py`. Each transition is an atomic compare-and-set (`UPDATE … WHERE state=?`) with an allowed-actor table, and writes an Event row.
  - **Job:** `received → generating → generated → planned → printing → done | failed | cancelled`.
  - **Build:**
    - Happy path: `slicing → review → (human approve) → approved → uploading → queued → (human Start through the gate) → starting → printing → done`.
    - Off ramps: `review → rejected`, any state → `failed`, and `cancelled` from queued, approved or printing.
    - Paused is a flag, not a state.
- **Worker: a separate `python -m farm worker`** under a new jobctl job `farm-worker`, using the same start.sh and heartbeat-watchdog pattern as `farm-web`.
  - Why separate: maker calls take up to 15 minutes and slicing is heavy, so request threads must stay fast and the worker must survive web restarts.
  - Concurrency is limited per maker and to one slice at a time.
  - It also runs the **tracker**, which reads Moonraker only:
    - remote names `farm/<order>/b<id>_<stem>.gcode` make the build↔print link unambiguous;
    - it moves Builds from starting to printing to done, failed or cancelled;
    - on a terminal state it deducts spool usage and writes an outbox entry.
- **One code path:** `farm run job.json` creates the rows and runs the same tasks inline, so the CLI keeps working.

### Profiles
- **`farm/profiles.py`** defines PrinterProfiles in git, each with:
  - `printers` it may run on (MaxNeo and S1 Plus share G-code);
  - nozzle;
  - the PrusaSlicer bundle and its printer, print and filament presets;
  - material;
  - allowed layer heights;
  - first-layer limits;
  - max volumetric flow;
  - whether supports are allowed.

  A job picks profile + layer height + printer, with the printer constrained to `profile.printers`. A queued Build can move between MaxNeo and S1 Plus without re-slicing. `Printer` keeps the hardware facts only.

### Starting a print
- **`farm/gate.py` is the only caller of `moonraker.start`**, which now requires a token only the gate can create. `start_build()` refuses unless every check below passes:
  1. The build is `queued`, and the compare-and-set to `starting` wins (a double click loses).
  2. A review approval exists for the current G-code sha256.
  3. The checklist (viewed 3D, bed clear, colour loaded) is ticked, **and** the server logged that the 3MF was actually served for this build.
  4. The printer isn't readonly and is in `profile.printers`.
  5. A live `audit.rules` returns zero FAIL. This is blocking, unlike today.
  6. Moonraker reports ready, not printing, not paused, and no other build is starting on that printer.
  7. The remote file exists with the expected size.
  8. A spool is chosen and its material matches the profile.
- **The web Start** also needs a nonce, issued when the Start dialog opens and valid for 2 minutes. The CLI `farm start` needs a TTY and the printer name typed.
- **CLAUDE.md gains:** agents never call `/api/builds/*/start` or `farm start`.

### Spools
- `gcode.segments()` splits extrusion at each M600 into grams per colour, using the diameter and density from the config block.
- The Start dialog assigns a spool to each colour.
- Deduction happens when the build is done; if it is cancelled or failed, the deduction is prorated from Moonraker history `filament_used`. The ledger is idempotent on (build, attempt, spool). This covers HANDOFF to-do 6.

### Maker plugin contract (`farm/makers/`, contract `farm-maker/1`)
- **`MakerSpec`** describes each maker: id, name, URL (overridable by env var), sync or async, timeout, poll interval, concurrency, file inputs (`private` for fonts), param schema (type, default, min, max, enum, label, personalise), typical material, nozzle, layer heights and profiles, colour changes (`none` or `farm`), supports, and experimental.
- **`Maker` methods:**
  - `health()`;
  - `defaults()`: spec values, overlaid by the maker's `/api/defaults` where it exists;
  - `validate(params, files, ctx)`: pure, run both when the form is submitted and before generating;
  - `generate(ctx) → GenResult`, which returns:
    - the models, with path, label, bbox and triangles;
    - `bed_groups` (which pieces share a bed);
    - named colour heights, e.g. `{"base_top": 4.0}`;
    - stats, warnings and the maker version.
- **Errors:** `MakerError(retryable)`.
- **Shared helpers:** STL bbox, zip unpacking, a bed-fit check, and timeouts per call (`net.post_json_body`, `get_bytes`).
- **Optional `GET /farm-maker.json` manifest** that each maker can add later. The adapter stays authoritative in v1. If the manifest exists, the adapter reads its version and default params from it, and a test catches the two drifting apart.
- **How each maker maps onto the contract:**
  - **likhan** (moved from `farm/likhan.py`, which becomes a re-export):
    - sync, 300 s;
    - the bed size comes from the profile;
    - `plates` become one Job per plate, grouped onto one bed;
    - `base_top` comes from `base_thickness_mm`.
  - **lithophane:**
    - sync, 900 s, STL;
    - its bed fit is checked from width and height, since it takes no bed parameter;
    - it warns on 0.6 thickness settings until Amir confirms them;
    - default profile is the 0.6 Hyper PLA litho one.
  - **lamp:**
    - sync, 900 s;
    - **always requests STL**, so its embedded V3 SE 3MF config never reaches the slicer (stripped if a 3MF ever arrives);
    - its split bed comes from the profile.
  - **plaque** (`/api/generate-plaque`): likhan-like.
  - **namakdani:**
    - upload and build inside one `generate()` call, concurrency 1;
    - the upload id is never stored (the maker keeps uploads in memory), so a retry uploads again.
  - **holder** (experimental):
    - images, then holders (202), then poll, then fetch `holder.stl`, with an 1800 s limit;
    - only profiles that allow supports qualify;
    - a non-watertight model is a non-retryable failure;
    - review needs a typed confirmation.

### invtool integration (the farm pulls; invtool never calls the farm)
- **invtool repo:**
  - **New model `ProductionRequest`:**
    - fields: unique `sale_id`, `maker_tool`, `params`, `personalisation`, private `assets`, `status`, farm ids, `last_event_key`;
    - enum `ProductionStatus` PENDING/CLAIMED/IN_PRODUCTION/PRINTED/FAILED/CANCELLED;
    - `Sale.production_status` for badges.
  - **Hook in `lib/record-sale.ts`:** in the same transaction, when the product is `MADE_TO_ORDER` and `maker_meta.tool` is set, create a ProductionRequest.
  - **MADE_TO_ORDER stock guard fix:** skip the stock guard, the decrement and the delist cascade for `MADE_TO_ORDER`, with a test.
  - **Documented `maker_meta` schema:** tool, contract, params, personalise fields, profile, layer height, colours, colour changes.
  - **Personalisation panel** on the sale page. eBay drops personalisation, so the operator enters it, and requests stay unclaimable until it is filled.
  - **Bearer-token API** under `app/api/production/`, using `FARM_API_TOKEN` from `.env` compared with `timingSafeEqual`:
    - `GET pending`;
    - `POST :id/claim`;
    - `POST :id/status`, idempotent on `event_key` and forward-only;
    - `GET :id/assets/:n`.
  - **Private storage** under `data/private/`, purged N days after DISPATCHED, with no buyer fields. The existing public-`private_assets` exposure is flagged separately.
- **3dp:**
  - `farm/invtool.py` polls every 60 s, validates through the adapter, creates the Order (`source=invtool`, unique `external_ref`), downloads the assets and claims the request.
  - Status goes back through the outbox:
    - generating → IN_PRODUCTION;
    - every job in the order done → PRINTED;
    - failed or cancelled are passed through.
  - The token lives in `farm_control/farm.conf` (chmod 600).

## Phased checklist (each phase ships alone; `farm run jobs/qabrastaan/*.json` must keep producing identical G-code throughout)

**Phase 0: prerequisites and safety net**
- [ ] Amir runs `sudo apt-get install -y flatpak`, then the user-level Flathub install of PrusaSlicer 2.9.6, plus `flatpak override --user --filesystem=/home/aali/repos/3dp`. Then set `FARM_PRUSASLICER` in `farm_control/farm.conf`. Today's apt 2.7.2 is for testing only.
- [ ] Add a `farm doctor` command that reports the slicer version (refuses <2.9.6 for production), each maker's health, and whether the font is present. It never reads or serves the font.
- [ ] likhan-maker: pull `c5658a6` → `94efc97` (fixed plate size and font option), then restart it through its own `stop.sh && start.sh` while idle. Amir copies the Charles Wright font into `assets/fonts/` by hand. Then dry-run A01 with no upload.
- [ ] `runlog`: add a `Run.guard()` so every exit writes a `result`, and keep slice/validate/project steps per bed. Make run folder names unique.
- [ ] Create `tests/` with stdlib unittest (`python -m unittest discover -s tests`) for `align_first_layer`, `insert_colour_changes`, `gcode.validate`, `layout_bed`, `watch.milestones`, `audit.rules` and `model3mf.load`. Add a **golden G-code diff for A01** (ignoring timestamp lines).

**Phase 1: DB and state machine in shadow mode**
- [ ] Add `farm/db.py` (schema and migrations) and `farm/jobs.py` (transitions, create functions, derived Job state).
- [ ] Add `farm db init` and `farm import-runs`, which backfills from the manifests.
- [ ] `cmd_run` also writes rows. Add `GET /api/builds`, read-only. The UI is unchanged.
- [ ] Tests: legal and illegal transitions, two connections racing the same compare-and-set with exactly one winning, idempotent migrations.

**Phase 2: profiles and presets**
- [ ] `farm/profiles.py` with `maxneo-petg-036` identical to today's setup (the golden diff must still match). The slicer and validate read their first-layer limits and flow cap from the profile.
- [ ] Add a farm-owned bundle, `prusa/farm_profiles.ini`. The user's own bundle is untouched. It holds:
  - a 0.6 PLA printer preset: `START_PRINT EXTRUDER_TEMP={first_layer_temperature[0]} BED_TEMP={first_layer_bed_temperature[0]}`, `autoemit_temperature_commands = 0`, and the bed matching the registry;
  - `Creality Hyper PLA 0.6 (farm)` with a real volumetric cap. **The value is for Amir to confirm** (about 15 proposed);
  - `0.28 Litho 0.6 (farm)`.

  These make up profile `neo-hyperpla-028-litho` for MaxNeo and S1 Plus.
- [ ] Add `farm presets check` with a test. For each profile it checks that the presets exist, autoemit is off, both placeholders are present, the cap is >0 and within the limit, no `*_extruder=0` lines remain, and the bed shape is within the registry.
- [ ] Add a validate check that START_PRINT carries numeric temperatures, and a read-only audit rule that the `START_PRINT` macro reads `EXTRUDER_TEMP` and `BED_TEMP`.

**Phase 3: maker contract, likhan adapter and worker**
- [ ] Add `farm/makers/{__init__,base,likhan}.py`, `farm/pipeline.py` (`generate_job`, `slice_build` and `upload_build`, extracted from `cmd_run`), and `net` timeouts.
- [ ] `farm/worker.py`: claim tasks with leases, per-maker semaphores, one slicer at a time, a heartbeat, and `--once`.
- [ ] Ops:
  - `agent_workspace/infrastructure/farm_control/worker_start.sh` and a jobctl job `farm-worker`;
  - a SERVER.md entry and `farm_control/PROJECT.md`;
  - CLAUDE.md and PIPELINE.md updated.
- [ ] Tests: the contract test against a fake likhan built on `http.server`, and a lease reclaimed after the worker is killed. The golden diff still matches.

**Phase 4: review, queue and the Start gate in the UI**
- [ ] `web.py`:
  - a route table;
  - `POST /api/builds/<id>/approve|reject|cancel|move`, `start-dialog` (which issues the nonce) and `start`;
  - `/api/model` records a "viewed" event.
- [ ] Upload becomes a worker task on approve, using the build-id remote name.
- [ ] Add `farm/gate.py` as above. `moonraker.start` requires the gate's token, and `cmd_start` goes through the gate.
- [ ] `index.html`:
  - a Review list (3D, Approve, Reject);
  - a queue on each printer card (order, Move, Cancel);
  - a Start dialog with the three checks, the spool picker and the audit status. The server re-checks everything.
- [ ] Test harness: `FARM_PRINTERS_FILE` points the registry at `tests/fake_moonraker.py` (http.server).
- [ ] `tests/test_gate.py` covers every refusal reason, plus a race where exactly one of two starts wins. Run a headless Chromium pass on a second port against the fake. **No real printer is called.**

**Phase 5: tracker and spool deduction**
- [ ] Add the tracker to the worker, sharing helpers with `monitor.py` and making no Moonraker writes. Add `gcode.segments()`.
- [ ] `farm/spools.py`:
  - a one-time move of `inventory.json` into the DB, with a backup;
  - the ledger;
  - `PATCH /api/spools/<id>` with a version check;
  - the UI saves one spool at a time.
- [ ] The batch grid reads Job states from the DB. Qabrastaan becomes one Order whose Jobs are labelled A01…, and label matching stays as a fallback.
- [ ] Tests:
  - segment sums match the `filament used [g]` header within 0.5 %;
  - partial deduction from a history fixture;
  - replays are idempotent;
  - a stale save returns 409;
  - tracker replays from recorded Moonraker JSON.

**Phase 6: New-job page and the other makers**
- [ ] New-job view, in this order:
  1. choose a maker; the form is rendered from its spec and defaults;
  2. upload files (`data/uploads/`, size-capped, gitignored);
  3. pick a profile valid for that maker, then a layer height, then the printer;
  4. set colours and colour changes, then qty.

  It creates the Order and Jobs, which enqueues generation. Fonts are chosen by name only.
- [ ] Adapters, in this order: lithophane, lamp + plaque, namakdani, holder (experimental).
- [ ] For each adapter:
  - contract tests for timeout, 4xx (not retryable) and 5xx (retryable);
  - one live generate against the local maker, with no printer;
  - a 2.9.6 dry slice that passes validate;
  - a check in the 3D viewer.

  For lithophanes, also measure slice time and memory on aali-cli.

**Phase 7: invtool**
- [ ] invtool repo: the items under "invtool integration", with migrations and tests. The PII test must stay green.
- [ ] 3dp: `farm/invtool.py`, polling and the outbox in the worker, and the token in `farm.conf`.
- [ ] Verify:
  - tests against a fake invtool cover idempotent claims, outbox retry and duplicate `event_key`;
  - end to end with a **test** MADE_TO_ORDER sale: it appears as an Order, a dry run reaches review, and a cancel flows back to invtool as CANCELLED.

**Phase 8: EnderV3SE onboarding (gated)**
- [ ] **Start conditions:** an explicit go-ahead from Amir in his own message, the printer idle (`farm status`), and a fresh config backup.
- [ ] Fixes:
  - a guided M600 with no auto-unload, replacing the broken `PAUSE_MACRO`;
  - CLEAR_PAUSE in START_PRINT;
  - the paused-safe idle timeout and 12 h cap;
  - one bed size agreed with Amir (today the registry, slicer and Klipper all differ).
- [ ] Then `farm audit EnderV3SE` must pass and `gcode_store` must show no "Unknown command".
- [ ] Add 0.4 profiles (first layer 0.15–0.3), and set `has_m600_prompt=True` and `readonly=False`.
- [ ] Update FARM.md's feature matrix and change log.
- [ ] The first real job goes through review and the Start gate with Amir present.

## Critical files
- 3dp:
  - **Modified:** `farm/__main__.py`, `farm/slicer.py`, `farm/gcode.py`, `farm/moonraker.py`, `farm/runlog.py`, `farm/net.py`, `farm/printers.py`, `farm/web.py`, `farm/static/index.html`, `farm/monitor.py` (shared helpers)
  - **New:** `farm/profiles.py`, `farm/db.py`, `farm/jobs.py`, `farm/gate.py`, `farm/worker.py`, `farm/pipeline.py`, `farm/spools.py`, `farm/invtool.py`, `farm/makers/*`, `prusa/farm_profiles.ini`, `tests/*`
- **invtool:** `prisma/schema.prisma`, `lib/record-sale.ts`, `lib/validation.ts`, `app/api/production/*`, the sale page personalisation panel
- **ops:** `agent_workspace/infrastructure/farm_control/*`, a new jobctl job `farm-worker`, `SERVER.md`

## Reused, not rewritten
`slicer.extract_preset`, `align_first_layer`, `insert_colour_changes` and `layout_bed`; `gcode.validate`; `project.export_project`; `model3mf.load` and the 3D viewer; `watch.milestones`; `audit.rules`; `monitor.py`; `net.post_multipart`; the `Run` folders and manifest; the `farm_control` start.sh and jobctl watchdog pattern; the likhan request logic, moved into the adapter.

## Verification, overall
- Every phase: `python -m unittest discover -s tests` passes, and the A01 golden G-code diff is identical.
- Every dry run uses no `--upload` and real makers only. Gate and UI tests use the fake Moonraker. Nothing in implementation starts a print or edits a printer config, except Phase 8, which needs Amir's go-ahead and an idle printer.
- After deploying, check `farm-web`, `farm-worker` and `farm-monitor` in `jobctl list`, and make sure SERVER.md is current.

## Open values for Amir (asked when each phase reaches them)
1. Hyper PLA volumetric cap on the 0.6 Sprite.
2. Lithophane thickness (`t_min`/`t_max`) at 0.6.
3. Whether to turn on `remaining_times`, which would give an exact countdown to the colour change.
4. The EnderV3SE bed size, and the timing of its hand-over.
5. How many days to keep personalisation in invtool before it is purged.

## Non-goals (v1)
- Auto-starting prints, or any start by an agent.
- Logins on Farm Control. LAN and Tailscale only, as today.
- Packing several models onto one bed beyond the current front-to-back layout, or mixing profiles on one bed.
- Multi-material printers.
- Makers inserting their own colour changes.
- Importing personalisation from eBay.
- Rewriting the maker apps, beyond an optional manifest.
- Replacing Fluidd for prompts or cancelling.
- Onboarding Ender3Pro or EnderCR10SE.
