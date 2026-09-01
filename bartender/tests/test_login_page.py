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
    app_module.app.config["SECRET_KEY"] = "test-secret"
    return app_module


def test_login_page_redirects_when_not_authenticated(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")


def test_valid_user_can_login_from_team_users(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    app_module.save_data(data)

    response = client.post(
        "/login",
        data={"user_id": "manager-1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    with client.session_transaction() as session:
        assert session["user_id"] == "manager-1"
        assert session["user_role"] == "manager"


def test_unauthenticated_request_redirects_to_ingress_login_path(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.INGRESS_PATH = "/api/hassio_ingress/test-token"
    app_module.app.config["APPLICATION_ROOT"] = app_module.INGRESS_PATH
    client = app_module.app.test_client()

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/api/hassio_ingress/test-token/login"


def test_owner_requires_pin_when_other_users_exist(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    data["settings"]["owner_pin"] = "1234"
    app_module.save_data(data)

    response = client.post(
        "/login",
        data={"user_id": "owner"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "PIN" in response.get_data(as_text=True)

    response = client.post(
        "/login",
        data={"user_id": "owner", "owner_pin": "1234"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    with client.session_transaction() as session:
        assert session["user_id"] == "owner"
        assert session["user_role"] == "owner"


def test_owner_recovery_redirects_to_settings_when_pin_missing(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    data["settings"]["owner_pin"] = ""
    app_module.save_data(data)

    response = client.post(
        "/login",
        data={"user_id": "owner"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/settings"
    with client.session_transaction() as session:
        assert session["user_id"] == "owner"
        assert session["user_role"] == "owner"
        assert session["owner_pin_recovery_required"] is True

    blocked_response = client.get("/api/team/audit", follow_redirects=False)
    assert blocked_response.status_code == 423
    assert blocked_response.get_json()["error"] == "Owner PIN setup required before other actions are available."


def test_saving_owner_pin_clears_recovery_lock(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    data["settings"]["owner_pin"] = ""
    app_module.save_data(data)

    login_response = client.post(
        "/login",
        data={"user_id": "owner"},
        follow_redirects=False,
    )
    assert login_response.status_code == 302
    assert login_response.headers["Location"] == "/settings"

    save_response = client.post(
        "/api/settings",
        json={"owner_pin": "2468"},
    )
    assert save_response.status_code == 200

    with client.session_transaction() as session:
        assert "owner_pin_recovery_required" not in session

    unlocked_response = client.get("/api/team/audit", follow_redirects=False)
    assert unlocked_response.status_code == 200
