"""Solana 1H execution processor — listens to the SOL stream.

Thin domain shell delegating to the shared 1H engine (see btc/processor.py)."""
from asset_processor import AssetProcessor

SYMBOL = "SOLUSDT"


class Processor(AssetProcessor):
    """SOL 1H processor — runs the shared engine for SOLUSDT."""

    def __init__(self, order_manager=None) -> None:
        super().__init__(SYMBOL, order_manager=order_manager)
