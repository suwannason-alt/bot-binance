"""
Binance Futures Trading Bot — entry point.

Wires together the WebSocket client, strategy evaluator, order executor,
Walk-Forward Optimizer, and Markov regime forecaster into a single async
event loop.

Startup sequence
----------------
1. :class:`~state_manager.StateManager` checks for a recent saved state.
2. If a fresh state (< 48 h) exists → :meth:`~warm_start.WarmStart.recover`
   restores WFO + forecaster + strategy state in < 5 s.
3. If stale / missing → :meth:`~warm_start.WarmStart.run` fetches ~3 000
   historical 1H bars, runs a full dry-run hydration (< 1 s), then hands
   over to the live loop.
4. ``state.buf_1h`` is pre-seeded with the last 600 historical bars so that
   indicators (EMA, RSI, ADX) are warm on the very first live candle.

Callbacks
---------
``on_1h_close``  — evaluates the breakout signal, updates WFO / forecaster,
                   persists state, opens a position when conditions are met.
``on_tick``      — mark-price SL/TP monitoring and heartbeat.

Usage::

    python main.py              # WFO enabled by default (recommended)
    python main.py --no-wfo    # classic fixed BREAKOUT_PERIOD=14
    python main.py --forecast  # WFO + Markov regime forecast
"""
import argparse
import asyncio
import gc
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

# ── Path bootstrap: modular layout — add the package dirs to sys.path so the flat
# `import config` / `from trader import …` style resolves after the restructure. ─
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parent
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import config_1h as config   # orchestrator reads/writes the 1H strategy config
import notifier
import strategy
from asset_processor import AssetProcessor
from btc.processor import Processor as BtcProcessor
from eth.processor import Processor as EthProcessor
from sol.processor import Processor as SolProcessor

# Feature-based domain processors (each a thin shell over the shared 1H engine).
# main spins up the enabled SECONDARY symbols from this registry; the primary
# (config.SYMBOL) keeps its proven WFO-driven on_1h_close path.
_DOMAIN_PROCESSORS = {
    "BTCUSDT": BtcProcessor,
    "ETHUSDT": EthProcessor,
    "SOLUSDT": SolProcessor,
}
from backtest import StrategyState
from data_store import MarketState
from order_manager import OrderManager
from regime_forecast import MarkovRegimeForecaster
from state_manager import StateManager
from trader import Trader
from walk_forward_optimizer import WalkForwardOptimizer
from warm_start import LiveHistory, WarmStart
from ws_client import BinanceWS, Route

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def _rss_mb() -> float:
    """Return current process RSS in MB. Reads /proc/self/status (Linux)."""
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1_048_576
    except Exception:
        pass
    try:
        with open("/proc/self/status") as _f:
            for _line in _f:
                if _line.startswith("VmRSS:"):
                    return int(_line.split()[1]) / 1024.0
    except OSError:
        pass
    return 0.0

# ── Core live objects ──────────────────────────────────────────────────────────
state  = MarketState()
trader = Trader()
# Resolve DB path from config so BOT_STATE_DB_PATH env var is honoured.
# In Docker this points to the named-volume mount (/app/state/bot_state.db)
# so the file survives container rebuilds and restarts.
sm     = StateManager(db_path=config.BOT_STATE_DB_PATH)

# ── Warm-start / autonomous strategy objects (populated before ws.run()) ───────
_wfo:          Optional[WalkForwardOptimizer]   = None
_forecaster:   Optional[MarkovRegimeForecaster] = None
_strat_state:  StrategyState                    = StrategyState()
_live_history: Optional[LiveHistory]            = None

# ── Session counters ───────────────────────────────────────────────────────────
_bars_since_last_1h: int = 9999   # 1H bars since last trade (for cooldown)
_last_heartbeat:   float = 0.0    # monotonic time of last heartbeat log
_bar_counter:        int = 0      # absolute 1H bar count since warm-start epoch

