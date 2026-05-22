"""
3-year per-year independent sweep.

For each parameter set, runs three independent $1000-start backtests:
  Y1: May 2023 → May 2024
  Y2: May 2024 → May 2025
  Y3: May 2025 → May 2026

Each year gets 220 warmup 1H bars prepended (for EMA200 computation)
but the balance starts at $1000 at the year boundary.

Usage:
  python sweep_3year.py
"""
import os
import sys
import asyncio
import pandas as pd
import numpy as np
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
import config
import backtest
import fetch_data

INITIAL_BALANCE = 1000.0
TARGET_PCT      = 55.0      # each year must hit ≥55% return
WARMUP_1H       = 210       # exactly MIN_CANDLES_1H — first eligible j=210 = first year bar


# ── Parameter sets to sweep ────────────────────────────────────────────────────

# Winning base (proven Y2+Y3)
_BASE = dict(
    ATR_SL_MULTIPLIER=2.0, ATR_TP_MULTIPLIER=4.0,
    EMA_TREND_SLOPE_BARS=7, BREAKOUT_PERIOD=7, TRADE_COOLDOWN_1H=1,
    LEVERAGE=10, RISK_PERCENT=5.0, RISK_USD=0.0,
    VOL_RATIO_MIN=0.3, REQUIRE_MACD_CONFIRM=False,
    RSI_1H_LONG_MIN=45, RSI_1H_LONG_MAX=78,
    RSI_1H_SHORT_MIN=22, RSI_1H_SHORT_MAX=55,
    EMA_1H_MIN_SEP=0.001, ATR_1H_PCT_MIN=0.05, ATR_1H_PCT_MAX=5.0,
    TRAIL_ACTIVATE_ATR=0.0, TRAIL_LOCK_ATR=0.0,
    DAILY_PROFIT_TARGET_USD=110.0, DAILY_LOSS_LIMIT_USD=50.0,
    DAILY_PROFIT_TARGET_PCT=0.0, DAILY_LOSS_LIMIT_PCT=0.0,
    EMA_SLOPE_MIN_PCT=0.09, ATR_RATIO_MIN=0.0,
    ADX_MIN=21.0, ADX_PERIOD=14,
    BREAKOUT_ATR_BUFFER=0.0,
    EMA_TREND_DISTANCE_MIN=0.0,
)

SWEEPS = []

# ── Group 1: EMA_TREND_DISTANCE_MIN sweep (with winning base) ─────────────────
for dist in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0]:
    SWEEPS.append({**_BASE, "EMA_TREND_DISTANCE_MIN": dist,
                   "_label": f"DIST={dist}"})

# ── Group 2: DIST + ADX combinations ─────────────────────────────────────────
for dist in [1.0, 2.0, 3.0]:
    for adx in [18.0, 21.0, 25.0]:
        SWEEPS.append({**_BASE,
                       "EMA_TREND_DISTANCE_MIN": dist,
                       "ADX_MIN": adx,
                       "_label": f"DIST={dist}+ADX={adx}"})

# ── Group 3: DIST + SLOPE combinations ────────────────────────────────────────
for dist in [1.0, 2.0, 3.0]:
    for slope in [0.05, 0.09, 0.12, 0.15]:
        SWEEPS.append({**_BASE,
                       "EMA_TREND_DISTANCE_MIN": dist,
                       "EMA_SLOPE_MIN_PCT": slope,
                       "_label": f"DIST={dist}+SLOPE={slope}"})

# ── Group 4: DIST + ATR_RATIO ─────────────────────────────────────────────────
for dist in [1.0, 2.0, 3.0]:
    for atr_r in [0.0, 0.9, 1.0, 1.1, 1.2]:
        SWEEPS.append({**_BASE,
                       "EMA_TREND_DISTANCE_MIN": dist,
                       "ATR_RATIO_MIN": atr_r,
                       "_label": f"DIST={dist}+ATR_R={atr_r}"})

