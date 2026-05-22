"""
Phase 5: Near-all-pass found in Phase 4:
  ATR_R=1.15+SLOPE=0.15+TRAIL=1.5+RISK=15 → Y1=+5.1%✗, Y2=+123.7%✓, Y3=+133.9%✓

Y1 needs to go from +5.1% to +55%.
Y1 has 23T, WR=30% — need either better WR or better RR to make it profitable enough.

Strategy: add supplementary filters that specifically help Y1 (May 2023 choppy)
without degrading Y2/Y3 (where the base already passes).
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
    BREAKOUT_ATR_BUFFER=0.0, EMA_TREND_DISTANCE_MIN=0.0,
)

# Near-all-pass base: ATR_R=1.15+SLOPE=0.15+TRAIL=1.5+RISK=15
# Y1=+5.1%✗  Y2=+123.7%✓  Y3=+133.9%✓
_F15 = {**_BASE,
    "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
    "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
}

SWEEPS = []

# ── Group A: Fine-grained ATR_R sweep (1.15→1.20, step 0.01) ─────────────────
# Find sweet spot between 1.15 (great Y3, bad Y1) and 1.2 (great Y1, bad Y3)
for atr_r in [1.15, 1.16, 1.17, 1.18, 1.19, 1.20]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
        "_label": f"A_ATR{atr_r}+S0.15+T1.5+R15"})

for atr_r in [1.15, 1.16, 1.17, 1.18, 1.19, 1.20]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
        "DAILY_LOSS_LIMIT_USD": 999.0,
        "_label": f"A_ATR{atr_r}+S0.15+T1.5+R15+L999"})

# ── Group B: ATR_R=1.15 + ADX filter (block choppy Y1 markets) ───────────────
# ADX=21 was effective at blocking May-Sep 2023 ranging in Phase 1+2
for adx in [15.0, 18.0, 21.0, 24.0, 25.0]:
    SWEEPS.append({**_F15, "ADX_MIN": adx,
        "_label": f"B_ATR1.15+ADX{adx}+T1.5+R15"})

for adx in [15.0, 18.0, 21.0]:
    SWEEPS.append({**_F15, "ADX_MIN": adx, "DAILY_LOSS_LIMIT_USD": 999.0,
        "_label": f"B_ATR1.15+ADX{adx}+T1.5+R15+L999"})

# ── Group C: ATR_R=1.15 + EMA distance filter (block near-EMA entries in Y1) ─
# May-Sep 2023: BTC oscillated within 0-3% of EMA200
# 2024+: BTC was 5-20%+ above EMA200 in strong trends
for dist in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]:
    SWEEPS.append({**_F15, "EMA_TREND_DISTANCE_MIN": dist,
        "_label": f"C_ATR1.15+DIST{dist}+T1.5+R15"})

for dist in [1.0, 1.5, 2.0, 2.5, 3.0]:
    SWEEPS.append({**_F15, "EMA_TREND_DISTANCE_MIN": dist, "DAILY_LOSS_LIMIT_USD": 999.0,
        "_label": f"C_ATR1.15+DIST{dist}+T1.5+R15+L999"})

# ── Group D: ATR_R=1.15 + stricter SLOPE (filter flat EMA momentum) ──────────
for slope in [0.15, 0.17, 0.18, 0.20, 0.22, 0.25]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": slope, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
        "_label": f"D_ATR1.15+S{slope}+T1.5+R15"})

for slope in [0.17, 0.18, 0.20, 0.22]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": slope, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
        "DAILY_LOSS_LIMIT_USD": 999.0,
        "_label": f"D_ATR1.15+S{slope}+T1.5+R15+L999"})

# ── Group E: Tighter SL → better RR (break-even WR drops from 38% to ~32%) ───
# With WR=30% and tight SL, E[trade] could turn positive
for sl, tp in [(1.5, 4.0), (1.5, 5.0), (1.0, 4.0), (1.0, 5.0), (1.0, 6.0)]:
    SWEEPS.append({**_F15,
        "ATR_SL_MULTIPLIER": sl, "ATR_TP_MULTIPLIER": tp,
        "_label": f"E_ATR1.15+SL{sl}+TP{tp}+T1.5+R15"})

# ── Group F: RSI tuning (require more momentum for entries) ─────────────────
# Higher RSI_LONG_MIN blocks neutral-momentum breakouts common in choppy Y1
for rsi_min in [47, 50, 52, 55]:
    SWEEPS.append({**_F15, "RSI_1H_LONG_MIN": rsi_min,
        "_label": f"F_ATR1.15+RSI_MIN{rsi_min}+T1.5+R15"})

# ── Group G: Scale RISK on the near-all-pass base ────────────────────────────
# Higher RISK amplifies any positive EV; if Y1 E[trade] > 0, scaling helps
for risk in [18.0, 20.0, 22.0, 25.0]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk,
        "_label": f"G_ATR1.15+S0.15+T1.5+R{risk}"})

for risk in [18.0, 20.0, 22.0, 25.0]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk,
        "DAILY_LOSS_LIMIT_USD": 999.0,
        "_label": f"G_ATR1.15+S0.15+T1.5+R{risk}+L999"})

# ── Group H: Longer TRAIL (TRAIL=2.0) variants on ATR_R=1.15 ─────────────────
# Previously TRAIL=2.0+ATR_R=1.15+LOSS999 gave Y1=+0.8%✗, Y2=+16.1%✗, Y3=+67.7%✓
# Try without LOSS999 or with different RISK
for trail in [1.5, 1.7, 2.0]:
    SWEEPS.append({**_BASE,
        "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
        "TRAIL_ACTIVATE_ATR": trail, "RISK_PERCENT": 15.0,
        "_label": f"H_ATR1.15+S0.15+T{trail}+R15"})

# ── Group I: Combined best-guesses — DIST + ADX on ATR_R=1.15 ────────────────
for adx in [18.0, 21.0]:
    for dist in [1.0, 2.0]:
        SWEEPS.append({**_F15, "ADX_MIN": adx, "EMA_TREND_DISTANCE_MIN": dist,
            "_label": f"I_ATR1.15+ADX{adx}+DIST{dist}+T1.5+R15"})

# ── Group J: BREAKOUT_PERIOD tuning (bigger breakouts less frequent but higher WR) ──
for bp in [10, 14, 20]:
    SWEEPS.append({**_F15, "BREAKOUT_PERIOD": bp,
        "_label": f"J_ATR1.15+BP{bp}+T1.5+R15"})

# ── Group K: VOL_RATIO_MIN (require volume surge on breakout) ────────────────
for vol in [0.5, 0.8, 1.0, 1.2]:
    SWEEPS.append({**_F15, "VOL_RATIO_MIN": vol,
        "_label": f"K_ATR1.15+VOL{vol}+T1.5+R15"})

# ── Group L: MACD confirmation ────────────────────────────────────────────────
SWEEPS.append({**_F15, "REQUIRE_MACD_CONFIRM": True,
    "_label": "L_ATR1.15+MACD+T1.5+R15"})

SWEEPS.append({**_F15, "REQUIRE_MACD_CONFIRM": True, "DAILY_LOSS_LIMIT_USD": 999.0,
    "_label": "L_ATR1.15+MACD+T1.5+R15+L999"})

# ── Reference points ─────────────────────────────────────────────────────────
SWEEPS.append({**_BASE,
    "ATR_RATIO_MIN": 1.15, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
    "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
    "_label": "REF_ATR1.15+S0.15+T1.5+R15"})

SWEEPS.append({**_BASE,
    "ATR_RATIO_MIN": 1.20, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
    "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
    "_label": "REF_ATR1.20+S0.15+T1.5+R15"})

_DEFAULTS = {k: v for k, v in _BASE.items()}
_DEFAULTS["MIN_CANDLES_1H"] = 210

def _apply(p):
    for k, v in _DEFAULTS.items():
        if hasattr(config, k): setattr(config, k, v)
    for k, v in p.items():
        if k.startswith("_"): continue
        if hasattr(config, k): setattr(config, k, v)
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)

def _slice(df5, df1, y0, y1):
    b1 = df1[df1["open_time"] < y0]
    w1 = b1.iloc[-WARMUP_1H:] if len(b1) >= WARMUP_1H else b1
    s1 = pd.concat([w1, df1[(df1["open_time"] >= y0) & (df1["open_time"] < y1)]], ignore_index=True)
    ws = int(w1["open_time"].iloc[0]) if len(w1) else y0
    s5 = df5[(df5["open_time"] >= ws) & (df5["open_time"] < y1)].reset_index(drop=True)
    return s5, s1.reset_index(drop=True)

def _run(d5, d1):
    s = backtest.run(d5, d1, initial_balance=INITIAL_BALANCE, mode="1h").stats
    return {"t": s.get("total_trades",0), "wr": s.get("win_rate",0),
            "ret": s.get("total_return_pct",0), "mdd": s.get("max_drawdown_pct",0)}

def _fmt(r):
    sg = "+" if r["ret"] >= 0 else ""
    ck = "✓" if r["ret"] >= TARGET_PCT else "✗"
    return f"{sg}{r['ret']:6.1f}% {ck} ({r['t']}T WR={r['wr']:.0f}% MDD={r['mdd']:.0f}%)"

async def main():
    df5, df1 = await fetch_data.fetch_all(symbol="BTCUSDT", days=1095)
    t0 = int(df1["open_time"].iloc[0])
    t1 = t0+365*86_400_000; t2 = t0+730*86_400_000
    t3 = int(df1["open_time"].iloc[-1])+3_600_000
    y1d5,y1d1 = _slice(df5,df1,t0,t1)
    y2d5,y2d1 = _slice(df5,df1,t1,t2)
    y3d5,y3d1 = _slice(df5,df1,t2,t3)

    print(f"\n{'Label':<52} {'Y1(2023-24)':>30}  {'Y2(2024-25)':>30}  {'Y3(2025-26)':>30}  PASS")
    print("─"*152)
    wins, rows = [], []
    for p in SWEEPS:
        _apply(p)
        r1,r2,r3 = _run(y1d5,y1d1),_run(y2d5,y2d1),_run(y3d5,y3d1)
        ok = r1["ret"]>=TARGET_PCT and r2["ret"]>=TARGET_PCT and r3["ret"]>=TARGET_PCT
        flag = "  *** ALL-PASS ***" if ok else ""
        print(f"{p.get('_label','?'):<52} {_fmt(r1)}  {_fmt(r2)}  {_fmt(r3)}{flag}")
        rows.append((min(r1["ret"],r2["ret"],r3["ret"]),p.get("_label","?"),r1,r2,r3,p))
        if ok: wins.append((p.get("_label","?"),r1,r2,r3,p))

    print("\n"+"="*152)
    if wins:
        print(f"\n  {len(wins)} ALL-PASS CONFIG(S):")
        for lbl,r1,r2,r3,p in wins:
            avg=(r1["ret"]+r2["ret"]+r3["ret"])/3
            print(f"\n  {lbl}  avg={avg:+.1f}%/yr")
            print(f"  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")
            print(f"  Params: { {k:v for k,v in p.items() if not k.startswith('_')} }")
    else:
        print("\n  Top 25 by worst-year:")
        for mn,lbl,r1,r2,r3,_ in sorted(rows,reverse=True)[:25]:
            print(f"  [{mn:+.1f}%] {lbl:<52} Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")

if __name__ == "__main__":
    asyncio.run(main())
