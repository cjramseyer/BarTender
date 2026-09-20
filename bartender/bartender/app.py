"""BarTender - Home Assistant Add-on for bar, keg, and tap management."""

import json
import os
import io
import csv
import base64
import hashlib
import zipfile
import re
import mimetypes
import ipaddress
import secrets
import math
import time
import threading
import uuid
import binascii
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable
from urllib.parse import urlsplit, urlunsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
except ImportError:  # pragma: no cover - dependency is installed in production
    InvalidSignature = ValueError
    serialization = None
    Ed25519PrivateKey = None
    Ed25519PublicKey = None

from bartender.pos_sync.service import (
    POS_SYNC_PROVIDERS,
    PosSyncError,
    add_or_update_custom_provider,
    get_pos_provider_catalog,
    get_pos_sync_status,
    import_custom_providers,
    mark_pos_sync_failed,
    normalize_pos_sync_settings,
    perform_pos_sync,
    validate_pos_sync_runtime_configuration,
)
from bartender.pos_sync.brewfather import (
    BREWFATHER_MANAGED_FIELDS,
    BrewfatherClient,
    BrewfatherError,
    credentials_configured,
    normalize_credentials,
    reconcile_beer,
    redact_credentials,
    is_importable_batch,
    utc_now as brewfather_now,
)
from bartender.storage import create_state_store

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
    g,
    send_file,
    redirect,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "bartender.json"
UPLOADS_DIR = DATA_DIR / "uploads"
INGRESS_PATH = os.environ.get("INGRESS_PATH", "")
DISPLAY_PORT = os.environ.get("DISPLAY_PORT", "8100")
EXTERNAL_API_MODE = str(os.environ.get("EXTERNAL_API_MODE", "")).strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
TELEMETRY_HEARTBEAT_URL = "https://bartender-telemetry.td2.info/v1/heartbeat"
EXTERNAL_API_PORT = os.environ.get("EXTERNAL_API_PORT", "8110")


def _parse_cors_origins(value: str) -> frozenset[str]:
    return frozenset(
        origin.strip().rstrip("/")
        for origin in re.split(r"[\s,;]+", str(value or ""))
        if origin.strip()
    )


def _normalize_license_portal_url(value: str) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    return candidate


CORS_ALLOWED_ORIGINS = _parse_cors_origins(os.environ.get("CORS_ALLOWED_ORIGINS", ""))
LICENSE_PORTAL_URL = _normalize_license_portal_url(os.environ.get("LICENSE_PORTAL_URL", ""))
LICENSE_PUBLIC_KEY = str(os.environ.get("LICENSE_PUBLIC_KEY", "") or "").strip()
LICENSE_APP_ID = "bartender"
TRIAL_DAYS = 30


def _session_timeout_minutes() -> int:
    try:
        configured = int(os.environ.get("SESSION_TIMEOUT_MINUTES", "240") or "240")
    except (TypeError, ValueError):
        configured = 240
    return max(5, min(configured, 43200))


SESSION_TIMEOUT_MINUTES = _session_timeout_minutes()
DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE = 120
DEFAULT_MOBILE_SESSION_TIMEOUT_MINUTES = 30
DEFAULT_STATION_SESSION_TIMEOUT_MINUTES = 30
_EXTERNAL_API_RATE_LIMIT_BUCKETS: dict[str, deque[float]] = {}
_EXTERNAL_API_RATE_LIMIT_LOCK = threading.Lock()
MAX_LOGO_UPLOAD_BYTES = 2 * 1024 * 1024
ALLOWED_LOGO_MIME_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}
LOGO_FILENAME_PREFIX = "bar-logo"


def _normalize_mobile_session_timeout_minutes(value) -> int:
    try:
        configured = int(value)
    except (TypeError, ValueError):
        configured = DEFAULT_MOBILE_SESSION_TIMEOUT_MINUTES
    return max(5, min(configured, SESSION_TIMEOUT_MINUTES))


def _normalize_station_session_timeout_minutes(value) -> int:
    try:
        configured = int(value)
    except (TypeError, ValueError):
        configured = DEFAULT_STATION_SESSION_TIMEOUT_MINUTES
    return max(5, min(configured, SESSION_TIMEOUT_MINUTES))


def _is_mobile_user_agent(user_agent: str) -> bool:
    return bool(
        re.search(
            r"android|iphone|ipad|ipod|mobile|windows phone",
            str(user_agent or "").lower(),
        )
    )


