"""
Binance Futures combined WebSocket client.

Subscribes to four streams for a single symbol:

    ``<symbol>@kline_5m``   – 5-minute candles
    ``<symbol>@kline_1h``   – 1-hour candles
    ``<symbol>@markPrice``  – mark price + funding rate
    ``<symbol>@aggTrade``   – aggregated trades

Callbacks are invoked on the asyncio event loop and must be coroutines.
The client reconnects automatically after any connection error.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Coroutine, Optional

import websockets

import config
from data_store import MarketState

logger = logging.getLogger("ws_client")

# Seconds to wait before reconnecting after a connection error
_RECONNECT_DELAY: int = 5

# WebSocket keep-alive / shutdown timeouts (seconds).  Binance closes idle
# sockets aggressively, so a 20 s ping cadence keeps the connection proven-live;
# close_timeout bounds how long a graceful close may block on reconnect.
_WS_PING_INTERVAL: int = 20
_WS_PING_TIMEOUT: int = 20
_WS_CLOSE_TIMEOUT: int = 10

# Type alias for async callbacks
_AsyncCallback = Callable[..., Coroutine[Any, Any, None]]


class BinanceWS:
    """Binance Futures combined-stream WebSocket client.

    Manages a single persistent connection to the Binance combined stream
    endpoint and dispatches candle / tick events to caller-supplied coroutines.

    Args:
        state:        Shared ``MarketState`` instance updated on every message.
        on_5m_close:  Coroutine called each time a 5-minute candle closes.
        on_1h_close:  Coroutine called each time a 1-hour candle closes (optional).
        on_tick:      Coroutine called on every mark-price update (optional).
                      Receives ``(state, mark_price)`` as arguments.
    """

    def __init__(
        self,
        state: MarketState,
        on_5m_close: _AsyncCallback,
        on_1h_close: Optional[_AsyncCallback] = None,
        on_tick: Optional[_AsyncCallback] = None,
    ) -> None:
        self.state = state
        self.on_5m_close = on_5m_close
        self.on_1h_close = on_1h_close
        self.on_tick = on_tick
        self._running = False
        # ── Stream-flow instrumentation ──────────────────────────────────────
        # Counts every frame received on the socket so we can prove whether the
        # `async for` loop is actually delivering data (vs. a connected-but-silent
        # socket).  Logged once on the first frame, then throttled thereafter.
        self._msg_count = 0

    async def run(self) -> None:
        """Start the WebSocket event loop, reconnecting on any error.

        Runs indefinitely until ``stop()`` is called or the task is cancelled.
        """
        self._running = True
        while self._running:
            try:
                await self._connect()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(
                    f"WebSocket error: {exc}  — reconnecting in {_RECONNECT_DELAY}s"
                )
                await asyncio.sleep(_RECONNECT_DELAY)

    def stop(self) -> None:
        """Signal the run loop to stop after the current message is processed."""
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _connect(self) -> None:
        """Open a WebSocket connection and process messages until it closes."""
        logger.info(f"Connecting to {config.WS_URL}")
        async with websockets.connect(
            config.WS_URL,
            ping_interval=_WS_PING_INTERVAL,
            ping_timeout=_WS_PING_TIMEOUT,
            close_timeout=_WS_CLOSE_TIMEOUT,
        ) as ws:
            logger.info("WebSocket connected")
            async for raw_message in ws:
                await self._handle(raw_message)

    async def _handle(self, raw_message: str) -> None:
        """Parse a raw WebSocket message and dispatch to the appropriate handler.

        Args:
            raw_message: JSON-encoded message string from the WebSocket stream.
        """
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            logger.warning("WS frame dropped — not valid JSON: %.120s", raw_message)
            return

        # ── Stream-flow proof ─────────────────────────────────────────────────
        # The first frame proves the socket is delivering data (not just
        # connected). After that, log a throttled heartbeat so a healthy stream
        # is visibly alive without flooding the log.
        self._msg_count += 1
        if self._msg_count == 1:
            logger.info("WS stream live — first frame received (stream=%s)",
                        msg.get("stream", "<none>"))
        elif self._msg_count % 1000 == 0:
            logger.info("WS stream flowing — %d frames received", self._msg_count)

        stream: str = msg.get("stream", "")
        data: dict = msg.get("data", {})

        if "@kline_5m" in stream:
            kline = data.get("k", {})
            if self.state.buf_5m.update(kline):
                logger.debug(
                    f"5M candle closed  close={kline['c']}  "
                    f"buf_5m={self.state.buf_5m.count}"
                )
                await self.on_5m_close(self.state)

        elif "@kline_1h" in stream:
            kline = data.get("k", {})
            if self.state.buf_1h.update(kline) and self.on_1h_close:
                logger.debug(f"1H candle closed  close={kline['c']}")
                await self.on_1h_close(self.state)

        elif "@markPrice" in stream:
            self.state.update_mark_price(data)
            if self.on_tick:
                await self.on_tick(self.state, self.state.mark_price)

        elif "@aggTrade" in stream:
            self.state.update_agg_trade(data)

        else:
            # No matching stream branch. This catches Binance control frames
            # (subscription acks `{"result":null,"id":..}`, `{"error":..}`) and
            # any unexpected stream name — all of which were previously dropped
            # silently. Surfacing them is the difference between "messages arrive
            # but never dispatch" and "no messages arrive at all".
            logger.warning("WS frame not dispatched — stream=%r  payload=%.160s",
                           stream or "<none>", raw_message)
