import os
import sys
import uuid
from datetime import datetime, timezone
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
    app_module.app.config["SECRET_KEY"] = "test-secret"
    return app_module


def _seed_tap_and_keg(app_module, pour_mode="inline_device"):
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    data["settings"]["pour_mode"] = pour_mode
    data["kegs"] = [
        {
            "id": 1,
            "name": "Test Keg",
            "status": "full",
            "filled_date": datetime.now(timezone.utc).date().isoformat(),
            "current_volume": 5,
            "volume_unit": "gal",
            "percent_full": 100,
        }
    ]
    data["taps"] = [
        {"id": 1, "number": 1, "label": "Tap 1", "keg_id": 1}
    ]
    app_module.save_data(data)


def _owner_headers():
    return {
        "X-BarTender-User-Id": "owner",
        "X-BarTender-Role": "owner",
        "X-BarTender-Name": "Owner",
    }


def test_inline_device_pour_mode_requires_pro_profile(tmp_path):
    app_module = _load_app_module(tmp_path)

    assert app_module._normalize_pour_mode("inline_device", "homebrewer") == "manual"
    assert app_module._normalize_pour_mode("inline_device", "pro") == "inline_device"


def _register_device(client):
    response = client.post(
        "/api/inline-devices",
        json={"name": "Cold room tap 1", "tap_id": 1},
        headers=_owner_headers(),
    )
    assert response.status_code == 201
    return response.get_json()


def test_inline_device_event_is_atomic_and_idempotent(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 1000,
        "unit": "ml",
    }
    headers = {"Authorization": f"Bearer {registered['token']}"}
    event_url = f"/api/inline-devices/{registered['device']['id']}/events"

    first_response = client.post(event_url, json=payload, headers=headers)
    duplicate_response = client.post(event_url, json=payload, headers=headers)

    assert first_response.status_code == 200
    assert first_response.get_json()["duplicate"] is False
    assert duplicate_response.status_code == 200
    assert duplicate_response.get_json()["duplicate"] is True

    data = app_module.load_data()
    assert data["kegs"][0]["status"] == "in_use"
    assert data["kegs"][0]["current_volume"] < 5
    assert len(data["pour_events"]) == 1
    assert data["pour_events"][0]["source"] == "inline_device"
    assert sum(entry["action"] == "pour_recorded" for entry in data["team_audit"]) == 1


