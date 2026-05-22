"""
Phase 3: Scale RISK around the best all-positive config (TRAIL=2.0+RISK=10)
and explore slope-bars + EMA_TREND_DISTANCE combos.
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

# Best all-positive base (ATR_R=1.2+SLOPE=0.15+ADX=0+TRAIL=2.0)
_TRAIL_BASE = {**_BASE,
    "ATR_RATIO_MIN": 1.2, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
    "TRAIL_ACTIVATE_ATR": 2.0,
}

SWEEPS = []

# ── Group A: Scale RISK (best all-positive config) ────────────────────────────
for risk in [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 17.0, 20.0, 22.0, 25.0]:
    SWEEPS.append({**_TRAIL_BASE, "RISK_PERCENT": risk, "_label": f"TRAIL2+RISK={risk}"})

# ── Group B: Add ADX=21 back with TRAIL ──────────────────────────────────────
for risk in [10.0, 12.0, 15.0, 18.0, 20.0]:
    SWEEPS.append({**_TRAIL_BASE, "ADX_MIN": 21.0, "RISK_PERCENT": risk,
                   "_label": f"TRAIL2+ADX21+RISK={risk}"})

# ── Group C: TRAIL variations with RISK=15 ───────────────────────────────────
for trail in [1.0, 1.5, 2.0, 2.5, 3.0]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": 1.2, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": trail, "RISK_PERCENT": 15.0,
                   "_label": f"TRAIL={trail}+RISK=15"})

# ── Group D: Add loss limit relaxation with TRAIL+RISK ───────────────────────
for loss in [100.0, 200.0, 999.0]:
    SWEEPS.append({**_TRAIL_BASE, "RISK_PERCENT": 15.0, "DAILY_LOSS_LIMIT_USD": loss,
                   "_label": f"TRAIL2+RISK15+LOSS={loss}"})

# ── Group E: Longer slope lookback (EMA_TREND_SLOPE_BARS) ────────────────────
for slope_bars in [7, 14, 21, 30]:
    for slope_pct in [0.05, 0.09, 0.12, 0.15]:
        SWEEPS.append({**_BASE,
                       "ATR_RATIO_MIN": 1.2, "EMA_TREND_SLOPE_BARS": slope_bars,
                       "EMA_SLOPE_MIN_PCT": slope_pct,
                       "_label": f"SB={slope_bars}+S={slope_pct}"})

# ── Group F: TRAIL+RISK + lower ATR_R (more trades) ─────────────────────────
for atr_r in [0.8, 0.9, 1.0, 1.05, 1.1]:
    SWEEPS.append({**_BASE,
                   "ATR_RATIO_MIN": atr_r, "EMA_SLOPE_MIN_PCT": 0.15, "ADX_MIN": 0.0,
                   "TRAIL_ACTIVATE_ATR": 2.0, "RISK_PERCENT": 15.0,
                   "_label": f"ATR_R={atr_r}+S0.15+TRAIL2+RISK15"})

# ── Group G: High profit cap with TRAIL to allow compounding ─────────────────
for cap in [200.0, 500.0, 999.0]:
    SWEEPS.append({**_TRAIL_BASE, "RISK_PERCENT": 12.0, "DAILY_PROFIT_TARGET_USD": cap,
                   "_label": f"TRAIL2+RISK12+CAP={cap}"})

# ── Group H: ATR_R=1.2+SLOPE=0.15 + DIST variations with TRAIL ──────────────
for dist in [0.5, 1.0, 1.5, 2.0]:
    SWEEPS.append({**_TRAIL_BASE, "EMA_TREND_DISTANCE_MIN": dist, "RISK_PERCENT": 12.0,
                   "_label": f"TRAIL2+RISK12+DIST={dist}"})

# ── Group I: TRAIL=2.0 + tighter SL (better effective RR) + high RISK ────────
for sl in [1.5, 1.0]:
    for risk in [10.0, 12.0, 15.0]:
        SWEEPS.append({**_TRAIL_BASE, "ATR_SL_MULTIPLIER": sl, "RISK_PERCENT": risk,
                       "_label": f"TRAIL2+SL={sl}+RISK={risk}"})

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

    print(f"\n{'Label':<44} {'Y1(2023-24)':>30}  {'Y2(2024-25)':>30}  {'Y3(2025-26)':>30}  PASS")
    print("─"*144)

    wins, rows = [], []
    for p in SWEEPS:
        _apply(p)
        r1,r2,r3 = _run(y1d5,y1d1),_run(y2d5,y2d1),_run(y3d5,y3d1)
        ok = r1["ret"]>=TARGET_PCT and r2["ret"]>=TARGET_PCT and r3["ret"]>=TARGET_PCT
        flag = "  *** ALL-PASS ***" if ok else ""
        print(f"{p.get('_label','?'):<44} {_fmt(r1)}  {_fmt(r2)}  {_fmt(r3)}{flag}")
        rows.append((min(r1["ret"],r2["ret"],r3["ret"]), p.get("_label","?"), r1,r2,r3,p))
        if ok: wins.append((p.get("_label","?"),r1,r2,r3,p))

    print("\n"+"="*144)
    if wins:
        print(f"\n  {len(wins)} ALL-PASS CONFIG(S):")
        for lbl,r1,r2,r3,p in wins:
            avg=(r1["ret"]+r2["ret"]+r3["ret"])/3
            print(f"\n  {lbl}  avg={avg:+.1f}%/yr")
            print(f"  Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")
            print(f"  Params: { {k:v for k,v in p.items() if not k.startswith('_')} }")
    else:
        print("\n  Top 15 by worst-year:")
        for mn,lbl,r1,r2,r3,_ in sorted(rows,reverse=True)[:15]:
            print(f"  [{mn:+.1f}%] {lbl:<44} Y1={r1['ret']:+.1f}%  Y2={r2['ret']:+.1f}%  Y3={r3['ret']:+.1f}%")

if __name__ == "__main__":
    asyncio.run(main())