# ── Group 5: DIST + RISK% ──────────────────────────────────────────────────────
for dist in [1.5, 2.0, 2.5, 3.0]:
    for risk in [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]:
        SWEEPS.append({**_BASE,
                       "EMA_TREND_DISTANCE_MIN": dist,
                       "RISK_PERCENT": risk,
                       "_label": f"DIST={dist}+RISK={risk}"})

# ── Group 6: Best from prior sessions (ATR_RATIO=1.2 + SLOPE=0.15) + DIST ────
for dist in [0.0, 1.0, 2.0, 3.0]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": 1.2,
                   "EMA_SLOPE_MIN_PCT": 0.15,
                   "EMA_TREND_DISTANCE_MIN": dist,
                   "_label": f"ATR_R=1.2+SLOPE=0.15+DIST={dist}"})


# ── Config defaults for reset between runs ────────────────────────────────────
_DEFAULTS = {
    "ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 4.0,
    "EMA_TREND_SLOPE_BARS": 7, "BREAKOUT_PERIOD": 7, "TRADE_COOLDOWN_1H": 1,
    "LEVERAGE": 10, "RISK_PERCENT": 5.0, "RISK_USD": 0.0,
    "VOL_RATIO_MIN": 0.3, "REQUIRE_MACD_CONFIRM": False,
    "RSI_1H_LONG_MIN": 45, "RSI_1H_LONG_MAX": 78,
    "RSI_1H_SHORT_MIN": 22, "RSI_1H_SHORT_MAX": 55,
    "EMA_1H_MIN_SEP": 0.001, "ATR_1H_PCT_MIN": 0.05, "ATR_1H_PCT_MAX": 5.0,
    "TRAIL_ACTIVATE_ATR": 0.0, "TRAIL_LOCK_ATR": 0.0,
    "DAILY_PROFIT_TARGET_USD": 110.0, "DAILY_LOSS_LIMIT_USD": 50.0,
    "DAILY_PROFIT_TARGET_PCT": 0.0, "DAILY_LOSS_LIMIT_PCT": 0.0,
    "EMA_SLOPE_MIN_PCT": 0.09, "ATR_RATIO_MIN": 0.0,
    "ADX_MIN": 21.0, "ADX_PERIOD": 14,
    "BREAKOUT_ATR_BUFFER": 0.0,
    "EMA_TREND_DISTANCE_MIN": 0.0,
    "MIN_CANDLES_1H": 210,
}


def _apply(params: dict):
    for k, v in _DEFAULTS.items():
        if hasattr(config, k):
            setattr(config, k, v)
    for k, v in params.items():
        if k.startswith("_"):
            continue
        if hasattr(config, k):
            setattr(config, k, v)
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)


def _year_slice(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                year_start_ms: int, year_end_ms: int) -> tuple:
    """Return (df_5m, df_1h) for the year with WARMUP_1H prefix bars."""
    before_1h = df_1h[df_1h["open_time"] < year_start_ms]
    warmup_1h = before_1h.iloc[-WARMUP_1H:] if len(before_1h) >= WARMUP_1H else before_1h

    year_1h = df_1h[
        (df_1h["open_time"] >= year_start_ms) & (df_1h["open_time"] < year_end_ms)
    ]
    s1 = pd.concat([warmup_1h, year_1h], ignore_index=True)

    if len(warmup_1h) > 0:
        warmup_5m_start = int(warmup_1h["open_time"].iloc[0])
    else:
        warmup_5m_start = year_start_ms
    year_5m = df_5m[
        (df_5m["open_time"] >= warmup_5m_start) & (df_5m["open_time"] < year_end_ms)
    ]
    return year_5m.reset_index(drop=True), s1.reset_index(drop=True)


def _run_year(df_5m, df_1h) -> dict:
    result = backtest.run(df_5m, df_1h, initial_balance=INITIAL_BALANCE, mode="1h")
    s = result.stats
    return {
        "trades":  s.get("total_trades", 0),
        "wr":      s.get("win_rate", 0),
        "ret":     s.get("total_return_pct", 0),
        "mdd":     s.get("max_drawdown_pct", 0),
        "final":   s.get("final_balance", INITIAL_BALANCE),
    }


