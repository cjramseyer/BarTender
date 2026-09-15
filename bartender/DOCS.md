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
- Use **Settings -> API Settings -> Test External API Access** to validate config.
- Admin validation endpoint: `POST /api/settings/external-auth/test`
- Admin UI remains on Home Assistant ingress.

### POS Sync Providers

The built-in POS sync provider catalog currently includes:

- Arryved
- Clover
- Lightspeed
- MOCK (testing and sandbox use)
- Square
- Toast

Custom static providers can also be configured from **Settings -> POS Sync** when
the built-in providers do not cover the installation's POS system. The currently
selected provider is shown in the Settings About panel.

## Features

- **Dashboard** — Live overview of all taps with their assigned kegs and status
- **Bar Stock** — Inventory tracking for bottles, spirits, mixers, and other supplies
- **Beer Catalog** — Manage reusable beer records for consistent keg assignment
- **Keg Management** — Track keg inventory, lifecycle, fill-level data, and on-deck status while selecting beer details from the catalog
- **Tap Management** — Assign kegs to numbered, labelled tap lines with single-tap keg assignment protection
- **Data Backup & Restore** — Export portable JSON or ZIP archive with date-stamped filenames; import with preview and replace/merge mode
- **Display View** — Minimal read-only tap board for a wall display
- **Printable Menu** — Printer-friendly "currently on tap" menu page with optional QR code
- **Settings** — Bar name/logo, measurement, theme, bar stock toggle, API Reference nav visibility toggle, external URL override, external API scoped token/allowlist/rate-limit controls, Team Access (owner profile, per-user PIN, reset PIN, disable/enable), pour mode in Pour Presets, keg type choices/default, pour defaults, dashboard button position, and printable menu QR mode in General
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
- Added Team Access enhancements: owner profile name, user PINs, reset PIN, and disable/enable controls.
- Added tap assignment guardrails that prevent selecting kegs already connected to other taps.
- Added bar stock category/size defaults and promotion of frequently used custom sizes.
- Updated export download filenames to include UTC date stamps.

## Configuration

Most BarTender settings are managed from within the web UI after the add-on starts.
The add-on configuration also provides:

- `session_timeout_minutes`: idle timeout for signed-in browser sessions. The default is
  `240` minutes (4 hours); valid values range from 5 minutes to 240 minutes. Change this
  under the add-on's **Configuration** tab and restart the add-on for the new value to
  take effect.

Sessions are signed with a secret persisted in `/data/.secret_key`, so normal add-on
restarts do not invalidate active sessions. Removing the add-on's stored data or
changing an explicitly supplied `SECRET_KEY` invalidates existing sessions.

The Settings UI provides separate mobile and pour-station idle timeouts. Both default
to 30 minutes and accept values from 5 minutes through the global timeout. A user can
choose **Use this device as a pour station** at login; station sessions use the station
timeout while retaining the normal application permissions.

BarTender records active browser sessions with the signed-in user, login method,
device category, user-agent, IP address, login time, last activity, expiration, and
revocation state. Session records are retained for up to 90 days after expiration or
revocation and are limited to the most recent 500 records. Device classification uses
conservative user-agent matching; BarTender does not use invasive browser fingerprinting.
IP addresses and user-agent strings are operational security data and should be handled
according to the operator's privacy and retention requirements.

## Licensing

The Settings About panel includes an owner-only Licensing section. An owner can start
one local 30-day Pro trial or paste a signed Pro license token issued by a separate
licensing service. Paid tokens are verified with the Ed25519 public key configured by
the add-on's `license_public_key` option; the private signing key never belongs in the
add-on or this repository.

The licensing state is provider-neutral and records the plan, license type, expiration,
and feature claims locally. Existing `brewery_type` profile behavior remains compatible
for current installations while individual Pro feature gates are migrated to the
central licensing state. Expired or absent licensing state reports the Base plan without
deleting application data.

For Flutter web or another browser client hosted on a different origin, configure the
add-on option `cors_allowed_origins` as a comma-, space-, or newline-separated list of
trusted origins, such as `http://127.0.0.1:5055,https://mobile.example`. Leave it
empty unless browser cross-origin access is required. Wildcard `*` access is not used.

