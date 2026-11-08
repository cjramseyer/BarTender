# BarTender Core App and Add-on Documentation

Last updated: 2026-11-08
Doc scope: Home Assistant add-on (web app + display service)

## Overview

BarTender is a Home Assistant add-on for bar management. It provides:

- A management web UI for stock, beers, kegs, taps, settings, and Pro drink recipes/shopping.
- A JSON REST API used by web and mobile clients.
- A read-only display service for wallboard-style viewing.
- A printer-friendly menu for assigned taps, packaged beers, public bottle/drink stock, and published Pro recipes.

Primary runtime components:

- Management server: Flask app on port 8099 (ingress-enabled).
- Display server: Flask app on port 8100 (read-only).
- Persistent storage: transactional SQLite state at `/data/bartender.db` by default, or a configured PostgreSQL/MariaDB backend. `/data/bartender.json` is a compatibility mirror used by the read-only server; older JSON installations migrate automatically.

## Feature Set

- Dashboard overview of taps, kegs, and counts.
- Beer catalog CRUD (create, list, update, delete).
- Bar stock CRUD (create, list, update, delete).
- Pro drink recipe catalog with ingredient stock links, live availability, traditional methods, and guest-menu publishing.
- Pro Shopping List, with each shortage listed once by drink, shared references, printing, and pre-filled stock creation/replenishment.
- App-Only Stock Categories configured in Settings to exclude recipe extras from public views without per-item checkboxes.
- Keg CRUD with lifecycle tracking and cleaning constraints.
- Tap CRUD with keg assignment.
- Settings management (bar name/logo, measurement, theme, bar stock toggle, API Reference nav toggle, keg type choices/default, default pour preset, dashboard manage button position, printable menu QR mode).
- Team Access management from a dedicated owner/manager-only page with profile PIN controls, user management, and staff restriction.
- Audit Log with role-aware read-only viewing, JSON export, Owner PIN-protected clearing, and inventory activity tracking.
- Pour workflow and current keg volume tracking.
- Export/import backups as versioned JSON and ZIP archives with preview.
- Analytics reset support for clearing historical pour data used in dashboard and forecasting calculations.
- Owner-only, disabled-by-default anonymous usage-statistics opt-in for active-installation estimates.
- Read-only display page.
- In-app API reference and API tester page.
- Runtime QR generation endpoint for printable menu.

## Architecture and Components

```mermaid
flowchart LR
  HA[Home Assistant Ingress] --> WEB[Flask Management App :8099]
  GUEST[Read-only viewers] --> DISP[Flask Display App :8100]

  WEB --> STORE[Transactional state store]
  STORE --> SQLITE[(SQLite bartender.db)]
  STORE -. external alternative .-> DB[(PostgreSQL or MariaDB)]
  WEB --> MIRROR[/data/bartender.json compatibility mirror]
  DISP --> MIRROR
  MOBILE[Flutter Mobile Viewer] --> WEB
```

Code locations:

- Management app routes/API: bartender/bartender/app.py
- Display service: bartender/bartender/display.py
- HTML templates: bartender/bartender/templates/
- Static assets: bartender/bartender/static/

## Configuration Reference

Add-on metadata and runtime behavior are defined in [the add-on configuration](../bartender/config.yaml).

Current add-on options:

- `storage_backend`: `internal` (SQLite default), `postgresql`, or `mariadb`.
- `database_url`: deployment-only connection URL for an external backend; keep credentials private.
- `session_timeout_minutes`: global idle timeout, default 240; allowed range 5-43200 minutes.
- `cors_allowed_origins`: trusted browser origins for cross-origin clients; empty by default.
- `license_portal_url`: optional owner-facing licensing portal link.
- `license_public_key`: Ed25519 public key for signed license verification; never configure a private signing key here.
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
  - 8110/tcp (external integrations API)

Runtime environment variables (set by run.sh):