def _station_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _mobile_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _mobile_principal(data: dict) -> dict | None:
    presented = _extract_request_api_token()
    if not presented:
        return None
    now = datetime.now(timezone.utc)
    token_hash = _mobile_token_hash(presented)
    for token in data.get("mobile_tokens", []):
        if token.get("token_hash") != token_hash or token.get("revoked_at"):
            continue
        try:
            expires_at = datetime.fromisoformat(str(token.get("expires_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if expires_at <= now:
            continue
        return {
            "id": token.get("user_id", ""),
            "name": token.get("user_name", token.get("user_id", "")),
            "role": _normalize_team_role(token.get("user_role", "staff")),
            "auth_type": "mobile",
        }
    return None


def _registered_station(data: dict) -> dict | None:
    token = str(request.cookies.get("bartender_station_token", "") or "").strip()
    if not token:
        return None
    token_hash = _station_token_hash(token)
    for station in data.get("station_registrations", []):
        if station.get("token_hash") == token_hash and not station.get("revoked_at"):
            return station
    return None


def _load_or_create_secret_key() -> str:
    secret_path = DATA_DIR / ".secret_key"
    try:
        secret = secret_path.read_text(encoding="utf-8").strip()
        if secret:
            return secret
        secret = secrets.token_urlsafe(48)
        secret_path.write_text(secret, encoding="utf-8")
        return secret
    except OSError:
        return "bartender-dev-secret-change-me"


@runtime_checkable
class SupportsReadBytes(Protocol):
    def read(self, size: int | None = -1, /) -> bytes: ...


def _read_addon_version() -> str:
    for env_key in ("ADDON_VERSION", "APP_VERSION", "BARTENDER_VERSION", "HA_ADDON_VERSION"):
        val = os.environ.get(env_key, "").strip()
        if val:
            return val

    # If running inside Home Assistant container with Supervisor token
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if supervisor_token:
        try:
            req = Request(
                "http://supervisor/addons/self/info",
                headers={
                    "Authorization": f"Bearer {supervisor_token}",
                    "X-Supervisor-Token": supervisor_token,
                },
            )
            with urlopen(req, timeout=1.5) as resp:
                if 200 <= resp.status < 300:
                    info = json.loads(resp.read().decode("utf-8"))
                    version = info.get("data", {}).get("version") or info.get("version")
                    if version:
                        return str(version).strip()
        except Exception:
            pass

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


def _read_release_highlights_file() -> tuple[str, list[str]]:
    highlights_path = Path(__file__).resolve().parents[1] / "release-highlights.json"
    try:
        with open(highlights_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError, TypeError):
        return "", []

    release_date = str(payload.get("date", "")).strip() if isinstance(payload, dict) else ""
    raw_highlights = payload.get("highlights") if isinstance(payload, dict) else None
    if not isinstance(raw_highlights, list):
        return release_date, []
    highlights = [
        str(highlight).strip()
        for highlight in raw_highlights
        if str(highlight).strip()
    ]
    return release_date, highlights


RELEASE_HIGHLIGHTS_DATE, RELEASE_HIGHLIGHTS = _read_release_highlights_file()

STANDARD_KEG_TYPE_CHOICES = [
    "Corny (5 gal)",
    "1/6 bbl (5.2 gal)",
    "1/4 bbl (7.75 gal)",
    "Full Size (1/2 bbl, 15.5 gal)",
    "20 L",
    "30 L",
    "50 L",
    "Custom",
]

LEGACY_KEG_TYPE_ALIASES = {
    "1/2 bbl (15.5 gal)": "Full Size (1/2 bbl, 15.5 gal)",
}
COMMON_POS_SYSTEMS = [
    "Square",
    "Toast",
    "Clover",
    "Lightspeed",
    "Arryved",
    "Shopify POS",
    "Aloha",
    "Barmetrix",
    "Other",
]

STANDARD_COUPLER_TYPES = [
    "Sankey D (US)",
    "Sankey S (Euro)",
    "Ball Lock",
    "Pin Lock",
    "A (German Slider)",
    "G (Grundy)",
    "M",
    "KeyKeg",
]

STANDARD_OWNERSHIP_TYPES = [
    "Owned",
    "Leased",
    "Deposit",
    "Brewery Owned",
]

STANDARD_GAS_TYPES = [
    "CO2 (100%)",
    "Nitro Blend (70/30)",
    "Nitro Blend (75/25)",
    "Pure N2 (100%)",
    "Beer Gas (60/40)",
]

STANDARD_FAUCET_TYPES = [
    "Forward-Sealing (Standard)",
    "Flow Control",
    "Stout / Nitro",
    "Czech Side-Pull",
    "Rear-Sealing (Standard)",
]

STANDARD_LINE_DIAMETERS = [
    "3/16\" ID",
    "1/4\" ID",
    "5/16\" ID",
    "3/8\" ID",
    "4mm ID",
    "5mm ID",
]

STANDARD_LINE_MATERIALS = [
    "Barrier / EVABarrier",
    "Vinyl",
    "Polyethylene",
    "Stainless Steel",
    "Copper",
]

STANDARD_TAP_STATUSES = [
    "active",
    "cleaning",
    "maintenance",
    "offline",
]

STANDARD_BEER_ALLERGENS = [
    "Barley",
    "Wheat",
    "Gluten",
    "Rye",
    "Oats",
    "Lactose",
    "Tree Nuts",
    "Peanuts",
    "Soy",
    "Sulfites",
    "Honey",
    "Coconut",
    "Fruit",
    "Eggs",
    "Isinglass",
]

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_prefix=1)
app.config["APPLICATION_ROOT"] = INGRESS_PATH or "/"
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or _load_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=SESSION_TIMEOUT_MINUTES)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
DATA_STATE_LOCK = threading.RLock()


@app.after_request
def add_cors_headers(response):
    origin = str(request.headers.get("Origin", "") or "").strip().rstrip("/")
    if origin and origin in CORS_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = (
            "Content-Type, Authorization, X-API-Token"
        )
        response.headers["Access-Control-Allow-Methods"] = (
            "GET, HEAD, POST, PUT, PATCH, DELETE, OPTIONS"
        )
        response.headers.add("Vary", "Origin")
    return response


@app.before_request
def acquire_mutation_lock():
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        DATA_STATE_LOCK.acquire()
        g.data_state_lock_acquired = True


@app.teardown_request
def release_mutation_lock(exception=None):
    if getattr(g, "data_state_lock_acquired", False):
        DATA_STATE_LOCK.release()


def _session_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_timeout_for_type(data: dict, session_type: str, user_agent: str = "") -> int:
    if session_type == "station":
        return _normalize_station_session_timeout_minutes(
            data.get("settings", {}).get("station_session_timeout_minutes")
        )
    if _is_mobile_user_agent(user_agent):
        return _normalize_mobile_session_timeout_minutes(
            data.get("settings", {}).get("mobile_session_timeout_minutes")
        )
    return SESSION_TIMEOUT_MINUTES


def _prune_user_sessions(data: dict) -> None:
    sessions = data.get("user_sessions", [])
    if not isinstance(sessions, list):
        data["user_sessions"] = []
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    retained = []
    for record in sessions:
        if not isinstance(record, dict):
            continue
        terminal_at = record.get("revoked_at") or record.get("expires_at")
        if terminal_at:
            try:
                terminal_time = datetime.fromisoformat(str(terminal_at).replace("Z", "+00:00"))
                if terminal_time.tzinfo is None:
                    terminal_time = terminal_time.replace(tzinfo=timezone.utc)
                if terminal_time < cutoff:
                    continue
            except ValueError:
                pass
        retained.append(record)
    data["user_sessions"] = retained[-500:]


def _create_user_session(
    data: dict,
    user: dict,
    login_method: str,
    station_mode: bool = False,
) -> str:
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    user_agent = str(request.headers.get("User-Agent", "") or "").strip()[:512]
    session_type = "station" if station_mode else ("mobile" if _is_mobile_user_agent(user_agent) else "desktop")
    timeout_minutes = _session_timeout_for_type(data, session_type, user_agent)
    record = {
        "id": session_id,
        "user_id": str(user.get("id", "") or "").strip(),
        "user_name": str(user.get("name", "") or "").strip(),
        "user_role": _normalize_team_role(user.get("role", "staff")),
        "login_method": login_method,
        "device_type": "mobile" if session_type == "mobile" else "desktop",
        "session_type": session_type,
        "user_agent": user_agent,
        "ip_address": str(_get_client_ip_address() or ""),
        "created_at": now.isoformat(),
        "last_activity_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=timeout_minutes)).isoformat(),
        "timeout_minutes": timeout_minutes,
        "revoked_at": "",
    }
    data.setdefault("user_sessions", []).append(record)
    _prune_user_sessions(data)
    session["session_id"] = session_id
    session["last_activity_at"] = now.timestamp()
    return session_id


def _find_user_session(data: dict, session_id: str) -> dict | None:
    return next(
        (record for record in data.get("user_sessions", []) if record.get("id") == session_id),
        None,
    )


@app.before_request
def enforce_session_timeout():
    if EXTERNAL_API_MODE:
        return None

    session_user_id = str(session.get("user_id", "") or "").strip()
    if not session_user_id:
        return None

    data = load_data()
    now = time.time()
    session_id = str(session.get("session_id", "") or "").strip()
    record = _find_user_session(data, session_id) if session_id else None
    if record is None:
        legacy_last_activity = session.get("last_activity_at", now)
        current_user = _get_current_team_user()
        _create_user_session(data, current_user, "legacy")
        session_id = str(session.get("session_id", ""))
        record = _find_user_session(data, session_id)
        try:
            legacy_last_activity = float(legacy_last_activity)
        except (TypeError, ValueError):
            legacy_last_activity = now
        if record:
            record["last_activity_at"] = datetime.fromtimestamp(
                legacy_last_activity, timezone.utc
            ).isoformat()
        session["last_activity_at"] = legacy_last_activity
        save_data(data)

    if record and record.get("revoked_at"):
        session.clear()
        if _normalized_request_path().startswith("/api/"):
            return jsonify({"error": "Session revoked. Please log in again."}), 401
        return _redirect_to_endpoint("login_view")

    timeout_minutes = SESSION_TIMEOUT_MINUTES
    if record:
        if record.get("session_type") == "station":
            timeout_minutes = _normalize_station_session_timeout_minutes(
                data.get("settings", {}).get("station_session_timeout_minutes")
            )
        elif record.get("session_type") == "mobile" or record.get("device_type") == "mobile":
            timeout_minutes = _normalize_mobile_session_timeout_minutes(
                data.get("settings", {}).get("mobile_session_timeout_minutes")
            )
        else:
            timeout_minutes = int(record.get("timeout_minutes", SESSION_TIMEOUT_MINUTES))
        record["timeout_minutes"] = timeout_minutes
    try:
        last_activity = float(session.get("last_activity_at", now))
    except (TypeError, ValueError):
        last_activity = now

    if now - last_activity > timeout_minutes * 60:
        if record:
            record["expires_at"] = _session_now_iso()
            _record_team_audit(data, _get_current_team_user(), "session_expired", session_id, {
                "device_type": record.get("device_type", "desktop"),
            })
            save_data(data)
        session.clear()
        if _normalized_request_path().startswith("/api/"):
            return jsonify({"error": "Session expired. Please log in again."}), 401
        ingress = _effective_ingress_path()
        if ingress:
            return redirect(f"{ingress}/login")
        return redirect(url_for("login_view"))

    session.permanent = True
    session["last_activity_at"] = now
    if record:
        record["last_activity_at"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        record["expires_at"] = datetime.fromtimestamp(
            now + timeout_minutes * 60, timezone.utc
        ).isoformat()
        save_data(data)
    return None


def _effective_ingress_path() -> str:
    for candidate in (
        INGRESS_PATH,
        request.headers.get("X-Ingress-Path"),
        request.headers.get("X-Forwarded-Prefix"),
        request.script_root,
    ):
        raw = str(candidate or "").strip()
        if not raw or raw == "/":
            continue
        return raw.rstrip("/")
    return ""


def _normalized_request_path() -> str:
    raw_path = request.path or "/"
    ingress_prefix = _effective_ingress_path()
    if not ingress_prefix:
        return raw_path

    prefix = ingress_prefix.rstrip("/")
    if raw_path == prefix or raw_path.startswith(prefix + "/"):
        return raw_path[len(prefix):] or "/"
    script_root = str(request.script_root or "").rstrip("/")
    if script_root and raw_path.startswith(script_root):
        return raw_path[len(script_root):] or "/"
    return raw_path


def _redirect_to_endpoint(endpoint: str):
    target = url_for(endpoint)
    ingress = _effective_ingress_path()
    if ingress and target.startswith("/") and not target.startswith(f"{ingress}/") and target != ingress:
        target = f"{ingress}{target}"
    return redirect(target)


def _owner_pin_recovery_needed(data: dict) -> bool:
    users = data.get("team_users", [])
    if not isinstance(users, list) or len(users) <= 1:
        return False
    return not _normalize_owner_pin(data.get("settings", {}).get("owner_pin", ""))


@app.before_request
def require_login_for_web_views():
    normalized_path = _normalized_request_path()
    if normalized_path.startswith("/static/"):
        return None
    if normalized_path in ("/login", "/logout") or normalized_path.startswith("/auth/scan/"):
        return None
    if normalized_path.startswith("/api/"):
        if normalized_path == "/api/mobile/login":
            return None
        if request.method == "OPTIONS":
            return None
        if normalized_path == "/api/settings" and request.method == "POST":
            bootstrap_data = load_data()
            if not bootstrap_data.get("settings", {}).get("setup_completed"):
                return None
        if EXTERNAL_API_MODE:
            return None
        if app.testing:
            return None
        mobile_user = _mobile_principal(load_data())
        if mobile_user:
            g.mobile_user = mobile_user
        if session.get("user_id") or mobile_user:
            return None
        return jsonify({"error": "Authentication required."}), 401
    if session.get("user_id"):
        return None
    ingress = _effective_ingress_path()
    if ingress:
        return redirect(f"{ingress}/login")
    return redirect(url_for("login_view"))


@app.before_request
def enforce_owner_pin_recovery():
    session_user_id = str(session.get("user_id", "") or "").strip()
    if not session_user_id:
        return None
    if _normalize_team_role(session.get("user_role")) != "owner":
        return None
    if not session.get("owner_pin_recovery_required"):
        return None

    data = load_data()
    if not _owner_pin_recovery_needed(data):
        session.pop("owner_pin_recovery_required", None)
        return None

    normalized_path = _normalized_request_path()
    if normalized_path.startswith("/static/"):
        return None
    if normalized_path in (
        "/logout",
        "/settings",
        "/team-access",
        "/api/settings",
        "/api/settings/reset",
        "/api/reset",
    ):
        return None
    if normalized_path.startswith("/api/"):
        return jsonify({
            "error": "Owner PIN setup required before other actions are available.",
        }), 423

    ingress = _effective_ingress_path()
    if ingress:
        return redirect(f"{ingress}/team-access")
    return redirect(url_for("team_access"))


@app.before_request
def enforce_external_api_controls():
    if not EXTERNAL_API_MODE:
        return None

    request_path = _normalized_request_path()
    if not request_path.startswith("/api/"):
        return jsonify({"error": "External API listener exposes API routes only."}), 404
    if request_path.startswith("/api/licensing/"):
        return jsonify({"error": "Licensing is available only through the management UI."}), 404

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
        "brewery_type": "homebrewer",
        "pos_system": "",
        "pos_sync_enabled": False,
        "pos_sync_provider": "",
        "pos_sync_credentials": {
            "api_key": "",
            "location_id": "",
            "merchant_id": "",
        },
        "pos_sync_provider_config_json": "",
        "pos_sync_last_run_at": "",
        "pos_sync_last_status": "never",
        "pos_sync_last_error": "",
        "pos_sync_last_counts": {
            "items_received": 0,
            "taps_updated": 0,
            "taps_created": 0,
        },
        "pos_sync_custom_providers": [],
        "brewfather_enabled": False,
        "brewfather_user_id": "",
        "brewfather_api_key": "",
        "brewfather_last_synced_at": "",
        "brewfather_last_status": "never",
        "brewfather_last_error": "",
        "brewfather_last_counts": {
            "recipes_received": 0,
            "batches_received": 0,
            "beers_created": 0,
            "beers_updated": 0,
            "conflicts": 0,
        },
        "bar_logo_url": "",
        "external_base_url": "",
        "external_api_token_auth_enabled": True,
        "external_api_token": "",
        "external_api_read_token": "",
        "external_api_write_token": "",
        "owner_pin": "",
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
        "display_title_on_tap": "On Draft",
        "display_full_width": False,
        "display_count": 2,
        "display_tap_assignments": [],
        "display_bar_stock_assignments": [],
        "pour_options": [
            {"name": "Pint", "amount": 16, "unit": "oz"},
            {"name": "Half Pint", "amount": 8, "unit": "oz"},
            {"name": "Growler", "amount": 64, "unit": "oz"},
            {"name": "Taste", "amount": 2, "unit": "oz"},
        ],
        "default_pour_preset": "16|oz|Pint",
        "audit_retention_days": 30,
        "analytics_low_keg_threshold_percent": 25,
        "analytics_days_left_method": "trailing_window",
        "analytics_days_left_window_days": 14,
        "anonymous_telemetry_enabled": False,
        "anonymous_telemetry_installation_id": "",
        "anonymous_telemetry_last_heartbeat_date": "",
        "mobile_session_timeout_minutes": DEFAULT_MOBILE_SESSION_TIMEOUT_MINUTES,
        "station_session_timeout_minutes": DEFAULT_STATION_SESSION_TIMEOUT_MINUTES,
        "license_type": "",
        "license_token": "",
        "license_expires_at": "",
        "license_features": [],
        "license_instance_id": "",
        "license_instance_private_key": "",
        "trial_started_at": "",
        "trial_expires_at": "",
    },
    "bar_stock": [],
    "beers": [],
    "kegs": [],
    "taps": [],
    "pour_events": [],
    "team_users": [
        {
            "id": "owner",
            "name": "Owner",
            "role": "owner",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ],
    "team_audit": [],
    "user_sessions": [],
    "station_registrations": [],
    "mobile_tokens": [],
    "brewfather_conflicts": [],
}


def _load_data_unlocked() -> dict:
    database = create_state_store(
        DATA_FILE.with_name("bartender.db"),
        os.environ.get("STORAGE_BACKEND", "internal"),
        os.environ.get("DATABASE_URL", ""),
    )
    database_path: Path | None = getattr(database, "path", None)
    if (database_path is not None and database_path.exists()) or DATA_FILE.exists():
        if database_path is not None and database_path.exists():
            data = database.load(DEFAULT_DATA)
        else:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            database.initialize(data)
        # Ensure all top-level keys exist
        for key, value in DEFAULT_DATA.items():
            if key not in data:
                data[key] = value
        if not isinstance(data.get("brewfather_conflicts"), list):
            data["brewfather_conflicts"] = []
        # Ensure nested settings keys exist for backward compatibility.
        if not isinstance(data.get("settings"), dict):
            data["settings"] = json.loads(json.dumps(DEFAULT_DATA["settings"]))
        else:
            for key, value in DEFAULT_DATA["settings"].items():
                data["settings"].setdefault(key, value)

        _ensure_owner_team_user(data)
        normalized_team_users = []
        for user in data.get("team_users", []):
            if not isinstance(user, dict):
                continue
            user.setdefault("id", "")
            user.setdefault("name", "")
            user["role"] = _normalize_team_role(user.get("role", "staff"))
            user["pin"] = _normalize_team_user_pin(user.get("pin", ""))
            user["disabled"] = _coerce_bool(user.get("disabled"), False)
            user["scan_token_hash"] = str(user.get("scan_token_hash", "") or "").strip()
            user["scan_issued_at"] = str(user.get("scan_issued_at", "") or "").strip()
            user["scan_require_pin"] = _coerce_bool(user.get("scan_require_pin"), False)
            user["release_seen_version"] = str(user.get("release_seen_version", "") or "").strip()[:64]
            normalized_team_users.append(user)
        data["team_users"] = normalized_team_users

        setup_default = str(data["settings"].get("bar_name", "")).strip() not in ("", "My Bar")
        data["settings"]["setup_completed"] = _coerce_bool(
            data["settings"].get("setup_completed"),
            setup_default,
        )
        data["settings"]["brewery_type"] = _normalize_brewery_type(
            data["settings"].get("brewery_type")
        )
        data["settings"]["pos_system"] = _normalize_pos_system(
            data["settings"].get("pos_system"),
            data["settings"].get("brewery_type"),
            data["settings"].get("pour_mode"),
        )
        normalize_pos_sync_settings(data["settings"])
        _normalize_brewfather_settings(data["settings"])
        data["settings"]["pour_mode"] = _normalize_pour_mode(
            data["settings"].get("pour_mode"),
            data["settings"].get("brewery_type"),
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
        data["settings"]["display_title_on_tap"] = _normalize_display_title_on_tap(
            data["settings"].get("display_title_on_tap")
        )
        data["settings"]["display_full_width"] = _coerce_bool(
            data["settings"].get("display_full_width"),
            False,
        )
        data["settings"]["display_count"] = _normalize_display_count(
            data["settings"].get("display_count"),
            data["settings"].get("brewery_type"),
        )
        data["settings"]["display_tap_assignments"] = _normalize_display_tap_assignments(
            data["settings"].get("display_tap_assignments"),
            data["settings"].get("display_count", 2),
        )
        data["settings"]["display_bar_stock_assignments"] = _normalize_display_bar_stock_assignments(
            data["settings"].get("display_bar_stock_assignments"),
            data["settings"].get("display_count", 2),
            data["settings"].get("brewery_type"),
        )
        data["settings"]["audit_retention_days"] = _normalize_audit_retention_days(
            data["settings"].get("audit_retention_days")
        )
        _prune_team_audit_by_retention(data)
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
        data["settings"]["anonymous_telemetry_enabled"] = _coerce_bool(
            data["settings"].get("anonymous_telemetry_enabled"),
            False,
        )
        if _normalize_brewery_type(data["settings"].get("brewery_type")) == "pro":
            data["settings"]["mobile_session_timeout_minutes"] = _normalize_mobile_session_timeout_minutes(
                data["settings"].get("mobile_session_timeout_minutes")
            )
            data["settings"]["station_session_timeout_minutes"] = _normalize_station_session_timeout_minutes(
                data["settings"].get("station_session_timeout_minutes")
            )
        else:
            data["settings"]["mobile_session_timeout_minutes"] = DEFAULT_MOBILE_SESSION_TIMEOUT_MINUTES
            data["settings"]["station_session_timeout_minutes"] = DEFAULT_STATION_SESSION_TIMEOUT_MINUTES
        data["settings"]["license_type"] = str(data["settings"].get("license_type", "") or "").strip().lower()
        data["settings"]["license_token"] = str(data["settings"].get("license_token", "") or "").strip()
        data["settings"]["license_expires_at"] = str(data["settings"].get("license_expires_at", "") or "").strip()
        data["settings"]["license_features"] = data["settings"].get("license_features", []) if isinstance(data["settings"].get("license_features", []), list) else []
        data["settings"]["license_instance_id"] = str(data["settings"].get("license_instance_id", "") or "").strip()[:128]
        data["settings"]["license_instance_private_key"] = str(data["settings"].get("license_instance_private_key", "") or "").strip()
        data["settings"]["trial_started_at"] = str(data["settings"].get("trial_started_at", "") or "").strip()
        data["settings"]["trial_expires_at"] = str(data["settings"].get("trial_expires_at", "") or "").strip()
        data["settings"].setdefault("anonymous_telemetry_installation_id", "")
        data["settings"].setdefault("anonymous_telemetry_last_heartbeat_date", "")
        if not isinstance(data.get("user_sessions"), list):
            data["user_sessions"] = []
        if not isinstance(data.get("station_registrations"), list):
            data["station_registrations"] = []
        if not isinstance(data.get("mobile_tokens"), list):
            data["mobile_tokens"] = []
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
            keg.setdefault("beer_brewery", keg.get("brewery", ""))
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
    data = database.load(DEFAULT_DATA)
    return json.loads(json.dumps(data))


def load_data() -> dict:
    with DATA_STATE_LOCK:
        return _load_data_unlocked()


def save_data(data: dict) -> None:
    with DATA_STATE_LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        create_state_store(
            DATA_FILE.with_name("bartender.db"),
            os.environ.get("STORAGE_BACKEND", "internal"),
            os.environ.get("DATABASE_URL", ""),
        ).save(data)
        temporary_file = DATA_FILE.with_name(f"{DATA_FILE.name}.tmp")
        with open(temporary_file, "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle, indent=2)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_file, DATA_FILE)


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


def _send_anonymous_telemetry_heartbeat() -> None:
    data = load_data()
    settings = data.get("settings", {})
    if not _coerce_bool(settings.get("anonymous_telemetry_enabled"), False):
        app.logger.info("Anonymous telemetry heartbeat skipped: disabled.")
        return

    today = datetime.now(timezone.utc).date().isoformat()
    if settings.get("anonymous_telemetry_last_heartbeat_date") == today:
        app.logger.info("Anonymous telemetry heartbeat skipped: already sent today.")
        return

    installation_id = str(settings.get("anonymous_telemetry_installation_id", "")).strip()
    if not installation_id:
        installation_id = str(uuid.uuid4())
        settings["anonymous_telemetry_installation_id"] = installation_id

    payload = json.dumps({
        "installation_id": installation_id,
        "app_version": APP_VERSION,
        "addon_version": APP_VERSION,
        "brewery_type": _normalize_brewery_type(settings.get("brewery_type")),
    }).encode("utf-8")
    request_payload = Request(
        TELEMETRY_HEARTBEAT_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"BarTender/{APP_VERSION}",
        },
        method="POST",
    )
    try:
        with urlopen(request_payload, timeout=5) as response:
            if response.status < 200 or response.status >= 300:
                app.logger.warning(
                    "Anonymous telemetry heartbeat rejected with HTTP status %s.",
                    response.status,
                )
                return
    except HTTPError as exc:
        app.logger.warning(
            "Anonymous telemetry heartbeat rejected with HTTP status %s.",
            exc.code,
        )
        return
    except OSError as exc:
        app.logger.warning(
            "Anonymous telemetry heartbeat failed: %s.",
            type(exc).__name__,
        )
        return

    settings["anonymous_telemetry_last_heartbeat_date"] = today
    save_data(data)
    app.logger.info("Anonymous telemetry heartbeat sent.")


def _schedule_anonymous_telemetry_heartbeat() -> None:
    threading.Thread(target=_send_anonymous_telemetry_heartbeat, daemon=True).start()


def _normalize_menu_qr_mode(value) -> str:
    mode = str(value or "both").strip().lower()
    if mode in ("off", "display", "print", "both"):
        return mode
    return "both"


def _normalize_display_title_on_tap(value) -> str:
    normalized = str(value or "On Draft").strip()
    if not normalized:
        return "On Draft"
    return normalized[:80]


def _normalize_display_count(value, brewery_type: str | None = None) -> int:
    normalized_type = _normalize_brewery_type(brewery_type)
    count = _coerce_int(value, 2)
    if count is None:
        return 2
    if normalized_type != "pro":
        return 2
    return max(1, min(12, count))


def _normalize_display_tap_assignments(value, display_count: int = 2) -> list[list[int]]:
    raw = value if isinstance(value, list) else []
    count = max(1, _coerce_int(display_count, 2) or 2)
    normalized = []
    for index in range(count):
        numbers = []
        if index < len(raw) and isinstance(raw[index], list):
            for item in raw[index]:
                number = _coerce_int(item, None)
                if number is not None and number > 0:
                    numbers.append(number)
        normalized.append(sorted(set(numbers)))
    return normalized


def _normalize_display_bar_stock_assignments(
    value,
    display_count: int = 2,
    brewery_type: str | None = None,
) -> list[bool]:
    count = max(1, _coerce_int(display_count, 2) or 2)
    raw = value if isinstance(value, list) else []
    normalized = [_coerce_bool(raw[index], False) if index < len(raw) else False for index in range(count)]
    if not any(normalized) and _normalize_brewery_type(brewery_type) == "pro":
        normalized[min(1, count - 1)] = True
    return normalized


def _assign_existing_taps_to_first_display(data: dict) -> bool:
    settings = data.get("settings", {})
    display_count = _normalize_display_count(
        settings.get("display_count"),
        settings.get("brewery_type"),
    )
    assignments = _normalize_display_tap_assignments(
        settings.get("display_tap_assignments"),
        display_count,
    )
    if any(assignments):
        return False

    tap_numbers = sorted(
        {
            number
            for tap in data.get("taps", [])
            if isinstance(tap, dict)
            for number in [_coerce_int(tap.get("number"), None)]
            if number is not None and number > 0
        }
    )
    if not tap_numbers:
        return False

    assignments[0] = tap_numbers
    settings["display_tap_assignments"] = assignments
    return True


def _reset_display_configuration_to_defaults(data: dict) -> dict:
    tap_numbers = sorted(
        {
            number
            for tap in data.get("taps", [])
            if isinstance(tap, dict)
            for number in [_coerce_int(tap.get("number"), None)]
            if number is not None and number > 0
        }
    )
    configuration = {
        "display_count": 2,
        "display_tap_assignments": [tap_numbers, []],
        "display_bar_stock_assignments": [False, True],
    }
    data["settings"].update(configuration)
    return configuration


def _normalize_pour_mode(value, brewery_type: str | None = None) -> str:
    mode = str(value or "manual").strip().lower()
    normalized_type = _normalize_brewery_type(brewery_type)
    if mode == "pos" and normalized_type != "pro":
        return "manual"
    if mode in ("manual", "pos", "inline_device"):
        return mode
    return "manual"


def _normalize_environment_mode(value) -> str:
    mode = str(value or "production").strip().lower()
    if mode in ("sandbox", "production"):
        return mode
    return "production"


def _license_b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(str(value).encode("ascii") + b"=" * (-len(str(value)) % 4))


def _license_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _load_license_public_key(value: str):
    if Ed25519PublicKey is None or serialization is None:
        raise ValueError("License verification is not configured.")
    normalized = str(value or "").strip()
    try:
        if normalized.startswith("-----BEGIN PUBLIC KEY-----"):
            public_key = serialization.load_pem_public_key(normalized.encode("ascii"))
        else:
            decoded = _license_b64decode(normalized)
            public_key = (
                Ed25519PublicKey.from_public_bytes(decoded)
                if len(decoded) == 32
                else serialization.load_der_public_key(decoded)
            )
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("License verification key is invalid.") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("License verification key is not Ed25519.")
    return public_key


def _license_normalize_bar_name(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _license_bar_name_hash(value: str) -> str:
    normalized = _license_normalize_bar_name(value)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _ensure_license_instance_identity(data: dict) -> tuple[str, str]:
    settings = data["settings"]
    instance_id = str(settings.get("license_instance_id", "") or "").strip()
    private_key_value = str(settings.get("license_instance_private_key", "") or "").strip()
    if instance_id and private_key_value:
        return instance_id, private_key_value
    if Ed25519PrivateKey is None or serialization is None:
        raise ValueError("License activation request generation is not configured.")

    instance_id = instance_id or secrets.token_urlsafe(18)
    private_key = Ed25519PrivateKey.generate()
    private_key_value = _license_b64encode(
        private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings["license_instance_id"] = instance_id
    settings["license_instance_private_key"] = private_key_value
    return instance_id, private_key_value


def _license_instance_public_key(private_key_value: str) -> str:
    if Ed25519PrivateKey is None or serialization is None:
        raise ValueError("License activation request generation is not configured.")
    private_key = Ed25519PrivateKey.from_private_bytes(_license_b64decode(private_key_value))
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _license_b64encode(public_key)


def _license_instance_key_id(public_key: str) -> str:
    return "sha256:" + hashlib.sha256(_license_b64decode(public_key)).hexdigest()


def _license_instance_public_key_sha256(public_key: str) -> str:
    return _license_b64encode(hashlib.sha256(_license_b64decode(public_key)).digest())


def _license_sign_activation_request(
    private_key_value: str,
    app_id: str,
    instance_id: str,
    instance_key_id: str,
    nonce: str,
) -> str:
    if Ed25519PrivateKey is None:
        raise ValueError("License activation request generation is not configured.")
    private_key = Ed25519PrivateKey.from_private_bytes(_license_b64decode(private_key_value))
    message = f"{app_id}.{instance_id}.{instance_key_id}.{nonce}".encode("utf-8")
    return _license_b64encode(private_key.sign(message))


def _license_status(settings: dict) -> dict:
    now = datetime.now(timezone.utc)
    license_type = str(settings.get("license_type", "") or "").strip().lower()
    expires_at = str(settings.get("license_expires_at", "") or "").strip()
    if license_type == "trial" and not expires_at:
        expires_at = str(settings.get("trial_expires_at", "") or "").strip()
    try:
        expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) if expires_at else None
        if expiration and expiration.tzinfo is None:
            expiration = expiration.replace(tzinfo=timezone.utc)
    except ValueError:
        expiration = None

    if license_type in ("trial", "paid") and expiration and expiration > now:
        return {
            "plan": "Trial" if license_type == "trial" else "Pro",
            "license_type": license_type,
            "active": True,
            "expires_at": expiration.isoformat(),
            "days_remaining": max(0, (expiration.date() - now.date()).days),
            "features": settings.get("license_features", []),
        }
    return {
        "plan": "Base",
        "license_type": "",
        "active": False,
        "expires_at": "",
        "days_remaining": 0,
        "features": [],
    }


def _validate_license_token(token: str) -> dict:
    if not LICENSE_PUBLIC_KEY or Ed25519PublicKey is None:
        raise ValueError("License verification is not configured.")
    parts = str(token or "").strip().split(".")
    if len(parts) not in (2, 3):
        raise ValueError("Invalid license token format.")
    if len(parts) == 3:
        try:
            header = json.loads(_license_b64decode(parts[0]).decode("utf-8"))
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid license token header.") from exc
        if header.get("alg") != "EdDSA":
            raise ValueError("Unsupported license token algorithm.")
        if header.get("typ") != "license+jwt" or not str(header.get("kid", "")).strip():
            raise ValueError("Invalid license token header claims.")
        signed_message = f"{parts[0]}.{parts[1]}".encode("ascii")
        payload_bytes = _license_b64decode(parts[1])
        signature = _license_b64decode(parts[2])
    else:
        signed_message = _license_b64decode(parts[0])
        payload_bytes = signed_message
        signature = _license_b64decode(parts[1])
    public_key = _load_license_public_key(LICENSE_PUBLIC_KEY)
    try:
        public_key.verify(signature, signed_message)
    except InvalidSignature as exc:
        raise ValueError("Invalid license signature.") from exc
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid license payload.") from exc
    if payload.get("app_id") != LICENSE_APP_ID or payload.get("plan") != "pro":
        raise ValueError("License is not valid for this application.")
    if len(parts) == 3:
        required_claims = (
            "version",
            "license_id",
            "aud",
            "license_type",
            "issued_at",
            "expires_at",
        )
        if any(not payload.get(claim) for claim in required_claims):
            raise ValueError("License token is missing required claims.")
        if payload.get("version") != 1 or payload.get("aud") != LICENSE_APP_ID:
            raise ValueError("License token is not valid for this application.")
        if payload.get("license_type") not in ("pro", "trial"):
            raise ValueError("License token has an unsupported license type.")
        binding = payload.get("instance_binding")
        if not isinstance(binding, dict) or any(
            not binding.get(claim)
            for claim in ("instance_value", "instance_key_id", "instance_public_key_sha256")
        ):
            raise ValueError("License token is missing required instance binding.")
    return payload


def _normalize_team_role(value) -> str:
    role = str(value or "staff").strip().lower()
    if role in ("owner", "manager", "staff"):
        return role
    return "staff"


def _get_current_team_user() -> dict:
    mobile_user = getattr(g, "mobile_user", None)
    if mobile_user:
        return mobile_user
    session_user_id = str(session.get("user_id", "") or "").strip()
    session_role = session.get("user_role")
    session_name = session.get("user_name")
    if session_user_id:
        return {
            "id": session_user_id,
            "name": str(session_name or session_user_id),
            "role": _normalize_team_role(session_role),
        }

    if not app.testing:
        return {"id": "anonymous", "name": "Anonymous", "role": "staff"}
    user_id = str(request.headers.get("X-BarTender-User-Id", "owner") or "owner").strip()
    role = _normalize_team_role(request.headers.get("X-BarTender-Role", "owner"))
    name = str(request.headers.get("X-BarTender-Name", user_id or "Owner")).strip() or "Owner"
    return {"id": user_id or "owner", "name": name, "role": role}


def _find_team_user_by_identifier(users: list[dict], value: str) -> dict | None:
    lookup = str(value or "").strip()
    if not lookup:
        return None
    lowered = lookup.lower()
    for user in users:
        user_id = str(user.get("id", "")).strip()
        user_name = str(user.get("name", "")).strip()
        if user_id.lower() == lowered or user_name.lower() == lowered:
            return user
    return None


def _scan_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _find_scan_user(data: dict, token: str) -> dict | None:
    token_hash = _scan_token_hash(token)
    for user in data.get("team_users", []):
        if not isinstance(user, dict):
            continue
        if secrets.compare_digest(str(user.get("scan_token_hash", "")), token_hash):
            return user
    return None


def _scan_requires_pin(user: dict) -> bool:
    return _coerce_bool(user.get("scan_require_pin"), False)


def _scan_login_url(token: str) -> str:
    ingress = _effective_ingress_path()
    return f"{ingress}/auth/scan/{token}"


def _public_team_user(user: dict) -> dict:
    public = {key: value for key, value in user.items() if key not in ("scan_token_hash",)}
    public["scan_enabled"] = bool(str(user.get("scan_token_hash", "")).strip())
    return public


def _current_user_release_seen_version() -> str:
    user_id = str(session.get("user_id", "") or "").strip().lower()
    if not user_id:
        return ""
    data = load_data()
    for user in data.get("team_users", []):
        if str(user.get("id", "")).strip().lower() == user_id:
            return str(user.get("release_seen_version", "") or "").strip()
    return ""


def _qr_png_data_url(value: str) -> str:
    if qrcode is None:
        return ""
    buffer = io.BytesIO()
    qr_module = cast(Any, qrcode)
    qr_module.make(value).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _ensure_owner_team_user(data: dict) -> None:
    users = data.get("team_users", [])
    if not isinstance(users, list):
        users = []
    for user in users:
        if not isinstance(user, dict):
            continue
        user.setdefault("pin", "")
        user.setdefault("disabled", False)
    has_owner = any(
        isinstance(user, dict) and str(user.get("role", "")).strip().lower() == "owner"
        for user in users
    )
    if has_owner or not users:
        data["team_users"] = users
        return

    users.insert(0, {
        "id": "owner",
        "name": "Owner",
        "role": "owner",
        "pin": "",
        "disabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    data["team_users"] = users


def _team_can(role: str, action: str) -> bool:
    normalized = _normalize_team_role(role)
    allowed = {
        "settings": {"owner", "manager"},
        "team_manage": {"owner", "manager"},
        "team_view": {"owner", "manager", "staff"},
        "audit_view": {"owner", "manager", "staff"},
    }
    return action in allowed and normalized in allowed[action]


def _normalize_brewfather_settings(settings: dict) -> None:
    settings["brewfather_enabled"] = _coerce_bool(settings.get("brewfather_enabled"), False)
    settings["brewfather_user_id"] = str(settings.get("brewfather_user_id", "") or "").strip()[:256]
    settings["brewfather_api_key"] = str(settings.get("brewfather_api_key", "") or "").strip()[:256]
    settings["brewfather_last_synced_at"] = str(settings.get("brewfather_last_synced_at", "") or "").strip()
    status = str(settings.get("brewfather_last_status", "never") or "never").strip().lower()
    settings["brewfather_last_status"] = status if status in ("never", "success", "failed") else "never"
    settings["brewfather_last_error"] = str(settings.get("brewfather_last_error", "") or "").strip()[:500]
    raw_counts = settings.get("brewfather_last_counts", {})
    raw_counts = raw_counts if isinstance(raw_counts, dict) else {}
    settings["brewfather_last_counts"] = {
        key: max(0, _coerce_int(raw_counts.get(key), 0) or 0)
        for key in (
            "recipes_received",
            "batches_received",
            "beers_created",
            "beers_updated",
            "conflicts",
        )
    }


def _brewfather_credentials(settings: dict) -> dict[str, str]:
    return normalize_credentials(settings.get("brewfather_user_id"), settings.get("brewfather_api_key"))


def _brewfather_settings_response(settings: dict) -> dict:
    _normalize_brewfather_settings(settings)
    response = {key: value for key, value in settings.items() if not key.startswith("brewfather_")}
    response.update({
        "brewfather_enabled": settings["brewfather_enabled"],
        "brewfather_last_synced_at": settings["brewfather_last_synced_at"],
        "brewfather_last_status": settings["brewfather_last_status"],
        "brewfather_last_error": settings["brewfather_last_error"],
        "brewfather_last_counts": settings["brewfather_last_counts"],
        "brewfather_credentials": redact_credentials(_brewfather_credentials(settings)),
    })
    return response


def _prune_team_audit_by_retention(data: dict) -> None:
    team_audit = data.get("team_audit", [])
    if not isinstance(team_audit, list):
        data["team_audit"] = []
        return

    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    retention_days = _normalize_audit_retention_days(settings.get("audit_retention_days", 30))
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    filtered = []
    for entry in team_audit:
        if not isinstance(entry, dict):
            continue
        try:
            entry_created = datetime.fromisoformat(str(entry.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            filtered.append(entry)
            continue
        if entry_created.tzinfo is None:
            entry_created = entry_created.replace(tzinfo=timezone.utc)
        if entry_created >= cutoff:
            filtered.append(entry)
    data["team_audit"] = filtered[:1000]


def _record_team_audit(data: dict, actor: dict, action: str, target: str, details: dict | None = None) -> None:
    event = {
        "id": str(int(time.time() * 1000)),
        "actor_id": actor.get("id", "owner"),
        "actor_name": actor.get("name", "Owner"),
        "actor_role": _normalize_team_role(actor.get("role", "owner")),
        "action": action,
        "target": target,
        "details": details or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    team_audit = data.setdefault("team_audit", [])
    if not isinstance(team_audit, list):
        team_audit = []
    _prune_team_audit_by_retention(data)
    team_audit = data["team_audit"]
    if not isinstance(team_audit, list):
        team_audit = []
    team_audit.insert(0, event)
    data["team_audit"] = team_audit[:1000]


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


def _default_settings_snapshot() -> dict:
    return json.loads(json.dumps(DEFAULT_DATA["settings"]))


def _default_data_snapshot() -> dict:
    return json.loads(json.dumps(DEFAULT_DATA))


def _normalize_settings_in_place(data: dict, setup_completed_explicit: bool = False) -> None:
    settings = data.get("settings")
    if not isinstance(settings, dict):
        settings = _default_settings_snapshot()
        data["settings"] = settings

    if settings.get("dashboard_manage_button_position") not in (
        "top-right",
        "bottom-left",
        "bottom-right",
    ):
        settings["dashboard_manage_button_position"] = "top-right"

    settings.pop("manage_button_position", None)

    settings["bar_stock_enabled"] = _coerce_bool(settings.get("bar_stock_enabled"), True)
    settings["analytics_enabled"] = _coerce_bool(settings.get("analytics_enabled"), True)
    settings["api_reference_enabled"] = _coerce_bool(settings.get("api_reference_enabled"), True)
    settings["brewery_type"] = _normalize_brewery_type(settings.get("brewery_type"))
    settings["pos_system"] = _normalize_pos_system(
        settings.get("pos_system"),
        settings.get("brewery_type"),
        settings.get("pour_mode"),
    )
    normalize_pos_sync_settings(settings)
    _normalize_brewfather_settings(settings)
    settings["pour_mode"] = _normalize_pour_mode(
        settings.get("pour_mode"),
        settings.get("brewery_type"),
    )
    settings["environment_mode"] = _normalize_environment_mode(settings.get("environment_mode"))
    if setup_completed_explicit:
        settings["setup_completed"] = _coerce_bool(settings.get("setup_completed"), False)
    elif str(settings.get("bar_name", "")).strip() not in ("", "My Bar"):
        settings["setup_completed"] = True
    settings["keg_type_choices"] = _normalize_keg_type_choices(
        settings.get("keg_type_choices", []),
        settings.get("default_keg_type", ""),
    )
    settings["default_keg_type"] = _normalize_default_keg_type(
        settings.get("default_keg_type", ""),
        settings.get("keg_type_choices", []),
    )
    settings["menu_qr_mode"] = _normalize_menu_qr_mode(settings.get("menu_qr_mode"))
    settings["display_title_on_tap"] = _normalize_display_title_on_tap(
        settings.get("display_title_on_tap")
    )
    settings["display_full_width"] = _coerce_bool(
        settings.get("display_full_width"),
        False,
    )
    settings["display_count"] = _normalize_display_count(
        settings.get("display_count"),
        settings.get("brewery_type"),
    )
    settings["display_tap_assignments"] = _normalize_display_tap_assignments(
        settings.get("display_tap_assignments"),
        settings.get("display_count", 2),
    )
    settings["display_bar_stock_assignments"] = _normalize_display_bar_stock_assignments(
        settings.get("display_bar_stock_assignments"),
        settings.get("display_count", 2),
        settings.get("brewery_type"),
    )
    settings["analytics_low_keg_threshold_percent"] = _normalize_low_keg_threshold(
        settings.get("analytics_low_keg_threshold_percent")
    )
    settings["analytics_days_left_method"] = _normalize_days_left_method(
        settings.get("analytics_days_left_method")
    )
    settings["analytics_days_left_window_days"] = _normalize_days_left_window_days(
        settings.get("analytics_days_left_window_days")
    )
    settings["pour_options"] = _normalize_pour_options(
        settings.get("pour_options"),
        settings.get("measurement", "us"),
    )
    settings["default_pour_preset"] = _normalize_default_pour_preset(
        settings.get("default_pour_preset", ""),
        settings.get("pour_options", []),
    )
    settings["bar_logo_url"] = _normalize_logo_url(settings.get("bar_logo_url", ""))
    settings["external_base_url"] = _normalize_external_base_url(settings.get("external_base_url", ""))
    settings["external_api_token_auth_enabled"] = _coerce_bool(
        settings.get("external_api_token_auth_enabled"),
        True,
    )
    settings["external_api_token"] = _normalize_external_api_token(settings.get("external_api_token", ""))
    settings["external_api_read_token"] = _normalize_external_api_token(
        settings.get("external_api_read_token", "")
    )
    settings["external_api_write_token"] = _normalize_external_api_token(
        settings.get("external_api_write_token", "")
    )
    settings["owner_pin"] = _normalize_owner_pin(settings.get("owner_pin", ""))
    settings["external_api_allowlist_enabled"] = _coerce_bool(
        settings.get("external_api_allowlist_enabled"),
        False,
    )
    settings["external_api_allowlist"] = _normalize_ip_allowlist_text(
        settings.get("external_api_allowlist", "")
    )
    settings["external_api_rate_limit_enabled"] = _coerce_bool(
        settings.get("external_api_rate_limit_enabled"),
        True,
    )
    settings["external_api_rate_limit_per_minute"] = _normalize_external_api_rate_limit_per_minute(
        settings.get("external_api_rate_limit_per_minute")
    )
    settings["audit_retention_days"] = _normalize_audit_retention_days(
        settings.get("audit_retention_days")
    )


def _normalize_logo_url(value) -> str:
    url = str(value or "").strip()
    if len(url) > 2048:
        return ""
    return url


def _uploaded_logo_candidates() -> list[Path]:
    if not UPLOADS_DIR.exists():
        return []
    return sorted(
        UPLOADS_DIR.glob(f"{LOGO_FILENAME_PREFIX}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def _get_uploaded_logo_file_path() -> Path | None:
    candidates = _uploaded_logo_candidates()
    if not candidates:
        return None
    return candidates[0]


def _remove_uploaded_logos() -> None:
    for path in _uploaded_logo_candidates():
        try:
            path.unlink()
        except OSError:
            continue


def _build_uploaded_logo_url() -> str:
    cache_bust = int(time.time())
    return f"media/bar-logo?v={cache_bust}"


def _infer_logo_extension(uploaded_file, content: bytes) -> str | None:
    mime_type = str(uploaded_file.mimetype or "").strip().lower()
    if mime_type in ALLOWED_LOGO_MIME_TYPES:
        return ALLOWED_LOGO_MIME_TYPES[mime_type]

    guessed_type, _ = mimetypes.guess_type(str(uploaded_file.filename or ""))
    guessed_type = str(guessed_type or "").strip().lower()
    if guessed_type in ALLOWED_LOGO_MIME_TYPES:
        return ALLOWED_LOGO_MIME_TYPES[guessed_type]

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp"

    preview = content[:512].lstrip()
    if preview.lower().startswith(b"<svg") or preview.lower().startswith(b"<?xml"):
        return ".svg"

    return None


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


def _normalize_owner_pin(value) -> str:
    pin = str(value or "").strip()
    if len(pin) > 32:
        pin = pin[:32]
    return pin


def _normalize_team_user_pin(value) -> str:
    pin = str(value or "").strip()
    if len(pin) > 32:
        pin = pin[:32]
    return pin


def _normalize_external_api_rate_limit_per_minute(value) -> int:
    parsed = _coerce_int(value, DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE)
    if parsed is None:
        return DEFAULT_EXTERNAL_API_RATE_LIMIT_PER_MINUTE
    return max(1, min(5000, parsed))


def _normalize_audit_retention_days(value) -> int:
    parsed = _coerce_int(value, 30)
    if parsed is None:
        return 30
    return max(1, min(180, parsed))


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


def _get_request_upload(name: str) -> SupportsReadBytes | None:
    upload = request.files.get(name)
    if upload is not None:
        return cast(SupportsReadBytes, upload)

    candidate = request.form.get(name)
    if candidate is not None and hasattr(candidate, "read"):
        return cast(SupportsReadBytes, candidate)

    content_type = str(request.content_type or "")
    if not content_type.startswith("multipart/form-data"):
        return None

    raw_body = request.get_data(cache=True, as_text=False)
    boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;]+))", content_type, re.IGNORECASE)
    if not boundary_match:
        return None
    boundary = (boundary_match.group(1) or boundary_match.group(2) or "").strip()
    if not boundary:
        return None

    marker = b"--" + boundary.encode("utf-8")
    for chunk in raw_body.split(marker):
        if not chunk:
            continue
        chunk = chunk.lstrip(b"\r\n")
        if chunk in (b"--", b"--\r\n", b"--\n"):
            continue
        header_end = chunk.find(b"\r\n\r\n")
        if header_end == -1:
            header_end = chunk.find(b"\n\n")
        if header_end == -1:
            continue
        headers = chunk[:header_end].decode("latin-1", "replace")
        payload = chunk[header_end + 4 :].rstrip(b"\r\n")
        if payload.endswith(b"--"):
            payload = payload[:-2]
        if f'name="{name}"' in headers or f"name={name}" in headers:
            if "filename=" in headers:
                return io.BytesIO(payload)
    return None


def _record_pour_event(
    data: dict,
    keg: dict,
    amount: float,
    unit: str,
    source: str,
    tap_id=None,
    preset_name: str = "",
    actor: dict | None = None,
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
    if actor is not None:
        _record_team_audit(
            data,
            actor,
            "pour_recorded",
            f"keg:{keg.get('id')}",
            {
                "keg_name": keg.get("name", ""),
                "tap_id": tap_id,
                "amount": event["amount"],
                "unit": normalized_unit,
                "source": source,
                "preset_name": preset_name,
            },
        )


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
            unit_value = event.get("unit")
            if not isinstance(unit_value, str):
                unit_value = None
            amount_ml = _convert_volume(
                _coerce_float(event.get("amount"), 0.0) or 0.0,
                unit_value,
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
            {"name": "Pint", "amount": 473, "unit": "ml"},
            {"name": "Half Pint", "amount": 237, "unit": "ml"},
            {"name": "Growler", "amount": 1893, "unit": "ml"},
            {"name": "Taste", "amount": 59, "unit": "ml"},
        ]
    return [
        {"name": "Pint", "amount": 16, "unit": "oz"},
        {"name": "Half Pint", "amount": 8, "unit": "oz"},
        {"name": "Growler", "amount": 64, "unit": "oz"},
        {"name": "Taste", "amount": 2, "unit": "oz"},
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


def _preferred_default_pour_preset(pour_options: list[dict]) -> str:
    for option in pour_options:
        if str(option.get("name", "")).strip().lower() == "pint":
            return _pour_option_value(option)
    return _pour_option_value(pour_options[0])


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

    return _preferred_default_pour_preset(pour_options)


def _normalize_brewery_type(value) -> str:
    normalized = str(value or "homebrewer").strip().lower()
    if normalized == "commercial":
        return "pro"
    if normalized in ("homebrewer", "pro"):
        return normalized
    return "homebrewer"


def _homebrewer_limit_for(collection: str) -> int:
    if collection == "taps":
        return 12
    if collection == "kegs":
        return 20
    return 0


def _enforce_homebrewer_limits(data: dict, collection: str) -> tuple[bool, str | None]:
    if data.get("settings", {}).get("brewery_type") != "homebrewer":
        return True, None

    limit = _homebrewer_limit_for(collection)
    if limit <= 0:
        return True, None

    current_count = len(data.get(collection, []) if isinstance(data.get(collection, []), list) else [])
    if current_count >= limit:
        return False, f"Homebrewer mode is limited to {limit} {collection}."
    return True, None


def _normalize_pos_system(value, brewery_type: str | None = None, pour_mode: str | None = None) -> str:
    normalized_type = _normalize_brewery_type(brewery_type)
    normalized_mode = str(pour_mode or "manual").strip().lower()
    valid_choices = {str(choice).strip().lower() for choice in COMMON_POS_SYSTEMS}
    candidate = str(value or "").strip()
    if normalized_type != "pro" or normalized_mode != "pos":
        return ""
    if not candidate:
        return ""
    lookup = candidate.strip().lower()
    if lookup in valid_choices:
        return next(choice for choice in COMMON_POS_SYSTEMS if choice.lower() == lookup)
    return ""


def _normalize_keg_type_choices(raw_choices, default_type: str) -> list[str]:
    choices = []
    seen = set()

    if isinstance(raw_choices, list):
        for item in raw_choices:
            value = _normalize_builtin_keg_type_label(item)
            key = value.lower()
            if not value or key in seen:
                continue
            seen.add(key)
            choices.append(value)

    fallback = _normalize_builtin_keg_type_label(default_type)
    fallback_key = fallback.lower()
    if fallback and fallback_key not in seen:
        choices.append(fallback)

    if choices:
        return choices

    return STANDARD_KEG_TYPE_CHOICES.copy()


def _normalize_builtin_keg_type_label(value) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""

    for legacy_value, canonical_value in LEGACY_KEG_TYPE_ALIASES.items():
        if normalized.lower() == legacy_value.lower():
            return canonical_value

    return normalized


def _normalize_default_keg_type(raw_default: str, choices: list[str]) -> str:
    default_value = _normalize_builtin_keg_type_label(raw_default)
    if not choices:
        return default_value

    if not default_value:
        return choices[0]

    for item in choices:
        if _normalize_builtin_keg_type_label(item).lower() == default_value.lower():
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
            "style_guideline": str(entry.get("style_guideline", "")).strip(),
            "packaging": _normalize_beer_packaging(entry.get("packaging", "kegged")),
            "brewer": str(entry.get("brewer", "")).strip(),
            "brewery": str(entry.get("brewery", "")).strip(),
            "abv": str(entry.get("abv", "")).strip(),
            "ibu": str(entry.get("ibu", "")).strip(),
            "brewed_on": str(entry.get("brewed_on", "")).strip(),
            "packaged_on": str(entry.get("packaged_on", "")).strip(),
            "best_by_date": str(entry.get("best_by_date", "")).strip(),
            "availability_status": str(entry.get("availability_status", "available")).strip() or "available",
            "description": str(entry.get("description", "")).strip(),
            "allergens": _normalize_beer_allergens(entry.get("allergens", [])),
            "color_srm": str(entry.get("color_srm", "")).strip(),
            "color_ebc": str(entry.get("color_ebc", "")).strip(),
            "serving_temperature": str(entry.get("serving_temperature", "")).strip(),
            "glassware": str(entry.get("glassware", "")).strip(),
            "supplier": str(entry.get("supplier", "")).strip(),
            "distributor": str(entry.get("distributor", "")).strip(),
            "sku": str(entry.get("sku", "")).strip(),
            "upc": str(entry.get("upc", "")).strip(),
            "recipe_url": str(entry.get("recipe_url", "")).strip(),
            "notes": str(entry.get("notes", "")).strip(),
            "brewfather_recipe_id": str(entry.get("brewfather_recipe_id", "")).strip(),
            "brewfather_batch_id": str(entry.get("brewfather_batch_id", "")).strip(),
            "brewfather_last_synced_at": str(entry.get("brewfather_last_synced_at", "")).strip(),
            "brewfather_source_snapshot": entry.get("brewfather_source_snapshot", {}) if isinstance(entry.get("brewfather_source_snapshot", {}), dict) else {},
            "brewfather_conflict": str(entry.get("brewfather_conflict", "")).strip() or None,
            "brewfather_batch_status": str(entry.get("brewfather_batch_status", "")).strip(),
            "brewfather_measured_og": str(entry.get("brewfather_measured_og", "")).strip(),
            "brewfather_measured_fg": str(entry.get("brewfather_measured_fg", "")).strip(),
            "brewfather_carbonation": str(entry.get("brewfather_carbonation", "")).strip(),
            "brewfather_latest_gravity": str(entry.get("brewfather_latest_gravity", "")).strip(),
            "brewfather_latest_temperature": str(entry.get("brewfather_latest_temperature", "")).strip(),
            "brewfather_packaging_volume": str(entry.get("brewfather_packaging_volume", "")).strip(),
            "brewfather_packaging_unit": str(entry.get("brewfather_packaging_unit", "")).strip(),
            "updated_at": entry.get("updated_at") or datetime.now(timezone.utc).isoformat(),
        })

    return normalized


def _normalize_beer_packaging(value) -> str:
    packaging = str(value or "kegged").strip().lower().replace("/", "_")
    if packaging in ("bottled", "bottle", "can", "canned", "bottled_can"):
        return "bottled_can"
    return "kegged"


def _normalize_beer_allergens(value) -> list[str]:
    values = value.split(",") if isinstance(value, str) else value
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        str(item).strip() for item in values if str(item).strip()
    ))


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
    keg["brewery"] = keg.get("beer_brewery") or keg.get("beer_brewer", "")
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
    if not isinstance(unit, str):
        return ""
    normalized = unit.strip().lower()
    if not normalized:
        return ""
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


def _convert_volume(amount: float, from_unit: str | None, to_unit: str):
    source = _normalize_volume_unit(from_unit)
    target = _normalize_volume_unit(to_unit)
    if source == target:
        return amount

    if not source or not target:
        return None

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
        if not updated_keg.get("kicked_date"):
            updated_keg["kicked_date"] = _today_utc_date()


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
    keg["cleaned_date"] = _today_utc_date()


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
        "release_highlights_date": RELEASE_HIGHLIGHTS_DATE,
        "license_portal_url": LICENSE_PORTAL_URL,
        "ingress": _effective_ingress_path(),
        "local_display_url": _external_display_url(),
        "current_user_name": str(session.get("user_name", "") or "").strip(),
        "current_user_role": _normalize_team_role(session.get("user_role")),
        "current_user_release_seen_version": _current_user_release_seen_version(),
        "station_registered": bool(_registered_station(load_data())),
    }


@app.route("/login", methods=["GET", "POST"])
def login_view():
    data = load_data()
    all_team_users = data.get("team_users", [])
    if not isinstance(all_team_users, list):
        all_team_users = []
    active_team_users = [
        user for user in all_team_users
        if not _coerce_bool(user.get("disabled"), False)
    ]
    scan_error = str(request.args.get("scan_error", "") or "").strip().lower()
    error = {
        "invalid": "That QR/NFC credential is invalid or revoked. Use manual sign-in or ask an owner/manager for a new badge.",
        "disabled": "This user account is disabled. Use manual sign-in with an active account.",
    }.get(scan_error)
    scan_token = str(request.values.get("scan_token", "") or "").strip()

    if request.method == "POST":
        user_id = str(request.form.get("user_id", "") or "").strip()
        station_mode = str(request.form.get("station_mode", "") or "").strip().lower() in ("1", "true", "on")
        matched_user = _find_scan_user(data, scan_token) if scan_token else _find_team_user_by_identifier(all_team_users, user_id)
        if matched_user is not None:
            user_id = str(matched_user.get("id", "") or user_id).strip()
        if matched_user is None:
            error = "This scan credential is invalid or revoked." if scan_token else "User not found. Choose a valid team member."
            _record_team_audit(
                data,
                {"id": "anonymous", "name": "Anonymous", "role": "staff"},
                "login_failed",
                user_id or "unknown",
                {"method": "qr_nfc" if scan_token else "manual"},
            )
            save_data(data)
        else:
            selected_role = _normalize_team_role(matched_user.get("role", "staff"))
            if _coerce_bool(matched_user.get("disabled"), False):
                error = "This user account is disabled."
                _record_team_audit(
                    data,
                    matched_user,
                    "login_failed",
                    user_id,
                    {"reason": "disabled", "method": "qr_nfc" if scan_token else "manual"},
                )
                save_data(data)
                return render_template(
                    "login.html",
                    settings=data["settings"],
                    users=active_team_users,
                    error=error,
                    selected_user_id=user_id,
                    require_owner_pin=False,
                    ingress=_effective_ingress_path(),
                )

            owner_pin_recovery_required = False
            owner_pin_required = False
            if selected_role == "owner" and len(active_team_users) > 1:
                expected_pin = _normalize_owner_pin(data.get("settings", {}).get("owner_pin", ""))
                if expected_pin:
                    owner_pin_required = True
                    supplied_pin = str(request.form.get("user_pin", "") or "").strip()
                    if not secrets.compare_digest(expected_pin, supplied_pin):
                        error = "PIN required for the owner account when additional team members are configured."
                        return render_template(
                            "login.html",
                            settings=data["settings"],
                            users=active_team_users,
                            error=error,
                            selected_user_id=user_id,
                            require_owner_pin=True,
                            scan_token=scan_token,
                            ingress=_effective_ingress_path(),
                        )
                else:
                    owner_pin_recovery_required = True

            expected_user_pin = _normalize_team_user_pin(matched_user.get("pin", ""))
            if expected_user_pin:
                supplied_user_pin = str(request.form.get("user_pin", "") or "").strip()
                if not secrets.compare_digest(expected_user_pin, supplied_user_pin):
                    error = "PIN required for this team member."
                    return render_template(
                        "login.html",
                        settings=data["settings"],
                        users=active_team_users,
                        error=error,
                        selected_user_id=user_id,
                        require_owner_pin=owner_pin_required,
                        scan_token=scan_token,
                        ingress=_effective_ingress_path(),
                    )

            session.clear()
            session.permanent = True
            session["user_id"] = str(matched_user.get("id", "")).strip() or user_id
            session["user_role"] = selected_role
            session["user_name"] = str(matched_user.get("name", session["user_id"]))
            session["last_activity_at"] = time.time()
            session_id = _create_user_session(
                data,
                matched_user,
                "manual" if not scan_token else "qr_nfc",
                station_mode=station_mode,
            )
            registered_station = _registered_station(data)
            if registered_station and station_mode:
                registered_station["last_used_at"] = _session_now_iso()
            _record_team_audit(data, matched_user, "login", session_id, {
                "method": "manual" if not scan_token else "qr_nfc",
                "session_type": "station" if station_mode else "auto",
                "device_type": "mobile" if _is_mobile_user_agent(request.headers.get("User-Agent", "")) else "desktop",
            })
            if owner_pin_recovery_required:
                session["owner_pin_recovery_required"] = True
            if scan_token:
                _record_team_audit(data, matched_user, "scan_login", str(matched_user.get("id", "")), {"transport": "qr_or_nfc"})
            save_data(data)
            if owner_pin_recovery_required:
                return _redirect_to_endpoint("team_access")
            return _redirect_to_endpoint("index")

    return render_template(
        "login.html",
        settings=data["settings"],
        users=active_team_users,
        error=error,
        selected_user_id=str(request.form.get("user_id", "") or "").strip() if request.method == "POST" else "",
        require_owner_pin=False,
        scan_token=scan_token,
        ingress=_effective_ingress_path(),
    )


@app.route("/auth/scan/<token>", methods=["GET"])
def scan_login(token: str):
    data = load_data()
    user = _find_scan_user(data, token)
    if user is None:
        _record_team_audit(data, {"id": "anonymous", "name": "Anonymous", "role": "staff"}, "scan_login_failed", "scan", {"reason": "invalid_or_revoked_credential"})
        save_data(data)
        return redirect(url_for("login_view", scan_error="invalid"))
    if _coerce_bool(user.get("disabled"), False):
        _record_team_audit(data, {"id": user.get("id", ""), "name": user.get("name", ""), "role": user.get("role", "staff")}, "scan_login_failed", str(user.get("id", "")), {"reason": "disabled_user"})
        save_data(data)
        return redirect(url_for("login_view", scan_error="disabled"))

    active_users = [item for item in data.get("team_users", []) if not _coerce_bool(item.get("disabled"), False)]
    owner_pin_required = (
        str(user.get("role", "")).lower() == "owner"
        and len(active_users) > 1
        and bool(_normalize_owner_pin(data.get("settings", {}).get("owner_pin", "")))
    )
    if _scan_requires_pin(user) or owner_pin_required:
        return redirect(url_for("login_view", scan_token=token))

    session.clear()
    session.permanent = True
    session["user_id"] = str(user.get("id", "")).strip()
    session["user_role"] = _normalize_team_role(user.get("role", "staff"))
    session["user_name"] = str(user.get("name", session["user_id"]))
    session["last_activity_at"] = time.time()
    registered_station = _registered_station(data)
    session_id = _create_user_session(
        data,
        user,
        "qr_nfc",
        station_mode=bool(registered_station),
    )
    if registered_station:
        registered_station["last_used_at"] = _session_now_iso()
    _record_team_audit(data, user, "login", session_id, {
        "method": "qr_nfc",
        "device_type": "mobile" if _is_mobile_user_agent(request.headers.get("User-Agent", "")) else "desktop",
    })
    if str(user.get("role", "")).lower() == "owner" and len(active_users) > 1 and not _normalize_owner_pin(data.get("settings", {}).get("owner_pin", "")):
        session["owner_pin_recovery_required"] = True
        _record_team_audit(data, user, "scan_login", session["user_id"], {"transport": "qr_or_nfc", "owner_recovery": True})
        save_data(data)
        return _redirect_to_endpoint("team_access")
    _record_team_audit(data, user, "scan_login", session["user_id"], {"transport": "qr_or_nfc"})
    save_data(data)
    return _redirect_to_endpoint("index")


@app.route("/logout")
def logout_view():
    data = load_data()
    session_id = str(session.get("session_id", "") or "").strip()
    record = _find_user_session(data, session_id) if session_id else None
    if record and not record.get("revoked_at"):
        record["revoked_at"] = _session_now_iso()
        _record_team_audit(data, _get_current_team_user(), "logout", session_id, {})
        save_data(data)
    session.clear()
    return _redirect_to_endpoint("login_view")


@app.route("/api/mobile/login", methods=["POST"])
def mobile_login():
    data = load_data()
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("user_id", "") or "").strip()
    pin = str(payload.get("pin", "") or "").strip()
    user = _find_team_user_by_identifier(data.get("team_users", []), identifier)
    if user is None or _coerce_bool(user.get("disabled"), False):
        return jsonify({"error": "Invalid mobile login."}), 401

    expected_pin = _normalize_team_user_pin(user.get("pin", ""))
    if str(user.get("role", "")).lower() == "owner" and len(data.get("team_users", [])) > 1:
        expected_pin = _normalize_owner_pin(data.get("settings", {}).get("owner_pin", "")) or expected_pin
    if not expected_pin or not secrets.compare_digest(expected_pin, pin):
        return jsonify({"error": "Invalid mobile login."}), 401

    raw_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    data.setdefault("mobile_tokens", []).append({
        "token_hash": _mobile_token_hash(raw_token),
        "user_id": str(user.get("id", "")),
        "user_name": str(user.get("name", "")),
        "user_role": _normalize_team_role(user.get("role", "staff")),
        "created_at": _session_now_iso(),
        "expires_at": expires_at.isoformat(),
        "revoked_at": "",
    })
    _record_team_audit(data, user, "mobile_login", str(user.get("id", "")), {})
    save_data(data)
    return jsonify({"token": raw_token, "expires_at": expires_at.isoformat(), "user": {
        "id": user.get("id", ""),
        "name": user.get("name", ""),
        "role": _normalize_team_role(user.get("role", "staff")),
    }})


# ---------------------------------------------------------------------------
# Routes – pages
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    data = load_data()
    ingress_path = _effective_ingress_path()
    return render_template(
        "index.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
        on_deck_kegs=_build_on_deck_kegs(data),
        dashboard_analytics=_build_dashboard_analytics(data),
        ingress=ingress_path,
    )


@app.route("/analytics")
def analytics_view():
    data = load_data()
    if not _analytics_enabled(data):
        return _redirect_to_endpoint("index")
    ingress_path = _effective_ingress_path()
    return render_template(
        "analytics.html",
        settings=data["settings"],
        dashboard_analytics=_build_dashboard_analytics(data),
        ingress=ingress_path,
    )


@app.route("/audit")
def audit_view():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "audit_view"):
        return jsonify({"error": "Insufficient permissions"}), 403

    return render_template(
        "audit.html",
        settings=data["settings"],
        audit_events=data.get("team_audit", []),
        ingress=_effective_ingress_path(),
    )


@app.route("/stock")
def stock():
    data = load_data()
    if not _bar_stock_enabled(data):
        return _redirect_to_endpoint("index")
    ingress_path = _effective_ingress_path()
    return render_template(
        "stock.html",
        settings=data["settings"],
        bar_stock=data["bar_stock"],
        ingress=ingress_path,
    )


@app.route("/kegs")
def kegs():
    data = load_data()
    ingress_path = _effective_ingress_path()
    coupler_choices = sorted(
        {
            str(keg.get("coupler_type", "")).strip()
            for keg in data.get("kegs", [])
            if str(keg.get("coupler_type", "")).strip()
        } | set(STANDARD_COUPLER_TYPES),
        key=lambda v: v.lower(),
    )
    ownership_choices = sorted(
        {
            str(keg.get("ownership_type", "")).strip()
            for keg in data.get("kegs", [])
            if str(keg.get("ownership_type", "")).strip()
        } | set(STANDARD_OWNERSHIP_TYPES),
        key=lambda v: v.lower(),
    )
    location_choices = sorted(
        {
            str(keg.get("location", "")).strip()
            for keg in data.get("kegs", [])
            if str(keg.get("location", "")).strip()
        },
        key=lambda v: v.lower(),
    )
    gas_choices = sorted(
        {
            str(keg.get("gas_type", "")).strip()
            for keg in data.get("kegs", [])
            if str(keg.get("gas_type", "")).strip()
        } | set(STANDARD_GAS_TYPES),
        key=lambda v: v.lower(),
    )
    return render_template(
        "kegs.html",
        settings=data["settings"],
        kegs=data["kegs"],
        beers=sorted(data.get("beers", []), key=lambda beer: str(beer.get("name", "")).lower()),
        coupler_choices=coupler_choices,
        ownership_choices=ownership_choices,
        location_choices=location_choices,
        gas_choices=gas_choices,
        ingress=ingress_path,
    )


@app.route("/beers")
def beers():
    data = load_data()
    ingress_path = _effective_ingress_path()
    beer_type_choices = sorted(
        {
            str(beer.get("type", "")).strip()
            for beer in data.get("beers", [])
            if str(beer.get("type", "")).strip()
        },
        key=lambda value: value.lower(),
    )
    supplier_choices = sorted(
        {
            str(beer.get("supplier", "")).strip()
            for beer in data.get("beers", [])
            if str(beer.get("supplier", "")).strip()
        },
        key=lambda value: value.lower(),
    )
    distributor_choices = sorted(
        {
            str(beer.get("distributor", "")).strip()
            for beer in data.get("beers", [])
            if str(beer.get("distributor", "")).strip()
        },
        key=lambda value: value.lower(),
    )
    allergen_choices = STANDARD_BEER_ALLERGENS.copy()
    for beer in data.get("beers", []):
        for allergen in beer.get("allergens", []):
            cleaned = str(allergen).strip()
            if cleaned and not any(cleaned.lower() == choice.lower() for choice in allergen_choices):
                allergen_choices.append(cleaned)
    return render_template(
        "beers.html",
        settings=data["settings"],
        beers=sorted(data.get("beers", []), key=lambda beer: str(beer.get("name", "")).lower()),
        beer_type_choices=beer_type_choices,
        supplier_choices=supplier_choices,
        distributor_choices=distributor_choices,
        allergen_choices=allergen_choices,
        ingress=ingress_path,
    )


@app.route("/taps")
def taps():
    data = load_data()
    ingress_path = _effective_ingress_path()
    faucet_choices = sorted(
        {
            str(tap.get("faucet_type", "")).strip()
            for tap in data.get("taps", [])
            if str(tap.get("faucet_type", "")).strip()
        } | set(STANDARD_FAUCET_TYPES),
        key=lambda v: v.lower(),
    )
    diameter_choices = sorted(
        {
            str(tap.get("line_inner_diameter", "")).strip()
            for tap in data.get("taps", [])
            if str(tap.get("line_inner_diameter", "")).strip()
        } | set(STANDARD_LINE_DIAMETERS),
        key=lambda v: v.lower(),
    )
    material_choices = sorted(
        {
            str(tap.get("line_material", "")).strip()
            for tap in data.get("taps", [])
            if str(tap.get("line_material", "")).strip()
        } | set(STANDARD_LINE_MATERIALS),
        key=lambda v: v.lower(),
    )
    location_choices = sorted(
        {
            str(tap.get("location", "")).strip()
            for tap in data.get("taps", [])
            if str(tap.get("location", "")).strip()
        },
        key=lambda v: v.lower(),
    )
    return render_template(
        "taps.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        faucet_choices=faucet_choices,
        diameter_choices=diameter_choices,
        material_choices=material_choices,
        location_choices=location_choices,
        ingress=ingress_path,
    )


@app.route("/settings")
def settings():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    ingress_path = _effective_ingress_path()
    template_settings = json.loads(json.dumps(data["settings"]))
    template_settings["brewfather_api_key"] = ""
    template_settings["brewfather_credentials"] = redact_credentials(_brewfather_credentials(data["settings"]))
    return render_template(
        "settings.html",
        settings=template_settings,
        taps=data.get("taps", []),
        pos_sync_providers=sorted(POS_SYNC_PROVIDERS.keys()),
        pos_sync_provider_catalog=get_pos_provider_catalog(data["settings"]),
        team_users=[_public_team_user(user) for user in data.get("team_users", [])],
        owner_pin_recovery_required=bool(session.get("owner_pin_recovery_required")),
        qr_ready=_qr_is_available(),
        qr_error=QR_IMPORT_ERROR,
        display_port=DISPLAY_PORT,
        external_api_port=EXTERNAL_API_PORT,
        external_api_base_url=_external_api_listener_base_url(),
        cors_allowed_origins=sorted(CORS_ALLOWED_ORIGINS),
        external_display_url=_external_display_url(data),
        external_menu_url=_external_menu_url(data),
        auto_external_display_url=_external_display_url(),
        auto_external_menu_url=_external_menu_url(),
        ingress=ingress_path,
    )


@app.route("/team-access")
def team_access():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403

    ingress_path = _effective_ingress_path()
    return render_template(
        "team_access.html",
        settings=data["settings"],
        team_users=data.get("team_users", []),
        owner_pin_recovery_required=bool(session.get("owner_pin_recovery_required")),
        ingress=ingress_path,
    )


@app.route("/api-reference")
def api_reference():
    data = load_data()
    ingress_path = _effective_ingress_path()
    return render_template(
        "api_reference.html",
        settings=data["settings"],
        endpoints=API_REFERENCE_ENDPOINTS,
        ingress=ingress_path,
    )


@app.route("/display")
def display_view():
    data = load_data()
    ingress_path = _effective_ingress_path()
    qr_image_path = f"{ingress_path}/api/menu/qr" if ingress_path else "/api/menu/qr"
    menu_qr_mode = _normalize_menu_qr_mode(data.get("settings", {}).get("menu_qr_mode"))
    qr_ready = _qr_is_available()

    brewery_type = _normalize_brewery_type(data.get("settings", {}).get("brewery_type"))
    display_count = _normalize_display_count(
        data.get("settings", {}).get("display_count"),
        brewery_type,
    )
    selected_display_index = max(1, min(display_count, _coerce_int(request.args.get("display"), 1) or 1))
    assignments = _normalize_display_tap_assignments(
        data.get("settings", {}).get("display_tap_assignments"),
        display_count,
    )
    bar_stock_assignments = _normalize_display_bar_stock_assignments(
        data.get("settings", {}).get("display_bar_stock_assignments"),
        display_count,
        brewery_type,
    )
    selected_taps = set(assignments[selected_display_index - 1]) if selected_display_index <= len(assignments) else set()

    taps = data["taps"]
    on_deck_kegs = _build_on_deck_kegs(data)
    show_taps = True
    show_on_deck = True
    show_bar_stock = _coerce_bool(data.get("settings", {}).get("bar_stock_enabled"), True)

    if brewery_type == "homebrewer" and display_count > 1:
        if selected_display_index == 1:
            show_taps = True
            show_on_deck = True
            show_bar_stock = False
        elif selected_display_index == 2:
            show_taps = False
            show_on_deck = False
            show_bar_stock = True
    elif brewery_type == "pro" and display_count > 1:
        if selected_taps:
            taps = [tap for tap in data["taps"] if _coerce_int(tap.get("number"), None) in selected_taps]
        else:
            taps = []
        show_taps = bool(selected_taps)
        show_bar_stock = show_bar_stock and bar_stock_assignments[selected_display_index - 1]

    return render_template(
        "display/index.html",
        settings=data["settings"],
        taps=taps,
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
        on_deck_kegs=on_deck_kegs,
        qr_image_path=qr_image_path,
        menu_qr_mode=menu_qr_mode,
        qr_ready=qr_ready,
        selected_display_index=selected_display_index,
        show_taps=show_taps,
        show_on_deck=show_on_deck,
        show_bar_stock=show_bar_stock,
    )


@app.route("/menu")
def menu_view():
    data = load_data()
    ingress_path = _effective_ingress_path()
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
    qr_image_path = f"{ingress_path}/api/menu/qr" if ingress_path else "/api/menu/qr"
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
        ingress=ingress_path,
    )


@app.route("/menu/qr-print")
def menu_qr_print_view():
    data = load_data()
    ingress_path = _effective_ingress_path()
    menu_path = _external_menu_url(data)
    qr_image_path = f"{ingress_path}/api/menu/qr" if ingress_path else "/api/menu/qr"
    return render_template(
        "menu_qr_print.html",
        settings=data["settings"],
        menu_path=menu_path,
        qr_image_path=qr_image_path,
        qr_ready=_qr_is_available(),
        qr_error=QR_IMPORT_ERROR,
        ingress=ingress_path,
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
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    return jsonify(_brewfather_settings_response(data["settings"]))


@app.route("/api/licensing/status", methods=["GET"])
def api_licensing_status():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("id") == "anonymous":
        return jsonify({"error": "Authentication required."}), 401
    return jsonify({"app_id": LICENSE_APP_ID, **_license_status(data["settings"])})


@app.route("/api/licensing/activation-request", methods=["POST"])
def api_create_license_activation_request():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can create an activation request."}), 403
    if _normalize_brewery_type(data["settings"].get("brewery_type")) != "pro":
        return jsonify({"error": "Activation requests are available only for Pro profiles."}), 403
    try:
        instance_id, private_key_value = _ensure_license_instance_identity(data)
        public_key = _license_instance_public_key(private_key_value)
        instance_key_id = _license_instance_key_id(public_key)
    except (ValueError, TypeError, binascii.Error) as exc:
        return jsonify({"error": str(exc)}), 503

    request_payload = {
        "schema_version": 1,
        "app_id": LICENSE_APP_ID,
        "request_type": "pro_activation",
        "nonce": _license_b64encode(secrets.token_bytes(32)),
        "instance_id": instance_id,
        "instance_key_id": instance_key_id,
        "bar_name_hash": _license_bar_name_hash(data["settings"].get("bar_name", "")),
        "instance_public_key": {
            "algorithm": "Ed25519",
            "encoding": "base64url",
            "value": public_key,
        },
        "requested_plan": "pro",
        "requested_features": [
            "multiple_displays",
            "pos_mode",
            "pos_sync",
            "extended_session_timeouts",
        ],
        "app_version": APP_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    request_payload["signature"] = {
        "algorithm": "Ed25519",
        "encoding": "base64url",
        "key_id": instance_key_id,
        "value": _license_sign_activation_request(
            private_key_value,
            request_payload["app_id"],
            request_payload["instance_id"],
            request_payload["instance_key_id"],
            request_payload["nonce"],
        ),
    }
    save_data(data)
    response = app.response_class(
        json.dumps(request_payload, indent=2) + "\n",
        mimetype="application/json",
    )
    response.headers["Content-Disposition"] = (
        'attachment; filename="bartender-activation-request.json"'
    )
    return response


@app.route("/api/licensing/trial", methods=["POST"])
def api_start_license_trial():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can start a trial."}), 403
    status = _license_status(data["settings"])
    if status["active"]:
        return jsonify({"error": "A license or trial is already active."}), 409
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=TRIAL_DAYS)
    data["settings"].update({
        "license_type": "trial",
        "license_expires_at": expires_at.isoformat(),
        "trial_started_at": now.isoformat(),
        "trial_expires_at": expires_at.isoformat(),
        "license_features": ["pro"],
    })
    display_assignments_initialized = _assign_existing_taps_to_first_display(data)
    _record_team_audit(data, current_user, "license_trial_started", LICENSE_APP_ID, {
        "expires_at": expires_at.isoformat(),
        "display_assignments_initialized": display_assignments_initialized,
    })
    save_data(data)
    return jsonify(_license_status(data["settings"]))


@app.route("/api/licensing/activate", methods=["POST"])
def api_activate_license():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can activate a license."}), 403
    token = str((request.get_json(silent=True) or {}).get("token", "") or "").strip()
    try:
        payload = _validate_license_token(token)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    local_instance_id = str(data["settings"].get("license_instance_id", "") or "").strip()
    instance_binding = payload.get("instance_binding")
    instance_binding = instance_binding if isinstance(instance_binding, dict) else {}
    token_instance_id = str(
        payload.get("instance_id")
        or instance_binding.get("instance_value", "")
        or ""
    ).strip()
    if token_instance_id and (not local_instance_id or token_instance_id != local_instance_id):
        return jsonify({"error": "License is bound to a different BarTender instance."}), 400
    token_key_id = str(
        instance_binding.get("instance_key_id")
        or payload.get("instance_key_id")
        or ""
    ).strip()
    local_private_key = str(data["settings"].get("license_instance_private_key", "") or "").strip()
    if token_key_id and local_private_key:
        local_public_key = _license_instance_public_key(local_private_key)
        if token_key_id != _license_instance_key_id(local_public_key):
            return jsonify({"error": "License is bound to a different BarTender instance key."}), 400
        token_public_key_hash = str(instance_binding.get("instance_public_key_sha256", "") or "").strip()
        if token_public_key_hash and token_public_key_hash != _license_instance_public_key_sha256(local_public_key):
            return jsonify({"error": "License is bound to a different BarTender instance public key."}), 400
    activated_license_type = (
        "trial" if payload.get("license_type") == "trial" else "paid"
    )
    data["settings"].update({
        "license_type": activated_license_type,
        "license_token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "license_expires_at": str(payload.get("expires_at", "")),
        "license_features": payload.get("features", []) if isinstance(payload.get("features", []), list) else [],
    })
    display_assignments_initialized = _assign_existing_taps_to_first_display(data)
    _record_team_audit(data, current_user, "license_activated", LICENSE_APP_ID, {
        "expires_at": data["settings"]["license_expires_at"],
        "display_assignments_initialized": display_assignments_initialized,
    })
    save_data(data)
    return jsonify(_license_status(data["settings"]))


@app.route("/api/licensing/clear", methods=["POST"])
def api_clear_license():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can clear licensing state."}), 403
    data["settings"].update({
        "license_type": "",
        "license_token": "",
        "license_token_hash": "",
        "license_expires_at": "",
        "license_features": [],
    })
    _record_team_audit(data, current_user, "license_cleared", LICENSE_APP_ID, {})
    save_data(data)
    return jsonify(_license_status(data["settings"]))


@app.route("/api/user/release-seen", methods=["POST"])
def api_mark_release_seen():
    current_user = _get_current_team_user()
    user_id = str(current_user.get("id", "") or "").strip()
    body = request.get_json(silent=True) or {}
    release_date = str(body.get("date", "") or "").strip()[:64]
    if not user_id or not release_date:
        return jsonify({"error": "User and release date are required."}), 400

    data = load_data()
    user = next(
        (
            item for item in data.get("team_users", [])
            if str(item.get("id", "")).strip().lower() == user_id.lower()
        ),
        None,
    )
    if user is None:
        return jsonify({"error": "User not found."}), 404

    user["release_seen_version"] = release_date
    save_data(data)
    return jsonify({"ok": True, "date": release_date})


@app.route("/api/storage/status", methods=["GET"])
def api_storage_status():
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    backend = str(os.environ.get("STORAGE_BACKEND", "internal") or "internal").strip().lower()
    if backend in ("internal", "sqlite"):
        return jsonify({
            "backend": "internal",
            "engine": "SQLite",
            "location": str(DATA_FILE.with_name("bartender.db")),
            "configured": True,
        })

    database_url = str(os.environ.get("DATABASE_URL", "") or "").strip()
    parsed = urlsplit(database_url)
    return jsonify({
        "backend": backend,
        "engine": "PostgreSQL" if backend in ("postgres", "postgresql") else "MariaDB",
        "host": parsed.hostname or "",
        "port": parsed.port or (5432 if backend in ("postgres", "postgresql") else 3306),
        "database": parsed.path.lstrip("/") if parsed.path else "",
        "configured": bool(database_url),
    })


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = load_data()
    current_user = _get_current_team_user()
    body = request.get_json(force=True)
    is_setup_bootstrap = (
        not session.get("user_id")
        and _coerce_bool(body.get("setup_completed"), False)
    )
    if not is_setup_bootstrap and not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    restricted_owner_only_keys = {
        "bar_name",
        "external_api_token",
        "external_api_read_token",
        "external_api_write_token",
        "owner_pin",
        "pos_sync_credentials",
        "pos_sync_provider_config_json",
        "brewfather_user_id",
        "brewfather_api_key",
        "audit_retention_days",
        "mobile_session_timeout_minutes",
        "station_session_timeout_minutes",
        "anonymous_telemetry_enabled",
    }
    if current_user.get("role") != "owner" and not is_setup_bootstrap:
        restricted_keys_found = [key for key in restricted_owner_only_keys if key in body]
        if restricted_keys_found:
            return jsonify({
                "error": "Insufficient permissions",
                "restricted_fields": restricted_keys_found,
            }), 403

    previous_settings = json.loads(json.dumps(data["settings"]))
    allowed = {
        "measurement",
        "theme",
        "bar_name",
        "brewery_type",
        "pos_system",
        "pos_sync_enabled",
        "pos_sync_provider",
        "pos_sync_credentials",
        "pos_sync_provider_config_json",
        "brewfather_enabled",
        "brewfather_user_id",
        "brewfather_api_key",
        "bar_logo_url",
        "external_base_url",
        "external_api_token_auth_enabled",
        "external_api_token",
        "external_api_read_token",
        "external_api_write_token",
        "owner_pin",
        "external_api_allowlist_enabled",
        "external_api_allowlist",
        "external_api_rate_limit_enabled",
        "external_api_rate_limit_per_minute",
        "audit_retention_days",
        "mobile_session_timeout_minutes",
        "station_session_timeout_minutes",
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
        "display_title_on_tap",
        "display_full_width",
        "display_count",
        "display_tap_assignments",
        "display_bar_stock_assignments",
        "pour_options",
        "default_pour_preset",
        "analytics_low_keg_threshold_percent",
        "analytics_days_left_method",
        "analytics_days_left_window_days",
        "anonymous_telemetry_enabled",
    }
    for key in allowed:
        if key in body:
            data["settings"][key] = body[key]

    # Backward compatibility for older clients posting manage_button_position.
    if "manage_button_position" in body and "dashboard_manage_button_position" not in body:
        data["settings"]["dashboard_manage_button_position"] = body.get("manage_button_position")

    _normalize_settings_in_place(data, setup_completed_explicit="setup_completed" in body)
    try:
        validate_pos_sync_runtime_configuration(
            data.get("settings", {}),
            for_sync_now=False,
        )
    except PosSyncError as exc:
        return jsonify({"error": str(exc), "hint": exc.hint}), exc.status_code

    if data["settings"]["owner_pin"]:
        session.pop("owner_pin_recovery_required", None)

    changed_fields = sorted(
        key
        for key, value in data["settings"].items()
        if previous_settings.get(key) != value
    )
    if changed_fields:
        _record_team_audit(
            data,
            current_user,
            "settings_updated",
            "settings",
            {"changed_fields": changed_fields},
        )
    save_data(data)
    if (
        "anonymous_telemetry_enabled" in changed_fields
        and data["settings"]["anonymous_telemetry_enabled"]
    ):
        _schedule_anonymous_telemetry_heartbeat()
    return jsonify(_brewfather_settings_response(data["settings"]))


@app.route("/api/pos/sync/status", methods=["GET"])
def api_pos_sync_status():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    return jsonify(get_pos_sync_status(data.get("settings", {})))


@app.route("/api/pos/providers", methods=["GET"])
def api_pos_sync_provider_catalog():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    return jsonify({"providers": get_pos_provider_catalog(data.get("settings", {}))})


@app.route("/api/pos/providers", methods=["POST"])
def api_pos_sync_add_provider():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    body = request.get_json(force=True)
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    try:
        provider = add_or_update_custom_provider(settings, body)
    except PosSyncError as exc:
        return jsonify({"error": str(exc), "hint": exc.hint}), exc.status_code

    _record_team_audit(
        data,
        current_user,
        "pos_provider_saved",
        "pos_sync",
        {
            "provider": provider.get("key", ""),
            "mode": provider.get("mode", "static"),
        },
    )
    save_data(data)
    return jsonify(
        {
            "ok": True,
            "provider": provider,
            "providers": get_pos_provider_catalog(settings),
        }
    )


@app.route("/api/pos/providers/import", methods=["POST"])
def api_pos_sync_import_providers():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    body = request.get_json(force=True)
    settings = data.get("settings", {}) if isinstance(data.get("settings", {}), dict) else {}
    try:
        summary = import_custom_providers(settings, body)
    except PosSyncError as exc:
        return jsonify({"error": str(exc), "hint": exc.hint}), exc.status_code

    _record_team_audit(
        data,
        current_user,
        "pos_provider_imported",
        "pos_sync",
        {
            "added_or_updated": summary.get("added_or_updated", 0),
        },
    )
    save_data(data)
    return jsonify({"ok": True, **summary})


@app.route("/api/pos/sync/now", methods=["POST"])
def api_pos_sync_now():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403

    try:
        status = perform_pos_sync(data)
        _record_team_audit(
            data,
            current_user,
            "pos_sync_run",
            "pos_sync",
            {
                "provider": status.get("provider", ""),
                "last_status": status.get("last_status", ""),
                "last_counts": status.get("last_counts", {}),
            },
        )
        save_data(data)
        return jsonify({"ok": True, "status": status})
    except PosSyncError as exc:
        status = mark_pos_sync_failed(
            data.get("settings", {}),
            str(exc),
            hint=exc.hint,
        )
        _record_team_audit(
            data,
            current_user,
            "pos_sync_failed",
            "pos_sync",
            {
                "error": str(exc),
                "hint": exc.hint,
            },
        )
        save_data(data)
        return jsonify({"ok": False, "error": str(exc), "hint": exc.hint, "status": status}), exc.status_code


def _brewfather_authorized_user() -> dict:
    return _get_current_team_user()


@app.route("/api/brewfather/status", methods=["GET"])
def api_brewfather_status():
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    settings = data["settings"]
    conflicts = data.get("brewfather_conflicts", [])
    return jsonify({
        "enabled": settings["brewfather_enabled"],
        "credentials": redact_credentials(_brewfather_credentials(settings)),
        "last_synced_at": settings["brewfather_last_synced_at"],
        "last_status": settings["brewfather_last_status"],
        "last_error": settings["brewfather_last_error"],
        "last_counts": settings["brewfather_last_counts"],
        "conflicts": len(conflicts) if isinstance(conflicts, list) else 0,
    })


@app.route("/api/brewfather/sync", methods=["POST"])
def api_brewfather_sync():
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    settings = data["settings"]
    credentials = _brewfather_credentials(settings)
    if not credentials_configured(credentials):
        return jsonify({
            "ok": False,
            "error": "Brewfather is not configured.",
            "hint": "Set the owner-only User ID and API key first.",
        }), 400

    _record_team_audit(data, current_user, "brewfather_sync_started", "brewfather", {})
    try:
        client = BrewfatherClient(credentials["user_id"], credentials["api_key"])
        recipes = client.fetch_recipes()
        fetched_batches = client.fetch_batches()
        batches = [batch for batch in fetched_batches if is_importable_batch(batch)]
        records = list(recipes)
        recipe_records = {
            str(item.get("brewfather_recipe_id", "")): index
            for index, item in enumerate(records)
            if item.get("brewfather_recipe_id")
        }
        for batch in batches:
            recipe_index = recipe_records.get(str(batch.get("brewfather_recipe_id", "")))
            if recipe_index is None:
                records.append(batch)
                continue
            merged = dict(records[recipe_index])
            merged.update({key: value for key, value in batch.items() if value not in ("", None)})
            records[recipe_index] = merged
        now = brewfather_now()
        conflicts = data.get("brewfather_conflicts", [])
        conflicts = conflicts if isinstance(conflicts, list) else []
        existing_conflict_ids = {str(item.get("id")) for item in conflicts if isinstance(item, dict)}
        counts = {
            "recipes_received": len(recipes),
            "batches_received": len(batches),
            "beers_created": 0,
            "beers_updated": 0,
            "conflicts": 0,
        }
        imported_ids = []
        for record in records:
            beer, outcome, conflict = reconcile_beer(data["beers"], record, now)
            imported_ids.append(beer.get("id"))
            if outcome == "created":
                counts["beers_created"] += 1
            elif outcome == "updated":
                counts["beers_updated"] += 1
            elif outcome == "conflict" and conflict:
                counts["conflicts"] += 1
                if conflict["id"] not in existing_conflict_ids:
                    conflicts.insert(0, conflict)
                    existing_conflict_ids.add(conflict["id"])

        data["brewfather_conflicts"] = conflicts[:500]
        settings["brewfather_last_synced_at"] = now
        settings["brewfather_last_status"] = "success"
        settings["brewfather_last_error"] = ""
        settings["brewfather_last_counts"] = counts
        _record_team_audit(data, current_user, "brewfather_sync_succeeded", "brewfather", counts)
        save_data(data)
        return jsonify({"ok": True, "counts": counts, "beer_ids": imported_ids, "conflicts": data["brewfather_conflicts"]})
    except BrewfatherError as exc:
        settings["brewfather_last_status"] = "failed"
        settings["brewfather_last_error"] = str(exc)
        _record_team_audit(data, current_user, "brewfather_sync_failed", "brewfather", {"error": str(exc), "hint": exc.hint})
        save_data(data)
        return jsonify({"ok": False, "error": str(exc), "hint": exc.hint}), exc.status_code


@app.route("/api/brewfather/conflicts", methods=["GET"])
def api_brewfather_conflicts():
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    return jsonify({"conflicts": data.get("brewfather_conflicts", [])})


@app.route("/api/brewfather/conflicts/<conflict_id>/resolve", methods=["POST"])
def api_brewfather_resolve_conflict(conflict_id: str):
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    body = request.get_json(force=True)
    resolution = str(body.get("resolution", "")).strip().lower()
    if resolution not in ("brewfather", "local"):
        return jsonify({"error": "Resolution must be 'brewfather' or 'local'."}), 400
    conflicts = data.get("brewfather_conflicts", [])
    conflict = next((item for item in conflicts if str(item.get("id")) == conflict_id), None)
    if not conflict:
        return jsonify({"error": "Conflict not found."}), 404
    beer = _get_beer_by_id(data, _coerce_int(conflict.get("beer_id"), None))
    if not beer:
        return jsonify({"error": "Linked beer not found."}), 404
    for field, values in conflict.get("fields", {}).items():
        if field not in BREWFATHER_MANAGED_FIELDS or not isinstance(values, dict):
            continue
        selected = values.get("brewfather") if resolution == "brewfather" else values.get("local")
        beer[field] = str(selected or "").strip()
        beer.setdefault("brewfather_source_snapshot", {})[field] = beer.get(field, "")
    beer["brewfather_conflict"] = None
    beer["brewfather_last_synced_at"] = brewfather_now()
    data["brewfather_conflicts"] = [item for item in conflicts if str(item.get("id")) != conflict_id]
    _record_team_audit(data, current_user, "brewfather_conflict_resolved", f"beer:{beer['id']}", {"resolution": resolution})
    save_data(data)
    return jsonify({"ok": True, "beer": beer})


@app.route("/api/brewfather/batches/<batch_id>", methods=["GET"])
def api_brewfather_batch_details(batch_id: str):
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    beer = next((item for item in data.get("beers", []) if item.get("brewfather_batch_id") == batch_id), None)
    if not beer:
        return jsonify({"error": "Brewfather batch is not linked to a beer."}), 404
    return jsonify({
        "batch_id": batch_id,
        "recipe_id": beer.get("brewfather_recipe_id", ""),
        "beer": beer,
        "eligible_for_keg_import": str(beer.get("brewfather_batch_status", "")).lower() in {
            "completed", "complete", "conditioning", "conditioned",
        },
    })


@app.route("/api/brewfather/batches/<batch_id>/import-keg", methods=["POST"])
def api_brewfather_import_keg(batch_id: str):
    data = load_data()
    current_user = _brewfather_authorized_user()
    if not _team_can(current_user.get("role", "owner"), "settings"):
        return jsonify({"error": "Insufficient permissions"}), 403
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"error": "Explicit confirmation is required to import a Brewfather batch as a keg."}), 400
    beer = next((item for item in data.get("beers", []) if item.get("brewfather_batch_id") == batch_id), None)
    if not beer:
        return jsonify({"error": "Brewfather batch is not linked to a beer. Run sync first."}), 404
    if not is_importable_batch(beer):
        return jsonify({"error": "Only completed or conditioning Brewfather batches can be imported as kegs."}), 409
    keg_id = _coerce_int(body.get("keg_id"), None)
    now = datetime.now(timezone.utc).isoformat()
    if keg_id is not None:
        keg = next((item for item in data.get("kegs", []) if item.get("id") == keg_id), None)
        if not keg:
            return jsonify({"error": "Keg not found."}), 404
        if keg.get("status") != "empty":
            return jsonify({"error": "Only an empty keg can receive a Brewfather batch."}), 409
        _apply_beer_to_keg(keg, beer)
        keg["status"] = "full"
        keg["filled_date"] = beer.get("packaged_on") or _today_utc_date()
        keg["percent_full"] = 100
        keg["updated_at"] = now
        operation = "filled"
    else:
        keg = {
            "id": _next_id(data["kegs"]),
            "name": str(body.get("name") or beer.get("name") or f"Brewfather {batch_id}").strip(),
            "serial_number": "",
            "beer_id": beer.get("id"),
            "beer_name": beer.get("name", ""),
            "type": str(body.get("type") or data["settings"].get("default_keg_type", "")).strip(),
            "size": str(body.get("size") or data["settings"].get("default_keg_type", "")).strip(),
            "custom_size": "",
            "status": "full",
            "coupler_type": "",
            "ownership_type": "Owned",
            "location": "",
            "serving_psi": "",
            "gas_type": "",
            "line_cleaning_keg": False,
            "on_deck": False,
            "current_volume": _coerce_float(body.get("volume"), None),
            "volume_unit": _normalize_volume_unit(body.get("volume_unit") or _default_volume_unit(data["settings"].get("measurement", "us"))),
            "notes": f"Imported from Brewfather batch {batch_id}",
            "filled_date": beer.get("packaged_on") or _today_utc_date(),
            "tapped_date": "",
            "kicked_date": "",
            "cleaned_date": "",
            "percent_full": 100,
            "created_at": now,
            "updated_at": now,
        }
        _apply_beer_to_keg(keg, beer)
        data["kegs"].append(keg)
        operation = "created"
    _record_team_audit(data, current_user, "brewfather_keg_imported", f"keg:{keg['id']}", {
        "brewfather_batch_id": batch_id,
        "brewfather_recipe_id": beer.get("brewfather_recipe_id", ""),
        "beer_id": beer.get("id"),
        "keg_id": keg.get("id"),
        "operation": operation,
    })
    save_data(data)
    return jsonify({"ok": True, "keg": keg}), 201 if operation == "created" else 200


