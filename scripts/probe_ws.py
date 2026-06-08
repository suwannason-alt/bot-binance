"""Standalone Binance WS connectivity probe — bypasses the whole app.

No warm start, no globals, no strategy. Just connects to the SAME stream URL the
bot uses and prints whatever arrives. This isolates "can this server receive
Binance frames at all?" from any bug inside main.py.

Run:  python probe_ws.py
Expect (healthy): "connected" within ~1s, then a frame line within ~3s.
"""
# ── Path bootstrap: modular layout — keep flat `import config` style resolvable
# from any subdirectory (src/core, backtesting, scripts). ──────────────────────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import asyncio
import json

import websockets

import config


async def main() -> None:
    print(f"Connecting to: {config.WS_URL}")
    try:
        async with websockets.connect(config.WS_URL, ping_interval=20, ping_timeout=20) as ws:
            print("connected — waiting for frames (15s)…")
            n = 0
            try:
                while n < 10:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                    n += 1
                    msg = json.loads(raw)
                    print(f"  frame #{n}: stream={msg.get('stream', '<none>')}  "
                          f"keys={list(msg.get('data', {}).keys())}")
            except asyncio.TimeoutError:
                print("  ✗ NO FRAMES for 15s — socket is connected but silent "
                      "(network/region block at Binance is the usual cause).")
                return
            print(f"  ✓ received {n} frames — Binance is reachable from this host.")
    except Exception as exc:
        print(f"  ✗ connect failed: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
