# BarTender

Manage your home bar with a web UI built into Home Assistant.

## Features

- **Dashboard** — Live overview of all taps with their assigned kegs and status
- **Bar Stock** — Inventory tracking for bottles, spirits, mixers, and other supplies
- **Beer Catalog** — Manage reusable beer records for consistent keg assignment
- **Keg Management** — Track keg inventory and lifecycle while selecting beer details from the catalog
- **Tap Management** — Assign kegs to numbered, labelled tap lines
- **Data Backup & Restore** — Export portable JSON or ZIP archive; import with preview and replace/merge mode
- **Display View** — Minimal read-only tap board for a wall display
- **Printable Menu** — Printer-friendly "currently on tap" menu page with optional QR code
- **Settings** — Bar name/logo, measurement, theme, bar stock toggle, API Reference nav visibility toggle, keg type choices/default, pour defaults, dashboard button position, and printable menu QR mode
- **Pour Workflow** — Track pours and automatically decrement current keg volume
- **API Reference + Tester** — Built-in endpoint docs and in-app request tester UI

## Recent Changes

- Added keg volume tracking and pour workflow via `POST /api/kegs/<id>/pour`.
- Added backup restore support with import preview and explicit `replace`/`merge` modes.
- Added portable versioned JSON backup export (`GET /api/export/json`).
- Added ZIP archive backup export (`GET /api/export/archive`) and kept `GET /api/export/csv` as a legacy alias.
- Added JSON and ZIP import endpoints:
  - `POST /api/import/json/preview`
  - `POST /api/import/json`
  - `POST /api/import/archive/preview`
  - `POST /api/import/archive`
- Added default name auto-increment in UI for new kegs (`Keg N`) and new taps (`Tap N`).
- Added keg full-status validation: kegs marked `full` must include name and beer details.
- Added stricter keg lifecycle rules:
  - only one line-cleaning keg can exist at a time
  - cleaning status can only transition back to empty (clean)
  - previously filled kegs that reach empty transition to cleaning
- Added printable menu route (`GET /menu`) and runtime QR generation endpoint (`GET /api/menu/qr`).
- Added QR health endpoint (`GET /api/menu/qr/health`) and settings control for display/print behavior.
- Added dashboard tap pour controls with preset selection.
- Updated pour behavior so pouring adjusts both `current_volume` and `percent_full`, with automatic `full` to `in_use` transition on first pour.
- Updated keg edit behavior so changing `current_volume` auto-adjusts `percent_full` when percent is not explicitly set.
- Added Beer Catalog page and beer CRUD APIs (`/api/beers`).
- Added keg-to-beer linking so beer details are selected from a catalog, including fill-keg beer selection.
- Added in-app API Reference page (`/api-reference`) with an API request tester.
- Added default pour preset setting applied to pour selectors across dashboard and taps pages.

## Configuration

No configuration required. All settings are managed from within the web UI after the add-on starts.

## Support

- [Open an issue](https://github.com/cjramseyer/BarTender/issues)
- [View the source](https://github.com/cjramseyer/BarTender)

## Full Documentation

- Core app and add-on reference: [../docs/core-app.md](../docs/core-app.md)
- Mobile app guide: [../docs/mobile-app.md](../docs/mobile-app.md)
- Getting started guide: [../docs/getting-started.md](../docs/getting-started.md)
- Troubleshooting and FAQ: [../docs/troubleshooting-faq.md](../docs/troubleshooting-faq.md)
