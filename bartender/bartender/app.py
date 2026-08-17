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
    allowed = {"measurement", "theme", "bar_name"}
    for key in allowed:
        if key in body:
            data["settings"][key] = body[key]
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
    item = {
        "id": _next_id(data["bar_stock"]),
        "name": body.get("name", ""),
        "category": body.get("category", ""),
        "quantity": body.get("quantity", 0),
        "unit": body.get("unit", ""),
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


@app.route("/api/kegs", methods=["GET"])
def api_list_kegs():
    data = load_data()
    return jsonify(data["kegs"])


@app.route("/api/kegs", methods=["POST"])
def api_add_keg():
    data = load_data()
    body = request.get_json(force=True)
    keg = {
        "id": _next_id(data["kegs"]),
        "name": body.get("name", ""),
        "type": body.get("type", ""),
        "size": body.get("size", ""),
        "custom_size": body.get("custom_size", ""),
        "status": body.get("status", "full"),
        "brewery": body.get("brewery", ""),
        "abv": body.get("abv", ""),
        "notes": body.get("notes", ""),
        "purchased_date": body.get("purchased_date", ""),
        "tapped_date": body.get("tapped_date", ""),
        "updated_at": datetime.utcnow().isoformat(),
    }
    data["kegs"].append(keg)
    save_data(data)
    return jsonify(keg), 201


@app.route("/api/kegs/<int:keg_id>", methods=["PUT"])
def api_update_keg(keg_id: int):
    data = load_data()
    for keg in data["kegs"]:
        if keg["id"] == keg_id:
            body = request.get_json(force=True)
            for field in ("name", "type", "size", "custom_size", "status", "brewery", "abv", "notes", "purchased_date", "tapped_date"):
                if field in body:
                    keg[field] = body[field]
            keg["updated_at"] = datetime.utcnow().isoformat()
            save_data(data)
            return jsonify(keg)
    return jsonify({"error": "Not found"}), 404


@app.route("/api/kegs/<int:keg_id>", methods=["DELETE"])
def api_delete_keg(keg_id: int):
    data = load_data()
    # unassign from taps
    for tap in data["taps"]:
        if tap.get("keg_id") == keg_id:
            tap["keg_id"] = None
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
            ["id", "name", "type", "size", "custom_size", "status", "brewery", "abv", "notes", "purchased_date", "tapped_date", "updated_at"],
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