- INGRESS_PATH: ingress mount path from Home Assistant.
- DATA_DIR: /data
- PORT: 8099
- DISPLAY_PORT: 8100
- EXTERNAL_API_MODE: enables external API-only listener mode.
- EXTERNAL_API_PORT: external integrations API port (default 8110).

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
- Profile and public-stock fields: `brewery_type` selects the homebrewer/pro profile; `public_stock_hidden_categories` lists app-only categories excluded from displays and menus.
- Accepted keys include measurement, theme, bar_name, brewery_type, bar_logo_url, external_base_url, external_api_token_auth_enabled, external_api_token, external_api_read_token, external_api_write_token, owner_pin, external_api_allowlist_enabled, external_api_allowlist, external_api_rate_limit_enabled, external_api_rate_limit_per_minute, audit_retention_days, api_reference_enabled, pour_mode, environment_mode, setup_completed, dashboard_manage_button_position, bar_stock_enabled, public_stock_hidden_categories, analytics_enabled, default_keg_type, keg_type_choices, menu_qr_mode, pour_options, default_pour_preset, analytics_low_keg_threshold_percent, analytics_days_left_method, analytics_days_left_window_days.
- Request example:

```json
{
  "measurement": "metric",
  "theme": "dark",
  "bar_name": "Garage Taproom"
}
```

- 200 response: updated settings object.
- `audit_retention_days` is owner-only; managers cannot update it.

menu_qr_mode values:

- off
- display
- print
- both

### Team Access

Team management is available from the dedicated /team-access page and is restricted to owner/manager roles. Staff users cannot access the page or the management APIs.

GET /api/team/users

- Returns team user list.
- Staff role receives only their own account.

POST /api/team/users

- Creates a user when action is omitted.
- Supports update actions when action is provided:
  - update (role)
  - update_profile (name)
  - set_pin
  - reset_pin
  - set_disabled

POST /api/team/users/delete

- Deletes a non-owner team user.

GET /api/team/audit

- Returns the audit trail for owner, manager, and staff roles.
- Includes Analytics pour events and successful changes to bar stock, kegs, and taps.

GET /api/team/audit/export

- Downloads audit events as a date-stamped JSON file.
- Requires owner or manager permissions.

POST /api/team/audit/clear

- Clears all audit events.
- Requires owner permissions and a JSON body containing the configured Owner PIN:

```json
{
  "owner_pin": "1234"
}
```

### Audit Log UI

- The in-app Menu opens `/audit` in a separate read-only window.
- All signed-in users can view events.
- Export is available to owners and managers; the clear control is shown only to owners.
- Audit retention remains configured in **Settings -> Audit Trail** and is owner-only.

POST /api/analytics/reset

- Clears historical pour events used for analytics and depletion forecasting.
- Requires owner permissions.

### Bar Stock

The management app and stock API retain all inventory, including garnishes and recipe extras. Public displays and the menu's **Bottles and Drinks** section filter stock by category only; there is no per-item visibility checkbox.

In **Settings -> General -> App-Only Stock Categories**, select categories to exclude from displays and menus. Standard categories, categories currently used by stock, and previously configured exclusions are available. Exclusions default to Garnish/Garnishes, Syrups, Bitters, Mixers, Tonic / Soda, Juice, Other, Ingredients, Fruit, Sweeteners, Spices, and Ice. Uncategorized items are treated as Other. All categories not excluded remain public, including custom bottle categories.

The `public_stock_hidden_categories` Settings field is an array of category names. Owners and managers can update it through `POST /api/settings`; comparisons ignore case and surrounding whitespace. An empty array displays all stock categories. The selection is preserved in backups and applies identically to both management and read-only display/menu routes. Public menus omit internal stock notes. Excluding a category never removes stock or changes recipe availability.

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

### Drinks and Recipes

With a Pro bar profile, open **Drinks** in the navigation or **Menu -> Bartender Recipes**. Both links are hidden for non-Pro profiles, and direct recipe-page/API access, including Add Classics, returns 403 with `DRINKS_PRO_REQUIRED`. All signed-in team members in a Pro profile can search and read recipes; only owners and managers can create, edit, or delete them. Recipe changes appear in the audit log. Switching away from Pro preserves saved recipes and includes them in backups, but disables recipe access and hides published cocktails/nonalcoholic recipes from both guest-menu servers.

