"""Ethereum 1H execution processor — listens to the ETH stream.

Thin domain shell delegating to the shared 1H engine (see btc/processor.py)."""
from asset_processor import AssetProcessor

SYMBOL = "ETHUSDT"


class Processor(AssetProcessor):
    """ETH 1H processor — runs the shared engine for ETHUSDT."""

    def __init__(self, order_manager=None) -> None:
        super().__init__(SYMBOL, order_manager=order_manager)
