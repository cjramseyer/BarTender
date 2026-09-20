"""Best-effort publish-only MQTT integration for BarTender."""

import json
import re
import threading
from datetime import datetime, timezone
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - optional runtime dependency
    mqtt = None


def mqtt_settings_configured(settings: dict[str, Any]) -> bool:
    return bool(
        settings.get("mqtt_enabled")
        and str(settings.get("mqtt_host", "") or "").strip()
    )


def _topic_prefix(value: Any) -> str:
    prefix = re.sub(r"[^A-Za-z0-9_./-]+", "-", str(value or "bartender").strip())
    return prefix.strip("/") or "bartender"


def _build_messages(data: dict[str, Any]) -> list[tuple[str, str, bool]]:
    settings = data.get("settings", {}) if isinstance(data.get("settings"), dict) else {}
    prefix = _topic_prefix(settings.get("mqtt_topic_prefix"))
    timestamp = datetime.now(timezone.utc).isoformat()
    displays = {
        "count": settings.get("display_count", 2),
        "tap_assignments": settings.get("display_tap_assignments", []),
        "bar_stock_assignments": settings.get("display_bar_stock_assignments", []),
    }
    latest_audit = data.get("team_audit", [])[-1] if data.get("team_audit") else {}
    event = {
        "action": latest_audit.get("action", "state_updated"),
        "resource": latest_audit.get("resource", ""),
        "timestamp": latest_audit.get("timestamp", timestamp),
    }
    snapshots = {
        "status": {"state": "online", "timestamp": timestamp},
        "taps": data.get("taps", []),
        "kegs": data.get("kegs", []),
        "bar-stock": data.get("bar_stock", []),
        "displays": displays,
    }
    messages = [
        (f"{prefix}/{topic}", json.dumps(payload, separators=(",", ":")), True)
        for topic, payload in snapshots.items()
    ]
    messages.append((f"{prefix}/events/state-updated", json.dumps(event, separators=(",", ":")), False))
    return messages


def publish_state_async(data: dict[str, Any]) -> None:
    settings = data.get("settings", {}) if isinstance(data.get("settings"), dict) else {}
    if not mqtt_settings_configured(settings) or mqtt is None:
        return

    payload = json.loads(json.dumps(data))
    threading.Thread(
        target=_publish_state,
        args=(payload,),
        name="bartender-mqtt-publisher",
        daemon=True,
    ).start()


def _publish_state(data: dict[str, Any]) -> None:
    settings = data["settings"]
    client = mqtt.Client(client_id="bartender-publisher")
    username = str(settings.get("mqtt_username", "") or "").strip()
    password = str(settings.get("mqtt_password", "") or "")
    if username:
        client.username_pw_set(username, password)
    if settings.get("mqtt_tls"):
        client.tls_set()

    try:
        client.connect(
            str(settings.get("mqtt_host", "") or "").strip(),
            int(settings.get("mqtt_port", 1883) or 1883),
            keepalive=10,
        )
        client.loop_start()
        for topic, message, retain in _build_messages(data):
            client.publish(topic, message, qos=1, retain=retain).wait_for_publish()
    except Exception:
        # MQTT is an optional integration and must never interrupt app writes.
        return
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass
