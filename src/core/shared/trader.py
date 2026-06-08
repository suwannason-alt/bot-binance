"""
Order execution layer.
Paper mode simulates fills; live mode uses ccxt Binance Futures.

Order-entry flow (live mode):
  USE_LIMIT_ENTRY=true  → post limit at close price (maker fee 0.02%)
                           wait up to LIMIT_ENTRY_TIMEOUT s for fill
                           → if not filled, cancel and fall back to market order
  USE_LIMIT_ENTRY=false → market order (taker fee 0.05%)
"""
import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

import config_1h as config   # the live order executor runs the 1H book
from strategy import Signal, position_size_usdt

logger = logging.getLogger("trader")


@dataclass
class Position:
    """An open or closed trade position.

    Attributes:
        side: Trade direction — ``"LONG"`` or ``"SHORT"``.
        entry: Fill price at which the position was opened.
        qty: Contract quantity.
        sl: Current stop-loss price (updated as trailing stop activates).
        tp: Take-profit price (fixed at entry).
        open_time: ISO-8601 UTC timestamp of when the position was opened.
        pnl: Realised profit / loss in USDT (set on close).
        closed: ``True`` once the position has been exited.
        close_reason: Exit trigger — ``"TP"``, ``"SL"``, or ``"BE"``.
        trail_activated: ``True`` once break-even has been activated
            (price moved ``TRAIL_ACTIVATE_ATR × initial_atr`` in favour).
        lock_activated: ``True`` once the profit-lock trail stage has fired.
        trail_peak: Highest price seen (LONG) or lowest price seen (SHORT)
            since break-even activation; used by the dynamic trailing stop.
        initial_atr: ATR value at entry — the reference for all trail thresholds.
        sl_order_id: Exchange stop-market order ID (live mode only).
    """

    side: str
    entry: float
    qty: float
    sl: float
    tp: float
    open_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    pnl: float = 0.0
    closed: bool = False
    close_reason: str = ""
    trail_activated: bool = False
    lock_activated: bool = False
    trail_peak: float = 0.0
    initial_atr: float = 0.0
    sl_order_id: Optional[str] = None