@app.route("/api/settings/reset", methods=["POST"])
def api_reset_settings_to_defaults():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    previous_settings = data.get("settings", {}) if isinstance(data.get("settings"), dict) else {}
    data["settings"] = _default_settings_snapshot()
    _normalize_settings_in_place(data, setup_completed_explicit=True)

    if data["settings"].get("owner_pin"):
        session.pop("owner_pin_recovery_required", None)
    else:
        session["owner_pin_recovery_needed"] = _owner_pin_recovery_needed(data)

    _record_team_audit(
        data,
        current_user,
        "settings_reset",
        "settings",
        {
            "from_bar_name": str(previous_settings.get("bar_name", "")),
            "to_bar_name": str(data["settings"].get("bar_name", "")),
        },
    )
    save_data(data)
    return jsonify(data["settings"])


@app.route("/api/settings/displays/reset", methods=["POST"])
def api_reset_display_configuration_to_defaults():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403
    if _normalize_brewery_type(data["settings"].get("brewery_type")) != "pro":
        return jsonify({"error": "Display configuration is available only for Pro profiles."}), 409

    configuration = _reset_display_configuration_to_defaults(data)
    _record_team_audit(
        data,
        current_user,
        "display_configuration_reset",
        "settings:displays",
        {"display_count": configuration["display_count"], "display_1_taps": configuration["display_tap_assignments"][0]},
    )
    save_data(data)
    return jsonify({"ok": True, **configuration})


