"""
Trading Strategy — 1H Swing + 5M Scalp modes
═════════════════════════════════════════════

1H SWING (primary, used for backtest):
  Rationale: 1H ATR is ~3-5× larger than 5M ATR, so trading fee (0.05%) is
  only ~9% of risk per trade vs. 33% for 5M — making profitability achievable.

  Entry (at each 1H candle close):
    LONG : price > EMA200  AND  EMA20 > EMA50 (≥0.2%)  AND  RSI ≥ 48
           AND  MACD hist > 0  AND  hist > hist_prev  (momentum accelerating)
    SHORT: price < EMA200  AND  EMA20 < EMA50         AND  RSI ≤ 52
           AND  MACD hist < 0  AND  hist < hist_prev

  SL  = ATR × 1.5   TP = ATR × 3.0  (2:1 RR → break-even ~37% win rate after fee)
  Fee = 0.0500% per fill

5M SCALP (live trading via WebSocket):
  Same logic on 5M data; uses evaluate_from_indicators().
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional

import numpy as np

import config
import indicators as ind
from data_store import MarketState


@dataclass
class Signal:
    side: str
    entry: float
    sl: float
    tp: float
    sl_pct: float
    tp_pct: float
    rr_ratio: float
    reason: str
    indicators_5m: dict
    indicators_1h: dict


# ── 1H swing signal evaluator ────────────────────────────────────────────────

def evaluate_1h_signal(
    i1: dict,
    bars_since_last: int = 9999,
) -> Optional[Signal]:
    """
    Evaluate a 1H-entry swing trade signal from a 1H indicator dict.
    Called once per closed 1H bar.
    """
    if bars_since_last < config.TRADE_COOLDOWN_1H:
        return None

    ema_f = i1.get("ema_fast")
    ema_s = i1.get("ema_slow")
    ema_t = i1.get("ema_trend")
    rsi   = i1.get("rsi")
    hist  = i1.get("macd_hist")
    h_prv = i1.get("macd_hist_prev")
    atr   = i1.get("atr")
    close = i1.get("close")

    ema_t_prev = i1.get("ema_trend_prev")   # EMA200 N bars ago for slope filter
    vol_ratio  = i1.get("vol_ratio")        # current volume / 20-bar MA

    if any(v is None for v in [ema_f, ema_s, ema_t, rsi, hist, h_prv, atr, close]):
        return None
    if close == 0:
        return None

    # Flat market: EMAs too close
    sep = abs(ema_f - ema_s) / ema_s
    if sep < config.EMA_1H_MIN_SEP:
        return None

    # ATR bounds: skip very quiet or spike bars
    atr_pct = atr / close * 100
    if not (config.ATR_1H_PCT_MIN < atr_pct < config.ATR_1H_PCT_MAX):
        return None

    # Volume filter: skip unusually quiet bars (low conviction moves)
    if vol_ratio is not None and vol_ratio < config.VOL_RATIO_MIN:
        return None

    # EMA200 slope: only trade when macro trend is moving in trade direction
    trend_rising  = ema_t_prev is None or ema_t >= ema_t_prev
    trend_falling = ema_t_prev is None or ema_t <= ema_t_prev

    long_ok = (
        ema_f > ema_s                              # EMA20 above EMA50
        and close > ema_t                          # price above EMA200 (macro bull)
        and trend_rising                           # EMA200 still rising (macro momentum)
        and config.RSI_1H_LONG_MIN <= rsi <= config.RSI_1H_LONG_MAX   # bullish but not overbought
        and hist > 0                               # MACD in positive territory
        and hist > h_prv                           # momentum accelerating upward
    )
    short_ok = (
        ema_f < ema_s
        and close < ema_t                          # price below EMA200 (macro bear)
        and trend_falling                          # EMA200 still falling (macro momentum)
        and config.RSI_1H_SHORT_MIN <= rsi <= config.RSI_1H_SHORT_MAX   # bearish but not oversold
        and hist < 0
        and hist < h_prv                           # momentum accelerating downward
    )

    if not long_ok and not short_ok:
        return None

    side  = "LONG" if long_ok else "SHORT"
    entry = close
    sl = entry - atr * config.ATR_SL_MULTIPLIER if side == "LONG" \
         else entry + atr * config.ATR_SL_MULTIPLIER
    tp = entry + atr * config.ATR_TP_MULTIPLIER if side == "LONG" \
         else entry - atr * config.ATR_TP_MULTIPLIER

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr      = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

    reason = (
        f"{side} | ema200={'above' if close > ema_t else 'below'} "
        f"ema_sep={sep*100:.2f}% rsi={rsi:.1f} "
        f"hist={hist:.2f}(Δ{hist-h_prv:+.2f}) "
        f"atr%={atr_pct:.2f} rr={rr}"
    )
    return Signal(
        side=side, entry=entry, sl=sl, tp=tp,
        sl_pct=sl_dist / entry * 100,
        tp_pct=tp_dist / entry * 100,
        rr_ratio=rr, reason=reason,
        indicators_5m={}, indicators_1h=i1,
    )


# ── 5M scalp signal evaluator (live WebSocket mode) ─────────────────────────

def _trend_bias_1h(i1: dict) -> Optional[str]:
    ema_f = i1.get("ema_fast"); ema_s = i1.get("ema_slow")
    ema_t = i1.get("ema_trend"); close = i1.get("close")
    rsi   = i1.get("rsi")
    if any(v is None for v in [ema_f, ema_s, ema_t, close, rsi]):
        return None
    sep = abs(ema_f - ema_s) / ema_s if ema_s else 0
    if sep < config.EMA_1H_MIN_SEP:
        return None
    if ema_f > ema_s and close > ema_t and rsi >= config.RSI_1H_LONG_MIN:
        return "LONG"
    if ema_f < ema_s and close < ema_t and rsi <= config.RSI_1H_SHORT_MAX:
        return "SHORT"
    return None


def evaluate_from_indicators(
    i5: dict, i1: dict, current_price: float,
    bars_since_last_trade: int = 9999,
) -> Optional[Signal]:
    """5M entry evaluator (live trading)."""
    if bars_since_last_trade < config.TRADE_COOLDOWN_1H:
        return None
    trend = _trend_bias_1h(i1)
    if trend is None:
        return None
    hist  = i5.get("macd_hist"); h_prv = i5.get("macd_hist_prev")
    rsi5  = i5.get("rsi"); atr5 = i5.get("atr"); ema_f5 = i5.get("ema_fast")
    if any(v is None for v in [hist, h_prv, rsi5, atr5, ema_f5]):
        return None
    atr_pct = atr5 / current_price * 100 if current_price else 0
    if not (0.10 < atr_pct < 2.5):
        return None
    long_ok  = (trend=="LONG"  and hist>0 and hist>h_prv and 30<rsi5<68 and current_price>ema_f5)
    short_ok = (trend=="SHORT" and hist<0 and hist<h_prv and 32<rsi5<70 and current_price<ema_f5)
    if not long_ok and not short_ok:
        return None
    side  = "LONG" if long_ok else "SHORT"
    entry = current_price
    sl = entry - atr5*config.ATR_SL_MULTIPLIER if side=="LONG" else entry + atr5*config.ATR_SL_MULTIPLIER
    tp = entry + atr5*config.ATR_TP_MULTIPLIER if side=="LONG" else entry - atr5*config.ATR_TP_MULTIPLIER
    sl_d = abs(entry-sl); tp_d = abs(tp-entry)
    rr   = round(tp_d/sl_d, 2) if sl_d else 0
    return Signal(side=side, entry=entry, sl=sl, tp=tp,
                  sl_pct=sl_d/entry*100, tp_pct=tp_d/entry*100, rr_ratio=rr,
                  reason=f"{side} 5M|rsi={rsi5:.1f} hist={hist:.2f} atr%={atr_pct:.2f}",
                  indicators_5m=i5, indicators_1h=i1)


def evaluate(state: MarketState, bars_since_last_trade: int = 9999) -> Optional[Signal]:
    """Live WebSocket entry point."""
    if state.buf_5m.count < config.MIN_CANDLES_5M:
        return None
    if state.buf_1h.count < config.MIN_CANDLES_1H:
        return None
    o5, h5, l5, c5, v5 = state.buf_5m.arrays()
    o1, h1, l1, c1, v1 = state.buf_1h.arrays()
    i5 = ind.compute_all(o5, h5, l5, c5, config, volumes=v5)
    i1 = ind.compute_all(o1, h1, l1, c1, config, volumes=v1)
    ema200 = ind.ema(c1, config.EMA_TREND)
    valid = ema200[~np.isnan(ema200)]
    i1["ema_trend"] = float(valid[-1]) if len(valid) else None
    price = state.mark_price if state.mark_price > 0 else i5["close"]
    return evaluate_from_indicators(i5, i1, price, bars_since_last_trade)


def position_size_usdt(balance, entry, sl,
                       risk_pct=None, leverage=None) -> float:
    if risk_pct is None:
        risk_pct = config.RISK_PERCENT
    if leverage is None:
        leverage = config.LEVERAGE
    sl_dist_pct = abs(entry - sl) / entry
    if sl_dist_pct == 0:
        return 0.0
    return balance * (risk_pct / 100) / (entry * sl_dist_pct)
