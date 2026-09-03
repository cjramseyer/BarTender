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


def test_owner_can_add_custom_provider_via_api(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/pos/providers",
        json={
            "key": "square-sandbox",
            "name": "Square Sandbox",
            "mode": "static",
            "static_taps": [
                {
                    "number": 1,
                    "label": "Tap 1",
                    "item_name": "Pale Ale",
                    "serving_size": "16 oz",
                    "price_label": "$6.50",
                    "available": True,
                }
            ],
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["provider"]["key"] == "square-sandbox"
    assert any(item["key"] == "square-sandbox" for item in payload["providers"])


def test_owner_can_import_custom_providers(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/pos/providers/import",
        json={
            "providers": [
                {
                    "key": "toast-sandbox",
                    "name": "Toast Sandbox",
                    "mode": "static",
                    "static_taps": [
                        {
                            "number": 4,
                            "label": "Tap 4",
                            "item_name": "Porter",
                            "serving_size": "16 oz",
                            "price_label": "$7.00",
                            "available": True,
                        }
                    ],
                },
                {
                    "key": "lightspeed-demo",
                    "name": "Lightspeed Demo",
                    "mode": "static",
                    "static_taps": [],
                },
            ]
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["added_or_updated"] == 2
    keys = {item["key"] for item in payload["catalog"]}
    assert "toast-sandbox" in keys
    assert "lightspeed-demo" in keys


def test_imported_static_provider_can_be_selected_and_synced(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    import_response = client.post(
        "/api/pos/providers/import",
        json={
            "providers": [
                {
                    "key": "sample-static",
                    "name": "Sample Static",
                    "mode": "static",
                    "static_taps": [
                        {
                            "number": 9,
                            "label": "Static Tap 9",
                            "item_name": "Static IPA",
                            "serving_size": "16 oz",
                            "price_label": "$6.25",
                            "available": True,
                        }
                    ],
                }
            ]
        },
        headers=owner_headers,
    )
    assert import_response.status_code == 200

    settings_response = client.post(
        "/api/settings",
        json={
            "pos_sync_enabled": True,
            "pos_sync_provider": "sample-static",
        },
        headers=owner_headers,
    )
    assert settings_response.status_code == 200

    sync_response = client.post("/api/pos/sync/now", headers=owner_headers)
    assert sync_response.status_code == 200
    sync_payload = sync_response.get_json()
    assert sync_payload["ok"] is True
    assert sync_payload["status"]["provider"] == "sample-static"

    reloaded = app_module.load_data()
    tap = next(item for item in reloaded["taps"] if item["number"] == 9)
    assert tap["label"] == "Static Tap 9"
    assert tap["pos_sync"]["item_name"] == "Static IPA"


def test_provider_catalog_includes_requested_common_built_ins(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.get("/api/pos/providers", headers=owner_headers)

    assert response.status_code == 200
    payload = response.get_json()
    keys = {item["key"] for item in payload["providers"]}
    assert "toast" in keys
    assert "square" in keys
    assert "clover" in keys
    assert "lightspeed" in keys
    assert "arryved" in keys


def test_requested_built_in_providers_can_sync(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    for provider in ("toast", "square", "clover", "lightspeed", "arryved"):
        settings_response = client.post(
            "/api/settings",
            json={
                "pos_sync_enabled": True,
                "pos_sync_provider": provider,
            },
            headers=owner_headers,
        )
        assert settings_response.status_code == 200

        sync_response = client.post("/api/pos/sync/now", headers=owner_headers)
        assert sync_response.status_code == 200
        sync_payload = sync_response.get_json()
        assert sync_payload["ok"] is True
        assert sync_payload["status"]["provider"] == provider
        assert sync_payload["status"]["last_counts"]["items_received"] == 2


def test_adding_custom_provider_does_not_require_runtime_config_json(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/pos/providers",
        json={
            "key": "empty-static",
            "name": "Empty Static",
            "mode": "static",
            "static_taps": [],
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["provider"]["key"] == "empty-static"


def test_enabled_static_provider_requires_runtime_config_json(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    add_response = client.post(
        "/api/pos/providers",
        json={
            "key": "cfg-required",
            "name": "Config Required",
            "mode": "static",
            "static_taps": [],
        },
        headers=owner_headers,
    )
    assert add_response.status_code == 200

    settings_response = client.post(
        "/api/settings",
        json={
            "pos_sync_enabled": True,
            "pos_sync_provider": "cfg-required",
        },
        headers=owner_headers,
    )

    assert settings_response.status_code == 400
    payload = settings_response.get_json()
    assert "Provider configuration JSON is required" in payload["error"]


def test_enabled_static_provider_accepts_valid_runtime_config_json(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    add_response = client.post(
        "/api/pos/providers",
        json={
            "key": "cfg-json",
            "name": "Config JSON Provider",
            "mode": "static",
            "static_taps": [],
        },
        headers=owner_headers,
    )
    assert add_response.status_code == 200

    settings_response = client.post(
        "/api/settings",
        json={
            "pos_sync_enabled": True,
            "pos_sync_provider": "cfg-json",
            "pos_sync_provider_config_json": '{"static_taps":[{"number":12,"label":"Cfg Tap","item_name":"Config Stout","serving_size":"16 oz","price_label":"$6.75","available":true}]}'
        },
        headers=owner_headers,
    )
    assert settings_response.status_code == 200

    sync_response = client.post("/api/pos/sync/now", headers=owner_headers)
    assert sync_response.status_code == 200
    payload = sync_response.get_json()
    assert payload["ok"] is True
    assert payload["status"]["provider"] == "cfg-json"

    reloaded = app_module.load_data()
    tap = next(item for item in reloaded["taps"] if item["number"] == 12)
    assert tap["label"] == "Cfg Tap"
