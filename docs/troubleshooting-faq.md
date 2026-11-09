# BarTender Troubleshooting and FAQ

Last updated: 2026-11-08

## Troubleshooting

### Add-on does not open from sidebar

Checks:

- Confirm add-on is running in Home Assistant.
- Open via Home Assistant sidebar entry, not direct unmanaged URL.
- Check add-on logs for startup errors.

### Web UI loads but data is missing

Checks:

- For internal storage, verify `/data/bartender.db` exists in the add-on data volume. Older JSON-only installations migrate automatically.
- `/data/bartender.json` is the compatibility mirror read by the display service; it is not the primary SQLite database.
- For external storage, check the add-on's storage_backend and database_url configuration and database connectivity.
- Use export/import for recovery rather than editing the JSON mirror while the app is running.
- Restart add-on and refresh browser.

### API request fails

Checks:

- Test GET /api/settings first to confirm base connectivity.
- Use /api-reference in the app to run requests in the current ingress context.
- Ensure request Content-Type is application/json for POST/PUT.
- Verify IDs exist before PUT/DELETE.

Typical statuses:

- 200/201: success
- 400: invalid fields or recipe specification
- 401: authentication required
- 403: insufficient permissions or a required Pro profile is not selected
- 404: record not found for update/delete
- 409: conflicting records or a stale shopping-list item
- 5xx: server-side failure or malformed input side effects

### User cannot sign in

Checks:

- Confirm the selected account is not disabled in Settings -> Team Access.
- If a user PIN is configured for that account, provide the user PIN at login.
- If owner login is used with multiple users, ensure Owner PIN is configured and entered.
- Mobile app sign-in always requires a PIN. The owner signs in with the Owner PIN from Settings -> Team Access; other team members use their user PIN.

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

### Drinks or Shopping List is missing

- Select the Pro bar profile in Settings. Drinks is in the navigation and Menu -> Bartender Recipes; Shopping List is only in the Menu dropdown.
- Direct non-Pro requests return 403 with DRINKS_PRO_REQUIRED or SHOPPING_LIST_PRO_REQUIRED.
- Staff can view recipes and shopping lists, but only owners/managers can edit recipes or use Add Classics.
- A profile change does not delete saved recipes; they remain in storage and backups.

### Stock item is absent from a display or menu

- Check the item's category against Settings -> General -> App-Only Stock Categories.
- Selected categories remain available in the management app but are excluded from both public display/menu servers. Uncategorized stock is treated as Other.
- Correct the item's category or change the category selection in Settings. No per-item visibility checkbox is required.
- Cocktail recipe publication is separate: select Publish on guest menu on the recipe, and use a Pro profile.

### Ingredient remains on Shopping List after a purchase

- Confirm the stock quantity is positive and the ingredient is associated with that stock record. Similar names alone do not establish an association.
- Add to Bar Stock from the shopping list creates and links new stock, or replenishes an existing record without duplication.
- Saving returns to the refreshed list. A stale item can return 409; refresh before trying again.
- Category visibility settings never change quantities or recipe availability.

## FAQ

Q: Does BarTender support importing data?
A: Yes. It supports JSON and ZIP imports with preview and replace/merge modes.

Q: Where is data stored?
A: In transactional SQLite at `/data/bartender.db` by default, or PostgreSQL/MariaDB when configured. `/data/bartender.json` mirrors state for compatibility and the read-only service.

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

Q: Why does a team member login fail even with the correct username?
A: The account may be disabled or require a user PIN. Check Settings -> Team Access.

Q: Are all 33 classics automatically installed or published?
A: No. An owner/manager imports them with Add Classics or selects individual starters. Existing names are skipped, and imported recipes remain staff-only until explicitly published.

Q: Does Can Make Now guarantee enough liquid for a serving?
A: No. It means all required ingredients have known stock on hand. Stock tracks containers/items, not the remaining measured volume needed for a recipe. Unknown quantities are not treated as available.

Q: Does viewing a recipe or printing Shopping List consume stock?
A: No. Recipe lookup, menu publishing, and printing do not deduct stock.

Q: Why is a shared ingredient listed under only one drink?
A: Each ingredient appears once under the first alphabetical drink that needs it. Also needed for lists the other affected drinks; the summary counts all affected drinks, even when empty sections are omitted.

Q: What does Not in bar stock mean?
A: No stock item is selected for that ingredient. Add/link it from Shopping List or edit the ingredient's stock association. Out of stock instead means a linked record has zero or negative quantity.

Q: Why are garnishes visible in the app but not on the wall display?
A: Their category is selected as app-only in Settings. Internal inventory still includes them so recipe availability and shopping remain accurate.

## Escalation Path

If issues persist:

1. Capture add-on logs.
2. Capture failing API request and response.
3. Open a GitHub issue with reproduction steps and environment details.

Redact API tokens, PINs, license tokens, and database credentials before sharing logs or requests.
