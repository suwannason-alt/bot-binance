"""
Order execution layer.
Paper mode simulates fills; live mode uses ccxt Binance Futures.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

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
    open_time: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    pnl: float = 0.0
    closed: bool = False
    close_reason: str = ""


class Trader:
    def __init__(self):
        self.position: Optional[Position] = None
        self.balance: float = 1000.0  # paper balance in USDT
        self.trade_log: list[Position] = []
        self._exchange = None

        if not config.PAPER_TRADING:
            self._init_live()

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

    async def fetch_balance(self) -> float:
        if config.PAPER_TRADING:
            return self.balance
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, self._exchange.fetch_balance)
        return float(data["USDT"]["free"])

    async def open_position(self, signal: Signal) -> Optional[Position]:
        if self.position is not None:
            logger.info("Position already open — skipping new signal")
            return None

        balance = await self.fetch_balance()
        qty = position_size_usdt(balance, signal.entry, signal.sl)
        if qty <= 0:
            logger.warning("Computed qty=0, skipping trade")
            return None

        logger.info(
            f"[{'PAPER' if config.PAPER_TRADING else 'LIVE'}] "
            f"Opening {signal.side} {qty} {config.SYMBOL} @ {signal.entry:.2f} "
            f"SL={signal.sl:.2f} TP={signal.tp:.2f} | {signal.reason}"
        )

        if not config.PAPER_TRADING:
            await self._place_live_order(signal, qty)

        self.position = Position(
            side=signal.side,
            entry=signal.entry,
            qty=qty,
            sl=signal.sl,
            tp=signal.tp,
        )
        return self.position

    async def _place_live_order(self, signal: Signal, qty: float):
        loop = asyncio.get_event_loop()
        sym = config.SYMBOL_CCXT
        side_ccxt = "buy" if signal.side == "LONG" else "sell"
        sl_side = "sell" if signal.side == "LONG" else "buy"

        # Set leverage
        await loop.run_in_executor(
            None,
            lambda: self._exchange.set_leverage(config.LEVERAGE, sym),
        )

        # Market entry
        await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(sym, "market", side_ccxt, qty),
        )

        # Stop-loss order
        await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(
                sym,
                "stop_market",
                sl_side,
                qty,
                params={"stopPrice": signal.sl, "reduceOnly": True},
            ),
        )

        # Take-profit order
        await loop.run_in_executor(
            None,
            lambda: self._exchange.create_order(
                sym,
                "take_profit_market",
                sl_side,
                qty,
                params={"stopPrice": signal.tp, "reduceOnly": True},
            ),
        )

    async def check_exit(self, current_price: float) -> Optional[Position]:
        """Check if SL or TP is hit (paper mode). Live mode uses exchange orders."""
        if self.position is None or self.position.closed:
            return None
        if config.PAPER_TRADING:
            return self._paper_check_exit(current_price)
        return None

    def _paper_check_exit(self, price: float) -> Optional[Position]:
        pos = self.position
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
            pos.close_reason = "SL" if hit_sl else "TP"

            self.balance += pos.pnl
            self.trade_log.append(pos)
            self.position = None

            logger.info(
                f"[PAPER] Position closed via {pos.close_reason} "
                f"PnL={pos.pnl:+.2f} USDT  Balance={self.balance:.2f}"
            )
            return pos
        return None

    def stats(self) -> dict:
        wins = [p for p in self.trade_log if p.pnl > 0]
        losses = [p for p in self.trade_log if p.pnl <= 0]
        total_pnl = sum(p.pnl for p in self.trade_log)
        win_rate = len(wins) / len(self.trade_log) * 100 if self.trade_log else 0
        return {
            "trades": len(self.trade_log),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "balance": self.balance,
        }
