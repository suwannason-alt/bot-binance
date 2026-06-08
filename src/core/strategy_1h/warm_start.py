"""Warm-start data pre-loader and in-memory strategy hydration.

Solves the "cold-start" problem: on day-one (or after a crash), the bot has
no rolling history to compute EMAs, ADX, Hurst, or WFO parameters.  This
module fetches sufficient historical 1H candles, computes all indicators, runs
the WFO sweep and Markov forecaster through every historical bar, and hands
a fully-hydrated :class:`WarmStartResult` to the live execution loop.

Architecture overview::

    ┌─────────────────────────────────────────────────────────────────────┐
    │  STARTUP                                                            │
    │                                                                     │
    │  1. StateManager.load()  ──────────→  stale / not found            │
    │         │                                    │                     │
    │         ▼ fresh (< 48h)             ▼ fresh start                  │
    │  WarmStart.recover(saved)       WarmStart.run(symbol)               │
    │         │                              │                           │
    │         │                   ┌──────────┴──────────┐                │
    │         │                   │  fetch_history()    │                │
    │         │                   │  ~3 000 × 1H bars   │                │
    │         │                   │  (REST API, ≈10 s)  │                │
    │         │                   └──────────┬──────────┘                │
    │         │                              │                           │
    │         │                   ┌──────────┴──────────┐                │
    │         │                   │  hydrate()          │                │
    │         │                   │  compute indicators │                │
    │         │                   │  run WFO on history │                │
    │         │                   │  feed forecaster    │                │
    │         │                   │  (< 500 ms)         │                │
    │         │                   └──────────┬──────────┘                │
    │         │                              │                           │
    │         └──────────────────────────────┘                           │
    │                            │                                        │
    │                            ▼                                        │
    │             WarmStartResult (fully hydrated)                        │
    │                            │                                        │
    │         ┌──────────────────┤                                        │
    │         │                  │                                        │
    │         ▼                  ▼                                        │
    │  hydrate_candle_buf()   live_loop picks up:                        │
    │  fills MarketState       wfo, forecaster,                          │
    │  buf_1h (last 600)       strat_state,                              │
    │                          live_history (sliding arrays)             │
    └─────────────────────────────────────────────────────────────────────┘

The **LiveHistory** object is a sliding window of the last
``WFO_TRAINING_WINDOW + WFO_RETUNE_INTERVAL + 300`` 1H bars (≈ ~3 300 bars,
~4.6 months).  On each new 1H close the live loop calls
:meth:`LiveHistory.append_candle` which advances the window and recomputes
ADX / ATR in O(n) time (numpy, < 1 ms).  The WFO calls
:meth:`LiveHistory.get_arrays` to obtain the indicator arrays it needs for the
training sweep.

The math in this module is **identical** to ``backtest.py`` because both call
the same :mod:`indicators` functions on the same-shaped numpy arrays.

Usage::

    import asyncio
    from warm_start import WarmStart
    from state_manager import StateManager

    sm = StateManager()
    saved = sm.load()

    if saved:
        result = WarmStart.recover(saved, symbol=config.SYMBOL)
    else:
        result = asyncio.run(WarmStart.run(symbol=config.SYMBOL))
        sm.save(result.wfo, result.forecaster, result.strat_state,
                bars_since_last=result.bars_since_last,
                position=None, balance=initial_balance)

    # Hand off to live loop:
    _wfo         = result.wfo
    _forecaster  = result.forecaster
    _strat_state = result.strat_state
    _live_history = result.live_history
    WarmStart.hydrate_candle_buffer(state.buf_1h, result.live_history)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config_1h as config   # warm-start hydrates the 1H WFO + forecaster
import indicators as ind
from adaptive_regime import hurst_exponent
from backtest import StrategyState
from regime_forecast import MarkovRegimeForecaster
from walk_forward_optimizer import BREAKOUT_GRID, WalkForwardOptimizer

logger = logging.getLogger("warm_start")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Safety margin added to the minimum required lookback.
# Extra bars absorb any indicator NaN warm-up period.
_LOOKBACK_SAFETY = 150

# Number of 1H bars kept in LiveHistory (≈ 4.5 months).
# Must be ≥ WFO_TRAINING_WINDOW + WFO_RETUNE_INTERVAL + safety.
_LIVE_HISTORY_CAPACITY = 3_600

# Number of historical 1H bars fed into MarketState.buf_1h on startup.
# Must be ≥ MIN_CANDLES_1H so the strategy evaluator has warm indicators.
_LIVE_BUF_SEED_BARS = max(config.MAX_CANDLES, config.MIN_CANDLES_1H)


# ---------------------------------------------------------------------------
# LiveHistory — rolling indicator buffer for WFO retuning
# ---------------------------------------------------------------------------

class LiveHistory:
    """Sliding window of raw 1H OHLCV data and pre-computed ADX / ATR arrays.

    Maintained in the live loop alongside ``MarketState.buf_1h``.
    ``MarketState.buf_1h`` (max 600 bars) feeds the signal evaluator;
    ``LiveHistory`` (up to 3 600 bars) feeds the Walk-Forward Optimizer.

    Args:
        capacity: Maximum number of bars to hold before evicting the oldest.
    """

    def __init__(self, capacity: int = _LIVE_HISTORY_CAPACITY) -> None:
        self._capacity = capacity
        # Raw OHLCV stored as deques (O(1) append/pop from both ends)
        self._open:   Deque[float] = deque(maxlen=capacity)
        self._high:   Deque[float] = deque(maxlen=capacity)
        self._low:    Deque[float] = deque(maxlen=capacity)
        self._close:  Deque[float] = deque(maxlen=capacity)
        self._volume: Deque[float] = deque(maxlen=capacity)

    # ------------------------------------------------------------------
    # Population
    # ------------------------------------------------------------------

    def seed_from_df(self, df_1h: pd.DataFrame) -> None:
        """Populate the buffer from a historical 1H DataFrame.

        Replaces any existing data.  Only the last ``capacity`` bars are
        retained if the DataFrame is longer.

        Args:
            df_1h: 1H OHLCV DataFrame (columns ``open``, ``high``, ``low``,
                   ``close``, ``volume``).
        """
        tail = df_1h.tail(self._capacity)
        self._open  = deque(tail["open"].astype(float).tolist(),  maxlen=self._capacity)
        self._high  = deque(tail["high"].astype(float).tolist(),  maxlen=self._capacity)
        self._low   = deque(tail["low"].astype(float).tolist(),   maxlen=self._capacity)
        self._close = deque(tail["close"].astype(float).tolist(), maxlen=self._capacity)
        self._volume= deque(tail["volume"].astype(float).tolist(),maxlen=self._capacity)
        logger.debug("LiveHistory seeded: %d bars", len(self._close))

    def append_candle(
        self,
        open_:  float,
        high:   float,
        low:    float,
        close:  float,
        volume: float,
    ) -> None:
        """Append a newly closed 1H candle.

        The oldest bar is evicted automatically when capacity is reached.

        Args:
            open_:  Bar open price.
            high:   Bar high price.
            low:    Bar low price.
            close:  Bar close price.
            volume: Bar volume.
        """
        self._open.append(open_)
        self._high.append(high)
        self._low.append(low)
        self._close.append(close)
        self._volume.append(volume)

    # ------------------------------------------------------------------
    # Array accessors
    # ------------------------------------------------------------------

    def get_arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray,
                                  np.ndarray, np.ndarray]:
        """Return ``(open, high, low, close, volume)`` as NumPy float arrays.

        Returns:
            Five 1-D float arrays in chronological order.
        """
        return (
            np.array(self._open,   dtype=np.float64),
            np.array(self._high,   dtype=np.float64),
            np.array(self._low,    dtype=np.float64),
            np.array(self._close,  dtype=np.float64),
            np.array(self._volume, dtype=np.float64),
        )

    def get_indicator_arrays(
        self,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray,
               np.ndarray, np.ndarray]:
        """Return OHLCV + pre-computed ADX and ATR arrays.

        ADX and ATR are recomputed from the full buffer contents each call.
        With ≤ 3 600 bars this takes < 5 ms (numpy operations).

        Returns:
            Tuple ``(open, high, low, close, volume, adx, atr)`` — all
            1-D float arrays of equal length.
        """
        o1, h1, l1, c1, v1 = self.get_arrays()
        adx = ind.adx(h1, l1, c1, config.ADX_PERIOD)
        atr = ind.atr(h1, l1, c1, config.ATR_PERIOD)
        return o1, h1, l1, c1, v1, adx, atr

    @property
    def bar_count(self) -> int:
        """Number of bars currently stored."""
        return len(self._close)

    @property
    def last_close(self) -> float:
        """Most recent close price (rightmost bar)."""
        return self._close[-1] if self._close else float("nan")


# ---------------------------------------------------------------------------
# WarmStartResult — handed off to the live loop
# ---------------------------------------------------------------------------

@dataclass
class WarmStartResult:
    """Fully hydrated strategy state ready for live execution.

    Attributes:
        live_history:      Sliding 1H bar buffer pre-seeded with historical data.
        wfo:               Walk-forward optimizer with the current active BP.
        forecaster:        Markov forecaster with the rolling transition matrix.
        strat_state:       Current :class:`~backtest.StrategyState` (active_bp,
                           entry_allowed, size_scale, etc.).
        bars_since_last:   1H bars since the last completed trade (for cooldown).
        bar_counter:       Total 1H bars processed during hydration (= warm-start
                           epoch; live loop increments this on each 1H close to
                           keep WFO bar indices in sync).
        lookback_bars:     Number of 1H bars fetched from the API.
        elapsed_fetch_s:   Wall-clock seconds spent on the REST API fetch.
        elapsed_hydrate_s: Wall-clock seconds spent on the dry-run hydration.
    """

    live_history:       LiveHistory
    wfo:                Optional[WalkForwardOptimizer]
    forecaster:         Optional[MarkovRegimeForecaster]
    strat_state:        StrategyState
    bars_since_last:    int                    = 9999
    bar_counter:        int                    = 0
    lookback_bars:      int                    = 0
    elapsed_fetch_s:    float                  = 0.0
    elapsed_hydrate_s:  float                  = 0.0


# ---------------------------------------------------------------------------
# WarmStart
# ---------------------------------------------------------------------------

class WarmStart:
    """Static-method façade for the full warm-start sequence.

    All methods are ``@staticmethod`` — no instance needed.  The typical call
    sequence is::

        result = await WarmStart.run(symbol="BTCUSDT")
        # or, for crash recovery:
        result = WarmStart.recover(saved_state, symbol="BTCUSDT")
    """

    # ------------------------------------------------------------------
    # Lookback calculation
    # ------------------------------------------------------------------

    @staticmethod
    def required_lookback_bars() -> int:
        """Return the minimum 1H bars needed for a complete cold-start.

        The dominant term is the WFO requirement: the optimizer needs a full
        training window plus one retune interval before it can first fire.
        On top of that we add indicator warm-up (EMA200 = 200 bars) and a
        safety margin.

        Returns:
            Integer number of 1H bars to fetch on cold start.
        """
        # WFO: need full training window + buffer so WFO fires at least once
        wfo_bars = config.WFO_TRAINING_WINDOW + config.WFO_RETUNE_INTERVAL

        # Indicator warm-up: EMA200 + slope bars + ADX/ATR period
        indicator_bars = (
            config.EMA_TREND
            + config.EMA_TREND_SLOPE_BARS
            + max(config.ADX_PERIOD, config.ATR_PERIOD)
            + 30
        )

        # Markov forecaster: needs ≥ 2 obs to form any transition; 300 for accuracy
        forecaster_bars = 300

        # Breakout extremes: largest grid period
        breakout_bars = max(BREAKOUT_GRID) + 5

        minimum = max(wfo_bars, indicator_bars, forecaster_bars, breakout_bars)
        total   = minimum + _LOOKBACK_SAFETY
        logger.info(
            "Required lookback: %d bars (WFO=%d  indicators=%d  safety=%d)",
            total, wfo_bars, indicator_bars, _LOOKBACK_SAFETY,
        )
        return total

    # ------------------------------------------------------------------
    # Historical data fetch
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_history(
        symbol:  str,
        n_bars:  int,
        retries: int = 3,
    ) -> pd.DataFrame:
        """Fetch ``n_bars`` of closed 1H candles from the Binance Futures REST API.

        Reuses :func:`fetch_data.fetch_klines` which handles batching, rate
        limits, and local CSV cache.  Only downloads bars that are not already
        cached.

        Args:
            symbol:  Trading pair (e.g. ``"BTCUSDT"``).
            n_bars:  Minimum number of closed 1H bars required.
            retries: Number of retry attempts on transient network errors.

        Returns:
            DataFrame with columns ``open_time``, ``open``, ``high``, ``low``,
            ``close``, ``volume``, ``close_time`` (timestamps in milliseconds).

        Raises:
            RuntimeError: If fewer than ``n_bars`` bars are returned after all
                retries.
        """
        # Convert bars to days (with a +10% buffer for weekends / thin periods)
        days_needed = int(n_bars / 24 * 1.10) + 5
        logger.info(
            "Warm start: fetching %d 1H bars (~%d days) for %s …",
            n_bars, days_needed, symbol,
        )

        import fetch_data  # local import to avoid circular at module level

        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                df = await fetch_data.fetch_klines(symbol, "1h", days=days_needed)
                actual = len(df)
                if actual < n_bars:
                    logger.warning(
                        "Fetch returned %d bars (needed %d) — increasing days and retrying",
                        actual, n_bars,
                    )
                    days_needed = int(days_needed * 1.25)
                    continue
                logger.info("Fetched %d 1H bars  (%.1f days)", actual, actual / 24)
                return df.reset_index(drop=True)
            except Exception as exc:
                last_exc = exc
                wait = 2 ** attempt
                logger.warning(
                    "Fetch attempt %d/%d failed: %s  (retry in %ds)",
                    attempt, retries, exc, wait,
                )
                await asyncio.sleep(wait)

        raise RuntimeError(
            f"WarmStart.fetch_history: could not fetch {n_bars} bars for {symbol} "
            f"after {retries} attempts.  Last error: {last_exc}"
        )

    # ------------------------------------------------------------------
    # In-memory dry-run hydration
    # ------------------------------------------------------------------

    @staticmethod
    def hydrate(df_1h: pd.DataFrame) -> WarmStartResult:
        """Run a dry-run over historical 1H data to hydrate all strategy state.

        This is the **core** of the warm start.  It loops through every
        historical bar — starting from the first bar where all indicators are
        valid — and executes the same WFO and forecaster logic that the live
        loop runs on every 1H close.

        No position simulation is performed.  The goal is purely to advance
        the WFO optimizer and Markov forecaster to the same state they would
        have been in if the bot had been running continuously since the first
        bar.

        Args:
            df_1h: Historical 1H OHLCV DataFrame.  Must contain at least
                   ``required_lookback_bars()`` rows.

        Returns:
            :class:`WarmStartResult` with all strategy objects hydrated and
            ready for live execution.
        """
        t0 = time.monotonic()

        # ── Raw arrays ────────────────────────────────────────────────────────
        c1 = df_1h["close"].values.astype(np.float64)
        h1 = df_1h["high"].values.astype(np.float64)
        l1 = df_1h["low"].values.astype(np.float64)
        o1 = df_1h["open"].values.astype(np.float64)
        v1 = df_1h["volume"].values.astype(np.float64)
        n  = len(c1)

        logger.info("Hydrating strategy state over %d 1H bars …", n)

        # ── Pre-compute indicators (vectorised, sub-second for 3000 bars) ─────
        adx_arr = ind.adx(h1, l1, c1, config.ADX_PERIOD)
        atr_arr = ind.atr(h1, l1, c1, config.ATR_PERIOD)

        # ── Instantiate autonomous components ─────────────────────────────────
        wfo:        Optional[WalkForwardOptimizer]   = None
        forecaster: Optional[MarkovRegimeForecaster] = None

        if config.WFO_ENABLED:
            wfo = WalkForwardOptimizer(initial_bp=config.BREAKOUT_PERIOD)
            logger.info(
                "  WFO enabled: grid=%s  retune every %d bars  train %d bars",
                BREAKOUT_GRID, config.WFO_RETUNE_INTERVAL, config.WFO_TRAINING_WINDOW,
            )

        if config.REGIME_FORECAST_ENABLED:
            forecaster = MarkovRegimeForecaster()
            logger.info(
                "  Forecast enabled: choppy_thr=%.0f%%  trend_min=%.0f%%",
                config.FORECAST_CHOPPY_THRESHOLD * 100,
                config.FORECAST_MIN_TREND_PROB   * 100,
            )

        strat_state = StrategyState(
            active_bp          = config.BREAKOUT_PERIOD,
            effective_cooldown = config.TRADE_COOLDOWN_1H,
        )

        # ── Minimum start bar for valid indicators ────────────────────────────
        # EMA200 needs 200 bars; ADX/ATR need their respective periods.
        min_bar = max(
            config.MIN_CANDLES_1H,
            config.EMA_TREND + config.EMA_TREND_SLOPE_BARS + 10,
            config.ADX_PERIOD * 2,
            max(BREAKOUT_GRID) + 5,
        )

        n_wfo_retunings  = 0
        n_state_updates  = 0
        last_log_bar     = min_bar

        for j in range(min_bar, n):

            # ── Skip if indicators not valid yet ──────────────────────────────
            if np.isnan(adx_arr[j]) or np.isnan(atr_arr[j]):
                continue

            adx_val = float(adx_arr[j])
            atr_val = float(atr_arr[j])
            close_j = float(c1[j])

            # ── Forecaster: classify + update ─────────────────────────────────
            if forecaster is not None:
                atr_pct  = atr_val / close_j * 100 if close_j > 0 else 1.0
                hurst_h  = hurst_exponent(c1[: j + 1])
                new_state = forecaster.classify(adx_val, atr_pct, hurst_h)
                forecaster.update(new_state)
                n_state_updates += 1

                # Update strat_state from forecast
                fc = forecaster.forecast()
                strat_state.current_regime = new_state
                strat_state.trend_prob     = fc.trend_prob
                strat_state.choppy_prob    = fc.choppy_prob
                if fc.choppy_prob >= config.FORECAST_CHOPPY_THRESHOLD:
                    strat_state.entry_allowed      = False
                    strat_state.effective_cooldown = config.WFO_CHOPPY_COOLDOWN
                    strat_state.size_scale         = 0.5
                else:
                    strat_state.entry_allowed      = True
                    strat_state.effective_cooldown = config.TRADE_COOLDOWN_1H
                    strat_state.size_scale         = min(1.0, 0.5 + fc.trend_prob)

            # ── WFO: check retune ─────────────────────────────────────────────
            if wfo is not None and wfo.should_retune(j):
                params = wfo.optimize(
                    c1=c1, h1=h1, l1=l1,
                    adx_arr=adx_arr, atr_arr=atr_arr,
                    end_bar=j,
                )
                strat_state.active_bp = params.breakout_period
                n_wfo_retunings += 1

                if j - last_log_bar >= 500 or n_wfo_retunings <= 3:
                    logger.debug(
                        "  WFO retune @ bar %d: BP=%d  PF=%.2f  n=%d",
                        j, params.breakout_period,
                        params.profit_factor, params.n_trades,
                    )
                    last_log_bar = j

        elapsed = time.monotonic() - t0

        # ── Build LiveHistory from the full historical DataFrame ──────────────
        live_history = LiveHistory(capacity=_LIVE_HISTORY_CAPACITY)
        live_history.seed_from_df(df_1h)

        # ── Summary log ──────────────────────────────────────────────────────
        fc_buf = forecaster.buffer_length if forecaster else 0
        logger.info(
            "Hydration complete in %.2f s: "
            "WFO_retunings=%d  active_BP=%d  "
            "forecaster_obs=%d  current_regime=%s",
            elapsed,
            n_wfo_retunings,
            strat_state.active_bp,
            fc_buf,
            strat_state.current_regime,
        )
        if wfo and wfo.log:
            last = wfo.log[-1]
            pf_str = f"{last.pf:.2f}" if last.pf < 99 else "∞"
            logger.info(
                "  Last WFO retune: bar=%d  BP=%d  PF=%s  n=%d",
                last.bar, last.bp, pf_str, last.n,
            )

        return WarmStartResult(
            live_history      = live_history,
            wfo               = wfo,
            forecaster        = forecaster,
            strat_state       = strat_state,
            bars_since_last   = 9999,   # conservative: allows immediate entry
            bar_counter       = n - 1,  # last processed bar index
            lookback_bars     = n,
            elapsed_fetch_s   = 0.0,    # filled by run()
            elapsed_hydrate_s = elapsed,
        )

    # ------------------------------------------------------------------
    # Crash-recovery restore
    # ------------------------------------------------------------------

    @staticmethod
    async def recover(
        saved: Dict[str, Any],
        symbol: str,
    ) -> WarmStartResult:
        """Restore strategy state from a previously saved :class:`StateManager` snapshot.

        Instead of a full warm start, fetches only enough recent bars to
        refresh the ``LiveHistory`` buffer and re-validate indicator values,
        then reconstructs the WFO and forecaster from the saved JSON state.

        Args:
            saved:  Dictionary returned by :meth:`StateManager.load`.
            symbol: Trading pair (e.g. ``"BTCUSDT"``).

        Returns:
            :class:`WarmStartResult` with restored state, ready for live loop.
        """
        logger.info("Recovering from saved state (symbol=%s) …", symbol)
        t0 = time.monotonic()

        # ── Fetch enough bars to refresh LiveHistory ──────────────────────────
        # We need at least _LIVE_HISTORY_CAPACITY bars so the WFO can retune.
        n_fetch = _LIVE_HISTORY_CAPACITY + _LOOKBACK_SAFETY
        t_fetch_start = time.monotonic()
        df_1h = await WarmStart.fetch_history(symbol=symbol, n_bars=n_fetch)
        elapsed_fetch = time.monotonic() - t_fetch_start

        # ── Restore WFO ───────────────────────────────────────────────────────
        wfo: Optional[WalkForwardOptimizer] = None
        if config.WFO_ENABLED:
            wfo_d = saved.get("wfo", {})
            wfo   = WalkForwardOptimizer(
                initial_bp=wfo_d.get("active_bp", config.BREAKOUT_PERIOD)
            )
            wfo.params.breakout_period = wfo_d.get("active_bp",       config.BREAKOUT_PERIOD)
            wfo.params.profit_factor   = wfo_d.get("profit_factor",   0.0)
            wfo.params.n_trades        = wfo_d.get("n_trades",         0)
            wfo.params.updated_bar     = wfo_d.get("updated_bar",     -1)
            wfo._last_retune_bar       = wfo_d.get("last_retune_bar", -1)
            logger.info(
                "  Restored WFO: BP=%d  last_retune_bar=%d",
                wfo.params.breakout_period, wfo._last_retune_bar,
            )

        # ── Restore Markov forecaster ─────────────────────────────────────────
        forecaster: Optional[MarkovRegimeForecaster] = None
        if config.REGIME_FORECAST_ENABLED:
            fc_d = saved.get("forecaster", {})
            forecaster = MarkovRegimeForecaster(
                lookback      = fc_d.get("lookback", 300),
                laplace_alpha = fc_d.get("alpha",    1.0),
            )
            # Replay saved observations into the buffer
            for obs in fc_d.get("buffer", []):
                forecaster._buffer.append(int(obs))
            forecaster._current = fc_d.get("current_state", 0)
            logger.info(
                "  Restored forecaster: %d observations  current_state=%d",
                forecaster.buffer_length, forecaster.current_state,
            )

        # ── Restore strategy state ────────────────────────────────────────────
        ss_d        = saved.get("strat_state", {})
        strat_state = StrategyState(
            active_bp          = ss_d.get("active_bp",          config.BREAKOUT_PERIOD),
            current_regime     = ss_d.get("current_regime",     0),
            trend_prob         = ss_d.get("trend_prob",         0.33),
            choppy_prob        = ss_d.get("choppy_prob",        0.33),
            entry_allowed      = ss_d.get("entry_allowed",      True),
            size_scale         = ss_d.get("size_scale",         1.0),
            effective_cooldown = ss_d.get("effective_cooldown", config.TRADE_COOLDOWN_1H),
        )

        # ── Refresh LiveHistory from newly fetched data ───────────────────────
        live_history = LiveHistory(capacity=_LIVE_HISTORY_CAPACITY)
        live_history.seed_from_df(df_1h)

        elapsed_total = time.monotonic() - t0
        logger.info(
            "Recovery complete in %.2f s  BP=%d  forecaster_obs=%d",
            elapsed_total,
            strat_state.active_bp,
            forecaster.buffer_length if forecaster else 0,
        )

        return WarmStartResult(
            live_history       = live_history,
            wfo                = wfo,
            forecaster         = forecaster,
            strat_state        = strat_state,
            bars_since_last    = saved.get("bars_since_last", 9999),
            bar_counter        = saved.get("bar_counter",     0),
            lookback_bars      = len(df_1h),
            elapsed_fetch_s    = elapsed_fetch,
            elapsed_hydrate_s  = 0.0,
        )

    # ------------------------------------------------------------------
    # Full cold-start entry point
    # ------------------------------------------------------------------

    @staticmethod
    async def run(symbol: str = "BTCUSDT") -> WarmStartResult:
        """Fetch historical data and run the full dry-run hydration.

        This is the primary entry point for a cold start (no saved state, or
        stale saved state).  Combines :meth:`fetch_history` + :meth:`hydrate`
        into a single awaitable call.

        Args:
            symbol: Trading pair (default ``"BTCUSDT"``).

        Returns:
            Fully hydrated :class:`WarmStartResult`.
        """
        n_bars = WarmStart.required_lookback_bars()

        # ── Fetch ─────────────────────────────────────────────────────────────
        t_fetch = time.monotonic()
        df_1h   = await WarmStart.fetch_history(symbol=symbol, n_bars=n_bars)
        elapsed_fetch = time.monotonic() - t_fetch

        # ── Hydrate ───────────────────────────────────────────────────────────
        result = WarmStart.hydrate(df_1h)
        result.elapsed_fetch_s = elapsed_fetch

        logger.info(
            "Warm start finished: fetch=%.1f s  hydrate=%.2f s  "
            "bars=%d  active_BP=%d",
            elapsed_fetch,
            result.elapsed_hydrate_s,
            result.lookback_bars,
            result.strat_state.active_bp,
        )
        return result

    # ------------------------------------------------------------------
    # Live candle buffer handover
    # ------------------------------------------------------------------

    @staticmethod
    def hydrate_candle_buffer(buf, live_history: LiveHistory) -> int:
        """Replay the last N historical bars into a live ``CandleBuffer``.

        Feeds the final ``_LIVE_BUF_SEED_BARS`` bars from ``live_history``
        directly into the ``CandleBuffer`` (``data_store.CandleBuffer``),
        simulating as if the bot had been receiving those candles live.  This
        ensures ``evaluate_1h_live()`` has warm indicators (EMA, RSI, ADX) on
        the very first live candle.

        Args:
            buf:          :class:`~data_store.CandleBuffer` instance from the
                          live ``MarketState`` (``state.buf_1h``).
            live_history: Populated :class:`LiveHistory`.

        Returns:
            Number of bars replayed into ``buf``.
        """
        o1, h1, l1, c1, v1 = live_history.get_arrays()
        n   = len(c1)
        start = max(0, n - _LIVE_BUF_SEED_BARS)
        replayed = 0

        for k in range(start, n):
            # Simulate a closed-candle Binance kline payload
            fake_close_time = int(k * 3_600_000)  # synthetic ms timestamp
            kline = {
                "t": int((k - 1) * 3_600_000),  # open_time
                "o": float(o1[k]),
                "h": float(h1[k]),
                "l": float(l1[k]),
                "c": float(c1[k]),
                "v": float(v1[k]),
                "x": True,  # candle is closed
            }
            buf.update(kline)
            replayed += 1

        logger.info(
            "CandleBuffer hydrated: %d bars replayed  (buf.count=%d)",
            replayed, buf.count,
        )
        return replayed

    # ------------------------------------------------------------------
    # Utility: print warm-start summary to console
    # ------------------------------------------------------------------

    @staticmethod
    def print_summary(result: WarmStartResult) -> None:
        """Print a one-page warm-start summary to stdout.

        Args:
            result: Completed :class:`WarmStartResult`.
        """
        ss  = result.strat_state
        wfo = result.wfo
        fc  = result.forecaster

        print()
        print("=" * 64)
        print("  WARM START COMPLETE")
        print("=" * 64)
        print(f"  Bars fetched   : {result.lookback_bars:,}  "
              f"({result.lookback_bars / 24:.1f} days)")
        print(f"  Fetch time     : {result.elapsed_fetch_s:.1f} s")
        print(f"  Hydration time : {result.elapsed_hydrate_s:.2f} s")
        print("─" * 64)

        # WFO
        if wfo:
            print(f"  WFO active BP  : {ss.active_bp} bars "
                  f"(last retune bar={wfo._last_retune_bar})")
            if wfo.log:
                last = wfo.log[-1]
                pf_s = f"{last.pf:.2f}" if last.pf < 99 else "∞"
                print(f"  Last retune    : bar {last.bar}  "
                      f"PF={pf_s}  n={last.n}")
                print(f"  Total retunings: {len(wfo.log)}")
        else:
            print(f"  Breakout period: {ss.active_bp} bars (WFO off)")

        # Forecaster
        if fc:
            fc_result = fc.forecast()
            print(f"  Regime forecast: TREND={fc_result.trend_prob:.0%}  "
                  f"CHOPPY={fc_result.choppy_prob:.0%}  "
                  f"QUIET={fc_result.quiet_prob:.0%}")
            print(f"  Current regime : {fc.current_state_name}  "
                  f"confidence={fc_result.confidence:.0%}")
            print(f"  Forecaster obs : {fc.buffer_length}")
        else:
            print("  Regime forecast: disabled")

        # Entry gate
        gate_str = "OPEN  ✓" if ss.entry_allowed else "BLOCKED  ✗ (choppy forecast)"
        print(f"  Entry gate     : {gate_str}")
        print(f"  Size scale     : {ss.size_scale:.0%}")
        print(f"  Bars since last: {result.bars_since_last}")
        print("=" * 64)
        print()