Use **Add Drink** for a custom specification or select one of 33 editable bar starters. The catalog includes Negroni, Manhattan, Daiquiri, Mojito, Old Fashioned, Dry Martini, Vodka Martini, Margarita, Whiskey Sour, Cosmopolitan, Moscow Mule, Gin and Tonic, Tom Collins, Rum and Coke, Cuba Libre, Bloody Mary, Screwdriver, Tequila Sunrise, Mai Tai, Pina Colada, Espresso Martini, Aperol Spritz, French 75, Sidecar, Sazerac, Boulevardier, Paloma, Mint Julep, White Russian, Long Island Iced Tea, Shirley Temple, Virgin Mojito, and Lime Soda.

Owners and managers can use **Add Classics** (`POST /api/drinks/classics`) to add every missing catalog recipe at once. Existing drinks are matched by case-insensitive name and are never overwritten. Repeating the action adds no duplicates. Imported recipes remain staff-only until explicitly published; inventory quantities are unchanged. Exact, unique ingredient-name matches are linked to stock, while all other links must be selected explicitly. The response reports `added` and `skipped` counts, and successful additions are audited.

These recipes are starting bar specifications, not a claim that every bar uses the same ratios. Adjust measures, substitutions, and allergen notes to the house recipe and actual ingredient labels. Optional garnishes and pasteurized egg white are marked explicitly; carbonated ingredients are topped after mixing, not shaken.

Each recipe stores a name, category (`classic`, `house`, `nonalcoholic`), base spirit, flavor, description, preparation method, ordered steps, glassware, ice, garnish, substitutions, allergens, source URL, staff notes, menu price text, publication flag, revision, and update timestamp. Preparation methods are `build`, `stir`, `shake`, `muddle`, and `blend`. Steps should describe chilling, dilution, straining, and when to add ice; carbonated ingredients should be added after shaking, never shaken.

Ingredients have their own names, positive measures, units, optional flags, and nullable `stock_id` links. Units are preserved as entered: `ml`, `cl`, `l`, `oz`, `tsp`, `tbsp`, `dash`, `drop`, and `piece`. The measurement setting supplies the default unit for new ingredients, but never silently converts an existing specification.

Ingredient indicators are calculated from current bar stock whenever recipes are loaded:

- **In stock:** the linked stock item has a known quantity greater than zero.
- **Out of stock:** the linked item has zero or negative quantity.
- **Not in bar stock:** no bar-stock item is selected for the ingredient.
- **Stock item missing:** an imported recipe references a stock item that does not exist.
- **Availability unknown:** the quantity cannot be determined or Bar Stock is disabled.

**Can Make Now** includes only recipes whose required ingredients all have stock on hand. Optional ingredients still show their own indicators but do not block readiness. Stock quantities represent containers/items, not measured liquid remaining; readiness does not guarantee sufficient volume for a serving. Viewing or saving recipes never deducts stock. Deleting stock clears its recipe links while preserving ingredient names, preventing accidental relinking if a numeric stock ID is reused.

Select **Publish on guest menu** to list a drink in the Cocktails or Nonalcoholic Drinks section on both management and read-only printable menus. Guest menus expose names, descriptions, price text, allergens, and availability only; ingredient lists, measures, steps, stock links, substitutions, and staff notes remain private. Unavailable published drinks are labeled **Currently unavailable**; unknown availability is labeled **Ask about availability**.

Recipe API:

| Method | Endpoint               | Behavior                                                                                         |
| ------ | ---------------------- | ------------------------------------------------------------------------------------------------ |
| GET    | `/api/drinks`          | List recipes with computed stock status; supports `q`, `category`, `method`, and `can_make=true` |
| GET    | `/api/drinks/<id>`     | Read one recipe with stock status                                                                |
| POST   | `/api/drinks`          | Create a recipe; owner/manager required                                                          |
| POST   | `/api/drinks/classics` | Add missing classic recipes; owner/manager required; existing drinks are preserved               |
| PUT    | `/api/drinks/<id>`     | Partially update a recipe; owner/manager required                                                |
| DELETE | `/api/drinks/<id>`     | Delete a recipe; owner/manager required                                                          |

