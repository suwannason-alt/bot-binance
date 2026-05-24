"""
Binance Futures Trading Bot — entry point.

Wires together the WebSocket client, strategy evaluator, and order executor
into a single async event loop.  Three callbacks are registered:

  ``on_1h_close``  — fired on each closed 1H candle; runs the 1H breakout
                     evaluator and opens a position when conditions are met.
  ``on_5m_close``  — fired on each closed 5M candle (currently a no-op stub).
  ``on_tick``      — fired on every mark-price update; checks SL/TP/trail
                     exits and emits the periodic heartbeat log.

Usage::

    python main.py
"""
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone

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

async def on_1h_close(mstate: MarketState) -> None:
    """Evaluate the 1H breakout strategy on each closed 1H candle.

    Applies all pre-entry guards (daily limits, session window, consecutive-loss
    circuit breaker, funding rate) before calling the strategy evaluator.
    Opens a position when a valid signal is found.

    Args:
        mstate: Current :class:`~data_store.MarketState` with updated buffers.
    """
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

    # ── Session time filter ───────────────────────────────────────────────────
    # Only open positions within configured UTC hours (0,0 = 24/7).
    s_start = config.SESSION_FILTER_START_UTC
    s_end   = config.SESSION_FILTER_END_UTC
    if s_start != 0 or s_end != 0:
        hour = datetime.now(timezone.utc).hour
        if s_start < s_end:
            in_session = s_start <= hour < s_end      # e.g. 7-22: normal window
        else:
            in_session = hour >= s_start or hour < s_end  # e.g. 22-6: wraps midnight
        if not in_session:
            logger.debug(
                f"1H close UTC {hour:02d}h — outside session window "
                f"[{s_start:02d}–{s_end:02d}) → skip"
            )
            return

    # ── Consecutive-loss circuit breaker ──────────────────────────────────────
    # Halt new entries today if MAX_CONSECUTIVE_LOSSES SLs in a row.
    # (open_position also checks, but logging here gives a clear 1H-bar message.)
    if config.MAX_CONSECUTIVE_LOSSES > 0:
        cl = trader.consecutive_losses
        if cl >= config.MAX_CONSECUTIVE_LOSSES:
            logger.info(
                f"1H close — consecutive-loss halt: {cl}/{config.MAX_CONSECUTIVE_LOSSES} SLs  "
                f"day_pnl={trader.day_pnl:+.2f}  (resets midnight UTC)"
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


async def on_5m_close(mstate: MarketState) -> None:
    """Handle a closed 5M candle event (stub — reserved for future use).

    Args:
        mstate: Current :class:`~data_store.MarketState`.
    """
    pass


async def on_tick(mstate: MarketState, price: float) -> None:
    """Handle every mark-price tick — SL/TP/trail monitoring and housekeeping.

    Runs daily reset, checks for position exits (paper simulation or live sync),
    applies post-SL cooldown when a stop-loss fires, and emits the periodic
    heartbeat log line.

    Args:
        mstate: Current :class:`~data_store.MarketState`.
        price:  Latest mark price from the Binance markPrice stream.
    """
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
            f"(TP={stats['tp_exits']} SL={stats['sl_exits']} BE={stats['be_exits']})  "
            f"consec_SL={stats['consecutive_losses']}"
        )
        # Post-SL extended cooldown: prevent re-entering the same whipsaw.
        # Sets bars_since_last to a negative offset so the strategy cooldown
        # effectively becomes POST_SL_COOLDOWN_1H bars instead of TRADE_COOLDOWN_1H.
        if closed.close_reason == "SL" and config.POST_SL_COOLDOWN_1H > 0:
            extra = config.POST_SL_COOLDOWN_1H - config.TRADE_COOLDOWN_1H
            if extra > 0:
                _bars_since_last_1h = -(extra - 1)
                logger.info(
                    f"Post-SL cooldown: next entry blocked for "
                    f"{config.POST_SL_COOLDOWN_1H} bars"
                )

    # ── Heartbeat — log status every HEARTBEAT_INTERVAL seconds ──────────────
    now = time.monotonic()
    if now - _last_heartbeat >= config.HEARTBEAT_INTERVAL:
        _last_heartbeat = now
        stats = trader.stats()
        pos_str = (
            f"  pos={trader.position.side}@{trader.position.entry:.0f}"
            f" SL={trader.position.sl:.0f}"
            f" trail={'✓' if trader.position.trail_activated else '✗'}"
            if trader.position else "  no_pos"
        )
        logger.info(
            f"♥ mark={price:.2f}  balance={trader.balance:.2f}  "
            f"day_pnl={trader.day_pnl:+.2f}  trades={stats['trades']}  "
            f"WR={stats['win_rate']:.0f}%  consec_SL={stats['consecutive_losses']}"
            f"{pos_str}  funding={mstate.funding_rate:.4%}"
        )


# ── Bootstrap ──────────────────────────────────────────────────────────────────

ws = BinanceWS(
    state=state,
    on_5m_close=on_5m_close,
    on_1h_close=on_1h_close,
    on_tick=on_tick,
)


async def main() -> None:
    """Bootstrap the trading bot and run the WebSocket event loop.

    Logs the startup configuration banner, registers OS signal handlers for
    graceful shutdown, initialises the trader (live balance + position recovery),
    then runs :meth:`~ws_client.BinanceWS.run` until cancelled or stopped.
    Prints a final session summary on exit.
    """
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
    session_str = (
        f"{config.SESSION_FILTER_START_UTC:02d}:00–{config.SESSION_FILTER_END_UTC:02d}:00 UTC"
        if (config.SESSION_FILTER_START_UTC or config.SESSION_FILTER_END_UTC) else "24/7"
    )
    logger.info(f"Starting trading bot [{mode}] symbol={config.SYMBOL}")
    logger.info(
        f"Strategy: 1H breakout  {sizing_label}  Leverage={config.LEVERAGE}x  "
        f"SL={config.ATR_SL_MULTIPLIER}×ATR  TP={config.ATR_TP_MULTIPLIER}×ATR  "
        f"ADX≥{config.ADX_MIN}  BP={config.BREAKOUT_PERIOD}bars  "
        f"SLOPE≥{config.EMA_SLOPE_MIN_PCT}%"
    )
    logger.info(
        f"Trail: BE@{config.TRAIL_ACTIVATE_ATR}×ATR  "
        f"LOCK@{config.TRAIL_LOCK_ATR}×ATR  STOP@{config.TRAIL_STOP_ATR}×ATR"
    )
    logger.info(
        f"Daily: profit={profit_label}  loss={loss_label}  "
        f"max_consec_SL={config.MAX_CONSECUTIVE_LOSSES}  "
        f"post_SL_cooldown={config.POST_SL_COOLDOWN_1H}bars"
    )
    logger.info(
        f"Live safety: funding_max={config.FUNDING_RATE_MAX:.4%}  "
        f"pos_sync={config.LIVE_POSITION_SYNC_SECS}s  "
        f"session={session_str}  "
        f"limit_entry={'ON' if config.USE_LIMIT_ENTRY else 'OFF'}"
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
