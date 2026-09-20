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


def test_mqtt_messages_publish_retained_snapshots_and_event():
    from bartender.mqtt import _build_messages

    messages = _build_messages({
        "settings": {
            "mqtt_topic_prefix": "bar/main",
            "display_count": 2,
            "display_tap_assignments": [[1], []],
            "display_bar_stock_assignments": [False, True],
        },
        "taps": [{"id": 1, "number": 1}],
        "kegs": [{"id": 1, "name": "House IPA"}],
        "bar_stock": [{"id": 1, "name": "Tonic"}],
        "team_audit": [{"action": "keg_filled", "resource": "keg:1", "created_at": "2026-09-20T00:00:00Z"}],
    })

    topics = {topic: retain for topic, _, retain in messages}
    assert topics == {
        "bar/main/status": True,
        "bar/main/taps": True,
        "bar/main/kegs": True,
        "bar/main/bar-stock": True,
        "bar/main/displays": True,
        "bar/main/events/state-updated": False,
    }


def test_mqtt_connection_publishes_snapshots(monkeypatch):
    from bartender import mqtt as mqtt_module

    published = []

    class FakePublishResult:
        def wait_for_publish(self):
            return None

    class FakeClient:
        def __init__(self, client_id):
            assert client_id == "bartender-publisher"

        def username_pw_set(self, username, password):
            assert username == "bartender"
            assert password == "secret"

        def tls_set(self):
            return None

        def connect(self, host, port, keepalive):
            assert host == "mqtt.example.test"
            assert port == 8883
            assert keepalive == 10

        def loop_start(self):
            return None

        def publish(self, topic, payload, qos, retain):
            published.append((topic, qos, retain))
            return FakePublishResult()

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeMqtt:
        Client = FakeClient

    monkeypatch.setattr(mqtt_module, "mqtt", FakeMqtt)
    success, message = mqtt_module.test_connection({
        "settings": {
            "mqtt_host": "mqtt.example.test",
            "mqtt_port": 8883,
            "mqtt_topic_prefix": "bar/main",
            "mqtt_username": "bartender",
            "mqtt_password": "secret",
            "mqtt_tls": True,
        },
    })

    assert success is True
    assert message == "Connected and published MQTT snapshots."
    assert ("bar/main/status", 1, True) in published
    assert ("bar/main/events/state-updated", 1, False) in published


def test_owner_can_save_mqtt_settings_without_exposing_password(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    headers = {"X-BarTender-User-Id": "owner", "X-BarTender-Role": "owner"}

    response = client.post(
        "/api/settings",
        json={
            "mqtt_enabled": True,
            "mqtt_host": "mqtt.example.test",
            "mqtt_port": 8883,
            "mqtt_topic_prefix": "bar/main",
            "mqtt_username": "bartender",
            "mqtt_password": "secret-password",
            "mqtt_tls": True,
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json()["mqtt_password"] == ""
    settings = app_module.load_data()["settings"]
    assert settings["mqtt_enabled"] is True
    assert settings["mqtt_host"] == "mqtt.example.test"
    assert settings["mqtt_port"] == 8883
    assert settings["mqtt_topic_prefix"] == "bar/main"
    assert settings["mqtt_username"] == "bartender"
    assert settings["mqtt_password"] == "secret-password"
    assert settings["mqtt_tls"] is True

    exported = app_module._build_export_json_payload(app_module.load_data())
    assert "mqtt_username" not in exported["data"]["settings"]
    assert "mqtt_password" not in exported["data"]["settings"]


def test_owner_settings_page_shows_mqtt_panel(tmp_path):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()

    with client.session_transaction() as session:
        session["user_id"] = "owner"
        session["user_role"] = "owner"
        session["user_name"] = "Owner"

    body = client.get("/settings").get_data(as_text=True)

    assert "<summary>MQTT</summary>" in body
    assert 'id="mqttHost"' in body
    assert 'id="mqttTopicPrefix"' in body
    assert 'class="settings-inline-field settings-mqtt-field"' in body
    assert "Test MQTT Connection" in body


def test_mqtt_test_endpoint_requires_owner_and_returns_probe_result(tmp_path, monkeypatch):
    app_module = _load_app_module(tmp_path)
    client = app_module.app.test_client()
    monkeypatch.setattr(app_module, "test_mqtt_connection", lambda data: (True, "Probe published."))

    forbidden = client.post("/api/settings/mqtt/test", headers={"X-BarTender-Role": "manager"})
    assert forbidden.status_code == 403

    response = client.post("/api/settings/mqtt/test", headers={"X-BarTender-Role": "owner"})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "message": "Probe published."}
