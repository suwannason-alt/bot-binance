"""
Technical indicator calculations using pure NumPy and Pandas.

All functions operate on 1-D NumPy arrays and return arrays of the same
length, with leading NaN values for bars that require more history than
is available.
"""
from __future__ import annotations

from typing import Optional, Tuple

import numpy as np
import pandas as pd


def ema(values: np.ndarray, period: int) -> np.ndarray:
    """Compute an Exponential Moving Average (Wilder / EMA-style multiplier).

    Args:
        values: 1-D array of price values.
        period: Look-back window; determines the smoothing factor k = 2/(period+1).

    Returns:
        Array of EMA values; the first ``period - 1`` entries are NaN.
    """
    result = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return result

    k = 2.0 / (period + 1)
    result[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1.0 - k)
    return result


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Compute a Simple Moving Average over a rolling window.

    Uses ``pandas.Series.rolling`` internally for efficient, vectorised
    computation.  Leading values with insufficient history are NaN.

    Args:
        values: 1-D array of numeric values.
        period: Rolling window length.

    Returns:
        Array of SMA values; the first ``period - 1`` entries are NaN.
    """
    return pd.Series(values, dtype=float).rolling(period, min_periods=period).mean().to_numpy()


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
    """Compute the Relative Strength Index (Wilder's smoothing).

    Args:
        closes: 1-D array of closing prices.
        period: RSI look-back period (default 14).

    Returns:
        Array of RSI values in [0, 100]; leading entries are NaN.
    """
    result = np.full_like(closes, np.nan, dtype=float)
    if len(closes) < period + 1:
        return result

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.full(len(closes), np.nan)
    avg_loss = np.full(len(closes), np.nan)
    avg_gain[period] = gains[:period].mean()
    avg_loss[period] = losses[:period].mean()

    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period

    for i in range(period, len(closes)):
        if avg_loss[i] == 0:
            result[i] = 100.0
        else:
            rs = avg_gain[i] / avg_loss[i]
            result[i] = 100.0 - (100.0 / (1.0 + rs))
    return result


def macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute MACD line, signal line, and histogram.

    Args:
        closes: 1-D array of closing prices.
        fast:   Fast EMA period (default 12).
        slow:   Slow EMA period (default 26).
        signal: Signal EMA period applied to the MACD line (default 9).

    Returns:
        Tuple of ``(macd_line, signal_line, histogram)`` arrays.
    """
    macd_line = ema(closes, fast) - ema(closes, slow)

    valid_mask = ~np.isnan(macd_line)
    signal_line = np.full_like(macd_line, np.nan)
    if valid_mask.sum() >= signal:
        valid_indices = np.where(valid_mask)[0]
        signal_line[valid_indices] = ema(macd_line[valid_mask], signal)

    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Compute Average True Range using Wilder's smoothing.

    Args:
        highs:  1-D array of bar high prices.
        lows:   1-D array of bar low prices.
        closes: 1-D array of closing prices.
        period: ATR look-back period (default 14).

    Returns:
        Array of ATR values; leading entries are NaN.
    """
    result = np.full_like(closes, np.nan, dtype=float)
    if len(closes) < period + 1:
        return result

    true_range = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )

    result[period] = true_range[:period].mean()
    for i in range(period + 1, len(closes)):
        result[i] = (result[i - 1] * (period - 1) + true_range[i - 1]) / period
    return result


def bollinger_bands(
    closes: np.ndarray,
    period: int = 20,
    std_dev: float = 2.0,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute Bollinger Bands (upper, middle, lower).

    Uses ``pandas.Series.rolling`` for efficient, vectorised computation.

    Args:
        closes:  1-D array of closing prices.
        period:  Rolling window length (default 20).
        std_dev: Number of standard deviations for the band width (default 2.0).

    Returns:
        Tuple of ``(upper, middle, lower)`` arrays.
    """
    series = pd.Series(closes, dtype=float)
    middle = series.rolling(period, min_periods=period).mean().to_numpy()
    std = series.rolling(period, min_periods=period).std(ddof=0).to_numpy()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def adx(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    """Compute the Average Directional Index (ADX) using Wilder's smoothing.

    ADX measures trend *strength*, not direction.

    - ADX ≥ 25: strong trend (breakouts are reliable)
    - ADX < 20: ranging / choppy (breakouts frequently fail)

    Args:
        highs:  1-D array of bar high prices.
        lows:   1-D array of bar low prices.
        closes: 1-D array of closing prices.
        period: ADX look-back period (default 14).

    Returns:
        Array of ADX values in [0, 100]; leading entries are NaN.
    """
    n = len(closes)
    result = np.full(n, np.nan, dtype=float)
    if n < period * 2 + 1:
        return result

    h_diff = highs[1:] - highs[:-1]
    l_diff = lows[:-1] - lows[1:]

    plus_dm = np.where((h_diff > l_diff) & (h_diff > 0), h_diff, 0.0)
    minus_dm = np.where((l_diff > h_diff) & (l_diff > 0), l_diff, 0.0)

    true_range = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )

    # Wilder's cumulative smoothing
    atr_w = np.full(n, np.nan, dtype=float)
    pdm_w = np.full(n, np.nan, dtype=float)
    mdm_w = np.full(n, np.nan, dtype=float)

    atr_w[period] = true_range[:period].sum()
    pdm_w[period] = plus_dm[:period].sum()
    mdm_w[period] = minus_dm[:period].sum()

    for i in range(period + 1, n):
        atr_w[i] = atr_w[i - 1] - atr_w[i - 1] / period + true_range[i - 1]
        pdm_w[i] = pdm_w[i - 1] - pdm_w[i - 1] / period + plus_dm[i - 1]
        mdm_w[i] = mdm_w[i - 1] - mdm_w[i - 1] / period + minus_dm[i - 1]

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(atr_w > 0, 100.0 * pdm_w / atr_w, np.nan)
        minus_di = np.where(atr_w > 0, 100.0 * mdm_w / atr_w, np.nan)
        di_sum = plus_di + minus_di
        dx = np.where(
            di_sum > 0,
            100.0 * np.abs(plus_di - minus_di) / di_sum,
            np.nan,
        )

    # First ADX value = mean of the first ``period`` DX values
    start = 2 * period - 1
    dx_window = dx[period:start]
    if not np.any(np.isnan(dx_window)) and len(dx_window) == period - 1:
        result[start] = np.nanmean(dx[period: start + 1])
        for i in range(start + 1, n):
            if not np.isnan(result[i - 1]) and not np.isnan(dx[i]):
                result[i] = (result[i - 1] * (period - 1) + dx[i]) / period

    return result


def last(arr: np.ndarray) -> Optional[float]:
    """Return the last non-NaN value in an array, or ``None`` if all are NaN.

    Args:
        arr: 1-D NumPy array.

    Returns:
        Last valid float value, or ``None``.
    """
    valid = arr[~np.isnan(arr)]
    return float(valid[-1]) if len(valid) > 0 else None


def compute_all(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    cfg: object,
    volumes: Optional[np.ndarray] = None,
) -> dict:
    """Compute all indicators required by the live strategy evaluator.

    Args:
        opens:   1-D array of bar open prices.
        highs:   1-D array of bar high prices.
        lows:    1-D array of bar low prices.
        closes:  1-D array of closing prices.
        cfg:     Config module (must expose EMA_FAST, EMA_SLOW, RSI_PERIOD, etc.).
        volumes: Optional 1-D array of bar volumes; used for volume-ratio filter.

    Returns:
        Dictionary mapping indicator names to their last scalar value (float or None).
    """
    ema_fast_arr = ema(closes, cfg.EMA_FAST)
    ema_slow_arr = ema(closes, cfg.EMA_SLOW)
    rsi_arr = rsi(closes, cfg.RSI_PERIOD)
    macd_line, signal_line, histogram = macd(
        closes, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL
    )
    atr_arr = atr(highs, lows, closes, cfg.ATR_PERIOD)
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, cfg.BB_PERIOD, cfg.BB_STD)

    # Bollinger band width as % of mid-line
    bbu = last(bb_upper)
    bbm = last(bb_mid)
    bbl = last(bb_lower)
    bb_width = (bbu - bbl) / bbm * 100 if (bbu is not None and bbm and bbm > 0) else None

    # RSI and MACD histogram previous-bar values
    valid_rsi = rsi_arr[~np.isnan(rsi_arr)]
    rsi_prev = float(valid_rsi[-2]) if len(valid_rsi) >= 2 else None

    valid_hist = histogram[~np.isnan(histogram)]
    hist_prev = float(valid_hist[-2]) if len(valid_hist) >= 2 else None

    # Volume ratio: current bar volume / 20-bar SMA
    vol_ratio: Optional[float] = None
    if volumes is not None and len(volumes) >= 20:
        vol_sma = sma(volumes.astype(float), 20)
        last_sma = last(vol_sma)
        if last_sma and last_sma > 0:
            vol_ratio = float(volumes[-1]) / last_sma

    # ATR ratio: current ATR / 20-bar ATR SMA (>1 = expanding, <1 = contracting)
    atr_ratio: Optional[float] = None
    atr_sma_arr = sma(atr_arr, 20)
    last_atr = last(atr_arr)
    last_atr_sma = last(atr_sma_arr)
    if last_atr is not None and last_atr_sma is not None and last_atr_sma > 0:
        atr_ratio = last_atr / last_atr_sma

    return {
        "ema_fast":       last(ema_fast_arr),
        "ema_slow":       last(ema_slow_arr),
        "rsi":            last(rsi_arr),
        "rsi_prev":       rsi_prev,
        "macd":           last(macd_line),
        "macd_signal":    last(signal_line),
        "macd_hist":      last(histogram),
        "macd_hist_prev": hist_prev,
        "atr":            last(atr_arr),
        "atr_ratio":      atr_ratio,
        "bb_upper":       bbu,
        "bb_mid":         bbm,
        "bb_lower":       bbl,
        "bb_width":       bb_width,
        "vol_ratio":      vol_ratio,
        "close":          float(closes[-1]),
    }
