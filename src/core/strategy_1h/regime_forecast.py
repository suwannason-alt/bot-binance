"""First-order Markov regime forecaster.

Classifies each 1H bar into one of three market states and maintains a rolling
first-order Markov transition matrix.  The :meth:`forecast` method returns the
probability distribution over the *next* bar's state given the current state.

States
------
``TREND  = 0``  ADX ≥ 25 or (Hurst ≥ 0.55 and ADX ≥ 20) — directional move.
``CHOPPY = 1``  High volatility with no directional bias — false-breakout zone.
``QUIET  = 2``  Compressed volatility (ATR% < 0.15) — pre-breakout consolidation.

Transition matrix update
------------------------
A rolling circular buffer of the last ``lookback`` observed states is kept.
Counts of each (from_state, to_state) pair are computed from consecutive
observations.  Laplace smoothing (``alpha`` pseudo-counts per cell) prevents
zero-probability rows even before enough data has accumulated.

Usage example::

    from regime_forecast import MarkovRegimeForecaster

    fc = MarkovRegimeForecaster(lookback=300)
    # Per 1H bar:
    state = fc.classify(adx=30.0, atr_pct=1.2, hurst=0.61)
    fc.update(state)
    forecast = fc.forecast()
    if forecast.choppy_prob > 0.65:
        # suppress entry
        pass
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List

import numpy as np


# ---------------------------------------------------------------------------
# State constants
# ---------------------------------------------------------------------------

TREND:  int = 0
CHOPPY: int = 1
QUIET:  int = 2
N_STATES: int = 3

_STATE_NAMES = {TREND: "TREND", CHOPPY: "CHOPPY", QUIET: "QUIET"}


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

@dataclass
class RegimeForecast:
    """Next-state probability distribution from the Markov forecaster.

    Attributes:
        trend_prob:     Probability of the next bar being in TREND state.
        choppy_prob:    Probability of the next bar being in CHOPPY state.
        quiet_prob:     Probability of the next bar being in QUIET state.
        dominant_state: Integer state with the highest forecast probability.
        confidence:     Probability of ``dominant_state`` (higher → more certain).
        current_state:  The most recently classified observed state.
    """

    trend_prob:     float
    choppy_prob:    float
    quiet_prob:     float
    dominant_state: int
    confidence:     float
    current_state:  int

    @property
    def dominant_name(self) -> str:
        """Human-readable name of the dominant forecasted state."""
        return _STATE_NAMES.get(self.dominant_state, "UNKNOWN")

    @property
    def current_name(self) -> str:
        """Human-readable name of the current observed state."""
        return _STATE_NAMES.get(self.current_state, "UNKNOWN")


# ---------------------------------------------------------------------------
# Forecaster
# ---------------------------------------------------------------------------

class MarkovRegimeForecaster:
    """First-order Markov chain forecaster for market regime states.

    Maintains a rolling window of observed states and derives the empirical
    1-step transition probabilities using Laplace-smoothed count matrices.

    Args:
        lookback:      Number of recent bars to include in the transition matrix.
                       Older observations are discarded from the rolling buffer.
                       Default 300 (≈ 12 days of 1H bars).
        laplace_alpha: Pseudo-counts added to each cell before normalisation.
                       ``1.0`` (default) ensures no probability is ever zero.
    """

    TREND  = TREND
    CHOPPY = CHOPPY
    QUIET  = QUIET

    def __init__(self, lookback: int = 300, laplace_alpha: float = 1.0) -> None:
        self._lookback:  int   = lookback
        self._alpha:     float = laplace_alpha
        self._buffer:    Deque[int] = deque(maxlen=lookback)
        self._current:   int   = TREND   # fallback before first observation

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    @staticmethod
    def classify(adx: float, atr_pct: float, hurst: float) -> int:
        """Classify a single bar into one of the three regime states.

        Decision rules (evaluated in order — first match wins):

        1. ``QUIET``  — ``atr_pct < 0.15`` (near-flat market, very low volatility).
        2. ``TREND``  — ``adx ≥ 25`` or ``(hurst ≥ 0.55 and adx ≥ 20)``.
                        The Hurst path lets early-trend bars through when ADX
                        hasn't fully built up yet (ADX lags by design).
        3. ``CHOPPY`` — all remaining bars (volatile but not directional).

        Args:
            adx:     Current ADX value (directionality strength indicator).
            atr_pct: ATR expressed as % of close price (normalised volatility).
            hurst:   Hurst exponent of recent close prices ∈ [0, 1].
                     H > 0.55 → persistence; H < 0.45 → mean-reversion.

        Returns:
            One of :data:`TREND` (0), :data:`CHOPPY` (1), :data:`QUIET` (2).
        """
        if atr_pct < 0.15:
            return QUIET
        if adx >= 25.0 or (hurst >= 0.55 and adx >= 20.0):
            return TREND
        return CHOPPY

    # ------------------------------------------------------------------
    # State update
    # ------------------------------------------------------------------

    def update(self, new_state: int) -> None:
        """Record a newly observed regime state and advance the forecaster.

        Appends ``new_state`` to the rolling buffer.  The oldest observation
        is automatically evicted when the buffer reaches ``lookback`` entries.

        Args:
            new_state: Observed state for the current bar — one of
                       :data:`TREND`, :data:`CHOPPY`, :data:`QUIET`.
        """
        self._buffer.append(new_state)
        self._current = new_state

    # ------------------------------------------------------------------
    # Forecast
    # ------------------------------------------------------------------

    def forecast(self) -> RegimeForecast:
        """Compute next-state probabilities from the current state.

        Builds a 3×3 Laplace-smoothed transition count matrix from the rolling
        buffer, normalises each row to sum to 1.0, and returns the row
        corresponding to :attr:`_current`.

        When the buffer has fewer than two observations the uniform prior is
        returned (each state probability = 1/3, confidence = 0.33).

        Returns:
            :class:`RegimeForecast` with transition probabilities, dominant
            state, and confidence score for the next bar.
        """
        buf: List[int] = list(self._buffer)

        if len(buf) < 2:
            # Insufficient history — return uninformative prior
            uniform = 1.0 / N_STATES
            return RegimeForecast(
                trend_prob=uniform,
                choppy_prob=uniform,
                quiet_prob=uniform,
                dominant_state=self._current,
                confidence=uniform,
                current_state=self._current,
            )

        # ── Build smoothed transition count matrix ────────────────────────
        trans = np.full((N_STATES, N_STATES), self._alpha, dtype=np.float64)
        for k in range(len(buf) - 1):
            from_s = buf[k]
            to_s   = buf[k + 1]
            trans[from_s, to_s] += 1.0

        # ── Row-normalise to obtain probabilities ─────────────────────────
        row_sums = trans.sum(axis=1, keepdims=True)
        probs = trans / row_sums   # shape (N_STATES, N_STATES)

        # ── Extract forecast row for current state ────────────────────────
        row = probs[self._current]
        dominant = int(np.argmax(row))

        return RegimeForecast(
            trend_prob=float(row[TREND]),
            choppy_prob=float(row[CHOPPY]),
            quiet_prob=float(row[QUIET]),
            dominant_state=dominant,
            confidence=float(row[dominant]),
            current_state=self._current,
        )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def transition_matrix(self) -> np.ndarray:
        """Return the full 3×3 Laplace-smoothed transition probability matrix.

        Row ``i`` is the probability distribution over next states given that
        the current state is ``i``.

        Returns:
            Float array of shape ``(3, 3)``; rows sum to 1.0.
        """
        buf: List[int] = list(self._buffer)
        trans = np.full((N_STATES, N_STATES), self._alpha, dtype=np.float64)
        for k in range(len(buf) - 1):
            trans[buf[k], buf[k + 1]] += 1.0
        row_sums = trans.sum(axis=1, keepdims=True)
        return trans / row_sums

    @property
    def buffer_length(self) -> int:
        """Number of observations currently stored in the rolling buffer."""
        return len(self._buffer)

    @property
    def current_state(self) -> int:
        """Most recently observed state integer."""
        return self._current

    @property
    def current_state_name(self) -> str:
        """Human-readable name of the most recently observed state."""
        return _STATE_NAMES.get(self._current, "UNKNOWN")
