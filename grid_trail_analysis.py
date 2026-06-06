"""TRAIL grid — capital-preservation lens (console-only).

Re-cut of the 80-combo grid for the user's ACTUAL objective:
  #1 maximize WIN RATE, #2 minimize MaxDD, keep CAGR reasonable (not maxed),
  with PF kept above a floor (consistency).  PF/MAR are NOT the ranking target.

Also answers the linchpin question: can ANY trail config cut the live (WFO)
drawdown, or is DD bound by the 8% RISK sizing?  -> full 80-combo WFO DD sweep
+ a RISK_PERCENT sweep (the suspected real smoothness lever).

Run:  python grid_trail_analysis.py
"""
from __future__ import annotations

import asyncio
import logging
import statistics
from itertools import product

import config
import fetch_data
import backtest
from run_backtest import _BASE, _apply_config

logging.disable(logging.WARNING)

ACTIVATE = [1.0, 1.2, 1.5, 1.8, 2.0]
LOCK     = [1.5, 1.8, 2.0, 2.5]
STOP     = [1.0, 1.2, 1.4, 1.5]
PF_FLOOR = 1.30
INIT_BAL = 1000.0


def _run(df5, df1, *, act, lock, stop, wfo, risk=8.0):
    cfg = dict(_BASE)
    cfg["WFO_ENABLED"] = wfo
    cfg["RISK_PERCENT"] = risk
    cfg["TRAIL_ACTIVATE_ATR"] = act
    cfg["TRAIL_LOCK_ATR"] = lock
    cfg["TRAIL_STOP_ATR"] = stop
    _apply_config(cfg)
    s = backtest.run(df5, df1, initial_balance=INIT_BAL, mode="1h").stats
    dd = s["max_drawdown_pct"]
    return {
        "act": act, "lock": lock, "stop": stop, "risk": risk,
        "n": s["total_trades"], "win": s["win_rate"], "cagr": s["cagr_pct"],
        "dd": dd, "pf": s["profit_factor"], "sharpe": s["sharpe"],
        "final": s["final_balance"], "mar": s["cagr_pct"] / abs(dd) if dd else 0.0,
    }


def _hdr():
    return (f"{'ACT':>4}{'LOCK':>5}{'STOP':>5} │ {'Win%':>6}{'MaxDD%':>8}{'PF':>6}"
            f"{'CAGR%':>7}{'MAR':>6}{'Trd':>5}{'$final':>9}")


def _row(r):
    flag = "" if r["pf"] >= PF_FLOOR else "  ⚠PF"
    return (f"{r['act']:>4}{r['lock']:>5}{r['stop']:>5} │ {r['win']:>6.1f}{r['dd']:>8.1f}"
            f"{r['pf']:>6.2f}{r['cagr']:>7.1f}{r['mar']:>6.2f}{r['n']:>5}{r['final']:>9.0f}{flag}")


def main():
    df5, df1 = asyncio.run(fetch_data.fetch_all(symbol="BTCUSDT", days=1825))
    combos = list(product(ACTIVATE, LOCK, STOP))
    print(f"\n{len(combos)} trail combos, 5yr window. Objective: max Win%, min DD, "
          f"PF≥{PF_FLOOR}, CAGR reasonable.\n")

    no = []
    wf = []
    for i, (a, l, s_) in enumerate(combos, 1):
        no.append(_run(df5, df1, act=a, lock=l, stop=s_, wfo=False))
        wf.append(_run(df5, df1, act=a, lock=l, stop=s_, wfo=True))
        print(f"\r  grid {i}/{len(combos)} (×2 modes)", end="", flush=True)
    print()

    # ── View A: best WIN RATE (no-WFO), PF floor enforced ────────────────────
    elig = [r for r in no if r["pf"] >= PF_FLOOR]
    print("\n=== TOP 10 by WIN RATE  (no-WFO screen, PF≥floor) ===")
    print(_hdr())
    for r in sorted(elig, key=lambda r: (-r["win"], r["dd"]))[:10]:
        print(_row(r))

    # ── View B: lowest MaxDD (no-WFO), PF floor enforced ─────────────────────
    print("\n=== TOP 10 by LOWEST MaxDD  (no-WFO screen, PF≥floor) ===")
    print(_hdr())
    for r in sorted(elig, key=lambda r: (r["dd"], -r["win"]))[:10]:  # dd is negative; higher=better
        print(_row(r))

    # ── Linchpin: does trail move the LIVE (WFO) drawdown at all? ─────────────
    wdd = [r["dd"] for r in wf]
    wwin = [r["win"] for r in wf]
    print("\n=== LIVE-REGIME (WFO) drawdown across ALL 80 trail combos ===")
    print(f"  MaxDD : best={max(wdd):.1f}  median={statistics.median(wdd):.1f}  "
          f"worst={min(wdd):.1f}   → spread {abs(min(wdd)-max(wdd)):.1f}pp")
    print(f"  Win%  : best={max(wwin):.1f}  median={statistics.median(wwin):.1f}  "
          f"worst={min(wwin):.1f}")
    print("  (If DD spread is small, trail params cannot smooth the live curve — "
          "sizing is the lever.)")

    # ── RISK_PERCENT sweep — the actual smoothness lever (WFO on) ─────────────
    print("\n=== RISK_PERCENT sweep  (trail fixed at current 1.5/2.0/1.2, WFO on) ===")
    print(f"{'RISK%':>6} │ {'Win%':>6}{'MaxDD%':>8}{'CAGR%':>7}{'PF':>6}{'MAR':>6}{'$final':>10}")
    for risk in (8.0, 6.0, 5.0, 4.0, 3.0, 2.0):
        r = _run(df5, df1, act=1.5, lock=2.0, stop=1.2, wfo=True, risk=risk)
        print(f"{risk:>6.0f} │ {r['win']:>6.1f}{r['dd']:>8.1f}{r['cagr']:>7.1f}"
              f"{r['pf']:>6.2f}{r['mar']:>6.2f}{r['final']:>10.0f}")

    # ── Reference baselines ──────────────────────────────────────────────────
    print("\n=== BASELINES (WFO on, RISK 8%) ===")
    for name, (a, l, s_) in {
        "current LIVE 1.5/2.0/1.2": (1.5, 2.0, 1.2),
        "BE-only      1.5/0/0":     (1.5, 0.0, 0.0),
    }.items():
        r = _run(df5, df1, act=a, lock=l, stop=s_, wfo=True)
        print(f"  {name:26} Win {r['win']:.1f}%  DD {r['dd']:.1f}%  PF {r['pf']:.2f}  "
              f"CAGR {r['cagr']:+.1f}%  MAR {r['mar']:.2f}  ${r['final']:.0f}")


if __name__ == "__main__":
    main()