@app.route("/api/analytics/reset", methods=["POST"])
def api_reset_analytics_data():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    previous_count = len(data.get("pour_events", []))
    data["pour_events"] = []
    _record_team_audit(
        data,
        current_user,
        "analytics_reset",
        "analytics",
        {"events_removed": previous_count},
    )
    save_data(data)
    return jsonify({"ok": True, "events_removed": previous_count})


@app.route("/api/reset", methods=["POST"])
def api_factory_reset_all_data():
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    payload = request.get_json(silent=True) or {}
    confirmation = str(payload.get("confirmation", "")).strip()
    if confirmation != "RESET ALL DATA":
        return jsonify({
            "error": "Confirmation phrase required.",
            "required_confirmation": "RESET ALL DATA",
        }), 400

    _remove_uploaded_logos()
    reset_data = _default_data_snapshot()
    save_data(reset_data)

    session.pop("owner_pin_recovery_required", None)
    session["user_id"] = "owner"
    session["user_role"] = "owner"
    session["user_name"] = "Owner"

    return jsonify({
        "ok": True,
        "message": "All BarTender data has been reset to factory defaults.",
    })


@app.route("/api/team/users", methods=["GET"])
def api_get_team_users():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_view"):
        return jsonify({"error": "Insufficient permissions"}), 403

    users = data.get("team_users", [])
    if current_user.get("role") == "staff":
        users = [user for user in users if user.get("id") == current_user.get("id")]
    return jsonify({"users": [_public_team_user(user) for user in users]})


