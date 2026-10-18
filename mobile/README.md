# BarTender Mobile

Flutter client for the BarTender API. The app is currently a lightweight mobile
viewer with tap-pour support:

- Dark mode by default, regardless of the device theme.
- View taps, assigned kegs, keg status, and fill levels.
- View keg details and bar stock.
- Record half-pint, pint, or large pours from a tap.
- Pull to refresh each data view.
- Test connectivity and save the configured BarTender server URL locally.
- Select a team member from the server-provided list when signing in.

The client does not yet provide inventory, tap, keg, or team-management workflows.
Native Android/iOS builds are the primary target; Flutter web support is included for
development previews.

## Run Locally

From the repository root:

```powershell
cd mobile
flutter pub get
flutter run
```

Available local targets depend on the installed Flutter SDK and devices:

```powershell
flutter devices
flutter run -d chrome --web-port 5055
```

The web target requires the BarTender server to allow the browser origin through the
`cors_allowed_origins` add-on option. For the local Chrome preview, configure trusted
origins such as:

```yaml
cors_allowed_origins: "http://127.0.0.1:5055,http://localhost:5055"
```

For native Android/iOS clients, CORS does not apply, but the device must be able to
reach the configured server URL.

## Configuration

On first launch, enter a reachable BarTender server URL, for example:

```text
http://10.248.20.10:8099
```

The management port must be exposed by the Home Assistant add-on or provided through
a reachable reverse proxy. Home Assistant ingress URLs are usually session-scoped and
are not a reliable direct mobile API endpoint.

Use **Test connection** on the server screen to verify the server and check that team
members are available. The URL is stored with `shared_preferences`. Use **Change
server** in the app bar to edit it; **Disconnect** clears the saved URL and sign-in.

## API Usage

The client currently calls:

- `GET /api/mobile/users`
- `POST /api/mobile/login`
- `GET /api/taps`
- `GET /api/kegs`
- `GET /api/stock`
- `POST /api/taps/<id>/pour`

API requests use a 10-second timeout. Server-provided JSON error messages are shown to
the user when available.

## Validate Changes

```powershell
cd mobile
dart format --output=none --set-exit-if-changed lib test
flutter analyze
flutter test
```

The repository currently has a smoke test for the setup screen. Add widget and service
tests as mobile features expand.

## More Documentation

See [docs/mobile-app.md](../docs/mobile-app.md) for the product-level mobile guide,
supported browser notes, connectivity assumptions, and current limitations.
