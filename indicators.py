"""Technical indicator calculations using pure NumPy."""
import numpy as np
from typing import Optional


def ema(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full_like(values, np.nan, dtype=float)
    if len(values) < period:
        return result
    k = 2.0 / (period + 1)
    result[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        result[i] = values[i] * k + result[i - 1] * (1 - k)
    return result


def sma(values: np.ndarray, period: int) -> np.ndarray:
    result = np.full(len(values), np.nan)
    for i in range(period - 1, len(values)):
        result[i] = values[i - period + 1 : i + 1].mean()
    return result


def rsi(closes: np.ndarray, period: int = 14) -> np.ndarray:
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
            result[i] = 100 - (100 / (1 + rs))
    return result


def macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = ema_fast - ema_slow
    valid = ~np.isnan(macd_line)
    signal_line = np.full_like(macd_line, np.nan)
    if valid.sum() >= signal:
        idx = np.where(valid)[0]
        sig_vals = ema(macd_line[valid], signal)
        signal_line[idx] = sig_vals
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> np.ndarray:
    result = np.full_like(closes, np.nan, dtype=float)
    if len(closes) < period + 1:
        return result
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )
    result[period] = tr[:period].mean()
    for i in range(period + 1, len(closes)):
        result[i] = (result[i - 1] * (period - 1) + tr[i - 1]) / period
    return result


def bollinger_bands(
    closes: np.ndarray,
    period: int = 20,
    std_dev: float = 2.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    upper = np.full_like(closes, np.nan, dtype=float)
    middle = np.full_like(closes, np.nan, dtype=float)
    lower = np.full_like(closes, np.nan, dtype=float)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        m = window.mean()
        s = window.std(ddof=0)
        middle[i] = m
        upper[i] = m + std_dev * s
        lower[i] = m - std_dev * s
    return upper, middle, lower


def last(arr: np.ndarray) -> Optional[float]:
    valid = arr[~np.isnan(arr)]
    return float(valid[-1]) if len(valid) > 0 else None


def compute_all(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    cfg,
    volumes: Optional[np.ndarray] = None,
) -> dict:
    ema_fast_arr = ema(closes, cfg.EMA_FAST)
    ema_slow_arr = ema(closes, cfg.EMA_SLOW)
    rsi_arr = rsi(closes, cfg.RSI_PERIOD)
    macd_line, signal_line, histogram = macd(
        closes, cfg.MACD_FAST, cfg.MACD_SLOW, cfg.MACD_SIGNAL
    )
    atr_arr = atr(highs, lows, closes, cfg.ATR_PERIOD)
    bb_upper, bb_mid, bb_lower = bollinger_bands(closes, cfg.BB_PERIOD, cfg.BB_STD)

    # BB width as % of midline
    bb_width = None
    bbu, bbm, bbl = last(bb_upper), last(bb_mid), last(bb_lower)
    if bbu is not None and bbm is not None and bbm > 0:
        bb_width = (bbu - bbl) / bbm * 100

    # RSI previous bar
    valid_rsi = rsi_arr[~np.isnan(rsi_arr)]
    rsi_prev = float(valid_rsi[-2]) if len(valid_rsi) >= 2 else None

    # Volume ratio (current / SMA20)
    vol_ratio = None
    if volumes is not None and len(volumes) >= 20:
        vol_sma = sma(volumes.astype(float), 20)
        last_vol = float(volumes[-1])
        last_sma = last(vol_sma)
        if last_sma and last_sma > 0:
            vol_ratio = last_vol / last_sma

    # Histogram previous bar
    valid_hist = histogram[~np.isnan(histogram)]
    hist_prev = float(valid_hist[-2]) if len(valid_hist) >= 2 else None

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
        "bb_upper":       bbu,
        "bb_mid":         bbm,
        "bb_lower":       bbl,
        "bb_width":       bb_width,
        "vol_ratio":      vol_ratio,
        "close":          float(closes[-1]),
    }
