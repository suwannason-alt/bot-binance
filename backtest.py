"""
Vectorized backtesting engine (v3).

Key change: 1H EMA200 computed and passed to strategy for macro trend filter.

Assumptions:
  - Entry at CLOSE of signal candle
  - SL/TP checked against NEXT candle high/low (no same-bar lookahead)
  - Commission 0.05% per fill  |  Slippage 0.02% per fill
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

import config
import indicators as ind
from strategy import evaluate_1h_signal, evaluate_from_indicators

logger = logging.getLogger("backtest")

COMMISSION = 0.0005
SLIPPAGE   = 0.0002


@dataclass
class Trade:
    side: str
    entry_time: object
    exit_time: Optional[object]
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    pnl_pct: float
    close_reason: str
    balance_after: float
    bars_held: int


@dataclass
class BacktestResult:
    trades: list[Trade]
    equity_curve: pd.Series
    stats: dict
    df_5m: pd.DataFrame
    df_1h: pd.DataFrame
    daily_pnl: pd.Series  # daily PnL as % of day_start_balance


def _apply_slippage(price: float, side: str, is_entry: bool) -> float:
    direction = 1 if (side == "LONG") == is_entry else -1
    return price * (1 + direction * SLIPPAGE)


def _commission_cost(price: float, qty: float) -> float:
    return price * qty * COMMISSION


def _position_qty(balance: float, entry: float, sl: float) -> float:
    # Use fixed-dollar risk if configured, otherwise % of balance
    risk    = config.RISK_USD if config.RISK_USD > 0 else balance * (config.RISK_PERCENT / 100)
    sl_dist = abs(entry - sl) / entry
    if sl_dist == 0:
        return 0.0
    qty = risk / (entry * sl_dist)
    # Cap at LEVERAGE × balance / entry (margin constraint)
    if config.LEVERAGE > 0:
        qty = min(qty, balance * config.LEVERAGE / entry)
    return qty


def run(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    initial_balance: float = 1000.0,
    mode: str = "1h",
) -> BacktestResult:

    # ── 5M indicator arrays ───────────────────────────────────────────────────
    h5 = df_5m["high"].values
    l5 = df_5m["low"].values
    c5 = df_5m["close"].values
    v5 = df_5m["volume"].values.astype(float)
    ct_5m = df_5m["close_time"].values.astype(np.int64)

    ema_fast_5m = ind.ema(c5, config.EMA_FAST)
    ema_slow_5m = ind.ema(c5, config.EMA_SLOW)
    rsi_5m      = ind.rsi(c5, config.RSI_PERIOD)
    _, _, hist_5m = ind.macd(c5, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    atr_5m      = ind.atr(h5, l5, c5, config.ATR_PERIOD)
    vol_sma_5m  = ind.sma(v5, 20)

    # Rolling high/low for 5M breakout mode
    bp_5m = config.BREAKOUT_PERIOD_5M
    roll_high_5m = np.full_like(c5, np.nan)
    roll_low_5m  = np.full_like(c5, np.nan)
    for _k in range(bp_5m, len(c5)):
        roll_high_5m[_k] = h5[_k - bp_5m : _k].max()
        roll_low_5m[_k]  = l5[_k - bp_5m : _k].min()

    # ── 1H indicator arrays ───────────────────────────────────────────────────
    c1    = df_1h["close"].values
    l1    = df_1h["low"].values
    h1_   = df_1h["high"].values
    v1    = df_1h["volume"].values.astype(float)
    ct_1h = df_1h["close_time"].values.astype(np.int64)

    ema_fast_1h  = ind.ema(c1, config.EMA_FAST)   # EMA20
    ema_slow_1h  = ind.ema(c1, config.EMA_SLOW)   # EMA50
    ema_trend_1h = ind.ema(c1, config.EMA_TREND)  # EMA200 — macro filter
    rsi_1h       = ind.rsi(c1, config.RSI_PERIOD)
    _, _, hist_1h = ind.macd(c1, config.MACD_FAST, config.MACD_SLOW, config.MACD_SIGNAL)
    atr_1h       = ind.atr(h1_, l1, c1, config.ATR_PERIOD)
    vol_sma_1h   = ind.sma(v1, 20)
    atr_sma_1h   = ind.sma(atr_1h, 20)  # for ATR ratio regime filter
    adx_1h       = ind.adx(h1_, l1, c1, config.ADX_PERIOD)

    bp = config.BREAKOUT_PERIOD
    roll_high_1h = np.full_like(c1, np.nan)
    roll_low_1h  = np.full_like(c1, np.nan)
    for _k in range(bp, len(c1)):
        roll_high_1h[_k] = h1_[_k - bp : _k].max()
        roll_low_1h[_k]  = l1[_k - bp : _k].min()

    # Map each 5M bar → last closed 1H bar
    idx_1h = np.searchsorted(ct_1h, ct_5m, side="right") - 1

    n        = len(df_5m)
    balance  = initial_balance
    trades: list[Trade] = []
    equity_times = [pd.Timestamp(df_5m["open_time"].iloc[0], unit="ms")]
    equity_vals  = [balance]

    position: Optional[dict] = None
    last_trade_1h_bar: int = -9999
    last_trade_5m_bar: int = -9999
    prev_j: int = -1
    _do_state = type("_DO", (), {"last_entry_day": -1})()  # state for daily_open mode

    # ── Daily profit target tracking ──────────────────────────────────────────
    current_day_epoch: int = -1      # days since unix epoch
    day_start_balance: float = initial_balance
    daily_target_hit: bool = False
    daily_profit_hit: bool = False   # true only on days that hit the PROFIT target (not loss limit)
    days_target_hit: int = 0
    days_profit_hit: int = 0         # days where daily profit target was reached
    days_loss_hit: int = 0           # days where daily loss limit was reached
    _daily_records: list[tuple] = []  # (date, pnl_pct)

    warmup = max(config.MIN_CANDLES_5M, config.MIN_CANDLES_1H)

    for i in range(warmup, n - 1):
        j = int(idx_1h[i])
        if j < config.MIN_CANDLES_1H:
            continue

        # ── Daily profit target: reset on new calendar day ────────────────────
        bar_day = int(ct_5m[i]) // 86_400_000  # ms → days since epoch
        if bar_day != current_day_epoch:
            if current_day_epoch >= 0:
                day_pnl_pct = (balance - day_start_balance) / day_start_balance * 100
                _daily_records.append((
                    pd.Timestamp(current_day_epoch * 86_400_000, unit="ms").date(),
                    day_pnl_pct,
                ))
                if daily_target_hit:
                    days_target_hit += 1
                if daily_profit_hit:
                    days_profit_hit += 1
                else:
                    # count loss-limit days (stopped trading due to drawdown)
                    if daily_target_hit:
                        days_loss_hit += 1
            current_day_epoch = bar_day
            day_start_balance = balance
            daily_target_hit = False
            daily_profit_hit = False

        # ── SL/TP check on the next bar ───────────────────────────────────────
        if position is not None:
            nxt_high = h5[i + 1]
            nxt_low  = l5[i + 1]
            nxt_time = pd.Timestamp(ct_5m[i + 1], unit="ms")

            # ── Trailing stop: update SL based on THIS bar's high/low ────────
            e_p   = position["entry_price"]
            e_atr = position["entry_atr"]
            if config.TRAIL_ACTIVATE_ATR > 0 and not position["be_activated"]:
                trigger = e_atr * config.TRAIL_ACTIVATE_ATR
                if position["side"] == "LONG" and h5[i] >= e_p + trigger:
                    new_sl = e_p   # move SL to break-even
                    if new_sl > position["sl"]:
                        position["sl"] = new_sl
                    position["be_activated"] = True
                elif position["side"] == "SHORT" and l5[i] <= e_p - trigger:
                    new_sl = e_p
                    if new_sl < position["sl"]:
                        position["sl"] = new_sl
                    position["be_activated"] = True

            if config.TRAIL_LOCK_ATR > 0 and not position["lock_activated"]:
                trigger2 = e_atr * config.TRAIL_LOCK_ATR
                if position["side"] == "LONG" and h5[i] >= e_p + trigger2:
                    new_sl = e_p + e_atr   # lock in 1×ATR profit
                    if new_sl > position["sl"]:
                        position["sl"] = new_sl
                    position["lock_activated"] = True
                elif position["side"] == "SHORT" and l5[i] <= e_p - trigger2:
                    new_sl = e_p - e_atr
                    if new_sl < position["sl"]:
                        position["sl"] = new_sl
                    position["lock_activated"] = True

            hit_sl = hit_tp = False
            if position["side"] == "LONG":
                hit_sl = nxt_low  <= position["sl"]
                hit_tp = nxt_high >= position["tp"]
            else:
                hit_sl = nxt_high >= position["sl"]
                hit_tp = nxt_low  <= position["tp"]

            if hit_sl or hit_tp:
                if hit_sl:
                    reason = "BE" if position["be_activated"] else "SL"
                else:
                    reason = "TP"
                raw_exit   = position["sl"] if hit_sl else position["tp"]
                exit_price = _apply_slippage(raw_exit, position["side"], is_entry=False)
                entry_p    = position["entry_price"]
                qty        = position["qty"]
                direction  = 1 if position["side"] == "LONG" else -1

                gross_pnl = (exit_price - entry_p) * direction * qty
                cost      = _commission_cost(exit_price, qty)  # entry commission already paid at open
                net_pnl   = gross_pnl - cost
                balance  += net_pnl

                trades.append(Trade(
                    side=position["side"],
                    entry_time=position["entry_time"],
                    exit_time=nxt_time,
                    entry_price=entry_p,
                    exit_price=exit_price,
                    qty=qty,
                    pnl=net_pnl,
                    pnl_pct=net_pnl / (entry_p * qty) * 100 if qty > 0 else 0,
                    close_reason=reason,
                    balance_after=balance,
                    bars_held=i + 1 - position["bar_idx"],
                ))
                equity_times.append(nxt_time)
                equity_vals.append(balance)
                last_trade_1h_bar = j
                last_trade_5m_bar = i
                position = None

                # Check daily profit target / loss limit (USD thresholds override PCT)
                day_pnl = balance - day_start_balance
                profit_thresh = (config.DAILY_PROFIT_TARGET_USD if config.DAILY_PROFIT_TARGET_USD > 0
                                 else day_start_balance * config.DAILY_PROFIT_TARGET_PCT)
                loss_thresh   = (config.DAILY_LOSS_LIMIT_USD if config.DAILY_LOSS_LIMIT_USD > 0
                                 else day_start_balance * config.DAILY_LOSS_LIMIT_PCT)
                if profit_thresh > 0 and day_pnl >= profit_thresh:
                    daily_target_hit = True
                    daily_profit_hit = True
                if loss_thresh > 0 and day_pnl <= -loss_thresh:
                    daily_target_hit = True  # stops trading; profit flag stays False

                if balance <= 10:
                    logger.warning("Balance critically low — stopping backtest")
                    break
                continue

        if position is not None:
            if j > prev_j:
                prev_j = j
            continue

        # ── Skip entry if daily profit target already reached ─────────────────
        if daily_target_hit:
            continue

        signal = None

        if mode == "5m":
            # ── 5M scalp: evaluate every bar, use 5M indicators for entry ────
            if i < 1:
                continue
            needed_5m = [ema_fast_5m[i], rsi_5m[i], atr_5m[i], hist_5m[i], hist_5m[i - 1]]
            if any(np.isnan(v) for v in needed_5m):
                continue
            if j < 1 or any(np.isnan([ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j], rsi_1h[j]])):
                continue

            i5 = {
                "ema_fast":       float(ema_fast_5m[i]),
                "rsi":            float(rsi_5m[i]),
                "atr":            float(atr_5m[i]),
                "macd_hist":      float(hist_5m[i]),
                "macd_hist_prev": float(hist_5m[i - 1]),
                "close":          float(c5[i]),
            }
            i1_5m = {
                "ema_fast":  float(ema_fast_1h[j]),
                "ema_slow":  float(ema_slow_1h[j]),
                "ema_trend": float(ema_trend_1h[j]),
                "rsi":       float(rsi_1h[j]),
                "close":     float(c1[j]),
            }
            if i - last_trade_5m_bar < config.TRADE_COOLDOWN_5M:
                continue
            signal = evaluate_from_indicators(i5, i1_5m, float(c5[i]),
                                              bars_since_last_trade=9999)

        elif mode == "5m_breakout":
            # ── 5M breakout: same logic as 1H swing but on 5M data ───────────
            # Uses 1H EMA200 as macro trend + 5M EMA20/50/RSI/ATR/breakout for entry
            if i < 1:
                continue
            needed_5m = [ema_fast_5m[i], ema_slow_5m[i], rsi_5m[i],
                         atr_5m[i], hist_5m[i], hist_5m[i - 1]]
            if any(np.isnan(v) for v in needed_5m):
                continue
            if j < 1 or np.isnan(ema_trend_1h[j]):
                continue
            if np.isnan(roll_high_5m[i]) or np.isnan(roll_low_5m[i]):
                continue

            if i - last_trade_5m_bar < config.TRADE_COOLDOWN_5M:
                continue

            slope_bars    = config.EMA_TREND_SLOPE_BARS
            j_prev_slope  = j - slope_bars
            ema_t_prev_5m = float(ema_trend_1h[j_prev_slope]) \
                if j_prev_slope >= 0 and not np.isnan(ema_trend_1h[j_prev_slope]) else None
            vol_ratio_5m  = float(v5[i] / vol_sma_5m[i]) \
                if not np.isnan(vol_sma_5m[i]) and vol_sma_5m[i] > 0 else None

            if np.isnan(atr_1h[j]):
                continue
            i5_bo = {
                "ema_fast":       float(ema_fast_5m[i]),
                "ema_slow":       float(ema_slow_5m[i]),
                "ema_trend":      float(ema_trend_1h[j]),
                "ema_trend_prev": ema_t_prev_5m,
                "rsi":            float(rsi_5m[i]),
                "macd_hist":      float(hist_5m[i]),
                "macd_hist_prev": float(hist_5m[i - 1]),
                "atr":            float(atr_1h[j]),
                "close":          float(c5[i]),
                "vol_ratio":      vol_ratio_5m,
                "rolling_max":    float(roll_high_5m[i]),
                "rolling_min":    float(roll_low_5m[i]),
            }
            signal = evaluate_1h_signal(i5_bo, 9999)

        elif mode == "5m_1h_cross":
            # ── 5M timing + 1H signal quality ────────────────────────────────
            # Enters when 5M close crosses a 1H breakout level.
            # Uses all 1H indicators (same quality as proven 1H strategy) but fires
            # the moment price crosses the level on a 5M bar — not at 1H bar close.
            # Earlier entry + same 1H quality = better RR and more opportunities/day.
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

            slope_bars    = config.EMA_TREND_SLOPE_BARS
            j_prev_slope  = j - slope_bars
            ema_t_prev_cr = float(ema_trend_1h[j_prev_slope]) \
                if j_prev_slope >= 0 and not np.isnan(ema_trend_1h[j_prev_slope]) else None
            vol_ratio_5m  = float(v5[i] / vol_sma_5m[i]) \
                if not np.isnan(vol_sma_5m[i]) and vol_sma_5m[i] > 0 else None

            i_cross = {
                "ema_fast":       float(ema_fast_1h[j]),   # 1H EMA20 (proven quality)
                "ema_slow":       float(ema_slow_1h[j]),   # 1H EMA50
                "ema_trend":      float(ema_trend_1h[j]),  # 1H EMA200 macro
                "ema_trend_prev": ema_t_prev_cr,
                "rsi":            float(rsi_1h[j]),        # 1H RSI (not noisy 5M)
                "macd_hist":      float(hist_1h[j]),       # 1H MACD
                "macd_hist_prev": float(hist_1h[j - 1]),
                "atr":            float(atr_1h[j]),        # 1H ATR for SL/TP sizing
                "close":          float(c5[i]),            # 5M close as breakout trigger
                "vol_ratio":      vol_ratio_5m,
                "rolling_max":    float(roll_high_1h[j]),  # 1H resistance (significant level)
                "rolling_min":    float(roll_low_1h[j]),   # 1H support
            }
            signal = evaluate_1h_signal(i_cross, 9999)    # cooldown handled above

        else:
            # ── 1H swing: evaluate only on new 1H bar close ──────────────────
            if j <= prev_j:
                continue
            prev_j = j

            if j < 1:
                continue
            needed_1h = [ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j], rsi_1h[j], atr_1h[j]]
            if any(np.isnan(v) for v in needed_1h):
                continue
            if np.isnan(hist_1h[j]) or np.isnan(hist_1h[j - 1]):
                continue

            slope_bars = config.EMA_TREND_SLOPE_BARS
            j_prev     = j - slope_bars
            ema_t_prev = float(ema_trend_1h[j_prev]) if j_prev >= 0 and not np.isnan(ema_trend_1h[j_prev]) else None
            vol_ratio  = float(v1[j] / vol_sma_1h[j]) if not np.isnan(vol_sma_1h[j]) and vol_sma_1h[j] > 0 else None
            atr_ratio  = float(atr_1h[j] / atr_sma_1h[j]) \
                if not np.isnan(atr_sma_1h[j]) and atr_sma_1h[j] > 0 else None
            adx_val    = float(adx_1h[j]) if not np.isnan(adx_1h[j]) else None
            i1 = {
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
                "vol_ratio":      vol_ratio,
                "rolling_max":    float(roll_high_1h[j]) if not np.isnan(roll_high_1h[j]) else None,
                "rolling_min":    float(roll_low_1h[j])  if not np.isnan(roll_low_1h[j])  else None,
            }
            bars_since = j - last_trade_1h_bar
            signal     = evaluate_1h_signal(i1, bars_since)

        # ── mode="daily_open" / "daily_trend": enter once per day based on macro trend ─
        if signal is None and mode in ("daily_open", "daily_trend"):
            bar_day_chk = int(ct_5m[i]) // 86_400_000
            if bar_day_chk != getattr(_do_state, "last_entry_day", -1):
                if j >= 1 and not any(np.isnan([ema_trend_1h[j], ema_fast_1h[j], ema_slow_1h[j], atr_1h[j]])):
                    if mode == "daily_trend":
                        # Pure EMA200 only — enter LONG above EMA200, SHORT below
                        _do_side = "LONG" if c5[i] > ema_trend_1h[j] else "SHORT"
                    else:
                        # daily_open: require EMA20/50 alignment too
                        _do_side = "LONG" if c5[i] > ema_trend_1h[j] and ema_fast_1h[j] > ema_slow_1h[j] \
                                   else ("SHORT" if c5[i] < ema_trend_1h[j] and ema_fast_1h[j] < ema_slow_1h[j] else None)
                    if _do_side is not None:
                        _do_atr = float(atr_1h[j])
                        _do_entry = float(c5[i])
                        _do_sl = _do_entry - _do_atr * config.ATR_SL_MULTIPLIER if _do_side == "LONG" \
                                 else _do_entry + _do_atr * config.ATR_SL_MULTIPLIER
                        _do_tp = _do_entry + _do_atr * config.ATR_TP_MULTIPLIER if _do_side == "LONG" \
                                 else _do_entry - _do_atr * config.ATR_TP_MULTIPLIER
                        from strategy import Signal as _Sig
                        signal = _Sig(side=_do_side, entry=_do_entry, sl=_do_sl, tp=_do_tp,
                                      sl_pct=abs(_do_entry-_do_sl)/_do_entry*100,
                                      tp_pct=abs(_do_tp-_do_entry)/_do_entry*100,
                                      rr_ratio=config.ATR_TP_MULTIPLIER/config.ATR_SL_MULTIPLIER,
                                      reason=mode, indicators_5m={}, indicators_1h={})
                        _do_state.last_entry_day = bar_day_chk

        if signal is None:
            continue

        entry_price = _apply_slippage(signal.entry, signal.side, is_entry=True)
        qty         = _position_qty(balance, entry_price, signal.sl)
        if qty < 0.001:
            continue

        balance -= _commission_cost(entry_price, qty)
        position = {
            "side":          signal.side,
            "entry_price":   entry_price,
            "sl":            signal.sl,
            "tp":            signal.tp,
            "qty":           qty,
            "entry_time":    pd.Timestamp(ct_5m[i], unit="ms"),
            "bar_idx":       i,
            "entry_atr":     float(atr_1h[j]),
            "be_activated":  False,
            "lock_activated": False,
        }
        last_trade_1h_bar = j

    # ── Force-close at end of data ────────────────────────────────────────────
    if position is not None:
        exit_price = _apply_slippage(float(c5[-1]), position["side"], is_entry=False)
        direction  = 1 if position["side"] == "LONG" else -1
        gross_pnl  = (exit_price - position["entry_price"]) * direction * position["qty"]
        cost       = _commission_cost(exit_price, position["qty"])
        net_pnl    = gross_pnl - cost
        balance   += net_pnl
        trades.append(Trade(
            side=position["side"],
            entry_time=position["entry_time"],
            exit_time=pd.Timestamp(ct_5m[-1], unit="ms"),
            entry_price=position["entry_price"],
            exit_price=exit_price,
            qty=position["qty"],
            pnl=net_pnl,
            pnl_pct=net_pnl / (position["entry_price"] * position["qty"]) * 100,
            close_reason="EOD",
            balance_after=balance,
            bars_held=n - 1 - position["bar_idx"],
        ))
        equity_times.append(pd.Timestamp(ct_5m[-1], unit="ms"))
        equity_vals.append(balance)

    # Flush the last day's record
    if current_day_epoch >= 0:
        day_pnl_pct = (balance - day_start_balance) / day_start_balance * 100
        _daily_records.append((
            pd.Timestamp(current_day_epoch * 86_400_000, unit="ms").date(),
            day_pnl_pct,
        ))
        if daily_target_hit:
            days_target_hit += 1
        if daily_profit_hit:
            days_profit_hit += 1
        elif daily_target_hit:
            days_loss_hit += 1

    equity_curve = pd.Series(
        equity_vals,
        index=pd.DatetimeIndex(equity_times),
        name="balance",
    )

    # Build daily PnL Series
    if _daily_records:
        dates, pnl_pcts = zip(*_daily_records)
        daily_pnl = pd.Series(list(pnl_pcts),
                              index=pd.DatetimeIndex([pd.Timestamp(d) for d in dates]),
                              name="daily_pnl_pct")
    else:
        daily_pnl = pd.Series(name="daily_pnl_pct", dtype=float)

    stats = _compute_stats(trades, initial_balance, balance, equity_curve,
                           days_target_hit, len(_daily_records),
                           days_profit_hit, days_loss_hit)
    return BacktestResult(trades=trades, equity_curve=equity_curve,
                          stats=stats, df_5m=df_5m, df_1h=df_1h, daily_pnl=daily_pnl)


def _compute_stats(
    trades: list[Trade],
    initial_balance: float,
    final_balance: float,
    equity: pd.Series,
    days_target_hit: int = 0,
    total_days: int = 0,
    days_profit_hit: int = 0,
    days_loss_hit: int = 0,
) -> dict:
    base = {"total_trades": 0, "initial_balance": initial_balance,
            "final_balance": final_balance,
            "total_return_pct": (final_balance - initial_balance) / initial_balance * 100,
            "days_target_hit": days_target_hit, "total_days": total_days,
            "days_profit_hit": days_profit_hit, "days_loss_hit": days_loss_hit}
    if not trades:
        return base

    pnls         = np.array([t.pnl for t in trades])
    wins         = pnls[pnls > 0]
    losses       = pnls[pnls <= 0]
    gross_profit = float(wins.sum())         if len(wins)   else 0.0
    gross_loss   = float(abs(losses.sum()))  if len(losses) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    total_return = (final_balance - initial_balance) / initial_balance * 100
    n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 0.01)
    cagr    = ((final_balance / initial_balance) ** (1 / n_years) - 1) * 100

    roll_max = equity.cummax()
    dd       = (equity - roll_max) / roll_max * 100
    max_dd   = float(dd.min())

    daily  = equity.resample("D").last().ffill().pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0

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
        "total_return_pct": total_return,
        "cagr_pct":         cagr,
        "max_drawdown_pct": max_dd,
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
