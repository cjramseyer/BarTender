"""Built-in POS provider adapters for common bar/restaurant systems."""

from .base import PosSyncPayload, PosTapRecord


class _StaticProviderAdapter:
    """Reusable adapter that returns deterministic provider-specific sample data."""

    provider_key = ""
    display_name = ""

    def _tap_prefix(self) -> str:
        return self.display_name or self.provider_key.upper()

    def pull_snapshot(self, credentials: dict[str, str] | None = None) -> PosSyncPayload:
        _ = credentials or {}
        prefix = self._tap_prefix()
        return PosSyncPayload(
            taps=[
                PosTapRecord(
                    number=1,
                    label=f"{prefix} Tap 1",
                    item_name=f"{prefix} IPA",
                    serving_size="16 oz",
                    price_label="$6.00",
                    available=True,
                ),
                PosTapRecord(
                    number=2,
                    label=f"{prefix} Tap 2",
                    item_name=f"{prefix} Lager",
                    serving_size="16 oz",
                    price_label="$5.50",
                    available=True,
                ),
            ]
        )


class ToastPosAdapter(_StaticProviderAdapter):
    provider_key = "toast"
    display_name = "Toast"


class SquarePosAdapter(_StaticProviderAdapter):
    provider_key = "square"
    display_name = "Square"


class CloverPosAdapter(_StaticProviderAdapter):
    provider_key = "clover"
    display_name = "Clover"


class LightspeedPosAdapter(_StaticProviderAdapter):
    provider_key = "lightspeed"
    display_name = "Lightspeed"


class ArryvedPosAdapter(_StaticProviderAdapter):
    provider_key = "arryved"
    display_name = "Arryved"
