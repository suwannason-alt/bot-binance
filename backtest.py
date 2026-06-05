"""
Vectorised backtesting engine.

Assumptions:
    - Entry at the CLOSE price of the signal candle.
    - SL/TP are checked against the *next* candle's high/low (no same-bar look-ahead).
    - Commission: 0.05 % per fill.  Slippage: 0.02 % per fill.
    - Daily profit/loss limits are evaluated after each closed trade.
"""
from __future__ import annotations

import gc
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import indicators as ind
from adaptive_regime import compute_adaptive_regime, hurst_exponent
from regime_forecast import MarkovRegimeForecaster, RegimeForecast
from strategy import Signal, evaluate_1h_signal, evaluate_from_indicators
from walk_forward_optimizer import (
    BREAKOUT_GRID,
    ActiveParams,
    WalkForwardOptimizer,
)

logger = logging.getLogger("backtest")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMMISSION: float = 0.0005  # 0.05 % taker fee per fill
_SLIPPAGE: float = 0.0002    # 0.02 % market impact per fill
_MIN_BALANCE: float = 10.0   # stop backtest if balance falls below this


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Trade:
    """A completed trade record produced by the backtest engine."""

    side: str
    entry_time: object
    exit_time: Optional[object]
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    close_reason: str   # "TP" | "SL" | "BE" | "EOD"
    balance_after: float
    bars_held: int


@dataclass
class BacktestResult:
    """Aggregated results returned by :func:`run`."""

    trades: List[Trade]
    equity_curve: pd.Series
    stats: dict
    df_5m: pd.DataFrame
    df_1h: pd.DataFrame
    daily_pnl: pd.Series              # daily PnL as % of day-start balance
    wfo_log: List[Dict] = field(default_factory=list)   # WFO retune history


@dataclass
class StrategyState:
    """Live autonomous strategy state — updated each 1H bar.

    Tracks the outputs of the Walk-Forward Optimizer and the Markov Regime
    Forecaster so that the main loop can gate entries and adjust sizing
    without modifying ``config`` at runtime.

    Attributes:
        active_bp:          Current breakout window (bars), selected by WFO.
        current_regime:     Most recently classified regime (0=TREND, 1=CHOPPY, 2=QUIET).
        trend_prob:         Forecast probability of the next bar being TREND.
        choppy_prob:        Forecast probability of the next bar being CHOPPY.
        entry_allowed:      ``False`` when the forecast blocks new entries.
        size_scale:         Position-size multiplier from forecast confidence [0.5, 1.0].
        effective_cooldown: Minimum bars between entries (may be extended by forecast).
    """

    active_bp:          int   = 14
    current_regime:     int   = 0     # TREND
    trend_prob:         float = 0.33
    choppy_prob:        float = 0.33
    entry_allowed:      bool  = True
    size_scale:         float = 1.0
    effective_cooldown: int   = 1


@dataclass
class _OpenPosition:
    """Internal mutable state for an open simulated position."""

    side: str
    entry_price: float
    sl: float
    tp: float
    qty: float
    entry_time: pd.Timestamp
    bar_idx: int
    entry_atr: float
    be_activated: bool = False
    lock_activated: bool = False
    trail_peak: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _apply_slippage(price: float, side: str, is_entry: bool) -> float:
    """Adjust a fill price for market-impact slippage.

    Entries receive adverse fill (slightly worse than signal price); exits do
    the same so round-trip cost is 2 × _SLIPPAGE.

    Args:
        price:    Raw signal price.
        side:     ``"LONG"`` or ``"SHORT"``.
        is_entry: ``True`` for entry fills, ``False`` for exit fills.

    Returns:
        Slippage-adjusted fill price.
    """
    direction = 1 if (side == "LONG") == is_entry else -1
    return price * (1.0 + direction * _SLIPPAGE)


def _commission_cost(price: float, qty: float) -> float:
    """Return the commission charged for a single fill.

    Args:
        price: Fill price.
        qty:   Contract quantity.

    Returns:
        Commission amount in quote currency.
    """
    return price * qty * _COMMISSION


def _position_qty(
    balance: float,
    entry: float,
    sl: float,
    atr_ratio: Optional[float] = None,
    size_scale: float = 1.0,
) -> float:
    """Compute the contract quantity for a new simulated position.

    Mirrors the live :func:`strategy.position_size_usdt` function exactly so
    that backtest results are directly comparable to live trading performance.

    Sizing priority (highest → lowest):

    0. ``EQUITY_PERCENT > 0``  →  **dynamic equity-percentage** (default).
       ``margin  = balance × EQUITY_PERCENT%``
       ``notional = margin × LEVERAGE``
       ``qty      = notional / entry``
       Compounds naturally with the running equity curve.

    1. ``ORDER_BALANCE_USD > 0``  →  fixed margin × leverage (no compounding).
    2. ``RISK_USD > 0``           →  fixed-dollar risk, capped by margin limit.
    3. ``RISK_PERCENT``           →  % of balance / SL distance, optionally vol-adjusted.

    Args:
        balance:    Running equity at the time of the signal bar.
        entry:      Anticipated fill price (after slippage).
        sl:         Stop-loss price.
        atr_ratio:  Current ATR / 20-bar ATR SMA; used for volatility-adjusted sizing
                    (mode 3 only).
        size_scale: Combined regime + forecast multiplier applied to the final qty.

    Returns:
        Contract quantity (BTC for BTCUSDT), or 0.0 if sizing fails.
    """
    # ── Mode 0: Dynamic equity-percentage ─────────────────────────────────────
    if config.EQUITY_PERCENT > 0:
        margin_usd     = balance * (config.EQUITY_PERCENT / 100.0)
        notional       = margin_usd * max(config.LEVERAGE, 1)
        return (notional / entry) * size_scale

    # ── Mode 1: Fixed order-balance ────────────────────────────────────────────
    if config.ORDER_BALANCE_USD > 0:
        notional = config.ORDER_BALANCE_USD * max(config.LEVERAGE, 1)
        return (notional / entry) * size_scale

    # ── Modes 2 / 3: Risk-based sizing ────────────────────────────────────────
    if config.RISK_USD > 0:
        risk_dollar = config.RISK_USD
    else:
        base_pct = config.RISK_PERCENT
        if config.VOL_SIZING_ENABLED and atr_ratio is not None and atr_ratio > 0:
            vol_scale = max(
                config.VOL_SIZING_MIN_SCALE,
                min(config.VOL_SIZING_MAX_SCALE, 1.0 / atr_ratio),
            )
            base_pct *= vol_scale
        risk_dollar = balance * (base_pct / 100.0)

    risk_dollar *= size_scale

    sl_distance = abs(entry - sl) / entry
    if sl_distance == 0:
        return 0.0

    qty = risk_dollar / (entry * sl_distance)
    if config.LEVERAGE > 0:
        qty = min(qty, balance * config.LEVERAGE / entry)
    return qty


