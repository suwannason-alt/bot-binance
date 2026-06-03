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


def _has_trades(stats: Dict[str, Any]) -> bool:
    """A backtest with no trades returns a stats dict lacking these keys."""
    return "profit_factor" in stats


def build_row(symbol: str,
              s115_train: Dict[str, Any], s110_train: Dict[str, Any],
              s115_test: Dict[str, Any], s110_test: Dict[str, Any],
              ruin_floor: float = RUIN_FLOOR) -> Dict[str, Any]:
    """Assemble one result row.

    Verdict and ΔPF come from the TEST window only (out-of-sample rule). The
    1.10 TRAIN profit factor and the train→test ``decay`` (pf_110_test minus
    pf_110_train; negative ⇒ worse out-of-sample ⇒ overfit) are reported so an
    overfit asset is visible. A window that produced no trades yields a
    ``NODATA`` row instead of raising.
    """
    pf110_train = s110_train.get("profit_factor")

    if not (_has_trades(s115_test) and _has_trades(s110_test)):
        return {
            "symbol":          symbol,
            "pf_115_test":     s115_test.get("profit_factor"),
            "pf_110_test":     s110_test.get("profit_factor"),
            "pf_110_train":    pf110_train,
            "delta_pf":        None,
            "decay":           None,
            "maxdd_110_test":  s110_test.get("max_drawdown_pct"),
            "cagr_110_test":   s110_test.get("cagr_pct"),
            "trades_110_test": s110_test.get("wins", 0) + s110_test.get("losses", 0),
            "verdict":         "NODATA",
        }

    pf115 = s115_test["profit_factor"]
    pf110 = s110_test["profit_factor"]
    dd110 = s110_test["max_drawdown_pct"]
    decay = (pf110 - pf110_train) if pf110_train is not None else None
    return {
        "symbol":          symbol,
        "pf_115_test":     pf115,
        "pf_110_test":     pf110,
        "pf_110_train":    pf110_train,
        "delta_pf":        pf110 - pf115,
        "decay":           decay,
        "maxdd_110_test":  dd110,
        "cagr_110_test":   s110_test["cagr_pct"],
        "trades_110_test": s110_test["wins"] + s110_test["losses"],
        "verdict":         verdict(pf115, pf110, dd110, ruin_floor=ruin_floor),
    }


def _sort_key_num(x: Any) -> float:
    """Sort helper: None/NaN sink to the bottom under reverse=True."""
    if isinstance(x, (int, float)) and x == x:   # real number, not NaN
        return float(x)
    return float("-inf")


def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort by ΔPF desc, then MaxDD least-negative first. NODATA rows sink last."""
    return sorted(
        rows,
        key=lambda r: (_sort_key_num(r["delta_pf"]), _sort_key_num(r["maxdd_110_test"])),
        reverse=True,
    )


def _fmt_pf(x: Any) -> str:
    """Profit-factor cell: dash for None/NaN, 'inf' for infinity, else 2dp."""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    if x == float("inf"):
        return "inf"
    return f"{x:.2f}"


def _fmt_delta(x: Any) -> str:
    """Signed ΔPF / decay cell."""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    if x == float("inf"):
        return "inf"
    return f"{x:+.2f}"


def _fmt_pct(x: Any) -> str:
    """Percentage cell."""
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:.1f}%"


def format_matrix(rows: List[Dict[str, Any]]) -> str:
    """Render the sorted verdict table as a single console string.

    Columns include the 1.10 TRAIN profit factor and the train→test ``Decay`` so
    overfit assets (strong in-sample, weak out-of-sample) stand out. NODATA rows
    (a window with no trades) render with dashes and sort to the bottom.
    """
    header = (
        f"{'Asset':<9} {'PF1.15t':>8} {'PF1.10t':>8} {'ΔPF':>7} "
        f"{'PF1.10tr':>9} {'Decay':>7} {'MaxDD1.10t':>11} "
        f"{'CAGR1.10t':>10} {'Trades':>7} {'VERDICT':>8}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in sort_rows(rows):
        lines.append(
            f"{r['symbol']:<9} {_fmt_pf(r['pf_115_test']):>8} "
            f"{_fmt_pf(r['pf_110_test']):>8} {_fmt_delta(r['delta_pf']):>7} "
            f"{_fmt_pf(r['pf_110_train']):>9} {_fmt_delta(r['decay']):>7} "
            f"{_fmt_pct(r['maxdd_110_test']):>11} {_fmt_pct(r['cagr_110_test']):>10} "
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
                   frac: float = SPLIT_FRAC,
                   ruin_floor: float = RUIN_FLOOR) -> Dict[str, Any]:
    """Fetch, split, run the 2×2 matrix, and build the result row for one asset.

    Runs both gates on the train and test windows. The verdict uses the TEST
    window only (out-of-sample); the TRAIN 1.10 result is reported so the
    train→test decay is visible for overfitting detection.
    """
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol=symbol, days=days))
    tr5, tr1, te5, te1 = split_by_time(df_5m, df_1h, frac)

    s115_train = run_single(tr5, tr1, atr_ratio=1.15)
    s110_train = run_single(tr5, tr1, atr_ratio=1.10)
    s115_test  = run_single(te5, te1, atr_ratio=1.15)
    s110_test  = run_single(te5, te1, atr_ratio=1.10)
    return build_row(symbol, s115_train, s110_train, s115_test, s110_test,
                     ruin_floor=ruin_floor)


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
    rows: List[Dict[str, Any]] = []
    for symbol in args.candidates:
        print(f"\n=== {symbol} ===")
        try:
            rows.append(evaluate_asset(symbol, days=args.days, frac=args.split,
                                       ruin_floor=args.ruin_floor))
        except Exception as exc:  # noqa: BLE001 — keep sweeping other assets
            print(f"  SKIP {symbol}: {exc}")
    print("\n" + format_matrix(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
