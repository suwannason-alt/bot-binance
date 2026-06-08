"""Multi-symbol WebSocket fan-out routing test (no pytest — standalone runner).

Exercises the integration seam the multi-asset system hinges on: a raw frame arriving
on the ONE shared socket must be parsed for its symbol and dispatched to the matching
:class:`ws_client.Route` — not to another symbol's processor.  This is pure,
deterministic, network-free code (no exchange, no live loop), so it's directly testable.

Covers: symbol parsing from the stream name, route lookup, the 1H/5M/tick branches, 5M
caching into buf_5m on 1H-only routes (no close-callback), and ``build_stream_url``.

Run:  python tests/test_ws_fanout.py     (exits non-zero on any mismatch)
"""
from __future__ import annotations

# ── Path bootstrap (modular layout — keep flat imports resolvable) ─────────────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import asyncio
import json

from data_store import MarketState
from ws_client import BinanceWS, Route, build_stream_url

# A closed 1H kline payload (Binance shape: x=True means the candle has closed).
def _kline_frame(symbol: str) -> str:
    s = symbol.lower()
    return json.dumps({
        "stream": f"{s}@kline_1h",
        "data": {"k": {"t": 0, "T": 1, "s": symbol, "o": "1", "h": "2", "l": "0.5",
                       "c": "1.5", "v": "10", "x": True}},
    })

def _mark_frame(symbol: str) -> str:
    s = symbol.lower()
    return json.dumps({"stream": f"{s}@markPrice", "data": {"s": symbol, "p": "1.23"}})


def main() -> int:
    print("Multi-symbol WS fan-out routing")
    fired_1h: list = []
    fired_tick: list = []

    async def _mk_1h(sym):
        async def _cb(state):
            fired_1h.append(sym)
        return _cb

    async def _mk_tick(sym):
        async def _cb(state, price):
            fired_tick.append(sym)
        return _cb

    async def _run() -> None:
        routes = {
            "BTCUSDT": Route(state=MarketState(),
                             on_1h_close=await _mk_1h("BTCUSDT"), on_tick=await _mk_tick("BTCUSDT")),
            "ETHUSDT": Route(state=MarketState(),
                             on_1h_close=await _mk_1h("ETHUSDT"), on_tick=await _mk_tick("ETHUSDT")),
            "SOLUSDT": Route(state=MarketState(),
                             on_1h_close=await _mk_1h("SOLUSDT"), on_tick=await _mk_tick("SOLUSDT")),
        }
        ws = BinanceWS(routes=routes)

        # URL covers all three symbols' 4 streams each (12 total).
        url = build_stream_url(list(routes))
        for sym in ("btcusdt", "ethusdt", "solusdt"):
            assert f"{sym}@kline_1h" in url, f"{sym} 1H stream missing from URL"
        assert url == ws.ws_url, "BinanceWS should build the same combined URL from routes"
        print(f"  ✓ build_stream_url covers all 3 symbols ({url.count('@')} streams)")

        # A 1H frame for ETH must wake ONLY the ETH processor.
        await ws._handle(_kline_frame("ETHUSDT"))
        assert fired_1h == ["ETHUSDT"], f"ETH 1H frame mis-routed: {fired_1h}"
        await ws._handle(_kline_frame("SOLUSDT"))
        await ws._handle(_kline_frame("BTCUSDT"))
        assert fired_1h == ["ETHUSDT", "SOLUSDT", "BTCUSDT"], f"1H routing wrong: {fired_1h}"
        print(f"  ✓ 1H frames routed to the correct processor each time ({fired_1h})")

        # Mark-price tick routes to the matching on_tick.
        await ws._handle(_mark_frame("SOLUSDT"))
        assert fired_tick == ["SOLUSDT"], f"tick mis-routed: {fired_tick}"
        print("  ✓ markPrice tick routed to the correct processor")

        # A complete 5M frame on a 1H-only route (on_5m_close=None) must be CACHED
        # into buf_5m (streaming/caching alongside 1H) — not dropped — and must not
        # crash on the absent close-callback.
        eth_route = ws._routes["ETHUSDT"]
        before = eth_route.state.buf_5m.count
        await ws._handle(json.dumps({"stream": "ethusdt@kline_5m",
            "data": {"k": {"t": 0, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "9", "x": True}}}))
        assert eth_route.state.buf_5m.count == before + 1, "5M frame should cache into buf_5m"
        print("  ✓ 5M frame cached into buf_5m on a 1H-only route (no callback crash)")

        # Unknown symbol / control frame must not raise.
        await ws._handle(json.dumps({"stream": "dogeusdt@kline_1h", "data": {"k": {"x": True}}}))
        await ws._handle(json.dumps({"result": None, "id": 1}))
        assert fired_1h == ["ETHUSDT", "SOLUSDT", "BTCUSDT"], "unknown symbol leaked into routing"
        print("  ✓ unrouted symbol + control frame handled without error")

    asyncio.run(_run())
    print("ALL WS FAN-OUT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
