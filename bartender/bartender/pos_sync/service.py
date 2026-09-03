"""POS sync normalization, status helpers, and sync runner."""

from datetime import datetime, timezone
import re

from .adapters.base import PosSyncAdapter, PosSyncPayload, PosTapRecord
from .adapters.built_in_providers import (
    ArryvedPosAdapter,
    CloverPosAdapter,
    LightspeedPosAdapter,
    SquarePosAdapter,
    ToastPosAdapter,
)
from .adapters.mock_provider import MockPosAdapter

POS_SYNC_PROVIDERS: dict[str, type[PosSyncAdapter]] = {
    "arryved": ArryvedPosAdapter,
    "clover": CloverPosAdapter,
    "lightspeed": LightspeedPosAdapter,
    "mock": MockPosAdapter,
    "square": SquarePosAdapter,
    "toast": ToastPosAdapter,
}

BUILT_IN_PROVIDER_NAMES: dict[str, str] = {
    "arryved": "Arryved",
    "clover": "Clover",
    "lightspeed": "Lightspeed",
    "mock": "MOCK",
    "square": "Square",
    "toast": "Toast",
}


class PosSyncError(Exception):
    """Domain error for POS sync operations."""

    def __init__(self, message: str, hint: str = "", status_code: int = 400):
        super().__init__(message)
        self.hint = hint
        self.status_code = status_code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def normalize_pos_sync_provider(value) -> str:
    return normalize_pos_sync_provider_with_custom(value, set())


def normalize_pos_sync_provider_with_custom(value, custom_provider_keys: set[str]) -> str:
    provider = str(value or "").strip().lower()
    supported = set(POS_SYNC_PROVIDERS.keys()) | set(custom_provider_keys)
    return provider if provider in supported else ""


def _normalize_provider_key(value) -> str:
    normalized = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9_-]+", "-", normalized)
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized[:48]


def _normalize_provider_mode(value) -> str:
    mode = str(value or "static").strip().lower()
    if mode in ("static",):
        return mode
    return "static"


def _normalize_static_taps(value) -> list[dict]:
    rows = value if isinstance(value, list) else []
    normalized: list[dict] = []
    seen_numbers: set[int] = set()

    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            number = int(row.get("number", 0) or 0)
        except (TypeError, ValueError):
            continue
        if number <= 0 or number in seen_numbers:
            continue
        seen_numbers.add(number)

        normalized.append(
            {
                "number": number,
                "label": str(row.get("label", "") or "").strip()[:80],
                "item_name": str(row.get("item_name", "") or "").strip()[:120],
                "serving_size": str(row.get("serving_size", "") or "").strip()[:40],
                "price_label": str(row.get("price_label", "") or "").strip()[:32],
                "available": _coerce_bool(row.get("available"), True),
            }
        )

    return normalized


def _normalize_custom_provider_entry(value) -> dict | None:
    if not isinstance(value, dict):
        return None

    key = _normalize_provider_key(value.get("key"))
    if not key or key in POS_SYNC_PROVIDERS:
        return None

    name = str(value.get("name", "") or "").strip()[:80]
    if not name:
        name = key.upper()

    mode = _normalize_provider_mode(value.get("mode"))
    static_taps = _normalize_static_taps(value.get("static_taps", []))
    return {
        "key": key,
        "name": name,
        "mode": mode,
        "static_taps": static_taps,
    }


def normalize_pos_sync_custom_providers(value) -> list[dict]:
    raw = value if isinstance(value, list) else []
    normalized: list[dict] = []
    seen_keys: set[str] = set()
    for entry in raw:
        parsed = _normalize_custom_provider_entry(entry)
        if not parsed:
            continue
        key = parsed["key"]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        normalized.append(parsed)
    return normalized


def normalize_pos_sync_credentials(value) -> dict[str, str]:
    raw = value if isinstance(value, dict) else {}
    normalized: dict[str, str] = {}
    for key in ("api_key", "location_id", "merchant_id"):
        normalized[key] = str(raw.get(key, "") or "").strip()[:256]
    return normalized


def normalize_pos_sync_status(value) -> str:
    status = str(value or "never").strip().lower()
    if status in ("never", "success", "failed"):
        return status
    return "never"


def normalize_pos_sync_counts(value) -> dict[str, int]:
    raw = value if isinstance(value, dict) else {}

    def _as_int(item) -> int:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            return 0
        return max(0, parsed)

    return {
        "items_received": _as_int(raw.get("items_received", 0)),
        "taps_updated": _as_int(raw.get("taps_updated", 0)),
        "taps_created": _as_int(raw.get("taps_created", 0)),
    }


