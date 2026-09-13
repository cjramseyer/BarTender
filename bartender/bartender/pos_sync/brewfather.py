"""Secure, read-only Brewfather recipe and batch synchronization."""

from datetime import datetime, timezone
import base64
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BREWFATHER_API_BASE_URL = "https://api.brewfather.app/v2"
BREWFATHER_MANAGED_FIELDS = (
    "name",
    "type",
    "style_guideline",
    "brewer",
    "abv",
    "ibu",
    "color_srm",
    "color_ebc",
    "brewed_on",
    "packaged_on",
    "description",
    "notes",
    "recipe_url",
)


class BrewfatherError(Exception):
    """Safe, user-facing Brewfather error."""

    def __init__(self, message: str, hint: str = "", status_code: int = 502):
        super().__init__(message)
        self.hint = hint
        self.status_code = status_code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_credentials(user_id, api_key) -> dict[str, str]:
    return {
        "user_id": str(user_id or "").strip()[:256],
        "api_key": str(api_key or "").strip()[:256],
    }


def credentials_configured(credentials: dict) -> bool:
    return bool(str(credentials.get("user_id", "")).strip() and str(credentials.get("api_key", "")).strip())


def redact_credentials(credentials: dict) -> dict[str, object]:
    return {
        "configured": credentials_configured(credentials),
        "user_id_configured": bool(str(credentials.get("user_id", "")).strip()),
        "api_key_configured": bool(str(credentials.get("api_key", "")).strip()),
    }


def _first(item: dict, *keys, default=""):
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return value
    return default


def _string(value) -> str:
    if isinstance(value, (dict, list)):
        return ""
    return str(value or "").strip()


def _date(value) -> str:
    text = _string(value)
    if not text:
        return ""
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text) / 1000, timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return text[:32]


