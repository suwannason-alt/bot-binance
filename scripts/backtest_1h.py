"""
Primary 1H backtest — single-symbol OR multi-asset portfolio under the STEP trail.

This is the production backtest entry point for the feature-based multi-asset system.
Pick any subset of symbols (one, two, or all three); each runs its OWN tuned profile
pulled straight from its domain config (``btc/config.py`` / ``eth/config.py`` /
``sol/config.py``, assembled into ``config_1h.CONFIG_MATRIX``) as an independent
$1,000 capital sleeve at RISK=0.5%, under the profit-locking STEP trailing stop, over
the full 5-year window.  Intra-hour STEP trailing is driven by the high-resolution 5M
bars (REST-fetched ``df_5m``) so stop ratchets fire mid-bar, not just on the 1H close.
Output is a per-symbol performance table plus a Total Portfolio Aggregated row.

Configure which symbols to test either via ``--symbols`` or by editing ``SYMBOLS``
below (``None`` = every ENABLED asset in the CONFIG_MATRIX).  The heavy lifting lives
in ``backtest.run_portfolio`` (the composite execution loop); this script only fetches
data and renders the console table.  ``run_portfolio`` reuses the parity-proven
single-symbol ``run()`` per sleeve, so a one-symbol run is a true single-asset backtest.

Honesty notes (read before trusting the headline):
  * The STEP regime is UNVALIDATED on these params — the ETH/SOL grid sweep used a
    break-even-only trail, BTC's prior live profile was the classic 1.5/2.0/1.2
    cascade, and BTC never went through the sweep.  This run is the *first* look at
    STEP trailing on these configs, not a confirmation of it.
  * ETH/SOL matrix params are in-sample grid optima pinned at the TP=6.0 grid ceiling
    (boundary optimum) — see project_altcoin_1h_tuning_sweep.

Usage::

    python scripts/backtest_1h.py                                   # all ENABLED, 5-year
    python scripts/backtest_1h.py --symbols BTCUSDT                 # BTC only
    python scripts/backtest_1h.py --symbols BTCUSDT ETHUSDT SOLUSDT # explicit portfolio
    python scripts/backtest_1h.py --days 1095                       # 3-year window
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
import config_1h as config
import fetch_data

logging.disable(logging.WARNING)   # silence per-run backtest noise; keep stdout clean

# ── Run configuration (override on the CLI; see __main__) ──────────────────────
SYMBOLS: list | None = None        # None = every ENABLED symbol in CONFIG_MATRIX
INIT_PER_ASSET      = 1000.0       # starting capital per symbol sleeve (USDT)
RISK_PERCENT        = 0.5          # % of sleeve balance risked per trade


def main(days: int, symbols: list | None) -> None:
    # ENABLED gates inclusion in the backtest; TRADE_MODE is a LIVE-only concept
    # (real orders vs dry-run) and does NOT affect the backtest — every selected asset
    # is simulated identically here.
    assets = symbols if symbols else config.enabled_symbols()

    _scope = "SINGLE-ASSET" if len(assets) == 1 else "MULTI-ASSET PORTFOLIO"
    print("═" * 104)
    print(f"{_scope} 1H BACKTEST — STEP profit-locking trail")
    print(f"Symbols: {', '.join(assets)}   |   {round(days/365)}yr   |   "
          f"${INIT_PER_ASSET:.0f}/sleeve  (${INIT_PER_ASSET*len(assets):.0f} portfolio)   |   "
          f"RISK={RISK_PERCENT}%/asset   |   WFO OFF · caps OFF · STEP trail ON")
    print("Per-symbol profile (BREAKOUT / ADX / TP / SL / TRAIL_ACTIVATE) from CONFIG_MATRIX:")
    for sym in assets:
        p = config.CONFIG_MATRIX.get(sym, {})
        print(f"    {sym:8} BP={p.get('BREAKOUT_PERIOD'):>2}  ADX≥{p.get('ADX_MIN'):<4} "
              f"TP={p.get('ATR_TP_MULTIPLIER')}×  SL={p.get('ATR_SL_MULTIPLIER')}×  "
              f"trail-act={p.get('TRAIL_ACTIVATE_ATR')}×ATR   "
              f"[live mode: {config.trade_mode(sym)}]")
    print("═" * 104)

    # ── Fetch all assets up front ─────────────────────────────────────────────
    datasets = {}
    for sym in assets:
        df5, df1 = asyncio.run(fetch_data.fetch_all(symbol=sym, days=days))
        t0 = pd.Timestamp(df5["open_time"].iloc[0], unit="ms")
        tN = pd.Timestamp(df5["open_time"].iloc[-1], unit="ms")
        print(f"  [{sym}] data: 5m={len(df5)} 1h={len(df1)}  {t0.date()} → {tN.date()} "
              f"({(tN-t0).days/365.25:.2f}yr)")
        datasets[sym] = (df5, df1)

    # ── Composite run (per-asset sleeves under STEP trail) ────────────────────
    print("\n  running composite portfolio (per-asset sleeves)…")
    out = backtest.run_portfolio(datasets, initial_balance_per_asset=INIT_PER_ASSET,
                                 risk_percent=RISK_PERCENT)
    rows, port = out["rows"], out["portfolio"]

    # ── Performance table ─────────────────────────────────────────────────────
    print("\n" + "─" * 104)
    print("  FINAL 5-YEAR MULTI-ASSET PERFORMANCE — profit-locked STEP trailing")
    print(f"  {'Asset':<10} {'Trades':>7} {'Win%':>7} {'PF':>7} {'MaxDD%':>9} "
          f"{'NetProfit$':>14} {'Final$':>12} {'CAGR%':>8}   exits TP/SL/BE")
    print("  " + "─" * 100)
    for sym in assets:
        r = rows[sym]
        print(f"  {sym:<10} {r['trades']:>7} {r['win_rate']:>7.2f} {r['profit_factor']:>7.2f} "
              f"{r['max_drawdown_pct']:>9.2f} {r['net_profit']:>+14,.2f} {r['final_balance']:>12,.2f} "
              f"{r['cagr_pct']:>8.2f}   {r['tp_exits']}/{r['sl_exits']}/{r['be_exits']}")
    print("  " + "─" * 100)
    print(f"  {'PORTFOLIO':<10} {port['trades']:>7} {port['win_rate']:>7.2f} "
          f"{port['profit_factor']:>7.2f} {port['max_drawdown_pct']:>9.2f} "
          f"{port['net_profit']:>+14,.2f} {port['final_balance']:>12,.2f} {port['cagr_pct']:>8.2f}")
    print("  " + "─" * 100)
    print(f"  Portfolio = sum of {len(assets)} independent ${INIT_PER_ASSET:.0f} sleeves "
          f"(start ${port['initial_balance']:,.0f}); MaxDD from union-index summed equity; "
          f"PF/Win% pool all trades.")
    print("─" * 104)
    print("\n⚠️  STEP trailing is UNVALIDATED on these params (sweep used BE-only / classic);")
    print("    ETH/SOL configs are in-sample grid optima pinned at the TP=6.0 ceiling.")
    print("    Treat as a first-look hypothesis, not a deployable/validated result.\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Primary 1H backtest — single-symbol or multi-asset portfolio (STEP trail)")
    ap.add_argument("--days", type=int, default=1825, help="History window (default 1825 = 5yr)")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS,
                    help="Symbols to backtest, e.g. --symbols BTCUSDT ETHUSDT "
                         "(default: all ENABLED in CONFIG_MATRIX)")
    main(**vars(ap.parse_args()))