@app.route("/api/team/sessions", methods=["GET", "POST"])
def api_team_sessions():
    data = load_data()
    current_user = _get_current_team_user()
    current_user_id = str(current_user.get("id", "")).strip().lower()
    if request.method == "GET":
        requested_user_id = str(request.args.get("user_id", "") or "").strip()
        if requested_user_id and requested_user_id.lower() != current_user_id:
            if not _team_can(current_user.get("role", "staff"), "team_manage"):
                return jsonify({"error": "Insufficient permissions"}), 403
        target_user_id = requested_user_id or str(current_user.get("id", "")).strip()
        sessions = []
        for record in data.get("user_sessions", []):
            if str(record.get("user_id", "")).strip().lower() != target_user_id.lower():
                continue
            if record.get("revoked_at"):
                continue
            sessions.append({
                "id": record.get("id", ""),
                "user_id": record.get("user_id", ""),
                "user_name": record.get("user_name", ""),
                "login_method": record.get("login_method", ""),
                "device_type": record.get("device_type", "desktop"),
                "user_agent": record.get("user_agent", ""),
                "ip_address": record.get("ip_address", ""),
                "created_at": record.get("created_at", ""),
                "last_activity_at": record.get("last_activity_at", ""),
                "expires_at": record.get("expires_at", ""),
                "is_current": record.get("id") == session.get("session_id"),
            })
        return jsonify({"sessions": sessions})

    if not _team_can(current_user.get("role", "staff"), "team_view"):
        return jsonify({"error": "Insufficient permissions"}), 403
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    session_id = str(payload.get("session_id", "")).strip()
    if action != "revoke" or not session_id:
        return jsonify({"error": "A session ID is required."}), 400
    record = _find_user_session(data, session_id)
    if record is None or record.get("revoked_at"):
        return jsonify({"error": "Session not found."}), 404
    if (
        str(record.get("user_id", "")).strip().lower() != current_user_id
        and current_user.get("role") not in ("owner", "manager")
    ):
        return jsonify({"error": "Insufficient permissions"}), 403
    record["revoked_at"] = _session_now_iso()
    _record_team_audit(data, current_user, "session_revoked", session_id, {
        "user_id": record.get("user_id", ""),
        "device_type": record.get("device_type", "desktop"),
    })
    save_data(data)
    return jsonify({"ok": True, "session_id": session_id})


