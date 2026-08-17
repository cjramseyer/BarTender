# BarTender

[![CI](https://github.com/cjramseyer/BarTender/actions/workflows/ci.yaml/badge.svg)](https://github.com/cjramseyer/BarTender/actions/workflows/ci.yaml)
[![Release](https://github.com/cjramseyer/BarTender/actions/workflows/deploy.yaml/badge.svg)](https://github.com/cjramseyer/BarTender/actions/workflows/deploy.yaml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A Home Assistant add-on for managing your bar — track kegs, taps, and bar stock from a clean web UI with ingress support.

## Features

- **Dashboard** — Live overview of all taps with their assigned kegs and current status
- **Bar Stock** — Inventory tracking for bottles, spirits, mixers, and other bar supplies with quantity and category management
- **Keg Management** — Track kegs by name, type, size, brewery, ABV, and lifecycle status (`full`, `in_use`, `empty`, `cleaning`, `retired`)
- **Tap Management** — Assign kegs to numbered taps and label each line
- **Settings** — Configurable bar name, measurement system (US / metric), and UI theme (light / dark)
- **Data Export** — Export all data as JSON or CSV (full or per-section)
- **Display View** — Minimal read-only tap board suitable for a wall display
- **Ingress** — Runs behind the Home Assistant ingress proxy; no port exposure required

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**.
2. Add this repository URL: `https://github.com/cjramseyer/BarTender`
3. Find **BarTender** in the list and click **Install**.
4. Start the add-on and open the web UI via the **BarTender** sidebar panel.

## Configuration

No required configuration. Optional options can be set in the add-on configuration tab.

| Option | Default | Description |
|--------|---------|-------------|
| _(none)_ | — | All settings are managed from within the web UI |

## REST API

The add-on exposes a JSON REST API at the ingress URL.

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/settings` | Get current settings |
| POST | `/api/settings` | Update settings |
| GET | `/api/stock` | List all bar stock items |
| POST | `/api/stock` | Add a stock item |
| PUT | `/api/stock/<id>` | Update a stock item |
| DELETE | `/api/stock/<id>` | Delete a stock item |
| GET | `/api/kegs` | List all kegs |
| POST | `/api/kegs` | Add a keg |
| PUT | `/api/kegs/<id>` | Update a keg |
| DELETE | `/api/kegs/<id>` | Delete a keg |
| GET | `/api/taps` | List all taps |
| POST | `/api/taps` | Add a tap |
| PUT | `/api/taps/<id>` | Update a tap |
| DELETE | `/api/taps/<id>` | Delete a tap |
| GET | `/api/export/json` | Export all data as JSON |
| GET | `/api/export/csv` | Export all data (or a section) as CSV |

CSV export accepts an optional `?section=stock|kegs|taps` query parameter.

## Supported Architectures

- `amd64`
- `aarch64`
- `armv7`

## Contributing

See [.github/CONTRIBUTING.md](.github/CONTRIBUTING.md) for guidelines.

## License

MIT
