import os
import sys
import json
import logging
from datetime import datetime, timedelta, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

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


def test_team_users_are_seeded_for_owner_access(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    response = client.get(
        "/api/team/users",
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert any(user["role"] == "owner" for user in payload["users"])


def test_staff_cannot_manage_users(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    response = client.post(
        "/api/team/users",
        json={"name": "Alice Manager", "role": "manager"},
        headers={"X-BarTender-User-Id": "staff-1", "X-BarTender-Role": "staff"},
    )

    assert response.status_code == 403
    payload = response.get_json()
    assert payload["error"] == "Insufficient permissions"


def test_staff_cannot_access_settings_page_or_api(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    staff_headers = {"X-BarTender-User-Id": "staff-1", "X-BarTender-Role": "staff"}

    with client.session_transaction() as sess:
        sess["user_id"] = "staff-1"
        sess["user_role"] = "staff"
        sess["user_name"] = "Staff One"

    page_response = client.get("/settings")
    assert page_response.status_code == 403

    api_response = client.get("/api/settings", headers=staff_headers)
    assert api_response.status_code == 403
    assert api_response.get_json()["error"] == "Insufficient permissions"


def test_staff_can_view_audit_events(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    staff_headers = {"X-BarTender-User-Id": "staff-1", "X-BarTender-Role": "staff"}

    audit_response = client.get("/api/team/audit", headers=staff_headers)
    assert audit_response.status_code == 200
    assert audit_response.get_json() == {"audit": []}

    with client.session_transaction() as session:
        session["user_id"] = "staff-1"
        session["user_name"] = "Staff One"
        session["user_role"] = "staff"
    page_response = client.get("/audit", headers=staff_headers)
    assert page_response.status_code == 200
    assert "Audit Log" in page_response.get_data(as_text=True)


def test_audit_export_and_clear_permissions(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["team_audit"] = [{"id": "1", "action": "settings_updated"}]
    data["settings"]["owner_pin"] = "2468"
    app_module.save_data(data)

    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}
    manager_headers = {"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"}
    staff_headers = {"X-BarTender-User-Id": "staff-1", "X-BarTender-Role": "staff"}

    manager_export = client.get("/api/team/audit/export", headers=manager_headers)
    assert manager_export.status_code == 200
    assert "attachment;" in manager_export.headers["Content-Disposition"]
    assert manager_export.get_json() == [{"id": "1", "action": "settings_updated"}]

    assert client.get("/api/team/audit/export", headers=staff_headers).status_code == 403
    assert client.post("/api/team/audit/clear", headers=manager_headers).status_code == 403
    assert client.post("/api/team/audit/clear", headers=owner_headers).status_code == 403
    assert client.post(
        "/api/team/audit/clear",
        json={"owner_pin": "0000"},
        headers=owner_headers,
    ).status_code == 403

    owner_clear = client.post(
        "/api/team/audit/clear",
        json={"owner_pin": "2468"},
        headers=owner_headers,
    )
    assert owner_clear.status_code == 200
    assert owner_clear.get_json() == {"cleared": 1}
    assert app_module.load_data()["team_audit"] == []


def test_settings_changes_are_audited(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}
    response = client.post(
        "/api/settings",
        json={"bar_name": "Team Bar"},
        headers=owner_headers,
    )

    assert response.status_code == 200
    audit_response = client.get(
        "/api/team/audit",
        headers=owner_headers,
    )
    assert audit_response.status_code == 200
    payload = audit_response.get_json()
    settings_event = next(entry for entry in payload["audit"] if entry["action"] == "settings_updated")
    assert settings_event["details"] == {
        "changed_fields": ["bar_name", "setup_completed"],
    }


def test_unchanged_settings_do_not_create_an_audit_event(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}
    initial_bar_name = app_module.load_data()["settings"]["bar_name"]

    response = client.post(
        "/api/settings",
        json={"bar_name": initial_bar_name},
        headers=owner_headers,
    )

    assert response.status_code == 200
    assert app_module.load_data()["team_audit"] == []


def test_anonymous_telemetry_is_owner_only_and_sent_once_daily(tmp_path, caplog):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}
    manager_headers = {"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"}

    assert client.post(
        "/api/settings",
        json={"anonymous_telemetry_enabled": True},
        headers=manager_headers,
    ).status_code == 403

    data = app_module.load_data()
    data["settings"]["anonymous_telemetry_enabled"] = True
    app_module.save_data(data)

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    caplog.set_level(logging.INFO, logger=app_module.app.logger.name)
    with patch.object(app_module, "urlopen", return_value=Response()) as urlopen_mock:
        app_module._send_anonymous_telemetry_heartbeat()
        app_module._send_anonymous_telemetry_heartbeat()

    assert urlopen_mock.call_count == 1
    assert "Anonymous telemetry heartbeat sent." in caplog.messages
    assert "Anonymous telemetry heartbeat skipped: already sent today." in caplog.messages
    request_payload = urlopen_mock.call_args.args[0]
    assert request_payload.get_header("User-agent") == f"BarTender/{app_module.APP_VERSION}"
    assert json.loads(request_payload.data) == {
        "installation_id": app_module.load_data()["settings"]["anonymous_telemetry_installation_id"],
        "app_version": app_module.APP_VERSION,
        "addon_version": app_module.APP_VERSION,
        "brewery_type": "homebrewer",
    }
    assert app_module.load_data()["settings"]["anonymous_telemetry_last_heartbeat_date"]


def test_anonymous_telemetry_logs_cloudflare_rejection(tmp_path, caplog):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["anonymous_telemetry_enabled"] = True
    app_module.save_data(data)

    caplog.set_level(logging.WARNING, logger=app_module.app.logger.name)
    with patch.object(
        app_module,
        "urlopen",
        side_effect=HTTPError("https://example.invalid", 503, "Unavailable", Message(), None),
    ):
        app_module._send_anonymous_telemetry_heartbeat()

    assert "Anonymous telemetry heartbeat rejected with HTTP status 503." in caplog.messages
    settings = app_module.load_data()["settings"]
    assert settings["anonymous_telemetry_last_heartbeat_date"] == ""


def test_manager_cannot_change_bar_name_or_api_tokens(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    manager_headers = {"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"}

    bar_name_response = client.post(
        "/api/settings",
        json={"bar_name": "Manager Bar"},
        headers=manager_headers,
    )
    assert bar_name_response.status_code == 403
    assert bar_name_response.get_json()["error"] == "Insufficient permissions"

    token_response = client.post(
        "/api/settings",
        json={"external_api_write_token": "super-secret"},
        headers=manager_headers,
    )
    assert token_response.status_code == 403
    assert token_response.get_json()["error"] == "Insufficient permissions"

    audit_retention_response = client.post(
        "/api/settings",
        json={"audit_retention_days": 90},
        headers=manager_headers,
    )
    assert audit_retention_response.status_code == 403
    assert audit_retention_response.get_json()["error"] == "Insufficient permissions"


def test_owner_can_change_bar_name_and_api_tokens(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/settings",
        json={
            "bar_name": "Owner Bar",
            "external_api_read_token": "read-token",
            "external_api_write_token": "write-token",
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["bar_name"] == "Owner Bar"
    assert payload["external_api_read_token"] == "read-token"
    assert payload["external_api_write_token"] == "write-token"


def test_default_keg_type_prefers_corny_and_normalizes_full_size_label(tmp_path):
    app_module = _load_app_module(tmp_path)

    data = app_module.load_data()

    assert data["settings"]["default_keg_type"] == "Corny (5 gal)"
    assert data["settings"]["keg_type_choices"][0] == "Corny (5 gal)"
    assert "Full Size (1/2 bbl, 15.5 gal)" in data["settings"]["keg_type_choices"]

    data["settings"]["keg_type_choices"] = ["1/2 bbl (15.5 gal)", "Corny (5 gal)"]
    data["settings"]["default_keg_type"] = "1/2 bbl (15.5 gal)"
    app_module.save_data(data)

    reloaded = app_module.load_data()

    assert reloaded["settings"]["keg_type_choices"][0] == "Full Size (1/2 bbl, 15.5 gal)"
    assert reloaded["settings"]["default_keg_type"] == "Full Size (1/2 bbl, 15.5 gal)"


def test_brewery_type_defaults_to_homebrewer_and_normalizes_valid_values(tmp_path):
    app_module = _load_app_module(tmp_path)

    data = app_module.load_data()

    assert data["settings"]["brewery_type"] == "homebrewer"

    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    reloaded = app_module.load_data()
    assert reloaded["settings"]["brewery_type"] == "pro"

    # Backward compatibility for legacy commercial setting
    data["settings"]["brewery_type"] = "commercial"
    app_module.save_data(data)
    reloaded = app_module.load_data()
    assert reloaded["settings"]["brewery_type"] == "pro"

    data["settings"]["brewery_type"] = "unsupported"
    app_module.save_data(data)
    assert app_module.load_data()["settings"]["brewery_type"] == "homebrewer"


def test_on_tap_display_title_defaults_to_on_draft_and_saves(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    data = app_module.load_data()
    assert data["settings"]["display_title_on_tap"] == "On Draft"
    assert data["settings"]["display_full_width"] is False

    response = client.post(
        "/api/settings",
        json={"display_title_on_tap": "Draft List", "display_full_width": True},
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["display_title_on_tap"] == "Draft List"
    assert payload["display_full_width"] is True
    assert app_module.load_data()["settings"]["display_title_on_tap"] == "Draft List"
    assert app_module.load_data()["settings"]["display_full_width"] is True


def test_pro_display_count_and_tap_assignments_save(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/settings",
        json={
            "brewery_type": "pro",
            "display_count": 2,
            "display_tap_assignments": [[1, 2], [3, 4]],
        },
        headers=owner_headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["display_count"] == 2
    assert payload["display_tap_assignments"] == [[1, 2], [3, 4]]
    assert app_module.load_data()["settings"]["display_tap_assignments"] == [[1, 2], [3, 4]]


def test_pos_pour_mode_is_forbidden_for_homebrewer_settings(tmp_path):
    app_module = _load_app_module(tmp_path)

    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    data["settings"]["pour_mode"] = "pos"
    app_module.save_data(data)
    reloaded = app_module.load_data()
    assert reloaded["settings"]["brewery_type"] == "homebrewer"
    assert reloaded["settings"]["pour_mode"] == "manual"

    data["settings"]["brewery_type"] = "pro"
    data["settings"]["pour_mode"] = "pos"
    app_module.save_data(data)
    assert app_module.load_data()["settings"]["pour_mode"] == "pos"


def test_pos_system_is_available_only_for_pro_pos_mode(tmp_path):
    app_module = _load_app_module(tmp_path)

    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    data["settings"]["pour_mode"] = "pos"
    data["settings"]["pos_system"] = "toast"
    app_module.save_data(data)
    reloaded = app_module.load_data()
    assert reloaded["settings"]["brewery_type"] == "pro"
    assert reloaded["settings"]["pour_mode"] == "pos"
    assert reloaded["settings"]["pos_system"] == "Toast"

    data["settings"]["brewery_type"] = "homebrewer"
    data["settings"]["pour_mode"] = "pos"
    data["settings"]["pos_system"] = "Toast"
    app_module.save_data(data)
    assert app_module.load_data()["settings"]["pour_mode"] == "manual"
    assert app_module.load_data()["settings"]["pos_system"] == ""


def test_homebrewer_limits_taps_and_kegs(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    app_module.save_data({
        "settings": {
            **app_module.DEFAULT_DATA["settings"],
            "brewery_type": "homebrewer",
        },
        "beers": [],
        "kegs": [],
        "taps": [],
        "pour_events": [],
        "team_users": [{"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"}],
        "team_audit": [],
    })

    for i in range(12):
        response = client.post(
            "/api/taps",
            json={"number": i + 1},
            headers=owner_headers,
        )
        assert response.status_code == 201

    response = client.post(
        "/api/taps",
        json={"number": 13},
        headers=owner_headers,
    )
    assert response.status_code == 409
    assert "12" in response.get_json()["error"]

    for i in range(20):
        response = client.post(
            "/api/kegs",
            json={"name": f"Keg {i + 1}", "status": "empty"},
            headers=owner_headers,
        )
        assert response.status_code == 201

    response = client.post(
        "/api/kegs",
        json={"name": "Keg 21", "status": "empty"},
        headers=owner_headers,
    )
    assert response.status_code == 409
    assert "20" in response.get_json()["error"]


def test_default_pour_preset_prefers_pint_and_includes_taste(tmp_path):
    app_module = _load_app_module(tmp_path)

    data = app_module.load_data()

    assert data["settings"]["default_pour_preset"] == "16|oz|Pint"
    assert data["settings"]["pour_options"] == [
        {"name": "Pint", "amount": 16, "unit": "oz"},
        {"name": "Half Pint", "amount": 8, "unit": "oz"},
        {"name": "Growler", "amount": 64, "unit": "oz"},
        {"name": "Taste", "amount": 2, "unit": "oz"},
    ]

    data["settings"]["pour_options"] = [
        {"name": "Half Pint", "amount": 8, "unit": "oz"},
        {"name": "Pint", "amount": 16, "unit": "oz"},
        {"name": "Growler", "amount": 64, "unit": "oz"},
        {"name": "Taste", "amount": 2, "unit": "oz"},
    ]
    data["settings"]["default_pour_preset"] = ""
    app_module.save_data(data)

    reloaded = app_module.load_data()

    assert reloaded["settings"]["default_pour_preset"] == "16|oz|Pint"


def test_owner_can_reset_settings_without_clearing_inventory_or_team(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": {
            **app_module.DEFAULT_DATA["settings"],
            "bar_name": "Busy Bar",
            "theme": "dark",
            "owner_pin": "2468",
            "setup_completed": True,
            "default_keg_type": "Full Size (1/2 bbl, 15.5 gal)",
            "default_pour_preset": "8|oz|Half Pint",
        },
        "beers": [{"id": 1, "name": "Amber Ale"}],
        "kegs": [{"id": 1, "name": "Keg 1"}],
        "taps": [{"id": 1, "name": "Tap 1"}],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    response = client.post(
        "/api/settings/reset",
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["bar_name"] == "My Bar"
    assert payload["theme"] == "light"
    assert payload["setup_completed"] is False
    assert payload["default_keg_type"] == "Corny (5 gal)"
    assert payload["default_pour_preset"] == "16|oz|Pint"

    reloaded = app_module.load_data()
    assert len(reloaded["beers"]) == 1
    assert reloaded["beers"][0]["id"] == 1
    assert reloaded["beers"][0]["name"] == "Amber Ale"
    assert len(reloaded["kegs"]) == 1
    assert reloaded["kegs"][0]["id"] == 1
    assert reloaded["kegs"][0]["name"] == "Keg 1"
    assert len(reloaded["taps"]) == 1
    assert reloaded["taps"][0]["id"] == 1
    assert reloaded["taps"][0]["name"] == "Tap 1"
    assert len(reloaded["team_users"]) == 2
    assert reloaded["team_audit"][0]["action"] == "settings_reset"


def test_owner_can_factory_reset_all_data(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    uploaded_logo = app_module.UPLOADS_DIR / "bar-logo.png"
    uploaded_logo.write_bytes(b"png")

    app_module.save_data({
        "settings": {
            **app_module.DEFAULT_DATA["settings"],
            "bar_name": "Busy Bar",
            "theme": "dark",
            "bar_logo_url": "media/bar-logo?v=123",
            "owner_pin": "2468",
            "setup_completed": True,
        },
        "beers": [{"id": 1, "name": "Amber Ale"}],
        "kegs": [{"id": 1, "name": "Keg 1"}],
        "taps": [{"id": 1, "name": "Tap 1"}],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [{"id": "1", "action": "settings_updated"}],
    })

    response = client.post(
        "/api/reset",
        json={"confirmation": "RESET ALL DATA"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True

    reloaded = app_module.load_data()
    assert reloaded["settings"]["bar_name"] == "My Bar"
    assert reloaded["settings"]["setup_completed"] is False
    assert reloaded["beers"] == []
    assert reloaded["kegs"] == []
    assert reloaded["taps"] == []
    assert len(reloaded["team_users"]) == 1
    assert reloaded["team_users"][0]["role"] == "owner"
    assert reloaded["team_audit"] == []
    assert not uploaded_logo.exists()


def test_first_created_user_defaults_to_owner(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({"settings": app_module.DEFAULT_DATA["settings"], "team_users": [], "team_audit": []})

    response = client.post(
        "/api/team/users",
        json={"name": "First Operator", "role": "staff"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["user"]["role"] == "owner"
    assert payload["user"]["id"] == "owner"


def test_manager_can_update_member_role_but_not_owner(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": app_module.DEFAULT_DATA["settings"],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
            {"id": "staff-1", "name": "Staff One", "role": "staff", "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    response = client.post(
        "/api/team/users",
        json={"action": "update", "user_id": "staff-1", "role": "manager"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert response.status_code == 200
    assert response.get_json()["user"]["role"] == "manager"

    allowed_manager_update = client.post(
        "/api/team/users",
        json={"action": "update", "user_id": "staff-1", "role": "manager"},
        headers={"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"},
    )
    assert allowed_manager_update.status_code == 200

    manager_demote_other_manager = client.post(
        "/api/team/users",
        json={"action": "update", "user_id": "manager-1", "role": "staff"},
        headers={"X-BarTender-User-Id": "manager-2", "X-BarTender-Role": "manager"},
    )
    assert manager_demote_other_manager.status_code == 200

    self_change_denied = client.post(
        "/api/team/users",
        json={"action": "update", "user_id": "manager-2", "role": "staff"},
        headers={"X-BarTender-User-Id": "manager-2", "X-BarTender-Role": "manager"},
    )
    assert self_change_denied.status_code == 403

    owner_denied = client.post(
        "/api/team/users",
        json={"action": "update", "user_id": "owner", "role": "staff"},
        headers={"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"},
    )
    assert owner_denied.status_code == 403


def test_owner_can_set_reset_pin_and_disable_member(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": app_module.DEFAULT_DATA["settings"],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
            {"id": "staff-1", "name": "Staff One", "role": "staff", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    set_pin = client.post(
        "/api/team/users",
        json={"action": "set_pin", "user_id": "staff-1", "pin": "2468"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert set_pin.status_code == 200
    assert set_pin.get_json()["user"]["pin"] == "2468"

    disable_user = client.post(
        "/api/team/users",
        json={"action": "set_disabled", "user_id": "staff-1", "disabled": True},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert disable_user.status_code == 200
    assert disable_user.get_json()["user"]["disabled"] is True

    enable_user = client.post(
        "/api/team/users",
        json={"action": "set_disabled", "user_id": "staff-1", "disabled": False},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert enable_user.status_code == 200
    assert enable_user.get_json()["user"]["disabled"] is False

    reset_pin = client.post(
        "/api/team/users",
        json={"action": "reset_pin", "user_id": "staff-1"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert reset_pin.status_code == 200
    assert reset_pin.get_json()["user"]["pin"] == ""


def test_owner_account_cannot_be_disabled(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": app_module.DEFAULT_DATA["settings"],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    response = client.post(
        "/api/team/users",
        json={"action": "set_disabled", "user_id": "owner", "disabled": True},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 400


def test_owner_can_update_owner_profile_name(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": app_module.DEFAULT_DATA["settings"],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    response = client.post(
        "/api/team/users",
        json={"action": "update_profile", "user_id": "owner", "name": "Chris"},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )

    assert response.status_code == 200
    assert response.get_json()["user"]["name"] == "Chris"


def test_manager_cannot_update_owner_profile_or_delete_owner(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": app_module.DEFAULT_DATA["settings"],
        "team_users": [
            {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
            {"id": "manager-1", "name": "Manager One", "role": "manager", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        ],
        "team_audit": [],
    })

    rename_response = client.post(
        "/api/team/users",
        json={"action": "update_profile", "user_id": "owner", "name": "New Owner"},
        headers={"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"},
    )
    assert rename_response.status_code == 403

    delete_response = client.post(
        "/api/team/users/delete",
        json={"user_id": "owner"},
        headers={"X-BarTender-User-Id": "manager-1", "X-BarTender-Role": "manager"},
    )
    assert delete_response.status_code == 400


def test_dashboard_analytics_handles_invalid_unit_data(tmp_path):
    app_module = _load_app_module(tmp_path)

    recent_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    data = {
        "settings": app_module.DEFAULT_DATA["settings"],
        "kegs": [],
        "taps": [],
        "pour_events": [{
            "created_at": recent_time,
            "keg_id": "keg-1",
            "tap_id": "tap-1",
            "amount": 12,
            "unit": {"bad": "value"},
            "source": "manual",
        }],
    }

    payload = app_module._build_dashboard_analytics(data)

    assert payload["recent_pour_count"] == 1
    assert payload["total_pour_count"] == 1


def test_audit_retention_days_defaults_and_clips_to_range(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    app_module.save_data({
        "settings": {
            **app_module.DEFAULT_DATA["settings"],
            "audit_retention_days": 500,
        },
        "team_users": [{"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"}],
        "team_audit": [],
    })

    response = client.post(
        "/api/settings",
        json={"audit_retention_days": 200},
        headers={"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"},
    )
    assert response.status_code == 200
    assert response.get_json()["audit_retention_days"] == 180
    data = app_module.load_data()
    assert data["settings"]["audit_retention_days"] == 180


def test_read_addon_version_from_env_and_supervisor(tmp_path, monkeypatch):
    app_module = _load_app_module(tmp_path)

    # 1. Environment variable
    monkeypatch.setenv("ADDON_VERSION", "1.2.3")
    assert app_module._read_addon_version() == "1.2.3"
    monkeypatch.delenv("ADDON_VERSION", raising=False)

    monkeypatch.setenv("APP_VERSION", "2.3.4")
    assert app_module._read_addon_version() == "2.3.4"
    monkeypatch.delenv("APP_VERSION", raising=False)

    # 2. Supervisor API
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")

    class FakeSupervisorResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"result": "ok", "data": {"version": "3.4.5"}}).encode("utf-8")

    with patch.object(app_module, "urlopen", return_value=FakeSupervisorResponse()) as mock_url:
        assert app_module._read_addon_version() == "3.4.5"
        req = mock_url.call_args[0][0]
        assert req.get_header("X-supervisor-token") == "fake-token"


def test_homebrewer_default_displays_split_taps_and_bar_stock(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    data["settings"]["display_count"] = 2
    data["settings"]["bar_stock_enabled"] = True
    data["taps"] = [{"id": 1, "number": 1, "label": "Main Tap", "keg_id": 1}]
    data["kegs"] = [{"id": 1, "name": "House IPA Keg", "status": "in_use", "percent_full": 80}]
    data["bar_stock"] = [{"id": 1, "name": "Bourbon", "category": "Spirits", "quantity": 3, "unit": "bottles"}]
    app_module.save_data(data)

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    # Display 1: Shows Taps, Does NOT show Bar Stock
    res1 = client.get("/display?display=1")
    assert res1.status_code == 200
    html1 = res1.get_data(as_text=True)
    assert "House IPA Keg" in html1
    assert "Tap #1" in html1
    assert "Bourbon" not in html1
    assert "📦 Bar Stock" not in html1

    # Display 2: Shows Bar Stock, Does NOT show Taps
    res2 = client.get("/display?display=2")
    assert res2.status_code == 200
    html2 = res2.get_data(as_text=True)
    assert "📦 Bar Stock" in html2
    assert "Bourbon" in html2
    assert "House IPA Keg" not in html2
    assert "Tap #1" not in html2
