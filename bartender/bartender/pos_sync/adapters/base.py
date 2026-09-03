"""Base adapter protocol for POS sync providers."""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass
class PosTapRecord:
    """Represents a provider-sourced tap/menu mapping row."""

    number: int
    label: str
    item_name: str
    serving_size: str
    price_label: str
    available: bool


@dataclass
class PosSyncPayload:
    """Normalized provider payload for sync operations."""

    taps: list[PosTapRecord]


@runtime_checkable
class PosSyncAdapter(Protocol):
    """Contract that provider adapters must satisfy."""

    provider_key: str

    def pull_snapshot(self, credentials: dict[str, str] | None = None) -> PosSyncPayload:
        """Fetch latest tap/menu information from a provider."""
        ...