def _build_rolling_extremes(
    highs: np.ndarray,
    lows: np.ndarray,
    period: int,
) -> Tuple[np.ndarray, np.ndarray]:
    """Compute lookahead-free rolling high and rolling low arrays.

    At bar ``k``, ``roll_high[k]`` is the max of ``highs[k-period : k]`` (the
    ``period`` bars *before* bar ``k``, exclusive).  This avoids using the current
    bar's data as the breakout trigger.

    Uses ``pandas.Series.rolling`` for efficiency.

    Args:
        highs:  1-D array of bar high prices.
        lows:   1-D array of bar low prices.
        period: Rolling window length.

    Returns:
        Tuple ``(roll_high, roll_low)`` of float arrays with leading NaN.
    """
    roll_high = pd.Series(highs).rolling(period, min_periods=period).max().shift(1).to_numpy()
    roll_low = pd.Series(lows).rolling(period, min_periods=period).min().shift(1).to_numpy()
    return roll_high, roll_low


def _apply_classic_trail(position: _OpenPosition, bar_high: float, bar_low: float) -> None:
    """Apply the classic three-stage trailing cascade to an open position.

    This is the original exit logic extracted into its own function so that
    :func:`_update_trailing_stop` can cleanly dispatch to either classic or
    adaptive mode.

    Stage 1 — **Break-even** (``TRAIL_ACTIVATE_ATR``):
        Move SL to the entry price once price has moved ``N×ATR`` in favour.
        Eliminates the risk of a profitable trade turning into a loss.

    Stage 2 — **Profit lock** (``TRAIL_LOCK_ATR``):
        Advance SL to ``entry + 1×ATR`` (a guaranteed +1R profit) once price
        has moved ``M×ATR`` in favour.  Prevents a large winner from closing at
        BE after a deep pullback.

    Stage 3 — **Dynamic trail** (``TRAIL_STOP_ATR``):
        After Stage 1 activates, SL tracks the running peak at a fixed
        ``−N×ATR`` offset.  Lets the trade run in a trending market.

    Args:
        position: The open position to modify in-place.
        bar_high: Current bar's high price.
        bar_low:  Current bar's low price.
    """
    atr   = position.entry_atr
    entry = position.entry_price
    price = bar_high if position.side == "LONG" else bar_low

    # Stage 1: break-even activation
    if not position.be_activated and config.TRAIL_ACTIVATE_ATR > 0:
        favour = (price - entry) if position.side == "LONG" else (entry - price)
        if favour >= config.TRAIL_ACTIVATE_ATR * atr:
            new_sl = entry
            if (position.side == "LONG" and new_sl > position.sl) or \
               (position.side == "SHORT" and new_sl < position.sl):
                position.sl = new_sl
            position.be_activated = True
            position.trail_peak   = price

    if not position.be_activated:
        return

    # Update running peak
    if position.side == "LONG":
        position.trail_peak = max(position.trail_peak, bar_high)
    else:
        position.trail_peak = min(position.trail_peak, bar_low) if position.trail_peak else bar_low

    # Stage 2: profit lock
    if not position.lock_activated and config.TRAIL_LOCK_ATR > 0:
        favour = (price - entry) if position.side == "LONG" else (entry - price)
        if favour >= config.TRAIL_LOCK_ATR * atr:
            locked_sl = (entry + atr) if position.side == "LONG" else (entry - atr)
            if (position.side == "LONG" and locked_sl > position.sl) or \
               (position.side == "SHORT" and locked_sl < position.sl):
                position.sl = locked_sl
            position.lock_activated = True

    # Stage 3: dynamic trailing stop
    if config.TRAIL_STOP_ATR > 0:
        if position.side == "LONG":
            trail_sl = position.trail_peak - config.TRAIL_STOP_ATR * atr
            if trail_sl > position.sl:
                position.sl = trail_sl
        else:
            trail_sl = position.trail_peak + config.TRAIL_STOP_ATR * atr
            if trail_sl < position.sl:
                position.sl = trail_sl


def _apply_adaptive_trail(position: _OpenPosition, bar_high: float, bar_low: float) -> None:
    """Apply a single tightening trailing funnel to an open position.

    Replaces the three discrete classic stages with a continuous trail whose
    ATR-distance shrinks linearly as price advances from entry toward the TP
    target.  This turns every "break-even exit" into a profitable exit
    capturing 70–95% of the TP move.

    Mathematical model::

        progress   = clamp((peak − entry) / (tp − entry),  0, 1)   # LONG
        trail_dist = ATR_SL_MULTIPLIER × (1 − progress)
                   + ADAPTIVE_TRAIL_MIN_ATR × progress
                   # = linear lerp: wide at activation → tight near TP

        SL_new = peak − trail_dist × ATR   (LONG; reverse signs for SHORT)

    Activation gate:
        The trail activates only after price moves ``TRAIL_ACTIVATE_ATR × ATR``
        in favour (same threshold as the classic Stage 1 break-even).  Before
        activation the original hard SL is untouched — this guards against
        early whipsaw exits near the entry.

    Example (LONG, entry = 100, ATR = 1, SL_MULT = 1.5, TP_MULT = 9, MIN = 0.35):

        +-----------+-----------+----------+---------+-----------------+
        | progress  | fav. ATRs | trail×   | SL ≈    | move protected  |
        +-----------+-----------+----------+---------+-----------------+
        | 0.00 (BE) |   1.5     | 1.50×    |  100.0  | 0 % (break-even)|
        | 0.17      |   1.5     | 1.305×   |  100.2  | — just BE+       |
        | 0.50      |   4.5     | 0.925×   |  103.6  | 40 %            |
        | 0.85      |   7.65    | 0.523×   |  107.1  | 79 %            |
        | 1.00      |   9.0     | 0.35×    |  108.7  | 96 %            |
        +-----------+-----------+----------+---------+-----------------+

    Args:
        position: The open position to modify in-place.
        bar_high: Current bar's high price.
        bar_low:  Current bar's low price.
    """
    atr     = position.entry_atr
    entry   = position.entry_price
    tp      = position.tp
    is_long = position.side == "LONG"
    price   = bar_high if is_long else bar_low

    # ── Activation gate — same threshold as TRAIL_ACTIVATE_ATR ───────────────
    act_thr = config.TRAIL_ACTIVATE_ATR
    if not position.be_activated:
        # When act_thr == 0, activate immediately (no minimum profit requirement).
        favour = (price - entry) if is_long else (entry - price)
        if act_thr > 0 and favour < act_thr * atr:
            return   # not yet in sufficient profit to begin trailing
        position.be_activated = True
        position.trail_peak   = price

    # ── Update running peak ───────────────────────────────────────────────────
    if is_long:
        position.trail_peak = max(position.trail_peak, bar_high)
    else:
        position.trail_peak = (min(position.trail_peak, bar_low)
                               if position.trail_peak > 0 else bar_low)

    peak = position.trail_peak

    # ── Progress toward TP  [0.0 = just activated … 1.0 = peak at TP] ────────
    tp_dist     = abs(tp - entry)
    favour_peak = (peak - entry) if is_long else (entry - peak)
    progress    = float(np.clip(favour_peak / tp_dist if tp_dist > 0 else 0.0, 0.0, 1.0))

    # ── Adaptive trail distance — linear interpolation ────────────────────────
    #
    #   max_trail = ATR_SL_MULTIPLIER   (original SL distance — widest point)
    #   min_trail = ADAPTIVE_TRAIL_MIN_ATR (tightest, applied at TP level)
    #
    #   At progress = 0.0: trail = max_trail  (≈ BE — SL moves to entry)
    #   At progress = 1.0: trail = min_trail  (very tight — captures ~96% of TP)
    max_trail = config.ATR_SL_MULTIPLIER
    min_trail = config.ADAPTIVE_TRAIL_MIN_ATR
    trail_atr = max_trail + (min_trail - max_trail) * progress   # lerp

    trail_price = (peak - trail_atr * atr if is_long else peak + trail_atr * atr)

    # ── Ratchet — SL moves only in the favourable direction ───────────────────
    if is_long and trail_price > position.sl:
        position.sl = trail_price
    elif not is_long and trail_price < position.sl:
        position.sl = trail_price