def _fmt(yr: dict) -> str:
    sign = "+" if yr["ret"] >= 0 else ""
    chk  = "✓" if yr["ret"] >= TARGET_PCT else "✗"
    return f"{sign}{yr['ret']:6.1f}% {chk} ({yr['trades']}T WR={yr['wr']:.0f}% MDD={yr['mdd']:.0f}%)"


async def main():
    print(f"\nLoading 3-year data…")
    df_5m_full, df_1h_full = await fetch_data.fetch_all(symbol="BTCUSDT", days=1095)

    # Determine year boundaries from actual data
    t0 = int(df_1h_full["open_time"].iloc[0])
    yr1_end = t0 + 365 * 86_400_000
    yr2_end = t0 + 730 * 86_400_000
    yr3_end = int(df_1h_full["open_time"].iloc[-1]) + 3_600_000  # include last bar

    d0  = datetime.fromtimestamp(t0 / 1000).strftime("%Y-%m-%d")
    d1  = datetime.fromtimestamp(yr1_end / 1000).strftime("%Y-%m-%d")
    d2  = datetime.fromtimestamp(yr2_end / 1000).strftime("%Y-%m-%d")
    d3  = datetime.fromtimestamp(yr3_end / 1000).strftime("%Y-%m-%d")
    print(f"Y1: {d0} → {d1}   Y2: {d1} → {d2}   Y3: {d2} → {d3}\n")

    # Pre-slice each year's dataframes (warmup included)
    y1_5m, y1_1h = _year_slice(df_5m_full, df_1h_full, t0,      yr1_end)
    y2_5m, y2_1h = _year_slice(df_5m_full, df_1h_full, yr1_end, yr2_end)
    y3_5m, y3_1h = _year_slice(df_5m_full, df_1h_full, yr2_end, yr3_end)

    print(f"{'Label':<40} {'Y1':>30}   {'Y2':>30}   {'Y3':>30}  ALL-PASS")
    print("─" * 140)

    all_pass_results = []
    all_rows = []

    for params in SWEEPS:
        label = params.get("_label", "?")
        _apply(params)
        r1 = _run_year(y1_5m, y1_1h)
        r2 = _run_year(y2_5m, y2_1h)
        r3 = _run_year(y3_5m, y3_1h)

        all_pass = (
            r1["ret"] >= TARGET_PCT
            and r2["ret"] >= TARGET_PCT
            and r3["ret"] >= TARGET_PCT
        )
        flag = "  *** ALL-PASS ***" if all_pass else ""
        print(f"{label:<40} {_fmt(r1)}   {_fmt(r2)}   {_fmt(r3)}{flag}")

        mn = min(r1["ret"], r2["ret"], r3["ret"])
        all_rows.append((mn, label, r1, r2, r3, params))
        if all_pass:
            all_pass_results.append((label, r1, r2, r3, params))

    print("\n" + "=" * 140)
    if all_pass_results:
        print(f"\n{'='*60}")
        print(f"  {len(all_pass_results)} ALL-PASS CONFIG(S) FOUND:")
        print(f"{'='*60}")
        for label, r1, r2, r3, p in all_pass_results:
            print(f"\n  Label : {label}")
            print(f"  Y1    : {_fmt(r1)}")
            print(f"  Y2    : {_fmt(r2)}")
            print(f"  Y3    : {_fmt(r3)}")
            avg = (r1["ret"] + r2["ret"] + r3["ret"]) / 3
            print(f"  Avg   : {avg:+.1f}%/yr")
            kv = {k: v for k, v in p.items() if not k.startswith("_")}
            print(f"  Params: {kv}")
    else:
        print("\n  No all-pass found. Top 10 by worst-year return:")
        for mn, label, r1, r2, r3, _ in sorted(all_rows, reverse=True)[:10]:
            print(f"  [{mn:+.1f}% worst]  {label:<40}  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
