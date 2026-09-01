import os
import sys
from datetime import datetime, timedelta, timezone
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
    assert any(entry["action"] == "settings_updated" for entry in payload["audit"])


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
