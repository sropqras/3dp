# Farm Control: Spools, Inventory (artefacts + portfolio), Farm Projects

**Status 2026-10-23: all three phases and the OMV catalogue shipped and deployed** (commits `ddd4f04`,
`98b7fc0`, `021d4dd`). Live: 693 artefacts, 1,100 prints from four printers, 1,668 NAS model files (391 matched to
printer files), the Qabrastaan project with 5 of 635 plates found printed.

## Context

The last pass got the tabs wrong:
- The tab called **Inventory** shows spools. It should be called **Spools**.
- **Inventory** should list every **artefact** the farm has made:
  - maker outputs, manual uploads, and G-code already sitting on the printers;
  - where each one printed successfully;
  - sortable and groupable by **Printer**, **Project** or **Maker**.
  - It is the start of a **product portfolio**.
- **Projects** should be a working tab for a **large order** (a "Farm Project"):
  - many parts, from any maker or added by hand;
  - shown on a flexible grid.
  - Today's grid is a hard-coded, likhan-only `jobs/*/batch.json` (Qabrastaan A..R).

Ironman's answers (2026-10-23):
- **Grid:** groups → parts × qty. Qabrastaan becomes the first project.
- **Portfolio:** artefacts are promoted to products by hand.
- **Success** comes from three sources: the tracker, Moonraker history, and a person's good/bad mark.
- **Manual artefacts:**
  - STL/3MF upload;
  - existing G-code on a printer;
  - bought or non-printed items;
  - everything already on the onboarded printers.

## Data model: migration 7 in `farm/db.py`

### `artefacts`: one deliverable thing

| Field | Values / meaning |
|---|---|
| `kind` | `model` \| `gcode` \| `item` |
| `title` | |
| `maker` | likhan, …, `upload`, `printer-file`, `item` |
| `job_id` | for maker or upload outputs; its pieces stay in `models` |
| `path` | |
| `sha256` | |
| `printer` + `remote_path` | for G-code that lives on a printer |
| `meta` | JSON: slicer, estimate, filament, layer height, bbox |
| `thumb` | |
| `created_at` | |

- One artefact per generated job, created by `pipeline.generate_job` at the moment its models are written.

### `prints`: one attempt of an artefact on a printer

| Field | Values / meaning |
|---|---|
| `artefact_id` | |
| `build_id` | may be empty |
| `printer` | |
| `file` | |
| `started_at`, `finished_at` | |
| `status` | `completed` \| `failed` \| `cancelled` |
| `source` | `tracker` \| `history` \| `import` |
| `quality` | empty, `good` or `bad` |
| `marked_by`, `marked_at`, `note` | |

- `UNIQUE(printer, file, started_at)`, so imports are idempotent.
- "Printed successfully" means completed and not marked bad. Good-marked prints are counted separately.

### Projects

- **`projects`**: name, client, note, state (open / delivered / archived), `hours_per_unit` (fallback estimate), created_at.
- **`project_groups`**: project_id, name, pos. The A, B, CH… rows of the grid.
- **`project_parts`**: group_id, label, qty, `source`, `maker` + `params`, `artefact_id`, `items_done` (for items), pos.
  - `source` is one of: `maker` (generate per part), `artefact` (re-print an existing one), `item` (bought / not printed).

### Products and links

- **`products`**: artefact_id, name, description, photo path, proven printer/profile/layer, created_by, created_at.
- `ALTER jobs ADD part_id` and `ALTER orders ADD project_id`, so work made for a project cell traces back to it.

## Backend

### `farm/inventory.py` (new)