# ── State persistence cadence ─────────────────────────────────────────────────
_STATE_SAVE_INTERVAL = 6   # save to SQLite every N 1H bars (≈ every 6 h)


# ── Callbacks ──────────────────────────────────────────────────────────────────

async def on_1h_close(mstate: MarketState) -> None:
    """Evaluate the 1H breakout strategy on each closed 1H candle.

    Execution order on each 1H close:

    1. **LiveHistory append** — push the closed candle into the sliding window
       so the WFO training arrays stay current.
    2. **Forecaster update** — classify the new bar's regime, update the Markov
       transition matrix, derive entry gate and size scale for this bar.
    3. **WFO retune** — if ``WFO_RETUNE_INTERVAL`` bars have elapsed since the
       last retune, run the mini-backtest sweep and update ``_strat_state.active_bp``.
    4. **Pre-entry guards** — daily limits, session window, consecutive-loss
       circuit breaker, funding rate.
    5. **Signal evaluation** — calls ``strategy.evaluate_1h_live`` with the
       hydrated ``MarketState``.
    6. **State persistence** — saves WFO / forecaster / position state to SQLite
       every ``_STATE_SAVE_INTERVAL`` 1H bars.

    Args:
        mstate: Current :class:`~data_store.MarketState` with updated buffers.
    """
    global _bars_since_last_1h, _bar_counter
    global _wfo, _forecaster, _strat_state, _live_history

    # Daily reset (idempotent — safe to call here and in on_tick)
    trader.reset_day()

    _bars_since_last_1h += 1
    _bar_counter         += 1

    # ── 0. Per-close marker ───────────────────────────────────────────────────
    # Unconditional proof that this handler ran for this bar, before any gate or
    # early-return. NOTE: this only fires once the WS actually delivers a closed
    # 1H frame — if the stream is silent, the blocker is upstream in ws_client,
    # not here (a silent socket emits no heartbeat from on_tick either).
    try:
        _c1_close = float(mstate.buf_1h.arrays()[3][-1]) if mstate.buf_1h.count else float("nan")
    except (IndexError, ValueError):
        _c1_close = float("nan")
    logger.info(
        "1H close #%d  close=%.2f  buf_1h=%d  bars_since=%d",
        _bar_counter, _c1_close, mstate.buf_1h.count, _bars_since_last_1h,
    )

    # ── 1. Append the just-closed candle to LiveHistory ───────────────────────
    if _live_history is not None and mstate.buf_1h.count > 0:
        # Single arrays() call — previous code called it twice (bug: 10 allocs not 5)
        o1_live, h1_live, l1_live, c1_live, v1_live = mstate.buf_1h.arrays()
        if len(c1_live) > 0:
            _live_history.append_candle(
                open_  = float(o1_live[-1]),
                high   = float(h1_live[-1]),
                low    = float(l1_live[-1]),
                close  = float(c1_live[-1]),
                volume = float(v1_live[-1]),
            )
        del o1_live, h1_live, l1_live, c1_live, v1_live

    # ── Pre-compute indicator arrays once and share between §2 and §3 ─────────
    # get_indicator_arrays() recomputes ADX+ATR over 3 600 bars (>10 tmp allocs).
    # Computing it once instead of twice per bar halves per-hour peak allocation.
    _ind_arrs = None
    if _live_history is not None and (
        (config.REGIME_FORECAST_ENABLED and _forecaster is not None)
        or (config.WFO_ENABLED and _wfo is not None)
    ):
        _ind_arrs = _live_history.get_indicator_arrays()

    # ── 2. Forecaster update (REGIME_FORECAST_ENABLED) ───────────────────────
    if config.REGIME_FORECAST_ENABLED and _forecaster is not None and _ind_arrs is not None:
        import indicators as _ind
        from adaptive_regime import hurst_exponent as _hurst
        _, h1_fc, l1_fc, c1_fc, _, adx_fc, atr_fc = _ind_arrs
        if len(c1_fc) > 0 and not (
            __import__("math").isnan(adx_fc[-1]) or __import__("math").isnan(atr_fc[-1])
        ):
            _adx_val  = float(adx_fc[-1])
            _atr_pct  = float(atr_fc[-1]) / float(c1_fc[-1]) * 100 if c1_fc[-1] > 0 else 1.0
            _hurst_v  = _hurst(c1_fc)
            _new_state = _forecaster.classify(_adx_val, _atr_pct, _hurst_v)
            _forecaster.update(_new_state)
            _fc = _forecaster.forecast()

            _strat_state.current_regime = _new_state
            _strat_state.trend_prob     = _fc.trend_prob
            _strat_state.choppy_prob    = _fc.choppy_prob

            if _fc.choppy_prob >= config.FORECAST_CHOPPY_THRESHOLD:
                _strat_state.entry_allowed      = False
                _strat_state.effective_cooldown = config.WFO_CHOPPY_COOLDOWN
                _strat_state.size_scale         = 0.5
                logger.debug(
                    "Forecast: CHOPPY imminent (%.0f%%) — entry blocked",
                    _fc.choppy_prob * 100,
                )
            else:
                _strat_state.entry_allowed      = True
                _strat_state.effective_cooldown = config.TRADE_COOLDOWN_1H
                _strat_state.size_scale         = min(1.0, 0.5 + _fc.trend_prob)

    # ── 3. WFO retune (WFO_ENABLED) ──────────────────────────────────────────
    if config.WFO_ENABLED and _wfo is not None and _ind_arrs is not None:
        _, h1_w, l1_w, c1_w, _, adx_w, atr_w = _ind_arrs
        wfo_bar_idx = _live_history.bar_count  # relative index within LiveHistory
        if _wfo.should_retune(wfo_bar_idx):
            # Pass the current ATR so the optimizer can apply dynamic lookback
            # (WFO_FAST_ENABLED) when an ATR spike is detected.
            import math as _math
            _cur_atr_live = (
                float(atr_w[-1])
                if len(atr_w) > 0 and not _math.isnan(float(atr_w[-1]))
                else None
            )
            wfo_params = _wfo.optimize(
                c1=c1_w, h1=h1_w, l1=l1_w,
                adx_arr=adx_w, atr_arr=atr_w,
                end_bar=wfo_bar_idx,
                current_atr=_cur_atr_live,
            )
            _strat_state.active_bp = wfo_params.breakout_period
            pf_str = f"{wfo_params.profit_factor:.2f}" if wfo_params.profit_factor < 99 else "∞"
            logger.info(
                "WFO retune: BP=%d  PF=%s  n=%d  (bar=%d)",
                wfo_params.breakout_period, pf_str,
                wfo_params.n_trades, wfo_bar_idx,
            )
            # Append to structured WFO log table
            sm.append_wfo_log(
                bar=_bar_counter,
                bp=wfo_params.breakout_period,
                pf=wfo_params.profit_factor,
                n=wfo_params.n_trades,
            )

    # ── Release shared indicator arrays and run periodic GC ──────────────────
    del _ind_arrs
    if _bar_counter % 24 == 0:   # once per calendar day
        gc.collect()
        logger.info("GC sweep  bar=%d  mem=%.1f MB", _bar_counter, _rss_mb())

    # ── Memory log every 1H candle close ─────────────────────────────────────
    logger.debug("1H close mem=%.1f MB  bar=%d", _rss_mb(), _bar_counter)

    # ── 3c. Hourly Discord status report ──────────────────────────────────────
    # Fires on EVERY 1H close, BEFORE all the entry guards below, so the channel
    # always receives the live "Entry funnel" diagnostics — even on no-signal
    # bars or when a daily/funding/cooldown guard will skip the entry. The body
    # mirrors exactly what the console logs print for this bar. Best-effort: the
    # notifier swallows every error, so a Discord outage can never stall the loop.
    if config.WFO_ENABLED:
        config.BREAKOUT_PERIOD = _strat_state.active_bp  # funnel shows WFO's BP
    await _send_1h_discord_report(mstate)

    # ── 3b. Initial cooldown gate (indicator settling) ────────────────────────
    # Suppress entries for the first INITIAL_COOLDOWN_BARS 1H bars after startup.
    # WFO state above has already been updated (training accumulates normally).
    # In live mode the warm_start already provides 3,030 bars of indicator history
    # so EMA200 is fully converged — this guard is mainly useful when
    # INITIAL_COOLDOWN_BARS is set to a non-zero value for manual testing.
    if config.INITIAL_COOLDOWN_BARS > 0 and _bar_counter <= config.INITIAL_COOLDOWN_BARS:
        logger.info(
            "Initial cooldown: bar %d/%d — indicators settling, no entries",
            _bar_counter, config.INITIAL_COOLDOWN_BARS,
        )
        _maybe_save_state()
        return

    if trader.daily_halted:
        logger.info(
            f"1H close — daily limit reached, skipping  "
            f"day_pnl={trader.day_pnl:+.2f}  balance={trader.balance:.2f}"
        )
        return

    # ── 4a. Forecast entry gate ───────────────────────────────────────────────
    if config.REGIME_FORECAST_ENABLED and not _strat_state.entry_allowed:
        logger.debug(
            "Forecast entry blocked (choppy=%.0f%%)  bars_since=%d",
            _strat_state.choppy_prob * 100, _bars_since_last_1h,
        )
        # Still persist state even on blocked bars
        _maybe_save_state()
        return

    # ── 4b. Session time filter ───────────────────────────────────────────────
    s_start = config.SESSION_FILTER_START_UTC
    s_end   = config.SESSION_FILTER_END_UTC
    if s_start != 0 or s_end != 0:
        hour = datetime.now(timezone.utc).hour
        if s_start < s_end:
            in_session = s_start <= hour < s_end
        else:
            in_session = hour >= s_start or hour < s_end
        if not in_session:
            logger.debug(
                f"1H close UTC {hour:02d}h — outside session window "
                f"[{s_start:02d}–{s_end:02d}) → skip"
            )
            return

    # ── 4c. Consecutive-loss circuit breaker ──────────────────────────────────
    if config.MAX_CONSECUTIVE_LOSSES > 0:
        cl = trader.consecutive_losses
        if cl >= config.MAX_CONSECUTIVE_LOSSES:
            logger.info(
                f"1H close — consecutive-loss halt: {cl}/{config.MAX_CONSECUTIVE_LOSSES} SLs  "
                f"day_pnl={trader.day_pnl:+.2f}  (resets midnight UTC)"
            )
            return

    # ── 4d. Funding rate gate ─────────────────────────────────────────────────
    if config.FUNDING_RATE_MAX > 0:
        fr = abs(mstate.funding_rate)
        if fr > config.FUNDING_RATE_MAX:
            logger.info(
                f"1H close — funding rate {fr:.4%} > max {config.FUNDING_RATE_MAX:.4%}  "
                f"→ skipping entry this bar"
            )
            return

    # ── 5. Signal evaluation ──────────────────────────────────────────────────
    # Inject WFO breakout period override into config so evaluate_1h_live()
    # uses the WFO-selected rolling window.
    if config.WFO_ENABLED:
        config.BREAKOUT_PERIOD = _strat_state.active_bp

    sig = strategy.evaluate_1h_live(mstate, _bars_since_last_1h)

    if sig:
        # Apply forecast size scale on top of regime scale from the signal
        if config.REGIME_FORECAST_ENABLED:
            import numpy as np
            sig.size_scale = float(
                np.clip(sig.size_scale * _strat_state.size_scale, 0.10, 2.0)
            )

        logger.info(f"SIGNAL  {sig.reason}")
        logger.info(
            f"  Entry={sig.entry:.2f}  SL={sig.sl:.2f} ({sig.sl_pct:.2f}%)  "
            f"TP={sig.tp:.2f} ({sig.tp_pct:.2f}%)  RR={sig.rr_ratio:.2f}  "
            f"BP={_strat_state.active_bp}bars  "
            f"size_scale={sig.size_scale:.0%}  "
            f"funding={mstate.funding_rate:.4%}"
        )
        pos = await trader.open_position(sig)
        if pos:
            _bars_since_last_1h = 0
            logger.info(
                f"  Position opened  qty={pos.qty:.4f}  "
                f"balance={trader.balance:.2f} USDT  day_pnl={trader.day_pnl:+.2f}"
            )
            await notifier.send_trade_open(
                side=pos.side, entry=pos.entry, sl=pos.sl, tp=pos.tp,
                qty=pos.qty, reason=sig.reason,
            )
    else:
        # ── Entry-funnel diagnostic — observability only, never gates trades ──
        diag = strategy.diagnose_1h_live(mstate, _bars_since_last_1h)
        ctx = (
            f"No 1H signal  mark={mstate.mark_price:.2f}  BP={_strat_state.active_bp}bars  "
            f"bars_since={_bars_since_last_1h}  funding={mstate.funding_rate:.4%}  "
            f"day_pnl={trader.day_pnl:+.2f}"
        )
        if diag is not None:
            logger.info(f"{ctx}\n{diag.text}")
        else:
            logger.info(f"{ctx}  (buffer warming: {mstate.buf_1h.count}/{config.MIN_CANDLES_1H})")

    # ── 6. State persistence ──────────────────────────────────────────────────
    _maybe_save_state()


