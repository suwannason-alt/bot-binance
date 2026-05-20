"""
Binance Futures Trading Bot — entry point
Usage:
  python main.py
"""
import asyncio
import logging
import signal
import sys

import config
import strategy
from data_store import MarketState
from trader import Trader
from ws_client import BinanceWS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def on_5m_close(state: MarketState):
    """Called every time a 5M candle closes — main strategy loop."""
    sig = strategy.evaluate(state)
    if sig:
        logger.info(f"SIGNAL  {sig.reason}")
        logger.info(
            f"  Entry={sig.entry:.2f}  SL={sig.sl:.2f} ({sig.sl_pct:.2f}%)  "
            f"TP={sig.tp:.2f} ({sig.tp_pct:.2f}%)  RR={sig.rr_ratio:.2f}"
        )
        pos = await trader.open_position(sig)
        if pos:
            logger.info(
                f"  Position opened  qty={pos.qty}  "
                f"balance={trader.balance:.2f} USDT"
            )
    else:
        logger.debug(
            f"No signal  5m_buf={state.buf_5m.count}  1h_buf={state.buf_1h.count}  "
            f"mark={state.mark_price:.2f}"
        )


async def on_1h_close(state: MarketState):
    """Optional hook for higher-timeframe events."""
    logger.info(f"1H candle closed  mark={state.mark_price:.2f}")


async def on_tick(state: MarketState, price: float):
    """Called on every mark-price update — used for SL/TP monitoring (paper)."""
    closed = await trader.check_exit(price)
    if closed:
        stats = trader.stats()
        logger.info(
            f"Stats  trades={stats['trades']}  "
            f"win_rate={stats['win_rate']:.1f}%  "
            f"total_pnl={stats['total_pnl']:+.2f}  "
            f"balance={stats['balance']:.2f}"
        )


# ── Bootstrap ──────────────────────────────────────────────────────────────────

state = MarketState()
trader = Trader()
ws = BinanceWS(
    state=state,
    on_5m_close=on_5m_close,
    on_1h_close=on_1h_close,
    on_tick=on_tick,
)


async def main():
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    logger.info(f"Starting trading bot [{mode}] symbol={config.SYMBOL}")
    logger.info(
        f"Risk={config.RISK_PERCENT}%  Leverage={config.LEVERAGE}x  "
        f"SL_mult={config.ATR_SL_MULTIPLIER}  TP_mult={config.ATR_TP_MULTIPLIER}"
    )

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        logger.info(f"Received {sig.name} — shutting down…")
        ws.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _shutdown, s)

    try:
        await ws.run()
    except asyncio.CancelledError:
        pass
    finally:
        stats = trader.stats()
        logger.info(
            f"Session ended  trades={stats['trades']}  "
            f"win_rate={stats['win_rate']:.1f}%  "
            f"total_pnl={stats['total_pnl']:+.2f}  "
            f"balance={stats['balance']:.2f}"
        )


if __name__ == "__main__":
    asyncio.run(main())
