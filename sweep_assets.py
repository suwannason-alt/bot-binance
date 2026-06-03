"""Multi-asset ATR_RATIO sweep — offline research tool.

Screens Binance perpetual pairs for compatibility with the loose
ATR_RATIO_MIN = 1.10 volatility gate, using out-of-sample, per-asset
1.10-vs-1.15 head-to-head validation. See
docs/superpowers/specs/2026-06-03-multi-asset-atr-sweep-design.md.

Run:  python sweep_assets.py            # full sweep, 5y, default candidates
      python sweep_assets.py --days 730 # custom window
"""
from __future__ import annotations

RUIN_FLOOR = -50.0      # MaxDD (%) below this disqualifies an asset
SPLIT_FRAC = 0.70       # train fraction; remainder is out-of-sample test


def verdict(pf_115_test: float, pf_110_test: float,
            maxdd_110_test: float, ruin_floor: float = RUIN_FLOOR) -> str:
    """Classify an asset from its TEST-window metrics.

    RUIN  — 1.10 drawdown breaches the ruin floor (overrides everything).
    FAIL  — 1.10 profit factor degrades vs the 1.15 baseline.
    PASS  — 1.10 holds its own on profit factor and survives the floor.
    """
    if maxdd_110_test < ruin_floor:
        return "RUIN"
    if pf_110_test < pf_115_test:
        return "FAIL"
    return "PASS"
