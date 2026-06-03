"""Multi-asset ATR_RATIO sweep — offline research tool.

Screens Binance perpetual pairs for compatibility with the loose
ATR_RATIO_MIN = 1.10 volatility gate, using out-of-sample, per-asset
1.10-vs-1.15 head-to-head validation. See
docs/superpowers/specs/2026-06-03-multi-asset-atr-sweep-design.md.

Run:  python sweep_assets.py            # full sweep, 5y, default candidates
      python sweep_assets.py --days 730 # custom window
"""
from __future__ import annotations

from typing import Tuple

import pandas as pd

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


def split_by_time(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                  frac: float = SPLIT_FRAC
                  ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split both feeds at one shared wall-clock boundary.

    The boundary is the 1h ``open_time`` at the ``frac`` index, so train and
    test windows are identical across the 5m and 1h feeds.

    Returns ``(train_5m, train_1h, test_5m, test_1h)`` with reset indices.
    """
    boundary = int(df_1h["open_time"].iloc[int(len(df_1h) * frac)])
    tr1 = df_1h[df_1h["open_time"] < boundary].reset_index(drop=True)
    te1 = df_1h[df_1h["open_time"] >= boundary].reset_index(drop=True)
    tr5 = df_5m[df_5m["open_time"] < boundary].reset_index(drop=True)
    te5 = df_5m[df_5m["open_time"] >= boundary].reset_index(drop=True)
    return tr5, tr1, te5, te1