# ---------------------------------------------------------------------------
# Discord hourly status report
# ---------------------------------------------------------------------------

async def _send_1h_discord_report(mstate: MarketState) -> None:
    """Send the per-1H-bar "Entry funnel" status report to Discord.

    Builds the same context + funnel block the console logs show for this bar
    and hands it to the (error-swallowing) notifier.  Best-effort: returns
    cleanly when the webhook is unset or the 1H buffer is still warming, and the
    notifier itself never raises — so this is safe to call on every 1H close.

    Args:
        mstate: Current :class:`~data_store.MarketState` with updated buffers.
    """
    if not config.DISCORD_ENABLED:
        return
    try:
        close = (
            float(mstate.buf_1h.arrays()[3][-1])
            if mstate.buf_1h.count else float("nan")
        )
    except (IndexError, ValueError):
        close = float("nan")

    ctx = (
        f"1H close #{_bar_counter}  close={close:.2f}  "
        f"buf_1h={mstate.buf_1h.count}  bars_since={_bars_since_last_1h}\n"
        f"mark={mstate.mark_price:.2f}  BP={_strat_state.active_bp}bars  "
        f"funding={mstate.funding_rate:.4%}  day_pnl={trader.day_pnl:+.2f}  "
        f"balance={trader.balance:.2f}"
    )
    diag = strategy.diagnose_1h_live(mstate, _bars_since_last_1h)
    if diag is not None:
        body = f"{ctx}\n\n{diag.text}"
    else:
        body = (
            f"{ctx}\n\n  (buffer warming: "
            f"{mstate.buf_1h.count}/{config.MIN_CANDLES_1H})"
        )
    header = "🤖 **Trading Bot Status Update** ➔ 1H Bar Evaluation"
    await notifier.send_funnel_report(header, body)


