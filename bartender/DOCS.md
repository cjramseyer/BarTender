# BarTender

Manage your home bar with a web UI built into Home Assistant.

## Installation and Startup

1. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store**.
2. Add repository URL: `https://github.com/cjramseyer/BarTender`.
3. Install **BarTender**.
4. Start the add-on.
5. Open the BarTender sidebar panel.
6. Complete the first-time setup wizard (bar name, measurement, theme).

## Read-Only Access Endpoints

- Display board: `/display`
- Printable menu: `/menu`
- API reference page: `/api-reference`

Use **Settings -> Read-Only External URLs** to copy generated external links.

## External API Integrations (POS/Hardware)

- External API listener: `8110/tcp`
- Token auth header support:
  - `Authorization: Bearer <token>`
  - `X-API-Token: <token>`
- Scoped tokens:
  - Read token for `GET`/`HEAD`/`OPTIONS`
  - Write token for `POST`/`PUT`/`DELETE` (also works for reads)
  - Legacy shared token supported for compatibility
- Optional IP/CIDR allowlist for trusted client networks.
- Configurable per-minute rate limiting.
- Use **Settings -> Features -> Test External API Access** to validate config.
- Admin UI remains on Home Assistant ingress.

## Features

- **Dashboard** — Live overview of all taps with their assigned kegs and status
- **Bar Stock** — Inventory tracking for bottles, spirits, mixers, and other supplies
- **Beer Catalog** — Manage reusable beer records for consistent keg assignment
- **Keg Management** — Track keg inventory, lifecycle, fill-level data, and on-deck status while selecting beer details from the catalog
- **Tap Management** — Assign kegs to numbered, labelled tap lines
- **Data Backup & Restore** — Export portable JSON or ZIP archive; import with preview and replace/merge mode
- **Display View** — Minimal read-only tap board for a wall display
- **Printable Menu** — Printer-friendly "currently on tap" menu page with optional QR code
- **Settings** — Bar name/logo, measurement, theme, bar stock toggle, API Reference nav visibility toggle, external URL override, external API scoped token/allowlist/rate-limit controls, pour mode, keg type choices/default, pour defaults, dashboard button position, and printable menu QR mode
- **Pour Workflow** — Track pours and automatically decrement current keg volume; manual pour controls are hidden when a non-manual pour mode is selected
- **First-Time Setup** — Wizard captures the bar name and initial defaults on first launch
- **Analytics** — Dashboard summaries for recent pours, near-empty kegs, and depletion forecasting
- **API Reference + Tester** — Built-in endpoint docs and in-app request tester UI

## Recent Changes

- Added keg volume tracking and pour workflow via `POST /api/kegs/<id>/pour`.
- Added first-time setup wizard requiring a bar name before initial use.
- Added pour mode settings and conditional pour control visibility.
- Added On Deck keg workflow and dashboard/display sections.
- Added dashboard pour analytics and depletion forecasting summaries.
- Changed bulk create flows to ask for a quantity instead of raw JSON input.
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

## Core Usage Flows

### Kegs

- Add or bulk-create keg entries.
- Fill empty kegs from the Beer Catalog.
- Record pours (Manual mode) to decrement volume and update fill percent.
- When a keg reaches cleaning status, use the clean workflow to reset it to ready defaults.

### Taps

- Create taps and assign full/on-tap kegs.
- Use pour presets to record standard pours.
- Monitor volume remaining and fill level from dashboard or taps views.

### Bar Stock

- Track inventory items with quantity, category, and notes.
- Disable the Bar Stock feature from Settings if not needed.

## Key Settings

- **Pour Mode**: Manual, POS (API), Inline Device (planned)
- **Keg Types**: editable keg/container options and defaults
- **Pour Presets**: named preset volumes and default preset
- **Analytics**: low-keg threshold and days-left forecasting window
- **Menu QR**: where QR appears on display/print output

## Troubleshooting

- **Modal closes unexpectedly**: update to latest build where overlay-close behavior is locked down for edit dialogs.
- **On Deck cannot be set**: keg must be filled (or on-tap) before On Deck can be enabled.
- **Keg needs cleaning**: mark clean from the kegs workflow to reset fill/beer fields.
- **QR unavailable**: verify dependencies from `requirements.txt` are installed.
- **Display not reachable externally**: confirm display port mapping and host networking in your add-on environment.

## Support

- [Open an issue](https://github.com/cjramseyer/BarTender/issues)
- [View the source](https://github.com/cjramseyer/BarTender)

## Full Documentation

- Core app and add-on reference: [../docs/core-app.md](../docs/core-app.md)
- Mobile app guide: [../docs/mobile-app.md](../docs/mobile-app.md)
- Getting started guide: [../docs/getting-started.md](../docs/getting-started.md)
- Troubleshooting and FAQ: [../docs/troubleshooting-faq.md](../docs/troubleshooting-faq.md)
