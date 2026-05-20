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
from strategy import evaluate_1h_signal

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


def _apply_slippage(price: float, side: str, is_entry: bool) -> float:
    direction = 1 if (side == "LONG") == is_entry else -1
    return price * (1 + direction * SLIPPAGE)


def _commission_cost(price: float, qty: float) -> float:
    return price * qty * COMMISSION


def _position_qty(balance: float, entry: float, sl: float) -> float:
    risk    = balance * (config.RISK_PERCENT / 100)
    sl_dist = abs(entry - sl) / entry
    return risk / (entry * sl_dist) if sl_dist > 0 else 0.0


def run(
    df_5m: pd.DataFrame,
    df_1h: pd.DataFrame,
    initial_balance: float = 1000.0,
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

    # Map each 5M bar → last closed 1H bar
    idx_1h = np.searchsorted(ct_1h, ct_5m, side="right") - 1

    n        = len(df_5m)
    balance  = initial_balance
    trades: list[Trade] = []
    equity_times = [pd.Timestamp(df_5m["open_time"].iloc[0], unit="ms")]
    equity_vals  = [balance]

    position: Optional[dict] = None
    last_trade_1h_bar: int = -9999
    prev_j: int = -1

    warmup = max(config.MIN_CANDLES_5M, config.MIN_CANDLES_1H)

    for i in range(warmup, n - 1):
        j = int(idx_1h[i])
        if j < config.MIN_CANDLES_1H:
            continue

        # ── SL/TP check on the next bar ───────────────────────────────────────
        if position is not None:
            nxt_high = h5[i + 1]
            nxt_low  = l5[i + 1]
            nxt_time = pd.Timestamp(ct_5m[i + 1], unit="ms")

            hit_sl = hit_tp = False
            if position["side"] == "LONG":
                hit_sl = nxt_low  <= position["sl"]
                hit_tp = nxt_high >= position["tp"]
            else:
                hit_sl = nxt_high >= position["sl"]
                hit_tp = nxt_low  <= position["tp"]

            if hit_sl or hit_tp:
                reason     = "SL" if hit_sl else "TP"
                raw_exit   = position["sl"] if hit_sl else position["tp"]
                exit_price = _apply_slippage(raw_exit, position["side"], is_entry=False)
                entry_p    = position["entry_price"]
                qty        = position["qty"]
                direction  = 1 if position["side"] == "LONG" else -1

                gross_pnl = (exit_price - entry_p) * direction * qty
                cost      = _commission_cost(entry_p, qty) + _commission_cost(exit_price, qty)
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
                position = None

                if balance <= 10:
                    logger.warning("Balance critically low — stopping backtest")
                    break
                continue

        if position is not None:
            if j > prev_j:
                prev_j = j
            continue

        # ── Only evaluate signal on new 1H bar close ─────────────────────────
        if j <= prev_j:
            continue
        prev_j = j

        # ── Validate 1H indicator values ──────────────────────────────────────
        if j < 1:
            continue
        needed_1h = [ema_fast_1h[j], ema_slow_1h[j], ema_trend_1h[j], rsi_1h[j], atr_1h[j]]
        if any(np.isnan(v) for v in needed_1h):
            continue
        if np.isnan(hist_1h[j]) or np.isnan(hist_1h[j - 1]):
            continue

        # ── Assemble 1H indicator dict ─────────────────────────────────────────
        slope_bars = config.EMA_TREND_SLOPE_BARS
        j_prev     = j - slope_bars
        ema_t_prev = float(ema_trend_1h[j_prev]) if j_prev >= 0 and not np.isnan(ema_trend_1h[j_prev]) else None
        vol_ratio = float(v1[j] / vol_sma_1h[j]) if not np.isnan(vol_sma_1h[j]) and vol_sma_1h[j] > 0 else None
        i1 = {
            "ema_fast":       float(ema_fast_1h[j]),
            "ema_slow":       float(ema_slow_1h[j]),
            "ema_trend":      float(ema_trend_1h[j]),
            "ema_trend_prev": ema_t_prev,
            "rsi":            float(rsi_1h[j]),
            "macd_hist":      float(hist_1h[j]),
            "macd_hist_prev": float(hist_1h[j - 1]),
            "atr":            float(atr_1h[j]),
            "close":          float(c1[j]),
            "vol_ratio":      vol_ratio,
        }

        bars_since = j - last_trade_1h_bar
        signal = evaluate_1h_signal(i1, bars_since)
        if signal is None:
            continue

        entry_price = _apply_slippage(signal.entry, signal.side, is_entry=True)
        qty         = _position_qty(balance, entry_price, signal.sl)
        if qty < 0.001:
            continue

        balance -= _commission_cost(entry_price, qty)
        position = {
            "side":        signal.side,
            "entry_price": entry_price,
            "sl":          signal.sl,
            "tp":          signal.tp,
            "qty":         qty,
            "entry_time":  pd.Timestamp(ct_5m[i], unit="ms"),
            "bar_idx":     i,
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

    equity_curve = pd.Series(
        equity_vals,
        index=pd.DatetimeIndex(equity_times),
        name="balance",
    )
    stats = _compute_stats(trades, initial_balance, balance, equity_curve)
    return BacktestResult(trades=trades, equity_curve=equity_curve,
                          stats=stats, df_5m=df_5m, df_1h=df_1h)


def _compute_stats(
    trades: list[Trade],
    initial_balance: float,
    final_balance: float,
    equity: pd.Series,
) -> dict:
    base = {"total_trades": 0, "initial_balance": initial_balance,
            "final_balance": final_balance,
            "total_return_pct": (final_balance - initial_balance) / initial_balance * 100}
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
    }
