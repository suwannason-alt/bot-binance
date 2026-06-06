"""Live/backtest trailing-stop parity test (no pytest — standalone runner).

Proves that the live trail logic in ``trader.Trader`` and the backtest trail
logic in ``backtest`` produce the *same* stop-loss price for the same inputs,
for BOTH the classic cascade and the adaptive funnel.  This is the acceptance
test for the CLAUDE.md live/backtest sync contract: before the dispatch fix the
live path always ran the classic cascade while the backtest honoured
``ADAPTIVE_TRAILING_ENABLED`` — so enabling adaptive trailing silently diverged
the two.

Live mode sees only a single mark price (no intrabar high/low), so the backtest
side is fed ``bar_high == bar_low == price`` to make the comparison apples-to-apples.

Run:  python test_trail_parity.py     (exits non-zero on any mismatch)
"""
from __future__ import annotations

import sys

import pandas as pd

import config
import backtest
from trader import Trader, Position

_TOL = 1e-9


def _make_pair(side: str, entry: float, atr: float, sl: float, tp: float):
    """Build a live Position and a backtest _OpenPosition with identical state."""
    live = Position(side=side, entry=entry, qty=1.0, sl=sl, tp=tp, initial_atr=atr)
    bt = backtest._OpenPosition(
        side=side, entry_price=entry, sl=sl, tp=tp, qty=1.0,
        entry_time=pd.Timestamp("2024-01-01"), bar_idx=0, entry_atr=atr,
    )
    return live, bt


def _price_path(entry: float, atr: float, side: str) -> list[float]:
    """Ascend strongly in favour, then reverse — exercises activation + ratchet."""
    steps = [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.5, 6.0, 5.0, 4.0, 2.0]  # ×ATR in favour
    sign = 1.0 if side == "LONG" else -1.0
    return [entry + sign * s * atr for s in steps]


def _run_case(trader: Trader, side: str, adaptive: bool, *,
              activate: float, lock: float, stop: float) -> None:
    entry, atr = 100.0, 1.0
    sl = entry - 1.5 * atr if side == "LONG" else entry + 1.5 * atr
    tp = entry + 6.0 * atr if side == "LONG" else entry - 6.0 * atr

    # Pin the full trail config on the module both paths read, so the comparison
    # is hermetic regardless of .env / _BASE drift.
    config.ADAPTIVE_TRAILING_ENABLED = adaptive
    config.TRAIL_ACTIVATE_ATR = activate
    config.TRAIL_LOCK_ATR = lock
    config.TRAIL_STOP_ATR = stop
    live, bt = _make_pair(side, entry, atr, sl, tp)

    for price in _price_path(entry, atr, side):
        trader._apply_trail(live, price)                       # live dispatcher
        backtest._update_trailing_stop(bt, bar_high=price, bar_low=price)
        diff = abs(live.sl - bt.sl)
        assert diff < _TOL, (
            f"[{'ADAPT' if adaptive else 'CLASSIC'} {side}] SL diverged at "
            f"price={price:.4f}: live={live.sl:.10f}  backtest={bt.sl:.10f}  "
            f"diff={diff:.2e}"
        )
    print(f"  ✓ {'adaptive' if adaptive else 'classic ':8} {side:5} — "
          f"final SL live={live.sl:.4f} == backtest={bt.sl:.4f}")


def main() -> int:
    config.ATR_SL_MULTIPLIER = 1.5
    config.ADAPTIVE_TRAIL_MIN_ATR = 0.35

    trader = Trader()  # paper mode (PAPER_TRADING default true) — no exchange init

    # 1) Classic + adaptive sweep — BE activation + dynamic trail (LOCK off).
    print("Live/backtest trailing-stop parity — classic/adaptive sweep")
    for adaptive in (False, True):
        for side in ("LONG", "SHORT"):
            _run_case(trader, side, adaptive, activate=2.0, lock=0.0, stop=1.5)

    # 2) Live deploy profile — exercises ALL THREE classic stages, including the
    #    LOCK stage (TRAIL_LOCK_ATR>0) that stage (1) never reaches.  This is the
    #    config actually shipping to live: BE@1.5 → LOCK@2.0 → TRAIL@1.2.
    print("Live deploy profile (classic ACTIVATE=1.5 LOCK=2.0 STOP=1.2)")
    for side in ("LONG", "SHORT"):
        _run_case(trader, side, adaptive=False, activate=1.5, lock=2.0, stop=1.2)

    print("ALL PARITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
