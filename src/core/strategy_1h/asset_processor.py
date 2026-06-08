"""
Per-asset 1H processor for the multi-asset live system.

One :class:`AssetProcessor` owns a single symbol's live state — its own
:class:`~data_store.MarketState`, warm-started candle buffer, and (when trading LIVE)
its own symbol-bound :class:`~trader.Trader`.  It is fed by the shared WebSocket via a
:class:`ws_client.Route`: the socket dispatches that symbol's 1H closes / mark-price
ticks to this object, exactly as ``main.on_1h_close`` handles the primary symbol.

**Concurrency model — race-free by construction, no lock.**  The shared WS read loop
handles one frame to completion before reading the next, so processors never run
concurrently.  Each 1H close therefore does ``config.apply_symbol(symbol)`` →
*synchronous* signal evaluation (reads the just-applied globals) → act, with no
``await`` between the apply and the read.  Nothing can interleave and clobber the
shared config globals.

**TRADE_MODE:**
  * ``"EVAL_ONLY"`` — evaluate + log the would-be signal / entry-funnel diagnostic.
    Never touches an exchange.  No ``Trader`` is created.  (Default for ETH/SOL.)
  * ``"LIVE"`` — additionally reserves margin from the shared
    :class:`~order_manager.OrderManager` and, if granted, places the order via its
    ``Trader``; margin is released when the position closes.

⚠️ The LIVE order path is **paper-validation-pending** — it has not been exercised
against a real or paper exchange in this build.  EVAL_ONLY is fully exercised by the
evaluation call; LIVE wiring is structurally complete but unverified end-to-end.
"""
from __future__ import annotations

import logging
from typing import Optional

import config_1h as config
import strategy
from data_store import MarketState

logger = logging.getLogger("asset_processor")


