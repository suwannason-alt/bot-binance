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

    ema_t_prev  = i1.get("ema_trend_prev")   # EMA200 N bars ago for slope filter
    vol_ratio   = i1.get("vol_ratio")        # current volume / 20-bar MA
    rolling_max = i1.get("rolling_max")      # rolling N-bar high (breakout threshold)
    rolling_min = i1.get("rolling_min")      # rolling N-bar low  (breakout threshold)

    atr_ratio    = i1.get("atr_ratio")
    adx_val      = i1.get("adx")

    macd_long_ok  = hist is not None and h_prv is not None and hist > 0 and hist > h_prv
    macd_short_ok = hist is not None and h_prv is not None and hist < 0 and hist < h_prv

    if any(v is None for v in [ema_f, ema_s, ema_t, rsi, atr, close]):
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

    # ATR ratio filter: skip contracting-ATR (choppy) regimes
    if config.ATR_RATIO_MIN > 0 and atr_ratio is not None and atr_ratio < config.ATR_RATIO_MIN:
        return None

    # ADX filter: skip low-trend-strength (choppy/ranging) markets
    if config.ADX_MIN > 0 and adx_val is not None and adx_val < config.ADX_MIN:
        return None

    # EMA200 slope: only trade when macro trend is moving in trade direction
    # With EMA_SLOPE_MIN_PCT > 0: require EMA200 to have moved a minimum percentage
    # (eliminates flat-EMA false breakouts in sideways markets)
    if config.EMA_SLOPE_MIN_PCT > 0 and ema_t_prev is not None:
        slope_needed = ema_t * (config.EMA_SLOPE_MIN_PCT / 100)
        trend_rising  = (ema_t - ema_t_prev) >= slope_needed
        trend_falling = (ema_t_prev - ema_t) >= slope_needed
    else:
        trend_rising  = ema_t_prev is None or ema_t >= ema_t_prev
        trend_falling = ema_t_prev is None or ema_t <= ema_t_prev

    bo_buf = atr * config.BREAKOUT_ATR_BUFFER

    # EMA200 distance: how far price is from EMA200 (positive = above)
    ema_dist_pct = (close - ema_t) / ema_t * 100
    ema_dist_ok_long  = ema_dist_pct >= config.EMA_TREND_DISTANCE_MIN
    ema_dist_ok_short = (-ema_dist_pct) >= config.EMA_TREND_DISTANCE_MIN

    long_ok = (
        ema_f > ema_s                              # EMA20 above EMA50
        and ema_dist_ok_long                       # price ≥ EMA200 by EMA_TREND_DISTANCE_MIN%
        and trend_rising                           # EMA200 still rising (macro momentum)
        and config.RSI_1H_LONG_MIN <= rsi <= config.RSI_1H_LONG_MAX   # bullish but not overbought
        and rolling_max is not None
        and close > rolling_max + bo_buf           # price breaks above N-bar high (momentum breakout)
        and (not config.REQUIRE_MACD_CONFIRM or macd_long_ok)
    )
    short_ok = (
        ema_f < ema_s
        and ema_dist_ok_short                      # price ≤ EMA200 by EMA_TREND_DISTANCE_MIN%
        and trend_falling                          # EMA200 still falling (macro momentum)
        and config.RSI_1H_SHORT_MIN <= rsi <= config.RSI_1H_SHORT_MAX   # bearish but not oversold
        and rolling_min is not None
        and close < rolling_min - bo_buf           # price breaks below N-bar low
        and (not config.REQUIRE_MACD_CONFIRM or macd_short_ok)
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
        f"bo_lvl={rolling_max if side=='LONG' else rolling_min:.2f} "
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
    if config.RISK_USD > 0:
        risk = config.RISK_USD
    else:
        risk_pct = risk_pct if risk_pct is not None else config.RISK_PERCENT
        risk = balance * (risk_pct / 100)
    sl_dist_pct = abs(entry - sl) / entry
    if sl_dist_pct == 0:
        return 0.0
    qty = risk / (entry * sl_dist_pct)
    lev = leverage if leverage is not None else config.LEVERAGE
    if lev > 0:
        qty = min(qty, balance * lev / entry)
    return qty


def evaluate_1h_live(state, bars_since_last: int = 9999) -> Optional[Signal]:
    """Evaluate 1H breakout signal for live trading — mirrors the proven backtest strategy."""
    if state.buf_1h.count < config.MIN_CANDLES_1H:
        return None

    o1, h1, l1, c1, v1 = state.buf_1h.arrays()
    i1 = ind.compute_all(o1, h1, l1, c1, config, volumes=v1)

    # EMA200 (macro trend filter)
    ema200_arr = ind.ema(c1, config.EMA_TREND)
    valid_ema200 = ema200_arr[~np.isnan(ema200_arr)]
    i1["ema_trend"] = float(valid_ema200[-1]) if len(valid_ema200) else None

    # EMA200 slope N bars ago
    slope_bars = config.EMA_TREND_SLOPE_BARS
    i1["ema_trend_prev"] = float(valid_ema200[-slope_bars - 1]) \
        if len(valid_ema200) > slope_bars else None

    # Rolling high/low for breakout (exclude current bar — consistent with backtest)
    bp = config.BREAKOUT_PERIOD
    if len(c1) > bp:
        i1["rolling_max"] = float(h1[-bp - 1:-1].max())
        i1["rolling_min"] = float(l1[-bp - 1:-1].min())
    else:
        i1["rolling_max"] = None
        i1["rolling_min"] = None

    # ADX — trend strength filter (critical for bear markets; not in compute_all)
    adx_arr = ind.adx(h1, l1, c1, config.ADX_PERIOD)
    i1["adx"] = ind.last(adx_arr)

    return evaluate_1h_signal(i1, bars_since_last)
