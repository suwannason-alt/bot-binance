"""
BTCUSDT 1H STEP-trail hyperparameter optimization — FIXED RISK_PERCENT = 4.0%.

Pure quant-research sweep over the 1H momentum-breakout engine under the
profit-locking STEP trailing stop, executed against high-resolution 5M bars so the
intra-hour step ratchet fires mid-bar (not just on the 1H close).  Execution risk is
LOCKED at 4% of balance per trade (live profile) across every run — no low-risk
profiles are evaluated.

Search space (self-designed; ranges carry headroom so a winner pinned to an edge is
visible rather than hidden):

    BREAKOUT_PERIOD     10, 14, 20, 28          rolling high/low lookback
    ADX_MIN             18, 22, 26, 30          trend-strength floor
    ATR_TP_MULTIPLIER   4.0, 6.0, 8.0, 10.0     take-profit distance (×ATR)
    ATR_SL_MULTIPLIER   1.0, 1.5, 2.0, 2.5      hard-stop distance (×ATR)
    TRAIL_STEP_ATR      0.5, 1.0, 1.5           STEP ratchet interval (×ATR)   ← the new knob

TRAIL_ACTIVATE_ATR is fixed at 1.0 (break-even locked after 1×ATR of favour — early
capital protection, consistent with the objective).  4 × 4 × 4 × 4 × 3 = 768 combos.

Ranking (objective = smoothest equity curve under 4% risk, then quality):
    1. GATE to a credible universe first  — enough trades to be real, PF>1.2, net>0.
       (a literal "min-DD-first" sort otherwise crowns whatever barely trades.)
    2. Within the gated set: sort by MaxDD ascending (smallest give-back first),
       tie-break Profit Factor desc, then Win Rate desc.

Loads the cached BTC CSVs directly (offline / deterministic — no incremental refetch
that would rewrite the cache and drift the numbers).  Console output only.

Usage::

    python scripts/optimize_btc_step_1h.py
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

import itertools
import logging
import time

import numpy as np
import pandas as pd

import backtest
import config_1h as config

logging.disable(logging.WARNING)   # silence per-run engine noise; keep stdout clean

# ── Locked execution constants ────────────────────────────────────────────────
RISK_PERCENT       = 4.0      # NON-NEGOTIABLE — live execution profile
TRAIL_ACTIVATE_ATR = 1.0     # break-even gate (fixed across the sweep)
INITIAL_BALANCE    = 1000.0

# ── Self-designed search space (Stage 1 — broad) ──────────────────────────────
GRID_STAGE1 = {
    "BREAKOUT_PERIOD":   [10, 14, 20, 28],
    "ADX_MIN":           [18.0, 22.0, 26.0, 30.0],
    "ATR_TP_MULTIPLIER": [4.0, 6.0, 8.0, 10.0],
    "ATR_SL_MULTIPLIER": [1.0, 1.5, 2.0, 2.5],
    "TRAIL_STEP_ATR":    [0.5, 1.0, 1.5],
}

# ── Stage 2 — pushes past every Stage-1 boundary the winner pinned to ──────────
# Stage-1 DD-min winner pinned at BP=10(min), ADX=30(max), TP=4.0(min), STEP=1.5(max);
# only SL=2.0 was interior.  Stage 2 extends each pinned axis outward to locate the
# true interior optimum (and confirm the new STEP knob's best value isn't a ceiling).
GRID_STAGE2 = {
    "BREAKOUT_PERIOD":   [6, 8, 10, 14],
    "ADX_MIN":           [28.0, 30.0, 34.0, 38.0],
    "ATR_TP_MULTIPLIER": [3.0, 4.0, 5.0],
    "ATR_SL_MULTIPLIER": [1.5, 2.0, 2.5],
    "TRAIL_STEP_ATR":    [1.5, 2.0, 2.5, 3.0],
}

GRID = GRID_STAGE1

# ── Gating thresholds (credible-universe filter applied BEFORE the DD sort) ────
GATE_PF_MIN  = 1.20
GATE_NET_MIN = 0.0       # must end profitable
# Trade-count floor is derived from the grid's own distribution at runtime.

_CSV_COLS_NOTE = "cached BTC CSVs (offline, deterministic)"


def _load_btc() -> tuple[pd.DataFrame, pd.DataFrame]:
    df5 = pd.read_csv(_REPO_ROOT / "data" / "btcusdt_5m.csv")
    df1 = pd.read_csv(_REPO_ROOT / "data" / "btcusdt_1h.csv")
    return df5, df1


def _pin_static_config() -> None:
    """Pin every global the engine reads that is NOT swept, once up front."""
    config.WFO_ENABLED               = False   # honor the swept BREAKOUT_PERIOD
    config.STEP_TRAILING_ENABLED     = True    # profit-locking ladder regime
    config.ADAPTIVE_TRAILING_ENABLED = False
    config.EQUITY_PERCENT            = 0.0     # → activate RISK_PERCENT mode
    config.ORDER_BALANCE_USD         = 0.0
    config.RISK_USD                  = 0.0
    config.RISK_PERCENT              = RISK_PERCENT
    config.DAILY_PROFIT_TARGET_USD   = 0.0     # caps OFF — clean compounding
    config.DAILY_LOSS_LIMIT_USD      = 0.0
    config.DAILY_PROFIT_TARGET_PCT   = 0.0
    config.DAILY_LOSS_LIMIT_PCT      = 0.0
    config.TRAIL_ACTIVATE_ATR        = TRAIL_ACTIVATE_ATR
    config.MIN_CANDLES_1H            = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)


def main() -> None:
    df5, df1 = _load_btc()
    t0 = pd.Timestamp(df5["open_time"].iloc[0], unit="ms")
    tN = pd.Timestamp(df5["open_time"].iloc[-1], unit="ms")
    span_yr = (tN - t0).days / 365.25

    keys = list(GRID.keys())
    combos = list(itertools.product(*(GRID[k] for k in keys)))
    n = len(combos)

    print("═" * 100)
    print("BTCUSDT 1H STEP-TRAIL OPTIMIZATION — RISK LOCKED AT 4.0%")
    print(f"Data: {_CSV_COLS_NOTE}  |  5m={len(df5):,} 1h={len(df1):,}  |  "
          f"{t0.date()} → {tN.date()} ({span_yr:.2f}yr)")
    print(f"Search space: {n} combos  |  TRAIL_ACTIVATE_ATR={TRAIL_ACTIVATE_ATR} (fixed)  |  "
          f"WFO OFF · caps OFF · STEP trail ON")
    print("═" * 100)

    _pin_static_config()

    rows: list[dict] = []
    t_start = time.time()
    for k, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        config.BREAKOUT_PERIOD   = params["BREAKOUT_PERIOD"]
        config.ADX_MIN           = params["ADX_MIN"]
        config.ATR_TP_MULTIPLIER = params["ATR_TP_MULTIPLIER"]
        config.ATR_SL_MULTIPLIER = params["ATR_SL_MULTIPLIER"]
        config.TRAIL_STEP_ATR    = params["TRAIL_STEP_ATR"]

        res = backtest.run(df5, df1, initial_balance=INITIAL_BALANCE, mode="1h")
        s = res.stats
        rows.append({
            **params,
            "trades":     s["total_trades"],
            "win_rate":   s.get("win_rate", 0.0),
            "pf":         s.get("profit_factor", 0.0),
            "max_dd":     s.get("max_drawdown_pct", 0.0),
            "net":        s["final_balance"] - INITIAL_BALANCE,
            "final":      s["final_balance"],
            "cagr":       s.get("cagr_pct", 0.0),
            "tp":         s.get("tp_exits", 0),
            "sl":         s.get("sl_exits", 0),
            "be":         s.get("be_exits", 0),
        })
        if k % 50 == 0 or k == n:
            el = time.time() - t_start
            print(f"  [{k:>3}/{n}] {el:5.0f}s elapsed  (~{el/k*(n-k):4.0f}s left)")

    df = pd.DataFrame(rows)

    # ── Trade-count distribution → data-driven credibility floor ──────────────
    med = float(df["trades"].median())
    floor = max(40.0, 0.5 * med)   # cut the low-activity tail, never below ~8 trades/yr
    print("\n" + "─" * 100)
    print(f"Trade-count distribution: min={df['trades'].min()} "
          f"median={med:.0f} max={df['trades'].max()}  →  credibility floor = {floor:.0f} trades")

    gated = df[(df["trades"] >= floor) & (df["pf"] >= GATE_PF_MIN) & (df["net"] > GATE_NET_MIN)].copy()
    print(f"Gated universe: {len(gated)}/{n} combos pass (trades≥{floor:.0f}, PF≥{GATE_PF_MIN}, net>0)")

    # ── Rank: MaxDD ascending (smallest give-back), tie-break PF, WR ───────────
    # max_dd is negative; "smallest give-back" = closest to 0 = largest value.
    ranked = gated.sort_values(
        by=["max_dd", "pf", "win_rate"], ascending=[False, False, False]
    ).reset_index(drop=True)

    def _show(title: str, frame: pd.DataFrame, m: int = 5) -> None:
        print("\n" + "═" * 100)
        print(title)
        print("─" * 100)
        hdr = (f"  {'#':>2} {'BP':>3} {'ADX':>4} {'TP':>4} {'SL':>4} {'STEP':>5} "
               f"{'Trades':>7} {'Win%':>6} {'PF':>5} {'MaxDD%':>8} {'Net$':>10} {'CAGR%':>7}  TP/SL/BE")
        print(hdr)
        for i, r in frame.head(m).iterrows():
            print(f"  {i+1:>2} {int(r['BREAKOUT_PERIOD']):>3} {r['ADX_MIN']:>4.0f} "
                  f"{r['ATR_TP_MULTIPLIER']:>4.1f} {r['ATR_SL_MULTIPLIER']:>4.1f} "
                  f"{r['TRAIL_STEP_ATR']:>5.1f} {int(r['trades']):>7} {r['win_rate']:>6.1f} "
                  f"{r['pf']:>5.2f} {r['max_dd']:>8.2f} {r['net']:>+10,.0f} {r['cagr']:>7.2f}  "
                  f"{int(r['tp'])}/{int(r['sl'])}/{int(r['be'])}")

    _show("TOP 3 — smallest MaxDD within the gated universe (PRIMARY DELIVERABLE)", ranked, 3)

    # Secondary lenses for context / honesty
    _show("(context) Highest Profit Factor in gated universe",
          gated.sort_values(["pf", "max_dd"], ascending=[False, False]).reset_index(drop=True), 5)
    _show("(context) Highest Net Profit in gated universe",
          gated.sort_values(["net"], ascending=[False]).reset_index(drop=True), 5)

    # ── Boundary-optimum check on the top pick ────────────────────────────────
    if len(ranked):
        top = ranked.iloc[0]
        edges = []
        for key, col in (("BREAKOUT_PERIOD", "BREAKOUT_PERIOD"), ("ADX_MIN", "ADX_MIN"),
                         ("ATR_TP_MULTIPLIER", "ATR_TP_MULTIPLIER"),
                         ("ATR_SL_MULTIPLIER", "ATR_SL_MULTIPLIER"),
                         ("TRAIL_STEP_ATR", "TRAIL_STEP_ATR")):
            vals = GRID[key]
            if top[col] in (min(vals), max(vals)):
                edges.append(f"{key}={top[col]} (grid {'min' if top[col]==min(vals) else 'max'})")
        print("\n" + "─" * 100)
        if edges:
            print("⚠️  Top pick sits on a grid boundary — extend & re-run before trusting as optimum:")
            for e in edges:
                print(f"      • {e}")
        else:
            print("✓  Top pick is interior to the grid on all five axes (no boundary optimum).")

    print("\n" + "─" * 100)
    print("HONESTY: in-sample, single-asset (BTCUSDT), best-of-768 selection over one")
    print("contiguous 5yr window — no walk-forward / OOS hold-out.  The STEP-trail")
    print("MECHANISM is robust; the specific NUMBERS are an in-sample fit.  Validate OOS")
    print("before any allocation change.")
    print("═" * 100)


if __name__ == "__main__":
    if "--stage2" in sys.argv:
        GRID = GRID_STAGE2
    main()
