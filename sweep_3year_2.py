"""
3-year sweep — Phase 2: focused on fixing Y2 (May2024–May2025) while keeping Y1/Y3 ≥55%.

Best so far: ATR_R=1.2+SLOPE=0.15 → Y1=+62% ✓, Y2=-2.7% ✗, Y3=-4.5% ✗
Need to boost Y2 and Y3 without hurting Y1.
"""
import os, sys, asyncio
import pandas as pd
sys.path.insert(0, os.path.dirname(__file__))
import config, backtest, fetch_data

INITIAL_BALANCE = 1000.0
TARGET_PCT      = 55.0
WARMUP_1H       = 210

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

# Best regime-filter base (Y1=+62% with this)
_BEST_FILTER = {**_BASE,
    "ATR_RATIO_MIN": 1.2,
    "EMA_SLOPE_MIN_PCT": 0.15,
    "ADX_MIN": 0.0,       # ADX was not needed when ATR_R+SLOPE are strong
}

SWEEPS = []

# ── Group A: Trailing stop to break-even (ATR_R=1.2+SLOPE=0.15) ──────────────
# BE activation at X×ATR: converts near-miss SLs to 0 → improves Y2 losers
for trail in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]:
    SWEEPS.append({**_BEST_FILTER,
                   "TRAIL_ACTIVATE_ATR": trail,
                   "_label": f"TRAIL_BE={trail}"})

# ── Group B: Wider TP (ATR_R=1.2+SLOPE=0.15) — catch full trends ─────────────
for tp in [5.0, 6.0, 7.0, 8.0, 10.0]:
    SWEEPS.append({**_BEST_FILTER,
                   "ATR_TP_MULTIPLIER": tp,
                   "_label": f"TP={tp}"})

# ── Group C: Tighter SL → better RR (ATR_R=1.2+SLOPE=0.15) ──────────────────
for sl, tp in [(1.5, 4.0), (1.5, 5.0), (1.5, 6.0), (1.0, 4.0), (1.0, 6.0)]:
    SWEEPS.append({**_BEST_FILTER,
                   "ATR_SL_MULTIPLIER": sl, "ATR_TP_MULTIPLIER": tp,
                   "_label": f"SL={sl}+TP={tp}"})

# ── Group D: MACD confirmation (ATR_R=1.2+SLOPE=0.15) ───────────────────────
SWEEPS.append({**_BEST_FILTER, "REQUIRE_MACD_CONFIRM": True, "_label": "MACD=True"})

# ── Group E: Higher RISK% with strong filters ────────────────────────────────
for risk in [6.0, 7.0, 8.0, 10.0, 12.0, 15.0]:
    SWEEPS.append({**_BEST_FILTER, "RISK_PERCENT": risk, "_label": f"RISK={risk}"})

# ── Group F: Relax loss limit (so more trades allowed on bad days) ─────────────
for loss in [50.0, 100.0, 150.0, 200.0, 999.0]:
    SWEEPS.append({**_BEST_FILTER, "DAILY_LOSS_LIMIT_USD": loss, "_label": f"LOSS_LIM={loss}"})

# ── Group G: Vary SLOPE with ATR_R=1.2 (find sweet-spot) ────────────────────
for slope in [0.08, 0.10, 0.12, 0.13, 0.15, 0.17, 0.20]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": 1.2, "EMA_SLOPE_MIN_PCT": slope,
                   "_label": f"ATR_R=1.2+SLOPE={slope}"})

# ── Group H: ATR_R=1.1 + SLOPE — look for a more balanced tradeoff ──────────
for slope in [0.12, 0.15, 0.18, 0.20]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": 1.1, "EMA_SLOPE_MIN_PCT": slope,
                   "_label": f"ATR_R=1.1+SLOPE={slope}"})

# ── Group I: TRAIL + wider TP combo ──────────────────────────────────────────
for trail in [2.0, 2.5]:
    for tp in [5.0, 6.0, 8.0]:
        SWEEPS.append({**_BEST_FILTER,
                       "TRAIL_ACTIVATE_ATR": trail, "ATR_TP_MULTIPLIER": tp,
                       "_label": f"TRAIL={trail}+TP={tp}"})

# ── Group J: TRAIL + higher RISK ─────────────────────────────────────────────
for trail in [2.0, 2.5]:
    for risk in [7.0, 8.0, 10.0]:
        SWEEPS.append({**_BEST_FILTER,
                       "TRAIL_ACTIVATE_ATR": trail, "RISK_PERCENT": risk,
                       "_label": f"TRAIL={trail}+RISK={risk}"})

# ── Group K: Vol ratio filter to improve entry quality ───────────────────────
for vol in [0.5, 0.8, 1.0, 1.2, 1.5]:
    SWEEPS.append({**_BEST_FILTER, "VOL_RATIO_MIN": vol, "_label": f"VOL={vol}"})

# ── Group L: Longer breakout period (major breakouts only) ───────────────────
for bp in [10, 14, 20, 28]:
    SWEEPS.append({**_BEST_FILTER, "BREAKOUT_PERIOD": bp, "_label": f"BP={bp}"})