@app.route("/api/team/stations", methods=["GET", "POST"])
def api_team_stations():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "staff"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403

    if request.method == "GET":
        stations = [
            {
                "id": station.get("id", ""),
                "name": station.get("name", ""),
                "created_at": station.get("created_at", ""),
                "last_used_at": station.get("last_used_at", ""),
                "revoked_at": station.get("revoked_at", ""),
            }
            for station in data.get("station_registrations", [])
            if not station.get("revoked_at")
        ]
        return jsonify({"stations": stations})

    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "register")).strip().lower()
    if action == "revoke":
        station_id = str(payload.get("station_id", "")).strip()
        station = next(
            (item for item in data.get("station_registrations", []) if item.get("id") == station_id),
            None,
        )
        if station is None or station.get("revoked_at"):
            return jsonify({"error": "Station registration not found."}), 404
        current_station = _registered_station(data)
        station["revoked_at"] = _session_now_iso()
        _record_team_audit(data, current_user, "station_revoked", station_id, {"name": station.get("name", "")})
        save_data(data)
        response = jsonify({"ok": True, "station_id": station_id})
        if current_station and current_station.get("id") == station_id:
            response.delete_cookie("bartender_station_token")
        return response

    name = str(payload.get("name", "")).strip()[:80]
    if not name:
        return jsonify({"error": "Station name is required."}), 400
    token = secrets.token_urlsafe(32)
    station = {
        "id": f"station-{secrets.token_urlsafe(10)}",
        "name": name,
        "token_hash": _station_token_hash(token),
        "created_by": str(current_user.get("id", "")),
        "created_at": _session_now_iso(),
        "last_used_at": "",
        "revoked_at": "",
    }
    data.setdefault("station_registrations", []).append(station)
    _record_team_audit(data, current_user, "station_registered", station["id"], {"name": name})
    save_data(data)
    response = jsonify({
        "ok": True,
        "station": {key: station[key] for key in ("id", "name", "created_at", "last_used_at")},
    })
    response.set_cookie("bartender_station_token", token, max_age=31536000, httponly=True, samesite="Lax")
    return response


@app.route("/api/team/users", methods=["POST"])
def api_create_team_user():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403

    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    if action == "delete":
        return api_delete_team_user()
    if action in ("issue_scan", "rotate_scan", "revoke_scan", "set_scan_policy"):
        return api_manage_scan_credential()
    if action in (
        "update",
        "update_profile",
        "set_pin",
        "update_pin",
        "reset_pin",
        "disable",
        "set_disabled",
    ):
        return api_update_team_user()

    name = str(payload.get("name", "")).strip()
    users = data.setdefault("team_users", [])
    if not isinstance(users, list):
        users = []
        data["team_users"] = users
    has_owner = any(
        isinstance(existing, dict) and str(existing.get("role", "")).strip().lower() == "owner"
        for existing in users
    )
    role = "owner" if not has_owner else _normalize_team_role(payload.get("role"))
    user_pin = _normalize_team_user_pin(payload.get("pin", ""))
    if not name:
        return jsonify({"error": "User name is required."}), 400

    if not has_owner:
        owner_placeholder = next(
            (user for user in users if str(user.get("id", "")).strip().lower() == "owner"),
            None,
        )
        if owner_placeholder is not None:
            owner_placeholder["name"] = name
            owner_placeholder["role"] = "owner"
            owner_placeholder["pin"] = user_pin
            owner_placeholder["disabled"] = False
            owner_placeholder["created_at"] = datetime.now(timezone.utc).isoformat()
            user = owner_placeholder
            user_id = "owner"
        else:
            user_id = "owner"
            user = {
                "id": user_id,
                "name": name,
                "role": "owner",
                "pin": user_pin,
                "disabled": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            users.insert(0, user)
        _record_team_audit(data, current_user, "user_created", user_id, {"role": role, "name": name})
        save_data(data)
        return jsonify({"user": _public_team_user(user)})

    user_id = str(payload.get("id") or payload.get("user_id") or f"user-{abs(hash(name)) % 1000000}").strip()
    if any(str(existing.get("id", "")).lower() == user_id.lower() for existing in users):
        return jsonify({"error": "A user with that ID already exists."}), 409

    user = {
        "id": user_id,
        "name": name,
        "role": role,
        "pin": user_pin,
        "disabled": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scan_token_hash": "",
        "scan_issued_at": "",
        "scan_require_pin": False,
    }
    users.append(user)
    _record_team_audit(data, current_user, "user_created", user_id, {"role": role, "name": name})
    save_data(data)
    return jsonify({"user": _public_team_user(user)})


def api_manage_scan_credential():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", "")).strip().lower()
    user_id = str(payload.get("user_id") or payload.get("id") or "").strip()
    user = next(
        (item for item in data.get("team_users", []) if str(item.get("id", "")).strip().lower() == user_id.lower()),
        None,
    )
    if user is None:
        return jsonify({"error": "User not found."}), 404
    if action == "set_scan_policy":
        user["scan_require_pin"] = _coerce_bool(payload.get("require_pin"), False)
        _record_team_audit(data, current_user, "user_scan_policy_updated", user_id, {"require_pin": user["scan_require_pin"]})
        save_data(data)
        return jsonify({"user": _public_team_user(user)})
    if action == "revoke_scan":
        user["scan_token_hash"] = ""
        user["scan_issued_at"] = ""
        _record_team_audit(data, current_user, "user_scan_credential_revoked", user_id, {})
        save_data(data)
        return jsonify({"user": _public_team_user(user), "revoked": True})
    if action not in ("issue_scan", "rotate_scan"):
        return jsonify({"error": "Unsupported scan credential action."}), 400

    token = secrets.token_urlsafe(32)
    user["scan_token_hash"] = _scan_token_hash(token)
    user["scan_issued_at"] = datetime.now(timezone.utc).isoformat()
    user["scan_require_pin"] = _coerce_bool(payload.get("require_pin"), user.get("scan_require_pin", False))
    _record_team_audit(data, current_user, "user_scan_credential_issued", user_id, {"rotated": action == "rotate_scan"})
    save_data(data)
    login_url = _scan_login_url(token)
    return jsonify({
        "user": _public_team_user(user),
        "login_url": login_url,
        "qr_data_url": _qr_png_data_url(login_url),
        "nfc_payload": login_url,
    })


@app.route("/api/team/users/update", methods=["POST"])
def api_update_team_user():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403

    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "update").strip().lower()
    user_id = str(payload.get("user_id") or payload.get("id") or "").strip()
    if not user_id:
        return jsonify({"error": "User ID is required."}), 400

    requested_role = _normalize_team_role(payload.get("role"))
    users = data.get("team_users", [])
    if not isinstance(users, list):
        return jsonify({"error": "Team users data is invalid."}), 400

    matching_user = next(
        (user for user in users if str(user.get("id", "")).strip().lower() == user_id.lower()),
        None,
    )
    if str(current_user.get("id", "")).strip().lower() == user_id.lower() and current_user.get("role") == "manager":
        return jsonify({"error": "Managers cannot change their own role. Another manager or the owner must do this."}), 403

    if matching_user is None:
        return jsonify({"error": "User not found."}), 404

    if str(matching_user.get("role", "")).strip().lower() == "owner" and current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can change the owner account."}), 403

    if current_user.get("role") == "manager" and requested_role == "owner":
        return jsonify({"error": "Only the owner can promote someone to owner."}), 403

    if current_user.get("role") == "manager" and str(matching_user.get("role", "")).strip().lower() == "owner":
        return jsonify({"error": "Only the owner can change the owner account."}), 403

    if action == "update_profile":
        new_name = str(payload.get("name", "")).strip()
        if not new_name:
            return jsonify({"error": "User name is required."}), 400
        if str(matching_user.get("role", "")).strip().lower() == "owner" and current_user.get("role") != "owner":
            return jsonify({"error": "Only the owner can update the owner profile."}), 403
        previous_name = str(matching_user.get("name", "")).strip()
        matching_user["name"] = new_name
        if str(current_user.get("id", "")).strip().lower() == user_id.lower():
            session["user_name"] = new_name
        _record_team_audit(
            data,
            current_user,
            "user_profile_updated",
            user_id,
            {"from_name": previous_name, "to_name": new_name},
        )
        save_data(data)
        return jsonify({"user": _public_team_user(matching_user)})

    if action in ("set_pin", "update_pin"):
        if str(current_user.get("id", "")).strip().lower() == user_id.lower() and current_user.get("role") == "manager":
            return jsonify({"error": "Managers cannot change their own PIN. Another manager or the owner must do this."}), 403
        matching_user["pin"] = _normalize_team_user_pin(payload.get("pin", ""))
        _record_team_audit(
            data,
            current_user,
            "user_pin_updated",
            user_id,
            {"pin_set": bool(matching_user.get("pin"))},
        )
        save_data(data)
        return jsonify({"user": _public_team_user(matching_user)})

    if action == "reset_pin":
        if str(current_user.get("id", "")).strip().lower() == user_id.lower() and current_user.get("role") == "manager":
            return jsonify({"error": "Managers cannot reset their own PIN. Another manager or the owner must do this."}), 403
        matching_user["pin"] = ""
        _record_team_audit(
            data,
            current_user,
            "user_pin_reset",
            user_id,
            {},
        )
        save_data(data)
        return jsonify({"user": _public_team_user(matching_user)})

    if action in ("disable", "set_disabled"):
        disable_value = _coerce_bool(payload.get("disabled"), True)
        if str(current_user.get("id", "")).strip().lower() == user_id.lower() and disable_value:
            return jsonify({"error": "You cannot disable the currently signed-in user."}), 400
        if str(matching_user.get("role", "")).strip().lower() == "owner" and disable_value:
            return jsonify({"error": "Owner account cannot be disabled."}), 400
        matching_user["disabled"] = disable_value
        _record_team_audit(
            data,
            current_user,
            "user_disabled_updated",
            user_id,
            {"disabled": disable_value},
        )
        save_data(data)
        return jsonify({"user": _public_team_user(matching_user)})

    if requested_role == "owner" and current_user.get("role") != "owner":
        return jsonify({"error": "Only the owner can promote someone to owner."}), 403

    previous_role = str(matching_user.get("role", "staff")).strip().lower()
    matching_user["role"] = requested_role
    _record_team_audit(
        data,
        current_user,
        "user_role_updated",
        user_id,
        {"from_role": previous_role, "to_role": requested_role},
    )
    save_data(data)
    return jsonify({"user": _public_team_user(matching_user)})


