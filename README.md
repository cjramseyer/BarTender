# BarTender

[![CI](https://github.com/cjramseyer/BarTender/actions/workflows/ci.yaml/badge.svg)](https://github.com/cjramseyer/BarTender/actions/workflows/ci.yaml)
[![Release](https://github.com/cjramseyer/BarTender/actions/workflows/deploy.yaml/badge.svg)](https://github.com/cjramseyer/BarTender/actions/workflows/deploy.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

This repository is structured as a Home Assistant add-on repository (like keypad-app).

- Add-on source lives in `bartender/`
- Mobile viewer app lives in `mobile/`

BarTender is a Home Assistant add-on for managing your bar — track kegs, taps, and bar stock from a clean web UI with ingress support.

## Repository Layout

- `repository.yaml` - Home Assistant add-on repository index file
- `bartender/` - BarTender add-on (Dockerfile, config, runtime script, Python app)
- `mobile/` - Flutter mobile read-only viewer

## Add-on Features

- **Dashboard** — Live overview of all taps with their assigned kegs and current status
- **Bar Stock** — Inventory tracking for bottles, spirits, mixers, and other bar supplies with quantity and category management
- **Beer Catalog** — Manage reusable beer records (name, type, brewer, ABV/IBU, brewed date, notes)
- **Keg Management** — Track keg inventory, lifecycle state, fill-level data, and on-deck status; select beer details from the Beer Catalog
- **Tap Management** — Assign kegs to numbered taps and label each line
- **Settings** — Configurable bar name/logo, measurement system (US / metric), UI theme (light / dark), bar stock visibility, API Reference nav visibility, external URL override, external API scoped token/allowlist/rate-limit controls, pour mode, keg type choices/default, and pour defaults
- **Pour Workflow** — Record pours against keg volume with unit conversion and validation; hide pour controls unless Manual mode is selected
- **Setup Wizard** — First-run setup flow that captures the bar name before first use
- **Analytics** — Dashboard summary for recent pours, depletion forecasting, and low-volume alerts
- **Data Backup & Restore** — Export portable versioned JSON or ZIP archive, with import preview and replace/merge modes
- **Display View** — Minimal read-only tap board suitable for a wall display
- **Printable Menu** — Printer-friendly "currently on tap" page with optional QR code linking back to the menu URL
- **API Reference + Tester** — In-app endpoint documentation with a request tester for GET/POST/PUT/DELETE calls
- **Ingress** — Admin UI runs behind the Home Assistant ingress proxy
- **External Integrations API** — Dedicated external API listener for POS/hardware integrations (port `8110`)

## Recent Changes

- Added keg volume tracking and pour workflow via `POST /api/kegs/<id>/pour`.
- Added first-time setup wizard that requires a bar name before initial use.
- Added pour mode setting so pour controls only appear in Manual mode.
- Added On Deck keg workflow and dashboard/display sections for upcoming kegs.
- Added dashboard pour analytics, low-volume alerts, and depletion forecasting.
- Changed bulk create flows to ask for a quantity instead of raw JSON.
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
- Added printable menu route (`GET /menu`) showing currently assigned taps and keg details.
- Added runtime QR generation for printable menu (`GET /api/menu/qr`) with health endpoint (`GET /api/menu/qr/health`).
- Added `menu_qr_mode` setting (`off|display|print|both`) to control QR visibility on screen and print output.
- Added dashboard tap pour controls with preset selection.
- Updated pour behavior so each pour also updates `percent_full`, and first pour transitions keg status from `full` to `in_use`.
- Updated keg edit behavior so changing `current_volume` auto-adjusts `percent_full` when percent is not explicitly set.
- Added Beer Catalog management and linked kegs to selected beers (`beer_id`, `beer_name`) instead of direct beer-detail editing in keg forms.
- Added fill-keg flow that requires selecting a beer from the catalog when marking a keg full.
- Added in-app API Reference page (`/api-reference`) and interactive API tester, with nav visibility controlled from Settings.
- Added default pour preset selection in Settings and automatic preselection anywhere pour presets are shown.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Add this repository URL: `https://github.com/cjramseyer/BarTender`
3. Find **BarTender** in the list and click **Install**.
4. Start the add-on and open the web UI via the **BarTender** sidebar panel.

## Configuration

No required configuration. Optional options can be set in the add-on configuration tab.

| Option   | Default | Description                                     |
| -------- | ------- | ----------------------------------------------- |
| _(none)_ | —       | All settings are managed from within the web UI |

### Network Ports

- `8099` (ingress-only) — Admin web UI and internal API via Home Assistant ingress.
- `8100` (exposed) — Read-only display/menu endpoints.
- `8110` (exposed) — External API listener for integrations.

External API security is configured in **Settings -> Features**:

- Scoped token authentication (`Authorization: Bearer <token>` or `X-API-Token`).
  - Read token: GET/HEAD/OPTIONS
  - Write token: POST/PUT/DELETE (also valid for read)
  - Legacy shared token supported for compatibility.
- Optional IP/CIDR allowlist.
- Configurable per-minute rate limiting.
- Built-in **Test External API Access** button for validation.

## REST API

The add-on exposes a JSON REST API at the ingress URL.

| Method | Endpoint                      | Description                                      |
| ------ | ----------------------------- | ------------------------------------------------ |
| GET    | `/api/settings`               | Get current settings                             |
| POST   | `/api/settings`               | Update settings                                  |
| POST   | `/api/settings/external-auth/test` | Validate external API auth/allowlist/rate-limit readiness |
| GET    | `/api/stock`                  | List all bar stock items                         |
| POST   | `/api/stock`                  | Add a stock item                                 |
| PUT    | `/api/stock/<id>`             | Update a stock item                              |
| DELETE | `/api/stock/<id>`             | Delete a stock item                              |
| GET    | `/api/beers`                  | List all beers                                   |
| POST   | `/api/beers`                  | Add a beer                                       |
| PUT    | `/api/beers/<id>`             | Update a beer                                    |
| DELETE | `/api/beers/<id>`             | Delete a beer                                    |
| GET    | `/api/kegs`                   | List all kegs                                    |
| POST   | `/api/kegs`                   | Add a keg                                        |
| PUT    | `/api/kegs/<id>`              | Update a keg                                     |
| POST   | `/api/kegs/<id>/fill`         | Fill/refill a keg                                |
| POST   | `/api/kegs/<id>/pour`         | Record a pour and reduce current volume          |
| DELETE | `/api/kegs/<id>`              | Delete a keg                                     |
| GET    | `/api/taps`                   | List all taps                                    |
| POST   | `/api/taps`                   | Add a tap                                        |
| PUT    | `/api/taps/<id>`              | Update a tap                                     |
| POST   | `/api/taps/<id>/pour`         | Record a preset/manual pour against assigned keg |
| DELETE | `/api/taps/<id>`              | Delete a tap                                     |
| GET    | `/api/export/json`            | Export portable versioned JSON backup            |
| GET    | `/api/export/archive`         | Export ZIP archive backup                        |
| GET    | `/api/export/csv`             | Legacy alias for archive ZIP export              |
| POST   | `/api/import/archive/preview` | Preview archive import result (replace or merge) |
| POST   | `/api/import/archive`         | Import ZIP archive backup (replace or merge)     |
| POST   | `/api/import/json/preview`    | Preview JSON import result (replace or merge)    |
| POST   | `/api/import/json`            | Import JSON backup (replace or merge)            |
| GET    | `/api/menu/qr`                | Generate printable menu QR code PNG              |
| GET    | `/api/menu/qr/health`         | Check runtime QR dependency readiness            |

Import endpoints accept a multipart file upload plus optional `mode=replace|merge` (default: `replace`).

UI reference route:

- `GET /api-reference` — In-app API documentation and request tester UI

## Supported Architectures

- `amd64`
- `aarch64`

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for guidelines.

## Additional Docs

- Add-on summary: [bartender/DOCS.md](bartender/DOCS.md)
- Core app and add-on reference: [docs/core-app.md](docs/core-app.md)
- Mobile app guide: [docs/mobile-app.md](docs/mobile-app.md)
- Getting started guide: [docs/getting-started.md](docs/getting-started.md)
- Troubleshooting and FAQ: [docs/troubleshooting-faq.md](docs/troubleshooting-faq.md)

## License

MIT
