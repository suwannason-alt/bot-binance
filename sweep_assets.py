"""Multi-asset ATR_RATIO sweep — offline research tool.

Screens Binance perpetual pairs for compatibility with the loose
ATR_RATIO_MIN = 1.10 volatility gate, using out-of-sample, per-asset
1.10-vs-1.15 head-to-head validation. See
docs/superpowers/specs/2026-06-03-multi-asset-atr-sweep-design.md.

Run:  python sweep_assets.py            # full sweep, 5y, default candidates
      python sweep_assets.py --days 730 # custom window
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any, Dict, List, Tuple

import pandas as pd

import backtest
import config
import fetch_data

RUIN_FLOOR = -50.0      # MaxDD (%) below this disqualifies an asset
SPLIT_FRAC = 0.70       # train fraction; remainder is out-of-sample test

DEFAULT_CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                      "AVAXUSDT", "LINKUSDT", "NEARUSDT"]


def verdict(pf_115_test: float, pf_110_test: float,
            maxdd_110_test: float, ruin_floor: float = RUIN_FLOOR) -> str:
    """Classify an asset from its TEST-window metrics.

    RUIN  — 1.10 drawdown breaches the ruin floor (overrides everything).
    FAIL  — 1.10 profit factor degrades vs the 1.15 baseline.
    PASS  — 1.10 holds its own on profit factor and survives the floor.
    """
    if maxdd_110_test < ruin_floor:
        return "RUIN"
    if pf_110_test < pf_115_test:
        return "FAIL"
    return "PASS"


def build_row(symbol: str, s115_test: Dict[str, Any],
              s110_test: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble one result row from the two TEST-window stats dicts."""
    pf115 = s115_test["profit_factor"]
    pf110 = s110_test["profit_factor"]
    dd110 = s110_test["max_drawdown_pct"]
    return {
        "symbol":          symbol,
        "pf_115_test":     pf115,
        "pf_110_test":     pf110,
        "delta_pf":        pf110 - pf115,
        "maxdd_110_test":  dd110,
        "cagr_110_test":   s110_test["cagr_pct"],
        "trades_110_test": s110_test["wins"] + s110_test["losses"],
        "verdict":         verdict(pf115, pf110, dd110),
    }


def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort by ΔPF descending, then by MaxDD (least-negative first)."""
    return sorted(rows, key=lambda r: (r["delta_pf"], r["maxdd_110_test"]),
                  reverse=True)


def _fmt_pf(pf: float) -> str:
    return "  inf" if pf == float("inf") else f"{pf:5.2f}"


def format_matrix(rows: List[Dict[str, Any]]) -> str:
    """Render the sorted verdict table as a single console string."""
    header = (
        f"{'Asset':<9} {'PF1.15t':>8} {'PF1.10t':>8} {'ΔPF':>7} "
        f"{'MaxDD1.10t':>11} {'CAGR1.10t':>10} {'Trades':>7} {'VERDICT':>8}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in sort_rows(rows):
        dpf = r["delta_pf"]
        dpf_s = "    inf" if dpf == float("inf") else f"{dpf:+7.2f}"
        lines.append(
            f"{r['symbol']:<9} {_fmt_pf(r['pf_115_test']):>8} "
            f"{_fmt_pf(r['pf_110_test']):>8} {dpf_s:>7} "
            f"{r['maxdd_110_test']:>10.1f}% {r['cagr_110_test']:>9.1f}% "
            f"{r['trades_110_test']:>7d} {r['verdict']:>8}"
        )
    return "\n".join(lines)


def run_single(df_5m: pd.DataFrame, df_1h: pd.DataFrame, atr_ratio: float,
               initial_balance: float = 1000.0) -> Dict[str, Any]:
    """Apply the fixed sweep config + the given gate, run one backtest.

    Holds RISK=8, TP=6.0, SL=1.5, BREAKOUT=14 constant and forces WFO OFF so
    the experiment isolates ATR_RATIO_MIN. Returns ``BacktestResult.stats``.
    """
    config.ATR_RATIO_MIN     = atr_ratio
    config.RISK_PERCENT      = 8.0
    config.ATR_TP_MULTIPLIER = 6.0
    config.ATR_SL_MULTIPLIER = 1.5
    config.BREAKOUT_PERIOD   = 14
    config.WFO_ENABLED       = False
    result = backtest.run(df_5m, df_1h, initial_balance=initial_balance,
                          mode="1h")
    return result.stats


def split_by_time(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                  frac: float = SPLIT_FRAC
                  ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split both feeds at one shared wall-clock boundary.

    The boundary is the 1h ``open_time`` at the ``frac`` index, so train and
    test windows are identical across the 5m and 1h feeds.

    Returns ``(train_5m, train_1h, test_5m, test_1h)`` with reset indices.
    """
    idx = min(int(len(df_1h) * frac), len(df_1h) - 1)
    boundary = int(df_1h["open_time"].iloc[idx])
    tr1 = df_1h[df_1h["open_time"] < boundary].reset_index(drop=True)
    te1 = df_1h[df_1h["open_time"] >= boundary].reset_index(drop=True)
    tr5 = df_5m[df_5m["open_time"] < boundary].reset_index(drop=True)
    te5 = df_5m[df_5m["open_time"] >= boundary].reset_index(drop=True)
    return tr5, tr1, te5, te1


def evaluate_asset(symbol: str, days: int,
                   frac: float = SPLIT_FRAC) -> Dict[str, Any]:
    """Fetch, split, run the 2×2 matrix, and build the result row for one asset.

    The train-window runs execute (and surface fetch/data errors early) but only
    the TEST-window stats drive the verdict, per the spec's out-of-sample rule.
    """
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol=symbol, days=days))
    tr5, tr1, te5, te1 = split_by_time(df_5m, df_1h, frac)

    run_single(tr5, tr1, atr_ratio=1.15)          # train baseline (context only)
    run_single(tr5, tr1, atr_ratio=1.10)          # train loose    (context only)
    s115_test = run_single(te5, te1, atr_ratio=1.15)
    s110_test = run_single(te5, te1, atr_ratio=1.10)
    return build_row(symbol, s115_test, s110_test)


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI options for the sweep."""
    p = argparse.ArgumentParser(
        description="Multi-asset ATR_RATIO 1.10-vs-1.15 out-of-sample sweep.",
    )
    p.add_argument("--days", type=int, default=1825,
                   help="Calendar days of history per asset (default: 1825 = 5y).")
    p.add_argument("--split", type=float, default=SPLIT_FRAC,
                   help=f"Train fraction, 0-1 (default: {SPLIT_FRAC}).")
    p.add_argument("--ruin-floor", dest="ruin_floor", type=float,
                   default=RUIN_FLOOR,
                   help=f"MaxDD %% disqualify floor (default: {RUIN_FLOOR}).")
    p.add_argument("--candidates", type=lambda s: s.split(","),
                   default=DEFAULT_CANDIDATES,
                   help="Comma-separated symbols (default: built-in pool).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    """Run the sweep across all candidates and print the verdict matrix."""
    args = parse_args(argv)
    global RUIN_FLOOR
    RUIN_FLOOR = args.ruin_floor
    rows: List[Dict[str, Any]] = []
    for symbol in args.candidates:
        print(f"\n=== {symbol} ===")
        try:
            rows.append(evaluate_asset(symbol, days=args.days, frac=args.split))
        except Exception as exc:  # noqa: BLE001 — keep sweeping other assets
            print(f"  SKIP {symbol}: {exc}")
    print("\n" + format_matrix(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
