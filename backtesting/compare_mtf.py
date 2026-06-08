"""
Comparative backtest — Baseline (1H strict) vs Challenger (1H filter + 5M
trigger + resting breakout STOP_MARKET).

Both arms share the IDENTICAL config (risk, trail, sizing, daily caps, WFO);
the ONLY difference is the execution architecture (``mode``).  This isolates
the architectural change so the comparison is a true A/B.

  Baseline   : mode="1h"        — evaluate the full funnel only at the 1H close,
                                   enter at the 1H close price.
  Challenger : mode="mtf_stop"  — 1H trend/ADX/slope filter cached at each 1H
                                   close; RSI/volume funnel + resting STOP_MARKET
                                   breakout evaluated on every 5M bar; fill at the
                                   breakout level the instant 5M price touches it.

Shared config (mirrors .env live profile):
  RISK_PERCENT=4.0   TRAIL 1.5 / 2.0 / 1.2   SL×1.5  TP×6.0  ADX≥20  WFO on.

Usage::

    python backtesting/compare_mtf.py                 # 5-year (default), USD daily caps
    python backtesting/compare_mtf.py --days 1825
    python backtesting/compare_mtf.py --no-caps       # also isolate the daily-cap confound
"""
from __future__ import annotations

# ── Path bootstrap (modular layout — keep flat imports resolvable) ─────────────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import argparse
import asyncio
import logging

import pandas as pd

import backtest
import config_1h as config   # 1H baseline arm reads the 1H strategy config
import fetch_data
import run_backtest as rb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("compare_mtf")

INITIAL_BALANCE: float = 1_000.0


def _build_config(no_caps: bool) -> dict:
    """Return the shared run config (live .env profile) for both arms.

    Args:
        no_caps: When ``True``, disable the USD daily circuit-breakers so the
            architecture effect can be measured without the dollar-cap confound.
    """
    cfg = dict(rb._BASE)
    cfg.update({
        "RISK_PERCENT":            4.0,    # .env live profile (capital preservation)
        "TRAIL_ACTIVATE_ATR":      1.5,
        "TRAIL_LOCK_ATR":          2.0,
        "TRAIL_STOP_ATR":          1.2,
        "ADAPTIVE_TRAILING_ENABLED": False,
        "WFO_ENABLED":             True,
    })
    if no_caps:
        cfg.update({
            "DAILY_PROFIT_TARGET_USD": 0.0,
            "DAILY_LOSS_LIMIT_USD":    0.0,
            "DAILY_PROFIT_TARGET_PCT": 0.0,
            "DAILY_LOSS_LIMIT_PCT":    0.0,
        })
    return cfg


def _row(stats: dict) -> dict:
    """Extract the headline metrics (+ risk-adjusted) from a stats dict."""
    cagr = stats.get("cagr_pct", 0.0)
    dd   = stats.get("max_drawdown_pct", 0.0)
    return {
        "trades":  stats.get("total_trades", 0),
        "win":     stats.get("win_rate", 0.0),
        "pf":      stats.get("profit_factor", 0.0),
        "dd":      dd,
        "cagr":    cagr,
        "sharpe":  stats.get("sharpe", 0.0),
        "ret":     stats.get("total_return_pct", 0.0),
        "final":   stats.get("final_balance", 0.0),
        "mar":     (cagr / abs(dd)) if dd != 0 else float("inf"),   # CAGR / |MaxDD|
        "tp":      stats.get("tp_exits", 0),
        "sl":      stats.get("sl_exits", 0),
        "be":      stats.get("be_exits", 0),
        "dph":     stats.get("days_profit_hit", 0),
        "dlh":     stats.get("days_loss_hit", 0),
    }


