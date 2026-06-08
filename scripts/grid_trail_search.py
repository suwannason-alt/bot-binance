"""TRAIL parameter grid search — capital-preservation objective (console-only).

Screens TRAIL_ACTIVATE_ATR × TRAIL_LOCK_ATR × TRAIL_STOP_ATR over the 5yr window.

Methodology (see chat rationale):
  * Primary screen = --no-wfo (fixed BREAKOUT_PERIOD=14) → clean trail-only effect
    (WFO BP schedule is *mostly* but not fully trail-independent, so WFO adds noise).
  * Confirm = WFO-on (matches live) for the finalists.
  * Robustness = in-sample/out-of-sample split computed from the ONE continuous run
    (warm throughout — no cold-start), + neighborhood plateau check in the cube.
  * Rank = PF and MAR(=CAGR/|DD|); win-rate is a tiebreaker/output, not a target.

Run:  python grid_trail_search.py
"""
from __future__ import annotations

# ── Path bootstrap: modular layout — keep flat `import config` / `import backtest`
# style resolvable from any subdirectory (src/core, backtesting, scripts). ──────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import asyncio
import logging
import statistics
from itertools import product

import pandas as pd

import config
import fetch_data
import backtest
from run_backtest import _BASE, _apply_config

logging.disable(logging.WARNING)  # silence per-retune WFO spam; keep stdout clean

ACTIVATE = [1.0, 1.2, 1.5, 1.8, 2.0]
LOCK     = [1.5, 1.8, 2.0, 2.5]
STOP     = [1.0, 1.2, 1.4, 1.5]

OOS_CUTOFF = pd.Timestamp("2025-01-01")  # in-sample < cutoff ≤ out-of-sample
INIT_BAL   = 1000.0


def _period_metrics(trades, equity, t0, t1):
    """Compute win%, PF, CAGR, MaxDD over [t0, t1) from a continuous run."""
    tr = [t for t in trades if t0 <= t.entry_time < t1]
    eq = equity[(equity.index >= t0) & (equity.index < t1)]
    if not tr or len(eq) < 2:
        return None
    pnls = [t.pnl for t in tr]
    wins = [p for p in pnls if p > 0]
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    pf = gp / gl if gl > 0 else float("inf")
    start, end = float(eq.iloc[0]), float(eq.iloc[-1])
    yrs = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    cagr = ((end / start) ** (1.0 / yrs) - 1.0) * 100.0 if start > 0 and end > 0 else -100.0
    roll_max = eq.cummax()
    dd = float(((eq - roll_max) / roll_max).min() * 100.0)
    return {
        "n": len(tr), "win": len(wins) / len(tr) * 100.0,
        "pf": pf, "cagr": cagr, "dd": dd,
    }


def _run(df5, df1, act, lock, stop, wfo):
    cfg = dict(_BASE)                       # fresh base every run (no BP carry-over)
    cfg["WFO_ENABLED"] = wfo
    cfg["TRAIL_ACTIVATE_ATR"] = act
    cfg["TRAIL_LOCK_ATR"] = lock
    cfg["TRAIL_STOP_ATR"] = stop
    _apply_config(cfg)
    r = backtest.run(df5, df1, initial_balance=INIT_BAL, mode="1h")
    s = r.stats
    dd = s["max_drawdown_pct"]
    mar = s["cagr_pct"] / abs(dd) if dd != 0 else 0.0
    return {
        "act": act, "lock": lock, "stop": stop,
        "n": s["total_trades"], "win": s["win_rate"], "cagr": s["cagr_pct"],
        "dd": dd, "pf": s["profit_factor"], "sharpe": s["sharpe"],
        "final": s["final_balance"], "mar": mar,
        "_result": r,
    }


def _znorm(vals):
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals) or 1.0
    return lambda x: (x - m) / sd


