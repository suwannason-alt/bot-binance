"""
Rolling OHLCV buffers for each timeframe.
Candles are updated in-place while the current bar is open,
then a new row is appended when the bar closes.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import config


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool


class CandleBuffer:
    def __init__(self, max_size: int = config.MAX_CANDLES):
        self._buf: deque[Candle] = deque(maxlen=max_size)
        self._current: Optional[Candle] = None

    def update(self, raw: dict) -> bool:
        """
        Process a kline payload dict.
        Returns True when a candle closes (triggers strategy evaluation).
        """
        k = raw
        c = Candle(
            open_time=k["t"],
            open=float(k["o"]),
            high=float(k["h"]),
            low=float(k["l"]),
            close=float(k["c"]),
            volume=float(k["v"]),
            closed=k["x"],
        )
        if self._current is None or c.open_time != self._current.open_time:
            if self._current is not None and self._current.closed:
                self._buf.append(self._current)
            self._current = c
        else:
            # update running candle
            self._current.high = max(self._current.high, c.high)
            self._current.low = min(self._current.low, c.low)
            self._current.close = c.close
            self._current.volume = c.volume
            self._current.closed = c.closed

        if c.closed:
            self._buf.append(self._current)
            self._current = None
            return True
        return False

    @property
    def count(self) -> int:
        return len(self._buf)

    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        candles = list(self._buf)
        opens = np.array([c.open for c in candles])
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        closes = np.array([c.close for c in candles])
        volumes = np.array([c.volume for c in candles])
        return opens, highs, lows, closes, volumes


@dataclass
class MarketState:
    mark_price: float = 0.0
    index_price: float = 0.0
    funding_rate: float = 0.0
    last_trade_price: float = 0.0
    last_trade_qty: float = 0.0
    last_trade_side: str = ""   # "BUY" | "SELL"

    buf_5m: CandleBuffer = field(default_factory=CandleBuffer)
    buf_1h: CandleBuffer = field(default_factory=CandleBuffer)

    def update_mark_price(self, data: dict):
        self.mark_price = float(data.get("p", 0))
        self.index_price = float(data.get("i", 0))
        self.funding_rate = float(data.get("r", 0))

    def update_agg_trade(self, data: dict):
        self.last_trade_price = float(data.get("p", 0))
        self.last_trade_qty = float(data.get("q", 0))
        self.last_trade_side = "SELL" if data.get("m") else "BUY"
