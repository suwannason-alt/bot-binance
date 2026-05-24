"""
Order execution layer.
Paper mode simulates fills; live mode uses ccxt Binance Futures.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

import config
from strategy import Signal, position_size_usdt

logger = logging.getLogger("trader")


@dataclass
class Position:
    side: str           # "LONG" | "SHORT"
    entry: float
    qty: float
    sl: float
    tp: float
    open_time: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    pnl: float = 0.0
    closed: bool = False
    close_reason: str = ""
    trail_activated: bool = False   # True once BE activated (price moved TRAIL_ACTIVATE_ATR×ATR)
    lock_activated: bool = False    # True once profit lock activated (TRAIL_LOCK_ATR)
    trail_peak: float = 0.0        # highest (LONG) / lowest (SHORT) price seen after BE
    initial_atr: float = 0.0       # ATR at entry — used for all trail thresholds
    sl_order_id: Optional[str] = None  # exchange SL order ID (live mode only)


class Trader:
    def __init__(self):
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

        if not config.PAPER_TRADING:
            self._init_live()

    # ── Daily helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _utc_date() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def reset_day(self):
        """Call once per day (at UTC midnight) to reset daily tracking."""
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
        logger.info(
            f"New day {today} — balance={self.balance:.2f}  "
            f"all-time profit_days={self.days_profit_hit}  loss_days={self.days_loss_hit}"
        )

    @property
    def day_pnl(self) -> float:
        return self.balance - self.day_start_balance

    def _check_daily_limits(self):
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

    def _init_live(self):
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

    async def initialize(self):
        """Fetch balance and recover any existing position on startup."""
        if not config.PAPER_TRADING:
            self.balance = await self.fetch_balance()
            self.day_start_balance = self.balance
            logger.info(f"Live balance: {self.balance:.2f} USDT")
            await self._recover_position()

    async def _recover_position(self):
        """
        On startup / reconnect, sync internal state with the exchange.
        If an open position exists (e.g. after a crash), rebuild a Position
        object so SL/TP monitoring continues without missing an exit.
        """
        loop = asyncio.get_event_loop()
        try:
            positions = await loop.run_in_executor(
                None,
                lambda: self._exchange.fetch_positions([config.SYMBOL_CCXT]),
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
                        lambda: self._exchange.fetch_open_orders(config.SYMBOL_CCXT),
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
        if config.PAPER_TRADING:
            return self.balance
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._exchange.fetch_balance)
        return float(data["USDT"]["free"])

    # ── Trade execution ───────────────────────────────────────────────────────

    async def open_position(self, signal: Signal) -> Optional[Position]:
        if self.position is not None:
            logger.debug("Position already open — skipping new signal")
            return None
        if self.daily_halted:
            logger.debug("Daily limit reached — skipping new signal")
            return None

        balance = await self.fetch_balance()
        raw_qty = position_size_usdt(balance, signal.entry, signal.sl)
        qty     = self._round_qty(raw_qty)

        if qty < config.MIN_ORDER_QTY:
            logger.warning(
                f"Computed qty {raw_qty:.6f} → rounded {qty:.6f} < MIN {config.MIN_ORDER_QTY} "
                f"— skipping trade (balance too low or ATR too wide)"
            )
            return None

        risk_label = (
            f"ORDER_BAL=${config.ORDER_BALANCE_USD:.0f}×{config.LEVERAGE}lev"
            if config.ORDER_BALANCE_USD > 0
            else (f"RISK_USD=${config.RISK_USD:.0f}" if config.RISK_USD > 0
                  else f"RISK={config.RISK_PERCENT}%")
        )
        logger.info(
            f"[{'PAPER' if config.PAPER_TRADING else 'LIVE'}] "
            f"Opening {signal.side} {qty:.4f} {config.SYMBOL} @ {signal.entry:.2f}  "
            f"SL={signal.sl:.2f}  TP={signal.tp:.2f}  {risk_label}  {signal.reason}"
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
        Place market entry + SL + TP orders on Binance Futures.

        Returns the SL order ID on success.
        Returns None if entry failed (no position opened) or if SL placement
        failed after entry (emergency market-close is triggered automatically).
        """
        loop = asyncio.get_event_loop()
        sym      = config.SYMBOL_CCXT
        side_in  = "buy"  if signal.side == "LONG" else "sell"
        side_out = "sell" if signal.side == "LONG" else "buy"
        sl_price = self._round_price(signal.sl)
        tp_price = self._round_price(signal.tp)

        # ── Set leverage (non-fatal if exchange rejects repeated calls) ──────
        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.set_leverage(config.LEVERAGE, sym)
            )
        except Exception as e:
            logger.warning(f"[LIVE] set_leverage warning (continuing): {e}")

        # ── Market entry ──────────────────────────────────────────────────────
        try:
            await loop.run_in_executor(
                None, lambda: self._exchange.create_order(sym, "market", side_in, qty)
            )
            logger.info(f"[LIVE] Market {signal.side} {qty:.4f} filled")
        except Exception as e:
            logger.error(f"[LIVE] Market entry failed — no position opened: {e}")
            return None  # entry never happened; caller will abort

        # ── Stop-loss order (CRITICAL — if this fails, emergency-close) ──────
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
            logger.info(f"[LIVE] SL order placed @ {sl_price}  id={sl_order_id}")
        except Exception as e:
            logger.error(
                f"[LIVE] SL placement failed: {e}  "
                f"→ EMERGENCY CLOSE to avoid unprotected position"
            )
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        sym, "market", side_out, qty,
                        params={"reduceOnly": True},
                    ),
                )
                logger.info("[LIVE] Emergency close executed successfully")
            except Exception as e2:
                logger.error(f"[LIVE] Emergency close ALSO failed: {e2} — manual intervention required!")
            return None  # signal to caller: position was closed

        # ── Take-profit order (best-effort; exchange also manages it) ────────
        try:
            await loop.run_in_executor(
                None,
                lambda: self._exchange.create_order(
                    sym, "take_profit_market", side_out, qty,
                    params={"stopPrice": tp_price, "reduceOnly": True},
                ),
            )
            logger.info(f"[LIVE] TP order placed @ {tp_price}")
        except Exception as e:
            logger.warning(f"[LIVE] TP order failed (non-critical, SL still active): {e}")

        return sl_order_id

    async def check_exit(self, current_price: float) -> Optional[Position]:
        """Check SL/TP and trailing stop. Paper: simulate fills. Live: update SL order on trail."""
        if self.position is None or self.position.closed:
            return None
        if config.PAPER_TRADING:
            return self._paper_check_exit(current_price)
        # Live mode: exchange handles SL/TP fills; we only need to update SL on trail activation
        await self._live_check_trail(current_price)
        return None

    def _apply_trail(self, pos: Position, price: float) -> bool:
        """
        Apply full trailing-stop logic — mirrors backtest behaviour exactly.

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

            logger.info(
                f"[PAPER] {pos.close_reason}  PnL={pos.pnl:+.2f}  "
                f"Balance={self.balance:.2f}  Day PnL={self.day_pnl:+.2f}"
            )
            self._check_daily_limits()
            return pos
        return None

    async def _live_check_trail(self, price: float):
        """
        Apply full trail logic and, if SL changed, cancel-replace the exchange SL order.
        Handles break-even, profit-lock, and dynamic trail — matches backtest exactly.
        """
        pos = self.position
        sl_before  = pos.sl
        sl_changed = self._apply_trail(pos, price)

        if not sl_changed or not pos.sl_order_id:
            return

        loop     = asyncio.get_event_loop()
        sym      = config.SYMBOL_CCXT
        sl_side  = "sell" if pos.side == "LONG" else "buy"
        new_sl_p = self._round_price(pos.sl)

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

    def stats(self) -> dict:
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
        }