class Trader:
    """Manages order execution and trade lifecycle for both paper and live modes.

    In paper mode all fills are simulated locally against the mark price.
    In live mode orders are routed to Binance USDM Futures via the ``ccxt``
    library, with optional limit-order entry and a periodic sync poll that
    detects when the exchange has filled an SL/TP order server-side.

    Attributes:
        position:          Currently open :class:`Position`, or ``None``.
        balance:           Current account balance in USDT.
        trade_log:         Ordered list of all closed :class:`Position` objects.
        day_start_balance: Balance at the start of the current UTC day.
        daily_halted:      ``True`` when today's profit or loss limit has been hit.
        daily_profit_hit:  ``True`` only when the profit target was the trigger.
        days_profit_hit:   Cumulative count of days where profit target was reached.
        days_loss_hit:     Cumulative count of days where loss limit was reached.
        total_days:        Total UTC days elapsed since the session started.
        consecutive_losses: Count of consecutive stop-loss exits (resets daily).
    """

    def __init__(self, symbol: Optional[str] = None):
        # Per-symbol binding for multi-asset live trading.  Defaults to the env
        # ``config.SYMBOL`` so the single-asset path is byte-for-byte unchanged; a
        # secondary-asset processor passes its own symbol so its exchange order calls
        # route to the right market.  ``symbol_ccxt`` mirrors the old config.SYMBOL_CCXT
        # 3-char-base slicing (BTCUSDT→BTC/USDT, ETHUSDT→ETH/USDT).
        self.symbol: str = symbol or config.SYMBOL
        self.symbol_ccxt: str = f"{self.symbol[:3]}/{self.symbol[3:]}"
        self.position: Optional[Position] = None
        self.balance: float = 1000.0  # paper balance; overwritten on live init
        self.trade_log: list[Position] = []
        self._exchange = None

        # Daily tracking
        self.day_start_balance: float = self.balance
        self._today_date: str = self._utc_date()
        self.daily_halted: bool = False       # True when daily profit or loss limit hit
        self.daily_profit_hit: bool = False   # True only when PROFIT target reached
        self.days_profit_hit: int = 0
        self.days_loss_hit: int = 0
        self.total_days: int = 0

        # Consecutive-loss circuit breaker (resets each UTC day)
        self.consecutive_losses: int = 0

        # Live position sync — avoid spamming the exchange API
        self._last_sync_time: float = 0.0

        if not config.PAPER_TRADING:
            self._init_live()

    # ── Daily helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def reset_day(self) -> None:
        """Reset daily tracking counters at UTC midnight.

        Idempotent — safe to call on every tick.  The reset only fires once
        per calendar day (checked via the stored UTC date string).
        """
        today = self._utc_date()
        if today == self._today_date:
            return  # already reset today
        # tally yesterday
        self.total_days += 1
        if self.daily_profit_hit:
            self.days_profit_hit += 1
        elif self.daily_halted:
            self.days_loss_hit += 1
        # reset for new day
        self._today_date = today
        self.day_start_balance = self.balance
        self.daily_halted = False
        self.daily_profit_hit = False
        self.consecutive_losses = 0           # circuit breaker resets every day
        logger.info(
            f"New day {today} — balance={self.balance:.2f}  "
            f"all-time profit_days={self.days_profit_hit}  loss_days={self.days_loss_hit}"
        )

    @property
    def day_pnl(self) -> float:
        """Current day's unrealised + realised PnL relative to day-start balance."""
        return self.balance - self.day_start_balance

    def _check_daily_limits(self) -> None:
        """Evaluate daily profit / loss thresholds and halt trading if exceeded.

        Sets ``daily_halted = True`` when either threshold is breached.
        Also sets ``daily_profit_hit = True`` when the profit target fires.
        No-op if ``daily_halted`` is already ``True``.
        """
        if self.daily_halted:
            return
        pnl = self.day_pnl
        profit_thresh = (config.DAILY_PROFIT_TARGET_USD if config.DAILY_PROFIT_TARGET_USD > 0
                         else self.day_start_balance * config.DAILY_PROFIT_TARGET_PCT)
        loss_thresh = (config.DAILY_LOSS_LIMIT_USD if config.DAILY_LOSS_LIMIT_USD > 0
                       else self.day_start_balance * config.DAILY_LOSS_LIMIT_PCT)
        if profit_thresh > 0 and pnl >= profit_thresh:
            self.daily_halted = True
            self.daily_profit_hit = True
            logger.info(
                f"Daily PROFIT target reached: PnL={pnl:+.2f} >= {profit_thresh:.2f}  "
                f"No more trades today."
            )
        elif loss_thresh > 0 and pnl <= -loss_thresh:
            self.daily_halted = True
            logger.info(
                f"Daily LOSS limit reached: PnL={pnl:+.2f} <= -{loss_thresh:.2f}  "
                f"No more trades today."
            )

    # ── Precision helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _round_qty(qty: float) -> float:
        """Round quantity to Binance lot step (avoids LOT_SIZE filter rejection)."""
        step = config.QTY_STEP
        return round(round(qty / step) * step, 8)

    @staticmethod
    def _round_price(price: float) -> float:
        """Round price to Binance tick size (avoids PRICE_FILTER rejection)."""
        tick = config.PRICE_TICK
        return round(round(price / tick) * tick, 2)

    # ── Exchange helpers ──────────────────────────────────────────────────────

    def _init_live(self) -> None:
        """Initialise the ``ccxt`` Binance USDM Futures exchange connection.

        Raises:
            Exception: Re-raises any ccxt connection or credential error after
                logging it, so the calling constructor propagates the failure.
        """
        try:
            import ccxt
            self._exchange = ccxt.binanceusdm({
                "apiKey": config.API_KEY,
                "secret": config.API_SECRET,
                "options": {"defaultType": "future"},
            })
            self._exchange.load_markets()
            logger.info("Binance Futures connected (LIVE mode)")
        except Exception as e:
            logger.error(f"Failed to connect to Binance: {e}")
            raise

    async def initialize(self) -> None:
        """Fetch live balance and recover any open position on startup."""
        if not config.PAPER_TRADING:
            self.balance = await self.fetch_balance()
            self.day_start_balance = self.balance
            logger.info(f"Live balance: {self.balance:.2f} USDT")
            await self._recover_position()

    async def _recover_position(self) -> None:
        """Sync internal state with the exchange on startup or reconnect.

        If an open position exists (e.g. after a process crash), this method
        rebuilds a :class:`Position` object so SL/TP monitoring resumes
        without missing an exit.  ATR is estimated at 1.5% of entry price
        when the actual value is unknown; it improves after the next 1H bar.
        """
        loop = asyncio.get_event_loop()
        try:
            positions = await loop.run_in_executor(
                None,
                lambda: self._exchange.fetch_positions([self.symbol_ccxt]),
            )
            for p in positions:
                contracts = float(p.get("contracts") or 0)
                if abs(contracts) < config.MIN_ORDER_QTY:
                    continue
                side  = "LONG" if contracts > 0 else "SHORT"
                entry = float(p.get("entryPrice") or 0)
                if entry <= 0:
                    continue
                # ATR unknown after restart — estimate as 1.5 % of price
                atr_est = entry * 0.015
                sl = (entry - atr_est * config.ATR_SL_MULTIPLIER if side == "LONG"
                      else entry + atr_est * config.ATR_SL_MULTIPLIER)
                tp = (entry + atr_est * config.ATR_TP_MULTIPLIER if side == "LONG"
                      else entry - atr_est * config.ATR_TP_MULTIPLIER)
                # Try to find the existing SL order on the exchange
                sl_order_id = None
                try:
                    open_orders = await loop.run_in_executor(
                        None,
                        lambda: self._exchange.fetch_open_orders(self.symbol_ccxt),
                    )
                    for o in open_orders:
                        if o.get("type") in ("stop_market", "stop") and o.get("reduceOnly"):
                            sl_order_id = o.get("id")
                            break
                except Exception:
                    pass

                self.position = Position(
                    side=side, entry=entry, qty=abs(contracts),
                    sl=sl, tp=tp, initial_atr=atr_est,
                    sl_order_id=sl_order_id,
                )
                logger.warning(
                    f"[LIVE] Recovered open position: {side} {abs(contracts):.4f} @ {entry:.2f}  "
                    f"SL≈{sl:.2f}  TP≈{tp:.2f}  sl_order={sl_order_id}  "
                    f"(ATR estimated — will improve after 1H bar)"
                )
                break
        except Exception as e:
            logger.error(f"[LIVE] Position recovery failed: {e}")

    async def fetch_balance(self) -> float:
        """Return the current free USDT balance.

        In paper mode, returns the simulated ``self.balance`` immediately.
        In live mode, queries the exchange via ``ccxt.fetch_balance()``.

        Returns:
            Free USDT balance as a float.
        """
        if config.PAPER_TRADING:
            return self.balance
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._exchange.fetch_balance)
        return float(data["USDT"]["free"])

    async def fetch_total_equity(self) -> float:
        """Return the total USDT equity: wallet balance + unrealized PnL.

        Used for ``EQUITY_PERCENT`` position sizing so the margin calculation
        is based on the full account value, not just the free (idle) balance.

        In paper mode the running ``self.balance`` already represents compounded
        equity after each closed trade — unrealized PnL is always zero at entry
        time because the bot holds at most one position and the guard in
        ``open_position()`` rejects new signals while a position is open.

        In live mode the Binance USDM Futures account supplies:
          ``totalWalletBalance``   — sum of all margin + realized PnL
          ``totalUnrealizedProfit`` — open position mark-to-market

        Falls back to the free balance if the account info keys are absent
        (e.g. unexpected CCXT response shape), so sizing degrades gracefully
        rather than crashing.

        Returns:
            Total equity in USDT as a float.
        """
        if config.PAPER_TRADING:
            return self.balance

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._exchange.fetch_balance)
        info = data.get("info", {})
        try:
            wallet     = float(info["totalWalletBalance"])
            unrealized = float(info.get("totalUnrealizedProfit", 0.0))
            total_equity = wallet + unrealized
            if total_equity > 0:
                return total_equity
        except (KeyError, TypeError, ValueError):
            pass
        # Graceful fallback: use reported USDT total, then free
        usdt = data.get("USDT", {})
        return float(usdt.get("total") or usdt.get("free") or self.balance)

    # ── Trade execution ───────────────────────────────────────────────────────

    async def open_position(self, signal: Signal) -> Optional[Position]:
        if self.position is not None:
            logger.debug("Position already open — skipping new signal")
            return None
        if self.daily_halted:
            logger.debug("Daily limit reached — skipping new signal")
            return None
        if config.MAX_CONSECUTIVE_LOSSES > 0 and self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            logger.info(
                f"Consecutive-loss circuit breaker: {self.consecutive_losses} SLs in a row "
                f"(max={config.MAX_CONSECUTIVE_LOSSES}) — no new entry today"
            )
            return None

        # Fetch total equity (wallet + unrealized PnL) for accurate EQUITY_PERCENT
        # sizing.  Falls back to free balance gracefully in live mode; paper mode
        # always returns self.balance directly.
        equity  = await self.fetch_total_equity()
        raw_qty = position_size_usdt(
            equity, signal.entry, signal.sl,
            atr_ratio=signal.indicators_1h.get("atr_ratio"),
            size_scale=signal.size_scale,
        )
        qty     = self._round_qty(raw_qty)

        if qty < config.MIN_ORDER_QTY:
            logger.warning(
                f"Computed qty {raw_qty:.6f} → rounded {qty:.6f} < MIN {config.MIN_ORDER_QTY} "
                f"— skipping trade (equity too low or ATR too wide)"
            )
            return None

        # ── Sizing mode label for the trade log ──────────────────────────────
        if config.EQUITY_PERCENT > 0:
            margin_usd   = equity * (config.EQUITY_PERCENT / 100.0)
            position_val = margin_usd * config.LEVERAGE
            risk_label   = (
                f"EQ={config.EQUITY_PERCENT:.0f}%×{config.LEVERAGE}lev"
                f"  margin=${margin_usd:.2f}  notional=${position_val:.2f}"
            )
        elif config.ORDER_BALANCE_USD > 0:
            risk_label = f"ORDER_BAL=${config.ORDER_BALANCE_USD:.0f}×{config.LEVERAGE}lev"
        elif config.RISK_USD > 0:
            risk_label = f"RISK_USD=${config.RISK_USD:.0f}"
        else:
            risk_label = f"RISK={config.RISK_PERCENT}%"
        regime_str = getattr(signal, "regime", "?")
        logger.info(
            f"[{'PAPER' if config.PAPER_TRADING else 'LIVE'}] "
            f"Opening {signal.side} {qty:.4f} {self.symbol} @ {signal.entry:.2f}  "
            f"SL={signal.sl:.2f}  TP={signal.tp:.2f}  "
            f"{risk_label}  regime={regime_str}  {signal.reason}"
        )

        sl_order_id = None
        if not config.PAPER_TRADING:
            sl_order_id = await self._place_live_order(signal, qty)
            if sl_order_id is None:
                # SL placement failed → position was closed by emergency handler; abort
                return None

        self.position = Position(
            side=signal.side,
            entry=signal.entry,
            qty=qty,
            sl=signal.sl,
            tp=signal.tp,
            initial_atr=float(signal.indicators_1h.get("atr") or 0.0),
            sl_order_id=sl_order_id,
        )
        return self.position

    async def _place_live_order(self, signal: Signal, qty: float) -> Optional[str]:
        """
        Dispatch to limit-entry or market-entry based on USE_LIMIT_ENTRY config.
        Returns SL order ID on success, None on failure (caller aborts).
        """
        if config.USE_LIMIT_ENTRY:
            return await self._place_limit_entry(signal, qty)
        return await self._place_market_entry(signal, qty)

    async def _set_leverage_safe(self, sym: str):
        """Set leverage; swallow repeated-call errors from the exchange."""
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.set_leverage(config.LEVERAGE, sym)
            )
        except Exception as e:
            logger.warning(f"[LIVE] set_leverage warning (continuing): {e}")

    async def _place_sl_tp_orders(
        self, sym: str, signal: Signal, qty: float
    ) -> Optional[str]:
        """
        Place SL + TP protective orders after an entry fill.
        SL is critical — emergency market-close fires if it fails.
        TP is best-effort.
        Returns SL order ID or None (position closed by emergency handler).
        """
        loop     = asyncio.get_event_loop()
        side_out = "sell" if signal.side == "LONG" else "buy"
        sl_price = self._round_price(signal.sl)
        tp_price = self._round_price(signal.tp)

        # ── Stop-loss (CRITICAL) ──────────────────────────────────────────────
        sl_order_id = None
        try:
            sl_order = await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    sym, "stop_market", side_out, qty,
                    params={"stopPrice": sl_price, "reduceOnly": True},
                ),
            )
            sl_order_id = sl_order.get("id") if sl_order else None
            logger.info(f"[LIVE] SL placed @ {sl_price}  id={sl_order_id}")
        except Exception as e:
            logger.error(f"[LIVE] SL placement FAILED: {e}  → EMERGENCY CLOSE")
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        sym, "market", side_out, qty, params={"reduceOnly": True}
                    ),
                )
                logger.info("[LIVE] Emergency close executed")
            except Exception as e2:
                logger.error(f"[LIVE] Emergency close FAILED too: {e2} — MANUAL ACTION REQUIRED")
            return None

        # ── Take-profit (best-effort) ─────────────────────────────────────────
        try:
            await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    sym, "take_profit_market", side_out, qty,
                    params={"stopPrice": tp_price, "reduceOnly": True},
                ),
            )
            logger.info(f"[LIVE] TP placed @ {tp_price}")
        except Exception as e:
            logger.warning(f"[LIVE] TP placement failed (SL still active): {e}")

        return sl_order_id

    async def _place_market_entry(self, signal: Signal, qty: float) -> Optional[str]:
        """Market-order entry (taker fee 0.05%). Reliable but more expensive."""
        loop    = asyncio.get_event_loop()
        sym     = self.symbol_ccxt
        side_in = "buy" if signal.side == "LONG" else "sell"

        await self._set_leverage_safe(sym)

        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.create_order(sym, "market", side_in, qty)
            )
            logger.info(f"[LIVE] Market {signal.side} {qty:.4f} filled (taker)")
        except Exception as e:
            logger.error(f"[LIVE] Market entry failed: {e}")
            return None

        return await self._place_sl_tp_orders(sym, signal, qty)

    async def _place_limit_entry(self, signal: Signal, qty: float) -> Optional[str]:
        """
        Post-only limit entry to capture maker rebate (0.02% vs 0.05% taker).

        Places limit at the breakout close price (the 1H candle just closed at
        exactly this level, so the next tick almost always trades through it).

        Wait up to LIMIT_ENTRY_TIMEOUT seconds for fill, then:
          → still open after half-timeout  : chase with new limit at mid-price
          → still open after full-timeout  : cancel, fall back to market order
        """
        loop     = asyncio.get_event_loop()
        sym      = self.symbol_ccxt
        side_in  = "buy" if signal.side == "LONG" else "sell"
        limit_px = self._round_price(signal.entry)

        await self._set_leverage_safe(sym)

        # ── Place limit order ─────────────────────────────────────────────────
        order_id = None
        try:
            order = await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    sym, "limit", side_in, qty, limit_px,
                    params={"timeInForce": "GTC"},
                ),
            )
            order_id = order.get("id")
            logger.info(
                f"[LIVE] Limit {signal.side} {qty:.4f} @ {limit_px} posted "
                f"(maker fee 0.02%)  id={order_id}"
            )
        except Exception as e:
            logger.warning(f"[LIVE] Limit order failed: {e}  → market fallback")
            return await self._place_market_entry(signal, qty)

        # ── Poll for fill ─────────────────────────────────────────────────────
        timeout    = config.LIMIT_ENTRY_TIMEOUT
        deadline   = _time.monotonic() + timeout
        half_point = _time.monotonic() + timeout / 2
        chased     = False

        while _time.monotonic() < deadline:
            await asyncio.sleep(5)
            try:
                status = await loop.run_in_executor(
                    None, lambda: self._exchange.fetch_order(order_id, sym)
                )
                s = status.get("status", "")
                if s in ("closed", "filled"):
                    filled_at = float(status.get("average") or limit_px)
                    logger.info(
                        f"[LIVE] Limit filled @ {filled_at:.2f}  "
                        f"(saved {(0.0005 - 0.0002) * filled_at * qty:.4f} USDT in fees)"
                    )
                    return await self._place_sl_tp_orders(sym, signal, qty)
                if s in ("canceled", "rejected", "expired"):
                    logger.warning(f"[LIVE] Limit order {s} — market fallback")
                    return await self._place_market_entry(signal, qty)
            except Exception:
                pass

            # Chase: after half-timeout, move limit to current mark price
            if not chased and _time.monotonic() >= half_point:
                chased = True
                try:
                    await loop.run_in_executor(
                        None, lambda: self._exchange.cancel_order(order_id, sym)
                    )
                    # Fetch current price and re-post slightly aggressively
                    ticker = await loop.run_in_executor(
                        None, lambda: self._exchange.fetch_ticker(sym)
                    )
                    mid = (float(ticker["bid"]) + float(ticker["ask"])) / 2
                    chase_px = self._round_price(
                        mid + 0.5 if side_in == "buy" else mid - 0.5  # 0.5 USDT inside book
                    )
                    new_order = await loop.run_in_executor(
                        None,
                        lambda: self._exchange.create_order(
                            sym, "limit", side_in, qty, chase_px,
                            params={"timeInForce": "GTC"},
                        ),
                    )
                    order_id = new_order.get("id")
                    logger.info(f"[LIVE] Limit chased to {chase_px}  id={order_id}")
                except Exception as e:
                    logger.warning(f"[LIVE] Chase failed: {e}  → market fallback")
                    return await self._place_market_entry(signal, qty)

        # ── Timeout: cancel and fall back to market ───────────────────────────
        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.cancel_order(order_id, sym)
            )
        except Exception:
            pass
        logger.info(f"[LIVE] Limit not filled in {timeout}s — market fallback")
        return await self._place_market_entry(signal, qty)

    async def check_exit(self, current_price: float) -> Optional[Position]:
        """
        Check SL/TP and trailing stop.
        Paper: simulate fills locally.
        Live : poll exchange for remote fills (SL/TP hit on exchange), then trail.
        """
        if self.position is None or self.position.closed:
            return None
        if config.PAPER_TRADING:
            return self._paper_check_exit(current_price)
        # Live mode ─────────────────────────────────────────────────────────────
        # 1. Sync with exchange: detect if SL/TP was already filled remotely.
        #    This is the CRITICAL fix — without it the bot freezes after any
        #    exchange-side fill (e.g. SL hit while WebSocket was lagging).
        synced = await self._live_sync_position(current_price)
        if synced:
            return synced
        # 2. If position is still open, update trailing stop on the exchange.
        await self._live_check_trail(current_price)
        return None

    def _apply_trail(self, pos: Position, price: float) -> bool:
        """Dispatch trailing-stop logic — mirrors ``backtest._update_trailing_stop``.

        When ``ADAPTIVE_TRAILING_ENABLED`` is ``True`` the position is managed by
        the single continuous tightening funnel (:meth:`_apply_adaptive_trail`);
        otherwise the classic three-stage cascade (:meth:`_apply_classic_trail`)
        runs.  Keeping this dispatch identical to the backtest is the CLAUDE.md
        live/backtest sync contract — before this guard the live path always ran
        the classic cascade while the backtest honoured the flag, so enabling
        adaptive trailing silently diverged the two.

        Returns:
            ``True`` if the SL changed (caller updates the exchange SL order in
            live mode).
        """
        if pos.initial_atr <= 0:
            return False
        if getattr(config, "STEP_TRAILING_ENABLED", False):
            return self._apply_step_trail(pos, price)
        if config.ADAPTIVE_TRAILING_ENABLED:
            return self._apply_adaptive_trail(pos, price)
        return self._apply_classic_trail(pos, price)

    def _apply_step_trail(self, pos: Position, price: float) -> bool:
        """Profit-based STEP trailing — mirrors ``backtest._apply_step_trail`` exactly.

        Monotonic profit-locking ladder: SL steps to break-even once price moves
        ``TRAIL_ACTIVATE_ATR × ATR`` in favour, then ratchets forward by ``1×ATR`` of
        locked profit for every additional ``1×ATR`` of favourable excursion
        (BE → +1ATR → +2ATR → …).  Stateless + ratcheted (SL never retreats), so no
        per-step position state is needed.  ``TRAIL_LOCK/STOP`` are unused.

        Live mode sees only the current mark ``price`` (no intrabar high/low), so
        ``price`` drives the favour calc — identical to the single-price classic
        live trail, and to the backtest under ``bar_high == bar_low == price``.

        Returns:
            ``True`` if the SL moved (caller updates the exchange SL order).
        """
        atr      = pos.initial_atr
        entry    = pos.entry
        activate = config.TRAIL_ACTIVATE_ATR
        is_long  = pos.side == "LONG"

        favour = (price - entry) if is_long else (entry - price)
        if activate <= 0 or favour < activate * atr:
            return False

        step   = getattr(config, "TRAIL_STEP_ATR", 1.0) or 1.0
        steps  = int((favour / atr - activate) / step + 1e-9)
        locked = steps * step * atr
        new_sl = (entry + locked) if is_long else (entry - locked)

        sl_changed = False
        if is_long and new_sl > pos.sl:
            pos.sl = new_sl
            sl_changed = True
        elif not is_long and new_sl < pos.sl:
            pos.sl = new_sl
            sl_changed = True

        if sl_changed:
            logger.info(
                f"Trail STEP — SL → {pos.sl:.2f}  "
                f"(+{favour:.2f} = {favour / atr:.1f}×ATR in favor, "
                f"{steps}×ATR profit locked)"
            )
        return sl_changed

    def _apply_adaptive_trail(self, pos: Position, price: float) -> bool:
        """Single tightening trailing funnel — mirrors ``backtest._apply_adaptive_trail``.

        The trail distance shrinks linearly from ``ATR_SL_MULTIPLIER`` at
        activation to ``ADAPTIVE_TRAIL_MIN_ATR`` as price advances toward the TP
        target, converting break-even exits into profitable trailed exits.

        Live mode sees only the current mark ``price`` (no intrabar high/low), so
        ``price`` drives both the favour check and the running peak — the same
        single-price behaviour as the classic live trail.

        Returns:
            ``True`` if the SL moved (caller updates the exchange SL order).
        """
        atr     = pos.initial_atr
        entry   = pos.entry
        tp      = pos.tp
        is_long = pos.side == "LONG"
        sl_changed = False

        # ── Activation gate — same threshold as classic Stage-1 break-even ────
        if not pos.trail_activated:
            favour = (price - entry) if is_long else (entry - price)
            if config.TRAIL_ACTIVATE_ATR > 0 and favour < config.TRAIL_ACTIVATE_ATR * atr:
                return False  # not yet in sufficient profit to begin trailing
            pos.trail_activated = True
            pos.trail_peak = price
            logger.info(
                f"Trail ADAPT activated — peak={price:.2f}  "
                f"(+{favour:.2f} = {favour / atr:.1f}×ATR in favor)"
            )

        # ── Update running peak ───────────────────────────────────────────────
        if is_long:
            pos.trail_peak = max(pos.trail_peak, price)
        else:
            pos.trail_peak = min(pos.trail_peak, price) if pos.trail_peak > 0 else price
        peak = pos.trail_peak

        # ── Progress toward TP  [0.0 = just activated … 1.0 = peak at TP] ─────
        tp_dist     = abs(tp - entry)
        favour_peak = (peak - entry) if is_long else (entry - peak)
        progress    = max(0.0, min(1.0, favour_peak / tp_dist if tp_dist > 0 else 0.0))

        # ── Adaptive trail distance — linear lerp (wide at BE → tight near TP) ─
        max_trail = config.ATR_SL_MULTIPLIER
        min_trail = config.ADAPTIVE_TRAIL_MIN_ATR
        trail_atr = max_trail + (min_trail - max_trail) * progress

        trail_price = (peak - trail_atr * atr) if is_long else (peak + trail_atr * atr)

        # ── Ratchet — SL moves only in the favourable direction ───────────────
        if is_long and trail_price > pos.sl:
            pos.sl = trail_price
            sl_changed = True
        elif not is_long and trail_price < pos.sl:
            pos.sl = trail_price
            sl_changed = True

        return sl_changed

    def _apply_classic_trail(self, pos: Position, price: float) -> bool:
        """
        Apply the classic three-stage trailing cascade — mirrors
        ``backtest._apply_classic_trail`` exactly.

        Three independent stages (all controlled by config):
          1. Break-even (TRAIL_ACTIVATE_ATR): SL → entry once price moves N×ATR
          2. Profit lock (TRAIL_LOCK_ATR):    SL → entry + 1×ATR after M×ATR move
          3. Dynamic trail (TRAIL_STOP_ATR):  SL trails peak at –N×ATR after BE

        Returns True if SL changed (caller must update exchange SL order in live mode).
        """
        atr = pos.initial_atr
        if atr <= 0:
            return False

        sl_changed = False

        # ── 1. Break-even activation ──────────────────────────────────────────
        if not pos.trail_activated and config.TRAIL_ACTIVATE_ATR > 0:
            favor = (price - pos.entry) if pos.side == "LONG" else (pos.entry - price)
            if favor >= config.TRAIL_ACTIVATE_ATR * atr:
                new_sl = pos.entry
                if (pos.side == "LONG"  and new_sl > pos.sl) or \
                   (pos.side == "SHORT" and new_sl < pos.sl):
                    pos.sl = new_sl
                    sl_changed = True
                pos.trail_activated = True
                pos.trail_peak = price
                logger.info(
                    f"Trail BE — SL → {pos.sl:.2f}  "
                    f"(+{favor:.2f} = {favor/atr:.1f}×ATR in favor)"
                )

        if not pos.trail_activated:
            return sl_changed

        # Update peak tracking
        if pos.side == "LONG":
            pos.trail_peak = max(pos.trail_peak, price)
        else:
            pos.trail_peak = min(pos.trail_peak, price) if pos.trail_peak > 0 else price

        # ── 2. Profit lock (TRAIL_LOCK_ATR) ───────────────────────────────────
        if not pos.lock_activated and config.TRAIL_LOCK_ATR > 0:
            favor = (price - pos.entry) if pos.side == "LONG" else (pos.entry - price)
            if favor >= config.TRAIL_LOCK_ATR * atr:
                new_sl = (pos.entry + atr) if pos.side == "LONG" else (pos.entry - atr)
                if (pos.side == "LONG"  and new_sl > pos.sl) or \
                   (pos.side == "SHORT" and new_sl < pos.sl):
                    pos.sl = new_sl
                    sl_changed = True
                pos.lock_activated = True
                logger.info(f"Trail LOCK — SL → {pos.sl:.2f} (+1×ATR profit locked)")

        # ── 3. Dynamic trailing stop (TRAIL_STOP_ATR) ─────────────────────────
        if config.TRAIL_STOP_ATR > 0:
            if pos.side == "LONG":
                trail_sl = pos.trail_peak - config.TRAIL_STOP_ATR * atr
                if trail_sl > pos.sl:
                    pos.sl = trail_sl
                    sl_changed = True
            else:
                trail_sl = pos.trail_peak + config.TRAIL_STOP_ATR * atr
                if trail_sl < pos.sl:
                    pos.sl = trail_sl
                    sl_changed = True

        return sl_changed

    def _paper_check_exit(self, price: float) -> Optional[Position]:
        """Simulate SL / TP / trail exit in paper mode.

        Applies the full trailing-stop logic, then checks whether ``price``
        has crossed the SL or TP.  On a hit, closes the position, updates
        the balance and consecutive-loss counter, and evaluates daily limits.

        Args:
            price: Current mark price to test against SL / TP levels.

        Returns:
            The closed :class:`Position` if an exit was triggered,
            otherwise ``None``.
        """
        pos = self.position

        self._apply_trail(pos, price)

        hit_sl = hit_tp = False
        if pos.side == "LONG":
            hit_sl = price <= pos.sl
            hit_tp = price >= pos.tp
        else:
            hit_sl = price >= pos.sl
            hit_tp = price <= pos.tp

        if hit_sl or hit_tp:
            exit_price = pos.sl if hit_sl else pos.tp
            multiplier = 1 if pos.side == "LONG" else -1
            pos.pnl = (exit_price - pos.entry) * multiplier * pos.qty
            pos.closed = True
            # "BE" covers any trail-activated stop (break-even, lock, or dynamic trail)
            pos.close_reason = "TP" if hit_tp else ("BE" if pos.trail_activated else "SL")

            self.balance += pos.pnl
            self.trade_log.append(pos)
            self.position = None

            # Update consecutive-loss streak
            if pos.close_reason == "SL":
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0   # TP or BE resets the streak

            logger.info(
                f"[PAPER] {pos.close_reason}  PnL={pos.pnl:+.2f}  "
                f"Balance={self.balance:.2f}  Day PnL={self.day_pnl:+.2f}  "
                f"consec_SL={self.consecutive_losses}"
            )
            self._check_daily_limits()
            return pos
        return None

    async def _live_sync_position(self, current_price: float) -> Optional[Position]:
        """
        Poll the exchange to detect when an SL/TP order was filled remotely.

        Why this is critical
        ─────────────────────
        Exchange SL/TP orders fill server-side. If the WebSocket drops for even a
        few seconds around the fill time we never see the fill event, so
        self.position stays non-None and the bot never opens another trade.

        This method polls every LIVE_POSITION_SYNC_SECS seconds. When it finds
        that the exchange position is gone it reconstructs the close from the
        most recent trade fill, updates balance from the exchange, and returns
        the closed Position so callers can log it and apply post-SL cooldown.
        """
        now = _time.monotonic()
        if now - self._last_sync_time < config.LIVE_POSITION_SYNC_SECS:
            return None
        self._last_sync_time = now

        if self.position is None or not self._exchange:
            return None

        loop = asyncio.get_event_loop()
        try:
            positions = await loop.run_in_executor(
                None,
                lambda: self._exchange.fetch_positions([self.symbol_ccxt]),
            )
            exchange_qty = 0.0
            for p in positions:
                q = abs(float(p.get("contracts") or 0))
                if q >= config.MIN_ORDER_QTY:
                    exchange_qty = q
                    break

            if exchange_qty >= config.MIN_ORDER_QTY:
                return None  # still open on exchange — nothing to do

            # ── Position is gone: reconstruct the close ───────────────────────
            pos = self.position
            exit_price = current_price
            close_reason = "SL"       # pessimistic default

            # Try to find the actual fill price from recent trades
            try:
                my_trades = await loop.run_in_executor(
                    None,
                    lambda: self._exchange.fetch_my_trades(self.symbol_ccxt, limit=5),
                )
                for t in reversed(my_trades):
                    fill_px = float(t.get("price") or 0)
                    if fill_px <= 0:
                        continue
                    exit_price = fill_px
                    # Classify: TP is far from entry in winning direction; SL is opposite
                    if pos.side == "LONG":
                        if fill_px >= pos.tp * 0.995:
                            close_reason = "TP"
                        elif pos.trail_activated and fill_px > pos.entry * 0.999:
                            close_reason = "BE"
                        else:
                            close_reason = "SL"
                    else:
                        if fill_px <= pos.tp * 1.005:
                            close_reason = "TP"
                        elif pos.trail_activated and fill_px < pos.entry * 1.001:
                            close_reason = "BE"
                        else:
                            close_reason = "SL"
                    break
            except Exception:
                # Fallback: infer from price relative to entry
                if pos.side == "LONG":
                    close_reason = "TP" if current_price > pos.entry else "SL"
                else:
                    close_reason = "TP" if current_price < pos.entry else "SL"
                if pos.trail_activated and close_reason == "SL":
                    close_reason = "BE"

            # Refresh balance from exchange (reflects real fee-adjusted PnL)
            try:
                self.balance = await self.fetch_balance()
                pos.pnl = self.balance - self.day_start_balance   # rough day-pnl proxy
            except Exception:
                mult = 1 if pos.side == "LONG" else -1
                pos.pnl = (exit_price - pos.entry) * mult * pos.qty
                self.balance += pos.pnl

            pos.closed = True
            pos.close_reason = close_reason
            self.trade_log.append(pos)
            self.position = None

            # Update consecutive-loss streak
            if close_reason == "SL":
                self.consecutive_losses += 1
            else:
                self.consecutive_losses = 0

            logger.info(
                f"[LIVE SYNC] Exchange closed position [{close_reason}]  "
                f"exit≈{exit_price:.2f}  balance={self.balance:.2f}  "
                f"day_pnl={self.day_pnl:+.2f}  consec_SL={self.consecutive_losses}"
            )
            self._check_daily_limits()
            return pos

        except Exception as e:
            logger.debug(f"[LIVE] Position sync poll error: {e}")
            return None

    async def _live_check_trail(self, price: float) -> None:
        """Apply trailing-stop logic and update the exchange SL order if it changed.

        Applies all three trail stages (break-even, profit-lock, dynamic trail)
        via :meth:`_apply_trail`.  When the SL moves, the existing exchange
        stop-market order is cancelled and a new one is placed at the updated
        price.

        One-tick dedupe: this runs on every markPrice tick (~1/s), so with a
        dynamic trail (``TRAIL_STOP_ATR > 0``) the raw float SL nudges up on
        almost every favourable tick.  The resting exchange order, however, is
        always at ``_round_price(previous pos.sl)`` — so if the new SL rounds to
        the *same* tick, the exchange order is already correct.  We skip the
        cancel+replace in that case, eliminating both the needless API round-trip
        and the brief window where the position sits unprotected between the
        cancel and the re-place.

        Args:
            price: Current mark price used to evaluate trail thresholds.
        """
        pos = self.position
        sl_before  = pos.sl
        sl_changed = self._apply_trail(pos, price)

        if not sl_changed or not pos.sl_order_id:
            return

        loop     = asyncio.get_event_loop()
        sym      = self.symbol_ccxt
        sl_side  = "sell" if pos.side == "LONG" else "buy"
        new_sl_p = self._round_price(pos.sl)

        # Skip when the rounded stop price is unchanged from the resting order.
        if abs(new_sl_p - self._round_price(sl_before)) < config.PRICE_TICK / 2:
            return

        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.cancel_order(pos.sl_order_id, sym)
            )
            new_order = await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    sym, "stop_market", sl_side, pos.qty,
                    params={"stopPrice": new_sl_p, "reduceOnly": True},
                ),
            )
            pos.sl_order_id = new_order.get("id") if new_order else None
            logger.info(f"[LIVE] SL updated: {sl_before:.2f} → {pos.sl:.2f}  id={pos.sl_order_id}")
        except Exception as e:
            logger.error(f"[LIVE] Failed to update SL order on trail: {e}")

    def stats(self) -> Dict:
        """Return a summary statistics dictionary for the current session.

        Returns:
            Dictionary with keys:
              ``trades``, ``wins``, ``tp_exits``, ``sl_exits``, ``be_exits``,
              ``win_rate`` (%), ``total_pnl``, ``balance``, ``day_pnl``,
              ``daily_halted``, ``days_profit_hit``, ``days_loss_hit``,
              ``consecutive_losses``.
        """
        tp_trades = [p for p in self.trade_log if p.close_reason == "TP"]
        sl_trades = [p for p in self.trade_log if p.close_reason == "SL"]
        be_trades = [p for p in self.trade_log if p.close_reason == "BE"]
        wins = [p for p in self.trade_log if p.pnl > 0]
        total_pnl = sum(p.pnl for p in self.trade_log)
        win_rate = len(wins) / len(self.trade_log) * 100 if self.trade_log else 0
        return {
            "trades": len(self.trade_log),
            "wins": len(wins),
            "tp_exits": len(tp_trades),
            "sl_exits": len(sl_trades),
            "be_exits": len(be_trades),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "balance": self.balance,
            "day_pnl": self.day_pnl,
            "daily_halted": self.daily_halted,
            "days_profit_hit": self.days_profit_hit,
            "days_loss_hit": self.days_loss_hit,
            "consecutive_losses": self.consecutive_losses,
        }
