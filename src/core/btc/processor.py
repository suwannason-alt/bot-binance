"""
Bitcoin 1H execution processor — listens to the BTC stream.

Thin domain shell: it binds the symbol and DELEGATES to the one shared 1H engine
(:class:`asset_processor.AssetProcessor` → ``strategy.evaluate_1h_live`` + the
Profit-Locked STEP trailing stop).  The strategy logic is NOT duplicated per asset —
all three domains run the same shared 1H engine block, parameterised by this domain's
``config.py`` (via ``config_1h.CONFIG_MATRIX``/``apply_symbol``).
"""
from asset_processor import AssetProcessor

SYMBOL = "BTCUSDT"


class Processor(AssetProcessor):
    """BTC 1H processor — runs the shared engine for BTCUSDT."""

    def __init__(self, order_manager=None) -> None:
        super().__init__(SYMBOL, order_manager=order_manager)
