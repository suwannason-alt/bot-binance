"""Walk-forward optimization engine for the 1H breakout period.

Every ``WFO_RETUNE_INTERVAL`` bars the engine runs a mini-backtest over the
previous ``WFO_TRAINING_WINDOW`` bars for each breakout period in
``BREAKOUT_GRID``, scores each by Profit Factor (with a minimum-trade filter),
and applies the winner for the next interval.  This eliminates the need for a
static, hand-tuned ``BREAKOUT_PERIOD`` while avoiding lookahead bias.

Architecture
------------
::

    ┌─────────────────────────────────────────────────────────┐
    │  Training window: bars [end − 2160 : end]               │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │
    │  │  BP = 7     │  │  BP = 14    │  │  BP = 28    │     │
    │  │  PF = 1.82  │  │  PF = 2.31  │  │  PF = 1.14  │  … │
    │  └─────────────┘  └─────────────┘  └─────────────┘     │
    │          winner: BP = 14 (highest PF, n ≥ MIN_TRADES)   │
    └─────────────────────────────────────────────────────────┘
               │
               ▼  apply for next 720 bars
    ┌──────────────────────────┐
    │  active_bp = 14          │
    │  backtest uses roll_h[14]│
    └──────────────────────────┘

No lookahead bias: the training window ends at ``end_bar − 1`` (the last
*closed* bar before the retune decision).  The apply window begins at
``end_bar`` — the next bar that will be evaluated for entries.

Mini-backtest simplifications
-----------------------------
The per-BP mini-backtest uses:
  - Breakout entry: ``close > rolling_high[bp]`` (LONG) or
                    ``close < rolling_low[bp]``  (SHORT)
  - ADX ≥ ``config.ADX_MIN`` entry gate (same as live strategy)
  - Fixed ``config.ATR_SL_MULTIPLIER`` × ATR for SL
  - Fixed ``config.ATR_TP_MULTIPLIER`` × ATR for TP
  - No cooldown, no commission, no slippage (it is a *relative* scoring metric,
    not an absolute P&L projection)
  - Profit Factor = gross_win_atr / gross_loss_atr (normalised by ATR so that
    BP lengths with different typical ATR values are compared fairly)

Profit Factor is preferred over total return as the selection metric because:
  1. It is scale-invariant — independent of position sizing.
  2. It captures trade quality, not luck from one large outlier.
  3. It naturally favours BPs that consistently beat the SL threshold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config

logger = logging.getLogger("wfo")

# ---------------------------------------------------------------------------
# Grid of candidate breakout periods (bars)
# ---------------------------------------------------------------------------

BREAKOUT_GRID: List[int] = [7, 10, 14, 21, 28]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ActiveParams:
    """Currently active parameters selected by the most recent WFO retune.

    Attributes:
        breakout_period: Breakout window length (bars) in effect.
        profit_factor:   Profit Factor achieved by this period on the training window.
                         ``float('inf')`` when there were no losing trades.
        n_trades:        Number of completed trades during the training window.
        updated_bar:     1H bar index at which this retune was performed.
    """

    breakout_period: int   = 14
    profit_factor:   float = 0.0
    n_trades:        int   = 0
    updated_bar:     int   = -1


@dataclass
class WFOLogEntry:
    """Single entry in the WFO retune history log.

    Attributes:
        bar:    1H bar index of the retune.
        bp:     Selected breakout period.
        pf:     Achieved Profit Factor (clipped at 9.99 for display).
        n:      Number of training-window trades used in the score.
        scores: Dict of ``{bp: (pf, n)}`` for all grid candidates.
    """

    bar:    int
    bp:     int
    pf:     float
    n:      int
    scores: Dict[int, Tuple[float, int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Optimizer
# ---------------------------------------------------------------------------

class WalkForwardOptimizer:
    """Walk-forward breakout-period optimizer.

    Args:
        initial_bp: Breakout period to use before the first retune (while
                    the training window is still accumulating).  Defaults to
                    ``config.BREAKOUT_PERIOD``.
    """

    def __init__(self, initial_bp: Optional[int] = None) -> None:
        _bp = initial_bp if initial_bp is not None else config.BREAKOUT_PERIOD
        self.params:           ActiveParams    = ActiveParams(breakout_period=_bp)
        self._last_retune_bar: int             = -1
        self.log:              List[WFOLogEntry] = []

    # ------------------------------------------------------------------
    # Retune trigger
    # ------------------------------------------------------------------

    def should_retune(self, current_bar: int) -> bool:
        """Return ``True`` when a retune is due.

        A retune is triggered on the first bar after the training window has
        fully accumulated (``current_bar ≥ WFO_TRAINING_WINDOW``) and then
        every ``WFO_RETUNE_INTERVAL`` bars thereafter.

        Args:
            current_bar: Current 1H bar index (0-based).

        Returns:
            ``True`` when ``optimize()`` should be called.
        """
        if current_bar < config.WFO_TRAINING_WINDOW:
            return False
        if self._last_retune_bar < 0:
            return True   # first retune ever
        return (current_bar - self._last_retune_bar) >= config.WFO_RETUNE_INTERVAL

    # ------------------------------------------------------------------
    # Optimization
    # ------------------------------------------------------------------

    def optimize(
        self,
        c1:          np.ndarray,
        h1:          np.ndarray,
        l1:          np.ndarray,
        adx_arr:     np.ndarray,
        atr_arr:     np.ndarray,
        end_bar:     int,
        current_atr: Optional[float] = None,
    ) -> ActiveParams:
        """Run the mini-backtest sweep and update active parameters.

        Evaluates every period in :data:`BREAKOUT_GRID` over the training
        window ``[end_bar − window : end_bar]``.  The window length is
        normally ``WFO_TRAINING_WINDOW`` (90 days), but shrinks to
        ``WFO_FAST_TRAINING_WINDOW`` (14 days) when the current ATR
        exceeds ``WFO_FAST_ATR_MULT × mean_ATR_in_window`` — allowing
        faster adaptation out of a cold start or a sudden volatility shift.

        The period with the highest Profit Factor (subject to
        ``WFO_MIN_TRADES``) is selected.  If no period meets the
        trade-count minimum, the current ``active_bp`` is preserved.

        Args:
            c1:          1H close price array (full backtest history up to ``end_bar``).
            h1:          1H high price array.
            l1:          1H low price array.
            adx_arr:     1H ADX array.
            atr_arr:     1H ATR array.
            end_bar:     Index of the last *closed* bar before the apply window begins.
                         The training window ends here; the apply window starts at
                         ``end_bar + 1``.
            current_atr: ATR value at ``end_bar`` (used for dynamic lookback check).
                         Pass ``None`` to always use the standard window.

        Returns:
            Updated :class:`ActiveParams` with the winning breakout period.
        """
        # ── Dynamic lookback horizon ──────────────────────────────────────────
        # Shrink the training window when current ATR is much larger than its
        # long-run mean — this happens at the start of a new volatility regime
        # or after a cold-start with sparse history.  The shorter window scores
        # recent bars more heavily, letting the WFO snap to a new BREAKOUT_PERIOD
        # within days rather than waiting the full 90-day cycle.
        training_window = config.WFO_TRAINING_WINDOW
        _using_fast     = False

        if (
            config.WFO_FAST_ENABLED
            and current_atr is not None
            and not np.isnan(current_atr)
            and current_atr > 0
        ):
            # Compare current ATR against the mean ATR in the standard window
            _slice_start = max(0, end_bar - config.WFO_TRAINING_WINDOW)
            _atr_slice   = atr_arr[_slice_start:end_bar]
            _valid_atr   = _atr_slice[~np.isnan(_atr_slice)]
            if len(_valid_atr) >= 10:   # need at least 10 valid bars to trust the mean
                _mean_atr = float(_valid_atr.mean())
                if _mean_atr > 0 and (current_atr / _mean_atr) >= config.WFO_FAST_ATR_MULT:
                    training_window = config.WFO_FAST_TRAINING_WINDOW
                    _using_fast     = True
                    logger.info(
                        "WFO dynamic lookback: ATR spike (%.2f× mean %.0f) → "
                        "shrinking window %d → %d bars (%.1f days)",
                        current_atr / _mean_atr, _mean_atr,
                        config.WFO_TRAINING_WINDOW,
                        config.WFO_FAST_TRAINING_WINDOW,
                        config.WFO_FAST_TRAINING_WINDOW / 24.0,
                    )

        start_bar = max(0, end_bar - training_window)
        scores: Dict[int, Tuple[float, int]] = {}

        best_bp: int   = self.params.breakout_period   # preserve if no winner
        best_pf: float = -1.0
        best_n:  int   = 0

        for bp in BREAKOUT_GRID:
            pf, n = self._eval_bp(c1, h1, l1, adx_arr, atr_arr, start_bar, end_bar, bp)
            scores[bp] = (pf, n)
            if n >= config.WFO_MIN_TRADES and pf > best_pf:
                best_pf = pf
                best_bp = bp
                best_n  = n

        self.params = ActiveParams(
            breakout_period=best_bp,
            profit_factor=best_pf,
            n_trades=best_n,
            updated_bar=end_bar,
        )
        self._last_retune_bar = end_bar
        self.log.append(WFOLogEntry(
            bar=end_bar, bp=best_bp, pf=best_pf, n=best_n, scores=scores
        ))

        logger.debug(
            "WFO retune @ bar %d → BP=%d  PF=%.2f  n=%d  window=%d%s  "
            "scores=%s",
            end_bar, best_bp, best_pf, best_n,
            training_window,
            " [FAST]" if _using_fast else "",
            {k: f"{v[0]:.2f}/{v[1]}" for k, v in scores.items()},
        )
        return self.params

    # ------------------------------------------------------------------
    # Mini-backtest
    # ------------------------------------------------------------------

    def _eval_bp(
        self,
        c1:       np.ndarray,
        h1:       np.ndarray,
        l1:       np.ndarray,
        adx_arr:  np.ndarray,
        atr_arr:  np.ndarray,
        start:    int,
        end:      int,
        bp:       int,
    ) -> Tuple[float, int]:
        """Score a single breakout period on the training window.

        Runs a simplified LONG-only breakout scan over ``c1[start:end]``:

        - Entry: ``close > rolling_high[bp]`` AND ``ADX ≥ config.ADX_MIN``.
        - SL:    ``entry − ATR_SL_MULTIPLIER × ATR``
        - TP:    ``entry + ATR_TP_MULTIPLIER × ATR``
        - SL/TP are checked against the *next* bar's low/high (no same-bar exit).
        - One trade at a time; re-enter only after the previous trade closes.
        - No commission, slippage, or cooldown (relative scoring, not P&L).

        Profit Factor is computed in ATR-normalised units so that different
        ATR environments are compared fairly.

        Args:
            c1:    Close price array (full history).
            h1:    High price array.
            l1:    Low price array.
            adx_arr: ADX indicator array.
            atr_arr: ATR indicator array.
            start: Slice start index into the full arrays.
            end:   Slice end index (exclusive) into the full arrays.
            bp:    Breakout period to evaluate.

        Returns:
            Tuple ``(profit_factor, n_trades)`` where ``profit_factor`` is
            ``gross_win / gross_loss`` (normalised), or ``0.0`` if no trades.
        """
        n_slice = end - start
        if n_slice < bp + 2:
            return 0.0, 0

        # ── Rolling extremes over training slice (no lookahead) ────────────
        roll_h = (
            pd.Series(h1[start:end])
            .rolling(bp, min_periods=bp)
            .max()
            .shift(1)
            .to_numpy()
        )
        roll_l = (
            pd.Series(l1[start:end])
            .rolling(bp, min_periods=bp)
            .min()
            .shift(1)
            .to_numpy()
        )

        gross_win:  float = 0.0
        gross_loss: float = 0.0
        n_trades:   int   = 0

        in_long:   bool  = False
        in_short:  bool  = False
        trade_sl:  float = 0.0
        trade_tp:  float = 0.0

        for k in range(bp + 1, n_slice - 1):
            abs_k = start + k

            atr = float(atr_arr[abs_k])
            adx = float(adx_arr[abs_k])
            rh  = float(roll_h[k])
            rl  = float(roll_l[k])

            if np.isnan(atr) or np.isnan(adx) or np.isnan(rh) or np.isnan(rl):
                continue
            if atr <= 0:
                continue

            close = float(c1[abs_k])

            # ── Exit management (check next bar) ──────────────────────────
            if in_long or in_short:
                next_h = float(h1[abs_k + 1])
                next_l = float(l1[abs_k + 1])

                if in_long:
                    hit_tp = next_h >= trade_tp
                    hit_sl = next_l <= trade_sl
                    if hit_tp or hit_sl:
                        # Score in ATR units (scale-invariant)
                        pnl_atr = (config.ATR_TP_MULTIPLIER if hit_tp
                                   else -config.ATR_SL_MULTIPLIER)
                        if pnl_atr > 0:
                            gross_win += pnl_atr
                        else:
                            gross_loss += abs(pnl_atr)
                        n_trades += 1
                        in_long = False

                elif in_short:
                    hit_tp = next_l <= trade_tp
                    hit_sl = next_h >= trade_sl
                    if hit_tp or hit_sl:
                        pnl_atr = (config.ATR_TP_MULTIPLIER if hit_tp
                                   else -config.ATR_SL_MULTIPLIER)
                        if pnl_atr > 0:
                            gross_win += pnl_atr
                        else:
                            gross_loss += abs(pnl_atr)
                        n_trades += 1
                        in_short = False

                if in_long or in_short:
                    continue   # position still open, skip entry logic

            # ── Entry logic ───────────────────────────────────────────────
            if adx < config.ADX_MIN:
                continue

            if close > rh:
                # LONG breakout
                in_long   = True
                trade_sl  = close - config.ATR_SL_MULTIPLIER * atr
                trade_tp  = close + config.ATR_TP_MULTIPLIER * atr

            elif close < rl:
                # SHORT breakout
                in_short  = True
                trade_sl  = close + config.ATR_SL_MULTIPLIER * atr
                trade_tp  = close - config.ATR_TP_MULTIPLIER * atr

        # ── Profit Factor ─────────────────────────────────────────────────
        if gross_loss == 0.0:
            pf = float("inf") if gross_win > 0.0 else 0.0
        else:
            pf = gross_win / gross_loss

        return pf, n_trades
