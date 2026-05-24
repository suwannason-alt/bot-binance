"""
Rolling OHLCV buffers for each timeframe.

Candles are updated in-place while the current bar is open, then appended to
the buffer when the bar closes.  ``MarketState`` aggregates all live market
data (mark price, funding rate, aggregated trades) alongside the two
time-frame buffers.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple

import numpy as np

import config


@dataclass
class Candle:
    """A single OHLCV candle with a close flag."""

    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool


class CandleBuffer:
    """Fixed-size ring buffer of closed ``Candle`` objects.

    While a candle is still open its high, low, close, and volume are updated
    in-place on every tick.  When the candle closes it is committed to the
    buffer and a new candle starts.

    Args:
        max_size: Maximum number of candles to keep in memory (FIFO eviction).
    """

    def __init__(self, max_size: int = config.MAX_CANDLES) -> None:
        self._buf: deque[Candle] = deque(maxlen=max_size)
        self._current: Optional[Candle] = None

    def update(self, raw: dict) -> bool:
        """Process a raw Binance kline payload and update the buffer.

        Args:
            raw: Kline dict from the Binance WebSocket stream (keys ``t``, ``o``,
                 ``h``, ``l``, ``c``, ``v``, ``x``).

        Returns:
            ``True`` when a candle closes (triggers strategy evaluation),
            ``False`` otherwise.
        """
        candle = Candle(
            open_time=raw["t"],
            open=float(raw["o"]),
            high=float(raw["h"]),
            low=float(raw["l"]),
            close=float(raw["c"]),
            volume=float(raw["v"]),
            closed=raw["x"],
        )

        if self._current is None or candle.open_time != self._current.open_time:
            # New bar starting — commit the previous one if it was closed
            if self._current is not None and self._current.closed:
                self._buf.append(self._current)
            self._current = candle
        else:
            # Update the running candle (high/low may expand, close/volume change)
            self._current.high = max(self._current.high, candle.high)
            self._current.low = min(self._current.low, candle.low)
            self._current.close = candle.close
            self._current.volume = candle.volume
            self._current.closed = candle.closed

        if candle.closed:
            self._buf.append(self._current)
            self._current = None
            return True

        return False

    @property
    def count(self) -> int:
        """Number of closed candles currently held in the buffer."""
        return len(self._buf)

    def arrays(self) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Return buffer contents as five NumPy arrays: (opens, highs, lows, closes, volumes).

        Returns:
            Tuple of 1-D float arrays in chronological order.
        """
        candles = list(self._buf)
        return (
            np.array([c.open   for c in candles]),
            np.array([c.high   for c in candles]),
            np.array([c.low    for c in candles]),
            np.array([c.close  for c in candles]),
            np.array([c.volume for c in candles]),
        )


@dataclass
class MarketState:
    """Aggregated live market data for a single symbol.

    Combines real-time price/funding information with the two rolling candle
    buffers used by the strategy evaluator.

    Attributes:
        mark_price:       Latest mark price (from Binance markPrice stream).
        index_price:      Latest index price.
        funding_rate:     Current funding rate (updated every tick).
        last_trade_price: Price of the most recent aggregated trade.
        last_trade_qty:   Quantity of the most recent aggregated trade.
        last_trade_side:  ``"BUY"`` or ``"SELL"`` from the aggTrade stream.
        buf_5m:           5-minute candle buffer.
        buf_1h:           1-hour candle buffer.
    """

    mark_price: float = 0.0
    index_price: float = 0.0
    funding_rate: float = 0.0
    last_trade_price: float = 0.0
    last_trade_qty: float = 0.0
    last_trade_side: str = ""  # "BUY" | "SELL"

    buf_5m: CandleBuffer = field(default_factory=CandleBuffer)
    buf_1h: CandleBuffer = field(default_factory=CandleBuffer)

    def update_mark_price(self, data: dict) -> None:
        """Update mark price, index price, and funding rate from a markPrice payload.

        Args:
            data: Parsed Binance markPrice stream event (keys ``p``, ``i``, ``r``).
        """
        self.mark_price = float(data.get("p", 0))
        self.index_price = float(data.get("i", 0))
        self.funding_rate = float(data.get("r", 0))

    def update_agg_trade(self, data: dict) -> None:
        """Update last trade price, quantity, and side from an aggTrade payload.

        Args:
            data: Parsed Binance aggTrade stream event (keys ``p``, ``q``, ``m``).
                  ``m=True`` means the buyer is the market maker (i.e., a sell order hit the book).
        """
        self.last_trade_price = float(data.get("p", 0))
        self.last_trade_qty = float(data.get("q", 0))
        self.last_trade_side = "SELL" if data.get("m") else "BUY"
