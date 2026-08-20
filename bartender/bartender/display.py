"""BarTender – read-only display server.

Runs on a separate port (default 8100) and shows only the current bar status
with no management controls or data-modification endpoints.
"""

import json
import os
from pathlib import Path

from flask import Flask, render_template

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_FILE = DATA_DIR / "bartender.json"

display_app = Flask(__name__, template_folder="templates")

DEFAULT_DATA = {
    "settings": {
        "measurement": "us",
        "theme": "light",
        "bar_name": "My Bar",
        "bar_stock_enabled": True,
    },
    "bar_stock": [],
    "kegs": [],
    "taps": [],
}


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


@display_app.route("/")
def index():
    data = load_data()
    return render_template(
        "display/index.html",
        settings=data["settings"],
        taps=data["taps"],
        kegs=data["kegs"],
        bar_stock=data["bar_stock"],
    )


@display_app.route("/menu")
def menu():
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

    return render_template(
        "display/menu.html",
        settings=data["settings"],
        on_tap=on_tap,
    )


if __name__ == "__main__":
    port = int(os.environ.get("DISPLAY_PORT", 8100))
    display_app.run(host="0.0.0.0", port=port, debug=False)