# ---------------------------------------------------------------------------
# State persistence helper
# ---------------------------------------------------------------------------

def _maybe_save_state() -> None:
    """Persist bot state to SQLite every ``_STATE_SAVE_INTERVAL`` 1H bars.

    Safe to call on every bar — exits cheaply when the interval has not
    elapsed.  Catches all exceptions so a DB write failure never halts the
    live loop.
    """
    if _bar_counter % _STATE_SAVE_INTERVAL != 0:
        return
    try:
        sm.save(
            wfo          = _wfo,
            forecaster   = _forecaster,
            strat_state  = _strat_state,
            bars_since_last = _bars_since_last_1h,
            position     = trader.position,
            balance      = trader.balance,
            bar_counter  = _bar_counter,
        )
    except Exception as exc:
        logger.warning("State save failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Callbacks (continued)
# ---------------------------------------------------------------------------

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
        await notifier.send_trade_closed(
            side=closed.side, close_reason=closed.close_reason, pnl=closed.pnl,
            balance=stats["balance"], win_rate=stats["win_rate"],
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
            f"{pos_str}  funding={mstate.funding_rate:.4%}  "
            f"mem={_rss_mb():.0f}MB"
        )


# ── Bootstrap ──────────────────────────────────────────────────────────────────

ws = BinanceWS(
    state=state,
    on_1h_close=on_1h_close,
    on_tick=on_tick,
)


# ---------------------------------------------------------------------------
# CLI parsing  (applied before asyncio.run so WFO flag reaches WarmStart)
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    """Parse command-line flags for the live trading bot.

    WFO is **ON by default**.  Use ``--no-wfo`` to run the classic fixed
    ``BREAKOUT_PERIOD`` mode.  All flags are applied to the live ``config``
    module before :func:`main` is entered, so :class:`~warm_start.WarmStart`
    sees the correct ``WFO_ENABLED`` value during hydration.

    Returns:
        Parsed :class:`argparse.Namespace`.
    """
    parser = argparse.ArgumentParser(
        description="BTCUSDT Futures live trading bot  [WFO ON by default]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WFO is the default execution mode — use --no-wfo for classic static\n"
            "BREAKOUT_PERIOD=14 mode.\n"
            "\n"
            "Examples:\n"
            "  python main.py              # WFO enabled (default, recommended)\n"
            "  python main.py --no-wfo    # classic fixed BREAKOUT_PERIOD=14\n"
            "  python main.py --forecast  # WFO + Markov regime forecast\n"
        ),
    )
    parser.add_argument(
        "--wfo",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Walk-Forward Optimization: auto-select BREAKOUT_PERIOD every 30 days "
            "(default: ON). Use --no-wfo to run classic fixed BREAKOUT_PERIOD=14."
        ),
    )
    parser.add_argument(
        "--forecast",
        action="store_true",
        default=False,
        help=(
            "Enable Markov regime forecast: suppress entries when next-bar choppy "
            "probability exceeds 65%%. Scale position size by trend confidence."
        ),
    )
    return parser.parse_args()


