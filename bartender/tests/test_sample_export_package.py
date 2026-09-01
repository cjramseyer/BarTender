import json
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _load_app_module(tmp_path):
    os.environ["DATA_DIR"] = str(tmp_path)
    import importlib

    import bartender.app as app_module

    importlib.reload(app_module)
    app_module.DATA_DIR = tmp_path
    app_module.DATA_FILE = tmp_path / "bartender.json"
    app_module.UPLOADS_DIR = tmp_path / "uploads"
    app_module.UPLOADS_DIR.mkdir(exist_ok=True)
    app_module.app.config["TESTING"] = True
    app_module.app.config["SECRET_KEY"] = "test-secret"
    return app_module


def test_sample_export_package_round_trips_live_archive_format(tmp_path):
    app_module = _load_app_module(tmp_path)
    sample_source = Path(__file__).resolve().parents[1] / "sample_data" / "demo_bar_data.json"

    with open(sample_source, "r", encoding="utf-8") as handle:
        sample_data = json.load(handle)

    app_module.save_data(sample_data)
    normalized = app_module.load_data()
    archive = app_module._build_export_archive(normalized)

    imported = app_module._import_archive_payload(archive)

    assert imported is not None
    assert app_module._import_summary(imported) == {
        "bar_name": "Harbor Tap Demo",
        "kegs": 3,
        "taps": 2,
        "beers": 3,
        "bar_stock": 2,
    }

    archive_path = Path(tmp_path) / "bartender_sample_export.zip"
    archive_path.write_bytes(archive)

    with zipfile.ZipFile(archive_path, mode="r") as zf:
        assert sorted(zf.namelist()) == sorted([
            "bar_stock.csv",
            "bar_stock.json",
            "bartender_export.json",
            "beers.csv",
            "beers.json",
            "kegs.csv",
            "kegs.json",
            "pour_events.json",
            "settings.json",
            "taps.csv",
            "taps.json",
        ])

        payload = json.loads(zf.read("bartender_export.json").decode("utf-8"))
        assert payload["format"] == "bartender-export"
        assert payload["version"] == 1
        assert payload["data"]["settings"]["bar_name"] == "Harbor Tap Demo"