def main():
    df5, df1 = asyncio.run(fetch_data.fetch_all(symbol="BTCUSDT", days=1825))
    combos = list(product(ACTIVATE, LOCK, STOP))
    print(f"\nGrid: {len(combos)} combos × 2 WFO modes  "
          f"(period {df1['open_time'].iloc[0]} … rows 1H={len(df1)})\n")

    # ── Primary screen: no-WFO (clean trail-only) ────────────────────────────
    rows = []
    for i, (a, l, s_) in enumerate(combos, 1):
        rows.append(_run(df5, df1, a, l, s_, wfo=False))
        print(f"\r  no-WFO screen: {i}/{len(combos)}", end="", flush=True)
    print()

    # PF can be inf (no losses) — clamp for scoring only.
    pf_clip = [min(r["pf"], 5.0) for r in rows]
    z_pf  = _znorm(pf_clip)
    z_mar = _znorm([r["mar"] for r in rows])
    for r, pfc in zip(rows, pf_clip):
        # Weight PF and MAR equally (risk-adjusted consistency + smoothness/return).
        r["score"] = z_pf(pfc) + z_mar(r["mar"])

    by = {(r["act"], r["lock"], r["stop"]): r for r in rows}

    def neighbors(r):
        out = []
        for axis, seq in (("act", ACTIVATE), ("lock", LOCK), ("stop", STOP)):
            i = seq.index(r[axis])
            for di in (-1, 1):
                j = i + di
                if 0 <= j < len(seq):
                    key = tuple(seq[j] if k == axis else r[k] for k in ("act", "lock", "stop"))
                    out.append(by[key])
        return out

    for r in rows:
        nb = neighbors(r)
        r["nbr_score"] = statistics.mean(n["score"] for n in nb)
        r["plateau"] = r["nbr_score"] >= r["score"] - 0.5  # neighbors not far below

    ranked = sorted(rows, key=lambda r: (-r["score"], -r["pf"], -r["win"]))

    print("\n=== TOP 12 by composite (no-WFO screen, PF+MAR z-score) ===")
    print(f"{'ACT':>4}{'LOCK':>5}{'STOP':>5} │ {'Win%':>6}{'PF':>6}{'CAGR%':>7}"
          f"{'MaxDD%':>8}{'MAR':>6}{'Shrp':>6}{'Trd':>5}{'$final':>9} │ {'score':>6}{'nbr':>6} plateau")
    for r in ranked[:12]:
        print(f"{r['act']:>4}{r['lock']:>5}{r['stop']:>5} │ {r['win']:>6.1f}{r['pf']:>6.2f}"
              f"{r['cagr']:>7.1f}{r['dd']:>8.1f}{r['mar']:>6.2f}{r['sharpe']:>6.2f}{r['n']:>5}"
              f"{r['final']:>9.0f} │ {r['score']:>6.2f}{r['nbr_score']:>6.2f}  {'YES' if r['plateau'] else 'no'}")

    # DD spread — is drawdown even movable by trails, or sizing-dominated?
    dds = [r["dd"] for r in rows]
    print(f"\nMaxDD spread across 80 combos: min={max(dds):.1f}  "
          f"median={statistics.median(dds):.1f}  worst={min(dds):.1f}  "
          f"(range {abs(min(dds)-max(dds)):.1f}pp)")
    cagrs = [r["cagr"] for r in rows]
    print(f"CAGR spread: best={max(cagrs):+.1f}  median={statistics.median(cagrs):+.1f}  "
          f"worst={min(cagrs):+.1f}")

    # ── Finalists: top-3 plateau picks, confirm WFO-on + OOS split ───────────
    finalists = [r for r in ranked if r["plateau"]][:3]
    if len(finalists) < 3:
        finalists = ranked[:3]

    print("\n=== FINALISTS — WFO-on confirmation + in/out-of-sample split ===")
    for rank, r in enumerate(finalists, 1):
        a, l, s_ = r["act"], r["lock"], r["stop"]
        w = _run(df5, df1, a, l, s_, wfo=True)
        res = r["_result"]
        eq = res.equity_curve
        t0, tN = eq.index[0], eq.index[-1]
        ins = _period_metrics(res.trades, eq, t0, OOS_CUTOFF)
        oos = _period_metrics(res.trades, eq, OOS_CUTOFF, tN + pd.Timedelta(days=1))
        print(f"\n#{rank}  ACT={a} LOCK={l} STOP={s_}")
        print(f"   no-WFO  5yr : Win {r['win']:.1f}%  PF {r['pf']:.2f}  CAGR {r['cagr']:+.1f}%  "
              f"DD {r['dd']:.1f}%  MAR {r['mar']:.2f}  ${r['final']:.0f}")
        print(f"   WFO-on  5yr : Win {w['win']:.1f}%  PF {w['pf']:.2f}  CAGR {w['cagr']:+.1f}%  "
              f"DD {w['dd']:.1f}%  MAR {w['mar']:.2f}  ${w['final']:.0f}")
        if ins:
            print(f"   in-sample   (<2025): Win {ins['win']:.1f}%  PF {ins['pf']:.2f}  "
                  f"CAGR {ins['cagr']:+.1f}%  DD {ins['dd']:.1f}%  (n={ins['n']})")
        if oos:
            print(f"   OUT-sample (≥2025): Win {oos['win']:.1f}%  PF {oos['pf']:.2f}  "
                  f"CAGR {oos['cagr']:+.1f}%  DD {oos['dd']:.1f}%  (n={oos['n']})")

    # Baselines for reference
    print("\n=== BASELINES (5yr) ===")
    for name, (a, l, s_) in {
        "current LIVE 1.5/2.0/1.2": (1.5, 2.0, 1.2),
        "BE-only     1.5/0/0":      (1.5, 0.0, 0.0),
    }.items():
        n_ = _run(df5, df1, a, l, s_, wfo=False)
        w_ = _run(df5, df1, a, l, s_, wfo=True)
        print(f"  {name:24} no-WFO: Win {n_['win']:.0f}% PF {n_['pf']:.2f} "
              f"CAGR {n_['cagr']:+.0f}% DD {n_['dd']:.0f}% MAR {n_['mar']:.2f}  ││  "
              f"WFO: Win {w_['win']:.0f}% PF {w_['pf']:.2f} CAGR {w_['cagr']:+.0f}% "
              f"DD {w_['dd']:.0f}% MAR {w_['mar']:.2f}")


if __name__ == "__main__":
    main()
