# BarTender Core App and Add-on Documentation

Last updated: 2026-08-19
Doc scope: Home Assistant add-on (web app + display service)

## Overview

BarTender is a Home Assistant add-on for bar management. It provides:

- A management web UI for stock, kegs, taps, and settings.
- A JSON REST API used by web and mobile clients.
- A read-only display service for wallboard-style viewing.

Primary runtime components:

- Management server: Flask app on port 8099 (ingress-enabled).
- Display server: Flask app on port 8100 (read-only).
- Persistent storage: JSON file at /data/bartender.json.

## Feature Set

- Dashboard overview of taps, kegs, and counts.
- Bar stock CRUD (create, list, update, delete).
- Keg CRUD with lifecycle tracking.
- Tap CRUD with keg assignment.
- Settings management (bar name, measurement, theme).
- Export to JSON and CSV (full or section-based).
- Read-only display page.

## Architecture and Components

```mermaid
flowchart LR
  HA[Home Assistant Ingress] --> WEB[Flask Management App :8099]
  HA --> DISP[Flask Display App :8100]

  WEB --> DATA[/data/bartender.json]
  DISP --> DATA
  MOBILE[Flutter Mobile Viewer] --> WEB
```

Code locations:

- Management app routes/API: bartender/bartender/app.py
- Display service: bartender/bartender/display.py
- HTML templates: bartender/bartender/templates/
- Static assets: bartender/bartender/static/

## Configuration Reference

Add-on metadata and runtime behavior are defined in bartender/config.yaml.

Current add-on options:

- No user-configurable options in the add-on config schema.
- UI settings are managed in-app through /api/settings.

Home Assistant add-on metadata:

- slug: bartender
- ingress: true
- panel_title: BarTender
- panel_icon: mdi:beer
- architectures: amd64, aarch64
- declared ports:
  - 8099/tcp (management app)
  - 8100/tcp (display app)

Runtime environment variables (set by run.sh):

- INGRESS_PATH: ingress mount path from Home Assistant.
- DATA_DIR: /data
- PORT: 8099
- DISPLAY_PORT: 8100

## API Reference

Base URL:

- Ingress usage: <ingress-path>
- Direct local/dev usage: http://host:8099

Content type:

- Request: application/json for POST/PUT.
- Response: application/json for API routes.

### Settings

GET /api/settings

- Purpose: Fetch current settings.
- 200 response example:

```json
{
  "measurement": "us",
  "theme": "light",
  "bar_name": "My Bar"
}
```

POST /api/settings

- Purpose: Update one or more settings.
- Accepted keys: measurement, theme, bar_name.
- Request example:

```json
{
  "measurement": "metric",
  "theme": "dark",
  "bar_name": "Garage Taproom"
}
```

- 200 response: updated settings object.

### Bar Stock

GET /api/stock

- Returns list of stock items.

POST /api/stock

- Creates a stock item.
- Request example:

```json
{
  "name": "Tonic Water",
  "category": "Mixers",
  "quantity": 12,
  "unit": "cans",
  "notes": "Keep chilled"
}
```

- 201 response example:

```json
{
  "id": 1,
  "name": "Tonic Water",
  "category": "Mixers",
  "quantity": 12,
  "unit": "cans",
  "notes": "Keep chilled",
  "updated_at": "2026-08-19T18:40:51.123456"
}
```

PUT /api/stock/<id>

- Updates provided fields only.
- 200 response: updated item.
- 404 response:

```json
{ "error": "Not found" }
```

DELETE /api/stock/<id>

- Deletes item.
- 200 response:

```json
{ "ok": true }
```

### Kegs

GET /api/kegs

- Returns list of kegs.

POST /api/kegs

- Creates a keg.
- If status is omitted, default is empty.
- Request example:

```json
{
  "name": "House IPA",
  "type": "IPA",
  "size": "1/6 bbl (5.2 gal)",
  "status": "in_use",
  "brewery": "Local Brewing",
  "abv": "6.2",
  "notes": "Fresh hop batch",
  "filled_date": "2026-08-10"
}
```