@app.route("/api/team/users", methods=["DELETE"])
def api_delete_team_user_http_forbidden():
    return jsonify({"error": "Team member deletion is only supported from the UI."}), 405


@app.route("/api/team/users/delete", methods=["POST"])
def api_delete_team_user():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "team_manage"):
        return jsonify({"error": "Insufficient permissions"}), 403

    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id") or payload.get("id") or "").strip()
    if not user_id:
        return jsonify({"error": "User ID is required."}), 400

    users = data.get("team_users", [])
    if not isinstance(users, list):
        return jsonify({"error": "Team users data is invalid."}), 400

    remaining = [user for user in users if str(user.get("id", "")).strip() != user_id]
    if len(remaining) == len(users):
        return jsonify({"error": "User not found."}), 404

    if str(current_user.get("id", "")).lower() == user_id.lower():
        return jsonify({"error": "You cannot delete the currently signed-in user."}), 400

    matching_user = next(
        (user for user in users if str(user.get("id", "")).strip().lower() == user_id.lower()),
        None,
    )
    if matching_user and str(matching_user.get("role", "")).strip().lower() == "owner":
        return jsonify({"error": "Owner account cannot be deleted."}), 400

    data["team_users"] = remaining
    _record_team_audit(data, current_user, "user_deleted", user_id, {"deleted_user_id": user_id})
    save_data(data)
    return jsonify({"deleted": True, "user_id": user_id})


@app.route("/api/team/audit", methods=["GET"])
def api_get_team_audit():
    data = load_data()
    current_user = _get_current_team_user()
    if not _team_can(current_user.get("role", "owner"), "audit_view"):
        return jsonify({"error": "Insufficient permissions"}), 403

    return jsonify({"audit": data.get("team_audit", [])})


