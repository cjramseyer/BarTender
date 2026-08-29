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
