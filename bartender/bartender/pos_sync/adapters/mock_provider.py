"""Mock POS provider for sandbox sync verification."""

from .base import PosSyncPayload, PosTapRecord


class MockPosAdapter:
    """Deterministic test adapter used as a sync proof-of-concept."""

    provider_key = "mock"

    def pull_snapshot(self, credentials: dict[str, str] | None = None) -> PosSyncPayload:
        _ = credentials or {}
        return PosSyncPayload(
            taps=[
                PosTapRecord(
                    number=1,
                    label="POS Tap 1",
                    item_name="Mock IPA",
                    serving_size="16 oz",
                    price_label="$6.00",
                    available=True,
                ),
                PosTapRecord(
                    number=2,
                    label="POS Tap 2",
                    item_name="Mock Lager",
                    serving_size="16 oz",
                    price_label="$5.50",
                    available=True,
                ),
            ]
        )
