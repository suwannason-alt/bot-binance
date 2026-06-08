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
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional

import websockets

import config
from data_store import MarketState

logger = logging.getLogger("ws_client")

# Seconds to wait before reconnecting after a connection error
_RECONNECT_DELAY: int = 5

# Type alias for async callbacks
_AsyncCallback = Callable[..., Coroutine[Any, Any, None]]


@dataclass
class Route:
    """Per-symbol destination for fanned-out stream frames.

    Bundles the symbol's own :class:`MarketState` and its three close/tick callbacks
    so one shared socket can drive many independent asset processors.
    """
    state: MarketState
    on_5m_close: Optional[_AsyncCallback] = None
    on_1h_close: Optional[_AsyncCallback] = None
    on_tick: Optional[_AsyncCallback] = None


def build_stream_url(symbols: List[str]) -> str:
    """Build the Binance combined-stream URL for every symbol's 4 streams.

    Mirrors the single-symbol ``config.WS_URL`` layout (kline_5m / kline_1h /
    markPrice / aggTrade) and concatenates the streams for all ``symbols`` so one
    connection feeds the whole multi-asset book.  The 5M kline is streamed/cached
    for intra-hour granularity; live SL/TP trailing itself runs per markPrice tick.
    """
    parts: List[str] = []
    for sym in symbols:
        s = sym.lower()
        parts += [f"{s}@kline_5m", f"{s}@kline_1h", f"{s}@markPrice", f"{s}@aggTrade"]
    return "wss://fstream.binance.com/market/stream?streams=" + "/".join(parts)


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
        state: Optional[MarketState] = None,
        on_5m_close: Optional[_AsyncCallback] = None,
        on_1h_close: Optional[_AsyncCallback] = None,
        on_tick: Optional[_AsyncCallback] = None,
        *,
        routes: Optional[Dict[str, Route]] = None,
        ws_url: Optional[str] = None,
    ) -> None:
        # Two construction modes:
        #   • Legacy single-symbol: pass state + callbacks → one implicit route keyed
        #     by config.SYMBOL, connecting to config.WS_URL.  Behaviour is unchanged.
        #   • Multi-asset: pass ``routes={SYMBOL: Route(...)}`` → frames are dispatched
        #     by the symbol parsed from each stream name; URL covers all routed symbols.
        if routes is not None:
            self._routes: Dict[str, Route] = {s.upper(): r for s, r in routes.items()}
            self.ws_url = ws_url or build_stream_url(list(self._routes))
        else:
            self._routes = {config.SYMBOL.upper(): Route(state, on_5m_close, on_1h_close, on_tick)}
            self.ws_url = ws_url or config.WS_URL
        # Back-compat attributes for the single-symbol callers that read them.
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
        logger.info(f"Connecting to {self.ws_url}")
        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
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

        # Route by the symbol prefix of the stream name (e.g. "ethusdt@kline_1h" →
        # ETHUSDT).  Always present on data streams; control frames have no "@".
        symbol = stream.split("@", 1)[0].upper() if "@" in stream else ""
        route = self._routes.get(symbol)

        if route is not None and "@kline_5m" in stream:
            # Always cache the fine-grained 5M data into buf_5m (streaming/caching
            # alongside 1H), regardless of whether a close-callback is registered.
            # NOTE: live SL/TP trailing does NOT depend on this — it runs per
            # markPrice tick (see the @markPrice branch).  The 5M cache is available
            # for intra-hour analysis and any future 5M consumer.
            kline = data.get("k", {})
            closed = route.state.buf_5m.update(kline)
            if closed and route.on_5m_close is not None:
                logger.debug(
                    f"[{symbol}] 5M candle closed  close={kline.get('c')}  "
                    f"buf_5m={route.state.buf_5m.count}"
                )
                await route.on_5m_close(route.state)

        elif route is not None and "@kline_1h" in stream:
            kline = data.get("k", {})
            if route.state.buf_1h.update(kline) and route.on_1h_close:
                logger.debug(f"[{symbol}] 1H candle closed  close={kline['c']}")
                await route.on_1h_close(route.state)

        elif route is not None and "@markPrice" in stream:
            route.state.update_mark_price(data)
            if route.on_tick:
                await route.on_tick(route.state, route.state.mark_price)

        elif route is not None and "@aggTrade" in stream:
            route.state.update_agg_trade(data)

        else:
            # No matching stream branch. This catches Binance control frames
            # (subscription acks `{"result":null,"id":..}`, `{"error":..}`) and
            # any unexpected stream name — all of which were previously dropped
            # silently. Surfacing them is the difference between "messages arrive
            # but never dispatch" and "no messages arrive at all".
            logger.warning("WS frame not dispatched — stream=%r  payload=%.160s",
                           stream or "<none>", raw_message)