def normalize_pos_sync_settings(settings: dict) -> None:
    settings["pos_sync_enabled"] = _coerce_bool(settings.get("pos_sync_enabled"), False)
    settings["pos_sync_custom_providers"] = normalize_pos_sync_custom_providers(
        settings.get("pos_sync_custom_providers", [])
    )
    custom_keys = {
        str(provider.get("key", "")).strip().lower()
        for provider in settings.get("pos_sync_custom_providers", [])
        if isinstance(provider, dict)
    }
    settings["pos_sync_provider"] = normalize_pos_sync_provider_with_custom(
        settings.get("pos_sync_provider"),
        custom_keys,
    )
    settings["pos_sync_credentials"] = normalize_pos_sync_credentials(
        settings.get("pos_sync_credentials", {})
    )
    settings["pos_sync_last_run_at"] = str(settings.get("pos_sync_last_run_at", "") or "").strip()
    settings["pos_sync_last_status"] = normalize_pos_sync_status(settings.get("pos_sync_last_status"))
    settings["pos_sync_last_error"] = str(settings.get("pos_sync_last_error", "") or "").strip()[:500]
    settings["pos_sync_last_counts"] = normalize_pos_sync_counts(settings.get("pos_sync_last_counts", {}))


def get_pos_sync_status(settings: dict) -> dict:
    normalize_pos_sync_settings(settings)
    return {
        "enabled": settings["pos_sync_enabled"],
        "provider": settings["pos_sync_provider"],
        "last_run_at": settings["pos_sync_last_run_at"],
        "last_status": settings["pos_sync_last_status"],
        "last_error": settings["pos_sync_last_error"],
        "last_counts": settings["pos_sync_last_counts"],
    }


def get_pos_provider_catalog(settings: dict) -> list[dict]:
    normalize_pos_sync_settings(settings)
    catalog = []
    for key in sorted(POS_SYNC_PROVIDERS.keys()):
        catalog.append(
            {
                "key": key,
                "name": BUILT_IN_PROVIDER_NAMES.get(key, key.upper()),
                "mode": "adapter",
                "source": "built_in",
            }
        )
    for provider in settings.get("pos_sync_custom_providers", []):
        if not isinstance(provider, dict):
            continue
        catalog.append(
            {
                "key": str(provider.get("key", "")).strip().lower(),
                "name": str(provider.get("name", "") or "").strip()[:80] or str(
                    provider.get("key", "")
                ).upper(),
                "mode": str(provider.get("mode", "static")).strip().lower() or "static",
                "source": "custom",
            }
        )
    return catalog


def add_or_update_custom_provider(settings: dict, provider: dict) -> dict:
    normalize_pos_sync_settings(settings)
    parsed = _normalize_custom_provider_entry(provider)
    if not parsed:
        raise PosSyncError(
            "Invalid provider payload.",
            hint="Set a unique provider key (not built-in) and valid fields.",
            status_code=400,
        )

    providers = settings.get("pos_sync_custom_providers", [])
    providers = providers if isinstance(providers, list) else []

    replaced = False
    for index, existing in enumerate(providers):
        if not isinstance(existing, dict):
            continue
        existing_key = str(existing.get("key", "")).strip().lower()
        if existing_key == parsed["key"]:
            providers[index] = parsed
            replaced = True
            break

    if not replaced:
        providers.append(parsed)

    settings["pos_sync_custom_providers"] = normalize_pos_sync_custom_providers(providers)
    normalize_pos_sync_settings(settings)
    return parsed


def import_custom_providers(settings: dict, payload) -> dict:
    normalize_pos_sync_settings(settings)

    if isinstance(payload, dict) and isinstance(payload.get("providers"), list):
        candidates = payload.get("providers", [])
    elif isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        candidates = [payload]
    else:
        raise PosSyncError(
            "Invalid provider import payload.",
            hint="Submit a provider object or list under 'providers'.",
            status_code=400,
        )

    added_or_updated = 0
    for candidate in candidates:
        parsed = _normalize_custom_provider_entry(candidate)
        if not parsed:
            continue
        add_or_update_custom_provider(settings, parsed)
        added_or_updated += 1

    normalize_pos_sync_settings(settings)
    return {
        "added_or_updated": added_or_updated,
        "catalog": get_pos_provider_catalog(settings),
    }


