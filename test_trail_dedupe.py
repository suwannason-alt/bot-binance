"""Live SL one-tick dedupe test (no pytest — standalone runner).

Verifies that ``Trader._live_check_trail`` does NOT cancel+replace the exchange
stop-loss order when the trailed SL rounds to the *same* price tick it already
rests on.  With a dynamic trail (``TRAIL_STOP_ATR>0``) the raw float SL nudges up
on nearly every markPrice tick (~1/s); without the dedupe each nudge would fire a
needless cancel+replace and open a brief unprotected window.

Run:  python test_trail_dedupe.py     (exits non-zero on failure)
"""
from __future__ import annotations

import asyncio
import sys

import config
from trader import Trader, Position


class _FakeExchange:
    """Counts cancel/create order calls; returns a fresh id on each create."""

    def __init__(self) -> None:
        self.cancels = 0
        self.creates = 0
        self._seq = 0

    def cancel_order(self, order_id, symbol):  # noqa: D401 - ccxt signature
        self.cancels += 1
        return {"id": order_id}

    def create_order(self, symbol, type_, side, qty, params=None):  # noqa: D401
        self.creates += 1
        self._seq += 1
        return {"id": f"sl{self._seq}"}


async def _feed(trader: Trader, prices: list[float]) -> None:
    for p in prices:
        await trader._live_check_trail(p)


def main() -> int:
    # Live deploy profile — classic cascade with an active dynamic trail.
    # Trader() stays in paper mode (no live exchange init); we inject a fake
    # exchange and call _live_check_trail directly — it uses self._exchange and
    # does not gate on PAPER_TRADING, so the cancel+replace branch runs as live.
    config.ADAPTIVE_TRAILING_ENABLED = False
    config.TRAIL_ACTIVATE_ATR = 1.5
    config.TRAIL_LOCK_ATR = 2.0
    config.TRAIL_STOP_ATR = 1.2
    config.ATR_SL_MULTIPLIER = 1.5
    config.PRICE_TICK = 0.10

    trader = Trader()
    fake = _FakeExchange()
    trader._exchange = fake

    entry, atr = 100.0, 1.0
    trader.position = Position(
        side="LONG", entry=entry, qty=1.0,
        sl=entry - 1.5 * atr, tp=entry + 6.0 * atr, initial_atr=atr,
    )
    trader.position.sl_order_id = "sl0"

    # 1) Activate the trail with a clear move (+1.6×ATR) → one real SL update.
    asyncio.run(_feed(trader, [101.6]))
    after_activate = fake.creates
    assert after_activate >= 1, "trail did not place an updated SL on activation"

    # 2) Sub-tick nudges: tiny increments that all round to the same SL tick.
    #    Each raises the raw float SL but must NOT trigger a cancel+replace.
    baseline_creates = fake.creates
    baseline_cancels = fake.cancels
    asyncio.run(_feed(trader, [101.601, 101.602, 101.603, 101.604, 101.605]))
    assert fake.creates == baseline_creates, (
        f"dedupe failed: {fake.creates - baseline_creates} redundant SL "
        f"replacements on sub-tick nudges (expected 0)"
    )
    assert fake.cancels == baseline_cancels, "dedupe failed: redundant cancels"
    print(f"  ✓ sub-tick nudges deduped — 0 redundant cancel+replace over 5 ticks")

    # 3) A genuine multi-tick move MUST still chase (cancel+replace fires).
    before = fake.creates
    asyncio.run(_feed(trader, [104.0]))
    assert fake.creates > before, "real tick move was wrongly suppressed"
    print(f"  ✓ genuine move still chases — SL replaced @ {trader.position.sl:.2f}")

    print("ALL DEDUPE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
