import os
import json
import sys
import time
import base64
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


def test_first_time_setup_can_save_without_authentication(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.app.config["TESTING"] = False
    client = app_module.app.test_client()

    response = client.post(
        "/api/settings",
        json={
            "bar_name": "New Bar",
            "measurement": "us",
            "theme": "light",
            "brewery_type": "homebrewer",
            "setup_completed": True,
        },
    )

    assert response.status_code == 200
    assert app_module.load_data()["settings"]["setup_completed"] is True


def test_cors_allows_only_configured_origins(tmp_path, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:5055, https://mobile.example")
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    allowed = client.get("/api/taps", headers={"Origin": "http://127.0.0.1:5055"})
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:5055"
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"

    denied = client.get("/api/taps", headers={"Origin": "https://untrusted.example"})
    assert denied.status_code == 200
    assert "Access-Control-Allow-Origin" not in denied.headers


def test_production_api_requires_authentication(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.app.config["TESTING"] = False
    client = app_module.app.test_client()

    response = client.post("/api/taps/1/pour", json={"amount": 16, "unit": "oz"})

    assert response.status_code == 401
    assert response.get_json()["error"] == "Authentication required."


def test_mobile_login_issues_token_for_user_pin(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["team_users"][0]["pin"] = "2468"
    app_module.save_data(data)
    client = app_module.app.test_client()

    response = client.post(
        "/api/mobile/login",
        json={"user_id": "owner", "pin": "2468"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["token"]
    assert payload["user"]["id"] == "owner"
    assert app_module.load_data()["mobile_tokens"]


def test_owner_can_start_pro_trial(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post("/api/licensing/trial")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["plan"] == "Trial"
    assert payload["active"] is True
    assert payload["days_remaining"] == 30
    assert app_module.load_data()["settings"]["license_type"] == "trial"


def test_owner_can_download_pro_activation_request(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    data["settings"]["bar_name"] = "Harbor Taproom"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post("/api/licensing/activation-request")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"].endswith(
        'filename="bartender-activation-request.json"'
    )
    payload = response.get_json()
    assert payload["app_id"] == "bartender"
    assert payload["request_type"] == "pro_activation"
    assert payload["instance_id"]
    assert payload["nonce"]
    assert payload["instance_key_id"].startswith("sha256:")
    assert payload["bar_name_hash"].startswith("sha256:")
    assert payload["instance_public_key"]["algorithm"] == "Ed25519"
    assert payload["signature"]["algorithm"] == "Ed25519"
    assert payload["signature"]["key_id"] == payload["instance_key_id"]
    public_key = base64.urlsafe_b64decode(
        payload["instance_public_key"]["value"]
        + "=" * (-len(payload["instance_public_key"]["value"]) % 4)
    )
    signature = base64.urlsafe_b64decode(
        payload["signature"]["value"]
        + "=" * (-len(payload["signature"]["value"]) % 4)
    )
    signing_message = (
        f"{payload['app_id']}.{payload['instance_id']}."
        f"{payload['instance_key_id']}.{payload['nonce']}"
    ).encode("utf-8")
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    Ed25519PublicKey.from_public_bytes(public_key).verify(signature, signing_message)
    assert "Harbor Taproom" not in response.get_data(as_text=True)
    stored = app_module.load_data()["settings"]
    assert stored["license_instance_id"] == payload["instance_id"]
    assert stored["license_instance_private_key"]


def test_pro_settings_show_configured_license_portal_link(tmp_path, monkeypatch):
    monkeypatch.setenv("LICENSE_PORTAL_URL", "https://licenses.example.com/portal/")
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    body = client.get("/settings").get_data(as_text=True)

    assert 'href="https://licenses.example.com/portal"' in body
    assert "Open License Portal" in body


def test_invalid_license_portal_url_is_not_rendered(tmp_path, monkeypatch):
    monkeypatch.setenv("LICENSE_PORTAL_URL", "javascript:alert(1)")
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    body = client.get("/settings").get_data(as_text=True)

    assert "Open License Portal" not in body


def test_manager_cannot_download_pro_activation_request(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "manager-1"
        session["user_role"] = "manager"
        session["user_name"] = "Manager"

    response = client.post("/api/licensing/activation-request")

    assert response.status_code == 403


def test_invalid_license_token_does_not_activate_pro(tmp_path, monkeypatch):
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", "invalid")
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post("/api/licensing/activate", json={"token": "bad-token"})

    assert response.status_code == 400
    assert app_module.load_data()["settings"]["license_type"] == ""


def test_license_for_different_instance_is_rejected(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def encode(value):
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    signing_key = Ed25519PrivateKey.generate()
    public_key = signing_key.public_key().public_bytes_raw()
    monkeypatch.setenv("LICENSE_PUBLIC_KEY", encode(public_key))
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    data["settings"]["license_instance_id"] = "local-instance"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    payload = json.dumps(
        {
            "app_id": "bartender",
            "plan": "pro",
            "instance_id": "other-instance",
            "expires_at": "2099-01-01T00:00:00+00:00",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    token = f"{encode(payload)}.{encode(signing_key.sign(payload))}"

    response = client.post("/api/licensing/activate", json={"token": token})

    assert response.status_code == 400
    assert "different BarTender instance" in response.get_json()["error"]


def test_manager_cannot_activate_license(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "manager-1"
        session["user_role"] = "manager"
        session["user_name"] = "Manager"

    response = client.post("/api/licensing/activate", json={"token": "token"})

    assert response.status_code == 403
    assert response.get_json()["error"] == "Only the owner can activate a license."


def test_external_api_listener_does_not_expose_licensing(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.EXTERNAL_API_MODE = True
    client = app_module.app.test_client()

    response = client.post(
        "/api/licensing/activate",
        json={"token": "token"},
        headers={"X-API-Token": "external-token"},
    )

    assert response.status_code == 404
    assert response.get_json()["error"] == "Licensing is available only through the management UI."


def test_owner_can_clear_license_state(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
    client.post("/api/licensing/trial")

    response = client.post("/api/licensing/clear")

    assert response.status_code == 200
    assert app_module.load_data()["settings"]["license_type"] == ""


def test_login_page_does_not_render_whats_new_notice(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["setup_completed"] = True
    app_module.save_data(data)
    client = app_module.app.test_client()

    response = client.get("/login")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="updateNoticeModal"' not in page
    assert 'id="titlebarWhatsNewButton"' not in page


def test_dashboard_includes_inline_tap_and_keg_edit_controls(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="dashboardTapEditModal"' in page
    assert 'id="dashboardKegEditModal"' in page
    assert "openDashboardTapEdit" in page
    assert "openDashboardKegEdit" in page
    assert "saveDashboardTap" in page
    assert "saveDashboardKeg" in page


def test_authenticated_page_keeps_whats_new_out_of_title_bar(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["setup_completed"] = True
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="titlebarWhatsNewButton"' not in page
    assert 'id="updateNoticeModal"' in page
    assert 'onclick="openWhatsNewFromMenu()"' in page


def test_whats_new_reappears_when_release_date_is_newer_than_dismissal(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["setup_completed"] = True
    data["team_users"][0]["release_seen_version"] = "2026-09-14"
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'window.BARTENDER_RELEASE_DATE = "2026-09-15"' in page
    assert 'window.BARTENDER_SEEN_RELEASE_DATE = "2026-09-14"' in page
    assert 'id="updateNoticeModal"' in page
    assert 'onclick="openWhatsNewFromMenu()"' in page


def test_whats_new_reappears_for_legacy_version_dismissal(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["setup_completed"] = True
    data["team_users"][0]["release_seen_version"] = "dev"
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    assert 'id="updateNoticeModal"' in response.get_data(as_text=True)


def test_whats_new_is_hidden_when_release_highlights_are_empty(tmp_path, monkeypatch):
    app_module = _load_app_module(tmp_path)
    monkeypatch.setattr(app_module, "RELEASE_HIGHLIGHTS", [])
    data = app_module.load_data()
    data["settings"]["setup_completed"] = True
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="updateNoticeModal"' in page
    assert "What's New" in page


def test_pro_profile_shows_title_bar_indicator(tmp_path):
    app_module = _load_app_module(tmp_path)
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'title="Pro bar profile is active"' in page
    assert "Pro" in page


def test_team_access_renders_scan_credential_modal(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/team-access")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'id="scanCredentialModal"' in page
    assert "function issueScanCredential" in page


def test_whats_new_dismissal_is_stored_per_user(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "release_seen_version": ""},
        {"id": "staff-1", "name": "Staff One", "role": "staff", "release_seen_version": ""},
    ]
    app_module.save_data(data)

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post("/api/user/release-seen", json={"date": "2026-09-15"})
    assert response.status_code == 200
    saved_users = {user["id"]: user for user in app_module.load_data()["team_users"]}
    assert saved_users["owner"]["release_seen_version"] == "2026-09-15"
    assert saved_users["staff-1"]["release_seen_version"] == ""

    with client.session_transaction() as session:
        session["user_id"] = "staff-1"
        session["user_role"] = "staff"
        session["user_name"] = "Staff One"
    staff_page = client.get("/")
    assert 'window.BARTENDER_SEEN_VERSION = ""' in staff_page.get_data(as_text=True)


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
        assert session.permanent is True
        assert "last_activity_at" in session


def test_authenticated_session_expires_after_idle_timeout(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
        session.permanent = True
        session["last_activity_at"] = time.time() - (app_module.SESSION_TIMEOUT_MINUTES * 60 + 1)

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"].startswith("/login")
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_authenticated_session_is_listed_and_can_be_revoked(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/api/team/sessions", headers={"User-Agent": "Mozilla/5.0"})

    assert response.status_code == 200
    sessions = response.get_json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["is_current"] is True
    session_id = sessions[0]["id"]

    revoked = client.post(
        "/api/team/sessions",
        json={"action": "revoke", "session_id": session_id},
    )

    assert revoked.status_code == 200
    rejected = client.get("/api/team/sessions")
    assert rejected.status_code == 401


def test_mobile_session_uses_configured_shorter_timeout(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["mobile_session_timeout_minutes"] = 30
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get(
        "/api/team/sessions",
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile"},
    )

    assert response.status_code == 200
    session_record = response.get_json()["sessions"][0]
    assert session_record["device_type"] == "mobile"
    stored = app_module.load_data()["user_sessions"][0]
    assert stored["timeout_minutes"] == 30


def test_station_login_uses_station_timeout(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    response = client.post(
        "/login",
        data={"user_id": "owner", "station_mode": "1"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    sessions = app_module.load_data()["user_sessions"]
    assert sessions[-1]["session_type"] == "station"
    assert sessions[-1]["timeout_minutes"] == 30


def test_station_registration_persists_for_future_logins_and_can_be_revoked(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    registered = client.post(
        "/api/team/stations",
        json={"name": "Main Pour Station"},
    )

    assert registered.status_code == 200
    station = registered.get_json()["station"]
    assert station["name"] == "Main Pour Station"
    assert app_module.load_data()["station_registrations"][0]["token_hash"]

    login_page = client.get("/login")
    login_body = login_page.get_data(as_text=True)
    assert 'name="station_mode"' in login_body
    assert 'value="1"' in login_body
    assert "checked" in login_body

    revoked = client.post(
        "/api/team/stations",
        json={"action": "revoke", "station_id": station["id"]},
    )

    assert revoked.status_code == 200
    assert 'name="station_mode" value="1" checked' not in client.get("/login").get_data(as_text=True)


def test_session_timeout_settings_are_clamped_to_global_timeout(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["settings"]["brewery_type"] = "pro"
    app_module.save_data(data)
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post(
        "/api/settings",
        json={
            "mobile_session_timeout_minutes": 999,
            "station_session_timeout_minutes": 1,
        },
    )

    assert response.status_code == 200
    settings = app_module.load_data()["settings"]
    assert settings["mobile_session_timeout_minutes"] == 240
    assert settings["station_session_timeout_minutes"] == 5


def test_homebrewer_hides_and_resets_pro_session_timeout_settings(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post(
        "/api/settings",
        json={
            "mobile_session_timeout_minutes": 120,
            "station_session_timeout_minutes": 90,
        },
    )

    assert response.status_code == 200
    settings = app_module.load_data()["settings"]
    assert settings["mobile_session_timeout_minutes"] == 30
    assert settings["station_session_timeout_minutes"] == 30

    page = client.get("/settings").get_data(as_text=True)
    assert 'id="mobileSessionTimeoutMinutes"' not in page
    assert 'id="stationSessionTimeoutMinutes"' not in page


def test_user_scan_credential_logs_in_and_can_be_revoked(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False},
        {"id": "staff-1", "name": "Staff One", "role": "staff", "pin": "", "disabled": False},
    ]
    data["settings"]["owner_pin"] = "1234"
    app_module.save_data(data)
    headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    issued = client.post(
        "/api/team/users",
        json={"action": "issue_scan", "user_id": "staff-1"},
        headers=headers,
    )
    assert issued.status_code == 200
    payload = issued.get_json()
    assert payload["login_url"].startswith("/auth/scan/")
    assert "scan_token_hash" not in payload["user"]

    token = payload["login_url"].rsplit("/", 1)[-1]
    response = client.get(f"/auth/scan/{token}", follow_redirects=False)
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["user_id"] == "staff-1"

    client.get("/logout")
    revoked = client.post(
        "/api/team/users",
        json={"action": "revoke_scan", "user_id": "staff-1"},
        headers=headers,
    )
    assert revoked.status_code == 200
    rejected = client.get(f"/auth/scan/{token}", follow_redirects=False)
    assert rejected.status_code == 302
    assert "/login" in rejected.headers["Location"]
    failed_login = client.get(rejected.headers["Location"])
    assert "invalid or revoked" in failed_login.get_data(as_text=True)
    audit = client.get("/api/team/audit", headers=headers).get_json()["audit"]
    assert any(entry["action"] == "scan_login_failed" for entry in audit)


def test_user_pin_required_when_configured(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "pin": "2468", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
    ]
    app_module.save_data(data)

    denied = client.post(
        "/login",
        data={"user_id": "manager-1"},
        follow_redirects=False,
    )

    assert denied.status_code == 200
    assert "PIN required for this team member." in denied.get_data(as_text=True)

    allowed = client.post(
        "/login",
        data={"user_id": "manager-1", "user_pin": "2468"},
        follow_redirects=False,
    )

    assert allowed.status_code == 302
    assert allowed.headers["Location"] == "/"


def test_disabled_user_cannot_login(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        {"id": "staff-1", "name": "Staff One", "role": "staff", "pin": "", "disabled": True, "created_at": "2024-01-01T00:00:00Z"},
    ]
    app_module.save_data(data)

    response = client.post(
        "/login",
        data={"user_id": "staff-1"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "This user account is disabled." in response.get_data(as_text=True)


def test_disabled_users_are_hidden_from_login_dropdown(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
        {"id": "staff-1", "name": "Staff One", "role": "staff", "pin": "", "disabled": True, "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "pin": "", "disabled": False, "created_at": "2024-01-01T00:00:00Z"},
    ]
    app_module.save_data(data)

    response = client.get("/login")

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "Staff One" not in page
    assert "Manager One" in page
    assert "Owner" in page


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
    assert 'name="owner_pin"' not in response.get_data(as_text=True)

    response = client.post(
        "/login",
        data={"user_id": "owner", "user_pin": "1234"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    with client.session_transaction() as session:
        assert session["user_id"] == "owner"
        assert session["user_role"] == "owner"


def test_owner_scan_uses_the_same_pin_field(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "pin": "", "disabled": False},
        {"id": "staff-1", "name": "Staff One", "role": "staff", "pin": "", "disabled": False},
    ]
    data["settings"]["owner_pin"] = "1234"
    app_module.save_data(data)
    owner_headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}
    issued = client.post(
        "/api/team/users",
        json={"action": "issue_scan", "user_id": "owner"},
        headers=owner_headers,
    ).get_json()
    token = issued["login_url"].rsplit("/", 1)[-1]

    challenge = client.get(f"/auth/scan/{token}", follow_redirects=False)
    assert challenge.status_code == 302
    response = client.post(
        "/login",
        data={"scan_token": token, "user_pin": "1234"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert session["user_id"] == "owner"


def test_owner_recovery_redirects_to_team_access_when_pin_missing(tmp_path):
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
    assert response.headers["Location"] == "/team-access"
    with client.session_transaction() as session:
        assert session["user_id"] == "owner"
        assert session["user_role"] == "owner"
        assert session["owner_pin_recovery_required"] is True

    team_access_response = client.get("/team-access", follow_redirects=False)
    assert team_access_response.status_code == 200
    assert "Save an Owner PIN here" in team_access_response.get_data(as_text=True)

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
    assert login_response.headers["Location"] == "/team-access"

    save_response = client.post(
        "/api/settings",
        json={"owner_pin": "2468"},
    )
    assert save_response.status_code == 200

    with client.session_transaction() as session:
        assert "owner_pin_recovery_required" not in session

    unlocked_response = client.get("/api/team/audit", follow_redirects=False)
    assert unlocked_response.status_code == 200


def test_settings_recovery_banner_links_to_team_access(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    data["settings"]["owner_pin"] = ""
    app_module.save_data(data)

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"
        session["owner_pin_recovery_required"] = True

    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Owner Recovery Mode" in body
    assert 'href="/team-access"' in body
    assert "Open Team Access" in body


def test_settings_save_without_owner_pin_keeps_existing_owner_pin(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["team_users"] = [
        {"id": "owner", "name": "Owner", "role": "owner", "created_at": "2024-01-01T00:00:00Z"},
        {"id": "manager-1", "name": "Manager One", "role": "manager", "created_at": "2024-01-01T00:00:00Z"},
    ]
    data["settings"]["owner_pin"] = "2468"
    app_module.save_data(data)

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.post(
        "/api/settings",
        json={"bar_name": "Updated Bar"},
    )

    assert response.status_code == 200
    assert response.get_json()["bar_name"] == "Updated Bar"
    assert app_module.load_data()["settings"]["owner_pin"] == "2468"


def test_settings_hides_display_count_for_homebrewer(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    data = app_module.load_data()
    data["settings"]["brewery_type"] = "homebrewer"
    data["settings"]["display_count"] = 2
    app_module.save_data(data)

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/settings", follow_redirects=False)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "App Version" in body
    assert f"v{app_module.APP_VERSION}" in body
    assert "Number of Displays" not in body
    assert "Homebrewer installs default to 2 displays." not in body
    assert "Basic settings save automatically." in body
    assert "Save Advanced Settings" in body
    assert 'id="posSyncProvider"' not in body
    assert "POS Sync Provider" not in body
    assert "Licensing" not in body
    assert 'onclick="activateLicense()"' not in body
    assert 'id="displayCount"' not in body
    assert 'id="proProfileRefreshNotice"' in body
    assert "Refresh this page after the change saves" in body


def test_settings_shows_active_cors_origins_as_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://mobile.example, http://localhost:5055")
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/settings")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Allowed CORS Origins" in body
    assert "https://mobile.example" in body
    assert "http://localhost:5055" in body
    assert 'id="corsAllowedOrigins"' not in body


def test_authenticated_layout_shows_logout_link(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/", follow_redirects=False)

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "nav-menu-button" in body
    assert "View" in body
    assert "Bar Stock" in body
    assert "API Reference" in body
    assert "toggleNavSetting" in body
    assert "View Display" in body
    assert "Open in New Window" in body
    assert "Printable Menu" in body
    assert "<summary>Options</summary>" in body
    assert 'class="nav-menu-section-title">Team</div>' in body
    assert 'class="nav-menu-divider"' in body
    assert 'href="/audit"' in body
    assert 'href="/audit"' in body and 'target="_blank"' in body
    assert 'href="/settings"' in body
    assert "nav-avatar-button" in body
    assert "Owner" in body
    assert "Log out" in body


def test_authenticated_layout_uses_request_ingress_for_logout_link(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.INGRESS_PATH = ""
    app_module.app.config["APPLICATION_ROOT"] = "/"
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get(
        "/",
        headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert 'href="/api/hassio_ingress/test-token/logout"' in body


def test_logout_clears_session_and_redirects_to_login(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get("/logout", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["Location"] == "/login"
    with client.session_transaction() as session:
        assert "user_id" not in session


def test_login_redirect_stays_within_ingress_path(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.INGRESS_PATH = ""
    app_module.app.config["APPLICATION_ROOT"] = "/"
    client = app_module.app.test_client()

    response = client.post(
        "/login",
        data={"user_id": "owner"},
        headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/api/hassio_ingress/test-token/"


def test_logout_redirect_stays_within_ingress_path(tmp_path):
    app_module = _load_app_module(tmp_path)
    app_module.INGRESS_PATH = ""
    app_module.app.config["APPLICATION_ROOT"] = "/"
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    response = client.get(
        "/logout",
        headers={"X-Ingress-Path": "/api/hassio_ingress/test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/api/hassio_ingress/test-token/login"