class AssetProcessor:
    """Drives the 1H strategy for one symbol off the shared stream.

    Args:
        symbol:        Trading pair, e.g. ``"ETHUSDT"`` (must be in ``CONFIG_MATRIX``).
        order_manager: Shared cross-asset margin arbiter (required for LIVE mode).
    """

    def __init__(self, symbol: str, order_manager: Optional[object] = None) -> None:
        self.symbol = symbol
        self.mode = config.trade_mode(symbol)          # "LIVE" | "EVAL_ONLY"
        self.order_manager = order_manager
        self.state = MarketState()
        self.bars_since_last = 9999
        self._bar_counter = 0
        self.trader = None
        if self.mode == "LIVE":
            from trader import Trader                   # local import: EVAL needs no trader
            self.trader = Trader(symbol=symbol)

    # ── Warm start ────────────────────────────────────────────────────────────
    async def warm_start(self) -> None:
        """Fetch ~3030 1H bars for this symbol and hydrate the candle buffer."""
        from warm_start import WarmStart
        warm = await WarmStart.run(symbol=self.symbol)
        WarmStart.hydrate_candle_buffer(self.state.buf_1h, warm.live_history)
        if self.trader is not None:
            await self.trader.initialize()
        logger.info("[%s] warm-started (mode=%s, buf_1h=%d)",
                    self.symbol, self.mode, self.state.buf_1h.count)

    # ── 1H close handler (registered as the Route.on_1h_close) ─────────────────
    async def on_1h_close(self, state: MarketState) -> None:
        """Apply this symbol's profile, evaluate the 1H signal, and act on the mode.

        The engine reads strategy knobs from the shared ``config`` globals, and the
        PRIMARY symbol's path (in ``main``) may be driving ``BREAKOUT_PERIOD`` via WFO.
        So we **snapshot the engine globals, apply this symbol's profile, evaluate
        synchronously, then restore** — leaving the primary's config exactly as it was.
        apply → evaluate → restore contains no ``await``, so under the WS's sequential
        dispatch nothing can observe the temporarily-mutated globals.
        """
        self._bar_counter += 1
        self.bars_since_last += 1          # one more bar since the last entry/exit
        saved = {k: getattr(config, k) for k in config._ENGINE_KEYS}
        try:
            config.apply_symbol(self.symbol)                   # pin this symbol's knobs
            sig = strategy.evaluate_1h_live(state, self.bars_since_last)   # sync read
            diag_text = None
            if sig is None:
                diag = strategy.diagnose_1h_live(state, self.bars_since_last)
                diag_text = diag.text if diag is not None else \
                    f"(buffer warming: {state.buf_1h.count}/{config.MIN_CANDLES_1H})"
            # If LIVE and a signal fired, size the intended margin while the symbol's
            # knobs are still applied (sizing reads shared RISK/LEVERAGE only, but we
            # keep it inside the window for correctness).
            margin = 0.0
            if sig is not None and self.mode == "LIVE" and self.trader is not None:
                qty = strategy.position_size_usdt(self.trader.balance, sig.entry, sig.sl)
                margin = (qty * sig.entry) / max(config.LEVERAGE, 1)
        finally:
            for k, v in saved.items():
                setattr(config, k, v)                          # restore primary's config

        if sig is None:
            logger.info("[%s] no 1H signal  mark=%.4f  bars_since=%d  %s",
                        self.symbol, state.mark_price, self.bars_since_last, diag_text)
            return
        if self.mode == "EVAL_ONLY":
            logger.info("[%s] EVAL_ONLY would-enter: %s  Entry=%.4f SL=%.4f TP=%.4f RR=%.2f "
                        "(dry-run, no order placed)",
                        self.symbol, sig.reason, sig.entry, sig.sl, sig.tp, sig.rr_ratio)
            return

        await self._enter_live(sig, margin)

    async def _enter_live(self, sig, margin: float) -> None:
        """LIVE entry: arbitrate global margin, then place the order. (paper-pending)

        ⚠️ Managing MULTIPLE concurrent LIVE symbols' trailing stops through the shared
        ``config`` globals is not yet safe (tick-time trail reads would race across
        symbols) — keep at most one symbol in TRADE_MODE="LIVE" until per-symbol config
        is threaded through the engine.  The default matrix ships exactly one (BTC).
        """
        if self.trader is None or self.order_manager is None:
            logger.warning("[%s] LIVE entry skipped — trader/order_manager unset", self.symbol)
            return
        if not await self.order_manager.reserve(self.symbol, margin):
            logger.info("[%s] entry skipped — insufficient shared margin (need %.2f)",
                        self.symbol, margin)
            return
        pos = await self.trader.open_position(sig)
        if pos:
            self.bars_since_last = 0
            logger.info("[%s] LIVE position opened qty=%.4f balance=%.2f",
                        self.symbol, pos.qty, self.trader.balance)
        else:
            await self.order_manager.release(self.symbol)      # entry failed → free margin

    # ── Mark-price tick handler (registered as Route.on_tick) ──────────────────
    async def on_tick(self, state: MarketState, price: float) -> None:
        """LIVE: manage the open position (STEP trail + SL/TP exit) and release margin
        on close.  EVAL_ONLY: nothing to manage."""
        if self.mode != "LIVE" or self.trader is None:
            return
        # The STEP trail reads this symbol's TRAIL_ACTIVATE_ATR (and SL/TP) from the
        # shared config globals, which the primary's path leaves set to ITS profile.
        # Snapshot→apply this symbol→manage→restore so each LIVE asset trails on its OWN
        # params.  check_exit may await (live order ops), but the WS dispatch is strictly
        # sequential — no other processor runs until this returns — so nothing observes
        # the temporarily-applied globals.
        saved = {k: getattr(config, k) for k in config._ENGINE_KEYS}
        try:
            config.apply_symbol(self.symbol)
            self.trader.reset_day()
            closed = await self.trader.check_exit(price)        # manages STEP trail + SL/TP
        finally:
            for k, v in saved.items():
                setattr(config, k, v)
        if closed is not None:                                  # position just closed
            self.bars_since_last = 0
            if self.order_manager is not None:
                await self.order_manager.release(self.symbol)