- **`backfill(con)`** is idempotent and runs nightly in the existing jobctl `farm-backup` job, plus on demand with `farm inventory import`. It:
  - creates artefacts for every DB job with models, and for every `runs/*` folder (reusing `pipeline.import_runs`'s parsing);
  - for each onboarded printer in the registry (read-only, including the watch-only EnderV3SE):
    - lists G-code via `/server/files/list?root=gcodes`;
    - reads `/server/files/metadata` (slicer, estimate, filament, thumbnails);
    - reads `/server/history/list` (all pages).

    This gives `gcode` artefacts and `prints` rows: the 135 migrated S1 Pro files and their 297 history jobs, plus the Neo, S1 Plus and V3SE files.
  - matches farm-uploaded G-code to its artefact by remote path, the same way `web.batch_view`'s `hist_by_file` does today.
- **`farm/tracker.py`**: when a build reaches done/failed, also write a `prints` row (`source=tracker`) for each artefact on the build.
- **`listing(con, by, q, products_only, printed_only)`**: artefacts grouped by printer, project or maker, with per-printer counts (ok / bad / failed), the projects that use each one, and its product.
- **`mark(con, print_id, quality, actor, note)`** and **`promote(con, artefact_id, body, actor)`** are for people only (`ui:`/`cli:`, `cfgchange.Refused`-style check).
- **Product photo:** either save the current `/api/cam/<printer>` snapshot or an upload, stored in `data/products/`.
- **Thumbnails:** Moonraker's G-code thumbnails are proxied through `/api/artefacts/<id>/thumb`. Model artefacts keep the existing 3D viewer button.

### Upload maker

- `farm/makers/upload.py` is a `farm-maker/1` adapter: it accepts `.stl`/`.3mf`, and "generate" copies the file into the job's models.
- STL/3MF uploads then go through the same plan → slice → review → approve gates, with no new print path.

### `farm/projects.py` (new)

- **Create and edit:** `create`, `add_group`, `add_parts`.
  - Bulk paste supports two forms: `label[,qty]` lines with a maker template (from `jobs/templates.json`), or a prefix plus a count (A01..A50).
  - `add_from_artefact` adds a part that re-prints an existing artefact.
  - `import_batch(path)` turns `jobs/qabrastaan/batch.json` into a project, with groups A..R and CH, and parts `A01`… using the qabrastaan plate template with the label set. Old Qabrastaan runs and prints link through their labels and artefacts.
- **`grid(con, pid)`** gives one cell per unit (part × qty) with a status:
  - **printed states:** `todo` → `generated` → `sliced` / `queued` → `printing` → `done` / `failed`;
  - **item states:** `item-todo` / `item-done`.

  It derives the status from the part's jobs (via `part_id`), their builds, and `prints` for the part's artefact. This replaces `web.db_cells` and `batch_view`.
  - Progress = cells done out of total.
  - Hours left = build estimates where known, otherwise `hours_per_unit`.
- **`act(con, pid, cell_ids, action, body, actor)`**:
  - `generate`: `pipeline.submit_order` with `project_id` and per-job `part_id`;
  - `send`: `pipeline.send_jobs` for already-generated parts, or for a part's artefact;
  - `item-done` / `item-undo`;
  - `remove`.

  Approving and starting stay where they are, behind the existing gates.

### `farm/web.py` routes

- **Projects:**
  - `GET /api/projects` (summaries), `GET /api/projects/<id>` (groups and the cell grid);
  - `POST /api/projects`, then `/api/projects/<id>/groups|parts|import-batch|act`.
- **Inventory:**
  - `GET /api/inventory?by=printer|project|maker&products=1&printed=1&q=`;
  - `POST /api/inventory/upload` (STL/3MF, or an item);
  - `POST /api/prints/<id>/mark`;
  - `POST /api/artefacts/<id>/product`;
  - `GET /api/artefacts/<id>/thumb`.
- `pipeline.library` stays behind Inventory's "send" path. Today's Projects "Generated content" section moves into Inventory.

## UI (`farm/static/index.html`)

**Tabs:** Dashboard | Projects | Inventory | Spools | Printers →

- **Spools:** today's spool editor moves here unchanged (`#spools`).
- **Projects:**
  - The project list shows cards with progress, hours left and state, plus **+ Project** and "Import batch.json".
  - The project view shows the grid by group, with legend colours as today plus item states. The grid wraps to any count.
  - **Selection:** click, shift-click, or a whole group header. The action bar offers Generate, Send to printer… (the existing send dialog), Mark item done, and Remove.
  - The parts editor supports bulk paste, maker template, qty, and "add from Inventory".
- **Inventory:**
  - **Group by** Printer / Project / Maker; a search box; and toggles for "printed OK only" and "Portfolio (products only)".
  - **Each artefact card shows:**
    - a thumbnail or a 3D button;
    - title and maker;
    - printer chips with ✓ok / ✗failed / ★good counts;
    - its projects;
    - **Send to printer** (model artefacts only; G-code-only artefacts can't be re-sliced, so they show where they live);
    - **Prints** (the list, each with Good / Bad);
    - **Make product** (name, description, photo from a printer camera or an upload).
  - **Upload** adds an STL/3MF or a non-printed item.

## Rules carried over

- Marking quality, promoting, and editing or acting on projects: person actors only.
- Agents may create projects and queue generation (that's preparing), but never approve or start.
- Printers are read-only for the backfill, EnderV3SE included (watching is allowed). Nothing is ever written to a printer from Inventory.
- The licensed font is never served. Artefact params may name it, but thumbnails and downloads never include font files.

## OMV NAS catalogue (Ironman, 2026-10-23)

`\\OMV\3dprinting\3dprinting\Project Files` (about 1,600 STL/3MF/STEP/OBJ/SCAD files) is catalogued, not copied:

- **`farm/nas.py` `scan`:** runs `rsync -r --list-only` against the OMV rsync daemon (`rsync://192.168.1.121/3dprinting/3dprinting/Project Files/`, read-only, anonymous; `FARM_NAS_URL` overrides it). It writes the `nas_files` table (path, size, mtime, ext, folder, design key) and runs nightly in `farm-backup`.
- **Matching:** PrusaSlicer names G-code `<input name>_<time>_<layer>mm_<temp>C_<MAT>_<PRINTER>.gcode`, so `design_key()` (shared with Inventory) reduces both NAS names and printer G-code names to the same key. The matrix shows, per NAS file and onboarded printer, whether it was sent (file on the printer) and how many prints completed, failed or were marked good.
- **Import** copies ONE file with rsync into `data/nas/<path>` and creates a `model` artefact (maker `nas`). From there, Send to printer goes through the upload maker and the normal gates. Only STL/3MF can be imported for printing; STEP/SCAD/OBJ are catalogue only.
- **Later:** `3dprinting/ProductImages` on OMV is a source of portfolio photos.

## Phases (each shippable)

1. Spools tab rename; migration 7; `inventory.backfill` + tracker `prints`; Inventory tab (group by, print list, good/bad marks); upload maker; nightly backfill in `farm-backup`.
2. Farm Projects: tables in use, `import_batch` (Qabrastaan), grid + selection actions, parts editor, items. Remove the `batch.json`-driven `batch_view` once the imported project matches it.
3. Portfolio: products, photos, the products-only view.

## Verification

- **Unit tests (offline):**
  - **migration and backfill:** extend `tests/fakes.py` FakeMoonraker with `files/list?root=gcodes`, metadata and thumbnails, and paged history. Check the backfill is idempotent (run it twice and get the same rows), maps prints to artefacts by remote path, and treats the watch-only printer as read-only.
  - **tracker:** writes `prints` rows.
  - **grid statuses:** every state, qty > 1, items.
  - **`import_batch`:** reproduces today's Qabrastaan cell counts (635).
  - **projects `act`:** generate goes through `submit_order` with `part_id`; send goes through `send_jobs`; agents are refused for marks and promotions.
  - **upload maker:** contract test.
- **UI check** (`tests/ui_check.*` pattern, fakes only):
  - Spools tab works;
  - Inventory groups by printer, project and maker;
  - mark a print good, promote to product;
  - create a project, paste parts, select cells, Generate (fake maker), Send.
- **Live, read-only:**
  - run `farm inventory import` against the real printers, and check the S1 Pro shows about 135 files and its history counts match Fluidd;
  - the Qabrastaan project grid matches the current batch grid before `batch_view` is removed.
- **Also:** full unittest, `tests/golden.py check`, then a farm-web/worker restart and the CLAUDE.md, SERVER.md and PROJECT.md notes.


## NAS catalogue 2.0 (2026-10-23)

Plan: `~/.claude/plans/graceful-mapping-flute.md`. Built:

- **The view:**
  - a Folders / Categories sidebar with counts and "new" dots;
  - a breadcrumb and an OMV online/offline pill;
  - facet chips with counts: type, printed OK / never / failed, printer, new, imported, has pictures, duplicates,
    category, tags;
  - sort, and Grid (thumbnails) / Table / Matrix (the per-printer table);
  - saved views as pinned tabs.
- **Pack drawer** (click a card):
  - 3D preview (the viewer, a lighter mesh, plus "Full resolution");
  - the folder's pictures, its files and the prints of its designs;
  - category and tags;
  - Import / Send / Add to project (group "From NAS").
- **Categories:** seeded from the 86 top folders into 13 categories.
  - Drag folders or ticked cards onto a category.
  - A per-file move is an override.
  - Deleting a category sends its files to Uncategorised.
  - OMV is never written.
- **Tags from names:** Thingiverse id (with a link), printer prefix (`CE3PRO_`), size, version, copy, material,
  calibration, CAD source. A person's own tags survive rescans.
- **Previews:** a 3MF's own slicer thumbnail, or a stdlib flat-shaded raster (top-down for flat pieces); the light
  mesh is ≤ 120k triangles.
  - Warmed nightly. The first 2-minute run built 252.
  - Files over 80 MB are on demand only.
- **Offline:** everything shown comes from the DB and the preview cache. Import and full-resolution 3D wait for OMV.
- **Tests:** `tests/test_nas_catalogue.py` (offline). The browser check is `tests/ui_nas.cjs`, read-only against a
  running server.
