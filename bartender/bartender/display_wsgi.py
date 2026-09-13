"""Production WSGI entry point for the read-only display app."""

from .display import display_app

__all__ = ["display_app"]
