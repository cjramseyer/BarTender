"""POS sync normalization, status helpers, and sync runner."""

from datetime import datetime, timezone

from .adapters.base import PosSyncAdapter
from .adapters.mock_provider import MockPosAdapter

POS_SYNC_PROVIDERS: dict[str, type[PosSyncAdapter]] = {
    "mock": MockPosAdapter,
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
    provider = str(value or "").strip().lower()
    return provider if provider in POS_SYNC_PROVIDERS else ""


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
    settings["pos_sync_provider"] = normalize_pos_sync_provider(settings.get("pos_sync_provider"))
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
    if adapter_cls is None:
        raise PosSyncError(
            f"POS sync provider '{provider}' is not supported.",
            hint="Select a supported provider in Settings.",
            status_code=400,
        )

    adapter = adapter_cls()
    payload = adapter.pull_snapshot(settings.get("pos_sync_credentials", {}))

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
