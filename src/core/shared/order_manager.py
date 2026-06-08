"""
Central cross-asset margin arbiter for multi-asset live trading.

In the multi-asset live system several enabled processors share ONE futures account.
If two assets signal an entry close together, naive per-asset sizing could commit more
margin than the account holds.  :class:`OrderManager` is a small global ledger that
arbitrates available margin across the LIVE sleeves so the account is never
oversubscribed.

**It is accounting, not a mutex.**  ``reserve`` / ``release`` hold an ``asyncio.Lock``
only across a tiny, *await-free* check-and-update critical section — never across a
network/order call — so the event loop is never blocked and other coroutines keep
running.  Under the live WebSocket's strictly-sequential frame dispatch (one frame is
handled to completion before the next is read) per-asset entries already can't
interleave, so the lock is belt-and-suspenders; it keeps the ledger correct even if
order placement is ever parallelised.  The correct parallel pattern is exactly what is
implemented here: **under the lock, check + reserve with no await; release the lock;
then await the actual order; on failure, release the reservation.**
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict

logger = logging.getLogger("order_manager")


class OrderManager:
    """Global margin ledger shared by all LIVE asset processors.

    Args:
        balance_provider: Zero-arg callable returning current total account equity
                          in USDT (e.g. ``lambda: primary_trader.balance``).  Must be
                          synchronous — it is read inside the await-free critical
                          section.
        max_utilization:  Fraction of equity that may be committed as margin across all
                          assets simultaneously (1.0 = the whole account; <1.0 keeps a
                          cash buffer).
    """

    def __init__(self, balance_provider: Callable[[], float], max_utilization: float = 1.0) -> None:
        self._balance_provider = balance_provider
        self._reserved: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.max_utilization = max_utilization

    @property
    def total_reserved(self) -> float:
        """Sum of margin currently committed across all assets (USDT)."""
        return sum(self._reserved.values())

    def reserved_for(self, symbol: str) -> float:
        """Margin currently committed for ``symbol`` (USDT)."""
        return self._reserved.get(symbol, 0.0)

    def available(self) -> float:
        """Uncommitted margin budget right now (USDT).  Read-only snapshot."""
        return float(self._balance_provider()) * self.max_utilization - self.total_reserved

    async def reserve(self, symbol: str, margin_usdt: float) -> bool:
        """Atomically commit ``margin_usdt`` for ``symbol`` if the account can fund it.

        Returns ``True`` and books the reservation when it fits the global budget;
        ``False`` (nothing booked) when it would oversubscribe the account.  The
        critical section is await-free, so the loop is never blocked.
        """
        if margin_usdt <= 0:
            return False
        async with self._lock:
            equity = float(self._balance_provider())
            budget = equity * self.max_utilization
            available = budget - self.total_reserved
            if margin_usdt > available + 1e-9:
                logger.warning(
                    "Margin DENIED %s: need %.2f but only %.2f available "
                    "(equity %.2f · max_util %.0f%% · reserved %.2f)",
                    symbol, margin_usdt, available, equity,
                    self.max_utilization * 100, self.total_reserved,
                )
                return False
            self._reserved[symbol] = self._reserved.get(symbol, 0.0) + margin_usdt
            logger.info(
                "Margin RESERVED %s: %.2f  (total reserved %.2f / budget %.2f)",
                symbol, margin_usdt, self.total_reserved, budget,
            )
            return True

    async def release(self, symbol: str) -> float:
        """Release all margin reserved for ``symbol`` (call when its position closes).

        Returns the amount freed (0.0 if nothing was reserved).
        """
        async with self._lock:
            freed = self._reserved.pop(symbol, 0.0)
        if freed:
            logger.info("Margin RELEASED %s: %.2f freed  (total reserved %.2f)",
                        symbol, freed, self.total_reserved)
        return freed
