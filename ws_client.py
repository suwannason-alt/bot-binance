"""
Binance Futures combined WebSocket client.
Streams:
  btcusdt@kline_5m   – 5-minute candles
  btcusdt@kline_1h   – 1-hour candles
  btcusdt@markPrice  – mark price + funding rate
  btcusdt@aggTrade   – aggregated trades
"""
import asyncio
import json
import logging
from typing import Callable, Coroutine, Any

import websockets

import config
from data_store import MarketState

logger = logging.getLogger("ws_client")

RECONNECT_DELAY = 5   # seconds before reconnecting after error


class BinanceWS:
    def __init__(
        self,
        state: MarketState,
        on_5m_close: Callable[[MarketState], Coroutine[Any, Any, None]],
        on_1h_close: Callable[[MarketState], Coroutine[Any, Any, None]] | None = None,
        on_tick: Callable[[MarketState, float], Coroutine[Any, Any, None]] | None = None,
    ):
        self.state = state
        self.on_5m_close = on_5m_close
        self.on_1h_close = on_1h_close
        self.on_tick = on_tick
        self._running = False

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"WebSocket error: {e}  — reconnecting in {RECONNECT_DELAY}s")
                await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self):
        logger.info(f"Connecting to {config.WS_URL}")
        async with websockets.connect(
            config.WS_URL,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
        ) as ws:
            logger.info("WebSocket connected")
            async for raw in ws:
                await self._handle(raw)

    async def _handle(self, raw: str):
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        stream = msg.get("stream", "")
        data = msg.get("data", {})

        if "@kline_5m" in stream:
            kline = data.get("k", {})
            closed = self.state.buf_5m.update(kline)
            if closed:
                logger.debug(
                    f"5M candle closed  close={kline['c']}  "
                    f"buf_5m={self.state.buf_5m.count}"
                )
                await self.on_5m_close(self.state)

        elif "@kline_1h" in stream:
            kline = data.get("k", {})
            closed = self.state.buf_1h.update(kline)
            if closed and self.on_1h_close:
                logger.debug(f"1H candle closed  close={kline['c']}")
                await self.on_1h_close(self.state)

        elif "@markPrice" in stream:
            self.state.update_mark_price(data)
            if self.on_tick:
                await self.on_tick(self.state, self.state.mark_price)

        elif "@aggTrade" in stream:
            self.state.update_agg_trade(data)

    def stop(self):
        self._running = False
