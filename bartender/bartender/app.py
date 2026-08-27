"""BarTender - Home Assistant Add-on for bar, keg, and tap management."""

import json
import os
import io
import csv
import zipfile
import re
import ipaddress
import secrets
import math
import time
import threading
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

try:
    import qrcode  # type: ignore[reportMissingModuleSource]

    QR_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - environment-specific import failure
    qrcode = None
    QR_IMPORT_ERROR = str(exc)

from flask import (
    Flask,
    render_template,
    request,
    jsonify,
    send_file,
    redirect,
    url_for,
)

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "bartender.json"
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
DISPLAY_PORT = os.environ.get("DISPLAY_PORT", "8100")
EXTERNAL_API_MODE = str(os.environ.get("EXTERNAL_API_MODE", "")).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
EXTERNAL_API_PORT = os.environ.get("EXTERNAL_API_PORT", "8110")
DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE = 120
_EXTERNAL_API_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_EXTERNAL_API_RATE_LIMIT_LOCK = threading.Lock()


def _read_addon_version() -> str:
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("version:"):
                    _, raw_value = stripped.split(":", 1)
                    return raw_value.strip().strip('"\'') or "dev"
    except OSError:
        pass
    return "dev"


APP_VERSION = _read_addon_version()
RELEASE_HIGHLIGHTS = [
    "First-time setup now guides initial bar name configuration.",
    "Pour controls follow the new pour mode setting.",
    "Bulk create now asks how many items to make.",
    "Kegs can be marked as On Deck and surfaced in dashboards.",
    "Dashboard analytics summarize recent pour activity.",
]

STANDARD_KEG_TYPE_CHOICES = [
    "1/6 bbl (5.2 gal)",
    "1/4 bbl (7.75 gal)",
    "1/2 bbl (15.5 gal)",
    "Corny (5 gal)",
    "20 L",
    "30 L",
    "50 L",
    "Custom",
]

app = Flask(__name__)
app.config["APPLICATION_ROOT"] = INGRESS_PATH or "/"