def test_inline_device_token_is_required_and_not_listed(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    device_id = registered["device"]["id"]
    payload = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    unauthorized = client.post(
        f"/api/inline-devices/{device_id}/events", json=payload
    )
    listed = client.get("/api/inline-devices", headers=_owner_headers())

    assert unauthorized.status_code == 401
    assert listed.status_code == 200
    assert "token" not in listed.get_json()["devices"][0]
    assert "token_hash" not in listed.get_json()["devices"][0]
    assert len(app_module.load_data()["pour_events"]) == 0


def test_inline_device_rejects_invalid_and_conflicting_events(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    event_url = f"/api/inline-devices/{registered['device']['id']}/events"
    headers = {"Authorization": f"Bearer {registered['token']}"}
    event_id = str(uuid.uuid4())
    payload = {
        "event_id": event_id,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    invalid = client.post(event_url, json={**payload, "amount": -1}, headers=headers)
    device_status = client.get("/api/inline-devices", headers=_owner_headers())
    accepted = client.post(event_url, json=payload, headers=headers)
    conflict = client.post(
        event_url, json={**payload, "amount": 600}, headers=headers
    )

    assert invalid.status_code == 400
    assert device_status.get_json()["devices"][0]["last_error"] == (
        "amount must be a finite number greater than zero."
    )
    assert accepted.status_code == 200
    assert conflict.status_code == 409
    data = app_module.load_data()
    assert len(data["pour_events"]) == 1
    assert sum(entry["action"] == "pour_recorded" for entry in data["team_audit"]) == 1


def test_inline_device_requires_inline_mode_and_assigned_keg(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module, pour_mode="manual")
    client = app_module.app.test_client()
    registered = _register_device(client)
    event_url = f"/api/inline-devices/{registered['device']['id']}/events"
    headers = {"Authorization": f"Bearer {registered['token']}"}
    payload = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    response = client.post(event_url, json=payload, headers=headers)

    assert response.status_code == 409
    assert response.get_json()["code"] == "INLINE_DEVICE_MODE_DISABLED"
    data = app_module.load_data()
    assert data["kegs"][0]["current_volume"] == 5
    assert data["pour_events"] == []


def test_inline_device_route_uses_device_token_on_external_listener(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    app_module.EXTERNAL_API_MODE = True

    response = client.post(
        f"/api/inline-devices/{registered['device']['id']}/heartbeat",
        headers={"Authorization": f"Bearer {registered['token']}"},
    )
    app_module.EXTERNAL_API_MODE = False
    device_status = client.get("/api/inline-devices", headers=_owner_headers())

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert device_status.get_json()["devices"][0]["health"] == "online"


def test_inline_device_event_rejects_unknown_or_unavailable_keg(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    event_url = f"/api/inline-devices/{registered['device']['id']}/events"
    headers = {"Authorization": f"Bearer {registered['token']}"}
    payload = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    unknown = client.post(
        f"/api/inline-devices/{uuid.uuid4()}/events",
        json=payload,
        headers=headers,
    )
    data = app_module.load_data()
    data["kegs"][0]["status"] = "cleaning"
    app_module.save_data(data)
    unavailable = client.post(event_url, json=payload, headers=headers)

    assert unknown.status_code == 404
    assert unavailable.status_code == 409
    assert unavailable.get_json()["code"] == "KEG_NOT_AVAILABLE"
    data = app_module.load_data()
    assert data["kegs"][0]["current_volume"] == 5
    assert data["pour_events"] == []


def test_inline_device_replay_ledger_survives_analytics_reset(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    event_url = f"/api/inline-devices/{registered['device']['id']}/events"
    headers = {"Authorization": f"Bearer {registered['token']}"}
    payload = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    accepted = client.post(event_url, json=payload, headers=headers)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    reset = client.post("/api/analytics/reset")
    duplicate = client.post(event_url, json=payload, headers=headers)

    assert accepted.status_code == 200
    assert reset.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.get_json()["duplicate"] is True
    data = app_module.load_data()
    assert data["pour_events"] == []
    assert len(data["inline_device_events"]) == 1


def test_inline_device_links_and_replay_records_are_not_exported(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    data = app_module.load_data()
    data["inline_device_events"].append({"device_id": registered["device"]["id"], "event_id": "retained"})

    exported = app_module._build_export_json_payload(data)["data"]
    restored = app_module._apply_import_payload(data, exported, "replace")

    assert "inline_devices" not in exported
    assert "inline_device_events" not in exported
    assert restored["inline_devices"][0]["id"] == registered["device"]["id"]
    assert restored["inline_device_events"][0]["event_id"] == "retained"


def test_staff_cannot_manage_inline_devices(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    staff_headers = {
        "X-BarTender-User-Id": "staff-1",
        "X-BarTender-Role": "staff",
    }

    response = client.get("/api/inline-devices", headers=staff_headers)

    assert response.status_code == 403


def test_inline_device_management_and_panel_are_hidden_for_non_pro(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    response = client.post(
        "/api/inline-devices",
        json={"name": "Device", "tap_id": 1},
        headers=_owner_headers(),
    )
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    settings_page = client.get("/settings")

    assert response.status_code == 403
    assert response.get_json()["code"] == "INLINE_DEVICE_PRO_REQUIRED"
    assert settings_page.status_code == 200
    assert 'id="inlineDevicesSettings"' not in settings_page.get_data(as_text=True)


def test_existing_device_cannot_record_pours_after_profile_downgrade(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    app_module.save_data(data)
    payload = {
        "event_id": str(uuid.uuid4()),
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "amount": 500,
        "unit": "ml",
    }

    response = client.post(
        f"/api/inline-devices/{registered['device']['id']}/events",
        json=payload,
        headers={"Authorization": f"Bearer {registered['token']}"},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "INLINE_DEVICE_PRO_REQUIRED"
    assert app_module.load_data()["pour_events"] == []


def test_settings_links_devices_without_rendering_credentials(tmp_path):
    app_module = _load_app_module(tmp_path)
    _seed_tap_and_keg(app_module)
    client = app_module.app.test_client()
    registered = _register_device(client)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/settings")
    body = response.get_data(as_text=True)
    firmware = client.get("/static/firmware/inline_device_esp32.ino")

    assert response.status_code == 200
    assert "Inline Devices" in body
    assert "/static/firmware/inline_device_esp32.ino" in body
    assert registered["device"]["name"] in body
    assert registered["token"] not in body
    assert "token_hash" not in body
    assert firmware.status_code == 200
    assert b"Paste the one-time Bearer token from Settings -> Inline Devices" in firmware.data
    assert b'const char DEVICE_TOKEN[] = "PASTE_DEVICE_TOKEN_HERE";' in firmware.data