# ── Group M: ATR_R=1.15 threshold sweep ──────────────────────────────────────
for atr_r in [1.05, 1.10, 1.15, 1.18, 1.20, 1.25, 1.30]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15,
                   "_label": f"ATR_R={atr_r}+SLOPE=0.15"})

# ── Group N: Best filter + MACD + higher RISK ────────────────────────────────
for risk in [7.0, 8.0, 10.0]:
    SWEEPS.append({**_BEST_FILTER,
                   "REQUIRE_MACD_CONFIRM": True, "RISK_PERCENT": risk,
                   "_label": f"MACD+RISK={risk}"})

# ── Group O: Explore ADX=21 + SLOPE=0.15 (no ATR_R) ─────────────────────────
for adx in [18, 21, 25]:
    for slope in [0.12, 0.15, 0.18]:
        SWEEPS.append({**_BASE,
                       "ADX_MIN": adx, "EMA_SLOPE_MIN_PCT": slope,
                       "_label": f"ADX={adx}+SLOPE={slope}"})

# ── Group P: Triple-filter ATR_R+SLOPE+ADX ───────────────────────────────────
for atr_r in [1.1, 1.2]:
    for slope in [0.12, 0.15]:
        for adx in [18, 21]:
            SWEEPS.append({**_BASE,
                           "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": slope, "ADX_MIN": adx,
                           "_label": f"ATR_R={atr_r}+S={slope}+ADX={adx}"})

_DEFAULTS = {k: v for k, v in _BASE.items()}
_DEFAULTS["MIN_CANDLES_1H"] = 210

def _apply(params):
    for k, v in _DEFAULTS.items():
        if hasattr(config, k): setattr(config, k, v)
    for k, v in params.items():
        if k.startswith("_"): continue
        if hasattr(config, k): setattr(config, k, v)
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)

def _year_slice(df_5m, df_1h, y0, y1):
    b1 = df_1h[df_1h["open_time"] < y0]
    w1 = b1.iloc[-WARMUP_1H:] if len(b1) >= WARMUP_1H else b1
    s1 = pd.concat([w1, df_1h[(df_1h["open_time"] >= y0) & (df_1h["open_time"] < y1)]], ignore_index=True)
    w5s = int(w1["open_time"].iloc[0]) if len(w1) else y0
    s5 = df_5m[(df_5m["open_time"] >= w5s) & (df_5m["open_time"] < y1)].reset_index(drop=True)
    return s5, s1.reset_index(drop=True)

def _run(d5, d1):
    r = backtest.run(d5, d1, initial_balance=INITIAL_BALANCE, mode="1h").stats
    return {"t": r.get("total_trades",0), "wr": r.get("win_rate",0),
            "ret": r.get("total_return_pct",0), "mdd": r.get("max_drawdown_pct",0)}

def _fmt(r):
    s = "+" if r["ret"] >= 0 else ""
    c = "✓" if r["ret"] >= TARGET_PCT else "✗"
    return f"{s}{r['ret']:6.1f}% {c} ({r['t']}T WR={r['wr']:.0f}% MDD={r['mdd']:.0f}%)"

async def main():
    df5, df1 = await fetch_data.fetch_all(symbol="BTCUSDT", days=1095)
    t0 = int(df1["open_time"].iloc[0])
    t1 = t0 + 365*86_400_000; t2 = t0 + 730*86_400_000
    t3 = int(df1["open_time"].iloc[-1]) + 3_600_000
    y1d5, y1d1 = _year_slice(df5, df1, t0, t1)
    y2d5, y2d1 = _year_slice(df5, df1, t1, t2)
    y3d5, y3d1 = _year_slice(df5, df1, t2, t3)

    print(f"\n{'Label':<40} {'Y1(2023-24)':>30}  {'Y2(2024-25)':>30}  {'Y3(2025-26)':>30}  PASS")
    print("─"*140)

    wins, rows = [], []
    for p in SWEEPS:
        _apply(p)
        r1, r2, r3 = _run(y1d5, y1d1), _run(y2d5, y2d1), _run(y3d5, y3d1)
        ok = r1["ret"]>=TARGET_PCT and r2["ret"]>=TARGET_PCT and r3["ret"]>=TARGET_PCT
        flag = "  *** ALL-PASS ***" if ok else ""
        print(f"{p.get('_label','?'):<40} {_fmt(r1)}  {_fmt(r2)}  {_fmt(r3)}{flag}")
        mn = min(r1["ret"], r2["ret"], r3["ret"])
        rows.append((mn, p.get("_label","?"), r1, r2, r3, p))
        if ok: wins.append((p.get("_label","?"), r1, r2, r3, p))

    print("\n"+"="*140)
    if wins:
        print(f"\n  {len(wins)} ALL-PASS CONFIG(S):")
        for lbl, r1, r2, r3, p in wins:
            avg = (r1["ret"]+r2["ret"]+r3["ret"])/3
            print(f"  {lbl}  avg={avg:+.1f}%/yr  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")
            print(f"  Params: { {k:v for k,v in p.items() if not k.startswith('_')} }")
    else:
        print("\n  No all-pass. Top 15 by worst-year return:")
        for mn, lbl, r1, r2, r3, _ in sorted(rows, reverse=True)[:15]:
            print(f"  [{mn:+.1f}% worst]  {lbl:<40}  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")

if __name__ == "__main__":
    asyncio.run(main())
