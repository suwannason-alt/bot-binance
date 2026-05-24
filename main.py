"""
Binance Futures Trading Bot — entry point
Usage:
  python main.py
"""
import asyncio
import logging
import signal
import sys
import time

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
_last_heartbeat: float   = 0.0    # monotonic time of last heartbeat log


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def on_1h_close(mstate: MarketState):
    """Called every time a 1H candle closes — main strategy evaluation."""
    global _bars_since_last_1h

    # Daily reset (idempotent — safe to call here and in on_tick)
    trader.reset_day()

    _bars_since_last_1h += 1

    if trader.daily_halted:
        logger.info(
            f"1H close — daily limit reached, skipping  "
            f"day_pnl={trader.day_pnl:+.2f}  balance={trader.balance:.2f}"
        )
        return

    # ── Funding rate gate ─────────────────────────────────────────────────────
    # Binance settles funding every 8 h. Extreme rates erode profit quickly.
    if config.FUNDING_RATE_MAX > 0:
        fr = abs(mstate.funding_rate)
        if fr > config.FUNDING_RATE_MAX:
            logger.info(
                f"1H close — funding rate {fr:.4%} > max {config.FUNDING_RATE_MAX:.4%}  "
                f"→ skipping entry this bar"
            )
            return

    sig = strategy.evaluate_1h_live(mstate, _bars_since_last_1h)
    if sig:
        logger.info(f"SIGNAL  {sig.reason}")
        logger.info(
            f"  Entry={sig.entry:.2f}  SL={sig.sl:.2f} ({sig.sl_pct:.2f}%)  "
            f"TP={sig.tp:.2f} ({sig.tp_pct:.2f}%)  RR={sig.rr_ratio:.2f}  "
            f"funding={mstate.funding_rate:.4%}"
        )
        pos = await trader.open_position(sig)
        if pos:
            _bars_since_last_1h = 0
            logger.info(
                f"  Position opened  qty={pos.qty:.4f}  "
                f"balance={trader.balance:.2f} USDT  day_pnl={trader.day_pnl:+.2f}"
            )
    else:
        logger.debug(
            f"No 1H signal  buf={mstate.buf_1h.count}  mark={mstate.mark_price:.2f}  "
            f"funding={mstate.funding_rate:.4%}  day_pnl={trader.day_pnl:+.2f}  "
            f"bars_since={_bars_since_last_1h}"
        )


async def on_5m_close(mstate: MarketState):
    """Called every time a 5M candle closes — only used for debug logging."""
    pass


async def on_tick(mstate: MarketState, price: float):
    """Called on every mark-price update — SL/TP/trail monitoring + housekeeping."""
    global _last_heartbeat

    # Daily reset on every tick so it fires at midnight even between 1H bars
    trader.reset_day()

    closed = await trader.check_exit(price)
    if closed:
        stats = trader.stats()
        logger.info(
            f"Trade closed [{closed.close_reason}]  PnL={closed.pnl:+.2f}  "
            f"balance={stats['balance']:.2f}  day_pnl={stats['day_pnl']:+.2f}  "
            f"trades={stats['trades']}  win_rate={stats['win_rate']:.1f}%  "
            f"(TP={stats['tp_exits']} SL={stats['sl_exits']} BE={stats['be_exits']})"
        )

    # ── Heartbeat — log status every HEARTBEAT_INTERVAL seconds ──────────────
    now = time.monotonic()
    if now - _last_heartbeat >= config.HEARTBEAT_INTERVAL:
        _last_heartbeat = now
        stats = trader.stats()
        pos_str = (
            f"  pos={trader.position.side}@{trader.position.entry:.0f}"
            f" SL={trader.position.sl:.0f}"
            if trader.position else "  no_pos"
        )
        logger.info(
            f"♥ mark={price:.2f}  balance={trader.balance:.2f}  "
            f"day_pnl={trader.day_pnl:+.2f}  trades={stats['trades']}"
            f"{pos_str}  funding={mstate.funding_rate:.4%}"
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

    sizing_label = (
        f"ORDER_BAL=${config.ORDER_BALANCE_USD:.0f}×{config.LEVERAGE}lev"
        if config.ORDER_BALANCE_USD > 0 else risk_label
    )
    logger.info(f"Starting trading bot [{mode}] symbol={config.SYMBOL}")
    logger.info(
        f"Strategy: 1H breakout  {sizing_label}  Leverage={config.LEVERAGE}x  "
        f"SL={config.ATR_SL_MULTIPLIER}×ATR  TP={config.ATR_TP_MULTIPLIER}×ATR  "
        f"ADX≥{config.ADX_MIN}  BP={config.BREAKOUT_PERIOD}bars"
    )
    logger.info(
        f"Daily targets: profit={profit_label}  loss_limit={loss_label}  "
        f"cooldown={config.TRADE_COOLDOWN_1H}bars  "
        f"funding_max={config.FUNDING_RATE_MAX:.4%}  "
        f"trail=BE{config.TRAIL_ACTIVATE_ATR}/LOCK{config.TRAIL_LOCK_ATR}/STOP{config.TRAIL_STOP_ATR}"
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