def _update_trailing_stop(position: _OpenPosition, bar_high: float, bar_low: float) -> None:
    """Dispatch trailing-stop logic to the correct implementation.

    When ``ADAPTIVE_TRAILING_ENABLED`` is ``True``, delegates to
    :func:`_apply_adaptive_trail`, which replaces all three classic stages with
    a single continuous tightening funnel.

    When ``ADAPTIVE_TRAILING_ENABLED`` is ``False`` (default), delegates to
    :func:`_apply_classic_trail` (break-even → profit lock → dynamic trail).

    Args:
        position: The open position to modify in-place.
        bar_high: Current bar's high price.
        bar_low:  Current bar's low price.
    """
    if position.entry_atr <= 0:
        return

    if config.ADAPTIVE_TRAILING_ENABLED:
        _apply_adaptive_trail(position, bar_high, bar_low)
    else:
        _apply_classic_trail(position, bar_high, bar_low)


def _check_daily_limits(
    balance: float,
    day_start_balance: float,
) -> bool:
    """Return ``True`` if today's profit/loss threshold has been reached.

    Args:
        balance:           Current account balance.
        day_start_balance: Balance at the start of the current trading day.

    Returns:
        ``True`` when further entries should be blocked for today.
    """
    day_pnl = balance - day_start_balance

    profit_threshold = (
        config.DAILY_PROFIT_TARGET_USD if config.DAILY_PROFIT_TARGET_USD > 0
        else day_start_balance * config.DAILY_PROFIT_TARGET_PCT
    )
    loss_threshold = (
        config.DAILY_LOSS_LIMIT_USD if config.DAILY_LOSS_LIMIT_USD > 0
        else day_start_balance * config.DAILY_LOSS_LIMIT_PCT
    )

    if profit_threshold > 0 and day_pnl >= profit_threshold:
        return True
    if loss_threshold > 0 and day_pnl <= -loss_threshold:
        return True
    return False


# ---------------------------------------------------------------------------
# Main backtest runner
# ---------------------------------------------------------------------------

