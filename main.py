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

# ── State ──────────────────────────────────────────────────────────────────────
state = MarketState()
trader = Trader()

_bars_since_last_1h: int = 9999   # 1H bars since last trade (for cooldown)


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def on_1h_close(mstate: MarketState):
    """Called every time a 1H candle closes — main strategy evaluation."""
    global _bars_since_last_1h

    # Daily reset: checks if UTC date has changed
    trader.reset_day()

    _bars_since_last_1h += 1

    if trader.daily_halted:
        logger.info(
            f"1H close — daily limit reached, skipping  "
            f"day_pnl={trader.day_pnl:+.2f}  balance={trader.balance:.2f}"
        )
        return

    sig = strategy.evaluate_1h_live(mstate, _bars_since_last_1h)
    if sig:
        logger.info(f"SIGNAL  {sig.reason}")
        logger.info(
            f"  Entry={sig.entry:.2f}  SL={sig.sl:.2f} ({sig.sl_pct:.2f}%)  "
            f"TP={sig.tp:.2f} ({sig.tp_pct:.2f}%)  RR={sig.rr_ratio:.2f}"
        )
        pos = await trader.open_position(sig)
        if pos:
            _bars_since_last_1h = 0
            logger.info(
                f"  Position opened  qty={pos.qty:.6f}  "
                f"balance={trader.balance:.2f} USDT  day_pnl={trader.day_pnl:+.2f}"
            )
    else:
        logger.debug(
            f"No 1H signal  buf={mstate.buf_1h.count}  mark={mstate.mark_price:.2f}  "
            f"day_pnl={trader.day_pnl:+.2f}  bars_since={_bars_since_last_1h}"
        )


async def on_5m_close(mstate: MarketState):
    """Called every time a 5M candle closes — only used for debug logging."""
    pass


async def on_tick(mstate: MarketState, price: float):
    """Called on every mark-price update — used for SL/TP/trail monitoring."""
    closed = await trader.check_exit(price)
    if closed:
        stats = trader.stats()
        logger.info(
            f"Stats  trades={stats['trades']}  win_rate={stats['win_rate']:.1f}%  "
            f"(TP={stats['tp_exits']} SL={stats['sl_exits']} BE={stats['be_exits']})  "
            f"total_pnl={stats['total_pnl']:+.2f}  balance={stats['balance']:.2f}  "
            f"day_pnl={stats['day_pnl']:+.2f}"
        )


# ── Bootstrap ──────────────────────────────────────────────────────────────────

ws = BinanceWS(
    state=state,
    on_5m_close=on_5m_close,
    on_1h_close=on_1h_close,
    on_tick=on_tick,
)


async def main():
    mode = "PAPER" if config.PAPER_TRADING else "LIVE"
    risk_label = (f"RISK_USD=${config.RISK_USD:.0f}" if config.RISK_USD > 0
                  else f"RISK_PCT={config.RISK_PERCENT}%")
    profit_label = (f"${config.DAILY_PROFIT_TARGET_USD:.0f}" if config.DAILY_PROFIT_TARGET_USD > 0
                    else f"{config.DAILY_PROFIT_TARGET_PCT*100:.0f}%")
    loss_label = (f"${config.DAILY_LOSS_LIMIT_USD:.0f}" if config.DAILY_LOSS_LIMIT_USD > 0
                  else f"{config.DAILY_LOSS_LIMIT_PCT*100:.0f}%")

    logger.info(f"Starting trading bot [{mode}] symbol={config.SYMBOL}")
    logger.info(
        f"Strategy: 1H breakout  {risk_label}  Leverage={config.LEVERAGE}x  "
        f"SL={config.ATR_SL_MULTIPLIER}×ATR  TP={config.ATR_TP_MULTIPLIER}×ATR"
    )
    logger.info(
        f"Daily targets: profit={profit_label}  loss_limit={loss_label}  "
        f"BP={config.BREAKOUT_PERIOD}bars  cooldown={config.TRADE_COOLDOWN_1H}bars"
    )

    loop = asyncio.get_running_loop()

    def _shutdown(sig):
        logger.info(f"Received {sig.name} — shutting down…")
        ws.stop()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, _shutdown, s)

    await trader.initialize()

    try:
        await ws.run()
    except asyncio.CancelledError:
        pass
    finally:
        stats = trader.stats()
        logger.info(
            f"Session ended  trades={stats['trades']}  win_rate={stats['win_rate']:.1f}%  "
            f"total_pnl={stats['total_pnl']:+.2f}  balance={stats['balance']:.2f}"
        )
        logger.info(
            f"Daily summary: profit_days={stats['days_profit_hit']}  "
            f"loss_days={stats['days_loss_hit']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
