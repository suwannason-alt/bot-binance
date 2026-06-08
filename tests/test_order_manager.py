"""Cross-asset margin arbiter test (no pytest — standalone runner).

Proves ``order_manager.OrderManager`` never lets the shared account be oversubscribed,
including under concurrent ``asyncio.gather`` reservations (the race the multi-asset
spec warns about).  Exits non-zero on any failure.

Run:  python tests/test_order_manager.py
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

from order_manager import OrderManager


async def _sequential_case() -> None:
    bal = {"v": 1000.0}
    om = OrderManager(balance_provider=lambda: bal["v"], max_utilization=1.0)

    assert await om.reserve("BTCUSDT", 600.0) is True,  "BTC 600 should fit in 1000"
    assert await om.reserve("ETHUSDT", 600.0) is False, "ETH 600 must be denied (only 400 left)"
    assert await om.reserve("ETHUSDT", 400.0) is True,  "ETH 400 should exactly fit"
    assert abs(om.total_reserved - 1000.0) < 1e-9,      "account fully committed"
    assert await om.reserve("SOLUSDT", 1.0) is False,   "nothing left for SOL"

    freed = await om.release("BTCUSDT")
    assert abs(freed - 600.0) < 1e-9,                   "releasing BTC frees its 600"
    assert abs(om.total_reserved - 400.0) < 1e-9,       "only ETH remains"
    assert await om.reserve("SOLUSDT", 500.0) is True,  "SOL 500 fits in the freed budget"
    print("  ✓ sequential — denies oversubscription, releases & re-reserves correctly")


async def _concurrent_case() -> None:
    # Three simultaneous 500-margin requests against a 1000 budget: at most two can
    # win, and the ledger must never exceed the budget even under gather().
    om = OrderManager(balance_provider=lambda: 1000.0, max_utilization=1.0)
    results = await asyncio.gather(*[om.reserve(f"A{i}", 500.0) for i in range(3)])
    granted = sum(1 for r in results if r)
    assert granted == 2, f"exactly two of three 500-margin reserves should win, got {granted}"
    assert om.total_reserved <= 1000.0 + 1e-9, f"budget breached: {om.total_reserved}"
    print(f"  ✓ concurrent — {granted}/3 gathered reserves granted, budget intact "
          f"(reserved={om.total_reserved:.0f}/1000)")


def main() -> int:
    print("OrderManager cross-asset margin arbitration")
    asyncio.run(_sequential_case())
    asyncio.run(_concurrent_case())
    print("ALL ORDER-MANAGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