The API returns per-ingredient `availability` and `stock_name`, plus recipe-level `can_make`, `missing_ingredients`, and `unknown_ingredients`. Computed fields are not persisted. Invalid specifications or new links to nonexistent stock return 400; insufficient editing permissions return 403; unknown recipes return 404. External tokens alone do not grant the owner/manager role required to edit recipes.

Recipes live in the `drink_recipes` application-state collection using the configured transactional storage backend. JSON backups include recipes; ZIP backups also contain `drink_recipes.json`. Import preview reports recipe counts. Replace restores recipes together with stock; merge updates recipes by ID or appends new IDs. Older backups without recipes remain supported, restoring an empty recipe catalog in replace mode and retaining current recipes in merge mode. Malformed recipe records are excluded during normalization and reflected in preview counts.

### Recipe Shopping List (Pro)

Open **Shopping List** from the Menu dropdown when using a Pro bar profile. The option is hidden for non-Pro profiles, and both `/shopping-list` and `GET /api/shopping-list` return 403 with `SHOPPING_LIST_PRO_REQUIRED` if accessed directly without a Pro profile. This follows the existing profile gate used by other Pro features; it does not introduce a separate license rule. The authenticated page and endpoint derive the list from every saved recipe, including staff-only drinks, without storing another inventory collection. Drinks and recipe stock indicators also require a Pro profile; saved records remain intact when changing profiles.

- Ingredients whose linked stock quantity is zero or negative appear as **Out of stock**.
- Ingredients without a selected bar-stock item appear as **Not in bar stock**; links to deleted or absent stock appear as **Stock item missing**.
- Linked ingredients are combined by stock ID. Unlinked ingredients are combined by case-insensitive name with normalized whitespace. Distinct linked stock products remain separate.
- The page groups ingredients into alphabetically ordered drink sections. Each ingredient appears only once, under the first alphabetical drink that needs it, with references to all other affected drinks. Ingredients required by any drink sort before ingredients that are optional for every drink. Empty sections are omitted, while the summary still counts all affected drinks.
- In-stock ingredients and ingredients with unknown quantities are excluded. When Bar Stock is disabled, the page reports unknown availability rather than inventing shortages.
- Stock replenishment, recipe editing, and recipe deletion update the list on its next load. Merely viewing or printing the list never changes inventory.

Use **Print List** for a paper copy; navigation and action controls are omitted from print output. Recipe names link back to the relevant expanded recipe. No purchase quantities are calculated because container counts cannot reliably represent the volume required for an unknown number of servings.

Each item has **Add to Bar Stock**, which opens the existing dialog on the Bar Stock page. New items start with the ingredient name, quantity 1, category Other, and recipe names in Notes. Existing out-of-stock items instead open **Restock Item** with their category, container size, and notes preserved. A recipe measure is never used as a container size. Adjust the fields and save to return to the shopping list.

Creating stock from this workflow automatically links the affected missing recipe ingredients to the new item and audits those links. Replenishing an existing item updates its original record without creating a duplicate. Saved positive quantities remove the resulting shortage on the next list load. Stale shopping-item requests return 409 without creating stock; failed saves leave the dialog open with an error message.

The API retains its combined, deduplicated array of items with `name`, nullable `stock_id`, `availability`, `optional`, and a `recipes` array of `{id, name, optional}` references. Drink grouping is applied to the page, not to the API contract. All signed-in team roles in a Pro profile can view it; unauthenticated API access returns 401.

### Beers

GET /api/beers

- Returns list of beer catalog entries.

POST /api/beers

- Creates a beer catalog entry.
- Required field: name.
- New catalog fields are optional, so existing beer records remain compatible.
- Request example:

```json
{
  "name": "House IPA",
  "type": "IPA",
  "style_guideline": "BJCP 21C",
  "brewer": "Local Brewing",
  "abv": "6.2",
  "ibu": "45",
  "brewed_on": "2026-08-15",
  "packaged_on": "2026-08-29",
  "best_by_date": "2026-11-29",
  "availability_status": "seasonal",
  "description": "Citrus-forward hazy IPA.",
  "allergens": ["Wheat"],
  "color_srm": "6",
  "serving_temperature": "38-42 F",
  "glassware": "Pint",
  "supplier": "Local Supply",
  "sku": "IPA-001",
  "recipe_url": "https://example.com/recipes/house-ipa",
  "notes": "Seasonal batch"
}
```

