"""
Vectorised backtesting engine.

Assumptions:
    - Entry at the CLOSE price of the signal candle.
    - SL/TP are checked against the *next* candle's high/low (no same-bar look-ahead).
    - Commission: 0.05 % per fill.  Slippage: 0.02 % per fill.
    - Daily profit/loss limits are evaluated after each closed trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
import indicators as ind
from strategy import Signal, evaluate_1h_signal, evaluate_from_indicators

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
    daily_pnl: pd.Series  # daily PnL as % of day-start balance


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
    """Compute the contract quantity for a new position.

    Sizing priority (highest → lowest):

    1. ``ORDER_BALANCE_USD > 0``  →  fixed margin × leverage (ignores SL distance).
    2. ``RISK_USD > 0``           →  fixed-dollar risk, capped by margin limit.
    3. ``RISK_PERCENT``           →  % of balance risk, capped; optionally vol-adjusted.

    Args:
        balance:    Current account balance in quote currency.
        entry:      Anticipated entry price.
        sl:         Stop-loss price.
        atr_ratio:  Current ATR / 20-bar ATR SMA; used for volatility-adjusted sizing.
        size_scale: Regime multiplier from the signal (e.g. 0.5 in HIGH_VOL regime).

    Returns:
        Contract quantity (BTC for BTCUSDT), or 0.0 if sizing fails.
    """
    # 1. Fixed order-balance sizing: notional = margin × leverage
    if config.ORDER_BALANCE_USD > 0:
        notional = config.ORDER_BALANCE_USD * max(config.LEVERAGE, 1)
        return (notional / entry) * size_scale

    # 2 / 3. Risk-based sizing
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


def _update_trailing_stop(position: _OpenPosition, bar_high: float, bar_low: float) -> None:
    """Apply all three trailing-stop stages to an open position in-place.

    Stages (in order):
        1. **Break-even** (``TRAIL_ACTIVATE_ATR``): move SL to entry when price
           moves N×ATR in favour.
        2. **Profit lock** (``TRAIL_LOCK_ATR``): advance SL to entry + 1×ATR
           when price moves M×ATR in favour.
        3. **Dynamic trail** (``TRAIL_STOP_ATR``): SL follows the running peak
           at −N×ATR after break-even is activated.

    Args:
        position: The open position to modify.
        bar_high: High of the current bar (used to update trail peak for LONG).
        bar_low:  Low of the current bar (used for SHORT).
    """
    atr = position.entry_atr
    if atr <= 0:
        return

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
            position.trail_peak = price

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

        # ── SL / TP check on the next bar's high/low ─────────────────────────
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
            j_slope = j - slope_bars
            ema_t_prev = (
                float(ema_trend_1h[j_slope])
                if j_slope >= 0 and not np.isnan(ema_trend_1h[j_slope]) else None
            )
            vol_ratio = (
                float(v1[j] / vol_sma_1h[j])
                if not np.isnan(vol_sma_1h[j]) and vol_sma_1h[j] > 0 else None
            )
            atr_ratio = (
                float(atr_1h[j] / atr_sma_1h[j])
                if not np.isnan(atr_sma_1h[j]) and atr_sma_1h[j] > 0 else None
            )
            adx_val = float(adx_1h[j]) if not np.isnan(adx_1h[j]) else None

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
                    "atr_ratio":      atr_ratio,
                    "adx":            adx_val,
                    "close":          float(c1[j]),
                    "open":           float(o1[j]),
                    "vol_ratio":      vol_ratio,
                    "rolling_max":    float(roll_high_1h[j]) if not np.isnan(roll_high_1h[j]) else None,
                    "rolling_min":    float(roll_low_1h[j])  if not np.isnan(roll_low_1h[j])  else None,
                },
                bars_since_last=j - last_trade_1h_bar,
            )

        if signal is None:
            continue

        # ── Open new position ─────────────────────────────────────────────────
        entry_price = _apply_slippage(signal.entry, signal.side, is_entry=True)
        qty = _position_qty(
            balance, entry_price, signal.sl,
            atr_ratio=signal.indicators_1h.get("atr_ratio"),
            size_scale=signal.size_scale,
        )
        if qty < config.MIN_ORDER_QTY:
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
    return BacktestResult(
        trades=trades, equity_curve=equity_curve, stats=stats,
        df_5m=df_5m, df_1h=df_1h, daily_pnl=daily_pnl,
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