## Browser Support

BarTender is intended for modern browsers. The practical supported baseline is:

| Browser                | Minimum version | Approximate release age |
| ---------------------- | --------------: | ----------------------: |
| Chrome                 |     60 or newer |           About 9 years |
| Firefox                |     54 or newer |           About 9 years |
| Safari on macOS        |     11 or newer |           About 9 years |
| Safari on iOS/iPadOS   |     11 or newer |           About 9 years |
| Chromium-based Edge    |     79 or newer |         About 6.5 years |
| Android Chrome/WebView |     67 or newer |           About 8 years |
| Samsung Internet       |      8 or newer |           About 8 years |

Internet Explorer 11, Edge Legacy, Safari 10 and older, iOS 10 and older, and old
embedded Android WebViews are not supported. Very old browsers may fail to load the
application because it uses `fetch`, Promises, `async`/`await`, modern DOM APIs,
template literals, and `CSS.escape`.

Some features have additional browser requirements:

- Station registration requires cookies. Private browsing, blocked cookies, or a
  separate browser profile will not retain a station registration.
- Copy buttons use the Clipboard API, which generally requires HTTPS or localhost
  and may be blocked by embedded WebViews or browser permissions.
- Printing QR/NFC credentials opens a popup, so popup blocking can prevent the print
  window from opening.
- QR generation requires the server-side QR dependency; it does not depend on the
  browser's QR scanner.
- NFC writing is not performed by BarTender's browser UI. The generated login URL
  must be copied or written using device-supported NFC tools.
- Mobile session classification uses the browser user-agent and may be affected by
  tablet or "request desktop site" modes.

## Core Usage Flows

### Kegs

- Add or bulk-create keg entries.
- Fill empty kegs from the Beer Catalog.
- Record pours (Manual mode) to decrement volume and update fill percent.
- When a keg reaches cleaning status, use the clean workflow to reset it to ready defaults.

### Taps

- Create taps and assign full/on-tap kegs.
- Kegs already assigned to another tap are shown as in-use and cannot be selected.
- Use pour presets to record standard pours.
- Monitor volume remaining and fill level from dashboard or taps views.

### Bar Stock

- Track inventory items with quantity, category, and notes.
- Use built-in category suggestions (spirits, mixers, and common bar groupings).
- Standard size options include 12 oz bottle, 16 oz bottle, and 12 oz can.
- Frequently used custom sizes are promoted into main size options.
- Disable the Bar Stock feature from Settings if not needed.

## Key Settings

- **Pour Mode**: Manual, POS (API), Inline Device (planned) (Settings -> Pour Presets)
- **Keg Types**: editable keg/container options and defaults
- **Pour Presets**: named preset volumes and default preset
- **Analytics**: low-keg threshold and days-left forecasting window
- **Menu QR**: where QR appears on display/print output (Settings -> General)

## Troubleshooting

- **Modal closes unexpectedly**: update to latest build where overlay-close behavior is locked down for edit dialogs.
- **On Deck cannot be set**: keg must be filled (or on-tap) before On Deck can be enabled.
- **Keg needs cleaning**: mark clean from the kegs workflow to reset fill/beer fields.
- **QR unavailable**: verify dependencies from `requirements.txt` are installed.
- **Display not reachable externally**: confirm display port mapping and host networking in your add-on environment.
- **User cannot sign in**: check whether the user is disabled or requires a user PIN.

## Support

- [Open an issue](https://github.com/cjramseyer/BarTender/issues)
- [View the source](https://github.com/cjramseyer/BarTender)

## Full Documentation

- Core app and add-on reference: [../docs/core-app.md](../docs/core-app.md)
- Mobile app guide: [../docs/mobile-app.md](../docs/mobile-app.md)
- Getting started guide: [../docs/getting-started.md](../docs/getting-started.md)
- Troubleshooting and FAQ: [../docs/troubleshooting-faq.md](../docs/troubleshooting-faq.md)