def _number(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return str(float(value)).rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _string(value)


def _items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "results", "recipes", "batches"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def normalize_recipe(recipe: dict) -> dict:
    recipe_id = _string(_first(recipe, "_id", "id", "recipeId"))
    style = _first(recipe, "style", "styleName", "style_guideline")
    style_name = style.get("name", "") if isinstance(style, dict) else style
    return {
        "brewfather_recipe_id": recipe_id,
        "name": _string(_first(recipe, "name", "recipeName", "title")),
        "type": _string(_first(recipe, "type", "category")),
        "style_guideline": _string(style_name),
        "brewer": _string(_first(recipe, "brewer", "brewerName", "author")),
        "abv": _number(_first(recipe, "abv", "alcoholByVolume")),
        "ibu": _number(_first(recipe, "ibu", "IBU")),
        "color_srm": _number(_first(recipe, "color", "srm", "colorSrm")),
        "color_ebc": _number(_first(recipe, "ebc", "colorEbc")),
        "description": _string(_first(recipe, "description", "notes")),
        "notes": _string(_first(recipe, "notes", "tastingNotes")),
        "recipe_url": _string(_first(recipe, "url", "recipeUrl", "webUrl")),
    }


def normalize_batch(batch: dict) -> dict:
    recipe: dict = {}
    recipe_value = batch.get("recipe")
    if isinstance(recipe_value, dict):
        recipe = recipe_value
    recipe_id = _string(_first(batch, "recipeId", "recipe_id")) or _string(_first(recipe, "_id", "id"))
    batch_id = _string(_first(batch, "_id", "id", "batchId"))
    recipe_data = dict(recipe)
    recipe_data.update(batch)
    result = normalize_recipe(recipe_data)
    result.update(
        {
            "brewfather_recipe_id": recipe_id or result["brewfather_recipe_id"],
            "brewfather_batch_id": batch_id,
            "name": _string(_first(batch, "name", "batchName", "title")) or result["name"],
            "brewed_on": _date(_first(batch, "brewDate", "brewedOn", "brew_date")),
            "packaged_on": _date(_first(batch, "packagingDate", "packagedOn", "packaged_date")),
            "abv": _number(_first(batch, "measuredAbv", "abv")) or result["abv"],
            "notes": _string(_first(batch, "tastingNotes", "notes")) or result["notes"],
            "recipe_url": _string(_first(batch, "url", "batchUrl", "webUrl")) or result["recipe_url"],
            "packaging_volume": _first(batch, "packagingVolume", "volume", default=""),
            "packaging_unit": _string(_first(batch, "packagingVolumeUnit", "volumeUnit", default="")),
            "status": _string(_first(batch, "status", "batchStatus")),
        }
    )
    return result


class BrewfatherClient:
    """Small read-only API client with pagination and Retry-After handling."""

    def __init__(
        self,
        user_id: str,
        api_key: str,
        opener=None,
        base_url: str = BREWFATHER_API_BASE_URL,
        sleeper=time.sleep,
        max_retries: int = 2,
    ):
        self.credentials = normalize_credentials(user_id, api_key)
        self.opener = opener or urlopen
        self.base_url = base_url.rstrip("/")
        self.sleeper = sleeper
        self.max_retries = max(0, int(max_retries))

    def _request_json(self, resource: str, params: dict[str, object] | None = None) -> dict | list:
        if not credentials_configured(self.credentials):
            raise BrewfatherError("Brewfather is not configured.", "Set the owner-only User ID and API key first.", 400)
        query = ""
        if params:
            from urllib.parse import urlencode
            query = "?" + urlencode(params)
        token = base64.b64encode(
            f"{self.credentials['user_id']}:{self.credentials['api_key']}".encode("utf-8")
        ).decode("ascii")
        request = Request(
            f"{self.base_url}/{resource.lstrip('/')}{query}",
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            method="GET",
        )
        for attempt in range(self.max_retries + 1):
            try:
                with self.opener(request, timeout=20) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 429:
                    retry_after = getattr(exc, "headers", None)
                    retry_after = retry_after.get("Retry-After", "") if retry_after else ""
                    try:
                        delay = max(0, min(60, int(float(retry_after))))
                    except (TypeError, ValueError):
                        delay = 1
                    if attempt < self.max_retries:
                        self.sleeper(delay)
                        continue
                    raise BrewfatherError(
                        "Brewfather rate limit reached.",
                        f"Retry after {delay} seconds.",
                        429,
                    ) from exc
                if exc.code in (401, 403):
                    raise BrewfatherError("Brewfather authentication failed.", "Check the User ID and read-only API key.", 502) from exc
                raise BrewfatherError("Brewfather API request failed.", f"The provider returned HTTP {exc.code}.", 502) from exc
            except (URLError, TimeoutError, OSError) as exc:
                raise BrewfatherError("Could not reach Brewfather.", "Check network access and try again.", 502) from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BrewfatherError("Brewfather returned invalid data.", "Try the sync again later.", 502) from exc

        raise BrewfatherError("Brewfather API request failed.")

    def fetch_all(self, resource: str, normalizer, limit: int = 100) -> list[dict]:
        records = []
        page = 1
        while page <= 1000:
            payload = self._request_json(resource, {"page": page, "limit": limit})
            page_items = _items(payload)
            records.extend(normalizer(item) for item in page_items)
            if not isinstance(payload, dict) or not page_items:
                break
            total_pages = payload.get("totalPages") or payload.get("total_pages")
            if total_pages:
                try:
                    if page >= int(total_pages):
                        break
                except (TypeError, ValueError):
                    pass
            if len(page_items) < limit and not payload.get("next") and not payload.get("hasNextPage"):
                break
            page += 1
        return records

    def fetch_recipes(self) -> list[dict]:
        return self.fetch_all("recipes", normalize_recipe)

    def fetch_batches(self) -> list[dict]:
        return self.fetch_all("batches", normalize_batch)


def managed_snapshot(record: dict) -> dict:
    return {field: record.get(field, "") for field in BREWFATHER_MANAGED_FIELDS}


def reconcile_beer(beers: list[dict], record: dict, now: str) -> tuple[dict, str, dict | None]:
    batch_id = str(record.get("brewfather_batch_id", "") or "")
    recipe_id = str(record.get("brewfather_recipe_id", "") or "")
    existing = next(
        (beer for beer in beers if batch_id and beer.get("brewfather_batch_id") == batch_id),
        None,
    ) or next(
        (beer for beer in beers if recipe_id and beer.get("brewfather_recipe_id") == recipe_id),
        None,
    )
    if existing is None:
        beer = {
            "id": max([int(item.get("id", 0) or 0) for item in beers] + [0]) + 1,
            **managed_snapshot(record),
            "packaging": "kegged",
            "availability_status": "available",
            "allergens": [],
            "brewfather_recipe_id": recipe_id,
            "brewfather_batch_id": batch_id,
            "brewfather_last_synced_at": now,
            "brewfather_source_snapshot": managed_snapshot(record),
            "brewfather_conflict": None,
            "updated_at": now,
        }
        beers.append(beer)
        return beer, "created", None

    previous = existing.get("brewfather_source_snapshot") or {}
    local_changed = any(existing.get(field, "") != previous.get(field, "") for field in BREWFATHER_MANAGED_FIELDS)
    remote_changed = any(record.get(field, "") != previous.get(field, "") for field in BREWFATHER_MANAGED_FIELDS)
    if local_changed and remote_changed:
        conflict = {
            "id": f"{batch_id or recipe_id}:{now}",
            "brewfather_batch_id": batch_id,
            "brewfather_recipe_id": recipe_id,
            "beer_id": existing.get("id"),
            "fields": {
                field: {"local": existing.get(field, ""), "brewfather": record.get(field, "")}
                for field in BREWFATHER_MANAGED_FIELDS
                if existing.get(field, "") != record.get(field, "")
            },
            "created_at": now,
        }
        existing["brewfather_conflict"] = conflict["id"]
        return existing, "conflict", conflict

    for field in BREWFATHER_MANAGED_FIELDS:
        if not local_changed or existing.get(field, "") == previous.get(field, ""):
            existing[field] = record.get(field, "")
    existing.update(
        {
            "brewfather_recipe_id": recipe_id or existing.get("brewfather_recipe_id", ""),
            "brewfather_batch_id": batch_id or existing.get("brewfather_batch_id", ""),
            "brewfather_last_synced_at": now,
            "brewfather_source_snapshot": managed_snapshot(record),
            "brewfather_conflict": None,
            "updated_at": now,
        }
    )
    return existing, "updated", None