def run(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    initial_balance: float = 1000.0,
    mode: str = "1h",
) -> BacktestResult:
    """Run a full vectorised backtest over the supplied OHLCV data.

    Args:
        df_5m:            5-minute candle DataFrame (from :func:`fetch_data.fetch_all`).
        df_1h:            1-hour candle DataFrame.
        initial_balance:  Starting account balance in USDT (default 1 000).
        mode:             Strategy mode.  Supported values:

                          - ``"1h"``          – 1H swing strategy (proven default).
                          - ``"5m"``          – 5M scalp with 1H trend filter.
                          - ``"5m_breakout"`` – 5M breakout using 1H ATR for SL/TP.
                          - ``"5m_1h_cross"`` – 5M timing + full 1H signal quality.
                          - ``"daily_open"``  – One entry per day, EMA20/50/200 aligned.
                          - ``"daily_trend"`` – One entry per day, EMA200 bias only.

    Returns:
        A :class:`BacktestResult` containing all trades, the equity curve,
        summary statistics, and a daily PnL series.
    """
    # ── 5M indicator arrays ──────────────────────────────────────────────────
    h5 = df_5m["high"].values
    l5 = df_5m["low"].values
    c5 = df_5m["close"].values
    v5 = df_5m["volume"].values.astype(float)
    ct_5m = df_5m["close_time"].values.astype(np.int64)

    ema_fast_5m = ind.ema(c5, config.EMA_FAST)
    ema_slow_5m = ind.ema(c5, config.EMA_SLOW)
    rsi_5m = ind.rsi(c5, config.RSI_PERIOD)
    _, _, hist_5m = ind.macd(c5, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    atr_5m = ind.atr(h5, l5, c5, config.ATR_PERIOD)
    vol_sma_5m = ind.sma(v5, 20)

    roll_high_5m, roll_low_5m = _build_rolling_extremes(h5, l5, config.BREAKOUT_PERIOD_5M)

    # ── 1H indicator arrays ──────────────────────────────────────────────────
    c1 = df_1h["close"].values
    o1 = df_1h["open"].values
    l1 = df_1h["low"].values
    h1 = df_1h["high"].values
    v1 = df_1h["volume"].values.astype(float)
    ct_1h = df_1h["close_time"].values.astype(np.int64)

    ema_fast_1h = ind.ema(c1, config.EMA_FAST)
    ema_slow_1h = ind.ema(c1, config.EMA_SLOW)
    ema_trend_1h = ind.ema(c1, config.EMA_TREND)
    rsi_1h = ind.rsi(c1, config.RSI_PERIOD)
    _, _, hist_1h = ind.macd(c1, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    atr_1h = ind.atr(h1, l1, c1, config.ATR_PERIOD)
    vol_sma_1h = ind.sma(v1, 20)
    atr_sma_1h = ind.sma(atr_1h, 20)
    adx_1h = ind.adx(h1, l1, c1, config.ADX_PERIOD)

    roll_high_1h, roll_low_1h = _build_rolling_extremes(h1, l1, config.BREAKOUT_PERIOD)

    # ── WFO: pre-compute rolling extremes for every grid breakout period ──────
    # Indexed by breakout period so that _active_bp_ can be switched without
    # recomputing (rolling is vectorised; no per-bar cost in the main loop).
    roll_high_grid: Dict[int, np.ndarray] = {config.BREAKOUT_PERIOD: roll_high_1h}
    roll_low_grid:  Dict[int, np.ndarray] = {config.BREAKOUT_PERIOD: roll_low_1h}
    if config.WFO_ENABLED:
        for _bp_g in BREAKOUT_GRID:
            if _bp_g not in roll_high_grid:
                roll_high_grid[_bp_g], roll_low_grid[_bp_g] = _build_rolling_extremes(
                    h1, l1, _bp_g
                )

    # ── Autonomous strategy components ────────────────────────────────────────
    wfo:        Optional[WalkForwardOptimizer]  = (
        WalkForwardOptimizer() if config.WFO_ENABLED else None
    )
    forecaster: Optional[MarkovRegimeForecaster] = (
        MarkovRegimeForecaster() if config.REGIME_FORECAST_ENABLED else None
    )
    strat_state: StrategyState = StrategyState(
        active_bp          = config.BREAKOUT_PERIOD,
        effective_cooldown = config.TRADE_COOLDOWN_1H,
    )

    # Map each 5M bar to the index of the last closed 1H bar
    idx_1h = np.searchsorted(ct_1h, ct_5m, side="right") - 1

    # ── Simulation state ──────────────────────────────────────────────────────
    n = len(df_5m)
    balance = initial_balance
    trades: List[Trade] = []
    equity_times: List[pd.Timestamp] = [pd.Timestamp(df_5m["open_time"].iloc[0], unit="ms")]
    equity_vals: List[float] = [balance]

    position: Optional[_OpenPosition] = None
    last_trade_1h_bar: int = -9999
    last_trade_5m_bar: int = -9999
    prev_1h_bar: int = -1
    _first_valid_j: int = -1     # first 1H bar that passes all NaN guards (cooldown anchor)

    # ── Daily tracking ────────────────────────────────────────────────────────
    current_day_epoch: int = -1
    day_start_balance: float = initial_balance
    daily_target_hit: bool = False
    daily_profit_hit: bool = False
    days_target_hit: int = 0
    days_profit_hit: int = 0
    days_loss_hit: int = 0
    daily_records: List[Tuple] = []

    # Daily-open mode state: track the last day an entry was taken
    last_daily_entry_day: int = -1

    warmup = max(config.MIN_CANDLES_5M, config.MIN_CANDLES_1H)

    for i in range(warmup, n - 1):
        j = int(idx_1h[i])
        if j < config.MIN_CANDLES_1H:
            continue

        # ── Daily reset on calendar-day change ───────────────────────────────
        bar_day = int(ct_5m[i]) // 86_400_000
        if bar_day != current_day_epoch:
            if current_day_epoch >= 0:
                day_pnl_pct = (balance - day_start_balance) / day_start_balance * 100
                daily_records.append((
                    pd.Timestamp(current_day_epoch * 86_400_000, unit="ms").date(),
                    day_pnl_pct,
                ))
                if daily_target_hit:
                    days_target_hit += 1
                if daily_profit_hit:
                    days_profit_hit += 1
                elif daily_target_hit:
                    days_loss_hit += 1
            current_day_epoch = bar_day
            day_start_balance = balance
            daily_target_hit = False
            daily_profit_hit = False

        # ══════════════════════════════════════════════════════════════════════
        # STEP 1 — EXIT MANAGEMENT (ADAPTIVE_TRAILING_ENABLED)
        # ──────────────────────────────────────────────────────────────────────
        # _update_trailing_stop() dispatches to one of two implementations:
        #
        #   ADAPTIVE_TRAILING_ENABLED = True  →  _apply_adaptive_trail()
        #     Single tightening funnel: trail_dist shrinks from ATR_SL_MULTIPLIER
        #     (at activation) → ADAPTIVE_TRAIL_MIN_ATR (at TP level).  Replaces
        #     the discrete BE / lock / dynamic-trail cascade.
        #
        #   ADAPTIVE_TRAILING_ENABLED = False (default)  →  _apply_classic_trail()
        #     Stage 1: move SL to entry (BE) after TRAIL_ACTIVATE_ATR × ATR gain.
        #     Stage 2: advance SL to entry + 1×ATR at TRAIL_LOCK_ATR × ATR.
        #     Stage 3: trail SL at -TRAIL_STOP_ATR × ATR from running peak.
        # ══════════════════════════════════════════════════════════════════════
        if position is not None:
            _update_trailing_stop(position, bar_high=h5[i], bar_low=l5[i])

            nxt_high = h5[i + 1]
            nxt_low = l5[i + 1]
            nxt_time = pd.Timestamp(ct_5m[i + 1], unit="ms")

            hit_sl = (nxt_low <= position.sl) if position.side == "LONG" else (nxt_high >= position.sl)
            hit_tp = (nxt_high >= position.tp) if position.side == "LONG" else (nxt_low <= position.tp)

            if hit_sl or hit_tp:
                close_reason = "TP" if hit_tp else ("BE" if position.be_activated else "SL")
                raw_exit = position.sl if hit_sl else position.tp
                exit_price = _apply_slippage(raw_exit, position.side, is_entry=False)
                direction = 1 if position.side == "LONG" else -1

                gross_pnl = (exit_price - position.entry_price) * direction * position.qty
                net_pnl = gross_pnl - _commission_cost(exit_price, position.qty)
                balance += net_pnl

                trades.append(Trade(
                    side=position.side,
                    entry_time=position.entry_time,
                    exit_time=nxt_time,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    qty=position.qty,
                    pnl=net_pnl,
                    pnl_pct=net_pnl / (position.entry_price * position.qty) * 100,
                    close_reason=close_reason,
                    balance_after=balance,
                    bars_held=i + 1 - position.bar_idx,
                ))
                equity_times.append(nxt_time)
                equity_vals.append(balance)
                last_trade_1h_bar = j
                last_trade_5m_bar = i
                position = None

                # Evaluate daily limits after each trade
                if _check_daily_limits(balance, day_start_balance):
                    daily_target_hit = True
                    day_pnl = balance - day_start_balance
                    profit_threshold = (
                        config.DAILY_PROFIT_TARGET_USD if config.DAILY_PROFIT_TARGET_USD > 0
                        else day_start_balance * config.DAILY_PROFIT_TARGET_PCT
                    )
                    if profit_threshold > 0 and day_pnl >= profit_threshold:
                        daily_profit_hit = True

                if balance <= _MIN_BALANCE:
                    logger.warning("Balance critically low — stopping backtest")
                    break
                continue

        if position is not None:
            if j > prev_1h_bar:
                prev_1h_bar = j
            continue

        if daily_target_hit:
            continue

        # ── Signal evaluation ─────────────────────────────────────────────────
        signal: Optional[Signal] = None

        if mode == "5m":
            if i < 1:
                continue
            needed_5m = [ema_fast_5m[i], rsi_5m[i], atr_5m[i], hist_5m[i], hist_5m[i - 1]]
            if any(np.isnan(v) for v in needed_5m):
                continue
            if j < 1 or any(np.isnan([ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j], rsi_1h[j]])):
                continue
            if i - last_trade_5m_bar < config.TRADE_COOLDOWN_5M:
                continue
            signal = evaluate_from_indicators(
                i5={
                    "ema_fast":       float(ema_fast_5m[i]),
                    "rsi":            float(rsi_5m[i]),
                    "atr":            float(atr_5m[i]),
                    "macd_hist":      float(hist_5m[i]),
                    "macd_hist_prev": float(hist_5m[i - 1]),
                    "close":          float(c5[i]),
                },
                i1={
                    "ema_fast":  float(ema_fast_1h[j]),
                    "ema_slow":  float(ema_slow_1h[j]),
                    "ema_trend": float(ema_trend_1h[j]),
                    "rsi":       float(rsi_1h[j]),
                    "close":     float(c1[j]),
                },
                current_price=float(c5[i]),
                bars_since_last_trade=9999,
            )

        elif mode == "5m_breakout":
            if i < 1:
                continue
            needed_5m = [ema_fast_5m[i], ema_slow_5m[i], rsi_5m[i],
                         atr_5m[i], hist_5m[i], hist_5m[i - 1]]
            if any(np.isnan(v) for v in needed_5m):
                continue
            if j < 1 or np.isnan(ema_trend_1h[j]) or np.isnan(atr_1h[j]):
                continue
            if np.isnan(roll_high_5m[i]) or np.isnan(roll_low_5m[i]):
                continue
            if i - last_trade_5m_bar < config.TRADE_COOLDOWN_5M:
                continue

            slope_bars = config.EMA_TREND_SLOPE_BARS
            j_slope = j - slope_bars
            ema_t_prev = (
                float(ema_trend_1h[j_slope])
                if j_slope >= 0 and not np.isnan(ema_trend_1h[j_slope]) else None
            )
            vol_ratio = (
                float(v5[i] / vol_sma_5m[i])
                if not np.isnan(vol_sma_5m[i]) and vol_sma_5m[i] > 0 else None
            )
            signal = evaluate_1h_signal(
                i1={
                    "ema_fast":       float(ema_fast_5m[i]),
                    "ema_slow":       float(ema_slow_5m[i]),
                    "ema_trend":      float(ema_trend_1h[j]),
                    "ema_trend_prev": ema_t_prev,
                    "rsi":            float(rsi_5m[i]),
                    "macd_hist":      float(hist_5m[i]),
                    "macd_hist_prev": float(hist_5m[i - 1]),
                    "atr":            float(atr_1h[j]),
                    "close":          float(c5[i]),
                    "vol_ratio":      vol_ratio,
                    "rolling_max":    float(roll_high_5m[i]),
                    "rolling_min":    float(roll_low_5m[i]),
                },
                bars_since_last=9999,
            )

        elif mode == "5m_1h_cross":
            if j < 1:
                continue
            needed_1h = [ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j],
                         rsi_1h[j], atr_1h[j], hist_1h[j], hist_1h[j - 1]]
            if any(np.isnan(v) for v in needed_1h):
                continue
            if np.isnan(roll_high_1h[j]) or np.isnan(roll_low_1h[j]):
                continue
            if i - last_trade_5m_bar < config.TRADE_COOLDOWN_5M:
                continue

            slope_bars = config.EMA_TREND_SLOPE_BARS
            j_slope = j - slope_bars
            ema_t_prev = (
                float(ema_trend_1h[j_slope])
                if j_slope >= 0 and not np.isnan(ema_trend_1h[j_slope]) else None
            )
            vol_ratio = (
                float(v5[i] / vol_sma_5m[i])
                if not np.isnan(vol_sma_5m[i]) and vol_sma_5m[i] > 0 else None
            )
            signal = evaluate_1h_signal(
                i1={
                    "ema_fast":       float(ema_fast_1h[j]),
                    "ema_slow":       float(ema_slow_1h[j]),
                    "ema_trend":      float(ema_trend_1h[j]),
                    "ema_trend_prev": ema_t_prev,
                    "rsi":            float(rsi_1h[j]),
                    "macd_hist":      float(hist_1h[j]),
                    "macd_hist_prev": float(hist_1h[j - 1]),
                    "atr":            float(atr_1h[j]),
                    "close":          float(c5[i]),   # 5M close as breakout trigger
                    "vol_ratio":      vol_ratio,
                    "rolling_max":    float(roll_high_1h[j]),
                    "rolling_min":    float(roll_low_1h[j]),
                },
                bars_since_last=9999,
            )

        elif mode in ("daily_open", "daily_trend"):
            if bar_day != last_daily_entry_day:
                if j >= 1 and not any(np.isnan([ema_trend_1h[j], ema_fast_1h[j],
                                                ema_slow_1h[j], atr_1h[j]])):
                    if mode == "daily_trend":
                        side = "LONG" if c5[i] > ema_trend_1h[j] else "SHORT"
                    else:
                        side = (
                            "LONG" if c5[i] > ema_trend_1h[j] and ema_fast_1h[j] > ema_slow_1h[j]
                            else "SHORT" if c5[i] < ema_trend_1h[j] and ema_fast_1h[j] < ema_slow_1h[j]
                            else None
                        )
                    if side is not None:
                        entry_do = float(c5[i])
                        atr_do = float(atr_1h[j])
                        sl_do = (entry_do - atr_do * config.ATR_SL_MULTIPLIER if side == "LONG"
                                 else entry_do + atr_do * config.ATR_SL_MULTIPLIER)
                        tp_do = (entry_do + atr_do * config.ATR_TP_MULTIPLIER if side == "LONG"
                                 else entry_do - atr_do * config.ATR_TP_MULTIPLIER)
                        rr = config.ATR_TP_MULTIPLIER / config.ATR_SL_MULTIPLIER
                        signal = Signal(
                            side=side, entry=entry_do, sl=sl_do, tp=tp_do,
                            sl_pct=abs(entry_do - sl_do) / entry_do * 100,
                            tp_pct=abs(tp_do - entry_do) / entry_do * 100,
                            rr_ratio=rr, reason=mode,
                            regime="UNKNOWN", size_scale=1.0,
                            indicators_5m={}, indicators_1h={},
                        )
                        last_daily_entry_day = bar_day

        else:
            # Default: "1h" mode — evaluate only on each new 1H bar close
            if j <= prev_1h_bar:
                continue
            prev_1h_bar = j

            if j < 1:
                continue
            needed_1h = [ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j], rsi_1h[j], atr_1h[j]]
            if any(np.isnan(v) for v in needed_1h):
                continue
            if np.isnan(hist_1h[j]) or np.isnan(hist_1h[j - 1]):
                continue

            slope_bars = config.EMA_TREND_SLOPE_BARS
            j_slope    = j - slope_bars
            ema_t_prev = (
                float(ema_trend_1h[j_slope])
                if j_slope >= 0 and not np.isnan(ema_trend_1h[j_slope]) else None
            )
            vol_ratio  = (
                float(v1[j] / vol_sma_1h[j])
                if not np.isnan(vol_sma_1h[j]) and vol_sma_1h[j] > 0 else None
            )
            atr_ratio  = (
                float(atr_1h[j] / atr_sma_1h[j])
                if not np.isnan(atr_sma_1h[j]) and atr_sma_1h[j] > 0 else None
            )
            adx_val    = float(adx_1h[j]) if not np.isnan(adx_1h[j]) else None

            # ══════════════════════════════════════════════════════════════════
            # STEP 1b — REGIME FORECAST UPDATE  (REGIME_FORECAST_ENABLED)
            # ──────────────────────────────────────────────────────────────────
            # Classify the current bar, update the Markov transition matrix,
            # and derive entry gates + size scale from the one-step-ahead
            # probability forecast.
            #
            # Gate logic:
            #   choppy_prob ≥ FORECAST_CHOPPY_THRESHOLD → suppress entry,
            #       extend cooldown to WFO_CHOPPY_COOLDOWN bars.
            #   trend_prob  ≥ FORECAST_MIN_TREND_PROB   → allow entry,
            #       scale size by clamp(trend_prob + 0.5, 0.5, 1.0).
            #   confidence  < FORECAST_MIN_CONFIDENCE   → forecast unreliable,
            #       keep full size regardless of probabilities.
            # ══════════════════════════════════════════════════════════════════
            if config.REGIME_FORECAST_ENABLED and forecaster is not None:
                _adx_fc   = float(adx_val) if adx_val is not None else 20.0
                _atr_pct_fc = (float(atr_1h[j]) / float(c1[j]) * 100
                               if c1[j] > 0 else 1.0)
                _hurst_fc = hurst_exponent(c1[: j + 1])
                _new_state = forecaster.classify(_adx_fc, _atr_pct_fc, _hurst_fc)
                forecaster.update(_new_state)
                _fc: RegimeForecast = forecaster.forecast()

                strat_state.current_regime = _new_state
                strat_state.trend_prob     = _fc.trend_prob
                strat_state.choppy_prob    = _fc.choppy_prob

                if _fc.choppy_prob >= config.FORECAST_CHOPPY_THRESHOLD:
                    # High probability of a choppy bar — block entry
                    strat_state.entry_allowed      = False
                    strat_state.effective_cooldown = config.WFO_CHOPPY_COOLDOWN
                    strat_state.size_scale         = 0.5
                else:
                    strat_state.entry_allowed = True
                    strat_state.effective_cooldown = config.TRADE_COOLDOWN_1H
                    if (_fc.confidence >= config.FORECAST_MIN_CONFIDENCE
                            and _fc.trend_prob >= config.FORECAST_MIN_TREND_PROB):
                        # Confident TREND forecast → full/scaled-up size
                        strat_state.size_scale = min(1.0, 0.5 + _fc.trend_prob)
                    elif _fc.confidence >= config.FORECAST_MIN_CONFIDENCE:
                        # Forecast exists but low trend probability → reduce size
                        strat_state.size_scale = max(0.5, _fc.trend_prob)
                    else:
                        # Below confidence floor → forecast is noise, full size
                        strat_state.size_scale = 1.0

            # Forecast entry gate: skip this bar if choppy regime is imminent
            if config.REGIME_FORECAST_ENABLED and not strat_state.entry_allowed:
                continue

            # Extended cooldown gate (must precede i1_dict build — bars_since_last
            # is also passed to evaluate_1h_signal for its internal check)
            if config.REGIME_FORECAST_ENABLED or config.WFO_ENABLED:
                _eff_cd = max(strat_state.effective_cooldown, config.TRADE_COOLDOWN_1H)
                if j - last_trade_1h_bar < _eff_cd:
                    continue

            # ══════════════════════════════════════════════════════════════════
            # STEP 1c — WALK-FORWARD RETUNE  (WFO_ENABLED)
            # ──────────────────────────────────────────────────────────────────
            # Every WFO_RETUNE_INTERVAL bars, sweep BREAKOUT_GRID over the past
            # WFO_TRAINING_WINDOW bars and pick the breakout period with the
            # highest Profit Factor (subject to WFO_MIN_TRADES minimum).
            # The selected period is used for rolling_max / rolling_min lookups
            # for the next interval — no lookahead, applied to future bars only.
            # ══════════════════════════════════════════════════════════════════
            if config.WFO_ENABLED and wfo is not None and wfo.should_retune(j):
                # Pass current_atr so the optimizer can apply dynamic lookback
                # when a volatility spike is detected (WFO_FAST_ENABLED).
                _cur_atr = (
                    float(atr_1h[j]) if not np.isnan(atr_1h[j]) else None
                )
                _wfo_params: ActiveParams = wfo.optimize(
                    c1=c1, h1=h1, l1=l1,
                    adx_arr=adx_1h, atr_arr=atr_1h,
                    end_bar=j,
                    current_atr=_cur_atr,
                )
                strat_state.active_bp = _wfo_params.breakout_period
                logger.info(
                    "WFO retune @ 1H bar %d: BP=%d  PF=%.2f  n_trades=%d",
                    j, _wfo_params.breakout_period,
                    _wfo_params.profit_factor, _wfo_params.n_trades,
                )
                # wfo.optimize() already calls gc.collect() internally; call
                # here too to reclaim backtest-level temporaries every retune.
                del _wfo_params, _cur_atr
                gc.collect()

            # ── Initial cooldown gate ─────────────────────────────────────────
            # Suppress all entries for the first INITIAL_COOLDOWN_BARS 1H bars
            # after the first valid execution bar.  WFO keeps running above so
            # its training history accumulates; only signal evaluation is skipped.
            # Useful for short backtests where EMA200 is still SMA-seeded.
            if config.INITIAL_COOLDOWN_BARS > 0:
                if _first_valid_j < 0:
                    _first_valid_j = j          # anchor: first bar that passed NaN guards
                if j < _first_valid_j + config.INITIAL_COOLDOWN_BARS:
                    continue

            # ── Select rolling extremes for the active breakout period ────────
            _active_bp = strat_state.active_bp if config.WFO_ENABLED else config.BREAKOUT_PERIOD
            _rh = roll_high_grid.get(_active_bp, roll_high_1h)
            _rl = roll_low_grid.get(_active_bp, roll_low_1h)

            # ══════════════════════════════════════════════════════════════════
            # STEP 2 — REGIME CLASSIFICATION (REGIME_FILTER / ADAPTIVE_REGIME)
            # ──────────────────────────────────────────────────────────────────
            # The regime state is computed here and injected into i1_dict.
            # evaluate_1h_signal() reads it to suppress / tighten entries.
            #
            # ADAPTIVE_REGIME_ENABLED = True  (recommended):
            #   compute_adaptive_regime() builds a RegimeState with:
            #     • score ∈ [0,1] from Hurst × ADX-momentum × BBW-percentile
            #     • score < ADAPTIVE_MIN_SCORE  → entry fully suppressed
            #     • entry_buffer_atr            → breakout filter tightens in chop
            #     • effective_adx_min           → ADX floor relaxed in strong trend
            #   All four outputs are smooth continuous functions — no cliff edges.
            #
            # ADAPTIVE_REGIME_ENABLED = False (classic):
            #   REGIME_FILTER_ENABLED in strategy.py checks static ADX thresholds.
            #   "RANGING" regime (detect_regime) suppresses entry entirely.
            # ══════════════════════════════════════════════════════════════════
            i1_dict = {
                "ema_fast":       float(ema_fast_1h[j]),
                "ema_slow":       float(ema_slow_1h[j]),
                "ema_trend":      float(ema_trend_1h[j]),
                "ema_trend_prev": ema_t_prev,
                "rsi":            float(rsi_1h[j]),
                "macd_hist":      float(hist_1h[j]),
                "macd_hist_prev": float(hist_1h[j - 1]),
                "atr":            float(atr_1h[j]),
                "atr_ratio":      atr_ratio,
                "adx":            adx_val,
                "close":          float(c1[j]),
                "open":           float(o1[j]),
                "vol_ratio":      vol_ratio,
                # Use WFO-selected rolling extremes (or static if WFO is off)
                "rolling_max":    float(_rh[j]) if not np.isnan(_rh[j]) else None,
                "rolling_min":    float(_rl[j]) if not np.isnan(_rl[j]) else None,
            }

            if config.ADAPTIVE_REGIME_ENABLED:
                _atr_pct = float(atr_1h[j]) / float(c1[j]) * 100 if c1[j] > 0 else 1.0
                i1_dict["regime_state"] = compute_adaptive_regime(
                    closes        = c1[:j + 1],
                    highs         = h1[:j + 1],
                    lows          = l1[:j + 1],
                    adx_arr       = adx_1h[:j + 1],
                    atr_pct       = _atr_pct,
                    tp_base       = config.ADAPTIVE_TP_BASE,
                    tp_max_ext    = config.ADAPTIVE_TP_MAX_EXT,
                    sl_base       = config.ATR_SL_MULTIPLIER,
                    sl_max_widen  = config.ADAPTIVE_SL_MAX_WIDEN,
                    size_min      = config.ADAPTIVE_SIZE_MIN,
                    buffer_max    = config.ADAPTIVE_BUFFER_MAX,
                    adx_min_base  = config.ADX_MIN,
                    adx_min_relax = config.ADAPTIVE_ADX_RELAX,
                )

            # ══════════════════════════════════════════════════════════════════
            # STEP 3 — SIGNAL EVALUATION (DYNAMIC_TP_ENABLED)
            # ──────────────────────────────────────────────────────────────────
            # evaluate_1h_signal() applies (in order):
            #
            # 1. Cooldown guard   — TRADE_COOLDOWN_1H bars between entries.
            # 2. Regime gate      — regime_state.score < ADAPTIVE_MIN_SCORE → None
            # 3. Flat/ATR guards  — EMA_1H_MIN_SEP, ATR_1H_PCT_MIN/MAX.
            # 4. Volume filter    — VOL_RATIO_MIN (institutional participation).
            # 5. ADX/slope filter — regime_state.effective_adx_min (adaptive) or
            #                       ADX_MIN (classic).  EMA200 slope direction.
            # 6. Breakout filter  — close > rolling_high + regime_state.entry_buffer_atr
            #                       (adaptive) or BREAKOUT_ATR_BUFFER (classic).
            # 7. TP / SL sizing   — regime_state.tp_mult / sl_mult (adaptive) or
            #                       DYNAMIC_TP_ENABLED × dynamic_tp_mult (classic).
            # ══════════════════════════════════════════════════════════════════
            signal = evaluate_1h_signal(
                i1=i1_dict,
                bars_since_last=j - last_trade_1h_bar,
            )

        if signal is None:
            continue

        # ══════════════════════════════════════════════════════════════════════
        # STEP 4 — POSITION SIZING
        # ──────────────────────────────────────────────────────────────────────
        # _position_qty() applies the active sizing mode then two scale layers:
        #
        # Sizing mode (highest-priority first):
        #   Mode 0 — EQUITY_PERCENT (default, 10%):
        #     margin       = balance × 10%           (reads live equity curve)
        #     position_val = margin × LEVERAGE        (= balance × 10% × 10 = 1× balance)
        #     qty          = position_val / entry_price
        #     → Compounds naturally: growing balance → larger notional each trade.
        #
        #   Mode 3 — RISK_PERCENT fallback (if EQUITY_PERCENT = 0):
        #     risk_dollar  = balance × RISK_PERCENT / 100
        #     qty          = risk_dollar / (entry × sl_distance)
        #
        # Layer A — Volatility-adjusted risk (VOL_SIZING_ENABLED, mode 3 only):
        #   Scales RISK_PERCENT inversely with ATR ratio (current ATR / 20-bar SMA).
        #
        # Layer B — Regime size scale (ADAPTIVE_REGIME_ENABLED):
        #   signal.size_scale from the RegimeState (0.30 → 1.00).
        #
        # Layer C — Forecast confidence scale (REGIME_FORECAST_ENABLED):
        #   strat_state.size_scale ∈ [0.5, 1.0] from the Markov forecaster.
        # ══════════════════════════════════════════════════════════════════════
        entry_price   = _apply_slippage(signal.entry, signal.side, is_entry=True)
        _signal_scale = signal.size_scale
        if config.REGIME_FORECAST_ENABLED:
            _signal_scale = float(np.clip(_signal_scale * strat_state.size_scale, 0.1, 2.0))
        qty = _position_qty(
            balance, entry_price, signal.sl,
            atr_ratio=signal.indicators_1h.get("atr_ratio"),
            size_scale=_signal_scale,
        )
        if qty < config.MIN_ORDER_QTY:
            continue
        if qty * entry_price < config.MIN_NOTIONAL:   # exchange min-notional guard
            continue

        balance -= _commission_cost(entry_price, qty)
        position = _OpenPosition(
            side=signal.side,
            entry_price=entry_price,
            sl=signal.sl,
            tp=signal.tp,
            qty=qty,
            entry_time=pd.Timestamp(ct_5m[i], unit="ms"),
            bar_idx=i,
            entry_atr=float(atr_1h[j]),
        )
        last_trade_1h_bar = j

    # ── Force-close any open position at end of data ──────────────────────────
    if position is not None:
        exit_price = _apply_slippage(float(c5[-1]), position.side, is_entry=False)
        direction = 1 if position.side == "LONG" else -1
        net_pnl = (exit_price - position.entry_price) * direction * position.qty
        net_pnl -= _commission_cost(exit_price, position.qty)
        balance += net_pnl

        trades.append(Trade(
            side=position.side,
            entry_time=position.entry_time,
            exit_time=pd.Timestamp(ct_5m[-1], unit="ms"),
            entry_price=position.entry_price,
            exit_price=exit_price,
            qty=position.qty,
            pnl=net_pnl,
            pnl_pct=net_pnl / (position.entry_price * position.qty) * 100,
            close_reason="EOD",
            balance_after=balance,
            bars_held=n - 1 - position.bar_idx,
        ))
        equity_times.append(pd.Timestamp(ct_5m[-1], unit="ms"))
        equity_vals.append(balance)

    # Flush the last day's record
    if current_day_epoch >= 0:
        day_pnl_pct = (balance - day_start_balance) / day_start_balance * 100
        daily_records.append((
            pd.Timestamp(current_day_epoch * 86_400_000, unit="ms").date(),
            day_pnl_pct,
        ))
        if daily_target_hit:
            days_target_hit += 1
        if daily_profit_hit:
            days_profit_hit += 1
        elif daily_target_hit:
            days_loss_hit += 1

    equity_curve = pd.Series(equity_vals, index=pd.DatetimeIndex(equity_times), name="balance")

    if daily_records:
        dates, pnl_pcts = zip(*daily_records)
        daily_pnl = pd.Series(
            list(pnl_pcts),
            index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]),
            name="daily_pnl_pct",
        )
    else:
        daily_pnl = pd.Series(name="daily_pnl_pct", dtype=float)

    stats = _compute_stats(
        trades, initial_balance, balance, equity_curve,
        days_target_hit, len(daily_records), days_profit_hit, days_loss_hit,
    )

    # Collect WFO retune log for the caller (run_backtest.py report)
    _wfo_log: List[Dict] = []
    if config.WFO_ENABLED and wfo is not None:
        for _entry in wfo.log:
            _wfo_log.append({
                "bar": _entry.bar,
                "bp":  _entry.bp,
                "pf":  _entry.pf,
                "n":   _entry.n,
                "scores": {k: v for k, v in _entry.scores.items()},
            })

    return BacktestResult(
        trades=trades, equity_curve=equity_curve, stats=stats,
        df_5m=df_5m, df_1h=df_1h, daily_pnl=daily_pnl,
        wfo_log=_wfo_log,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _compute_stats(
    trades: List[Trade],
    initial_balance: float,
    final_balance: float,
    equity: pd.Series,
    days_target_hit: int = 0,
    total_days: int = 0,
    days_profit_hit: int = 0,
    days_loss_hit: int = 0,
) -> Dict:
    """Compute summary statistics from a completed backtest.

    Args:
        trades:          List of all closed trades.
        initial_balance: Starting balance.
        final_balance:   Ending balance.
        equity:          Equity curve Series indexed by timestamp.
        days_target_hit: Days on which the daily limit (profit or loss) was hit.
        total_days:      Total calendar days in the backtest.
        days_profit_hit: Days on which the daily *profit* target was reached.
        days_loss_hit:   Days on which the daily *loss* limit was hit.

    Returns:
        Dictionary of scalar statistics (CAGR, Sharpe, max drawdown, etc.).
    """
    base: Dict = {
        "total_trades":     0,
        "initial_balance":  initial_balance,
        "final_balance":    final_balance,
        "total_return_pct": (final_balance - initial_balance) / initial_balance * 100,
        "days_target_hit":  days_target_hit,
        "total_days":       total_days,
        "days_profit_hit":  days_profit_hit,
        "days_loss_hit":    days_loss_hit,
    }
    if not trades:
        return base

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gross_profit = float(wins.sum()) if len(wins) else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
    cagr = ((final_balance / initial_balance) ** (1.0 / n_years) - 1.0) * 100.0

    roll_max = equity.cummax()
    max_drawdown = float(((equity - roll_max) / roll_max * 100).min())

    daily_returns = equity.resample("D").last().ffill().pct_change().dropna()
    sharpe = (
        float(daily_returns.mean() / daily_returns.std() * np.sqrt(252))
        if daily_returns.std() > 0 else 0.0
    )

    return {
        "total_trades":     len(trades),
        "wins":             int(len(wins)),
        "losses":           int(len(losses)),
        "win_rate":         float(len(wins) / len(pnls) * 100),
        "tp_exits":         sum(1 for t in trades if t.close_reason == "TP"),
        "sl_exits":         sum(1 for t in trades if t.close_reason == "SL"),
        "be_exits":         sum(1 for t in trades if t.close_reason == "BE"),
        "eod_exits":        sum(1 for t in trades if t.close_reason == "EOD"),
        "initial_balance":  initial_balance,
        "final_balance":    final_balance,
        "total_return_pct": (final_balance - initial_balance) / initial_balance * 100,
        "cagr_pct":         cagr,
        "max_drawdown_pct": max_drawdown,
        "sharpe":           sharpe,
        "profit_factor":    profit_factor,
        "gross_profit":     gross_profit,
        "gross_loss":       gross_loss,
        "avg_win":          float(wins.mean())   if len(wins)   else 0.0,
        "avg_loss":         float(losses.mean()) if len(losses) else 0.0,
        "avg_bars_held":    float(np.mean([t.bars_held for t in trades])),
        "best_trade":       float(pnls.max()),
        "worst_trade":      float(pnls.min()),
        "days_target_hit":  days_target_hit,
        "total_days":       total_days,
        "days_profit_hit":  days_profit_hit,
        "days_loss_hit":    days_loss_hit,
    }
