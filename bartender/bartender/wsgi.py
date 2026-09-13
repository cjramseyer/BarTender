"""Production WSGI entry point for the management and external API app."""

from .app import EXTERNAL_API_MODE, _schedule_anonymous_telemetry_heartbeat, app

if not EXTERNAL_API_MODE:
	_schedule_anonymous_telemetry_heartbeat()

__all__ = ["app"]
