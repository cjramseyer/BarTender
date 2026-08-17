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
    "settings": {"measurement": "us", "theme": "light", "bar_name": "My Bar"},
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
        return data
    return json.loads(json.dumps(DEFAULT_DATA))


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


if __name__ == "__main__":
    port = int(os.environ.get("DISPLAY_PORT", 8100))
    display_app.run(host="0.0.0.0", port=port, debug=False)
