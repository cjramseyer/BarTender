"""BarTender - Home Assistant Add-on for bar, keg, and tap management."""

import json
import os
import io
import csv
import zipfile
from datetime import datetime, timezone
from pathlib import Path

try:
    import qrcode

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

app = Flask(__name__)
app.config["APPLICATION_ROOT"] = INGRESS_PATH or "/"


# ---------------------------------------------------------------------------
# Data persistence
# ---------------------------------------------------------------------------

DEFAULT_DATA = {
    "settings": {
        "measurement": "us",
        "theme": "light",
        "bar_name": "My Bar",
        "dashboard_manage_button_position": "top-right",
        "bar_stock_enabled": True,
        "default_keg_type": "",
        "menu_qr_mode": "both",
        "pour_options": [
            {"name": "Half Pint", "amount": 8, "unit": "oz"},
            {"name": "Pint", "amount": 16, "unit": "oz"},
        ],
    },
    "bar_stock": [],
    "kegs": [],
    "taps": [],
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
        data["settings"]["menu_qr_mode"] = _normalize_menu_qr_mode(
            data["settings"].get("menu_qr_mode")
        )
        data["settings"]["pour_options"] = _normalize_pour_options(
            data["settings"].get("pour_options"),
            data["settings"].get("measurement", "us"),
        )
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


def _normalize_menu_qr_mode(value) -> str:
    mode = str(value or "both").strip().lower()
    if mode in ("off", "display", "print", "both"):
        return mode
    return "both"


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


def _is_cleaning_transition_allowed(previous_status: str, next_status: str) -> bool:
    """When a keg needs cleaning, it can only be marked clean (empty)."""
    prev = _normalize_keg_status(previous_status)
    nxt = _normalize_keg_status(next_status)
    if prev != "cleaning":
        return True
    return nxt == "empty"


def _validate_full_keg_requirements(keg_like: dict):
    """Require name + beer details once a keg is full."""
    status = _normalize_keg_status(keg_like.get("status", "empty"))
    if status != "full":
        return None

    name = str(keg_like.get("name", "")).strip()
    beer_value = (
        str(keg_like.get("type", "")).strip()
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
        ingress=INGRESS_PATH,
    )


@app.route("/display")
def display_view():
    data = load_data()
    return render_template(
        "display/index.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
    )


@app.route("/menu")
def menu_view():
    data = load_data()
    kegs_by_id = {
        keg.get("id"): keg for keg in data.get("kegs", []) if isinstance(keg, dict)
    }

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

    menu_path = f"{INGRESS_PATH}/menu" if INGRESS_PATH else "/menu"
    qr_image_path = f"{INGRESS_PATH}/api/menu/qr" if INGRESS_PATH else "/api/menu/qr"
    menu_qr_mode = _normalize_menu_qr_mode(data.get("settings", {}).get("menu_qr_mode"))
    qr_ready = _qr_is_available()
    return render_template(
        "menu.html",
        settings=data["settings"],
        on_tap=on_tap,
        menu_path=menu_path,
        qr_image_path=qr_image_path,
        menu_qr_mode=menu_qr_mode,
        qr_ready=qr_ready,
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

    menu_path = f"{INGRESS_PATH}/menu" if INGRESS_PATH else "/menu"
    menu_url = f"{request.host_url.rstrip('/')}{menu_path}"

    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(menu_url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")
    out = io.BytesIO()
    img.save(out, format="PNG")
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
        "dashboard_manage_button_position",
        "bar_stock_enabled",
        "default_keg_type",
        "menu_qr_mode",
        "pour_options",
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
    data["settings"]["default_keg_type"] = str(
        data["settings"].get("default_keg_type", "")
    ).strip()
    data["settings"]["menu_qr_mode"] = _normalize_menu_qr_mode(
        data["settings"].get("menu_qr_mode")
    )
    data["settings"]["pour_options"] = _normalize_pour_options(
        data["settings"].get("pour_options"),
        data["settings"].get("measurement", "us"),
    )

    save_data(data)
    return jsonify(data["settings"])


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
# API – Kegs
# ---------------------------------------------------------------------------

KEG_SIZES_US = ["1/6 bbl (5.2 gal)", "1/4 bbl (7.75 gal)", "1/2 bbl (15.5 gal)", "Corny (5 gal)", "Custom"]
KEG_SIZES_METRIC = ["20 L", "30 L", "50 L", "Custom"]
KEG_STATUSES = ["full", "in_use", "empty", "cleaning", "retired"]


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
    keg_type = str(body.get("type", "")).strip() or str(
        data.get("settings", {}).get("default_keg_type", "")
    ).strip()
    keg = {
        "id": _next_id(data["kegs"]),
        "name": body.get("name", ""),
        "type": keg_type,
        "size": body.get("size", ""),
        "custom_size": body.get("custom_size", ""),
        "status": initial_status,
        "beer_brewer": body.get("beer_brewer", body.get("brewery", "")),
        "beer_abv": body.get("beer_abv", body.get("abv", "")),
        "beer_ibu": body.get("beer_ibu", ""),
        "beer_brewed_on": body.get("beer_brewed_on", ""),
        "line_cleaning_keg": _coerce_bool(body.get("line_cleaning_keg"), False),
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
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

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
    data["kegs"].append(keg)
    save_data(data)
    return jsonify(keg), 201


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
                "brewery",
                "abv",
                "notes",
                "tapped_date",
                "filled_date",
                "percent_full",
            ):
                if field in body:
                    keg[field] = body[field]

            if "current_volume" in body:
                keg["current_volume"] = _coerce_float(keg.get("current_volume"), None)
            if "volume_unit" in body:
                keg["volume_unit"] = _normalize_volume_unit(keg.get("volume_unit"))

            _sync_percent_for_volume_change(
                previous_keg,
                keg,
                current_volume_explicit="current_volume" in body,
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

            target_status = _normalize_keg_status(body.get("status", "full"))
            if target_status not in KEG_STATUSES:
                return jsonify({"error": "Invalid status"}), 400

            keg["status"] = target_status
            validation_error = _validate_full_keg_requirements(keg)
            if validation_error:
                return jsonify(validation_error), 409
            keg["filled_date"] = body.get("filled_date") or _today_utc_date()
            keg["percent_full"] = _clamp_percent_full(body.get("percent_full"), 100)
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
    tap = {
        "id": _next_id(data["taps"]),
        "number": body.get("number", len(data["taps"]) + 1),
        "label": body.get("label", ""),
        "keg_id": body.get("keg_id"),
        "notes": body.get("notes", ""),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    _set_keg_tapped_date_if_missing(data, tap.get("keg_id"))
    data["taps"].append(tap)
    save_data(data)
    return jsonify(tap), 201


@app.route("/api/taps/<int:tap_id>", methods=["PUT"])
def api_update_tap(tap_id: int):
    data = load_data()
    for tap in data["taps"]:
        if tap["id"] == tap_id:
            body = request.get_json(force=True)
            for field in ("number", "label", "keg_id", "notes"):
                if field in body:
                    tap[field] = body[field]
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
        "name",
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
        zf.writestr("bar_stock.json", json.dumps(data.get("bar_stock", []), indent=2))
        zf.writestr("bartender_export.json", json.dumps(payload, indent=2))

        # CSV exports for convenience.
        zf.writestr("kegs.csv", _rows_to_csv_bytes(_keg_csv_rows(data.get("kegs", []))))
        zf.writestr("taps.csv", _rows_to_csv_bytes(_tap_csv_rows(data.get("taps", []))))
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
            bar_stock = _read_archive_json(zf, "bar_stock.json", [])
            return {
                "settings": settings if isinstance(settings, dict) else {},
                "kegs": kegs if isinstance(kegs, list) else [],
                "taps": taps if isinstance(taps, list) else [],
                "bar_stock": bar_stock if isinstance(bar_stock, list) else [],
            }
    except zipfile.BadZipFile:
        return None


def _sanitize_import_payload(raw_data: dict) -> dict:
    """Constrain imported payload to the expected top-level schema."""
    return {
        "settings": raw_data.get("settings", {}) if isinstance(raw_data.get("settings", {}), dict) else {},
        "kegs": [x for x in raw_data.get("kegs", []) if isinstance(x, dict)] if isinstance(raw_data.get("kegs", []), list) else [],
        "taps": [x for x in raw_data.get("taps", []) if isinstance(raw_data.get("taps", []), list) and isinstance(x, dict)] if isinstance(raw_data.get("taps", []), list) else [],
        "bar_stock": [x for x in raw_data.get("bar_stock", []) if isinstance(x, dict)] if isinstance(raw_data.get("bar_stock", []), list) else [],
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
    merged["bar_stock"] = _merge_collection(
        merged.get("bar_stock", []),
        incoming.get("bar_stock", []),
    )
    return merged


def _import_summary(payload: dict) -> dict:
    settings = payload.get("settings", {}) if isinstance(payload.get("settings", {}), dict) else {}
    kegs = payload.get("kegs", []) if isinstance(payload.get("kegs", []), list) else []
    taps = payload.get("taps", []) if isinstance(payload.get("taps", []), list) else []
    bar_stock = payload.get("bar_stock", []) if isinstance(payload.get("bar_stock", []), list) else []

    return {
        "bar_name": settings.get("bar_name") or "My Bar",
        "kegs": len(kegs),
        "taps": len(taps),
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