@app.before_request
def enforce_external_api_controls():
    if not EXTERNAL_API_MODE:
        return None

    if not request.path.startswith("/api/"):
        return jsonify({"error": "External API listener exposes API routes only."}), 404

    data = load_data()
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    client_ip = _get_client_ip_address()

    if _coerce_bool(settings.get("external_api_allowlist_enabled"), False):
        allowlist = _parse_ip_allowlist(_normalize_ip_allowlist_text(settings.get("external_api_allowlist", "")))
        if not allowlist:
            return jsonify({"error": "External API allowlist is enabled but empty."}), 403
        if not _is_client_ip_allowed(client_ip, allowlist):
            return jsonify({"error": "Client IP is not allowed."}), 403

    if _coerce_bool(settings.get("external_api_token_auth_enabled"), True):
        presented = _extract_request_api_token()
        legacy_token = _normalize_external_api_token(settings.get("external_api_token", ""))
        read_token = _normalize_external_api_token(settings.get("external_api_read_token", ""))
        write_token = _normalize_external_api_token(settings.get("external_api_write_token", ""))

        if not any((legacy_token, read_token, write_token)):
            return jsonify({"error": "External API token is not configured."}), 503

        request_is_read = _is_read_only_request_method(request.method)
        token_ok = False
        if request_is_read:
            token_ok = any(
                _token_matches(configured, presented)
                for configured in (read_token, write_token, legacy_token)
            )
        else:
            token_ok = any(
                _token_matches(configured, presented)
                for configured in (write_token, legacy_token)
            )

        if not token_ok:
            return jsonify({"error": "Invalid or missing API token."}), 401

    if _coerce_bool(settings.get("external_api_rate_limit_enabled"), True):
        requests_per_minute = _normalize_external_api_rate_limit_per_minute(
            settings.get("external_api_rate_limit_per_minute")
        )
        token_for_limit = _extract_request_api_token()
        if token_for_limit:
            rate_key = f"token:{token_for_limit}"
        elif client_ip is not None:
            rate_key = f"ip:{client_ip.compressed}"
        else:
            rate_key = "ip:unknown"
        allowed, retry_after_seconds = _consume_external_api_rate_limit(
            rate_key,
            requests_per_minute,
        )
        if not allowed:
            response = jsonify(
                {
                    "error": "Rate limit exceeded.",
                    "retry_after_seconds": retry_after_seconds,
                    "requests_per_minute": requests_per_minute,
                }
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(retry_after_seconds)
            return response

    return None


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------

DEFAULT_DATA = {
    "settings": {
        "measurement": "us",
        "theme": "light",
        "bar_name": "My Bar",
        "bar_logo_url": "",
        "external_base_url": "",
        "external_api_token_auth_enabled": True,
        "external_api_token": "",
        "external_api_read_token": "",
        "external_api_write_token": "",
        "external_api_allowlist_enabled": False,
        "external_api_allowlist": "",
        "external_api_rate_limit_enabled": True,
        "external_api_rate_limit_per_minute": DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE,
        "api_reference_enabled": True,
        "pour_mode": "manual",
        "environment_mode": "production",
        "setup_completed": False,
        "dashboard_manage_button_position": "top-right",
        "bar_stock_enabled": True,
        "analytics_enabled": True,
        "default_keg_type": "",
        "keg_type_choices": STANDARD_KEG_TYPE_CHOICES,
        "menu_qr_mode": "both",
        "pour_options": [
            {"name": "Half Pint", "amount": 8, "unit": "oz"},
            {"name": "Pint", "amount": 16, "unit": "oz"},
        ],
        "default_pour_preset": "8|oz|Half Pint",
        "analytics_low_keg_threshold_percent": 25,
        "analytics_days_left_method": "trailing_window",
        "analytics_days_left_window_days": 14,
    },
    "bar_stock": [],
    "beers": [],
    "kegs": [],
    "taps": [],
    "pour_events": [],
}


def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Ensure all top-level keys exist
        for key, value in DEFAULT_DATA.items():
            if key not in data:
                data[key] = value
        # Ensure nested settings keys exist for backward compatibility.
        if not isinstance(data.get("settings"), dict):
            data["settings"] = json.loads(json.dumps(DEFAULT_DATA["settings"]))
        else:
            for key, value in DEFAULT_DATA["settings"].items():
                data["settings"].setdefault(key, value)

        setup_default = str(data["settings"].get("bar_name", "")).strip() not in ("", "My Bar")
        data["settings"]["setup_completed"] = _coerce_bool(
            data["settings"].get("setup_completed"),
            setup_default,
        )
        data["settings"]["pour_mode"] = _normalize_pour_mode(
            data["settings"].get("pour_mode")
        )
        data["settings"]["environment_mode"] = _normalize_environment_mode(
            data["settings"].get("environment_mode")
        )

        # Backward compatibility: migrate legacy manage_button_position key.
        if (
            "dashboard_manage_button_position" not in data["settings"]
            and "manage_button_position" in data["settings"]
        ):
            data["settings"]["dashboard_manage_button_position"] = data["settings"].get(
                "manage_button_position"
            )
        data["settings"].pop("manage_button_position", None)

        if data["settings"].get("dashboard_manage_button_position") not in (
            "top-right",
            "bottom-left",
            "bottom-right",
        ):
            data["settings"]["dashboard_manage_button_position"] = "top-right"

        data["settings"]["bar_stock_enabled"] = _coerce_bool(
            data["settings"].get("bar_stock_enabled"),
            True,
        )
        data["settings"]["analytics_enabled"] = _coerce_bool(
            data["settings"].get("analytics_enabled"),
            True,
        )
        data["settings"]["api_reference_enabled"] = _coerce_bool(
            data["settings"].get("api_reference_enabled"),
            True,
        )
        data["settings"]["menu_qr_mode"] = _normalize_menu_qr_mode(
            data["settings"].get("menu_qr_mode")
        )
        data["settings"]["analytics_low_keg_threshold_percent"] = _normalize_low_keg_threshold(
            data["settings"].get("analytics_low_keg_threshold_percent")
        )
        data["settings"]["analytics_days_left_method"] = _normalize_days_left_method(
            data["settings"].get("analytics_days_left_method")
        )
        data["settings"]["analytics_days_left_window_days"] = _normalize_days_left_window_days(
            data["settings"].get("analytics_days_left_window_days")
        )
        data["settings"]["pour_options"] = _normalize_pour_options(
            data["settings"].get("pour_options"),
            data["settings"].get("measurement", "us"),
        )
        data["settings"]["default_pour_preset"] = _normalize_default_pour_preset(
            data["settings"].get("default_pour_preset", ""),
            data["settings"].get("pour_options", []),
        )
        data["settings"]["bar_logo_url"] = _normalize_logo_url(
            data["settings"].get("bar_logo_url", "")
        )
        data["settings"]["external_base_url"] = _normalize_external_base_url(
            data["settings"].get("external_base_url", "")
        )
        data["settings"]["external_api_token_auth_enabled"] = _coerce_bool(
            data["settings"].get("external_api_token_auth_enabled"),
            True,
        )
        data["settings"]["external_api_token"] = _normalize_external_api_token(
            data["settings"].get("external_api_token", "")
        )
        data["settings"]["external_api_read_token"] = _normalize_external_api_token(
            data["settings"].get("external_api_read_token", "")
        )
        data["settings"]["external_api_write_token"] = _normalize_external_api_token(
            data["settings"].get("external_api_write_token", "")
        )
        data["settings"]["external_api_allowlist_enabled"] = _coerce_bool(
            data["settings"].get("external_api_allowlist_enabled"),
            False,
        )
        data["settings"]["external_api_allowlist"] = _normalize_ip_allowlist_text(
            data["settings"].get("external_api_allowlist", "")
        )
        data["settings"]["external_api_rate_limit_enabled"] = _coerce_bool(
            data["settings"].get("external_api_rate_limit_enabled"),
            True,
        )
        data["settings"]["external_api_rate_limit_per_minute"] = _normalize_external_api_rate_limit_per_minute(
            data["settings"].get("external_api_rate_limit_per_minute")
        )
        data["beers"] = _normalize_beers(data.get("beers", []))
        data["settings"]["keg_type_choices"] = _normalize_keg_type_choices(
            data["settings"].get("keg_type_choices", []),
            data["settings"].get("default_keg_type", ""),
        )
        data["settings"]["default_keg_type"] = _normalize_default_keg_type(
            data["settings"].get("default_keg_type", ""),
            data["settings"].get("keg_type_choices", []),
        )
        beer_types = {
            str(beer.get("type", "")).strip().lower()
            for beer in data.get("beers", [])
            if str(beer.get("type", "")).strip()
        }
        normalized_choices = [
            str(choice).strip()
            for choice in data["settings"].get("keg_type_choices", [])
            if str(choice).strip()
        ]
        if normalized_choices and beer_types:
            choice_keys = {choice.lower() for choice in normalized_choices}
            if choice_keys.issubset(beer_types):
                data["settings"]["keg_type_choices"] = STANDARD_KEG_TYPE_CHOICES.copy()
                data["settings"]["default_keg_type"] = _normalize_default_keg_type(
                    data["settings"].get("default_keg_type", ""),
                    data["settings"].get("keg_type_choices", []),
                )
        data["pour_events"] = [event for event in data.get("pour_events", []) if isinstance(event, dict)]
        beers_by_id = {
            beer.get("id"): beer for beer in data.get("beers", []) if isinstance(beer.get("id"), int)
        }
        # Backward compatibility for stock size fields.
        for item in data.get("bar_stock", []):
            if "size_label" not in item:
                item["size_label"] = item.get("unit", "")
            item.setdefault("size_value", None)
            item.setdefault("size_unit", "")
        # Backward compatibility: migrate legacy purchase_date field to filled_date.
        for keg in data.get("kegs", []):
            if not keg.get("filled_date") and keg.get("purchased_date"):
                keg["filled_date"] = keg.get("purchased_date", "")
            keg.pop("purchased_date", None)
            if "beer_brewer" not in keg:
                keg["beer_brewer"] = keg.get("brewery", "")
            if "beer_abv" not in keg:
                keg["beer_abv"] = keg.get("abv", "")
            keg.setdefault("beer_ibu", "")
            keg.setdefault("beer_brewed_on", "")
            keg.setdefault("beer_type", "")
            keg["beer_id"] = _coerce_int(keg.get("beer_id"), None)
            keg.setdefault("beer_name", "")
            keg.setdefault("on_deck", False)
            keg.setdefault("keg_age_days", 0)
            if keg.get("beer_id") is not None:
                beer = beers_by_id.get(keg.get("beer_id"))
                if beer:
                    _apply_beer_to_keg(keg, beer)
                else:
                    keg["beer_id"] = None
            keg["line_cleaning_keg"] = _coerce_bool(
                keg.get("line_cleaning_keg"),
                False,
            )
            if "current_volume" not in keg:
                keg["current_volume"] = None
            else:
                keg["current_volume"] = _coerce_float(keg.get("current_volume"), None)
            if not keg.get("volume_unit"):
                keg["volume_unit"] = _default_volume_unit(
                    data.get("settings", {}).get("measurement", "us")
                )
            else:
                keg["volume_unit"] = _normalize_volume_unit(keg.get("volume_unit"))
            if keg.get("status") == "filled":
                keg["status"] = "full"
            if "percent_full" not in keg:
                keg["percent_full"] = _default_percent_for_status(keg.get("status", "empty"))
            else:
                keg["percent_full"] = _clamp_percent_full(keg.get("percent_full"), _default_percent_for_status(keg.get("status", "empty")))
            keg["created_at"] = (
                keg.get("created_at")
                or keg.get("updated_at")
                or datetime.now(timezone.utc).isoformat()
            )
            if _coerce_bool(keg.get("on_deck"), False) and not _can_mark_on_deck(keg):
                keg["on_deck"] = False
        for tap in data.get("taps", []):
            tap.setdefault("label", "")
            tap.setdefault("notes", "")
            tap["keg_id"] = _coerce_int(tap.get("keg_id"), None)
            tap["ever_assigned_keg"] = _coerce_bool(tap.get("ever_assigned_keg"), False)
            if tap.get("keg_id") is not None:
                tap["ever_assigned_keg"] = True
        return data
    return json.loads(json.dumps(DEFAULT_DATA))


def save_data(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _next_id(items: list) -> int:
    if not items:
        return 1
    return max(item.get("id", 0) for item in items) + 1


def _coerce_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _bar_stock_enabled(data: dict) -> bool:
    return _coerce_bool(data.get("settings", {}).get("bar_stock_enabled"), True)


def _analytics_enabled(data: dict) -> bool:
    return _coerce_bool(data.get("settings", {}).get("analytics_enabled"), True)


def _normalize_menu_qr_mode(value) -> str:
    mode = str(value or "both").strip().lower()
    if mode in ("off", "display", "print", "both"):
        return mode
    return "both"


def _normalize_pour_mode(value) -> str:
    mode = str(value or "manual").strip().lower()
    if mode in ("manual", "pos", "inline_device"):
        return mode
    return "manual"


def _normalize_environment_mode(value) -> str:
    mode = str(value or "production").strip().lower()
    if mode in ("sandbox", "production"):
        return mode
    return "production"


def _normalize_days_left_method(value) -> str:
    method = str(value or "trailing_window").strip().lower()
    if method in ("trailing_window",):
        return method
    return "trailing_window"


def _normalize_days_left_window_days(value) -> int:
    window_days = _coerce_int(value, 14)
    if window_days is None:
        return 14
    return max(3, min(90, window_days))


def _normalize_low_keg_threshold(value) -> int:
    threshold = _coerce_int(value, 25)
    if threshold is None:
        return 25
    return max(1, min(100, threshold))


def _normalize_logo_url(value) -> str:
    url = str(value or "").strip()
    if len(url) > 2048:
        return ""
    return url


def _normalize_external_base_url(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    parsed = urlsplit(raw)
    scheme = str(parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return ""
    if not parsed.netloc:
        return ""

    normalized_path = parsed.path.rstrip("/")
    return urlunsplit((scheme, parsed.netloc, normalized_path, "", ""))


def _normalize_external_api_token(value) -> str:
    token = str(value or "").strip()
    if len(token) > 512:
        token = token[:512]
    return token


def _normalize_external_api_rate_limit_per_minute(value) -> int:
    parsed = _coerce_int(value, DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE)
    if parsed is None:
        return DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE
    return max(1, min(5000, parsed))


def _normalize_ip_allowlist_text(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    tokens = []
    seen = set()
    for part in re.split(r"[\n,;\s]+", raw):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            continue
        normalized = str(network)
        if normalized in seen:
            continue
        seen.add(normalized)
        tokens.append(normalized)
    return "\n".join(tokens)


def _parse_ip_allowlist(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    parsed = []
    for part in re.split(r"[\n,;\s]+", str(text or "")):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            parsed.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue
    return parsed


def _get_client_ip_address():
    forwarded = str(request.headers.get("X-Forwarded-For", "")).strip()
    candidate = forwarded.split(",")[0].strip() if forwarded else str(request.remote_addr or "").strip()
    if not candidate:
        return None
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _is_client_ip_allowed(
    client_ip,
    allowlist: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    if client_ip is None:
        return False
    for network in allowlist:
        if client_ip.version != network.version:
            continue
        if client_ip in network:
            return True
    return False


def _extract_request_api_token() -> str:
    bearer = str(request.headers.get("Authorization", "")).strip()
    if bearer.lower().startswith("bearer "):
        return bearer[7:].strip()
    return str(request.headers.get("X-API-Token", "")).strip()


def _token_matches(configured_token: str, presented_token: str) -> bool:
    if not configured_token or not presented_token:
        return False
    return secrets.compare_digest(configured_token, presented_token)


def _is_read_only_request_method(method: str) -> bool:
    return str(method or "").upper() in ("GET", "HEAD", "OPTIONS")


def _consume_external_api_rate_limit(key: str, requests_per_minute: int):
    now = time.monotonic()
    window_start = now - 60.0
    with _EXTERNAL_API_RATE_LIMIT_LOCK:
        bucket = _EXTERNAL_API_RATE_LIMIT_BUCKETS.setdefault(key, deque())
        while bucket and bucket[0] <= window_start:
            bucket.popleft()

        if len(bucket) >= requests_per_minute:
            retry_after_seconds = max(1, int(math.ceil(60.0 - (now - bucket[0]))))
            return False, retry_after_seconds

        bucket.append(now)
        return True, 0


def _external_api_listener_base_url() -> str:
    parsed = urlsplit(request.host_url)
    host = parsed.hostname or "localhost"
    port = str(EXTERNAL_API_PORT or "8110").strip()
    netloc = f"{_format_host_for_url(host)}:{port}" if port else parsed.netloc
    return f"{parsed.scheme}://{netloc}"


def _record_pour_event(
    data: dict,
    keg: dict,
    amount: float,
    unit: str,
    source: str,
    tap_id=None,
    preset_name: str = "",
) -> None:
    normalized_unit = _normalize_volume_unit(unit)
    event = {
        "id": _next_id(data.setdefault("pour_events", [])),
        "keg_id": keg.get("id"),
        "tap_id": tap_id,
        "amount": round(float(amount), 3),
        "unit": normalized_unit,
        "amount_ml": round(_convert_volume(float(amount), normalized_unit, "ml") or 0.0, 3),
        "source": source,
        "preset_name": preset_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data.setdefault("pour_events", []).append(event)


def _build_on_deck_kegs(data: dict) -> list[dict]:
    return [
        keg
        for keg in data.get("kegs", [])
        if _coerce_bool(keg.get("on_deck"), False)
        and not _coerce_bool(keg.get("line_cleaning_keg"), False)
        and keg.get("status") not in ("retired",)
    ]


def _build_dashboard_analytics(data: dict) -> dict:
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    measurement = settings.get("measurement", "us")
    display_unit = _default_volume_unit(measurement)
    low_threshold_pct = _normalize_low_keg_threshold(
        settings.get("analytics_low_keg_threshold_percent")
    )
    days_left_method = _normalize_days_left_method(
        settings.get("analytics_days_left_method")
    )
    days_left_window_days = _normalize_days_left_window_days(
        settings.get("analytics_days_left_window_days")
    )
    now = datetime.now(timezone.utc)
    recent_window = now - timedelta(days=7)
    forecast_window = now - timedelta(days=days_left_window_days)

    line_cleaning_keg_ids = {
        keg.get("id")
        for keg in data.get("kegs", [])
        if isinstance(keg, dict)
        and _coerce_bool(keg.get("line_cleaning_keg"), False)
        and keg.get("id") is not None
    }

    events = [
        event
        for event in data.get("pour_events", [])
        if isinstance(event, dict)
        and event.get("keg_id") not in line_cleaning_keg_ids
    ]
    parsed_events = []
    recent_events = []
    for event in events:
        try:
            created_at = datetime.fromisoformat(str(event.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        amount_ml = _coerce_float(event.get("amount_ml"), None)
        if amount_ml is None:
            amount_ml = _convert_volume(
                _coerce_float(event.get("amount"), 0.0) or 0.0,
                event.get("unit"),
                "ml",
            ) or 0.0
        parsed = {
            **event,
            "created_at_dt": created_at,
            "amount_ml": round(amount_ml, 3),
        }
        parsed_events.append(parsed)
        if created_at >= recent_window:
            recent_events.append(parsed)

    total_recent_ml = sum(_coerce_float(event.get("amount_ml"), 0.0) or 0.0 for event in recent_events)
    total_recent_display = _convert_volume(total_recent_ml, "ml", display_unit) or 0.0
    total_pour_ml = sum(_coerce_float(event.get("amount_ml"), 0.0) or 0.0 for event in parsed_events)
    total_pour_display = _convert_volume(total_pour_ml, "ml", display_unit) or 0.0

    taps_by_id = {
        tap.get("id"): tap
        for tap in data.get("taps", [])
        if isinstance(tap, dict) and tap.get("id") is not None
    }
    kegs_by_id = {
        keg.get("id"): keg
        for keg in data.get("kegs", [])
        if isinstance(keg, dict) and keg.get("id") is not None
    }

    tap_totals_ml = {}
    keg_totals_ml = {}
    preset_counts = {}
    for event in parsed_events:
        amount_ml = _coerce_float(event.get("amount_ml"), 0.0) or 0.0
        tap_id = event.get("tap_id")
        keg_id = event.get("keg_id")
        if tap_id is not None:
            tap_totals_ml[tap_id] = tap_totals_ml.get(tap_id, 0.0) + amount_ml
        if keg_id is not None:
            keg_totals_ml[keg_id] = keg_totals_ml.get(keg_id, 0.0) + amount_ml
        preset_name = str(event.get("preset_name", "")).strip()
        if preset_name:
            preset_counts[preset_name] = preset_counts.get(preset_name, 0) + 1

    low_volume_kegs = []
    forecast_items = []
    for keg in data.get("kegs", []):
        if _coerce_bool(keg.get("line_cleaning_keg"), False):
            continue
        current_volume = _coerce_float(keg.get("current_volume"), None)
        if current_volume is None or current_volume <= 0:
            continue
        fill_pct = _clamp_percent_full(
            keg.get("percent_full"),
            _default_percent_for_status(keg.get("status", "empty")),
        )
        if fill_pct <= low_threshold_pct:
            low_volume_kegs.append({
                "id": keg.get("id"),
                "name": keg.get("name") or "Unnamed Keg",
                "fill_pct": fill_pct,
                "current_volume": current_volume,
                "volume_unit": keg.get("volume_unit") or display_unit,
            })

        keg_unit = _normalize_volume_unit(keg.get("volume_unit") or display_unit)
        recent_keg_events = []
        for event in parsed_events:
            if event.get("keg_id") != keg.get("id"):
                continue
            if event.get("created_at_dt") < forecast_window:
                continue
            converted = _convert_volume(
                _coerce_float(event.get("amount_ml"), 0.0) or 0.0,
                "ml",
                keg_unit,
            )
            if converted is not None:
                recent_keg_events.append(converted)

        if not recent_keg_events:
            continue

        daily_rate = sum(recent_keg_events) / float(days_left_window_days)
        if daily_rate <= 0:
            continue

        forecast_items.append({
            "id": keg.get("id"),
            "name": keg.get("name") or "Unnamed Keg",
            "days_remaining": round(current_volume / daily_rate, 1),
            "current_volume": current_volume,
            "volume_unit": keg_unit,
        })

    low_volume_kegs.sort(key=lambda item: item.get("fill_pct", 101))
    forecast_items.sort(key=lambda item: item.get("days_remaining", 9999))

    top_taps = [
        {
            "tap_id": tap_id,
            "tap_number": taps_by_id.get(tap_id, {}).get("number"),
            "label": taps_by_id.get(tap_id, {}).get("label", ""),
            "volume": round(_convert_volume(total_ml, "ml", display_unit) or 0.0, 2),
            "volume_unit": display_unit,
        }
        for tap_id, total_ml in sorted(
            tap_totals_ml.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]

    top_kegs = [
        {
            "keg_id": keg_id,
            "name": (kegs_by_id.get(keg_id, {}) or {}).get("name") or "Unnamed Keg",
            "volume": round(_convert_volume(total_ml, "ml", display_unit) or 0.0, 2),
            "volume_unit": display_unit,
        }
        for keg_id, total_ml in sorted(
            keg_totals_ml.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]

    top_presets = [
        {"name": name, "count": count}
        for name, count in sorted(
            preset_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )
    ]

    recent_events.sort(key=lambda event: event.get("created_at_dt"), reverse=True)
    recent_events_view = []
    for event in recent_events[:25]:
        amount_ml = _coerce_float(event.get("amount_ml"), 0.0) or 0.0
        event_unit = _normalize_volume_unit(event.get("unit"))
        event_amount = _convert_volume(amount_ml, "ml", event_unit)
        recent_events_view.append({
            "created_at": event.get("created_at"),
            "tap_id": event.get("tap_id"),
            "tap_number": taps_by_id.get(event.get("tap_id"), {}).get("number"),
            "keg_id": event.get("keg_id"),
            "keg_name": (kegs_by_id.get(event.get("keg_id"), {}) or {}).get("name") or "Unnamed Keg",
            "amount": round(event_amount or 0.0, 2),
            "unit": event_unit,
            "preset_name": str(event.get("preset_name", "")).strip(),
            "source": event.get("source", ""),
        })

    return {
        "recent_pour_count": len(recent_events),
        "recent_pour_volume": round(total_recent_display, 2),
        "recent_pour_unit": display_unit,
        "total_pour_count": len(parsed_events),
        "total_pour_volume": round(total_pour_display, 2),
        "total_pour_unit": display_unit,
        "low_keg_threshold_percent": low_threshold_pct,
        "days_left_method": days_left_method,
        "days_left_window_days": days_left_window_days,
        "low_volume_kegs": low_volume_kegs,
        "forecast_items": forecast_items,
        "top_taps": top_taps[:5],
        "top_kegs": top_kegs[:5],
        "top_presets": top_presets[:5],
        "recent_events": recent_events_view,
    }


def _format_host_for_url(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _external_readonly_base_url(data: dict | None = None) -> str:
    if isinstance(data, dict):
        settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
        manual_base = _normalize_external_base_url(settings.get("external_base_url", ""))
        if manual_base:
            return manual_base

    parsed = urlsplit(request.host_url)
    host = parsed.hostname or "localhost"
    display_port = str(DISPLAY_PORT or "8100").strip()
    if display_port:
        netloc = f"{_format_host_for_url(host)}:{display_port}"
    else:
        netloc = parsed.netloc
    return f"{parsed.scheme}://{netloc}"


def _external_display_url(data: dict | None = None) -> str:
    return f"{_external_readonly_base_url(data)}/"


def _external_menu_url(data: dict | None = None) -> str:
    return f"{_external_readonly_base_url(data)}/menu"


def _qr_is_available() -> bool:
    return qrcode is not None


def _line_cleaning_keg_conflict(data: dict, candidate_id=None) -> bool:
    for keg in data.get("kegs", []):
        if not _coerce_bool(keg.get("line_cleaning_keg"), False):
            continue
        if candidate_id is not None and keg.get("id") == candidate_id:
            continue
        return True
    return False


def _set_keg_tapped_date_if_missing(data: dict, keg_id) -> None:
    if keg_id is None:
        return
    for keg in data["kegs"]:
        if keg.get("id") == keg_id and not keg.get("tapped_date"):
            keg["tapped_date"] = datetime.now(timezone.utc).date().isoformat()
            break


def _set_filled_date_for_status_transition(keg: dict, incoming_status: str) -> None:
    is_full_transition = incoming_status in ("full", "filled")
    if is_full_transition and not keg.get("filled_date"):
        keg["filled_date"] = _today_utc_date()


def _normalize_keg_status(status):
    if status == "filled":
        return "full"
    return status


def _default_percent_for_status(status: str) -> int:
    status = _normalize_keg_status(status)
    if status == "full":
        return 100
    if status == "in_use":
        return 50
    return 0


def _clamp_percent_full(value, fallback: int) -> int:
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return fallback


def _coerce_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value, default=None):
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _default_volume_unit(measurement: str) -> str:
    return "oz" if measurement == "us" else "ml"


def _default_pour_options(measurement: str) -> list[dict]:
    if measurement == "metric":
        return [
            {"name": "Half Pint", "amount": 237, "unit": "ml"},
            {"name": "Pint", "amount": 473, "unit": "ml"},
        ]
    return [
        {"name": "Half Pint", "amount": 8, "unit": "oz"},
        {"name": "Pint", "amount": 16, "unit": "oz"},
    ]


def _normalize_pour_options(raw_options, measurement: str) -> list[dict]:
    fallback = _default_pour_options(measurement)
    if not isinstance(raw_options, list):
        return fallback

    normalized = []
    for option in raw_options:
        if not isinstance(option, dict):
            continue
        name = str(option.get("name", "")).strip()
        amount = _coerce_float(option.get("amount"), None)
        unit = _normalize_volume_unit(option.get("unit"))
        if not name or amount is None or amount <= 0 or not unit:
            continue
        normalized.append({
            "name": name,
            "amount": round(amount, 3),
            "unit": unit,
        })

    return normalized or fallback


def _pour_option_value(option: dict) -> str:
    name = str(option.get("name", "")).replace("|", "/")
    amount = _coerce_float(option.get("amount"), None)
    unit = _normalize_volume_unit(option.get("unit"))
    if amount is None or not unit:
        return ""
    amount_token = format(amount, "g")
    return f"{amount_token}|{unit}|{name}"


def _parse_pour_preset_value(value: str):
    parts = str(value or "").split("|", 2)
    if len(parts) != 3:
        return None
    amount = _coerce_float(parts[0], None)
    unit = _normalize_volume_unit(parts[1])
    name = str(parts[2]).replace("|", "/")
    if amount is None or not unit:
        return None
    return amount, unit, name


def _normalize_default_pour_preset(raw_default, pour_options: list[dict]) -> str:
    if not pour_options:
        return ""

    parsed_default = _parse_pour_preset_value(str(raw_default or "").strip())
    if parsed_default:
        default_amount, default_unit, default_name = parsed_default
        for option in pour_options:
            option_amount = _coerce_float(option.get("amount"), None)
            option_unit = _normalize_volume_unit(option.get("unit"))
            option_name = str(option.get("name", "")).replace("|", "/")
            if option_amount is None:
                continue
            if (
                abs(option_amount - default_amount) < 1e-9
                and option_unit == default_unit
                and option_name == default_name
            ):
                return _pour_option_value(option)

    return _pour_option_value(pour_options[0])


def _normalize_keg_type_choices(raw_choices, default_type: str) -> list[str]:
    choices = []
    seen = set()

    if isinstance(raw_choices, list):
        for item in raw_choices:
            value = str(item or "").strip()
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            choices.append(value)

    fallback = str(default_type or "").strip()
    fallback_key = fallback.lower()
    if fallback and fallback_key not in seen:
        choices.append(fallback)

    if choices:
        return choices

    return STANDARD_KEG_TYPE_CHOICES.copy()


def _normalize_default_keg_type(raw_default: str, choices: list[str]) -> str:
    default_value = str(raw_default or "").strip()
    if not choices:
        return default_value

    if not default_value:
        return choices[0]

    for item in choices:
        if item.lower() == default_value.lower():
            return item

    return choices[0]


def _normalize_beers(raw_beers) -> list[dict]:
    if not isinstance(raw_beers, list):
        return []

    normalized = []
    seen_ids = set()
    next_generated_id = 1

    for entry in raw_beers:
        if not isinstance(entry, dict):
            continue

        candidate_id = _coerce_int(entry.get("id"), None)
        if (
            candidate_id is None
            or candidate_id <= 0
            or candidate_id in seen_ids
        ):
            while next_generated_id in seen_ids:
                next_generated_id += 1
            candidate_id = next_generated_id
            next_generated_id += 1

        seen_ids.add(candidate_id)
        if candidate_id >= next_generated_id:
            next_generated_id = candidate_id + 1

        normalized.append({
            "id": candidate_id,
            "name": str(entry.get("name", "")).strip(),
            "type": str(entry.get("type", entry.get("style", ""))).strip(),
            "packaging": _normalize_beer_packaging(entry.get("packaging", "kegged")),
            "brewer": str(entry.get("brewer", "")).strip(),
            "brewery": str(entry.get("brewery", "")).strip(),
            "abv": str(entry.get("abv", "")).strip(),
            "ibu": str(entry.get("ibu", "")).strip(),
            "brewed_on": str(entry.get("brewed_on", "")).strip(),
            "notes": str(entry.get("notes", "")).strip(),
            "updated_at": entry.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        })

    return normalized


def _normalize_beer_packaging(value) -> str:
    packaging = str(value or "kegged").strip().lower().replace("/", "_")
    if packaging in ("bottled", "bottle", "can", "canned", "bottled_can"):
        return "bottled_can"
    return "kegged"


def _is_beer_kegged(beer: dict) -> bool:
    return _normalize_beer_packaging(beer.get("packaging")) == "kegged"


def _get_beer_by_id(data: dict, beer_id: int | None):
    if beer_id is None:
        return None
    for beer in data.get("beers", []):
        if beer.get("id") == beer_id:
            return beer
    return None


def _apply_beer_to_keg(keg: dict, beer: dict) -> None:
    keg["beer_id"] = beer.get("id")
    keg["beer_name"] = beer.get("name", "")
    keg["beer_type"] = beer.get("type", "")
    keg["beer_brewer"] = beer.get("brewer", "")
    keg["beer_brewery"] = beer.get("brewery", "")
    keg["beer_abv"] = beer.get("abv", "")
    keg["beer_ibu"] = beer.get("ibu", "")
    keg["beer_brewed_on"] = beer.get("brewed_on", "")
    # Keep legacy keys synchronized for older clients/views.
    keg["brewery"] = keg.get("beer_brewer", "")
    keg["abv"] = keg.get("beer_abv", "")


def _apply_pour_to_keg(data: dict, keg: dict, amount: float, pour_unit: str):
    current_volume = _coerce_float(keg.get("current_volume"), None)
    if current_volume is None:
        return {"error": "Current volume is not set for this keg."}, 400

    if current_volume <= 0:
        return {"error": "No volume remaining in this keg."}, 409

    keg_unit = _normalize_volume_unit(
        keg.get("volume_unit")
        or _default_volume_unit(data.get("settings", {}).get("measurement", "us"))
    )
    normalized_pour_unit = _normalize_volume_unit(pour_unit or keg_unit)

    converted_amount = _convert_volume(amount, normalized_pour_unit, keg_unit)
    if converted_amount is None:
        return {
            "error": f"Unsupported unit conversion: {normalized_pour_unit} to {keg_unit}.",
        }, 400

    if converted_amount > current_volume:
        return {"error": "Pour amount exceeds remaining volume."}, 409

    previous_status = _normalize_keg_status(keg.get("status", "empty"))
    previous_percent = _clamp_percent_full(
        keg.get("percent_full"),
        _default_percent_for_status(keg.get("status", "empty")),
    )
    keg["current_volume"] = max(0.0, round(current_volume - converted_amount, 3))
    keg["volume_unit"] = keg_unit

    if keg["current_volume"] > 0 and current_volume > 0:
        scaled_percent = round(previous_percent * (keg["current_volume"] / current_volume))
        keg["percent_full"] = _clamp_percent_full(scaled_percent, previous_percent)
        if previous_status == "full":
            keg["status"] = "in_use"
    elif keg["current_volume"] <= 0:
        if keg.get("filled_date"):
            keg["status"] = "cleaning"
        else:
            keg["status"] = "empty"
        keg["percent_full"] = 0

    keg["updated_at"] = datetime.now(timezone.utc).isoformat()
    return keg, 200


def _normalize_volume_unit(unit: str | None) -> str:
    if not unit:
        return ""
    normalized = unit.strip().lower()
    aliases = {
        "floz": "oz",
        "fl oz": "oz",
        "fl_oz": "oz",
        "ounce": "oz",
        "ounces": "oz",
        "gallon": "gal",
        "gallons": "gal",
        "milliliter": "ml",
        "milliliters": "ml",
        "millilitre": "ml",
        "millilitres": "ml",
        "liter": "l",
        "liters": "l",
        "litre": "l",
        "litres": "l",
    }
    return aliases.get(normalized, normalized)


def _convert_volume(amount: float, from_unit: str, to_unit: str):
    source = _normalize_volume_unit(from_unit)
    target = _normalize_volume_unit(to_unit)
    if source == target:
        return amount

    to_ml = {
        "ml": 1.0,
        "l": 1000.0,
        "oz": 29.5735,
        "gal": 3785.41,
    }
    if source not in to_ml or target not in to_ml:
        return None

    return (amount * to_ml[source]) / to_ml[target]


def _sync_percent_for_status(keg: dict, status: str, percent_explicit: bool) -> None:
    status = _normalize_keg_status(status)
    if percent_explicit:
        # Preserve user-entered values during add/edit/update flows.
        keg["percent_full"] = _clamp_percent_full(
            keg.get("percent_full"),
            _default_percent_for_status(status),
        )
        return

    if status == "full":
        keg["percent_full"] = 100
        return

    if status in ("empty", "cleaning", "retired"):
        keg["percent_full"] = 0
        return

    if status == "in_use":
        current = _clamp_percent_full(keg.get("percent_full"), 50)
        keg["percent_full"] = current if 0 < current < 100 else 50


def _apply_needs_cleaning_transition(
    previous_keg: dict,
    updated_keg: dict,
    status_explicit: bool,
    percent_explicit: bool,
) -> None:
    """Move previously filled kegs to cleaning when they reach empty."""
    was_previously_filled = bool(previous_keg.get("filled_date"))
    if not was_previously_filled:
        return

    incoming_status = _normalize_keg_status(updated_keg.get("status", "empty"))
    incoming_percent = _clamp_percent_full(
        updated_keg.get("percent_full"),
        _default_percent_for_status(incoming_status),
    )

    reaches_empty = False
    if status_explicit and incoming_status == "empty":
        reaches_empty = True
    if percent_explicit and incoming_percent == 0:
        reaches_empty = True

    if reaches_empty and incoming_status not in ("cleaning", "retired"):
        updated_keg["status"] = "cleaning"
        updated_keg["percent_full"] = 0


def _sync_percent_for_volume_change(
    previous_keg: dict,
    updated_keg: dict,
    current_volume_explicit: bool,
    percent_explicit: bool,
) -> None:
    """When volume is edited directly, keep percent_full in sync unless user set percent explicitly."""
    if not current_volume_explicit or percent_explicit:
        return

    previous_volume = _coerce_float(previous_keg.get("current_volume"), None)
    current_volume = _coerce_float(updated_keg.get("current_volume"), None)

    if current_volume is None:
        return
    if current_volume <= 0:
        updated_keg["percent_full"] = 0
        return

    if previous_volume is None or previous_volume <= 0:
        return

    previous_percent = _clamp_percent_full(
        previous_keg.get("percent_full"),
        _default_percent_for_status(previous_keg.get("status", "empty")),
    )
    scaled_percent = round(previous_percent * (current_volume / previous_volume))
    updated_keg["percent_full"] = _clamp_percent_full(scaled_percent, previous_percent)


def _parse_measurement_token(raw_value: str):
    token = str(raw_value or "").strip()
    if not token:
        return None
    fraction_match = re.fullmatch(r"(\d+)\s*/\s*(\d+)", token)
    if fraction_match:
        numerator = int(fraction_match.group(1))
        denominator = int(fraction_match.group(2))
        if denominator == 0:
            return None
        return numerator / denominator
    return _coerce_float(token, None)


def _extract_keg_capacity(keg: dict):
    candidates = []
    if keg.get("size") == "Custom":
        candidates.append(keg.get("custom_size", ""))
    else:
        candidates.append(keg.get("size", ""))
        candidates.append(keg.get("custom_size", ""))

    pattern = re.compile(
        r"(\d+(?:\.\d+)?|\d+\s*/\s*\d+)\s*(gal|gallons?|oz|ounces?|ml|millilit(?:er|re)s?|l|lit(?:er|re)s?)",
        re.IGNORECASE,
    )

    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue

        preferred_source = text
        paren_match = re.search(r"\(([^)]*)\)", text)
        if paren_match:
            preferred_source = paren_match.group(1)

        matches = pattern.findall(preferred_source) or pattern.findall(text)
        if not matches:
            continue

        amount_raw, unit_raw = matches[-1]
        amount = _parse_measurement_token(amount_raw)
        unit = _normalize_volume_unit(unit_raw)
        if amount is None or amount <= 0 or not unit:
            continue
        return amount, unit

    return None


def _sync_percent_from_current_volume(keg: dict) -> bool:
    current_volume = _coerce_float(keg.get("current_volume"), None)
    if current_volume is None:
        return False
    if current_volume <= 0:
        keg["percent_full"] = 0
        return True

    capacity = _extract_keg_capacity(keg)
    if capacity is None:
        return False

    capacity_amount, capacity_unit = capacity
    volume_unit = _normalize_volume_unit(keg.get("volume_unit"))
    if not volume_unit:
        return False

    converted_current = _convert_volume(current_volume, volume_unit, capacity_unit)
    if converted_current is None:
        return False

    computed_percent = round((converted_current / capacity_amount) * 100)
    keg["percent_full"] = _clamp_percent_full(computed_percent, 0)
    return True


def _is_cleaning_transition_allowed(previous_status: str, next_status: str) -> bool:
    """When a keg needs cleaning, it can only be marked clean (empty)."""
    prev = _normalize_keg_status(previous_status)
    nxt = _normalize_keg_status(next_status)
    if prev != "cleaning":
        return True
    return nxt == "empty"


def _can_mark_on_deck(keg: dict) -> bool:
    status = _normalize_keg_status(keg.get("status", "empty"))
    if status in ("full", "in_use"):
        return True
    return bool(str(keg.get("filled_date", "")).strip())


def _reset_keg_to_clean_ready(keg: dict) -> None:
    keg["status"] = "empty"
    keg["percent_full"] = 0
    keg["filled_date"] = ""
    keg["current_volume"] = 0
    keg["beer_id"] = None
    keg["beer_name"] = ""
    keg["beer_type"] = ""
    keg["type"] = keg.get("type", "")
    keg["beer_brewer"] = ""
    keg["beer_brewery"] = ""
    keg["beer_abv"] = ""
    keg["beer_ibu"] = ""
    keg["beer_brewed_on"] = ""
    keg["brewery"] = ""
    keg["abv"] = ""
    keg["tapped_date"] = ""
    keg["on_deck"] = False
    keg["keg_age_days"] = 0


def _validate_full_keg_requirements(keg_like: dict):
    """Require name + beer details once a keg is full."""
    status = _normalize_keg_status(keg_like.get("status", "empty"))
    if status != "full":
        return None

    name = str(keg_like.get("name", "")).strip()
    beer_value = (
        str(keg_like.get("beer_id", "")).strip()
        or str(keg_like.get("beer_name", "")).strip()
        or str(keg_like.get("type", "")).strip()
        or str(keg_like.get("beer_brewer", "")).strip()
        or str(keg_like.get("brewery", "")).strip()
    )

    missing = []
    if not name:
        missing.append("name")
    if not beer_value:
        missing.append("beer")

    if missing:
        return {
            "error": "Kegs marked Full must include name and beer details.",
            "code": "FULL_KEG_MISSING_REQUIRED_FIELDS",
            "missing": missing,
        }
    return None


@app.context_processor
def inject_runtime_metadata():
    return {
        "app_version": APP_VERSION,
        "release_highlights": RELEASE_HIGHLIGHTS,
    }


# ---------------------------------------------------------------------------
# Routes – pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    data = load_data()
    return render_template(
        "index.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
        on_deck_kegs=_build_on_deck_kegs(data),
        dashboard_analytics=_build_dashboard_analytics(data),
        ingress=INGRESS_PATH,
    )


@app.route("/analytics")
def analytics_view():
    data = load_data()
    if not _analytics_enabled(data):
        return redirect(url_for("index"))
    return render_template(
        "analytics.html",
        settings=data["settings"],
        dashboard_analytics=_build_dashboard_analytics(data),
        ingress=INGRESS_PATH,
    )


@app.route("/stock")
def stock():
    data = load_data()
    if not _bar_stock_enabled(data):
        return redirect(url_for("index"))
    return render_template(
        "stock.html",
        settings=data["settings"],
        bar_stock=data["bar_stock"],
        ingress=INGRESS_PATH,
    )


@app.route("/kegs")
def kegs():
    data = load_data()
    return render_template(
        "kegs.html",
        settings=data["settings"],
        kegs=data["kegs"],
        beers=sorted(data.get("beers", []), key=lambda beer: str(beer.get("name", "")).lower()),
        ingress=INGRESS_PATH,
    )


@app.route("/beers")
def beers():
    data = load_data()
    return render_template(
        "beers.html",
        settings=data["settings"],
        beers=sorted(data.get("beers", []), key=lambda beer: str(beer.get("name", "")).lower()),
        ingress=INGRESS_PATH,
    )


@app.route("/taps")
def taps():
    data = load_data()
    return render_template(
        "taps.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        ingress=INGRESS_PATH,
    )


@app.route("/settings")
def settings():
    data = load_data()
    return render_template(
        "settings.html",
        settings=data["settings"],
        qr_ready=_qr_is_available(),
        qr_error=QR_IMPORT_ERROR,
        display_port=DISPLAY_PORT,
        external_api_port=EXTERNAL_API_PORT,
        external_display_url=_external_display_url(data),
        external_menu_url=_external_menu_url(data),
        auto_external_display_url=_external_display_url(),
        auto_external_menu_url=_external_menu_url(),
        ingress=INGRESS_PATH,
    )


@app.route("/api-reference")
def api_reference():
    data = load_data()
    return render_template(
        "api_reference.html",
        settings=data["settings"],
        endpoints=API_REFERENCE_ENDPOINTS,
        ingress=INGRESS_PATH,
    )


@app.route("/display")
def display_view():
    data = load_data()
    qr_image_path = f"{INGRESS_PATH}/api/menu/qr" if INGRESS_PATH else "/api/menu/qr"
    menu_qr_mode = _normalize_menu_qr_mode(data.get("settings", {}).get("menu_qr_mode"))
    qr_ready = _qr_is_available()
    return render_template(
        "display/index.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
        on_deck_kegs=_build_on_deck_kegs(data),
        qr_image_path=qr_image_path,
        menu_qr_mode=menu_qr_mode,
        qr_ready=qr_ready,
    )


@app.route("/menu")
def menu_view():
    data = load_data()
    kegs_by_id = {
        keg.get("id"): keg for keg in data.get("kegs", []) if isinstance(keg, dict)
    }
    packaged_beers = sorted(
        [
            beer
            for beer in data.get("beers", [])
            if _normalize_beer_packaging(beer.get("packaging", "kegged")) != "kegged"
        ],
        key=lambda beer: str(beer.get("name", "")).lower(),
    )

    on_tap = []
    for tap in sorted(
        data.get("taps", []),
        key=lambda t: (t.get("number") is None, t.get("number", 0), t.get("id", 0)),
    ):
        keg_id = tap.get("keg_id")
        if keg_id is None:
            continue
        keg = kegs_by_id.get(keg_id)
        if not keg:
            continue
        fill_pct = _clamp_percent_full(
            keg.get("percent_full"),
            _default_percent_for_status(keg.get("status", "empty")),
        )
        on_tap.append({
            "tap": tap,
            "keg": keg,
            "fill_pct": fill_pct,
        })

    menu_path = _external_menu_url(data)
    qr_image_path = f"{INGRESS_PATH}/api/menu/qr" if INGRESS_PATH else "/api/menu/qr"
    menu_qr_mode = _normalize_menu_qr_mode(data.get("settings", {}).get("menu_qr_mode"))
    qr_ready = _qr_is_available()
    return render_template(
        "menu.html",
        settings=data["settings"],
        on_tap=on_tap,
        packaged_beers=packaged_beers,
        menu_path=menu_path,
        qr_image_path=qr_image_path,
        menu_qr_mode=menu_qr_mode,
        qr_ready=qr_ready,
        qr_error=QR_IMPORT_ERROR,
        ingress=INGRESS_PATH,
    )


@app.route("/menu/qr-print")
def menu_qr_print_view():
    data = load_data()
    menu_path = _external_menu_url(data)
    qr_image_path = f"{INGRESS_PATH}/api/menu/qr" if INGRESS_PATH else "/api/menu/qr"
    return render_template(
        "menu_qr_print.html",
        settings=data["settings"],
        menu_path=menu_path,
        qr_image_path=qr_image_path,
        qr_ready=_qr_is_available(),
        qr_error=QR_IMPORT_ERROR,
        ingress=INGRESS_PATH,
    )


@app.route("/api/menu/qr")
def api_menu_qr():
    if not _qr_is_available():
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "QR generation dependencies are not installed.",
                    "hint": "Install requirements with: pip install -r requirements.txt",
                    "details": QR_IMPORT_ERROR,
                }
            ),
            503,
        )

    qr_module = qrcode
    if qr_module is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "QR generation dependencies are not installed.",
                    "hint": "Install requirements with: pip install -r requirements.txt",
                    "details": QR_IMPORT_ERROR,
                }
            ),
            503,
        )

    data = load_data()
    menu_url = _external_menu_url(data)

    qr = qr_module.QRCode(box_size=8, border=2)
    qr.add_data(menu_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    out = io.BytesIO()
    img.save(out, "PNG")
    out.seek(0)
    return send_file(
        out,
        mimetype="image/png",
        as_attachment=False,
        download_name="bartender_menu_qr.png",
    )


@app.route("/api/menu/qr/health")
def api_menu_qr_health():
    if _qr_is_available():
        return jsonify(
            {
                "ok": True,
                "qr_ready": True,
            }
        )

    return (
        jsonify(
            {
                "ok": False,
                "qr_ready": False,
                "error": "QR generation dependencies are not installed.",
                "hint": "Install requirements with: pip install -r requirements.txt",
                "details": QR_IMPORT_ERROR,
            }
        ),
        503,
    )


# ---------------------------------------------------------------------------
# API – Settings
# ---------------------------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    data = load_data()
    return jsonify(data["settings"])


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = load_data()
    body = request.get_json(force=True)
    allowed = {
        "measurement",
        "theme",
        "bar_name",
        "bar_logo_url",
        "external_base_url",
        "external_api_token_auth_enabled",
        "external_api_token",
        "external_api_read_token",
        "external_api_write_token",
        "external_api_allowlist_enabled",
        "external_api_allowlist",
        "external_api_rate_limit_enabled",
        "external_api_rate_limit_per_minute",
        "api_reference_enabled",
        "pour_mode",
        "environment_mode",
        "setup_completed",
        "dashboard_manage_button_position",
        "bar_stock_enabled",
        "analytics_enabled",
        "default_keg_type",
        "keg_type_choices",
        "menu_qr_mode",
        "pour_options",
        "default_pour_preset",
        "analytics_low_keg_threshold_percent",
        "analytics_days_left_method",
        "analytics_days_left_window_days",
    }
    for key in allowed:
        if key in body:
            data["settings"][key] = body[key]

    # Backward compatibility for older clients posting manage_button_position.
    if "manage_button_position" in body and "dashboard_manage_button_position" not in body:
        data["settings"]["dashboard_manage_button_position"] = body.get("manage_button_position")

    if data["settings"].get("dashboard_manage_button_position") not in (
        "top-right",
        "bottom-left",
        "bottom-right",
    ):
        data["settings"]["dashboard_manage_button_position"] = "top-right"

    data["settings"].pop("manage_button_position", None)

    data["settings"]["bar_stock_enabled"] = _coerce_bool(
        data["settings"].get("bar_stock_enabled"),
        True,
    )
    data["settings"]["analytics_enabled"] = _coerce_bool(
        data["settings"].get("analytics_enabled"),
        True,
    )
    data["settings"]["api_reference_enabled"] = _coerce_bool(
        data["settings"].get("api_reference_enabled"),
        True,
    )
    data["settings"]["pour_mode"] = _normalize_pour_mode(
        data["settings"].get("pour_mode")
    )
    data["settings"]["environment_mode"] = _normalize_environment_mode(
        data["settings"].get("environment_mode")
    )
    if "setup_completed" in body:
        data["settings"]["setup_completed"] = _coerce_bool(
            data["settings"].get("setup_completed"),
            False,
        )
    elif str(data["settings"].get("bar_name", "")).strip() not in ("", "My Bar"):
        data["settings"]["setup_completed"] = True
    data["settings"]["keg_type_choices"] = _normalize_keg_type_choices(
        data["settings"].get("keg_type_choices", []),
        data["settings"].get("default_keg_type", ""),
    )
    data["settings"]["default_keg_type"] = _normalize_default_keg_type(
        data["settings"].get("default_keg_type", ""),
        data["settings"].get("keg_type_choices", []),
    )
    data["settings"]["menu_qr_mode"] = _normalize_menu_qr_mode(
        data["settings"].get("menu_qr_mode")
    )
    data["settings"]["analytics_low_keg_threshold_percent"] = _normalize_low_keg_threshold(
        data["settings"].get("analytics_low_keg_threshold_percent")
    )
    data["settings"]["analytics_days_left_method"] = _normalize_days_left_method(
        data["settings"].get("analytics_days_left_method")
    )
    data["settings"]["analytics_days_left_window_days"] = _normalize_days_left_window_days(
        data["settings"].get("analytics_days_left_window_days")
    )
    data["settings"]["pour_options"] = _normalize_pour_options(
        data["settings"].get("pour_options"),
        data["settings"].get("measurement", "us"),
    )
    data["settings"]["default_pour_preset"] = _normalize_default_pour_preset(
        data["settings"].get("default_pour_preset", ""),
        data["settings"].get("pour_options", []),
    )
    data["settings"]["bar_logo_url"] = _normalize_logo_url(
        data["settings"].get("bar_logo_url", "")
    )
    data["settings"]["external_base_url"] = _normalize_external_base_url(
        data["settings"].get("external_base_url", "")
    )
    data["settings"]["external_api_token_auth_enabled"] = _coerce_bool(
        data["settings"].get("external_api_token_auth_enabled"),
        True,
    )
    data["settings"]["external_api_token"] = _normalize_external_api_token(
        data["settings"].get("external_api_token", "")
    )
    data["settings"]["external_api_read_token"] = _normalize_external_api_token(
        data["settings"].get("external_api_read_token", "")
    )
    data["settings"]["external_api_write_token"] = _normalize_external_api_token(
        data["settings"].get("external_api_write_token", "")
    )
    data["settings"]["external_api_allowlist_enabled"] = _coerce_bool(
        data["settings"].get("external_api_allowlist_enabled"),
        False,
    )
    data["settings"]["external_api_allowlist"] = _normalize_ip_allowlist_text(
        data["settings"].get("external_api_allowlist", "")
    )
    data["settings"]["external_api_rate_limit_enabled"] = _coerce_bool(
        data["settings"].get("external_api_rate_limit_enabled"),
        True,
    )
    data["settings"]["external_api_rate_limit_per_minute"] = _normalize_external_api_rate_limit_per_minute(
        data["settings"].get("external_api_rate_limit_per_minute")
    )

    save_data(data)
    return jsonify(data["settings"])


@app.route("/api/settings/external-auth/test", methods=["POST"])
def api_test_external_auth_settings():
    data = load_data()
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}

    token_auth_enabled = _coerce_bool(settings.get("external_api_token_auth_enabled"), True)
    legacy_token_configured = bool(_normalize_external_api_token(settings.get("external_api_token", "")))
    read_token_configured = bool(_normalize_external_api_token(settings.get("external_api_read_token", "")))
    write_token_configured = bool(_normalize_external_api_token(settings.get("external_api_write_token", "")))

    allowlist_enabled = _coerce_bool(settings.get("external_api_allowlist_enabled"), False)
    allowlist_entries = _parse_ip_allowlist(_normalize_ip_allowlist_text(settings.get("external_api_allowlist", "")))

    rate_limit_enabled = _coerce_bool(settings.get("external_api_rate_limit_enabled"), True)
    rate_limit_per_minute = _normalize_external_api_rate_limit_per_minute(
        settings.get("external_api_rate_limit_per_minute")
    )

    warnings = []
    if token_auth_enabled and not any(
        (legacy_token_configured, read_token_configured, write_token_configured)
    ):
        warnings.append("Token authentication is enabled but no token is configured.")
    if allowlist_enabled and not allowlist_entries:
        warnings.append("Allowlist is enabled but no valid IP/CIDR entries were parsed.")
    if read_token_configured and not write_token_configured and not legacy_token_configured:
        warnings.append("Read token is configured but write token is empty, so write endpoints will be denied.")

    return jsonify(
        {
            "ok": len(warnings) == 0,
            "external_api_base_url": _external_api_listener_base_url(),
            "checks": {
                "token_auth_enabled": token_auth_enabled,
                "legacy_token_configured": legacy_token_configured,
                "read_token_configured": read_token_configured,
                "write_token_configured": write_token_configured,
                "allowlist_enabled": allowlist_enabled,
                "allowlist_entry_count": len(allowlist_entries),
                "rate_limit_enabled": rate_limit_enabled,
                "rate_limit_per_minute": rate_limit_per_minute,
            },
            "warnings": warnings,
            "auth_headers": {
                "Authorization": "Bearer <token>",
                "X-API-Token": "<token>",
            },
        }
    )


@app.route("/api/settings/keg-types/reset", methods=["POST"])
def api_reset_keg_types_to_defaults():
    data = load_data()
    data["settings"]["keg_type_choices"] = STANDARD_KEG_TYPE_CHOICES.copy()
    data["settings"]["default_keg_type"] = _normalize_default_keg_type(
        "",
        data["settings"].get("keg_type_choices", []),
    )
    save_data(data)
    return jsonify(
        {
            "ok": True,
            "keg_type_choices": data["settings"].get("keg_type_choices", []),
            "default_keg_type": data["settings"].get("default_keg_type", ""),
        }
    )


# ---------------------------------------------------------------------------
# API – Bar Stock
# ---------------------------------------------------------------------------

@app.route("/api/stock", methods=["GET"])
def api_list_stock():
    data = load_data()
    if not _bar_stock_enabled(data):
        return jsonify({"error": "Bar stock feature is disabled"}), 403
    return jsonify(data["bar_stock"])


@app.route("/api/stock", methods=["POST"])
def api_add_stock():
    data = load_data()
    if not _bar_stock_enabled(data):
        return jsonify({"error": "Bar stock feature is disabled"}), 403
    body = request.get_json(force=True)
    size_label = body.get("size_label", body.get("unit", ""))
    size_value = body.get("size_value", None)
    size_unit = body.get("size_unit", "")
    item = {
        "id": _next_id(data["bar_stock"]),
        "name": body.get("name", ""),
        "category": body.get("category", ""),
        "quantity": body.get("quantity", 0),
        "unit": body.get("unit", size_label),
        "size_label": size_label,
        "size_value": size_value,
        "size_unit": size_unit,
        "notes": body.get("notes", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    data["bar_stock"].append(item)
    save_data(data)
    return jsonify(item), 201


@app.route("/api/stock/<int:item_id>", methods=["PUT"])
def api_update_stock(item_id: int):
    data = load_data()
    if not _bar_stock_enabled(data):
        return jsonify({"error": "Bar stock feature is disabled"}), 403
    for item in data["bar_stock"]:
        if item["id"] == item_id:
            body = request.get_json(force=True)
            for field in ("name", "category", "quantity", "unit", "notes"):
                if field in body:
                    item[field] = body[field]
            for field in ("size_label", "size_value", "size_unit"):
                if field in body:
                    item[field] = body[field]
            # Keep unit aligned to selected size for older clients/views.
            if "size_label" in body and "unit" not in body:
                item["unit"] = body.get("size_label") or ""
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_data(data)
            return jsonify(item)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/stock/<int:item_id>", methods=["DELETE"])
def api_delete_stock(item_id: int):
    data = load_data()
    if not _bar_stock_enabled(data):
        return jsonify({"error": "Bar stock feature is disabled"}), 403
    data["bar_stock"] = [i for i in data["bar_stock"] if i["id"] != item_id]
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API – Beers
# ---------------------------------------------------------------------------

@app.route("/api/beers", methods=["GET"])
def api_list_beers():
    data = load_data()
    beers = sorted(
        data.get("beers", []),
        key=lambda beer: str(beer.get("name", "")).lower(),
    )
    return jsonify(beers)


@app.route("/api/beers", methods=["POST"])
def api_add_beer():
    data = load_data()
    body = request.get_json(force=True)
    name = str(body.get("name", "")).strip()
    if not name:
        return jsonify({"error": "Beer name is required."}), 400

    beer = {
        "id": _next_id(data.get("beers", [])),
        "name": name,
        "type": str(body.get("type", "")).strip(),
        "packaging": _normalize_beer_packaging(body.get("packaging", "kegged")),
        "brewer": str(body.get("brewer", "")).strip(),
        "brewery": str(body.get("brewery", "")).strip(),
        "abv": str(body.get("abv", "")).strip(),
        "ibu": str(body.get("ibu", "")).strip(),
        "brewed_on": str(body.get("brewed_on", "")).strip(),
        "notes": str(body.get("notes", "")).strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    data.setdefault("beers", []).append(beer)
    save_data(data)
    return jsonify(beer), 201


@app.route("/api/beers/<int:beer_id>", methods=["PUT"])
def api_update_beer(beer_id: int):
    data = load_data()
    body = request.get_json(force=True)

    for beer in data.get("beers", []):
        if beer.get("id") != beer_id:
            continue

        for field in ("name", "type", "brewer", "brewery", "abv", "ibu", "brewed_on", "notes"):
            if field in body:
                beer[field] = str(body.get(field, "")).strip()
        if "packaging" in body:
            beer["packaging"] = _normalize_beer_packaging(body.get("packaging"))

        if not str(beer.get("name", "")).strip():
            return jsonify({"error": "Beer name is required."}), 400

        beer["updated_at"] = datetime.now(timezone.utc).isoformat()

        for keg in data.get("kegs", []):
            if keg.get("beer_id") == beer_id:
                _apply_beer_to_keg(keg, beer)
                keg["updated_at"] = datetime.now(timezone.utc).isoformat()

        save_data(data)
        return jsonify(beer)

    return jsonify({"error": "Not found"}), 404


@app.route("/api/beers/<int:beer_id>", methods=["DELETE"])
def api_delete_beer(beer_id: int):
    data = load_data()

    linked_kegs = [
        keg for keg in data.get("kegs", []) if keg.get("beer_id") == beer_id
    ]
    if linked_kegs:
        return jsonify({
            "error": "This beer is currently assigned to one or more kegs.",
            "code": "BEER_ASSIGNED_TO_KEG",
            "keg_count": len(linked_kegs),
            "keg_names": [keg.get("name", "") for keg in linked_kegs],
        }), 409

    data["beers"] = [beer for beer in data.get("beers", []) if beer.get("id") != beer_id]
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API – Kegs
# ---------------------------------------------------------------------------

KEG_SIZES_US = ["1/6 bbl (5.2 gal)", "1/4 bbl (7.75 gal)", "1/2 bbl (15.5 gal)", "Corny (5 gal)", "Custom"]
KEG_SIZES_METRIC = ["20 L", "30 L", "50 L", "Custom"]
KEG_STATUSES = ["full", "in_use", "empty", "cleaning", "retired"]
API_REFERENCE_ENDPOINTS = [
    ("GET", "/api/settings", "Get current settings"),
    ("POST", "/api/settings", "Update settings"),
    ("GET", "/api/stock", "List all bar stock items"),
    ("POST", "/api/stock", "Add a stock item"),
    ("PUT", "/api/stock/<id>", "Update a stock item"),
    ("DELETE", "/api/stock/<id>", "Delete a stock item"),
    ("GET", "/api/beers", "List all beers"),
    ("POST", "/api/beers", "Add a beer"),
    ("PUT", "/api/beers/<id>", "Update a beer"),
    ("DELETE", "/api/beers/<id>", "Delete a beer"),
    ("GET", "/api/kegs", "List all kegs"),
    ("POST", "/api/kegs", "Add a keg"),
    ("POST", "/api/kegs/bulk", "Bulk add kegs"),
    ("PUT", "/api/kegs/<id>", "Update a keg"),
    ("POST", "/api/kegs/<id>/fill", "Fill/refill a keg"),
    ("POST", "/api/kegs/<id>/clean", "Mark a cleaning keg clean and reset to ready defaults"),
    ("POST", "/api/kegs/<id>/pour", "Record a pour and reduce volume"),
    ("DELETE", "/api/kegs/<id>", "Delete a keg"),
    ("GET", "/api/taps", "List all taps"),
    ("POST", "/api/taps", "Add a tap"),
    ("POST", "/api/taps/bulk", "Bulk add taps"),
    ("PUT", "/api/taps/<id>", "Update a tap"),
    ("POST", "/api/taps/<id>/pour", "Record a pour for the assigned keg"),
    ("DELETE", "/api/taps/<id>", "Delete a tap"),
    ("GET", "/api/export/json", "Export portable versioned JSON backup"),
    ("GET", "/api/export/archive", "Export ZIP archive backup"),
    ("GET", "/api/export/csv", "Legacy alias for ZIP archive export"),
    ("POST", "/api/import/archive/preview", "Preview archive import results"),
    ("POST", "/api/import/archive", "Import ZIP archive backup"),
    ("POST", "/api/import/json/preview", "Preview JSON import results"),
    ("POST", "/api/import/json", "Import JSON backup"),
    ("GET", "/api/menu/qr", "Generate printable menu QR code PNG"),
    ("GET", "/api/menu/qr/health", "Check runtime QR dependency readiness"),
]


def _today_utc_date() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@app.route("/api/kegs", methods=["GET"])
def api_list_kegs():
    data = load_data()
    return jsonify(data["kegs"])


@app.route("/api/kegs", methods=["POST"])
def api_add_keg():
    data = load_data()
    body = request.get_json(force=True)
    initial_status = _normalize_keg_status(body.get("status", "empty"))
    incoming_filled_date = body.get("filled_date", body.get("purchased_date", ""))
    has_percent_full = "percent_full" in body
    beer_id = _coerce_int(body.get("beer_id"), None)
    if body.get("beer_id") not in (None, "") and beer_id is None:
        return jsonify({"error": "Invalid beer selection."}), 400

    selected_beer = _get_beer_by_id(data, beer_id)
    if beer_id is not None and not selected_beer:
        return jsonify({"error": "Selected beer was not found."}), 404
    if selected_beer and not _is_beer_kegged(selected_beer):
        return jsonify({"error": "Only kegged beers can be assigned to kegs."}), 409

    keg_type = str(body.get("type", "")).strip() or str(
        data.get("settings", {}).get("default_keg_type", "")
    ).strip()
    timestamp = datetime.now(timezone.utc).isoformat()
    keg = {
        "id": _next_id(data["kegs"]),
        "name": body.get("name", ""),
        "beer_id": beer_id,
        "beer_name": str(body.get("beer_name", "")).strip(),
        "beer_type": str(body.get("beer_type", "")).strip(),
        "type": keg_type,
        "size": body.get("size", ""),
        "custom_size": body.get("custom_size", ""),
        "status": initial_status,
        "beer_brewer": body.get("beer_brewer", body.get("brewery", "")),
        "beer_abv": body.get("beer_abv", body.get("abv", "")),
        "beer_ibu": body.get("beer_ibu", ""),
        "beer_brewed_on": body.get("beer_brewed_on", ""),
        "line_cleaning_keg": _coerce_bool(body.get("line_cleaning_keg"), False),
        "on_deck": _coerce_bool(body.get("on_deck"), False),
        "current_volume": _coerce_float(body.get("current_volume"), None),
        "volume_unit": _normalize_volume_unit(
            body.get("volume_unit")
            or _default_volume_unit(data.get("settings", {}).get("measurement", "us"))
        ),
        # Keep legacy keys in sync for older clients.
        "brewery": body.get("brewery", body.get("beer_brewer", "")),
        "abv": body.get("abv", body.get("beer_abv", "")),
        "notes": body.get("notes", ""),
        "tapped_date": body.get("tapped_date", ""),
        "filled_date": incoming_filled_date,
        "percent_full": _clamp_percent_full(body.get("percent_full"), _default_percent_for_status(initial_status)),
        "created_at": timestamp,
        "updated_at": timestamp,
    }

    if selected_beer:
        _apply_beer_to_keg(keg, selected_beer)

    if keg.get("line_cleaning_keg") and _line_cleaning_keg_conflict(data):
        return jsonify({
            "error": "Only one keg can be marked as the line cleaning keg.",
            "code": "LINE_CLEANING_KEG_EXISTS",
        }), 409

    validation_error = _validate_full_keg_requirements(keg)
    if validation_error:
        return jsonify(validation_error), 409

    _set_filled_date_for_status_transition(keg, initial_status)
    _sync_percent_for_status(keg, initial_status, has_percent_full)
    if "current_volume" in body:
        _sync_percent_from_current_volume(keg)
    if _coerce_bool(keg.get("on_deck"), False) and not _can_mark_on_deck(keg):
        return jsonify({
            "error": "Keg must be filled before it can be marked On Deck.",
            "code": "ON_DECK_REQUIRES_FILLED_KEG",
        }), 409
    data["kegs"].append(keg)
    save_data(data)
    return jsonify(keg), 201


@app.route("/api/kegs/bulk", methods=["POST"])
def api_add_kegs_bulk():
    data = load_data()
    body = request.get_json(force=True)
    items = body if isinstance(body, list) else body.get("items", [])
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Body must include a non-empty items array."}), 400

    simulated_data = json.loads(json.dumps(data))
    created = []

    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            return jsonify({
                "error": "Each bulk item must be an object.",
                "index": index,
            }), 400

        item = dict(raw_item)
        initial_status = _normalize_keg_status(item.get("status", "empty"))
        incoming_filled_date = item.get("filled_date", item.get("purchased_date", ""))
        has_percent_full = "percent_full" in item
        beer_id = _coerce_int(item.get("beer_id"), None)
        if item.get("beer_id") not in (None, "") and beer_id is None:
            return jsonify({"error": "Invalid beer selection.", "index": index}), 400

        selected_beer = _get_beer_by_id(simulated_data, beer_id)
        if beer_id is not None and not selected_beer:
            return jsonify({"error": "Selected beer was not found.", "index": index}), 404
        if selected_beer and not _is_beer_kegged(selected_beer):
            return jsonify({"error": "Only kegged beers can be assigned to kegs.", "index": index}), 409

        keg_type = str(item.get("type", "")).strip() or str(
            simulated_data.get("settings", {}).get("default_keg_type", "")
        ).strip()
        timestamp = datetime.now(timezone.utc).isoformat()
        keg = {
            "id": _next_id(simulated_data["kegs"]),
            "name": item.get("name", ""),
            "beer_id": beer_id,
            "beer_name": str(item.get("beer_name", "")).strip(),
            "beer_type": str(item.get("beer_type", "")).strip(),
            "type": keg_type,
            "size": item.get("size", ""),
            "custom_size": item.get("custom_size", ""),
            "status": initial_status,
            "beer_brewer": item.get("beer_brewer", item.get("brewery", "")),
            "beer_abv": item.get("beer_abv", item.get("abv", "")),
            "beer_ibu": item.get("beer_ibu", ""),
            "beer_brewed_on": item.get("beer_brewed_on", ""),
            "line_cleaning_keg": _coerce_bool(item.get("line_cleaning_keg"), False),
            "on_deck": _coerce_bool(item.get("on_deck"), False),
            "current_volume": _coerce_float(item.get("current_volume"), None),
            "volume_unit": _normalize_volume_unit(
                item.get("volume_unit")
                or _default_volume_unit(simulated_data.get("settings", {}).get("measurement", "us"))
            ),
            "brewery": item.get("brewery", item.get("beer_brewer", "")),
            "abv": item.get("abv", item.get("beer_abv", "")),
            "notes": item.get("notes", ""),
            "tapped_date": item.get("tapped_date", ""),
            "filled_date": incoming_filled_date,
            "percent_full": _clamp_percent_full(item.get("percent_full"), _default_percent_for_status(initial_status)),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

        if selected_beer:
            _apply_beer_to_keg(keg, selected_beer)

        if keg.get("line_cleaning_keg") and any(
            _coerce_bool(existing.get("line_cleaning_keg"), False)
            for existing in simulated_data.get("kegs", [])
        ):
            return jsonify({
                "error": "Only one keg can be marked as the line cleaning keg.",
                "code": "LINE_CLEANING_KEG_EXISTS",
                "index": index,
            }), 409

        validation_error = _validate_full_keg_requirements(keg)
        if validation_error:
            validation_error["index"] = index
            return jsonify(validation_error), 409

        _set_filled_date_for_status_transition(keg, initial_status)
        _sync_percent_for_status(keg, initial_status, has_percent_full)
        if "current_volume" in item:
            _sync_percent_from_current_volume(keg)
        if _coerce_bool(keg.get("on_deck"), False) and not _can_mark_on_deck(keg):
            return jsonify({
                "error": "Keg must be filled before it can be marked On Deck.",
                "code": "ON_DECK_REQUIRES_FILLED_KEG",
                "index": index,
            }), 409
        simulated_data["kegs"].append(keg)
        created.append(keg)

    save_data(simulated_data)
    return jsonify({"ok": True, "created": created, "count": len(created)}), 201


@app.route("/api/kegs/<int:keg_id>", methods=["PUT"])
def api_update_keg(keg_id: int):
    data = load_data()
    for keg in data["kegs"]:
        if keg["id"] == keg_id:
            previous_keg = dict(keg)
            body = request.get_json(force=True)
            if "purchased_date" in body and "filled_date" not in body:
                body["filled_date"] = body["purchased_date"]

            if "status" in body:
                body["status"] = _normalize_keg_status(body["status"])
                if not _is_cleaning_transition_allowed(
                    keg.get("status", "empty"),
                    body["status"],
                ):
                    return jsonify({
                        "error": "Kegs marked as needs cleaning can only be set to Clean.",
                        "code": "CLEANING_KEG_MUST_BE_CLEANED_FIRST",
                    }), 409

            # Backward compatibility: map between legacy and new beer fields.
            if "brewery" in body and "beer_brewer" not in body:
                body["beer_brewer"] = body["brewery"]
            if "abv" in body and "beer_abv" not in body:
                body["beer_abv"] = body["abv"]
            if "beer_brewer" in body and "brewery" not in body:
                body["brewery"] = body["beer_brewer"]
            if "beer_abv" in body and "abv" not in body:
                body["abv"] = body["beer_abv"]
            if "line_cleaning_keg" in body:
                body["line_cleaning_keg"] = _coerce_bool(
                    body.get("line_cleaning_keg"),
                    False,
                )
                if body["line_cleaning_keg"] and _line_cleaning_keg_conflict(
                    data,
                    candidate_id=keg_id,
                ):
                    return jsonify({
                        "error": "Only one keg can be marked as the line cleaning keg.",
                        "code": "LINE_CLEANING_KEG_EXISTS",
                    }), 409

            selected_beer = None
            if "beer_id" in body:
                parsed_beer_id = _coerce_int(body.get("beer_id"), None)
                if body.get("beer_id") not in (None, "") and parsed_beer_id is None:
                    return jsonify({"error": "Invalid beer selection."}), 400
                body["beer_id"] = parsed_beer_id
                if parsed_beer_id is not None:
                    selected_beer = _get_beer_by_id(data, parsed_beer_id)
                    if not selected_beer:
                        return jsonify({"error": "Selected beer was not found."}), 404
                    if not _is_beer_kegged(selected_beer):
                        return jsonify({"error": "Only kegged beers can be assigned to kegs."}), 409

            has_percent_full = "percent_full" in body
            if has_percent_full:
                incoming_percent = body.get("percent_full")
                if incoming_percent in (None, ""):
                    # Treat empty/omitted values as "do not change".
                    has_percent_full = False
                    body.pop("percent_full", None)
                else:
                    existing_percent = _clamp_percent_full(
                        keg.get("percent_full"),
                        _default_percent_for_status(keg.get("status", "empty")),
                    )
                    body["percent_full"] = _clamp_percent_full(
                        incoming_percent,
                        existing_percent,
                    )

            for field in (
                "name",
                "beer_id",
                "beer_name",
                "type",
                "size",
                "custom_size",
                "status",
                "beer_brewer",
                "beer_abv",
                "beer_ibu",
                "beer_brewed_on",
                "line_cleaning_keg",
                "on_deck",
                "current_volume",
                "volume_unit",
                "brewery",
                "abv",
                "notes",
                "tapped_date",
                "filled_date",
                "percent_full",
            ):
                if field in body:
                    keg[field] = body[field]

            if "beer_id" in body:
                if selected_beer:
                    _apply_beer_to_keg(keg, selected_beer)
                elif body.get("beer_id") is None:
                    keg["beer_name"] = ""
                    for field in (
                        "type",
                        "beer_type",
                        "beer_brewer",
                        "beer_brewery",
                        "beer_abv",
                        "beer_ibu",
                        "beer_brewed_on",
                        "beer_packaging",
                        "brewery",
                        "abv",
                    ):
                        keg[field] = ""

            if "current_volume" in body:
                keg["current_volume"] = _coerce_float(keg.get("current_volume"), None)
            if "volume_unit" in body:
                keg["volume_unit"] = _normalize_volume_unit(keg.get("volume_unit"))

            if "current_volume" in body:
                previous_volume = _coerce_float(previous_keg.get("current_volume"), None)
                updated_volume = _coerce_float(keg.get("current_volume"), None)
                volume_changed = not (
                    previous_volume is None and updated_volume is None
                ) and not (
                    previous_volume is not None
                    and updated_volume is not None
                    and abs(previous_volume - updated_volume) < 1e-9
                )

                if volume_changed:
                    if updated_volume is None:
                        _sync_percent_for_status(
                            keg,
                            keg.get("status", "empty"),
                            False,
                        )
                    elif not _sync_percent_from_current_volume(keg):
                        _sync_percent_for_volume_change(
                            previous_keg,
                            keg,
                            current_volume_explicit=True,
                            percent_explicit=has_percent_full,
                        )

            if "status" in body:
                _set_filled_date_for_status_transition(keg, body["status"])
                # Preserve existing value when only status changes.
                if has_percent_full:
                    _sync_percent_for_status(keg, body["status"], True)

            if "status" not in body and has_percent_full:
                keg["percent_full"] = _clamp_percent_full(keg.get("percent_full"), _default_percent_for_status(keg.get("status", "empty")))

            _apply_needs_cleaning_transition(
                previous_keg,
                keg,
                status_explicit="status" in body,
                percent_explicit=has_percent_full,
            )

            if (
                _normalize_keg_status(previous_keg.get("status", "empty")) == "cleaning"
                and _normalize_keg_status(keg.get("status", "empty")) == "empty"
            ):
                _reset_keg_to_clean_ready(keg)

            if _coerce_bool(keg.get("on_deck"), False) and not _can_mark_on_deck(keg):
                return jsonify({
                    "error": "Keg must be filled before it can be marked On Deck.",
                    "code": "ON_DECK_REQUIRES_FILLED_KEG",
                }), 409

            validation_error = _validate_full_keg_requirements(keg)
            if validation_error:
                return jsonify(validation_error), 409

            keg.pop("purchased_date", None)
            keg["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/fill", methods=["POST"])
def api_fill_keg(keg_id: int):
    data = load_data()
    body = request.get_json(silent=True) or {}
    for keg in data["kegs"]:
        if keg["id"] == keg_id:
            if keg.get("status") != "empty" and not body.get("force", False):
                return jsonify({"error": "Keg is not empty", "status": keg.get("status")}), 409

            if "beer_id" in body:
                parsed_beer_id = _coerce_int(body.get("beer_id"), None)
                if body.get("beer_id") not in (None, "") and parsed_beer_id is None:
                    return jsonify({"error": "Invalid beer selection."}), 400
                if parsed_beer_id is None:
                    keg["beer_id"] = None
                    keg["beer_name"] = ""
                    for field in (
                        "type",
                        "beer_type",
                        "beer_brewer",
                        "beer_brewery",
                        "beer_abv",
                        "beer_ibu",
                        "beer_brewed_on",
                        "beer_packaging",
                        "brewery",
                        "abv",
                    ):
                        keg[field] = ""
                else:
                    selected_beer = _get_beer_by_id(data, parsed_beer_id)
                    if not selected_beer:
                        return jsonify({"error": "Selected beer was not found."}), 404
                    if not _is_beer_kegged(selected_beer):
                        return jsonify({"error": "Only kegged beers can be assigned to kegs."}), 409
                    _apply_beer_to_keg(keg, selected_beer)

            target_status = _normalize_keg_status(body.get("status", "full"))
            if target_status not in KEG_STATUSES:
                return jsonify({"error": "Invalid status"}), 400

            keg["status"] = target_status
            validation_error = _validate_full_keg_requirements(keg)
            if validation_error:
                return jsonify(validation_error), 409
            keg["filled_date"] = body.get("filled_date") or _today_utc_date()
            keg["percent_full"] = _clamp_percent_full(body.get("percent_full"), 100)
            keg["on_deck"] = False
            keg["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/clean", methods=["POST"])
def api_clean_keg(keg_id: int):
    data = load_data()
    for keg in data["kegs"]:
        if keg["id"] != keg_id:
            continue

        if _normalize_keg_status(keg.get("status", "empty")) != "cleaning":
            return jsonify({
                "error": "Only kegs that need cleaning can be marked clean.",
                "code": "CLEAN_ACTION_REQUIRES_CLEANING_STATUS",
            }), 409

        _reset_keg_to_clean_ready(keg)
        keg["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_data(data)
        return jsonify(keg)

    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/pour", methods=["POST"])
def api_pour_keg(keg_id: int):
    data = load_data()
    body = request.get_json(force=True)

    amount = _coerce_float(body.get("amount"), None)
    if amount is None or amount <= 0:
        return jsonify({"error": "Pour amount must be greater than zero."}), 400

    for keg in data["kegs"]:
        if keg["id"] != keg_id:
            continue
        payload, status = _apply_pour_to_keg(data, keg, amount, body.get("unit"))
        if status != 200:
            return jsonify(payload), status
        _record_pour_event(data, keg, amount, body.get("unit"), "keg", preset_name=str(body.get("preset_name", "")))
        save_data(data)
        return jsonify(payload)

    return jsonify({"error": "Not found"}), 404


@app.route("/api/taps/<int:tap_id>/pour", methods=["POST"])
def api_pour_tap(tap_id: int):
    data = load_data()
    body = request.get_json(force=True)

    amount = _coerce_float(body.get("amount"), None)
    if amount is None or amount <= 0:
        return jsonify({"error": "Pour amount must be greater than zero."}), 400

    target_tap = None
    for tap in data.get("taps", []):
        if tap.get("id") == tap_id:
            target_tap = tap
            break

    if target_tap is None:
        return jsonify({"error": "Tap not found."}), 404

    keg_id = target_tap.get("keg_id")
    if keg_id is None:
        return jsonify({"error": "No keg is assigned to this tap."}), 409

    for keg in data.get("kegs", []):
        if keg.get("id") != keg_id:
            continue

        payload, status = _apply_pour_to_keg(data, keg, amount, body.get("unit"))
        if status != 200:
            return jsonify(payload), status
        _record_pour_event(data, keg, amount, body.get("unit"), "tap", tap_id=tap_id, preset_name=str(body.get("preset_name", "")))
        save_data(data)
        return jsonify(payload)

    return jsonify({"error": "Assigned keg not found."}), 404


@app.route("/api/kegs/<int:keg_id>", methods=["DELETE"])
def api_delete_keg(keg_id: int):
    data = load_data()
    assigned_taps = [tap for tap in data["taps"] if tap.get("keg_id") == keg_id]
    if assigned_taps:
        tap_numbers = [tap.get("number") for tap in assigned_taps if tap.get("number") is not None]
        return jsonify({
            "error": "This keg is assigned to one or more taps. Disconnect it from all taps (or delete those taps) before deleting the keg.",
            "code": "KEG_ASSIGNED_TO_TAP",
            "tap_count": len(assigned_taps),
            "tap_numbers": tap_numbers,
        }), 409

    data["kegs"] = [k for k in data["kegs"] if k["id"] != keg_id]
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API – Taps
# ---------------------------------------------------------------------------

@app.route("/api/taps", methods=["GET"])
def api_list_taps():
    data = load_data()
    return jsonify(data["taps"])


@app.route("/api/taps", methods=["POST"])
def api_add_tap():
    data = load_data()
    body = request.get_json(force=True)
    raw_keg_id = body.get("keg_id")
    parsed_keg_id = _coerce_int(raw_keg_id, None)
    if raw_keg_id not in (None, "") and parsed_keg_id is None:
        return jsonify({"error": "Invalid keg_id."}), 400
    tap = {
        "id": _next_id(data["taps"]),
        "number": body.get("number", len(data["taps"]) + 1),
        "label": body.get("label", ""),
        "keg_id": parsed_keg_id,
        "ever_assigned_keg": parsed_keg_id is not None,
        "notes": body.get("notes", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_keg_tapped_date_if_missing(data, tap.get("keg_id"))
    data["taps"].append(tap)
    save_data(data)
    return jsonify(tap), 201


@app.route("/api/taps/bulk", methods=["POST"])
def api_add_taps_bulk():
    data = load_data()
    body = request.get_json(force=True)
    items = body if isinstance(body, list) else body.get("items", [])
    if not isinstance(items, list) or not items:
        return jsonify({"error": "Body must include a non-empty items array."}), 400

    simulated_data = json.loads(json.dumps(data))
    created = []

    for index, raw_item in enumerate(items):
        if not isinstance(raw_item, dict):
            return jsonify({
                "error": "Each bulk item must be an object.",
                "index": index,
            }), 400

        item = dict(raw_item)
        keg_id = item.get("keg_id")
        if keg_id in ("", None):
            keg_id = None
        else:
            keg_id = _coerce_int(keg_id, None)
            if keg_id is None:
                return jsonify({"error": "Invalid keg_id.", "index": index}), 400

        if keg_id is not None and not any(k.get("id") == keg_id for k in simulated_data.get("kegs", [])):
            return jsonify({"error": "Assigned keg not found.", "index": index}), 404

        number = _coerce_int(item.get("number"), None)
        if number is None or number <= 0:
            return jsonify({"error": "Tap number must be a positive integer.", "index": index}), 400

        tap = {
            "id": _next_id(simulated_data["taps"]),
            "number": number,
            "label": item.get("label", ""),
            "keg_id": keg_id,
            "ever_assigned_keg": keg_id is not None,
            "notes": item.get("notes", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _set_keg_tapped_date_if_missing(simulated_data, tap.get("keg_id"))
        simulated_data["taps"].append(tap)
        created.append(tap)

    save_data(simulated_data)
    return jsonify({"ok": True, "created": created, "count": len(created)}), 201


@app.route("/api/taps/<int:tap_id>", methods=["PUT"])
def api_update_tap(tap_id: int):
    data = load_data()
    for tap in data["taps"]:
        if tap["id"] == tap_id:
            body = request.get_json(force=True)
            for field in ("number", "label", "notes"):
                if field in body:
                    tap[field] = body[field]
            if "keg_id" in body:
                raw_keg_id = body.get("keg_id")
                parsed_keg_id = _coerce_int(raw_keg_id, None)
                if raw_keg_id not in (None, "") and parsed_keg_id is None:
                    return jsonify({"error": "Invalid keg_id."}), 400
                tap["keg_id"] = parsed_keg_id
                if parsed_keg_id is not None:
                    tap["ever_assigned_keg"] = True
            _set_keg_tapped_date_if_missing(data, tap.get("keg_id"))
            tap["updated_at"] = datetime.now(timezone.utc).isoformat()
            save_data(data)
            return jsonify(tap)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/taps/<int:tap_id>", methods=["DELETE"])
def api_delete_tap(tap_id: int):
    data = load_data()
    data["taps"] = [t for t in data["taps"] if t["id"] != tap_id]
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _keg_csv_rows(kegs: list[dict]) -> list[list]:
    header = [
        "id",
        "created_at",
        "name",
        "beer_id",
        "beer_name",
        "type",
        "size",
        "custom_size",
        "status",
        "beer_brewer",
        "beer_abv",
        "beer_ibu",
        "beer_brewed_on",
        "line_cleaning_keg",
        "current_volume",
        "volume_unit",
        "notes",
        "tapped_date",
        "filled_date",
        "percent_full",
        "updated_at",
    ]
    rows = [header]
    for keg in kegs:
        rows.append([keg.get(field, "") for field in header])
    return rows


def _tap_csv_rows(taps: list[dict]) -> list[list]:
    header = ["id", "number", "label", "keg_id", "notes", "updated_at"]
    rows = [header]
    for tap in taps:
        rows.append([tap.get(field, "") for field in header])
    return rows


def _stock_csv_rows(stock: list[dict]) -> list[list]:
    header = ["id", "name", "category", "quantity", "unit", "notes", "updated_at"]
    rows = [header]
    for item in stock:
        rows.append([item.get(field, "") for field in header])
    return rows


def _beer_csv_rows(beers: list[dict]) -> list[list]:
    header = [
        "id",
        "name",
        "type",
        "packaging",
        "brewer",
        "brewery",
        "abv",
        "ibu",
        "brewed_on",
        "notes",
        "updated_at",
    ]
    rows = [header]
    for beer in beers:
        rows.append([beer.get(field, "") for field in header])
    return rows


def _rows_to_csv_bytes(rows: list[list]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _build_export_json_payload(data: dict) -> dict:
    return {
        "format": "bartender-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


def _build_export_archive(data: dict) -> bytes:
    """Build a ZIP archive with full BarTender data as separate files."""
    payload = _build_export_json_payload(data)
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Canonical JSON exports by section.
        zf.writestr("settings.json", json.dumps(data.get("settings", {}), indent=2))
        zf.writestr("kegs.json", json.dumps(data.get("kegs", []), indent=2))
        zf.writestr("taps.json", json.dumps(data.get("taps", []), indent=2))
        zf.writestr("beers.json", json.dumps(data.get("beers", []), indent=2))
        zf.writestr("bar_stock.json", json.dumps(data.get("bar_stock", []), indent=2))
        zf.writestr("pour_events.json", json.dumps(data.get("pour_events", []), indent=2))
        zf.writestr("bartender_export.json", json.dumps(payload, indent=2))

        # CSV exports for convenience.
        zf.writestr("kegs.csv", _rows_to_csv_bytes(_keg_csv_rows(data.get("kegs", []))))
        zf.writestr("taps.csv", _rows_to_csv_bytes(_tap_csv_rows(data.get("taps", []))))
        zf.writestr("beers.csv", _rows_to_csv_bytes(_beer_csv_rows(data.get("beers", []))))
        zf.writestr("bar_stock.csv", _rows_to_csv_bytes(_stock_csv_rows(data.get("bar_stock", []))))

    out.seek(0)
    return out.getvalue()


def _read_archive_json(zf: zipfile.ZipFile, name: str, default):
    if name not in zf.namelist():
        return default
    try:
        with zf.open(name, "r") as f:
            return json.loads(f.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return default


def _import_archive_payload(file_bytes: bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes), mode="r") as zf:
            names = set(zf.namelist())

            # Preferred payload: single full JSON file.
            if "bartender_export.json" in names:
                full_data = _read_archive_json(zf, "bartender_export.json", None)
                if isinstance(full_data, dict):
                    if (
                        full_data.get("format") == "bartender-export"
                        and isinstance(full_data.get("data"), dict)
                    ):
                        return full_data["data"]
                    return full_data

            # Section-based fallback payload.
            settings = _read_archive_json(zf, "settings.json", {})
            kegs = _read_archive_json(zf, "kegs.json", [])
            taps = _read_archive_json(zf, "taps.json", [])
            beers = _read_archive_json(zf, "beers.json", [])
            bar_stock = _read_archive_json(zf, "bar_stock.json", [])
            return {
                "settings": settings if isinstance(settings, dict) else {},
                "kegs": kegs if isinstance(kegs, list) else [],
                "taps": taps if isinstance(taps, list) else [],
                "beers": beers if isinstance(beers, list) else [],
                "bar_stock": bar_stock if isinstance(bar_stock, list) else [],
                "pour_events": _read_archive_json(zf, "pour_events.json", []),
            }
    except zipfile.BadZipFile:
        return None


def _sanitize_import_payload(raw_data: dict) -> dict:
    """Constrain imported payload to the expected top-level schema."""
    return {
        "settings": raw_data.get("settings", {}) if isinstance(raw_data.get("settings", {}), dict) else {},
        "kegs": [x for x in raw_data.get("kegs", []) if isinstance(x, dict)] if isinstance(raw_data.get("kegs", []), list) else [],
        "taps": [x for x in raw_data.get("taps", []) if isinstance(raw_data.get("taps", []), list) and isinstance(x, dict)] if isinstance(raw_data.get("taps", []), list) else [],
        "beers": [x for x in raw_data.get("beers", []) if isinstance(x, dict)] if isinstance(raw_data.get("beers", []), list) else [],
        "bar_stock": [x for x in raw_data.get("bar_stock", []) if isinstance(x, dict)] if isinstance(raw_data.get("bar_stock", []), list) else [],
        "pour_events": [x for x in raw_data.get("pour_events", []) if isinstance(x, dict)] if isinstance(raw_data.get("pour_events", []), list) else [],
    }


def _extract_export_data(raw_payload):
    if not isinstance(raw_payload, dict):
        return None
    if raw_payload.get("format") == "bartender-export":
        if raw_payload.get("version") != 1:
            return None
        data = raw_payload.get("data")
        return data if isinstance(data, dict) else None
    return raw_payload


def _coerce_import_mode(mode) -> str:
    normalized = str(mode or "replace").strip().lower()
    return normalized if normalized in ("replace", "merge") else "replace"


def _merge_collection(existing: list[dict], incoming: list[dict]) -> list[dict]:
    merged = [dict(item) for item in existing if isinstance(item, dict)]
    id_index = {
        item.get("id"): idx
        for idx, item in enumerate(merged)
        if isinstance(item.get("id"), int)
    }

    next_id = _next_id(merged)
    for item in incoming:
        if not isinstance(item, dict):
            continue

        candidate = dict(item)
        item_id = candidate.get("id")
        if not isinstance(item_id, int) or item_id <= 0:
            item_id = next_id
            next_id += 1
            candidate["id"] = item_id

        if item_id in id_index:
            merged[id_index[item_id]].update(candidate)
        else:
            id_index[item_id] = len(merged)
            merged.append(candidate)
            if item_id >= next_id:
                next_id = item_id + 1

    return merged


def _apply_import_payload(existing_data: dict, payload: dict, mode: str) -> dict:
    if mode == "replace":
        return _sanitize_import_payload(payload)

    merged = json.loads(json.dumps(existing_data))
    incoming = _sanitize_import_payload(payload)

    merged_settings = merged.get("settings", {})
    if not isinstance(merged_settings, dict):
        merged_settings = {}
    merged_settings.update(incoming.get("settings", {}))
    merged["settings"] = merged_settings

    merged["kegs"] = _merge_collection(
        merged.get("kegs", []),
        incoming.get("kegs", []),
    )
    merged["taps"] = _merge_collection(
        merged.get("taps", []),
        incoming.get("taps", []),
    )
    merged["beers"] = _merge_collection(
        merged.get("beers", []),
        incoming.get("beers", []),
    )
    merged["bar_stock"] = _merge_collection(
        merged.get("bar_stock", []),
        incoming.get("bar_stock", []),
    )
    return merged


def _import_summary(payload: dict) -> dict:
    settings = payload.get("settings", {}) if isinstance(payload.get("settings", {}), dict) else {}
    kegs = payload.get("kegs", []) if isinstance(payload.get("kegs", []), list) else []
    taps = payload.get("taps", []) if isinstance(payload.get("taps", []), list) else []
    beers = payload.get("beers", []) if isinstance(payload.get("beers", []), list) else []
    bar_stock = payload.get("bar_stock", []) if isinstance(payload.get("bar_stock", []), list) else []

    return {
        "bar_name": settings.get("bar_name") or "My Bar",
        "kegs": len(kegs),
        "taps": len(taps),
        "beers": len(beers),
        "bar_stock": len(bar_stock),
    }

@app.route("/api/export/json")
def export_json():
    data = load_data()
    payload = _build_export_json_payload(data)
    return send_file(
        io.BytesIO(json.dumps(payload, indent=2).encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name="bartender_export.json",
    )


@app.route("/api/export/archive")
def export_archive():
    data = load_data()
    archive = _build_export_archive(data)
    return send_file(
        io.BytesIO(archive),
        mimetype="application/zip",
        as_attachment=True,
        download_name="bartender_export.zip",
    )


@app.route("/api/export/csv")
def export_csv():
    data = load_data()
    archive = _build_export_archive(data)
    return send_file(
        io.BytesIO(archive),
        mimetype="application/zip",
        as_attachment=True,
        download_name="bartender_export.zip",
    )


@app.route("/api/import/archive", methods=["POST"])
def import_archive():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "No archive file provided."}), 400

    mode = _coerce_import_mode(request.form.get("mode"))

    file_bytes = upload.read()
    imported = _import_archive_payload(file_bytes)
    if imported is None:
        return jsonify({"error": "Invalid ZIP archive."}), 400

    existing = load_data()
    applied = _apply_import_payload(existing, imported, mode)
    save_data(applied)

    # Return normalized payload after load_data applies compatibility defaults.
    normalized = load_data()
    save_data(normalized)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/import/archive/preview", methods=["POST"])
def import_archive_preview():
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"error": "No archive file provided."}), 400

    mode = _coerce_import_mode(request.form.get("mode"))

    file_bytes = upload.read()
    imported = _import_archive_payload(file_bytes)
    if imported is None:
        return jsonify({"error": "Invalid ZIP archive."}), 400

    existing = load_data()
    preview_payload = _apply_import_payload(existing, imported, mode)
    return jsonify({"ok": True, "summary": _import_summary(preview_payload), "mode": mode})


@app.route("/api/import/json", methods=["POST"])
def import_json():
    mode = _coerce_import_mode(request.form.get("mode") or request.args.get("mode"))

    raw_payload = None
    upload = request.files.get("file")
    if upload is not None:
        try:
            raw_payload = json.loads(upload.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "Invalid JSON payload."}), 400
    else:
        raw_payload = request.get_json(silent=True)

    extracted = _extract_export_data(raw_payload)
    if extracted is None:
        return jsonify({"error": "Invalid or unsupported export payload."}), 400

    existing = load_data()
    applied = _apply_import_payload(existing, extracted, mode)
    save_data(applied)

    normalized = load_data()
    save_data(normalized)
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/import/json/preview", methods=["POST"])
def import_json_preview():
    mode = _coerce_import_mode(request.form.get("mode") or request.args.get("mode"))

    raw_payload = None
    upload = request.files.get("file")
    if upload is not None:
        try:
            raw_payload = json.loads(upload.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return jsonify({"error": "Invalid JSON payload."}), 400
    else:
        raw_payload = request.get_json(silent=True)

    extracted = _extract_export_data(raw_payload)
    if extracted is None:
        return jsonify({"error": "Invalid or unsupported export payload."}), 400

    existing = load_data()
    preview_payload = _apply_import_payload(existing, extracted, mode)
    return jsonify({"ok": True, "summary": _import_summary(preview_payload), "mode": mode})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    app.run(host="0.0.0.0", port=port, debug=False)