def _print_table(base: dict, chal: dict, label: str) -> None:
    """Print the side-by-side comparison table for one cap-configuration.

    Every metric is oriented so a HIGHER value is better (drawdown is stored
    signed-negative, so −40% > −50% correctly reads as an improvement).  The Δ
    arrow ▲/▼ therefore means better/worse for all rows.
    """
    def delta(b, c, unit=""):
        d = c - b
        arrow = "▲" if d > 0 else ("▼" if d < 0 else "—")
        return f"{arrow} {d:+.2f}{unit}"

    print()
    print("═" * 78)
    print(f"  COMPARISON — {label}")
    print("═" * 78)
    print(f"  {'Metric':<22}{'Baseline (1H)':>16}{'Challenger (MTF)':>18}{'Δ':>20}")
    print("  " + "─" * 74)
    print(f"  {'Trade count':<22}{base['trades']:>16d}{chal['trades']:>18d}"
          f"{delta(base['trades'], chal['trades']):>20}")
    print(f"  {'Win rate %':<22}{base['win']:>16.2f}{chal['win']:>18.2f}"
          f"{delta(base['win'], chal['win'], 'pp'):>20}")
    print(f"  {'Profit factor':<22}{base['pf']:>16.2f}{chal['pf']:>18.2f}"
          f"{delta(base['pf'], chal['pf']):>20}")
    print(f"  {'Max drawdown %':<22}{base['dd']:>16.2f}{chal['dd']:>18.2f}"
          f"{delta(base['dd'], chal['dd'], 'pp'):>20}")
    print(f"  {'CAGR %/yr':<22}{base['cagr']:>16.2f}{chal['cagr']:>18.2f}"
          f"{delta(base['cagr'], chal['cagr'], 'pp'):>20}")
    print("  " + "─" * 74 + "   risk-adjusted")
    print(f"  {'Sharpe (daily→ann)':<22}{base['sharpe']:>16.2f}{chal['sharpe']:>18.2f}"
          f"{delta(base['sharpe'], chal['sharpe']):>20}")
    print(f"  {'MAR (CAGR/|DD|)':<22}{base['mar']:>16.2f}{chal['mar']:>18.2f}"
          f"{delta(base['mar'], chal['mar']):>20}")
    print(f"  {'Total return %':<22}{base['ret']:>16.1f}{chal['ret']:>18.1f}"
          f"{delta(base['ret'], chal['ret'], 'pp'):>20}")
    print(f"  {'Final $ (1k start)':<22}{base['final']:>16,.0f}{chal['final']:>18,.0f}"
          f"{'':>20}")
    print("  " + "─" * 74 + "   exits / daily caps")
    base_exits = f"{base['tp']}/{base['sl']}/{base['be']}"
    chal_exits = f"{chal['tp']}/{chal['sl']}/{chal['be']}"
    base_caps  = f"{base['dph']}/{base['dlh']}"
    chal_caps  = f"{chal['dph']}/{chal['dlh']}"
    print(f"  {'TP / SL / BE exits':<22}{base_exits:>16}{chal_exits:>18}")
    print(f"  {'Days profit/loss cap':<22}{base_caps:>16}{chal_caps:>18}")
    print("═" * 78)


def main(days: int, no_caps: bool) -> None:
    """Fetch data once, run both arms under identical config, print the report."""
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol="BTCUSDT", days=days))
    t0 = pd.Timestamp(df_5m["open_time"].iloc[0],  unit="ms")
    tN = pd.Timestamp(df_5m["open_time"].iloc[-1], unit="ms")
    years = (tN - t0).days / 365.25
    logger.info("Window: %s → %s  (%.2f yr)  5M=%d  1H=%d",
                t0.date(), tN.date(), years, len(df_5m), len(df_1h))

    rb._apply_config(_build_config(no_caps))
    cap_label = "daily caps OFF (architecture-isolated)" if no_caps \
        else f"USD daily caps ${config.DAILY_PROFIT_TARGET_USD:.0f}/${config.DAILY_LOSS_LIMIT_USD:.0f}"
    logger.info("Shared config: RISK=%.1f%%  TRAIL %.1f/%.1f/%.1f  SL×%.1f TP×%.1f  ADX≥%.0f  WFO=%s  |  %s",
                config.RISK_PERCENT, config.TRAIL_ACTIVATE_ATR, config.TRAIL_LOCK_ATR,
                config.TRAIL_STOP_ATR, config.ATR_SL_MULTIPLIER, config.ATR_TP_MULTIPLIER,
                config.ADX_MIN, config.WFO_ENABLED, cap_label)

    print("\n" + "#" * 78)
    print("#  BASELINE  — mode='1h'  (1H strict evaluation, enter at 1H close)")
    print("#" * 78)
    res_base = backtest.run(df_5m, df_1h, initial_balance=INITIAL_BALANCE, mode="1h")

    print("\n" + "#" * 78)
    print("#  CHALLENGER — mode='mtf_stop'  (1H filter + 5M trigger + resting STOP_MARKET)")
    print("#" * 78)
    res_chal = backtest.run(df_5m, df_1h, initial_balance=INITIAL_BALANCE, mode="mtf_stop")

    _print_table(_row(res_base.stats), _row(res_chal.stats),
                 label=f"{years:.1f}-yr BTCUSDT  |  {cap_label}")
    print()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Baseline vs MTF-stop comparative backtest")
    p.add_argument("--days", type=int, default=1825, help="History window in days (default 1825 = 5yr).")
    p.add_argument("--no-caps", action="store_true", help="Disable USD daily caps (isolate architecture).")
    args = p.parse_args()
    main(days=args.days, no_caps=args.no_caps)
