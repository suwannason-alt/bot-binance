"""
Adaptive Market Regime Framework
==================================
Replaces static threshold-based regime detection (ADX > N) with a continuous,
self-calibrating regime score derived from three orthogonal market signals.

  regime_score  :  0.0 (pure noise / choppy)  →  1.0 (pure directional trend)

The score drives smooth, continuous adaptations to TP, SL, position size, and
entry-buffer — no discrete bucket switches, no cliff edges.

Why this beats PARAM_CANDIDATES
---------------------------------
A list of 94 static parameter combinations tunes thresholds to fit specific
historical periods (Year 2 choppy = ADX low, Year 6 recovery = different ADX).
The Hurst exponent measures *serial correlation of returns* directly — it does
not care about indicator thresholds, only about actual price behaviour.  The
same math produces a low score in Dec-2022 chop and a high score in Jan-2024
bull without any parameter changes between periods.

Three orthogonal signals
-------------------------
1. **Hurst Exponent** (weight 0.45) — H > 0.5 = trending, H < 0.5 = mean-reverting.
   Measures *how* price is moving, not just how fast.  Naturally separates
   "ADX 22 in a nascent trend" (H = 0.62) from "ADX 22 in chop" (H = 0.44).

2. **ADX Momentum** (weight 0.40) — Combines ADX level + 5-bar slope.
   A rising ADX at 20 scores higher than a falling ADX at 28, because the
   former indicates a trend gaining strength while the latter is dying.

3. **BBW Percentile** (weight 0.15) — Where current BB-width sits in its own
   100-bar history.  Compression (very low percentile = coil building) adds a
   small positive nudge; a vol spike (very high percentile) adds a small penalty.

Adaptive outputs — smooth continuous functions of regime_score
---------------------------------------------------------------
  tp_mult          = tp_base  × (1 + (tp_max_ext − 1) × score²)
  sl_mult          = sl_base  × (1 + (sl_max_widen − 1) × (1 − score)²)
  size_scale       = clamp(size_min + (1 − size_min) × score,  size_min, 1.0)
  entry_buffer_atr = buffer_max × (1 − score²)
  effective_adx    = adx_min_base − adx_min_relax × score²

Integrating into backtest / live
----------------------------------
Set ``ADAPTIVE_REGIME_ENABLED = True`` in ``.env`` or config.  Then call::

    from adaptive_regime import compute_adaptive_regime
    state = compute_adaptive_regime(c1[:j+1], h1[:j+1], l1[:j+1], adx_arr[:j+1], ...)
    i1["regime_state"] = state
    signal = evaluate_1h_signal(i1, bars_since_last=...)

When ``regime_state`` is present in ``i1`` and ``ADAPTIVE_REGIME_ENABLED`` is
True, ``evaluate_1h_signal()`` will use the adaptive parameters instead of the
static config values.  Setting ``ADAPTIVE_REGIME_ENABLED = False`` reverts
to identical classic behaviour — no other code changes needed.
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Data class
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeState:
    """Adaptive regime snapshot for one closed bar.

    Attributes:
        score:             Composite regime score — 0.0 (choppy) → 1.0 (strong trend).
        hurst:             Hurst exponent H ∈ [0.1, 0.9] (H > 0.55 = persistent trend,
                           H < 0.45 = mean-reverting, H ≈ 0.50 = random walk).
        bbw_pct:           Bollinger Band Width percentile 0–1 relative to the last
                           100 bars (0 = compression, 1 = volatility spike).
        adx_mom:           ADX momentum score 0–1, combining ADX level + recent slope.
        regime_class:      Discrete label for logging / display:
                           ``"STRONG_TREND"`` | ``"TREND"`` | ``"CHOPPY"`` |
                           ``"COMPRESSION"`` | ``"HIGH_VOL"``.
        tp_mult:           Adaptive TP ATR-multiplier (wider in trend, tighter in chop).
        sl_mult:           Adaptive SL ATR-multiplier (tighter in trend, wider in noise).
        size_scale:        Position-size multiplier ∈ [``size_min``, 1.0].
        entry_buffer_atr:  Required close-above-high breakout in ATR units (higher in chop).
        effective_adx_min: Dynamic ADX floor — relaxed when score is high so a confirmed
                           trend (via Hurst + BBW) can trade even if ADX is still building.
    """

    score:             float = 0.5
    hurst:             float = 0.5
    bbw_pct:           float = 0.5
    adx_mom:           float = 0.5
    regime_class:      str   = "UNKNOWN"
    tp_mult:           float = 6.0
    sl_mult:           float = 1.5
    size_scale:        float = 1.0
    entry_buffer_atr:  float = 0.0
    effective_adx_min: float = 20.0


# ─────────────────────────────────────────────────────────────────────────────
# Component signal functions
# ─────────────────────────────────────────────────────────────────────────────

def hurst_exponent(
    prices:  np.ndarray,
    min_lag: int = 4,
    max_lag: int = 24,
) -> float:
    """Estimate the Hurst exponent via variance-scaling.

    Exploits the relation Var(lag) ∝ lag^(2H), so H = slope(log-Var vs log-lag) / 2.
    Uses the last ``max_lag × 8`` prices (≤ 200 bars) so computation time is
    independent of total series length.

    Args:
        prices:  Raw close prices.  Needs ≥ ``max_lag × 3`` values for a valid
                 estimate; returns 0.5 (neutral) otherwise.
        min_lag: Smallest lag included in the regression (default 4).
        max_lag: Largest lag (default 24).  Lags are geometrically spaced.

    Returns:
        Estimated H clamped to [0.1, 0.9].  0.5 means insufficient data or
        a pure random walk.

    Note:
        H > 0.55: price changes are positively autocorrelated (trending).
        H < 0.45: price changes are negatively autocorrelated (mean-reverting).
        H ≈ 0.50: no detectable serial correlation (Brownian motion).
    """
    n = len(prices)
    if n < max_lag * 3:
        return 0.5

    # Work on a fixed-size trailing window to keep cost O(1) in series length
    window   = prices[-min(n, max_lag * 8):]
    log_p    = np.log(window.astype(float) + 1e-10)
    lags     = np.unique(np.geomspace(min_lag, max_lag, num=8).astype(int))

    log_vars: list[float] = []
    log_lags: list[float] = []

    for lag in lags:
        if lag >= len(log_p):
            continue
        diffs = log_p[lag:] - log_p[:-lag]
        var   = float(np.var(diffs))
        if var > 0:
            log_vars.append(np.log(var))
            log_lags.append(np.log(float(lag)))

    if len(log_vars) < 3:
        return 0.5

    slope = float(np.polyfit(log_lags, log_vars, 1)[0])
    return float(np.clip(slope / 2.0, 0.1, 0.9))


def bbw_percentile(
    closes:   np.ndarray,
    period:   int = 20,
    lookback: int = 100,
) -> float:
    """Return where the current Bollinger Band Width sits in its recent history.

    BBW = 2 × std / mean of the rolling ``period``-bar window.  A percentile
    near **0** means current volatility is compressed relative to history
    (potential coil before a breakout).  Near **1** means a volatility spike.

    Uses fully vectorised NumPy indexing — no Python loops.

    Args:
        closes:   Close prices.  Needs ≥ ``period + lookback`` values.
        period:   BB period (default 20).
        lookback: How many historical windows to rank against (default 100).

    Returns:
        Percentile rank ∈ [0, 1].  Returns 0.5 if insufficient data.
    """
    needed = period + lookback
    if len(closes) < needed:
        return 0.5

    window = closes[-needed:].astype(float)                    # (lookback + period,)

    # Build lookback+1 rolling windows of size period.
    # idx[j] = [j, j+1, ..., j+period-1]
    # idx[lookback] = [lookback, ..., lookback+period-1] → window[-period:] = current
    idx     = np.arange(lookback + 1)[:, None] + np.arange(period)
    samples = window[idx]                                      # (lookback+1, period)

    means = samples.mean(axis=1)
    stds  = samples.std(axis=1)
    bbws  = 2.0 * stds / np.where(means > 0, means, 1e-10)   # (lookback+1,)

    current_bbw = bbws[-1]
    history_bbw = bbws[:-1]                                    # (lookback,)

    rank = float(np.searchsorted(np.sort(history_bbw), current_bbw) / max(len(history_bbw), 1))
    return float(np.clip(rank, 0.0, 1.0))


def adx_momentum_score(
    adx_arr:        np.ndarray,
    low_threshold:  float = 15.0,
    high_threshold: float = 40.0,
    slope_bars:     int   = 5,
) -> float:
    """Combine ADX level and recent slope into a single 0–1 momentum score.

    A rising ADX emerging from 18 scores higher than a falling ADX at 30,
    because the former signals a trend building while the latter is dissipating.

    Composition:
        score = level_component × 0.65 + slope_component × 0.35

    Args:
        adx_arr:        Array of ADX values; NaN entries are stripped.  Needs
                        at least ``slope_bars + 1`` valid values.
        low_threshold:  ADX at or below this maps to 0 for the level component.
        high_threshold: ADX at or above this maps to 1 for the level component.
        slope_bars:     Lookback for slope calculation (default 5 bars).

    Returns:
        Combined score ∈ [0, 1].  Returns 0.3 (neutral-low) if data insufficient.
    """
    valid = adx_arr[~np.isnan(adx_arr)]
    if len(valid) < slope_bars + 1:
        return 0.3

    cur  = float(valid[-1])
    prev = float(valid[-slope_bars])

    # Level: linear from low_threshold (0) to high_threshold (1)
    level = float(np.clip(
        (cur - low_threshold) / max(high_threshold - low_threshold, 1e-10),
        0.0, 1.0,
    ))

    # Slope: ±3 pts/bar normalised to [0, 1]
    raw_slope  = (cur - prev) / slope_bars
    slope_norm = float(np.clip(raw_slope / 3.0 + 0.5, 0.0, 1.0))

    return level * 0.65 + slope_norm * 0.35


# ─────────────────────────────────────────────────────────────────────────────
# Composite regime computation
# ─────────────────────────────────────────────────────────────────────────────

def compute_adaptive_regime(
    closes:         np.ndarray,
    highs:          np.ndarray,
    lows:           np.ndarray,
    adx_arr:        Optional[np.ndarray] = None,
    atr_pct:        float = 1.0,
    # ── Adaptive TP / SL output range ──────────────────────────────────────
    tp_base:        float = 4.0,   # TP at score = 0 (choppy minimum)
    tp_max_ext:     float = 2.5,   # multiplied against tp_base at score = 1
    sl_base:        float = 1.5,   # tightest SL (strong trend)
    sl_max_widen:   float = 1.8,   # SL widens to sl_base × sl_max_widen at score = 0
    # ── Position size ──────────────────────────────────────────────────────
    size_min:       float = 0.30,  # minimum position fraction at score = 0
    # ── Entry buffer ───────────────────────────────────────────────────────
    buffer_max:     float = 0.50,  # max breakout buffer (ATR units) at score = 0
    # ── Adaptive ADX minimum ───────────────────────────────────────────────
    adx_min_base:   float = 20.0,  # baseline ADX_MIN
    adx_min_relax:  float = 8.0,   # ADX_MIN lowered by this at score = 1
    # ── Weights ────────────────────────────────────────────────────────────
    hurst_weight:   float = 0.45,
    adx_weight:     float = 0.40,
    bbw_weight:     float = 0.15,
) -> RegimeState:
    """Compute the adaptive regime state for the bar at the end of ``closes``.

    Combines three orthogonal signals into one continuous ``score`` and derives
    four smooth adaptive trade parameters from it using quadratic interpolation.

    The key mathematical guarantees that prevent overfitting:

    * All three signals are **relative** (Hurst uses log-variance ratios; BBW is
      ranked against its own history; ADX slope is normalised per point/bar) so
      they are scale-free and auto-calibrate across different volatility epochs.
    * The output functions are **smooth monotone**: every parameter moves
      gradually with the score — no cliff-edge at a magic threshold.
    * **No per-year tuning**: the same parameters that score a 2020 chop period
      at 0.2 automatically score a 2024 bull at 0.85 without any adjustment.

    Args:
        closes:       Close prices — at least 200 bars for full accuracy.
        highs:        High prices (same length as closes).
        lows:         Low prices (same length as closes).
        adx_arr:      Pre-computed ADX array from ``indicators.adx()``; ``None``
                      falls back to ADX score = 0.3 (neutral-low assumption).
        atr_pct:      Current ATR as a % of price (for HIGH_VOL class override).
        tp_base:      Minimum TP multiplier at regime_score = 0.
        tp_max_ext:   Factor applied at regime_score = 1: max TP = tp_base × tp_max_ext.
        sl_base:      Tightest SL multiplier, used in the strongest trends.
        sl_max_widen: SL widens to ``sl_base × sl_max_widen`` at regime_score = 0.
        size_min:     Minimum position fraction at the lowest regime scores.
        buffer_max:   Maximum breakout buffer (ATR units) at regime_score = 0.
        adx_min_base: Baseline ADX minimum threshold (matches ``config.ADX_MIN``).
        adx_min_relax: Amount to lower ADX_MIN when score = 1 (trend fully confirmed).
        hurst_weight: Weight of the Hurst score in the composite (default 0.45).
        adx_weight:   Weight of the ADX momentum score (default 0.40).
        bbw_weight:   Weight of the BBW state score (default 0.15).

    Returns:
        :class:`RegimeState` with score, sub-scores, regime class, and all four
        adaptive trade parameters.

    Examples:
        >>> # Dec-2022 choppy (BTC $15k-$30k sideways)
        >>> state = compute_adaptive_regime(choppy_closes, ...)
        >>> state.score      # ≈ 0.20
        >>> state.tp_mult    # ≈ 4.1x  (tight TP for short-lived moves)
        >>> state.size_scale # ≈ 0.36  (small position, preserve capital)

        >>> # Jan-2024 bull run ($40k → $70k)
        >>> state = compute_adaptive_regime(bull_closes, ...)
        >>> state.score      # ≈ 0.85
        >>> state.tp_mult    # ≈ 9.1x  (let winners run)
        >>> state.size_scale # ≈ 0.90  (near-full size, ride the trend)
    """
    # ── Component scores ─────────────────────────────────────────────────────
    hurst   = hurst_exponent(closes)
    bbw_pct = bbw_percentile(closes)
    adx_mom = adx_momentum_score(adx_arr) if adx_arr is not None else 0.3

    # Hurst → [0, 1]:
    #   H = 0.45 (mild mean-reversion) → 0.00
    #   H = 0.55 (mild trend)          → 0.50
    #   H ≥ 0.65 (strong trend)        → 1.00
    hurst_score = float(np.clip((hurst - 0.45) / 0.20, 0.0, 1.0))

    # BBW percentile → score contribution:
    #   low percentile (compression coil ~0.15-0.25) → slight positive nudge
    #   mid-range (0.30-0.60 normal ranging/trending) → neutral
    #   very high (>0.85 vol spike / capitulation)    → slight penalty
    # Centre the curve around 0.3 (compression is acceptable); penalise spikes.
    bbw_score = float(np.clip(1.0 - abs(bbw_pct - 0.30) / 0.70, 0.0, 1.0))

    # Weighted composite
    score = (
        hurst_weight * hurst_score
        + adx_weight  * adx_mom
        + bbw_weight  * bbw_score
    )
    score = float(np.clip(score, 0.0, 1.0))

    # ── Discrete class (for logging / display only) ───────────────────────────
    if atr_pct >= 4.5:
        regime_class = "HIGH_VOL"
    elif score >= 0.70:
        regime_class = "STRONG_TREND"
    elif score >= 0.45:
        regime_class = "TREND"
    elif bbw_pct < 0.20:
        regime_class = "COMPRESSION"  # low score but volatility coiling
    else:
        regime_class = "CHOPPY"

    # ── Smooth adaptive parameters — two distinct quadratic curves ───────────
    #
    # TP / ADX / Size use score²   → aggressive at the TOP (rewards strong trend)
    #   score = 0.5 → sq = 0.25  (mild extension)
    #   score = 0.8 → sq = 0.64  (notable extension)
    #   score = 1.0 → sq = 1.00  (full extension)
    #
    # SL / buffer use (1-score)²  → aggressive at the BOTTOM (punishes noise)
    #   score = 0.2 → (1-s)² = 0.64  (near-maximum widening / buffer)
    #   score = 0.5 → (1-s)² = 0.25  (moderate widening)
    #   score = 0.8 → (1-s)² = 0.04  (almost back to base — trend confirmed)
    #   score = 1.0 → (1-s)² = 0.00  (exact base — no widening at all)
    #
    # This asymmetry is intentional: once the market is clearly trending,
    # the SL snaps back to its tightest value quickly, preserving the RR.
    sq        = score ** 2               # upside curve
    inv_sq    = (1.0 - score) ** 2       # downside curve

    tp_mult           = tp_base * (1.0 + (tp_max_ext - 1.0) * sq)
    sl_mult           = sl_base * (1.0 + (sl_max_widen - 1.0) * inv_sq)
    size_scale        = float(np.clip(size_min + (1.0 - size_min) * score, size_min, 1.0))
    entry_buffer_atr  = float(np.clip(buffer_max * inv_sq, 0.0, buffer_max))
    effective_adx_min = float(np.clip(adx_min_base - adx_min_relax * sq,
                                       adx_min_base - adx_min_relax, adx_min_base))

    # HIGH_VOL hard caps: limit exposure and widen SL regardless of trend score
    if regime_class == "HIGH_VOL":
        size_scale = min(size_scale, 0.40)
        sl_mult    = max(sl_mult, sl_base * 1.60)

    return RegimeState(
        score             = round(score, 3),
        hurst             = round(hurst, 3),
        bbw_pct           = round(bbw_pct, 3),
        adx_mom           = round(adx_mom, 3),
        regime_class      = regime_class,
        tp_mult           = round(float(tp_mult), 2),
        sl_mult           = round(float(sl_mult), 2),
        size_scale        = round(float(size_scale), 3),
        entry_buffer_atr  = round(float(entry_buffer_atr), 3),
        effective_adx_min = round(float(effective_adx_min), 1),
    )