def _apply_args_to_config(args: argparse.Namespace) -> None:
    """Write parsed CLI flags into the live ``config`` module.

    Must be called **before** ``asyncio.run(main())`` so that
    :class:`~warm_start.WarmStart` reads the correct ``config.WFO_ENABLED``
    value during the hydration phase and creates (or skips) the
    :class:`~walk_forward_optimizer.WalkForwardOptimizer` accordingly.

    Args:
        args: Parsed namespace from :func:`_parse_args`.
    """
    config.WFO_ENABLED             = args.wfo
    config.REGIME_FORECAST_ENABLED = args.forecast

    if not args.wfo:
        logger.warning(
            "WFO disabled (--no-wfo): running classic mode with fixed "
            "BREAKOUT_PERIOD=%d.  Omit --no-wfo to restore auto-tuning.",
            config.BREAKOUT_PERIOD,
        )


async def main() -> None:
    """Bootstrap the trading bot and run the WebSocket event loop.

    Logs the startup configuration banner, registers OS signal handlers for
    graceful shutdown, initialises the trader (live balance + position recovery),
    then runs :meth:`~ws_client.BinanceWS.run` until cancelled or stopped.
    Prints a final session summary on exit.
    """
    # Profit-locking STEP trail is the production live exit logic (opt-in flag, off in
    # config so backtest/grid tools keep their classic trail — turned on here for live).
    config.STEP_TRAILING_ENABLED = True
    # Apply this symbol's per-asset 1H profile from the CONFIG_MATRIX (breakout / ADX /
    # TP / SL / trail-activate).  No-op for symbols not in the matrix (flat defaults
    # retained).  Must run before the banner so it reflects the active profile.
    if config.apply_symbol(config.SYMBOL):
        logger.info(f"Applied CONFIG_MATRIX profile for {config.SYMBOL}")
    logger.info("Trailing regime: profit-locking STEP ladder (BE → +1ATR → +2ATR …)")

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
    # ── Feature-flag summary ──────────────────────────────────────────────────
    _feat: list = []
    if config.WFO_ENABLED:
        _feat.append(
            f"WFO(retune={config.WFO_RETUNE_INTERVAL}bars/"
            f"train={config.WFO_TRAINING_WINDOW}bars)"
        )
    if config.REGIME_FORECAST_ENABLED:
        _feat.append(f"FORECAST(choppy≥{config.FORECAST_CHOPPY_THRESHOLD:.0%}→block)")
    if config.ADAPTIVE_REGIME_ENABLED:
        _feat.append("ADAPTIVE")
    if config.ADAPTIVE_TRAILING_ENABLED:
        _feat.append("ADAPT_TRAIL")
    _feat_str = "  ".join(_feat) if _feat else "classic (WFO off)"

    logger.info(f"Starting trading bot [{mode}] symbol={config.SYMBOL}")
    logger.info(f"Autonomous mode: {_feat_str}")
    logger.info(
        f"Strategy: 1H breakout  {sizing_label}  Leverage={config.LEVERAGE}x  "
        f"SL={config.ATR_SL_MULTIPLIER}×ATR  TP={config.ATR_TP_MULTIPLIER}×ATR  "
        f"ADX≥{config.ADX_MIN}  BP={config.BREAKOUT_PERIOD}bars (WFO will override)  "
        f"SLOPE≥{config.EMA_SLOPE_MIN_PCT}%"
        if config.WFO_ENABLED else
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

    # ══════════════════════════════════════════════════════════════════════════
    # WARM START — hydrate WFO + forecaster before going live
    # ══════════════════════════════════════════════════════════════════════════
    #
    # Decision tree:
    #   1. Load saved state from SQLite (StateManager).
    #   2. If fresh (< 48 h): recover WFO + forecaster from snapshot +
    #      fetch last N bars to refresh LiveHistory.  No full dry run needed.
    #   3. If stale / absent: full warm start — fetch ~3 000 bars, run dry-run
    #      hydration, write state snapshot for next restart.
    #   4. Hydrate MarketState.buf_1h with the last MAX_CANDLES historical bars
    #      so ``evaluate_1h_live()`` has warm indicators on the first tick.
    # ══════════════════════════════════════════════════════════════════════════
    global _wfo, _forecaster, _strat_state, _live_history
    global _bars_since_last_1h, _bar_counter

    saved_state = sm.load()

    if saved_state is not None:
        # ── Crash recovery path ───────────────────────────────────────────────
        logger.info("Restoring from saved state (crash recovery / fast restart) …")
        try:
            warm = await WarmStart.recover(saved_state, symbol=config.SYMBOL)
        except Exception as exc:
            logger.error("Recovery failed: %s — falling back to fresh warm start", exc)
            warm = await WarmStart.run(symbol=config.SYMBOL)
    else:
        # ── Fresh cold start ──────────────────────────────────────────────────
        logger.info("No saved state — running full warm start …")
        warm = await WarmStart.run(symbol=config.SYMBOL)

    # Transfer hydrated components to module-level globals
    _wfo             = warm.wfo
    _forecaster      = warm.forecaster
    _strat_state     = warm.strat_state
    _live_history    = warm.live_history
    _bars_since_last_1h = warm.bars_since_last
    _bar_counter        = warm.bar_counter

    # Seed MarketState.buf_1h with the last MAX_CANDLES historical bars
    WarmStart.hydrate_candle_buffer(state.buf_1h, warm.live_history)

    # Apply WFO-selected breakout period to config for evaluate_1h_live()
    if config.WFO_ENABLED and _wfo is not None:
        config.BREAKOUT_PERIOD = _strat_state.active_bp

    # Persist initial state immediately (overwrites any stale entry)
    sm.save(
        wfo=_wfo, forecaster=_forecaster, strat_state=_strat_state,
        bars_since_last=_bars_since_last_1h,
        position=trader.position, balance=trader.balance,
        bar_counter=_bar_counter,
    )

    WarmStart.print_summary(warm)
    # ─────────────────────────────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════════════════════
    # MULTI-ASSET FAN-OUT (additive — proven primary path untouched)
    # ══════════════════════════════════════════════════════════════════════════
    # Every ENABLED symbol other than the primary (config.SYMBOL) gets its own
    # AssetProcessor fed by the SAME socket via a per-symbol Route.  The primary keeps
    # its exact proven handlers (state / on_1h_close / on_tick + WFO).  Secondaries
    # snapshot+restore the shared config around their evaluate, so the primary's
    # WFO-driven config is never disturbed.  If no secondaries are enabled we keep the
    # original single-symbol socket verbatim.
    global ws
    _secondaries = [s for s in config.enabled_symbols() if s != config.SYMBOL]
    if _secondaries:
        # Build each enabled secondary from its feature-based domain processor
        # (falls back to a generic AssetProcessor if a symbol has no domain module).
        processors = [
            _DOMAIN_PROCESSORS.get(sym, lambda order_manager=None, _s=sym:
                                   AssetProcessor(_s, order_manager=order_manager))(order_manager=None)
            for sym in _secondaries
        ]
        # Global margin budget = total capital across every LIVE sleeve (primary +
        # secondaries).  In paper each sleeve runs its own simulated balance; in live
        # against one real account these all read the same equity.
        _live_traders = [trader] + [p.trader for p in processors if p.trader is not None]
        order_mgr = OrderManager(balance_provider=lambda: sum(t.balance for t in _live_traders))
        for proc in processors:
            proc.order_manager = order_mgr
        for proc in processors:
            await proc.warm_start()
        routes = {config.SYMBOL: Route(state=state,
                                       on_1h_close=on_1h_close, on_tick=on_tick)}
        for proc in processors:
            routes[proc.symbol] = Route(state=proc.state, on_1h_close=proc.on_1h_close,
                                        on_tick=proc.on_tick)
        ws = BinanceWS(routes=routes)
        logger.info("Multi-asset fan-out ACTIVE on one shared socket — primary=%s  "
                    "secondaries=[%s]", config.SYMBOL,
                    ", ".join(f"{p.symbol}:{p.mode}" for p in processors))
        live_syms = [s for s in config.enabled_symbols() if config.trade_mode(s) == "LIVE"]
        if len(live_syms) > 1:
            logger.warning("Multiple LIVE symbols (%s) — concurrent LIVE trail management "
                           "through shared config is paper-pending; recommend one LIVE symbol.",
                           ", ".join(live_syms))
    else:
        logger.info("Single-asset mode — only %s enabled (proven path, no fan-out).",
                    config.SYMBOL)

    try:
        # The shared WebSocket feed drives the whole 1H book: the primary symbol via
        # on_1h_close and each enabled secondary via its AssetProcessor route.
        await ws.run()
    except asyncio.CancelledError:
        pass
    finally:
        # Final state save on clean shutdown
        try:
            sm.save(
                wfo=_wfo, forecaster=_forecaster, strat_state=_strat_state,
                bars_since_last=_bars_since_last_1h,
                position=trader.position, balance=trader.balance,
                bar_counter=_bar_counter,
            )
            logger.info("Final state saved to %s", sm.db_path())
        except Exception as exc:
            logger.warning("Final state save failed: %s", exc)

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
    # Parse CLI flags and apply to config BEFORE entering the async loop.
    # This ensures WarmStart.hydrate() sees the correct WFO_ENABLED value
    # and creates the WalkForwardOptimizer during the hydration phase.
    _args = _parse_args()
    _apply_args_to_config(_args)
    asyncio.run(main())