@app.route("/api/team/audit/export", methods=["GET"])
def api_export_team_audit():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") not in {"owner", "manager"}:
        return jsonify({"error": "Insufficient permissions"}), 403

    date_stamp = _export_date_stamp()
    return send_file(
        io.BytesIO(json.dumps(data.get("team_audit", []), indent=2).encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"bartender_audit_{date_stamp}.json",
    )


@app.route("/api/team/audit/clear", methods=["POST"])
def api_clear_team_audit():
    data = load_data()
    current_user = _get_current_team_user()
    if current_user.get("role") != "owner":
        return jsonify({"error": "Insufficient permissions"}), 403

    expected_pin = _normalize_owner_pin(data.get("settings", {}).get("owner_pin", ""))
    if not expected_pin:
        return jsonify({"error": "An Owner PIN must be configured before clearing audit events."}), 409

    body = request.get_json(silent=True) or {}
    supplied_pin = str(body.get("owner_pin", "") or "").strip()
    if not secrets.compare_digest(expected_pin, supplied_pin):
        return jsonify({"error": "Invalid Owner PIN."}), 403

    cleared_count = len(data.get("team_audit", []))
    data["team_audit"] = []
    save_data(data)
    return jsonify({"cleared": cleared_count})


@app.route("/api/settings/logo/upload", methods=["POST"])
def api_upload_bar_logo():
    uploaded_file = request.files.get("file")
    if uploaded_file is None or not str(uploaded_file.filename or "").strip():
        return jsonify({"error": "No logo file provided."}), 400

    content = uploaded_file.read()
    if not content:
        return jsonify({"error": "Uploaded logo is empty."}), 400
    if len(content) > MAX_LOGO_UPLOAD_BYTES:
        return jsonify({"error": "Logo file is too large (max 2 MB)."}), 413

    extension = _infer_logo_extension(uploaded_file, content)
    if extension is None:
        return jsonify({"error": "Unsupported logo format. Use PNG, JPG, GIF, WEBP, or SVG."}), 415

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _remove_uploaded_logos()
    logo_path = UPLOADS_DIR / f"{LOGO_FILENAME_PREFIX}{extension}"
    with open(logo_path, "wb") as f:
        f.write(content)

    data = load_data()
    data["settings"]["bar_logo_url"] = _build_uploaded_logo_url()
    save_data(data)

    return jsonify({
        "ok": True,
        "bar_logo_url": data["settings"]["bar_logo_url"],
        "size_bytes": len(content),
    })


@app.route("/media/bar-logo", methods=["GET"])
def media_bar_logo():
    logo_path = _get_uploaded_logo_file_path()
    if logo_path is None or not logo_path.exists():
        return jsonify({"error": "Bar logo not found."}), 404
    return send_file(logo_path)


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
    current_user = _get_current_team_user()
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
    _record_team_audit(
        data,
        current_user,
        "stock_created",
        f"stock:{item['id']}",
        {key: item.get(key) for key in ("name", "category", "quantity", "unit")},
    )
    save_data(data)
    return jsonify(item), 201


@app.route("/api/stock/<int:item_id>", methods=["PUT"])
def api_update_stock(item_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
            _record_team_audit(
                data,
                current_user,
                "stock_updated",
                f"stock:{item_id}",
                {
                    "changed_fields": sorted(body.keys()),
                    **{key: item.get(key) for key in ("name", "category", "quantity", "unit")},
                },
            )
            save_data(data)
            return jsonify(item)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/stock/<int:item_id>", methods=["DELETE"])
def api_delete_stock(item_id: int):
    data = load_data()
    current_user = _get_current_team_user()
    if not _bar_stock_enabled(data):
        return jsonify({"error": "Bar stock feature is disabled"}), 403
    deleted_item = next((item for item in data["bar_stock"] if item["id"] == item_id), None)
    data["bar_stock"] = [i for i in data["bar_stock"] if i["id"] != item_id]
    if deleted_item is not None:
        _record_team_audit(
            data,
            current_user,
            "stock_deleted",
            f"stock:{item_id}",
            {key: deleted_item.get(key) for key in ("name", "category", "quantity", "unit")},
        )
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


@app.route("/api/beers/search")
def api_search_beers():
    data = load_data()
    query = (request.args.get("q") or "").strip().lower()
    beers = sorted(
        data.get("beers", []),
        key=lambda beer: str(beer.get("name", "")).lower(),
    )
    if not query:
        return jsonify(beers[:25])

    filtered = []
    for beer in beers:
        haystack = " ".join(
            [
                str(beer.get("name", "")),
                str(beer.get("brewery", "")),
                str(beer.get("brewer", "")),
                str(beer.get("type", "")),
                str(beer.get("style_guideline", "")),
                str(beer.get("description", "")),
                " ".join(beer.get("allergens", [])),
                str(beer.get("supplier", "")),
                str(beer.get("distributor", "")),
                str(beer.get("sku", "")),
                str(beer.get("upc", "")),
                str(beer.get("notes", "")),
            ]
        ).lower()
        if query in haystack:
            filtered.append(beer)

    return jsonify(filtered[:25])


@app.route("/api/beers/export/csv")
def export_beers_csv():
    data = load_data()
    rows = [BEER_CSV_HEADER]
    for beer in sorted(data.get("beers", []), key=lambda item: str(item.get("name", "")).lower()):
        rows.append([
            ", ".join(beer.get(field, [])) if field == "allergens" else beer.get(field, "")
            for field in BEER_CSV_HEADER
        ])
    csv_bytes = _rows_to_csv_bytes(rows)
    date_stamp = _export_date_stamp()
    return send_file(
        io.BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"beers_{date_stamp}.csv",
    )


BEER_CSV_HEADER = [
    "name",
    "type",
    "style_guideline",
    "packaging",
    "brewer",
    "brewery",
    "abv",
    "ibu",
    "brewed_on",
    "packaged_on",
    "best_by_date",
    "availability_status",
    "description",
    "allergens",
    "color_srm",
    "color_ebc",
    "serving_temperature",
    "glassware",
    "supplier",
    "distributor",
    "sku",
    "upc",
    "recipe_url",
    "notes",
]


def _normalize_csv_header(name: str) -> str:
    return str(name or "").strip().lower().replace(" ", "_")


def _coerce_beer_packaging(value):
    normalized = str(value or "kegged").strip().lower()
    aliases = {
        "kegged": "kegged",
        "keg": "kegged",
        "bottled": "bottled_can",
        "bottle": "bottled_can",
        "can": "bottled_can",
        "bottled_can": "bottled_can",
    }
    if normalized in aliases:
        return aliases[normalized]
    return "kegged"


def _validate_beer_csv_row(row: dict, row_number: int):
    beer_name = str(row.get("name", "") or "").strip()
    if not beer_name:
        return f"Row {row_number}: Beer name is required."

    packaging = _coerce_beer_packaging(row.get("packaging", "kegged"))
    if packaging not in {"kegged", "bottled_can"}:
        return f"Row {row_number}: packaging must be 'kegged' or 'bottled_can'."

    for field in ("abv", "ibu", "color_srm", "color_ebc"):
        raw = str(row.get(field, "") or "").strip()
        if raw and not re.fullmatch(r"\d+(?:\.\d+)?", raw):
            return f"Row {row_number}: '{field}' must be numeric or blank."

    return None


def _parse_beer_csv_rows(file_bytes: bytes):
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return [], ["CSV file is empty or missing a header row."]

    normalized_headers = [_normalize_csv_header(name) for name in reader.fieldnames]
    unexpected = [name for name in normalized_headers if name not in BEER_CSV_HEADER]
    if unexpected:
        return [], [
            "Unsupported CSV column(s): " + ", ".join(sorted(set(unexpected))) + ". "
            "Use the exact header set: " + ", ".join(BEER_CSV_HEADER) + "."
        ]

    missing = [name for name in BEER_CSV_HEADER if name not in normalized_headers and name == "name"]
    if missing:
        return [], ["CSV is missing required column 'name'."]

    rows = []
    errors = []
    for row_index, row in enumerate(reader, start=2):
        if not row or not any(str(value or "").strip() for value in row.values()):
            continue

        normalized = {}
        for key in BEER_CSV_HEADER:
            original_value = row.get(next((name for name in reader.fieldnames if _normalize_csv_header(name) == key), key), "")
            normalized[key] = "" if original_value is None else str(original_value).strip()

        validation_error = _validate_beer_csv_row(normalized, row_index)
        if validation_error:
            errors.append(validation_error)
            continue

        rows.append({
            "name": normalized.get("name", "").strip(),
            "type": normalized.get("type", "").strip(),
            "style_guideline": normalized.get("style_guideline", "").strip(),
            "packaging": _coerce_beer_packaging(normalized.get("packaging", "kegged")),
            "brewer": normalized.get("brewer", "").strip(),
            "brewery": normalized.get("brewery", "").strip(),
            "abv": normalized.get("abv", "").strip(),
            "ibu": normalized.get("ibu", "").strip(),
            "brewed_on": normalized.get("brewed_on", "").strip(),
            "packaged_on": normalized.get("packaged_on", "").strip(),
            "best_by_date": normalized.get("best_by_date", "").strip(),
            "availability_status": normalized.get("availability_status", "available").strip() or "available",
            "description": normalized.get("description", "").strip(),
            "allergens": _normalize_beer_allergens(normalized.get("allergens", "")),
            "color_srm": normalized.get("color_srm", "").strip(),
            "color_ebc": normalized.get("color_ebc", "").strip(),
            "serving_temperature": normalized.get("serving_temperature", "").strip(),
            "glassware": normalized.get("glassware", "").strip(),
            "supplier": normalized.get("supplier", "").strip(),
            "distributor": normalized.get("distributor", "").strip(),
            "sku": normalized.get("sku", "").strip(),
            "upc": normalized.get("upc", "").strip(),
            "recipe_url": normalized.get("recipe_url", "").strip(),
            "notes": normalized.get("notes", "").strip(),
        })

    return rows, errors


@app.route("/api/beers/import/csv/preview", methods=["POST"])
def preview_beer_csv_import():
    upload = _get_request_upload("file")
    if upload is None:
        return jsonify({"error": "No CSV file provided."}), 400

    rows, errors = _parse_beer_csv_rows(upload.read())
    return jsonify({
        "ok": not bool(errors),
        "summary": {"beers": len(rows), "errors": len(errors)},
        "rows": rows,
        "errors": errors,
    })


@app.route("/api/beers/import/csv", methods=["POST"])
def import_beer_csv():
    upload = _get_request_upload("file")
    if upload is None:
        return jsonify({"error": "No CSV file provided."}), 400

    rows, errors = _parse_beer_csv_rows(upload.read())
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    data = load_data()
    existing_beers = data.setdefault("beers", [])
    next_id = _next_id(existing_beers)
    for row in rows:
        beer = {
            "id": next_id,
            **row,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_beers.append(beer)
        next_id += 1

    save_data(data)
    return jsonify({"ok": True, "summary": {"beers": len(rows)}})


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
        "style_guideline": str(body.get("style_guideline", "")).strip(),
        "packaging": _normalize_beer_packaging(body.get("packaging", "kegged")),
        "brewer": str(body.get("brewer", "")).strip(),
        "brewery": str(body.get("brewery", "")).strip(),
        "abv": str(body.get("abv", "")).strip(),
        "ibu": str(body.get("ibu", "")).strip(),
        "brewed_on": str(body.get("brewed_on", "")).strip(),
        "packaged_on": str(body.get("packaged_on", "")).strip(),
        "best_by_date": str(body.get("best_by_date", "")).strip(),
        "availability_status": str(body.get("availability_status", "available")).strip() or "available",
        "description": str(body.get("description", "")).strip(),
        "allergens": _normalize_beer_allergens(body.get("allergens", [])),
        "color_srm": str(body.get("color_srm", "")).strip(),
        "color_ebc": str(body.get("color_ebc", "")).strip(),
        "serving_temperature": str(body.get("serving_temperature", "")).strip(),
        "glassware": str(body.get("glassware", "")).strip(),
        "supplier": str(body.get("supplier", "")).strip(),
        "distributor": str(body.get("distributor", "")).strip(),
        "sku": str(body.get("sku", "")).strip(),
        "upc": str(body.get("upc", "")).strip(),
        "recipe_url": str(body.get("recipe_url", "")).strip(),
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

        for field in (
            "name", "type", "style_guideline", "brewer", "brewery", "abv", "ibu",
            "brewed_on", "packaged_on", "best_by_date", "availability_status", "description",
            "color_srm", "color_ebc", "serving_temperature", "glassware", "supplier",
            "distributor", "sku", "upc", "recipe_url", "notes",
        ):
            if field in body:
                beer[field] = str(body.get(field, "")).strip()
        if "allergens" in body:
            beer["allergens"] = _normalize_beer_allergens(body.get("allergens", []))
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

KEG_SIZES_US = ["Corny (5 gal)", "1/6 bbl (5.2 gal)", "1/4 bbl (7.75 gal)", "Full Size (1/2 bbl, 15.5 gal)", "Custom"]
KEG_SIZES_METRIC = ["20 L", "30 L", "50 L", "Custom"]
KEG_STATUSES = ["full", "in_use", "empty", "cleaning", "retired"]
API_REFERENCE_ENDPOINTS = [
    ("GET", "/api/settings", "Get current settings"),
    ("POST", "/api/settings", "Update settings"),
    ("POST", "/api/settings/displays/reset", "Reset Pro display configuration to defaults"),
    ("GET", "/api/storage/status", "Get credential-safe storage status"),
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
    ("POST", "/api/team/users", "Issue, rotate, revoke, or configure a user scan credential"),
    ("GET", "/auth/scan/<token>", "Sign in with a user QR/NFC scan credential"),
    ("GET", "/api/brewfather/status", "Get Brewfather sync status"),
    ("POST", "/api/brewfather/sync", "Run Brewfather recipe and batch sync"),
    ("GET", "/api/brewfather/conflicts", "List Brewfather catalog conflicts"),
    ("POST", "/api/brewfather/conflicts/<id>/resolve", "Resolve a Brewfather catalog conflict"),
    ("GET", "/api/brewfather/batches/<id>", "Review a linked Brewfather batch"),
    ("POST", "/api/brewfather/batches/<id>/import-keg", "Import a confirmed Brewfather batch as a keg"),
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
    current_user = _get_current_team_user()
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

    allowed, error = _enforce_homebrewer_limits(data, "kegs")
    if not allowed:
        return jsonify({"error": error}), 409

    keg_type = _normalize_builtin_keg_type_label(body.get("type", "")) or _normalize_builtin_keg_type_label(
        data.get("settings", {}).get("default_keg_type", "")
    )
    default_keg_size = _normalize_builtin_keg_type_label(
        data.get("settings", {}).get("default_keg_type", "")
    )
    timestamp = datetime.now(timezone.utc).isoformat()
    keg = {
        "id": _next_id(data["kegs"]),
        "name": body.get("name", ""),
        "serial_number": str(body.get("serial_number", "")).strip(),
        "beer_id": beer_id,
        "beer_name": str(body.get("beer_name", "")).strip(),
        "beer_type": str(body.get("beer_type", "")).strip(),
        "type": keg_type,
        "size": _normalize_builtin_keg_type_label(body.get("size", "")) or default_keg_size,
        "custom_size": body.get("custom_size", ""),
        "status": initial_status,
        "coupler_type": str(body.get("coupler_type", "")).strip(),
        "ownership_type": str(body.get("ownership_type", "")).strip(),
        "location": str(body.get("location", "")).strip(),
        "serving_psi": str(body.get("serving_psi", "")).strip(),
        "gas_type": str(body.get("gas_type", "")).strip(),
        "beer_brewer": body.get("beer_brewer", ""),
        "beer_brewery": body.get("beer_brewery", body.get("brewery", "")),
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
        "brewery": body.get("brewery", body.get("beer_brewery", body.get("beer_brewer", ""))),
        "abv": body.get("abv", body.get("beer_abv", "")),
        "notes": body.get("notes", ""),
        "tapped_date": body.get("tapped_date", ""),
        "filled_date": incoming_filled_date,
        "kicked_date": str(body.get("kicked_date", "")).strip(),
        "cleaned_date": str(body.get("cleaned_date", "")).strip(),
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
    _record_team_audit(
        data,
        current_user,
        "keg_created",
        f"keg:{keg['id']}",
        {key: keg.get(key) for key in ("name", "beer_name", "status", "type", "size")},
    )
    save_data(data)
    return jsonify(keg), 201


@app.route("/api/kegs/bulk", methods=["POST"])
def api_add_kegs_bulk():
    data = load_data()
    current_user = _get_current_team_user()
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

        allowed, error = _enforce_homebrewer_limits(simulated_data, "kegs")
        if not allowed:
            return jsonify({"error": error, "index": index}), 409

        keg_type = _normalize_builtin_keg_type_label(item.get("type", "")) or _normalize_builtin_keg_type_label(
            simulated_data.get("settings", {}).get("default_keg_type", "")
        )
        default_keg_size = _normalize_builtin_keg_type_label(
            simulated_data.get("settings", {}).get("default_keg_type", "")
        )
        timestamp = datetime.now(timezone.utc).isoformat()
        keg = {
            "id": _next_id(simulated_data["kegs"]),
            "name": item.get("name", ""),
            "serial_number": str(item.get("serial_number", "")).strip(),
            "beer_id": beer_id,
            "beer_name": str(item.get("beer_name", "")).strip(),
            "beer_type": str(item.get("beer_type", "")).strip(),
            "type": keg_type,
            "size": _normalize_builtin_keg_type_label(item.get("size", "")) or default_keg_size,
            "custom_size": item.get("custom_size", ""),
            "status": initial_status,
            "coupler_type": str(item.get("coupler_type", "")).strip(),
            "ownership_type": str(item.get("ownership_type", "")).strip(),
            "location": str(item.get("location", "")).strip(),
            "serving_psi": str(item.get("serving_psi", "")).strip(),
            "gas_type": str(item.get("gas_type", "")).strip(),
            "beer_brewer": item.get("beer_brewer", ""),
            "beer_brewery": item.get("beer_brewery", item.get("brewery", "")),
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
            "brewery": item.get("brewery", item.get("beer_brewery", item.get("beer_brewer", ""))),
            "abv": item.get("abv", item.get("beer_abv", "")),
            "notes": item.get("notes", ""),
            "tapped_date": item.get("tapped_date", ""),
            "filled_date": incoming_filled_date,
            "kicked_date": str(item.get("kicked_date", "")).strip(),
            "cleaned_date": str(item.get("cleaned_date", "")).strip(),
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

    _record_team_audit(
        simulated_data,
        current_user,
        "kegs_bulk_created",
        "kegs",
        {"count": len(created), "keg_ids": [keg["id"] for keg in created]},
    )
    save_data(simulated_data)
    return jsonify({"ok": True, "created": created, "count": len(created)}), 201


@app.route("/api/kegs/<int:keg_id>", methods=["PUT"])
def api_update_keg(keg_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
            if "brewery" in body and "beer_brewery" not in body:
                body["beer_brewery"] = body["brewery"]
            if "beer_brewery" in body and "brewery" not in body:
                body["brewery"] = body["beer_brewery"]
            if "abv" in body and "beer_abv" not in body:
                body["beer_abv"] = body["abv"]
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
                "serial_number",
                "beer_id",
                "beer_name",
                "type",
                "size",
                "custom_size",
                "status",
                "coupler_type",
                "ownership_type",
                "location",
                "serving_psi",
                "gas_type",
                "beer_brewer",
                "beer_brewery",
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
                "kicked_date",
                "cleaned_date",
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
            _record_team_audit(
                data,
                current_user,
                "keg_updated",
                f"keg:{keg_id}",
                {
                    "changed_fields": sorted(body.keys()),
                    **{key: keg.get(key) for key in ("name", "beer_name", "status", "percent_full", "current_volume", "volume_unit")},
                },
            )
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/fill", methods=["POST"])
def api_fill_keg(keg_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
            capacity = _extract_keg_capacity(keg)
            if "current_volume" in body:
                keg["current_volume"] = _coerce_float(body.get("current_volume"), None)
                if keg["current_volume"] is None or keg["current_volume"] < 0:
                    return jsonify({"error": "Current volume must be zero or greater."}), 400
            elif capacity is not None:
                keg["current_volume"] = capacity[0]

            if "volume_unit" in body:
                keg["volume_unit"] = _normalize_volume_unit(body.get("volume_unit"))
            elif capacity is not None:
                keg["volume_unit"] = capacity[1]
            keg["on_deck"] = False
            keg["updated_at"] = datetime.now(timezone.utc).isoformat()
            _record_team_audit(
                data,
                current_user,
                "keg_filled",
                f"keg:{keg_id}",
                {key: keg.get(key) for key in ("name", "beer_name", "status", "percent_full", "current_volume", "volume_unit", "filled_date")},
            )
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/clean", methods=["POST"])
def api_clean_keg(keg_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
        _record_team_audit(
            data,
            current_user,
            "keg_cleaned",
            f"keg:{keg_id}",
            {key: keg.get(key) for key in ("name", "status", "percent_full")},
        )
        save_data(data)
        return jsonify(keg)

    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>/pour", methods=["POST"])
def api_pour_keg(keg_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
        _record_pour_event(
            data,
            keg,
            amount,
            body.get("unit"),
            "keg",
            preset_name=str(body.get("preset_name", "")),
            actor=current_user,
        )
        save_data(data)
        return jsonify(payload)

    return jsonify({"error": "Not found"}), 404


@app.route("/api/taps/<int:tap_id>/pour", methods=["POST"])
def api_pour_tap(tap_id: int):
    data = load_data()
    current_user = _get_current_team_user()
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
        _record_pour_event(
            data,
            keg,
            amount,
            body.get("unit"),
            "tap",
            tap_id=tap_id,
            preset_name=str(body.get("preset_name", "")),
            actor=current_user,
        )
        save_data(data)
        return jsonify(payload)

    return jsonify({"error": "Assigned keg not found."}), 404


@app.route("/api/kegs/<int:keg_id>", methods=["DELETE"])
def api_delete_keg(keg_id: int):
    data = load_data()
    current_user = _get_current_team_user()
    assigned_taps = [tap for tap in data["taps"] if tap.get("keg_id") == keg_id]
    if assigned_taps:
        tap_numbers = [tap.get("number") for tap in assigned_taps if tap.get("number") is not None]
        return jsonify({
            "error": "This keg is assigned to one or more taps. Disconnect it from all taps (or delete those taps) before deleting the keg.",
            "code": "KEG_ASSIGNED_TO_TAP",
            "tap_count": len(assigned_taps),
            "tap_numbers": tap_numbers,
        }), 409

    deleted_keg = next((keg for keg in data["kegs"] if keg["id"] == keg_id), None)
    data["kegs"] = [k for k in data["kegs"] if k["id"] != keg_id]
    if deleted_keg is not None:
        _record_team_audit(
            data,
            current_user,
            "keg_deleted",
            f"keg:{keg_id}",
            {key: deleted_keg.get(key) for key in ("name", "beer_name", "status", "type", "size")},
        )
    save_data(data)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API – Taps
# ---------------------------------------------------------------------------

def _find_tap_assigned_to_keg(taps: list[dict], keg_id: int | None, exclude_tap_id: int | None = None) -> dict | None:
    if keg_id is None:
        return None
    for tap in taps:
        if exclude_tap_id is not None and tap.get("id") == exclude_tap_id:
            continue
        if tap.get("keg_id") == keg_id:
            return tap
    return None

@app.route("/api/taps", methods=["GET"])
def api_list_taps():
    data = load_data()
    return jsonify(data["taps"])


@app.route("/api/taps", methods=["POST"])
def api_add_tap():
    data = load_data()
    current_user = _get_current_team_user()
    body = request.get_json(force=True)
    raw_keg_id = body.get("keg_id")
    parsed_keg_id = _coerce_int(raw_keg_id, None)
    if raw_keg_id not in (None, "") and parsed_keg_id is None:
        return jsonify({"error": "Invalid keg_id."}), 400
    conflicting_tap = _find_tap_assigned_to_keg(data.get("taps", []), parsed_keg_id)
    if conflicting_tap is not None:
        return jsonify({
            "error": "Keg is already connected to another tap.",
            "code": "KEG_ALREADY_CONNECTED",
            "tap_id": conflicting_tap.get("id"),
            "tap_number": conflicting_tap.get("number"),
        }), 409
    allowed, error = _enforce_homebrewer_limits(data, "taps")
    if not allowed:
        return jsonify({"error": error}), 409
    tap = {
        "id": _next_id(data["taps"]),
        "number": body.get("number", len(data["taps"]) + 1),
        "label": body.get("label", ""),
        "keg_id": parsed_keg_id,
        "ever_assigned_keg": parsed_keg_id is not None,
        "location": str(body.get("location", "")).strip(),
        "status": str(body.get("status", "active")).strip().lower() or "active",
        "tap_handle": str(body.get("tap_handle", "")).strip(),
        "faucet_type": str(body.get("faucet_type", "")).strip(),
        "line_length_feet": str(body.get("line_length_feet", "")).strip(),
        "line_inner_diameter": str(body.get("line_inner_diameter", "")).strip(),
        "line_material": str(body.get("line_material", "")).strip(),
        "target_pressure_psi": str(body.get("target_pressure_psi", "")).strip(),
        "target_temperature": str(body.get("target_temperature", "")).strip(),
        "clean_interval_days": _coerce_int(body.get("clean_interval_days"), 14) or 14,
        "last_cleaned_date": str(body.get("last_cleaned_date", "")).strip(),
        "last_serviced_date": str(body.get("last_serviced_date", "")).strip(),
        "notes": body.get("notes", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_keg_tapped_date_if_missing(data, tap.get("keg_id"))
    data["taps"].append(tap)
    _record_team_audit(
        data,
        current_user,
        "tap_created",
        f"tap:{tap['id']}",
        {key: tap.get(key) for key in ("number", "label", "keg_id")},
    )
    save_data(data)
    return jsonify(tap), 201


@app.route("/api/taps/bulk", methods=["POST"])
def api_add_taps_bulk():
    data = load_data()
    current_user = _get_current_team_user()
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

        conflicting_tap = _find_tap_assigned_to_keg(simulated_data.get("taps", []), keg_id)
        if conflicting_tap is not None:
            return jsonify({
                "error": "Keg is already connected to another tap.",
                "code": "KEG_ALREADY_CONNECTED",
                "index": index,
                "tap_id": conflicting_tap.get("id"),
                "tap_number": conflicting_tap.get("number"),
            }), 409

        allowed, error = _enforce_homebrewer_limits(simulated_data, "taps")
        if not allowed:
            return jsonify({"error": error, "index": index}), 409

        number = _coerce_int(item.get("number"), None)
        if number is None or number <= 0:
            return jsonify({"error": "Tap number must be a positive integer.", "index": index}), 400

        tap = {
            "id": _next_id(simulated_data["taps"]),
            "number": number,
            "label": item.get("label", ""),
            "keg_id": keg_id,
            "ever_assigned_keg": keg_id is not None,
            "location": str(item.get("location", "")).strip(),
            "status": str(item.get("status", "active")).strip().lower() or "active",
            "tap_handle": str(item.get("tap_handle", "")).strip(),
            "faucet_type": str(item.get("faucet_type", "")).strip(),
            "line_length_feet": str(item.get("line_length_feet", "")).strip(),
            "line_inner_diameter": str(item.get("line_inner_diameter", "")).strip(),
            "line_material": str(item.get("line_material", "")).strip(),
            "target_pressure_psi": str(item.get("target_pressure_psi", "")).strip(),
            "target_temperature": str(item.get("target_temperature", "")).strip(),
            "clean_interval_days": _coerce_int(item.get("clean_interval_days"), 14) or 14,
            "last_cleaned_date": str(item.get("last_cleaned_date", "")).strip(),
            "last_serviced_date": str(item.get("last_serviced_date", "")).strip(),
            "notes": item.get("notes", ""),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _set_keg_tapped_date_if_missing(simulated_data, tap.get("keg_id"))
        simulated_data["taps"].append(tap)
        created.append(tap)

    _record_team_audit(
        simulated_data,
        current_user,
        "taps_bulk_created",
        "taps",
        {"count": len(created), "tap_ids": [tap["id"] for tap in created]},
    )
    save_data(simulated_data)
    return jsonify({"ok": True, "created": created, "count": len(created)}), 201


@app.route("/api/taps/<int:tap_id>", methods=["PUT"])
def api_update_tap(tap_id: int):
    data = load_data()
    current_user = _get_current_team_user()
    for tap in data["taps"]:
        if tap["id"] == tap_id:
            body = request.get_json(force=True)
            for field in (
                "number",
                "label",
                "location",
                "status",
                "tap_handle",
                "faucet_type",
                "line_length_feet",
                "line_inner_diameter",
                "line_material",
                "target_pressure_psi",
                "target_temperature",
                "clean_interval_days",
                "last_cleaned_date",
                "last_serviced_date",
                "notes",
            ):
                if field in body:
                    if field == "clean_interval_days":
                        tap[field] = _coerce_int(body.get(field), 14) or 14
                    else:
                        tap[field] = body[field]
            if "keg_id" in body:
                raw_keg_id = body.get("keg_id")
                parsed_keg_id = _coerce_int(raw_keg_id, None)
                if raw_keg_id not in (None, "") and parsed_keg_id is None:
                    return jsonify({"error": "Invalid keg_id."}), 400
                conflicting_tap = _find_tap_assigned_to_keg(
                    data.get("taps", []),
                    parsed_keg_id,
                    exclude_tap_id=tap_id,
                )
                if conflicting_tap is not None:
                    return jsonify({
                        "error": "Keg is already connected to another tap.",
                        "code": "KEG_ALREADY_CONNECTED",
                        "tap_id": conflicting_tap.get("id"),
                        "tap_number": conflicting_tap.get("number"),
                    }), 409
                tap["keg_id"] = parsed_keg_id
                if parsed_keg_id is not None:
                    tap["ever_assigned_keg"] = True
            _set_keg_tapped_date_if_missing(data, tap.get("keg_id"))
            tap["updated_at"] = datetime.now(timezone.utc).isoformat()
            _record_team_audit(
                data,
                current_user,
                "tap_updated",
                f"tap:{tap_id}",
                {
                    "changed_fields": sorted(body.keys()),
                    **{key: tap.get(key) for key in ("number", "label", "keg_id")},
                },
            )
            save_data(data)
            return jsonify(tap)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/taps/<int:tap_id>", methods=["DELETE"])
def api_delete_tap(tap_id: int):
    data = load_data()
    current_user = _get_current_team_user()
    deleted_tap = next((tap for tap in data["taps"] if tap["id"] == tap_id), None)
    data["taps"] = [t for t in data["taps"] if t["id"] != tap_id]
    if deleted_tap is not None:
        _record_team_audit(
            data,
            current_user,
            "tap_deleted",
            f"tap:{tap_id}",
            {key: deleted_tap.get(key) for key in ("number", "label", "keg_id")},
        )
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
        "serial_number",
        "beer_id",
        "beer_name",
        "type",
        "size",
        "custom_size",
        "status",
        "coupler_type",
        "ownership_type",
        "location",
        "serving_psi",
        "gas_type",
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
        "kicked_date",
        "cleaned_date",
        "percent_full",
        "updated_at",
    ]
    rows = [header]
    for keg in kegs:
        rows.append([keg.get(field, "") for field in header])
    return rows


def _tap_csv_rows(taps: list[dict]) -> list[list]:
    header = [
        "id",
        "number",
        "label",
        "location",
        "status",
        "tap_handle",
        "faucet_type",
        "line_length_feet",
        "line_inner_diameter",
        "line_material",
        "target_pressure_psi",
        "target_temperature",
        "clean_interval_days",
        "last_cleaned_date",
        "last_serviced_date",
        "keg_id",
        "notes",
        "updated_at",
    ]
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


def _export_date_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _build_export_json_payload(data: dict) -> dict:
    export_data = json.loads(json.dumps(data))
    export_settings = export_data.get("settings", {})
    if isinstance(export_settings, dict):
        export_settings.pop("brewfather_api_key", None)
        export_settings.pop("brewfather_user_id", None)
        export_settings.pop("license_instance_private_key", None)
        export_settings["brewfather_credentials"] = redact_credentials(
            normalize_credentials(
                data.get("settings", {}).get("brewfather_user_id"),
                data.get("settings", {}).get("brewfather_api_key"),
            )
        )
    return {
        "format": "bartender-export",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "data": export_data,
    }


def _build_export_archive(data: dict) -> bytes:
    """Build a ZIP archive with full BarTender data as separate files."""
    payload = _build_export_json_payload(data)
    export_data = payload["data"]
    out = io.BytesIO()
    with zipfile.ZipFile(out, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Canonical JSON exports by section.
        zf.writestr("settings.json", json.dumps(export_data.get("settings", {}), indent=2))
        zf.writestr("kegs.json", json.dumps(export_data.get("kegs", []), indent=2))
        zf.writestr("taps.json", json.dumps(export_data.get("taps", []), indent=2))
        zf.writestr("beers.json", json.dumps(export_data.get("beers", []), indent=2))
        zf.writestr("bar_stock.json", json.dumps(export_data.get("bar_stock", []), indent=2))
        zf.writestr("pour_events.json", json.dumps(export_data.get("pour_events", []), indent=2))
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
    date_stamp = _export_date_stamp()
    return send_file(
        io.BytesIO(json.dumps(payload, indent=2).encode("utf-8")),
        mimetype="application/json",
        as_attachment=True,
        download_name=f"bartender_export_{date_stamp}.json",
    )


@app.route("/api/export/archive")
def export_archive():
    data = load_data()
    archive = _build_export_archive(data)
    date_stamp = _export_date_stamp()
    return send_file(
        io.BytesIO(archive),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"bartender_export_{date_stamp}.zip",
    )


@app.route("/api/export/csv")
def export_csv():
    data = load_data()
    archive = _build_export_archive(data)
    date_stamp = _export_date_stamp()
    return send_file(
        io.BytesIO(archive),
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"bartender_export_{date_stamp}.zip",
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