def mark_pos_sync_failed(settings: dict, error: str, hint: str = "") -> dict:
    normalize_pos_sync_settings(settings)
    details = str(error or "POS sync failed.").strip()[:500]
    detail_hint = str(hint or "").strip()[:500]
    if detail_hint:
        details = f"{details} Hint: {detail_hint}"

    settings["pos_sync_last_run_at"] = _now_iso()
    settings["pos_sync_last_status"] = "failed"
    settings["pos_sync_last_error"] = details
    settings["pos_sync_last_counts"] = {
        "items_received": 0,
        "taps_updated": 0,
        "taps_created": 0,
    }
    return get_pos_sync_status(settings)


def _next_tap_id(taps: list[dict]) -> int:
    if not taps:
        return 1
    return max(int(tap.get("id", 0) or 0) for tap in taps) + 1


def perform_pos_sync(data: dict) -> dict:
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    normalize_pos_sync_settings(settings)

    if not settings.get("pos_sync_enabled"):
        raise PosSyncError(
            "POS sync is disabled.",
            hint="Enable POS sync in Settings before running a sync.",
            status_code=409,
        )

    provider = settings.get("pos_sync_provider", "")
    if not provider:
        raise PosSyncError(
            "POS sync provider is not configured.",
            hint="Choose a provider in Settings before running a sync.",
            status_code=400,
        )

    adapter_cls = POS_SYNC_PROVIDERS.get(provider)
    if adapter_cls is not None:
        adapter = adapter_cls()
        payload = adapter.pull_snapshot(settings.get("pos_sync_credentials", {}))
    else:
        custom_providers = settings.get("pos_sync_custom_providers", [])
        custom_map = {
            str(item.get("key", "")).strip().lower(): item
            for item in custom_providers
            if isinstance(item, dict)
        }
        custom_provider = custom_map.get(provider)
        if custom_provider is None:
            raise PosSyncError(
                f"POS sync provider '{provider}' is not supported.",
                hint="Select a supported provider in Settings.",
                status_code=400,
            )

        mode = str(custom_provider.get("mode", "static")).strip().lower()
        if mode != "static":
            raise PosSyncError(
                f"Provider '{provider}' mode '{mode}' is not supported yet.",
                hint="Use mode 'static' for imported providers.",
                status_code=400,
            )

        static_rows = _normalize_static_taps(custom_provider.get("static_taps", []))
        payload = PosSyncPayload(
            taps=[
                PosTapRecord(
                    number=int(row.get("number", 0) or 0),
                    label=str(row.get("label", "") or "").strip(),
                    item_name=str(row.get("item_name", "") or "").strip(),
                    serving_size=str(row.get("serving_size", "") or "").strip(),
                    price_label=str(row.get("price_label", "") or "").strip(),
                    available=_coerce_bool(row.get("available"), True),
                )
                for row in static_rows
                if int(row.get("number", 0) or 0) > 0
            ]
        )

    raw_taps = data.get("taps", [])
    taps = raw_taps if isinstance(raw_taps, list) else []
    if raw_taps is not taps:
        data["taps"] = taps

    by_number = {}
    for tap in taps:
        if not isinstance(tap, dict):
            continue
        try:
            number = int(tap.get("number", 0) or 0)
        except (TypeError, ValueError):
            continue
        if number > 0:
            by_number[number] = tap

    taps_created = 0
    taps_updated = 0
    now = _now_iso()
    for row in payload.taps:
        sync_meta = {
            "provider": provider,
            "item_name": row.item_name,
            "serving_size": row.serving_size,
            "price_label": row.price_label,
            "available": bool(row.available),
            "synced_at": now,
        }

        existing = by_number.get(row.number)
        if existing is None:
            created = {
                "id": _next_tap_id(taps),
                "number": row.number,
                "label": row.label or f"Tap {row.number}",
                "keg_id": None,
                "notes": "",
                "ever_assigned_keg": False,
                "pos_sync": sync_meta,
            }
            taps.append(created)
            by_number[row.number] = created
            taps_created += 1
            continue

        if row.label:
            existing["label"] = row.label
        existing["pos_sync"] = sync_meta
        taps_updated += 1

    settings["pos_sync_last_run_at"] = now
    settings["pos_sync_last_status"] = "success"
    settings["pos_sync_last_error"] = ""
    settings["pos_sync_last_counts"] = {
        "items_received": len(payload.taps),
        "taps_updated": taps_updated,
        "taps_created": taps_created,
    }
    return get_pos_sync_status(settings)