PUT /api/beers/<id>

- Updates provided beer fields.
- Behavior: linked kegs are refreshed with the updated beer details.

Beer catalog CSV import and export use the canonical column order documented in `bartender/README.md`. The `allergens` CSV column accepts a comma-separated value and is stored as a list.

DELETE /api/beers/<id>

- Deletes beer entry if not linked to a keg.
- 409 response includes linked keg names when deletion is blocked.

### Kegs

GET /api/kegs

- Returns list of kegs.

POST /api/kegs

- Creates a keg.
- If status is omitted, default is empty.
- Request example:

```json
{
  "name": "Keg 12",
  "beer_id": 3,
  "size": "1/6 bbl (5.2 gal)",
  "status": "in_use",
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
- Kegs marked full must include a keg name and beer selection/details.

POST /api/kegs/<id>/fill

- Marks keg as full (or provided target status).
- Supports setting `beer_id` during fill workflow.

Request example:

```json
{
  "beer_id": 3,
  "filled_date": "2026-08-22",
  "percent_full": 100
}
```

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
  - percent_full is reduced proportionally whenever a pour is recorded
  - first successful pour on a full keg transitions status to in_use
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
- 409 response when assigning a keg already connected to another tap:

```json
{
  "error": "Keg is already connected to another tap.",
  "code": "KEG_ALREADY_CONNECTED",
  "tap_id": 1,
  "tap_number": 1
}
```

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

POST /api/taps/<id>/pour

- Records a pour against the keg assigned to a tap.
- Request body:

```json
{
  "amount": 16,
  "unit": "oz",
  "preset_name": "Pint"
}
```

- Validation:
  - tap must exist
  - tap must have an assigned keg
  - amount must be greater than zero
  - keg current_volume must be set and greater than zero
  - unsupported unit conversions are rejected

- Behavior:
  - delegates to keg pour logic
  - updates both current_volume and percent_full
  - applies the same status transitions as keg pour

### Export

GET /api/export/json

- Downloads portable versioned JSON backup as bartender_export_YYYY-MM-DD.json.

GET /api/export/archive

- Downloads ZIP archive backup as bartender_export_YYYY-MM-DD.zip.
- Includes section JSON files, versioned full JSON payload, and convenience CSV files.

GET /api/beers/export/csv

- Downloads beer catalog CSV as beers_YYYY-MM-DD.csv.
- Includes all beer catalog fields using the canonical CSV header order.

### Printable Menu and QR

GET /api-reference

- Renders in-app API documentation and test runner UI.
- Supports trying GET/POST/PUT/DELETE requests against ingress-relative endpoints.

GET /menu

- Renders assigned taps, packaged beers, and public stock in Bottles and Drinks. Pro profiles also show published Cocktails and Nonalcoholic Drinks sections. Internal notes, recipe steps, and app-only stock categories are omitted.

GET /api/menu/qr

- Generates a PNG QR code that points to the canonical external read-only menu URL (host + DISPLAY_PORT, default 8100).
- 503 response when QR dependencies are unavailable.

GET /api/menu/qr/health

- Returns QR runtime readiness.
- 200 when ready, 503 with error details when dependencies are missing.

GET /api/export/csv

- Legacy alias for archive ZIP export (bartender_export_YYYY-MM-DD.zip).

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

- replace: overwrite current settings, beers, drink recipes, kegs, taps, and bar stock.
- merge: merge settings keys and upsert beers/drink recipes/kegs/taps/stock by id.

## Error Behavior

Common API error patterns:

- 404 with {"error":"Not found"} for missing stock/keg/tap id on update.
- 2xx for successful CRUD operations.
- 5xx for malformed unexpected input or server errors.

Notes:

- API validates keg lifecycle transitions, pour constraints, and import payloads.
- Clients should treat non-2xx as failure and display returned error context.

## Data Model Reference

Primary inventory document structure (additional audit, session, and device collections are also stored):

```json
{
  "settings": {},
  "bar_stock": [],
  "beers": [],
  "drink_recipes": [],
  "kegs": [],
  "taps": []
}
```

settings fields:

- measurement: string (us or metric expected by UI)
- theme: string (light or dark)
- bar_name: string
- brewery_type: string (homebrewer or pro; the legacy commercial value normalizes to pro)
- dashboard_manage_button_position: string (top-right, bottom-left, bottom-right)
- bar_stock_enabled: boolean
- public_stock_hidden_categories: array of app-only category names excluded from public displays and menus
- default_keg_type: string
- menu_qr_mode: string (off, display, print, both)
- pour_options: array of preset objects (name, amount, unit)
- default_pour_preset: string encoded as amount|unit|name
- anonymous_telemetry_enabled: boolean (owner-only; default false)
- anonymous_telemetry_installation_id: locally generated UUID, created only after opt-in
- anonymous_telemetry_last_heartbeat_date: UTC date of the last successful heartbeat

### Anonymous Usage Statistics

Owners can enable **Settings -> Privacy -> Share Anonymous Usage Statistics**. When enabled, the management server sends at most one HTTPS heartbeat per UTC day. The payload contains only:

```json
{
  "installation_id": "locally-generated-uuid",
  "app_version": "version",
  "addon_version": "version",
  "brewery_type": "homebrewer"
}
```

The feature is disabled by default. Its payload does not include a bar name, Home Assistant or device identifier, user data, inventory, audit events, credentials, or logs. As with any HTTPS request, the receiving service can process the request's source IP address at the network layer; BarTender does not include it in the telemetry payload or store it locally.

beer catalog fields:

- id: integer
- name: string
- type: string
- style_guideline: string
- packaging: string (`kegged` or `bottled_can`)
- brewer: string
- brewery: string
- abv: string
- ibu: string
- brewed_on: date string (YYYY-MM-DD)
- packaged_on: date string (YYYY-MM-DD)
- best_by_date: date string (YYYY-MM-DD)
- availability_status: string (`available`, `seasonal`, `coming_soon`, or `retired`)
- description: string for customer-facing menu copy
- allergens: array of strings
- color_srm: numeric string
- color_ebc: numeric string
- serving_temperature: string
- glassware: string
- supplier: string
- distributor: string
- sku: string
- upc: string
- recipe_url: string
- notes: string
- updated_at: ISO datetime string (UTC)

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
- serial_number: string
- beer_id: integer or null
- beer_name: string
- type: string
- size: string
- custom_size: string
- status: string
- coupler_type: string
- ownership_type: string
- location: string
- serving_psi: string
- gas_type: string
- beer_brewer: string
- beer_abv: string
- beer_ibu: string
- beer_brewed_on: date string (YYYY-MM-DD)
- line_cleaning_keg: boolean
- on_deck: boolean
- current_volume: number or null
- volume_unit: string (oz, gal, ml, l)
- brewery: string (legacy compatibility mirror)
- abv: string (legacy compatibility mirror)
- notes: string
- tapped_date: date string (YYYY-MM-DD)
- filled_date: date string (YYYY-MM-DD)
- kicked_date: date string (YYYY-MM-DD)
- cleaned_date: date string (YYYY-MM-DD)
- percent_full: integer (0-100)
- created_at: ISO datetime string (UTC)
- updated_at: ISO datetime string (UTC)
- tapped_date: date string (YYYY-MM-DD)
- updated_at: ISO datetime string (UTC)

Backward compatibility:

- Incoming legacy purchased_date values are mapped to filled_date.
- When current_volume is edited directly and percent_full is omitted, percent_full is auto-scaled to match the new volume.

tap fields:

- id: integer
- number: integer
- label: string
- location: string
- status: string (`active`, `cleaning`, `maintenance`, `offline`)
- tap_handle: string
- faucet_type: string
- line_length_feet: string
- line_inner_diameter: string
- line_material: string
- target_pressure_psi: string
- target_temperature: string
- clean_interval_days: integer
- last_cleaned_date: date string (YYYY-MM-DD)
- last_serviced_date: date string (YYYY-MM-DD)
- keg_id: integer or null
- ever_assigned_keg: boolean
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
