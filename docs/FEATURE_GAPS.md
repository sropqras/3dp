# Farm Control against current farm software (2026-10-23)

This is a survey of what farm-management products offer today for documents/files, products and printers,
compared with Farm Control.

**Sources:**
- vendor documentation and changelogs: SimplyPrint (early-2026 changelog), Printago docs, Obico, Moonraker changelog
  0.9–0.11, Spoolman 0.23/0.24, Fluidd 1.37, Klipper v0.13;
- marketing pages only for SimplyPrint features and Printago add-ons;
- no primary changelogs for 3DQue/AutoFarm3D, 3DPrinterOS or Bambu Farm Manager, so they are left out.

## Where Farm Control already leads

- **Config and macro standard across printers.** No other product has this: audit rules, generated fixes, a gated
  apply with verify and auto-rollback, drift, and now the shared macro library and the Fluidd layout
  (`docs/FARM_STANDARD.md`).
- **Human visual gate and approval hash before every start.** SimplyPrint has file approval; nobody re-checks the
  G-code hash, the opened 3D view and the spool at Start.
- **NAS catalogue matched to what printed where.** None of the products do this.
- **Parametric makers per order line.** Only Printago's "SKU params" comes close.

## Document / file management

| Feature | Elsewhere | Farm Control |
|---|---|---|
| Model and G-code library with previews, time and material | SimplyPrint | **has**: Inventory, Moonraker thumbnails, WebGL viewer |
| Tags (auto from 3MF/slicer metadata), folders | SimplyPrint 2026 | partial: search and group-by, no tags |
| Version history of a model | 3DPrinterOS | partial: sha256 per artefact, no version chain |
| NAS/cloud sync | none | **has**: hourly OMV catalogue, new-file badge, Import ticked |
| Exact time estimates (Klipper Estimator via Moonraker `[analysis]`) | Moonraker 0.10 | missing |

## Product management

| Feature | Elsewhere | Farm Control |
|---|---|---|
| SKU catalogue; order → SKU → parts → jobs | Printago | partial: portfolio products, projects → parts × qty; no SKU code or recipe |
| Product linked to proven settings | none | **has** (proven printer/profile/layer); not yet the default when sending |
| Bill of materials (multi-part product) | Printago (sub-assemblies "coming soon") | partial: project parts incl. bought items |
| Filament cost per print and per product | Printago, SimplyPrint statistics | missing (grams are known) |
| Quote / price from cost | Printago "coming soon" | missing |
| Finished-goods stock | Printago "coming soon" | missing (invtool holds sale stock) |

## Printer management

| Feature | Elsewhere | Farm Control |
|---|---|---|
| Fleet dashboard, cameras, versions, services | everyone | **has** |
| Smart routing (file material/nozzle/colour → printer with that loaded) | SimplyPrint | partial: the profile limits the printers; a person picks one |
| Maintenance by hours, grams, print count or failures; predicted due date | SimplyPrint, Printago | partial: hours only |
| AI failure detection (spaghetti, detachment) | Obico (new model 2026-05) | missing (moonraker-obico runs on enders3dp, but nothing reads it) |
| Spools with locations, QR labels, live deduction | Spoolman (Moonraker `[spoolman]`, Fluidd 1.37) | partial: own spools and ledger, deduction per colour; no labels or locations |
| Update / rollback of Klipper and Moonraker | Moonraker update manager | partial: versions shown and flagged; upgrades done in Fluidd |

## Worth building next (ranked)

1. **Cost per print, per product and per project.**
   - Add a price per kg on spools, plus machine and energy rates per hour.
   - Cost = ledger grams × price + hours × rates.
   - Show it on builds, products and project totals.
2. **Product = SKU with a recipe.**
   - Add a `sku` and `product_parts` (artefact or maker+params, qty, bought item).
   - "Add product × N to project" expands it into project parts.
3. **Proven settings as the default.** Sending a product's part pre-fills its proven printer, profile and layer.
4. **Suggested printer, not auto-start.** Rank printers that are idle, have the right spool loaded, are not
   maintenance-due and are not watch-only.
5. **Optional Spoolman sync.**
   - Use `FARM_SPOOLMAN_URL` and its REST API for prices, locations and QR labels.
   - At Start, call Moonraker's `spoolman_set_active_spool`.
6. **More maintenance triggers.** Grams, print count and failure count, plus "days until due".
7. **Camera failure watch.** Read Obico's status where it runs, or compare snapshots. Alert only, never pause by
   itself.
8. **Artefact versions and tags** taken from slicer metadata.

## Not worth building

- Moonraker `[job_queue]`, auto-start and auto-eject: they conflict with the visual gate.
- Cloud slicing and STEP import.
- Update buttons: Fluidd already does them.
