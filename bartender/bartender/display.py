"""BarTender – read-only display server.

Runs on a separate port (default 8100) and shows only the current bar status
with no management controls or data-modification endpoints.
"""

import json
import os
from pathlib import Path

from flask import Flask, render_template, send_file, request

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "bartender.json"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGO_FILENAME_PREFIX = "bar-logo"

display_app = Flask(__name__, template_folder="templates")

DEFAULT_DATA = {
    "settings": {
        "measurement": "us",
        "theme": "light",
        "bar_name": "My Bar",
        "bar_logo_url": "",
        "bar_stock_enabled": True,
    },
    "beers": [],
    "bar_stock": [],
    "kegs": [],
    "taps": [],
}


def _coerce_int(value, fallback=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _coerce_bool(value, fallback: bool = False) -> bool:
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    val_str = str(value).strip().lower()
    if val_str in ("true", "1", "yes", "on"):
        return True
    if val_str in ("false", "0", "no", "off"):
        return False
    return fallback


def _build_on_deck_kegs(data: dict) -> list[dict]:
    return [
        keg
        for keg in data.get("kegs", [])
        if isinstance(keg, dict)
        and _coerce_bool(keg.get("on_deck"), False)
        and not _coerce_bool(keg.get("line_cleaning_keg"), False)
    ]


def _normalize_brewery_type(value) -> str:
    normalized = str(value or "homebrewer").strip().lower()
    if normalized == "commercial":
        return "pro"
    if normalized in ("homebrewer", "pro"):
        return normalized
    return "homebrewer"


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


def load_data() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, value in DEFAULT_DATA.items():
            if key not in data:
                data[key] = value
        if not isinstance(data.get("settings"), dict):
            data["settings"] = json.loads(json.dumps(DEFAULT_DATA["settings"]))
        else:
            for key, value in DEFAULT_DATA["settings"].items():
                data["settings"].setdefault(key, value)
        return data
    return json.loads(json.dumps(DEFAULT_DATA))


def _default_percent_for_status(status: str) -> int:
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


def _normalize_beer_packaging(value) -> str:
    packaging = str(value or "kegged").strip().lower().replace("/", "_")
    if packaging in ("bottled", "bottle", "can", "canned", "bottled_can"):
        return "bottled_can"
    return "kegged"


def _get_uploaded_logo_file_path() -> Path | None:
    if not UPLOADS_DIR.exists():
        return None
    candidates = sorted(
        UPLOADS_DIR.glob(f"{LOGO_FILENAME_PREFIX}.*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


@display_app.route("/")
def index():
    data = load_data()
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
    selected_taps = set(assignments[selected_display_index - 1]) if selected_display_index <= len(assignments) else set()

    taps = data.get("taps", [])
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
            taps = [tap for tap in taps if _coerce_int(tap.get("number"), None) in selected_taps]
        else:
            taps = []

    return render_template(
        "display/index.html",
        settings=data["settings"],
        taps=taps,
        kegs=data.get("kegs", []),
        bar_stock=data.get("bar_stock", []),
        on_deck_kegs=on_deck_kegs,
        selected_display_index=selected_display_index,
        show_taps=show_taps,
        show_on_deck=show_on_deck,
        show_bar_stock=show_bar_stock,
    )


@display_app.route("/menu")
def menu():
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

    return render_template(
        "display/menu.html",
        settings=data["settings"],
        on_tap=on_tap,
        packaged_beers=packaged_beers,
    )


@display_app.route("/media/bar-logo")
def media_bar_logo():
    logo_path = _get_uploaded_logo_file_path()
    if logo_path is None or not logo_path.exists():
        return "", 404
    return send_file(logo_path)


if __name__ == "__main__":
    port = int(os.environ.get("DISPLAY_PORT", 8100))
    display_app.run(host="0.0.0.0", port=port, debug=False)
