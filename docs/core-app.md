# BarTender Core App and Add-on Documentation

Last updated: 2026-08-20
Doc scope: Home Assistant add-on (web app + display service)

## Overview

BarTender is a Home Assistant add-on for bar management. It provides:

- A management web UI for stock, kegs, taps, and settings.
- A JSON REST API used by web and mobile clients.
- A read-only display service for wallboard-style viewing.
- A printer-friendly menu page that lists currently assigned taps.

Primary runtime components:

- Management server: Flask app on port 8099 (ingress-enabled).
- Display server: Flask app on port 8100 (read-only).
- Persistent storage: JSON file at /data/bartender.json.

## Feature Set

- Dashboard overview of taps, kegs, and counts.
- Bar stock CRUD (create, list, update, delete).
- Keg CRUD with lifecycle tracking and cleaning constraints.
- Tap CRUD with keg assignment.
- Settings management (bar name, measurement, theme, bar stock toggle, default keg type, dashboard manage button position, printable menu QR mode).
- Pour workflow and current keg volume tracking.
- Export/import backups as versioned JSON and ZIP archives with preview.
- Read-only display page.
- Runtime QR generation endpoint for printable menu.

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
- Accepted keys: measurement, theme, bar_name, dashboard_manage_button_position, bar_stock_enabled, default_keg_type, menu_qr_mode.
- Request example:

```json
{
  "measurement": "metric",
  "theme": "dark",
  "bar_name": "Garage Taproom"
}
```

- 200 response: updated settings object.

menu_qr_mode values:

- off
- display
- print
- both

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

- Deletes keg only when it is not assigned to any tap.
- 200 response:

```json
{ "ok": true }
```

- 409 response example:

```json
{
  "error": "This keg is assigned to one or more taps. Disconnect it from all taps (or delete those taps) before deleting the keg.",
  "code": "KEG_ASSIGNED_TO_TAP",
  "tap_count": 1,
  "tap_numbers": [1]
}
```

Valid keg statuses:

- full
- in_use
- empty
- cleaning
- retired

Lifecycle and validation rules:

- Kegs in cleaning state can only transition to empty (clean).
- Only one keg can be marked as the line-cleaning keg.
- Kegs that were previously filled and reach empty are moved to cleaning.
- Kegs marked full must include a keg name and beer details (type or brewer).

POST /api/kegs/<id>/pour

- Records a pour amount and decrements current_volume.
- Request body:

```json
{
  "amount": 12,
  "unit": "oz"
}
```

- Validation:
  - amount must be greater than zero
  - current_volume must be set and greater than zero
  - pour amount cannot exceed current_volume
  - unsupported unit conversions are rejected

- Behavior:
  - current_volume is updated atomically
  - when volume reaches zero, status becomes cleaning if keg was previously filled; otherwise empty

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

- Downloads portable versioned JSON backup as bartender_export.json.

GET /api/export/archive

- Downloads ZIP archive backup as bartender_export.zip.
- Includes section JSON files, versioned full JSON payload, and convenience CSV files.

### Printable Menu and QR

GET /menu

- Renders printer-friendly "currently on tap" menu content.

GET /api/menu/qr

- Generates a PNG QR code that points to /menu on the current host.
- 503 response when QR dependencies are unavailable.

GET /api/menu/qr/health

- Returns QR runtime readiness.
- 200 when ready, 503 with error details when dependencies are missing.

GET /api/export/csv

- Legacy alias for archive ZIP export (bartender_export.zip).

### Import

POST /api/import/archive/preview

- Previews archive import result without writing data.
- Accepts multipart file upload and optional mode=replace|merge.

POST /api/import/archive

- Imports ZIP backup.
- Accepts multipart file upload and optional mode=replace|merge.

POST /api/import/json/preview

- Previews JSON import result without writing data.
- Accepts multipart file upload and optional mode=replace|merge.

POST /api/import/json

- Imports versioned JSON backup.
- Accepts multipart file upload and optional mode=replace|merge.

Import mode behavior:

- replace: overwrite current settings, kegs, taps, and bar stock.
- merge: merge settings keys and upsert kegs/taps/stock by id.

## Error Behavior

Common API error patterns:

- 404 with {"error":"Not found"} for missing stock/keg/tap id on update.
- 2xx for successful CRUD operations.
- 5xx for malformed unexpected input or server errors.

Notes:

- API validates keg lifecycle transitions, pour constraints, and import payloads.
- Clients should treat non-2xx as failure and display returned error context.

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
- dashboard_manage_button_position: string (top-right, bottom-left, bottom-right)
- bar_stock_enabled: boolean
- default_keg_type: string

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
- beer_brewer: string
- beer_abv: string
- beer_ibu: string
- beer_brewed_on: date string (YYYY-MM-DD)
- line_cleaning_keg: boolean
- current_volume: number or null
- volume_unit: string (oz, gal, ml, l)
- brewery: string (legacy compatibility mirror)
- abv: string (legacy compatibility mirror)
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

- Export portable versioned JSON and ZIP archive backups.
- Import JSON and ZIP backups with preview and explicit replace/merge mode.

Operational recommendation:

- Use preview before import to verify counts and selected mode.
- Store periodic JSON and ZIP backups outside Home Assistant host for recovery.

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
