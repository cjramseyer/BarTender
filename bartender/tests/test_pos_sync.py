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


def test_pos_sync_defaults_exist_in_settings(tmp_path):
    app_module = _load_app_module(tmp_path)

    settings = app_module.load_data()["settings"]

    assert settings["pos_sync_enabled"] is False
    assert settings["pos_sync_provider"] == ""
    assert settings["pos_sync_last_status"] == "never"
    assert settings["pos_sync_last_counts"]["items_received"] == 0


def test_owner_can_save_pos_sync_settings(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/settings",
        json={
            "pos_sync_enabled": True,
            "pos_sync_provider": "mock",
            "pos_sync_credentials": {
                "api_key": "demo-key",
                "location_id": "loc-1",
                "merchant_id": "m-1",
            },
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["pos_sync_enabled"] is True
    assert payload["pos_sync_provider"] == "mock"
    assert payload["pos_sync_credentials"]["location_id"] == "loc-1"


def test_pos_sync_status_endpoint_returns_status(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.get("/api/pos/sync/status", headers=owner_headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["enabled"] is False
    assert payload["last_status"] == "never"


def test_pos_sync_now_updates_mapped_taps_with_mock_provider(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    data = app_module.load_data()
    data["taps"] = [{"id": 1, "number": 1, "label": "Original Tap", "keg_id": None, "notes": ""}]
    app_module.save_data(data)

    settings_response = client.post(
        "/api/settings",
        json={
            "pos_sync_enabled": True,
            "pos_sync_provider": "mock",
        },
        headers=owner_headers,
    )
    assert settings_response.status_code == 200

    response = client.post("/api/pos/sync/now", headers=owner_headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"]["last_status"] == "success"
    assert payload["status"]["last_counts"]["items_received"] == 2

    reloaded = app_module.load_data()
    taps_by_number = {tap["number"]: tap for tap in reloaded["taps"]}
    assert taps_by_number[1]["label"] == "POS Tap 1"
    assert taps_by_number[1]["pos_sync"]["item_name"] == "Mock IPA"
    assert taps_by_number[2]["label"] == "POS Tap 2"


def test_pos_sync_now_returns_actionable_error_when_disabled(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    data = app_module.load_data()
    data["taps"] = [{"id": 7, "number": 7, "label": "Manual Tap", "keg_id": None, "notes": ""}]
    app_module.save_data(data)

    response = client.post("/api/pos/sync/now", headers=owner_headers)

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["ok"] is False
    assert "Enable POS sync" in payload["hint"]
    assert payload["status"]["last_status"] == "failed"

    reloaded = app_module.load_data()
    assert reloaded["taps"][0]["label"] == "Manual Tap"