- 201 response: created keg object.

PUT /api/kegs/<id>

- Updates provided fields only.
- 200 response: updated keg.
- 404 response:

```json
{ "error": "Not found" }
```

DELETE /api/kegs/<id>

- Deletes keg and unassigns it from taps.
- 200 response:

```json
{ "ok": true }
```

Valid keg statuses:

- full
- in_use
- empty
- cleaning
- retired

### Taps

GET /api/taps

- Returns list of taps.

POST /api/taps

- Creates a tap entry.
- Request example:

```json
{
  "number": 1,
  "label": "Left",
  "keg_id": 5,
  "notes": "Nitro line"
}
```

- 201 response: created tap object.
- Behavior: if keg_id is assigned and keg.tapped_date is missing, tapped_date auto-fills to current UTC date.

PUT /api/taps/<id>

- Updates provided fields only.
- 200 response: updated tap object.
- Same tapped_date auto-fill behavior when assigning a keg.
- 404 response:

```json
{ "error": "Not found" }
```

DELETE /api/taps/<id>

- Deletes tap.
- 200 response:

```json
{ "ok": true }
```

### Export

GET /api/export/json

- Downloads full dataset as bartender_export.json.

GET /api/export/csv

- Downloads CSV export (bartender_export.csv).
- Optional query: section=stock|kegs|taps
- Example:
  - /api/export/csv?section=kegs

## Error Behavior

Common API error patterns:

- 404 with {"error":"Not found"} for missing stock/keg/tap id on update.
- 2xx for successful CRUD operations.
- 5xx for malformed unexpected input or server errors.

Notes:

- There is minimal request validation in current implementation.
- Clients should treat non-2xx as failure and retry or display error context.

## Data Model Reference

Top-level document structure:

```json
{
  "settings": {},
  "bar_stock": [],
  "kegs": [],
  "taps": []
}
```

settings fields:

- measurement: string (us or metric expected by UI)
- theme: string (light or dark)
- bar_name: string

bar_stock item fields:

- id: integer
- name: string
- category: string
- quantity: number
- unit: string
- notes: string
- updated_at: ISO datetime string (UTC)

keg fields:

- id: integer
- name: string
- type: string
- size: string
- custom_size: string
- status: string
- brewery: string
- abv: string
- notes: string
- filled_date: date string (YYYY-MM-DD)
- percent_full: integer (0-100)
- tapped_date: date string (YYYY-MM-DD)
- updated_at: ISO datetime string (UTC)

Backward compatibility:

- Incoming legacy purchased_date values are mapped to filled_date.

tap fields:

- id: integer
- number: integer
- label: string
- keg_id: integer or null
- notes: string
- updated_at: ISO datetime string (UTC)

## Export and Import Behavior

Current support:

- Export only (JSON and CSV).
- No import endpoint in current codebase.

Export examples:

- Full JSON backup: GET /api/export/json
- Keg-only CSV: GET /api/export/csv?section=kegs

Operational recommendation:

- Use JSON export before upgrades or major data edits.
- Store backups outside Home Assistant host for recovery.

## Deployment and Runtime Notes

Home Assistant add-on specifics:

- Ingress is enabled and is the expected access path.
- Management and display services run in one container via run.sh.
- Data persists in /data/bartender.json (survives container restarts).

Build details:

- Base image: ghcr.io/hassio-addons/base:21.0.1
- Python dependencies from bartender/requirements.txt

## FAQ and Troubleshooting

See docs/troubleshooting-faq.md for a full guide.
Quick checks:

- UI not loading: verify add-on is running and open via ingress.
- Data not persisting: confirm /data volume health and permissions.
- API failures: inspect add-on logs and test /api/settings first.

## Documentation Versioning

Documentation is versioned with source control and should be updated with each release tag.
Release checklist recommendation:

1. Update docs affected by behavior/config/API changes.
2. Commit docs in same PR as code changes.
3. Validate README links and examples before release.
