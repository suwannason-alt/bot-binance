"""
Autonomous backtest runner — single-shot execution.

Instead of sweeping 100+ static PARAM_CANDIDATES, the strategy self-tunes:

  - **Walk-Forward Optimizer (WFO)** — re-selects BREAKOUT_PERIOD every 30 days.
    Trains on the past 90 days of 1H data, picks the period with the highest
    Profit Factor, applies it to the next 30 days.  Zero lookahead.
    **Enabled by default.**  Disable with ``--no-wfo`` for classic static mode.

  - **Markov Regime Forecast** — classifies each bar as TREND/CHOPPY/QUIET,
    suppresses entries when next-bar choppy probability exceeds 65%
    (``--forecast``).  Extended cooldown and reduced size in uncertain regimes.

  - **Adaptive Regime Framework** — continuous regime score from Hurst,
    ADX-momentum, and BBW-percentile feeds smooth TP/SL/size functions
    (``--adaptive``).  No cliff-edge parameter switches.

Usage::

    python run_backtest.py                        # 6-year with WFO (default, max available data)
    python run_backtest.py --no-wfo               # 6-year classic static mode
    python run_backtest.py --days 1825            # 5-year run with WFO
    python run_backtest.py --forecast             # WFO + Markov regime forecast
    python run_backtest.py --adaptive             # WFO + adaptive regime framework
    python run_backtest.py --all                  # all autonomous features
    python run_backtest.py --no-wfo --forecast    # forecast-only, no WFO
    python run_backtest.py --min-cagr 30 --max-dd 60   # tighter goal criteria

Goal (period-invariant — same threshold for 3yr, 5yr, 6yr)::

    CAGR  ≥ 20 %/yr   (default, override with --min-cagr)
    MaxDD ≤ 70 %       (default, override with --max-dd)
    Years ≥ 100 %      (default, override with --year-frac; 0.8 = allow 1 bad yr)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Dict, List, Optional

import pandas as pd

import backtest
import config
import fetch_data
import visualize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_backtest")

# ── Defaults (overridable via CLI) ─────────────────────────────────────────────
# Data available: BTCUSDT perpetual futures launched 2019-09-08.
# Effective coverage to today ≈ 6.7 years.  Use YEARS=6 for 6 complete calendar
# years (Sep 2019 → Sep 2025); YEARS=7 includes the partial 7th year.
# Setting YEARS=8 creates a phantom Year 8 (no data) — avoid.
INITIAL_BALANCE: float = 1_000.0
YEARS:           int   = 6
DAYS:            int   = YEARS * 365

# ── Goal thresholds (period-invariant) ────────────────────────────────────────
GOAL_MIN_CAGR_PCT:  float = 20.0   # CAGR ≥ 20 %/yr
GOAL_MAX_DD_PCT:    float = 70.0   # MaxDD ≤ 70 %
GOAL_MIN_YEAR_FRAC: float = 1.0    # all years profitable (1.0); or 0.8 = allow 1 bad yr

# ── Base configuration — mirrors .env exactly ─────────────────────────────────
# Every value here must be kept in sync with .env.
# Run `python run_backtest.py` to verify the live config produces the expected
# backtest result before deploying any change to production.
#
# Proven result (RISK_PERCENT=8%, WFO=ON):
#   $1,000 → $32,000  CAGR +101%/yr  MaxDD -53%  (5-year WFO)
#   $1,000 → $42,000  CAGR +112%/yr  MaxDD -49%  (5-year Classic --no-wfo)
_BASE: Dict = {
    # ── [3] Position sizing ── mirrors .env [3] ───────────────────────────────
    # EQUITY_PERCENT=0 activates RISK_PERCENT mode (SL-distance based, proven best).
    # RISK_PERCENT=8%: each SL always costs exactly 8% of balance regardless of ATR.
    "EQUITY_PERCENT":          0.0,    # 0 = use RISK_PERCENT (proven +112%/yr)
    "RISK_PERCENT":            8.0,
    "LEVERAGE":                10,
    "RISK_USD":                0.0,
    "ORDER_BALANCE_USD":       0.0,

    # ── [4] Strategy parameters ── mirrors .env [4] ───────────────────────────
    "ATR_SL_MULTIPLIER":       1.5,
    "ATR_TP_MULTIPLIER":       6.0,
    "BREAKOUT_PERIOD":         14,     # WFO auto-tunes this every 30 days
    "ADX_MIN":                 20.0,
    "ADX_PERIOD":              14,
    "TRAIL_ACTIVATE_ATR":      1.5,
    "TRAIL_LOCK_ATR":          0.0,
    "TRAIL_STOP_ATR":          0.0,
    "ATR_RATIO_MIN":           1.10,
    "EMA_SLOPE_MIN_PCT":       0.15,
    "VOL_RATIO_MIN":           0.3,
    "ATR_1H_PCT_MIN":          0.05,
    "ATR_1H_PCT_MAX":          5.0,
    "EMA_TREND_SLOPE_BARS":    7,
    "TRADE_COOLDOWN_1H":       1,
    "EMA_1H_MIN_SEP":          0.001,
    "RSI_1H_LONG_MIN":         45,
    "RSI_1H_LONG_MAX":         78,
    "RSI_1H_SHORT_MIN":        22,
    "RSI_1H_SHORT_MAX":        55,
    "REQUIRE_MACD_CONFIRM":    False,
    "BREAKOUT_ATR_BUFFER":     0.0,
    "EMA_TREND_DISTANCE_MIN":  0.0,
    "BODY_ATR_RATIO_MIN":      0.0,

    # ── [5] Daily circuit breakers ── mirrors .env [5] ────────────────────────
    "DAILY_PROFIT_TARGET_USD": 110.0,
    "DAILY_LOSS_LIMIT_USD":    50.0,
    "DAILY_PROFIT_TARGET_PCT": 0.0,
    "DAILY_LOSS_LIMIT_PCT":    0.0,

    # ── [7] Walk-Forward Optimization ── mirrors .env [7] ─────────────────────
    "WFO_ENABLED":              True,  # ← default ON; disable with --no-wfo
    "WFO_RETUNE_INTERVAL":      720,
    "WFO_TRAINING_WINDOW":      2160,
    "WFO_MIN_TRADES":           4,
    "WFO_FAST_ENABLED":         True,
    "WFO_FAST_TRAINING_WINDOW": 336,
    "WFO_FAST_ATR_MULT":        2.0,

    # ── [8] Indicator warmup ── mirrors .env [8] ──────────────────────────────
    "INITIAL_COOLDOWN_BARS":    0,     # 0 = off (live bot is already warm-started)

    # ── [11] Autonomous features ── all OFF = proven maximum-growth mode ──────
    "WFO_ENABLED":              True,  # toggled via --wfo / --no-wfo CLI flag
    "REGIME_FORECAST_ENABLED":  False, # toggled via --forecast CLI flag
    "ADAPTIVE_REGIME_ENABLED":  False, # toggled via --adaptive CLI flag
    "ADAPTIVE_TRAILING_ENABLED": False,
    "REGIME_FILTER_ENABLED":    False,
    "DYNAMIC_TP_ENABLED":       False,
    "VOL_SIZING_ENABLED":       False,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _apply_config(params: Dict) -> None:
    """Write ``params`` into the live ``config`` module.

    Also ensures ``MIN_CANDLES_1H ≥ EMA_TREND + 10`` so warm-up is adequate.

    Args:
        params: Mapping of config attribute names to new values.
    """
    for key, value in params.items():
        if hasattr(config, key):
            setattr(config, key, value)
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)


def _year_end_balance(equity: pd.Series, cutoff: pd.Timestamp) -> float:
    """Return the equity-curve value at or immediately before ``cutoff``.

    Args:
        equity: Time-indexed equity curve.
        cutoff: Upper-bound timestamp (inclusive).

    Returns:
        Last equity value on or before ``cutoff``.
    """
    subset = equity[equity.index <= cutoff]
    return float(subset.iloc[-1]) if len(subset) else float(equity.iloc[-1])


def _year_trade_breakdown(result: backtest.BacktestResult,
                          cutoffs: List[pd.Timestamp]) -> List[Dict]:
    """Compute per-year trade statistics.

    Args:
        result:  Completed backtest result.
        cutoffs: List of year-boundary timestamps.

    Returns:
        List of per-year dicts with ``n``, ``tp``, ``sl``, ``be``,
        ``long``, ``short`` counts.
    """
    equity     = result.equity_curve
    boundaries = [equity.index[0]] + list(cutoffs) + [equity.index[-1]]
    rows: List[Dict] = []
    for yi in range(len(boundaries) - 1):
        t_s = boundaries[yi]
        t_e = boundaries[yi + 1]
        yr  = [t for t in result.trades if t_s <= t.entry_time < t_e]
        rows.append({
            "n":     len(yr),
            "tp":    sum(1 for t in yr if t.close_reason == "TP"),
            "sl":    sum(1 for t in yr if t.close_reason == "SL"),
            "be":    sum(1 for t in yr if t.close_reason == "BE"),
            "long":  sum(1 for t in yr if t.side == "LONG"),
            "short": sum(1 for t in yr if t.side == "SHORT"),
        })
    return rows


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _print_report(
    result: backtest.BacktestResult,
    balances: List[float],
    year_ok: List[bool],
    cutoffs: List[pd.Timestamp],
    years: int,
    goal_met: bool,
    cagr_met: bool,
    dd_met: bool,
    years_met: bool,
) -> None:
    """Print a comprehensive single-run backtest report to stdout.

    Includes year-by-year breakdown, trade statistics, goal assessment,
    WFO retune log (if WFO was enabled), and a feature-flag summary.

    Args:
        result:     Completed backtest result.
        balances:   Year-boundary balances (length ``years + 1``).
        year_ok:    Per-year profitability flags.
        cutoffs:    Year-boundary timestamps.
        years:      Total years in the backtest window.
        goal_met:   All three sub-goals passed simultaneously.
        cagr_met:   CAGR sub-goal passed.
        dd_met:     MaxDD sub-goal passed.
        years_met:  Year-fraction sub-goal passed.
    """
    stats = result.stats

    # ── Active feature flags ─────────────────────────────────────────────────
    flags: List[str] = []
    if config.WFO_ENABLED:
        flags.append("WFO")
    if config.REGIME_FORECAST_ENABLED:
        flags.append("FORECAST")
    if config.ADAPTIVE_REGIME_ENABLED:
        flags.append("ADAPTIVE")
    if config.ADAPTIVE_TRAILING_ENABLED:
        flags.append("ADAPT_TRAIL")
    if config.REGIME_FILTER_ENABLED:
        flags.append("REGIME")
    if config.DYNAMIC_TP_ENABLED:
        flags.append("DYN_TP")
    if config.VOL_SIZING_ENABLED:
        flags.append("VOL_SZ")
    flags_str = "  [" + " ".join(flags) + "]" if flags else "  [classic]"

    sizing = (
        f"ORDER_BAL=${config.ORDER_BALANCE_USD:.0f}×{config.LEVERAGE}lev"
        if config.ORDER_BALANCE_USD > 0
        else f"RISK={config.RISK_PERCENT:.0f}% LEV={config.LEVERAGE}×"
    )

    print()
    print("=" * 76)
    print(f"  AUTONOMOUS BACKTEST  |  {years}-year  |  {sizing}"
          f"  ADX≥{config.ADX_MIN:.0f}  TP×{config.ATR_TP_MULTIPLIER:.1f}"
          f"{flags_str}")
    print("=" * 76)

    # ── Year-by-year breakdown ───────────────────────────────────────────────
    yr_rows = _year_trade_breakdown(result, cutoffs)
    for i in range(years):
        b_s   = balances[i]
        b_e   = balances[i + 1]
        ret   = (b_e - b_s) / b_s * 100
        sign  = "+" if ret >= 0 else ""
        ok    = "✓" if year_ok[i] else "✗"
        detail = ""
        if i < len(yr_rows):
            yr = yr_rows[i]
            detail = (f"  [{yr['n']}t: TP={yr['tp']} SL={yr['sl']}"
                      f" BE={yr['be']} | {yr['long']}L {yr['short']}S]")
        print(f"  Year {i + 1}  ${b_s:>10,.0f}  →  ${b_e:>10,.0f}"
              f"  {sign}{ret:.1f}%  {ok}{detail}")

    print("─" * 76)

    # ── Overall statistics ───────────────────────────────────────────────────
    n_t   = stats.get("total_trades", 0)
    tp_e  = stats.get("tp_exits", 0)
    sl_e  = stats.get("sl_exits", 0)
    be_e  = stats.get("be_exits", 0)
    wr    = stats.get("win_rate", 0)
    cagr  = stats.get("cagr_pct", 0)
    dd    = stats.get("max_drawdown_pct", 0)
    pf    = stats.get("profit_factor", 0)
    sh    = stats.get("sharpe", 0)
    ret_t = stats.get("total_return_pct", 0)
    init  = stats.get("initial_balance", INITIAL_BALANCE)
    final = stats.get("final_balance", INITIAL_BALANCE)

    print(f"  Trades       : {n_t}"
          f"  (TP={tp_e}  SL={sl_e}  BE={be_e})")
    print(f"  Win rate     : {wr:.1f}%")
    print(f"  Total return : {ret_t:+.1f}%   ${init:,.0f} → ${final:,.0f}")
    print(f"  CAGR         : {cagr:+.1f}%/yr")
    print(f"  Max drawdown : {dd:.1f}%")
    print(f"  Profit factor: {pf:.2f}")
    print(f"  Sharpe ratio : {sh:.2f}")

    # ── Goal assessment ──────────────────────────────────────────────────────
    print("─" * 76)
    year_frac  = sum(year_ok) / max(years, 1)
    goal_parts = [
        f"CAGR {'✓' if cagr_met  else '✗'} {cagr:+.0f}%/yr ≥ {GOAL_MIN_CAGR_PCT:.0f}%",
        f"MaxDD {'✓' if dd_met   else '✗'} {dd:.0f}% ≤ {GOAL_MAX_DD_PCT:.0f}%",
        f"Yrs {'✓' if years_met  else '✗'} {sum(year_ok)}/{years}"
        f" ({year_frac:.0%} ≥ {GOAL_MIN_YEAR_FRAC:.0%})",
    ]
    print(f"  GOAL {'MET ✓' if goal_met else 'NOT MET ✗'}  │  "
          + "  │  ".join(goal_parts))

    # ── WFO retune log ───────────────────────────────────────────────────────
    if config.WFO_ENABLED and result.wfo_log:
        print("─" * 76)
        print(f"  WFO retune log  ({len(result.wfo_log)} retunings)")
        for entry in result.wfo_log:
            scores_str = "  ".join(
                f"BP{bp}={v[0]:.1f}({v[1]})"
                for bp, v in sorted(entry["scores"].items())
            )
            pf_disp = f"{entry['pf']:.2f}" if entry["pf"] < 99 else "∞"
            print(f"    bar {entry['bar']:>5d}  →  BP={entry['bp']:>2d}"
                  f"  PF={pf_disp:<5}  n={entry['n']:>3d}"
                  f"  │  {scores_str}")

    print("=" * 76)
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(
    days:            int   = DAYS,
    initial_balance: float = INITIAL_BALANCE,
    years:           int   = YEARS,
    symbol:          str   = "BTCUSDT",
) -> bool:
    """Run the autonomous single-shot backtest and return whether the goal was met.

    Applies the proven base configuration, enables autonomous features as
    requested via config flags (set before calling ``main()``), fetches
    historical data, runs ``backtest.run()`` exactly **once**, and prints a
    comprehensive report.

    Args:
        days:            Calendar days of historical data to fetch.
        initial_balance: Starting balance in USDT.
        years:           Number of calendar years in the backtest window.
        symbol:          Trading pair (e.g. ``"BTCUSDT"``).

    Returns:
        ``True`` if the goal was met (CAGR, MaxDD, and years-fraction);
        ``False`` otherwise.
    """
    logger.info(
        "Fetching %d-day data (%d year%s)…",
        days, years, "s" if years != 1 else "",
    )
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol=symbol, days=days))

    t0 = pd.Timestamp(df_5m["open_time"].iloc[0],  unit="ms")
    tN = pd.Timestamp(df_5m["open_time"].iloc[-1], unit="ms")
    logger.info(
        "Period: %s → %s  5M=%d  1H=%d",
        t0.strftime("%Y-%m-%d"), tN.strftime("%Y-%m-%d"),
        len(df_5m), len(df_1h),
    )

    # Log active autonomous features
    feature_log: List[str] = []
    if config.WFO_ENABLED:
        feature_log.append(
            f"WFO (retune every {config.WFO_RETUNE_INTERVAL} bars,"
            f" train {config.WFO_TRAINING_WINDOW} bars,"
            f" min_trades={config.WFO_MIN_TRADES})"
        )
    if config.REGIME_FORECAST_ENABLED:
        feature_log.append(
            f"Forecast (choppy≥{config.FORECAST_CHOPPY_THRESHOLD:.0%}→block,"
            f" trend≥{config.FORECAST_MIN_TREND_PROB:.0%}→full size)"
        )
    if config.ADAPTIVE_REGIME_ENABLED:
        feature_log.append(
            f"Adaptive (TP {config.ADAPTIVE_TP_BASE:.1f}×→{config.ADAPTIVE_TP_BASE * config.ADAPTIVE_TP_MAX_EXT:.1f}×,"
            f" min_score={config.ADAPTIVE_MIN_SCORE:.2f})"
        )
    if config.ADAPTIVE_TRAILING_ENABLED:
        feature_log.append(
            f"AdaptTrail (min={config.ADAPTIVE_TRAIL_MIN_ATR:.2f}×ATR)"
        )
    if feature_log:
        for fl in feature_log:
            logger.info("  ► %s", fl)
    else:
        logger.info("  ► Classic static mode (no autonomous features)")

    # ── Single backtest run ──────────────────────────────────────────────────
    result = backtest.run(df_5m, df_1h, initial_balance=initial_balance, mode="1h")
    stats  = result.stats
    equity = result.equity_curve

    eq_start = equity.index[0]
    cutoffs  = [eq_start + pd.DateOffset(years=y) for y in range(1, years)]
    balances = (
        [initial_balance]
        + [_year_end_balance(equity, c) for c in cutoffs]
        + [float(equity.iloc[-1])]
    )

    year_ok   = [balances[i + 1] > balances[i] for i in range(years)]
    year_frac = sum(year_ok) / max(years, 1)

    cagr_met  = stats.get("cagr_pct", 0)        >= GOAL_MIN_CAGR_PCT
    dd_met    = stats.get("max_drawdown_pct", 0) <= GOAL_MAX_DD_PCT
    years_met = year_frac                        >= GOAL_MIN_YEAR_FRAC
    goal_met  = cagr_met and dd_met and years_met

    _print_report(
        result=result,
        balances=balances,
        year_ok=year_ok,
        cutoffs=cutoffs,
        years=years,
        goal_met=goal_met,
        cagr_met=cagr_met,
        dd_met=dd_met,
        years_met=years_met,
    )

    chart = visualize.plot(
        result,
        show=False,
        year_marks=cutoffs,
        year_balances=balances,
    )
    print(f"  Chart → {chart}")
    print()

    return goal_met


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the autonomous backtest runner.

    Returns:
        Parsed :class:`argparse.Namespace` with all CLI option values.
    """
    _cagr_d = f"{GOAL_MIN_CAGR_PCT:.0f}"
    _dd_d   = f"{GOAL_MAX_DD_PCT:.0f}"
    _frac_d = f"{GOAL_MIN_YEAR_FRAC:.2f}"

    parser = argparse.ArgumentParser(
        description="Autonomous BTCUSDT backtest runner  [WFO ON by default]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "WFO is the default execution mode — use --no-wfo for classic static mode.\n"
            "\n"
            "Autonomous feature flags:\n"
            "  --wfo / --no-wfo  Walk-forward BREAKOUT_PERIOD optimizer (default: ON)\n"
            "  --forecast        Markov regime forecast (blocks entries when choppy)\n"
            "  --adaptive        Adaptive regime framework (continuous TP/SL/size)\n"
            "  --all             Enable forecast + adaptive + adaptive-trail (WFO already on)\n"
            "\n"
            "Goal sub-criteria (all three must pass simultaneously):\n"
            f"  CAGR  >= --min-cagr   (default: {_cagr_d} pct/yr)\n"
            f"  MaxDD <= --max-dd     (default: {_dd_d} pct)\n"
            f"  Years >= --year-frac  (default: {_frac_d}  →  all years profitable)\n"
            "\n"
            "Examples:\n"
            "  python run_backtest.py                           # 5-year with WFO (default)\n"
            "  python run_backtest.py --no-wfo                  # classic static BREAKOUT_PERIOD=14\n"
            "  python run_backtest.py --forecast                # WFO + Markov regime forecast\n"
            "  python run_backtest.py --all --days 2190         # full stack, 6-year\n"
            "  python run_backtest.py --no-wfo --risk 10 --tp 7.0  # classic with custom params\n"
        ),
    )

    # ── Period / balance ──────────────────────────────────────────────────────
    parser.add_argument(
        "--days", type=int, default=None,
        help="Calendar days of historical data (default: 5×365=1825).",
    )
    parser.add_argument(
        "--balance", type=float, default=None,
        help="Starting balance in USD (default: 1000).",
    )
    parser.add_argument(
        "--symbol", type=str, default="BTCUSDT",
        help="Trading pair symbol (default: BTCUSDT).",
    )
    parser.add_argument(
        "--leverage", type=int, default=None,
        help="Futures leverage multiplier (default: 10).",
    )
    parser.add_argument(
        "--order-balance", type=float, default=None,
        metavar="USD",
        help="Fixed margin per order in USD (0 = disabled; uses RISK_PERCENT instead).",
    )

    # ── Autonomous features ───────────────────────────────────────────────────
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
        "--forecast", action="store_true", default=False,
        help="Enable Markov regime forecast (entry gate + size scaling).",
    )
    parser.add_argument(
        "--adaptive", action="store_true", default=False,
        help="Enable Adaptive Regime Framework (continuous TP/SL/size).",
    )
    parser.add_argument(
        "--adaptive-trail", action="store_true", default=False,
        help="Enable Adaptive Trailing Stop (tightening funnel toward TP).",
    )
    parser.add_argument(
        "--all", dest="all_features", action="store_true", default=False,
        help=(
            "Enable forecast + adaptive regime + adaptive trailing simultaneously. "
            "WFO is already on by default; combine with --no-wfo to disable it."
        ),
    )

    # ── Base config overrides ─────────────────────────────────────────────────
    parser.add_argument(
        "--risk", type=float, default=None,
        metavar="PCT",
        help="Risk percent per trade (default: 8.0).",
    )
    parser.add_argument(
        "--tp", type=float, default=None,
        metavar="MULT",
        help="ATR TP multiplier (default: 6.0).",
    )
    parser.add_argument(
        "--sl", type=float, default=None,
        metavar="MULT",
        help="ATR SL multiplier (default: 1.5).",
    )
    parser.add_argument(
        "--adx", type=float, default=None,
        metavar="MIN",
        help="Minimum ADX to allow entry (default: 20.0).",
    )
    parser.add_argument(
        "--breakout", type=int, default=None,
        metavar="BARS",
        help="Breakout period in 1H bars (default: 14; ignored while WFO is active).",
    )
    parser.add_argument(
        "--trail-lock", type=float, default=None,
        metavar="ATR",
        help="Lock 1×ATR profit after N×ATR move (default: 0 = off).",
    )

    # ── Goal overrides ────────────────────────────────────────────────────────
    parser.add_argument(
        "--min-cagr", type=float, default=None,
        metavar="PCT",
        help=f"Min CAGR to satisfy goal (default: {_cagr_d} pct/yr).",
    )
    parser.add_argument(
        "--max-dd", type=float, default=None,
        metavar="PCT",
        help=f"Max drawdown to satisfy goal (default: {_dd_d} pct).",
    )
    parser.add_argument(
        "--year-frac", type=float, default=None,
        metavar="FRAC",
        help=f"Min fraction of profitable years, 0.0–1.0 (default: {_frac_d}).",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()

    # ── Resolve period / balance ──────────────────────────────────────────────
    if args.balance is not None:
        INITIAL_BALANCE = args.balance          # noqa: F841
    if args.days is not None:
        DAYS  = args.days                       # noqa: F841
        YEARS = max(1, round(args.days / 365))  # noqa: F841

    # ── Goal threshold overrides ──────────────────────────────────────────────
    if args.min_cagr  is not None:
        GOAL_MIN_CAGR_PCT  = args.min_cagr      # noqa: F841
    if args.max_dd    is not None:
        GOAL_MAX_DD_PCT    = args.max_dd         # noqa: F841
    if args.year_frac is not None:
        GOAL_MIN_YEAR_FRAC = args.year_frac      # noqa: F841

    # ── Build config dict from base + CLI overrides ───────────────────────────
    run_config = dict(_BASE)

    if args.leverage       is not None:
        run_config["LEVERAGE"]           = args.leverage
    if args.order_balance  is not None:
        run_config["ORDER_BALANCE_USD"]  = args.order_balance
    if args.risk           is not None:
        run_config["RISK_PERCENT"]       = args.risk
    if args.tp             is not None:
        run_config["ATR_TP_MULTIPLIER"]  = args.tp
    if args.sl             is not None:
        run_config["ATR_SL_MULTIPLIER"]  = args.sl
    if args.adx            is not None:
        run_config["ADX_MIN"]            = args.adx
    if args.breakout       is not None:
        run_config["BREAKOUT_PERIOD"]    = args.breakout
    if args.trail_lock     is not None:
        run_config["TRAIL_LOCK_ATR"]     = args.trail_lock

    # ── Autonomous feature flags ──────────────────────────────────────────────
    use_all = args.all_features
    run_config["WFO_ENABLED"]              = args.wfo             or use_all
    run_config["REGIME_FORECAST_ENABLED"]  = args.forecast        or use_all
    run_config["ADAPTIVE_REGIME_ENABLED"]  = args.adaptive        or use_all
    run_config["ADAPTIVE_TRAILING_ENABLED"] = args.adaptive_trail or use_all

    # Apply to live config module
    _apply_config(run_config)

    ok = main(
        days=DAYS,
        initial_balance=INITIAL_BALANCE,
        years=YEARS,
        symbol=args.symbol,
    )
    sys.exit(0 if ok else 1)
