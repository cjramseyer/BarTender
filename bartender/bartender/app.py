"""BarTender - Home Assistant Add-on for bar, keg, and tap management."""

import json
import os
import io
import csv
from datetime import datetime
from pathlib import Path

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
        "manage_button_position": "top-right",
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


def _set_keg_tapped_date_if_missing(data: dict, keg_id) -> None:
    if keg_id is None:
        return
    for keg in data["kegs"]:
        if keg.get("id") == keg_id and not keg.get("tapped_date"):
            keg["tapped_date"] = datetime.utcnow().date().isoformat()
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
    allowed = {"measurement", "theme", "bar_name", "manage_button_position"}
    for key in allowed:
        if key in body:
            data["settings"][key] = body[key]

    if data["settings"].get("manage_button_position") not in ("top-right", "bottom-left", "bottom-right"):
        data["settings"]["manage_button_position"] = "top-right"

    save_data(data)
    return jsonify(data["settings"])


# ---------------------------------------------------------------------------
# API – Bar Stock
# ---------------------------------------------------------------------------

@app.route("/api/stock", methods=["GET"])
def api_list_stock():
    data = load_data()
    return jsonify(data["bar_stock"])


@app.route("/api/stock", methods=["POST"])
def api_add_stock():
    data = load_data()
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
        "updated_at": datetime.utcnow().isoformat(),
    }
    data["bar_stock"].append(item)
    save_data(data)
    return jsonify(item), 201


@app.route("/api/stock/<int:item_id>", methods=["PUT"])
def api_update_stock(item_id: int):
    data = load_data()
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
            item["updated_at"] = datetime.utcnow().isoformat()
            save_data(data)
            return jsonify(item)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/stock/<int:item_id>", methods=["DELETE"])
def api_delete_stock(item_id: int):
    data = load_data()
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
    return datetime.utcnow().date().isoformat()


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
    keg = {
        "id": _next_id(data["kegs"]),
        "name": body.get("name", ""),
        "type": body.get("type", ""),
        "size": body.get("size", ""),
        "custom_size": body.get("custom_size", ""),
        "status": initial_status,
        "brewery": body.get("brewery", ""),
        "abv": body.get("abv", ""),
        "notes": body.get("notes", ""),
        "tapped_date": body.get("tapped_date", ""),
        "filled_date": incoming_filled_date,
        "percent_full": _clamp_percent_full(body.get("percent_full"), _default_percent_for_status(initial_status)),
        "updated_at": datetime.utcnow().isoformat(),
    }
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
            body = request.get_json(force=True)
            if "purchased_date" in body and "filled_date" not in body:
                body["filled_date"] = body["purchased_date"]

            if "status" in body:
                body["status"] = _normalize_keg_status(body["status"])

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

            for field in ("name", "type", "size", "custom_size", "status", "brewery", "abv", "notes", "tapped_date", "filled_date", "percent_full"):
                if field in body:
                    keg[field] = body[field]

            if "status" in body:
                _set_filled_date_for_status_transition(keg, body["status"])
                # Preserve existing value when only status changes.
                if has_percent_full:
                    _sync_percent_for_status(keg, body["status"], True)

            if "status" not in body and has_percent_full:
                keg["percent_full"] = _clamp_percent_full(keg.get("percent_full"), _default_percent_for_status(keg.get("status", "empty")))

            keg.pop("purchased_date", None)
            keg["updated_at"] = datetime.utcnow().isoformat()
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
            keg["filled_date"] = body.get("filled_date") or _today_utc_date()
            keg["percent_full"] = _clamp_percent_full(body.get("percent_full"), 100)
            keg["updated_at"] = datetime.utcnow().isoformat()
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


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
        "updated_at": datetime.utcnow().isoformat(),
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
            tap["updated_at"] = datetime.utcnow().isoformat()
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

@app.route("/api/export/json")
def export_json():
    data = load_data()
    buf = io.BytesIO(json.dumps(data, indent=2).encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/json",
        as_attachment=True,
        download_name="bartender_export.json",
    )


@app.route("/api/export/csv")
def export_csv():
    data = load_data()
    section = request.args.get("section", "all")
    buf = io.StringIO()
    writer = csv.writer(buf)

    def write_section(title, items, fields):
        writer.writerow([title])
        if items:
            writer.writerow(fields)
            for item in items:
                writer.writerow([item.get(f, "") for f in fields])
        writer.writerow([])

    if section in ("all", "stock"):
        write_section(
            "Bar Stock",
            data["bar_stock"],
            ["id", "name", "category", "quantity", "unit", "notes", "updated_at"],
        )
    if section in ("all", "kegs"):
        write_section(
            "Kegs",
            data["kegs"],
            ["id", "name", "type", "size", "custom_size", "status", "brewery", "abv", "notes", "tapped_date", "filled_date", "percent_full", "updated_at"],
        )
    if section in ("all", "taps"):
        write_section(
            "Taps",
            data["taps"],
            ["id", "number", "label", "keg_id", "notes", "updated_at"],
        )

    output = buf.getvalue().encode("utf-8")
    return send_file(
        io.BytesIO(output),
        mimetype="text/csv",
        as_attachment=True,
        download_name="bartender_export.csv",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8099))
    app.run(host="0.0.0.0", port=port, debug=False)
