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
    trail_activated: bool = False   # True once price moved TRAIL_ACTIVATE_ATR×ATR in favor
    initial_atr: float = 0.0       # ATR at entry — used to compute trail threshold
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
        """Fetch live balance on startup (no-op in paper mode)."""
        if not config.PAPER_TRADING:
            self.balance = await self.fetch_balance()
            self.day_start_balance = self.balance
            logger.info(f"Live balance fetched: {self.balance:.2f} USDT")

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
        qty = position_size_usdt(balance, signal.entry, signal.sl)
        if qty <= 0:
            logger.warning("Computed qty=0, skipping trade")
            return None

        risk_label = (f"RISK_USD=${config.RISK_USD:.0f}" if config.RISK_USD > 0
                      else f"RISK={config.RISK_PERCENT}%")
        logger.info(
            f"[{'PAPER' if config.PAPER_TRADING else 'LIVE'}] "
            f"Opening {signal.side} {qty:.6f} {config.SYMBOL} @ {signal.entry:.2f} "
            f"SL={signal.sl:.2f} TP={signal.tp:.2f} | {risk_label} | {signal.reason}"
        )

        sl_order_id = None
        if not config.PAPER_TRADING:
            sl_order_id = await self._place_live_order(signal, qty)

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
        """Place market + SL + TP orders. Returns the SL order ID."""
        loop = asyncio.get_event_loop()
        sym = config.SYMBOL_CCXT
        side_ccxt = "buy" if signal.side == "LONG" else "sell"
        sl_side = "sell" if signal.side == "LONG" else "buy"

        await loop.run_in_executor(
            None,
            lambda: self._exchange.set_leverage(config.LEVERAGE, sym),
        )
        await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(sym, "market", side_ccxt, qty),
        )
        sl_order = await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(
                sym, "stop_market", sl_side, qty,
                params={"stopPrice": signal.sl, "reduceOnly": True},
            ),
        )
        await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(
                sym, "take_profit_market", sl_side, qty,
                params={"stopPrice": signal.tp, "reduceOnly": True},
            ),
        )
        return sl_order.get("id") if sl_order else None

    async def check_exit(self, current_price: float) -> Optional[Position]:
        """Check SL/TP and trailing stop. Paper: simulate fills. Live: update SL order on trail."""
        if self.position is None or self.position.closed:
            return None
        if config.PAPER_TRADING:
            return self._paper_check_exit(current_price)
        # Live mode: exchange handles SL/TP fills; we only need to update SL on trail activation
        await self._live_check_trail(current_price)
        return None

    def _apply_trail(self, pos: Position, price: float):
        """Move SL to break-even if price has moved TRAIL_ACTIVATE_ATR×ATR in favor."""
        if pos.trail_activated or config.TRAIL_ACTIVATE_ATR <= 0 or pos.initial_atr <= 0:
            return
        favor = (price - pos.entry) if pos.side == "LONG" else (pos.entry - price)
        if favor >= config.TRAIL_ACTIVATE_ATR * pos.initial_atr:
            pos.trail_activated = True
            pos.sl = pos.entry
            logger.info(
                f"Trail activated — SL → BE {pos.entry:.2f}  "
                f"(moved {favor:.2f} = {favor / pos.initial_atr:.1f}×ATR in favor)"
            )

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
        """Update the exchange SL order when trailing stop activates (live mode only)."""
        pos = self.position
        was_activated = pos.trail_activated
        self._apply_trail(pos, price)
        if pos.trail_activated and not was_activated and pos.sl_order_id:
            loop = asyncio.get_event_loop()
            sym = config.SYMBOL_CCXT
            sl_side = "sell" if pos.side == "LONG" else "buy"
            try:
                await loop.run_in_executor(
                    None,
                    lambda: self._exchange.cancel_order(pos.sl_order_id, sym),
                )
                new_sl = await loop.run_in_executor(
                    None,
                    lambda: self._exchange.create_order(
                        sym, "stop_market", sl_side, pos.qty,
                        params={"stopPrice": pos.entry, "reduceOnly": True},
                    ),
                )
                pos.sl_order_id = new_sl.get("id") if new_sl else None
                logger.info(f"[LIVE] SL order updated to BE {pos.entry:.2f}")
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
