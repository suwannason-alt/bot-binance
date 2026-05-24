"""
Trading Strategy  —  1H Swing (primary)  +  5M Scalp (live WebSocket)
======================================================================

Quantitative enhancements (all feature-flagged, off by default):
  1. Market regime detection  — skip RANGING bars, extend TP in STRONG_TREND
  2. Dynamic TP / SL          — multiplier adapts to ADX strength at signal time
  3. Volatility-adjusted sizing — position scales inversely with ATR ratio
  4. Candle body quality filter — reject doji/indecision at breakout levels

1H SWING entry conditions (on each closed 1H bar):
  LONG : price > EMA200  AND  EMA20 > EMA50  AND  RSI in [RSI_LONG_MIN, RSI_LONG_MAX]
         AND  close > rolling_N_bar_high + buffer  (momentum breakout)
         AND  EMA200 slope rising  AND  ADX >= ADX_MIN
  SHORT: mirror of LONG

SL = entry +/- ATR_SL_MULTIPLIER × ATR
TP = entry +/- dynamic_tp_mult(regime) × ATR
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

import config
import indicators as ind
from data_store import MarketState

logger = logging.getLogger("strategy")


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Signal:
    """A validated trade signal produced by the strategy evaluator.

    Attributes:
        side: Trade direction — ``"LONG"`` or ``"SHORT"``.
        entry: Intended entry price.
        sl: Initial stop-loss price.
        tp: Take-profit price.
        sl_pct: Stop-loss distance as a percentage of entry.
        tp_pct: Take-profit distance as a percentage of entry.
        rr_ratio: Reward-to-risk ratio (``tp_dist / sl_dist``).
        reason: Human-readable description of why the signal was generated.
        regime: Market regime at signal time (``"STRONG_TREND"``, ``"WEAK_TREND"``,
            ``"RANGING"``, or ``"HIGH_VOL"``).
        size_scale: Position-size multiplier (``1.0`` in normal conditions;
            ``< 1.0`` in ``HIGH_VOL`` regime).
        indicators_5m: Snapshot of 5-minute indicator values at signal time.
        indicators_1h: Snapshot of 1-hour indicator values at signal time.
    """

    side: str
    entry: float
    sl: float
    tp: float
    sl_pct: float
    tp_pct: float
    rr_ratio: float
    reason: str
    regime: str
    size_scale: float
    indicators_5m: Dict
    indicators_1h: Dict


# ─────────────────────────────────────────────────────────────────────────────
# 1. Market regime detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_regime(
    adx: Optional[float],
    atr_pct: float,
    atr_ratio: Optional[float],
) -> str:
    """Classify the current market into one of four regimes.

    Args:
        adx: Current ADX value, or ``None`` if insufficient history.
        atr_pct: ATR expressed as a percentage of current price (``atr/close*100``).
        atr_ratio: Current ATR divided by its 20-bar SMA; ``None`` if unavailable.

    Returns:
        One of the following regime strings:

          - ``"HIGH_VOL"``     — ATR% ≥ ``REGIME_HIGH_VOL_PCT`` (extreme volatility spike).
          - ``"RANGING"``      — ADX < ``ADX_MIN`` or ATR contracting below ``ATR_RATIO_MIN``.
          - ``"STRONG_TREND"`` — ADX ≥ ``REGIME_STRONG_ADX`` with expanding volatility.
          - ``"WEAK_TREND"``   — ADX ≥ ``ADX_MIN`` but below the strong-trend threshold.

    Note:
        ``RANGING`` causes callers to skip entry when ``REGIME_FILTER_ENABLED`` is set.
        ``HIGH_VOL`` allows entry but scales position size by ``HIGH_VOL_SIZE_SCALE``.
    """
    # Extreme volatility check first (takes priority over trend classification)
    if atr_pct >= config.REGIME_HIGH_VOL_PCT:
        return "HIGH_VOL"

    # No meaningful trend
    if adx is None or adx < config.ADX_MIN:
        return "RANGING"

    # ATR contracting while ADX still elevated = trend dying / choppy
    if config.ATR_RATIO_MIN > 0 and atr_ratio is not None and atr_ratio < config.ATR_RATIO_MIN:
        return "RANGING"

    # Strong trend: ADX well above threshold and volatility is expanding
    if adx >= config.REGIME_STRONG_ADX:
        return "STRONG_TREND"

    return "WEAK_TREND"


# ─────────────────────────────────────────────────────────────────────────────
# 2. Dynamic TP multiplier
# ─────────────────────────────────────────────────────────────────────────────

def dynamic_tp_mult(regime: str) -> float:
    """Return the TP ATR-multiplier for a trade, adjusted by market regime.

    Args:
        regime: Current market regime string (from :func:`detect_regime`).

    Returns:
        The effective TP multiplier as a float:

          - ``STRONG_TREND`` → ``ATR_TP_MULTIPLIER × DYNAMIC_TP_STRONG_MULT``
            (let winners run further).
          - ``WEAK_TREND``   → ``ATR_TP_MULTIPLIER × DYNAMIC_TP_WEAK_MULT``
            (take profits before momentum fades).
          - All other regimes → ``ATR_TP_MULTIPLIER`` unchanged.

        Always returns ``ATR_TP_MULTIPLIER`` when ``DYNAMIC_TP_ENABLED`` is ``False``.
    """
    if not config.DYNAMIC_TP_ENABLED:
        return config.ATR_TP_MULTIPLIER
    if regime == "STRONG_TREND":
        return config.ATR_TP_MULTIPLIER * config.DYNAMIC_TP_STRONG_MULT
    if regime == "WEAK_TREND":
        return config.ATR_TP_MULTIPLIER * config.DYNAMIC_TP_WEAK_MULT
    return config.ATR_TP_MULTIPLIER


# ─────────────────────────────────────────────────────────────────────────────
# 3. Position sizing (volatility-adjusted)
# ─────────────────────────────────────────────────────────────────────────────

def position_size_usdt(
    balance: float,
    entry: float,
    sl: float,
    risk_pct: Optional[float] = None,
    leverage: Optional[int] = None,
    atr_ratio: Optional[float] = None,
    size_scale: float = 1.0,
) -> float:
    """Compute the contract quantity to open for a given risk profile.

    Sizing priority (highest → lowest):

    1. ``ORDER_BALANCE_USD > 0`` — fixed margin × leverage; ignores SL distance.
    2. ``RISK_USD > 0``          — fixed-dollar risk, capped by leverage margin.
    3. ``RISK_PERCENT``          — percentage of balance risk, optionally vol-adjusted.

    Volatility-adjusted sizing (when ``VOL_SIZING_ENABLED`` and ``atr_ratio`` given):
    Scales ``risk_pct`` inversely with the ATR ratio so expected dollar-risk per
    trade stays constant across low-vol and high-vol conditions. For example:

      - ATR 1.8× normal → risk × 0.56  (expanded volatility → smaller position)
      - ATR 0.6× normal → risk × 1.25  (compressed volatility → larger position)

    Args:
        balance:    Current account balance in USDT.
        entry:      Intended entry price.
        sl:         Stop-loss price.
        risk_pct:   Risk percentage override; uses ``RISK_PERCENT`` when ``None``.
        leverage:   Leverage cap override; uses ``LEVERAGE`` when ``None``.
        atr_ratio:  Current ATR divided by its 20-bar SMA; used for vol-adjusted sizing.
        size_scale: Additional multiplier applied after all other calculations
            (``< 1.0`` reduces position in ``HIGH_VOL`` regime).

    Returns:
        Un-rounded contract quantity as a float.  Callers must apply lot-step
        rounding before submitting an order.
    """
    # 1. Fixed order-balance sizing — does not compound; use RISK_PERCENT for growth
    if config.ORDER_BALANCE_USD > 0:
        notional = config.ORDER_BALANCE_USD * max(config.LEVERAGE, 1)
        return (notional / entry) * size_scale

    # 2 / 3. Risk-based sizing
    if config.RISK_USD > 0:
        risk_dollar = config.RISK_USD
    else:
        base_pct = risk_pct if risk_pct is not None else config.RISK_PERCENT

        # Volatility adjustment: normalise expected $ risk per trade
        if config.VOL_SIZING_ENABLED and atr_ratio is not None and atr_ratio > 0:
            vol_scale = 1.0 / atr_ratio
            vol_scale = max(config.VOL_SIZING_MIN_SCALE,
                            min(config.VOL_SIZING_MAX_SCALE, vol_scale))
            base_pct = base_pct * vol_scale

        risk_dollar = balance * (base_pct / 100)

    # Apply regime size scale (e.g. 0.5 in HIGH_VOL)
    risk_dollar *= size_scale

    sl_dist = abs(entry - sl) / entry
    if sl_dist == 0:
        return 0.0
    qty = risk_dollar / (entry * sl_dist)

    lev = leverage if leverage is not None else config.LEVERAGE
    if lev > 0:
        qty = min(qty, balance * lev / entry)
    return qty


# ─────────────────────────────────────────────────────────────────────────────
# 4. 1H swing signal evaluator  (used by backtest + live)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_1h_signal(
    i1: dict,
    bars_since_last: int = 9999,
) -> Optional[Signal]:
    """Evaluate a 1H-entry swing trade signal from a pre-built indicator dict.

    Applies the full filter stack in order:

    1. Trade cooldown guard.
    2. Market regime gate (skip ``RANGING`` when ``REGIME_FILTER_ENABLED``).
    3. Flat-market / ATR-bounds guards.
    4. Volume ratio filter.
    5. ADX and ATR-ratio filters (when regime filter is disabled).
    6. EMA200 slope filter.
    7. EMA200 distance filter.
    8. MACD momentum confirmation (optional).
    9. Directional conditions (long / short).
    10. Candle body quality filter (optional).

    Args:
        i1: Indicator dictionary for the closed 1H bar.  Expected keys:
            ``ema_fast``, ``ema_slow``, ``ema_trend``, ``ema_trend_prev``,
            ``rsi``, ``macd_hist``, ``macd_hist_prev``, ``atr``, ``atr_ratio``,
            ``close``, ``open``, ``rolling_max``, ``rolling_min``, ``adx``,
            ``vol_ratio``.
        bars_since_last: Number of 1H bars since the previous trade.

    Returns:
        A :class:`Signal` if all conditions are met, otherwise ``None``.
    """
    if bars_since_last < config.TRADE_COOLDOWN_1H:
        return None

    # ── Unpack indicator dict ─────────────────────────────────────────────────
    ema_f = i1.get("ema_fast")
    ema_s = i1.get("ema_slow")
    ema_t = i1.get("ema_trend")
    rsi   = i1.get("rsi")
    hist  = i1.get("macd_hist")
    h_prv = i1.get("macd_hist_prev")
    atr   = i1.get("atr")
    close = i1.get("close")
    open_ = i1.get("open")  # candle open (for body quality filter)

    ema_t_prev  = i1.get("ema_trend_prev")
    vol_ratio   = i1.get("vol_ratio")
    rolling_max = i1.get("rolling_max")
    rolling_min = i1.get("rolling_min")
    atr_ratio   = i1.get("atr_ratio")
    adx_val     = i1.get("adx")

    # ── Basic null / sanity guards ────────────────────────────────────────────
    if any(v is None for v in [ema_f, ema_s, ema_t, rsi, atr, close]):
        return None
    if close == 0 or atr == 0:
        return None

    atr_pct = atr / close * 100

    # ── 1. Market regime detection ────────────────────────────────────────────
    regime = detect_regime(adx_val, atr_pct, atr_ratio)

    # Skip RANGING markets entirely when filter is active
    if config.REGIME_FILTER_ENABLED and regime == "RANGING":
        return None

    # Position-size scale for HIGH_VOL regime
    size_scale = config.HIGH_VOL_SIZE_SCALE if regime == "HIGH_VOL" else 1.0

    # ── 2. Flat-market / ATR-bounds guards ────────────────────────────────────
    sep = abs(ema_f - ema_s) / ema_s
    if sep < config.EMA_1H_MIN_SEP:
        return None

    if not (config.ATR_1H_PCT_MIN < atr_pct < config.ATR_1H_PCT_MAX):
        return None

    # ── 3. Volume filter (institutional participation) ────────────────────────
    if vol_ratio is not None and vol_ratio < config.VOL_RATIO_MIN:
        return None

    # ── 4. ATR-ratio / ADX fallback (when regime filter is disabled) ──────────
    # detect_regime already applies these rules when REGIME_FILTER_ENABLED; kept
    # here for backward compat with configs that set ATR_RATIO_MIN / ADX_MIN
    # without enabling the full regime filter.
    if not config.REGIME_FILTER_ENABLED:
        if config.ATR_RATIO_MIN > 0 and atr_ratio is not None and atr_ratio < config.ATR_RATIO_MIN:
            return None
        if config.ADX_MIN > 0 and adx_val is not None and adx_val < config.ADX_MIN:
            return None

    # ── 5. EMA200 slope filter ────────────────────────────────────────────────
    if config.EMA_SLOPE_MIN_PCT > 0 and ema_t_prev is not None:
        slope_needed  = ema_t * (config.EMA_SLOPE_MIN_PCT / 100)
        trend_rising  = (ema_t - ema_t_prev) >= slope_needed
        trend_falling = (ema_t_prev - ema_t) >= slope_needed
    else:
        trend_rising  = ema_t_prev is None or ema_t >= ema_t_prev
        trend_falling = ema_t_prev is None or ema_t <= ema_t_prev

    # ── 6. EMA200 distance filter ─────────────────────────────────────────────
    ema_dist_pct      = (close - ema_t) / ema_t * 100
    ema_dist_ok_long  =   ema_dist_pct  >=  config.EMA_TREND_DISTANCE_MIN
    ema_dist_ok_short = (-ema_dist_pct) >= config.EMA_TREND_DISTANCE_MIN

    # ── 7. MACD momentum check ────────────────────────────────────────────────
    macd_long_ok  = hist is not None and h_prv is not None and hist > 0 and hist > h_prv
    macd_short_ok = hist is not None and h_prv is not None and hist < 0 and hist < h_prv

    # ── 8. Breakout buffer ────────────────────────────────────────────────────
    bo_buf = atr * config.BREAKOUT_ATR_BUFFER

    # ── 9. Directional signal conditions ─────────────────────────────────────
    long_ok = (
        ema_f > ema_s
        and ema_dist_ok_long
        and trend_rising
        and config.RSI_1H_LONG_MIN <= rsi <= config.RSI_1H_LONG_MAX
        and rolling_max is not None
        and close > rolling_max + bo_buf
        and (not config.REQUIRE_MACD_CONFIRM or macd_long_ok)
    )
    short_ok = (
        ema_f < ema_s
        and ema_dist_ok_short
        and trend_falling
        and config.RSI_1H_SHORT_MIN <= rsi <= config.RSI_1H_SHORT_MAX
        and rolling_min is not None
        and close < rolling_min - bo_buf
        and (not config.REQUIRE_MACD_CONFIRM or macd_short_ok)
    )

    if not long_ok and not short_ok:
        return None

    # ── 10. Candle body quality filter ────────────────────────────────────────
    # Reject doji / pin-bar candles at the breakout level.
    if config.BODY_ATR_RATIO_MIN > 0 and open_ is not None:
        if abs(close - open_) / atr < config.BODY_ATR_RATIO_MIN:
            return None

    # ── Build signal ──────────────────────────────────────────────────────────
    side    = "LONG" if long_ok else "SHORT"
    entry   = close
    tp_mult = dynamic_tp_mult(regime)

    sl = (entry - atr * config.ATR_SL_MULTIPLIER if side == "LONG"
          else entry + atr * config.ATR_SL_MULTIPLIER)
    tp = (entry + atr * tp_mult if side == "LONG"
          else entry - atr * tp_mult)

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr      = round(tp_dist / sl_dist, 2) if sl_dist > 0 else 0

    adx_str = f"{adx_val:.1f}" if adx_val is not None else "n/a"
    bo_lvl  = rolling_max if side == "LONG" else rolling_min
    reason  = (
        f"{side}|regime={regime}  ema200={'above' if close > ema_t else 'below'}  "
        f"sep={sep*100:.2f}%  rsi={rsi:.1f}  adx={adx_str}  "
        f"bo={bo_lvl:.2f}  atr%={atr_pct:.2f}  tp_mult={tp_mult:.1f}  rr={rr}"
    )

    return Signal(
        side=side, entry=entry, sl=sl, tp=tp,
        sl_pct=sl_dist / entry * 100,
        tp_pct=tp_dist / entry * 100,
        rr_ratio=rr,
        reason=reason,
        regime=regime,
        size_scale=size_scale,
        indicators_5m={},
        indicators_1h=i1,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5M scalp evaluator  (live WebSocket mode)
# ─────────────────────────────────────────────────────────────────────────────

def _trend_bias_1h(i1: dict) -> Optional[str]:
    """Determine directional bias from 1H indicators for 5M scalp filtering.

    Args:
        i1: 1-hour indicator dictionary containing ``ema_fast``, ``ema_slow``,
            ``ema_trend``, ``close``, and ``rsi``.

    Returns:
        ``"LONG"`` or ``"SHORT"`` when a clear bias exists, otherwise ``None``.
    """
    ema_f = i1.get("ema_fast")
    ema_s = i1.get("ema_slow")
    ema_t = i1.get("ema_trend")
    close = i1.get("close")
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
    i5: dict,
    i1: dict,
    current_price: float,
    bars_since_last_trade: int = 9999,
) -> Optional[Signal]:
    """Evaluate a 5-minute scalp entry signal from pre-built indicator dicts.

    Used by the live WebSocket engine after each 5M candle close.  Entry is
    filtered by the 1H trend bias so scalps always trade with the higher
    timeframe direction.

    Args:
        i5: 5-minute indicator dictionary (from :func:`indicators.compute_all`).
        i1: 1-hour indicator dictionary (from :func:`indicators.compute_all`).
        current_price: Latest mark price at evaluation time.
        bars_since_last_trade: 1H bars elapsed since the previous trade.

    Returns:
        A :class:`Signal` if all conditions are met, otherwise ``None``.
    """
    if bars_since_last_trade < config.TRADE_COOLDOWN_1H:
        return None
    trend = _trend_bias_1h(i1)
    if trend is None:
        return None

    hist   = i5.get("macd_hist")
    h_prv  = i5.get("macd_hist_prev")
    rsi5   = i5.get("rsi")
    atr5   = i5.get("atr")
    ema_f5 = i5.get("ema_fast")

    if any(v is None for v in [hist, h_prv, rsi5, atr5, ema_f5]):
        return None

    atr_pct = atr5 / current_price * 100 if current_price else 0
    if not (0.10 < atr_pct < 2.5):
        return None

    long_ok  = (trend == "LONG"  and hist > 0 and hist > h_prv and 30 < rsi5 < 68
                and current_price > ema_f5)
    short_ok = (trend == "SHORT" and hist < 0 and hist < h_prv and 32 < rsi5 < 70
                and current_price < ema_f5)

    if not long_ok and not short_ok:
        return None

    side  = "LONG" if long_ok else "SHORT"
    entry = current_price
    sl    = (entry - atr5 * config.ATR_SL_MULTIPLIER if side == "LONG"
             else entry + atr5 * config.ATR_SL_MULTIPLIER)
    tp    = (entry + atr5 * config.ATR_TP_MULTIPLIER if side == "LONG"
             else entry - atr5 * config.ATR_TP_MULTIPLIER)

    sl_dist = abs(entry - sl)
    tp_dist = abs(tp - entry)
    rr      = round(tp_dist / sl_dist, 2) if sl_dist else 0

    return Signal(
        side=side, entry=entry, sl=sl, tp=tp,
        sl_pct=sl_dist / entry * 100,
        tp_pct=tp_dist / entry * 100,
        rr_ratio=rr,
        reason=f"{side} 5M|rsi={rsi5:.1f} hist={hist:.2f} atr%={atr_pct:.2f}",
        regime="UNKNOWN",
        size_scale=1.0,
        indicators_5m=i5,
        indicators_1h=i1,
    )


def evaluate(state: MarketState, bars_since_last_trade: int = 9999) -> Optional[Signal]:
    """Evaluate a live 5M scalp signal from the current :class:`MarketState`.

    Computes all indicators from the raw candle buffers, enriches the 1H
    indicator dict with EMA200, then delegates to
    :func:`evaluate_from_indicators`.

    Args:
        state: Current :class:`~data_store.MarketState` with live candle buffers.
        bars_since_last_trade: 1H bars elapsed since the previous trade.

    Returns:
        A :class:`Signal` if a valid scalp entry is found, otherwise ``None``.
    """
    if state.buf_5m.count < config.MIN_CANDLES_5M:
        return None
    if state.buf_1h.count < config.MIN_CANDLES_1H:
        return None

    o5, h5, l5, c5, v5 = state.buf_5m.arrays()
    o1, h1, l1, c1, v1 = state.buf_1h.arrays()

    i5 = ind.compute_all(o5, h5, l5, c5, config, volumes=v5)
    i1 = ind.compute_all(o1, h1, l1, c1, config, volumes=v1)

    ema200 = ind.ema(c1, config.EMA_TREND)
    valid  = ema200[~np.isnan(ema200)]
    i1["ema_trend"] = float(valid[-1]) if len(valid) else None

    price = state.mark_price if state.mark_price > 0 else i5["close"]
    return evaluate_from_indicators(i5, i1, price, bars_since_last_trade)


# ─────────────────────────────────────────────────────────────────────────────
# 1H live evaluator  (mirrors backtest strategy exactly)
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_1h_live(state: MarketState, bars_since_last: int = 9999) -> Optional[Signal]:
    """Evaluate a 1H breakout signal for live trading.

    Mirrors the proven backtest strategy exactly, including all quantitative
    enhancements.  Called by ``main.on_1h_close`` on every closed 1H bar.

    Args:
        state: Current :class:`~data_store.MarketState` with live candle buffers.
        bars_since_last: Number of 1H bars elapsed since the previous trade.

    Returns:
        A :class:`Signal` if all entry conditions are satisfied, otherwise ``None``.
    """
    if state.buf_1h.count < config.MIN_CANDLES_1H:
        return None

    o1, h1, l1, c1, v1 = state.buf_1h.arrays()
    i1 = ind.compute_all(o1, h1, l1, c1, config, volumes=v1)

    # ── EMA200 (macro trend) ──────────────────────────────────────────────────
    ema200_arr   = ind.ema(c1, config.EMA_TREND)
    valid_ema200 = ema200_arr[~np.isnan(ema200_arr)]
    i1["ema_trend"] = float(valid_ema200[-1]) if len(valid_ema200) else None

    # ── EMA200 slope ──────────────────────────────────────────────────────────
    slope_bars = config.EMA_TREND_SLOPE_BARS
    i1["ema_trend_prev"] = (float(valid_ema200[-slope_bars - 1])
                            if len(valid_ema200) > slope_bars else None)

    # ── Candle open (for body quality filter) ─────────────────────────────────
    i1["open"] = float(o1[-1])

    # ── Rolling high/low for breakout ─────────────────────────────────────────
    bp = config.BREAKOUT_PERIOD
    if len(c1) > bp:
        i1["rolling_max"] = float(h1[-bp - 1:-1].max())
        i1["rolling_min"] = float(l1[-bp - 1:-1].min())
    else:
        i1["rolling_max"] = None
        i1["rolling_min"] = None

    # ── ADX ───────────────────────────────────────────────────────────────────
    adx_arr = ind.adx(h1, l1, c1, config.ADX_PERIOD)
    i1["adx"] = ind.last(adx_arr)

    return evaluate_1h_signal(i1, bars_since_last)
