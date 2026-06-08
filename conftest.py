"""Pytest path bootstrap for the modular layout.

Mirrors the per-entry-point sys.path shim so the flat ``import config`` /
``import backtest`` style resolves when bare ``pytest`` collects the suites in
``tests/``.  The standalone runners (``python tests/test_*.py``) carry their own
header and do not rely on this file.
"""
import sys
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_ROOT / _seg) if _seg else str(_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)
