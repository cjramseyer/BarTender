# BarTender Troubleshooting and FAQ

Last updated: 2026-08-22

## Troubleshooting

### Add-on does not open from sidebar

Checks:

- Confirm add-on is running in Home Assistant.
- Open via Home Assistant sidebar entry, not direct unmanaged URL.
- Check add-on logs for startup errors.

### Web UI loads but data is missing

Checks:

- Verify /data/bartender.json exists in add-on data volume.
- Confirm the file is valid JSON.
- Restart add-on and refresh browser.

### API request fails

Checks:

- Test GET /api/settings first to confirm base connectivity.
- Use /api-reference in the app to run requests in the current ingress context.
- Ensure request Content-Type is application/json for POST/PUT.
- Verify IDs exist before PUT/DELETE.

Typical statuses:

- 200/201: success
- 404: record not found for update/delete
- 5xx: server-side failure or malformed input side effects

### Edit button does nothing in web UI

Checks:

- Ensure browser loaded latest JS/HTML after upgrade (hard refresh).
- Confirm item data is present and not malformed.
- Check browser console for JavaScript errors.

### Mobile app cannot connect

Checks:

- URL includes http:// or https://.
- Phone and server are network-reachable.
- Endpoint responds in mobile browser.
- If using ingress path, test a direct reachable endpoint instead.

### Export not downloading

Checks:

- Try JSON export first: /api/export/json
- Try archive export: /api/export/archive
- Confirm browser pop-up/download policies are not blocking files.

### Printable menu QR does not render

Checks:

- Open GET /api/menu/qr/health and verify it returns ok=true.
- If it returns 503, install runtime dependencies from requirements.txt.
- Restart the add-on/runtime after dependency install.
- Reopen /menu and confirm QR mode in Settings is not set to off.

### Import fails or preview is incorrect

Checks:

- Verify backup file is from BarTender export (.json or .zip).
- Use Preview first and confirm chosen mode (replace or merge).
- For JSON import, ensure payload is valid UTF-8 JSON.
- For ZIP import, ensure archive is not corrupted and includes export files.

### Beer cannot be deleted

Checks:

- A beer linked to one or more kegs cannot be deleted.
- Edit affected kegs and clear/reassign the beer first.
- Retry delete from Beers page after unlinking.

## FAQ

Q: Does BarTender support importing data?
A: Yes. It supports JSON and ZIP imports with preview and replace/merge modes.

Q: Where is data stored?
A: In /data/bartender.json inside add-on runtime volume.

Q: Can I run only display mode?
A: Display is a separate read-only service, but it is started alongside management service in current add-on runtime.

Q: Does assigning a keg to a tap set tapped date?
A: Yes, if tapped date is empty. Existing tapped dates are preserved.

Q: What statuses are expected for kegs?
A: full, in_use, empty, cleaning, retired.

Q: Where can I test API calls from the app UI?
A: Use the API Reference page (`/api-reference`) from Settings or the top navigation.

Q: Why can deleting a beer return 409?
A: The beer is still assigned to one or more kegs and must be unlinked first.

Q: Is mobile app read/write?
A: Current mobile app is read-only.

Q: Why does the printable menu show "QR unavailable"?
A: QR dependencies are missing in the running environment. Install requirements and restart, then recheck /api/menu/qr/health.

## Escalation Path

If issues persist:

1. Capture add-on logs.
2. Capture failing API request and response.
3. Open a GitHub issue with reproduction steps and environment details.
