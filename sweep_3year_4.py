"""
Phase 4: Focused on the two best near-all-pass configs:
  A) TRAIL=1.5+RISK=15 → Y1=+118%✓, Y2=+84%✓, Y3=-4%✗  (need to fix Y3)
  B) TRAIL2+RISK15+LOSS=999 → Y1=+83%✓, Y2=+58%✓, Y3=+5%✗  (need to boost Y3)
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

# Strong regime filter base (Y1=+62%, Y2/Y3 need work)
_F = {**_BASE, "ATR_RATIO_MIN": 1.2, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0}

SWEEPS = []

# ── Branch A: Fix TRAIL=1.5+RISK=15 Y3 failure ───────────────────────────────

# A1: Add LOSS=999 (same as what helped Y2 in Branch B)
for risk in [15.0, 17.0, 18.0, 20.0]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk,
                   "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"A_TRAIL1.5+RISK{risk}+LOSS999"})

# A2: Higher profit cap (allow more trades on good days)
for cap in [200.0, 500.0, 999.0]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
                   "DAILY_PROFIT_TARGET_USD": cap, "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"A_TRAIL1.5+RISK15+LOSS999+CAP{cap}"})

# A3: Intermediate TRAIL values
for trail in [1.6, 1.7, 1.8, 1.9]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": trail, "RISK_PERCENT": 15.0,
                   "_label": f"A_TRAIL{trail}+RISK15"})

for trail in [1.6, 1.7, 1.8, 1.9]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": trail, "RISK_PERCENT": 15.0,
                   "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"A_TRAIL{trail}+RISK15+LOSS999"})

# A4: TRAIL=1.5 + less restrictive ATR_R (more Y3 trades)
for atr_r in [1.05, 1.1, 1.15]:
    SWEEPS.append({**_BASE, "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0,
                   "_label": f"A_ATR{atr_r}+S0.15+TRAIL1.5+R15"})

# A5: TRAIL=1.5 + vary RISK (check if Y3 has positive EV)
for risk in [18.0, 20.0, 22.0, 25.0]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk,
                   "_label": f"A_TRAIL1.5+RISK{risk}"})

# ── Branch B: Scale TRAIL2+LOSS=999 Y3 from +4.7% to 55% ────────────────────

# B1: Higher RISK with TRAIL2+LOSS999
for risk in [18.0, 20.0, 22.0, 25.0, 28.0, 30.0]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 2.0, "RISK_PERCENT": risk,
                   "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"B_TRAIL2+RISK{risk}+LOSS999"})

# B2: Higher profit cap + TRAIL2+LOSS999 + vary RISK
for risk in [20.0, 25.0]:
    for cap in [500.0, 999.0]:
        SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 2.0, "RISK_PERCENT": risk,
                       "DAILY_LOSS_LIMIT_USD": 999.0, "DAILY_PROFIT_TARGET_USD": cap,
                       "_label": f"B_TRAIL2+RISK{risk}+LOSS999+CAP{cap}"})

# B3: TRAIL2+LOSS999 + ATR_R=1.1 (more trades)
for atr_r in [1.05, 1.1, 1.15]:
    SWEEPS.append({**_BASE, "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": 2.0, "RISK_PERCENT": 15.0, "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"B_ATR{atr_r}+S0.15+TRAIL2+R15+LOSS999"})

# B4: TRAIL=1.5+LOSS=999 + scaled RISK
for risk in [15.0, 18.0, 20.0]:
    SWEEPS.append({**_F, "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk,
                   "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"B_TRAIL1.5+RISK{risk}+LOSS999"})

# ── Branch C: Keep Y3 base-config performance, somehow fix Y1 ────────────────
# Base config Y3=+55.5%. What if we only filter Y1 bad signals using a regime filter
# that doesn't hurt Y3?

# C1: Low SLOPE (0.05-0.08) with ATR_R=1.1 + TRAIL + high RISK
for slope in [0.05, 0.06, 0.07, 0.08]:
    SWEEPS.append({**_BASE, "ATR_RATIO_MIN": 1.1, "EMA_SLOPE_MIN_PCT": slope, "ADX_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": 15.0, "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"C_ATR1.1+S{slope}+TRAIL1.5+R15+LOSS999"})

# C2: Use ADX=21 only (no SLOPE, no ATR_R) + TRAIL + RISK
for risk in [12.0, 15.0, 18.0, 20.0]:
    SWEEPS.append({**_BASE, "ADX_MIN": 21.0, "EMA_SLOPE_MIN_PCT": 0.0, "ATR_RATIO_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk, "DAILY_LOSS_LIMIT_USD": 999.0,
                   "_label": f"C_ADX21+TRAIL1.5+RISK{risk}+LOSS999"})

# C3: DIST=3+ filter (cleanest long positions) + TRAIL + RISK
for dist in [3.0, 4.0]:
    for risk in [12.0, 15.0, 18.0]:
        SWEEPS.append({**_BASE, "EMA_TREND_DISTANCE_MIN": dist, "ADX_MIN": 21.0,
                       "TRAIL_ACTIVATE_ATR": 1.5, "RISK_PERCENT": risk, "DAILY_LOSS_LIMIT_USD": 999.0,
                       "_label": f"C_DIST{dist}+TRAIL1.5+RISK{risk}+LOSS999"})

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

    print(f"\n{'Label':<48} {'Y1(2023-24)':>30}  {'Y2(2024-25)':>30}  {'Y3(2025-26)':>30}  PASS")
    print("─"*148)
    wins, rows = [], []
    for p in SWEEPS:
        _apply(p)
        r1,r2,r3 = _run(y1d5,y1d1),_run(y2d5,y2d1),_run(y3d5,y3d1)
        ok = r1["ret"]>=TARGET_PCT and r2["ret"]>=TARGET_PCT and r3["ret"]>=TARGET_PCT
        flag = "  *** ALL-PASS ***" if ok else ""
        print(f"{p.get('_label','?'):<48} {_fmt(r1)}  {_fmt(r2)}  {_fmt(r3)}{flag}")
        rows.append((min(r1["ret"],r2["ret"],r3["ret"]),p.get("_label","?"),r1,r2,r3,p))
        if ok: wins.append((p.get("_label","?"),r1,r2,r3,p))

    print("\n"+"="*148)
    if wins:
        print(f"\n  {len(wins)} ALL-PASS CONFIG(S):")
        for lbl,r1,r2,r3,p in wins:
            avg=(r1["ret"]+r2["ret"]+r3["ret"])/3
            print(f"\n  {lbl}  avg={avg:+.1f}%/yr")
            print(f"  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")
            print(f"  Params: { {k:v for k,v in p.items() if not k.startswith('_')} }")
    else:
        print("\n  Top 20 by worst-year:")
        for mn,lbl,r1,r2,r3,_ in sorted(rows,reverse=True)[:20]:
            print(f"  [{mn:+.1f}%] {lbl:<48} Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")

if __name__ == "__main__":
    asyncio.run(main())
