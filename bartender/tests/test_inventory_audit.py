import os
import sys
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
    app_module.save_data(app_module.DEFAULT_DATA)
    app_module.app.config["TESTING"] = True
    return app_module


def test_inventory_mutations_are_recorded_in_the_audit_trail(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    headers = {
        "X-BarTender-User-Id": "owner",
        "X-BarTender-Role": "owner",
        "X-BarTender-Name": "Owner",
    }

    data = app_module.load_data()
    data["settings"]["brewery_type"] = "commercial"
    data["beers"] = [{"id": 1, "name": "House IPA", "packaging": "kegged"}]
    app_module.save_data(data)

    stock = client.post(
        "/api/stock",
        json={"name": "Tonic", "category": "Mixer", "quantity": 12, "unit": "bottles"},
        headers=headers,
    ).get_json()
    assert client.put(
        f"/api/stock/{stock['id']}",
        json={"quantity": 8},
        headers=headers,
    ).status_code == 200
    assert client.delete(f"/api/stock/{stock['id']}", headers=headers).status_code == 200

    keg = client.post(
        "/api/kegs",
        json={"name": "Keg One", "status": "empty"},
        headers=headers,
    ).get_json()
    assert client.put(
        f"/api/kegs/{keg['id']}",
        json={"notes": "Updated configuration"},
        headers=headers,
    ).status_code == 200
    assert client.post(
        f"/api/kegs/{keg['id']}/fill",
        json={"beer_id": 1},
        headers=headers,
    ).status_code == 200
    assert client.put(
        f"/api/kegs/{keg['id']}",
        json={"status": "cleaning"},
        headers=headers,
    ).status_code == 200
    assert client.post(f"/api/kegs/{keg['id']}/clean", headers=headers).status_code == 200
    assert client.delete(f"/api/kegs/{keg['id']}", headers=headers).status_code == 200
    assert client.post(
        "/api/kegs/bulk",
        json={"items": [{"name": "Keg Two"}, {"name": "Keg Three"}]},
        headers=headers,
    ).status_code == 201

    tap = client.post(
        "/api/taps",
        json={"number": 1, "label": "Main Tap"},
        headers=headers,
    ).get_json()
    assert client.put(
        f"/api/taps/{tap['id']}",
        json={"label": "Updated Tap"},
        headers=headers,
    ).status_code == 200
    assert client.delete(f"/api/taps/{tap['id']}", headers=headers).status_code == 200
    assert client.post(
        "/api/taps/bulk",
        json={"items": [{"number": 2}, {"number": 3}]},
        headers=headers,
    ).status_code == 201

    actions = {entry["action"] for entry in app_module.load_data()["team_audit"]}
    assert {
        "stock_created",
        "stock_updated",
        "stock_deleted",
        "keg_created",
        "keg_updated",
        "keg_filled",
        "keg_cleaned",
        "keg_deleted",
        "kegs_bulk_created",
        "tap_created",
        "tap_updated",
        "tap_deleted",
        "taps_bulk_created",
    } <= actions