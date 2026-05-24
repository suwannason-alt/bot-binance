"""
Backtest runner — BTCUSDT Futures
Start: configurable  |  Mode: continuous compounding
Goal: balance grows every year.
Tunes parameters automatically until goal is met.

Usage:
  python run_backtest.py                        # 5-year, $1 000 start
  python run_backtest.py --days 365 --balance 1000   # 1-year, $1 000 start
  python run_backtest.py --days 730             # 2-year run
"""
import argparse
import asyncio
import logging
import sys
from typing import Dict, List, Optional

import pandas as pd

import backtest
import config
import fetch_data
import visualize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_backtest")

# Defaults — overridden by CLI args
INITIAL_BALANCE = 1000.0
YEARS = 7
DAYS  = YEARS * 365

# ── Base parameters (proven 3-year core) ─────────────────────────────────────
_BASE: Dict = {
    "ATR_SL_MULTIPLIER":       1.5,
    "ATR_TP_MULTIPLIER":       5.0,
    "ATR_RATIO_MIN":           1.15,
    "EMA_SLOPE_MIN_PCT":       0.15,
    "ADX_MIN":                 0.0,
    "ADX_PERIOD":              14,
    "TRAIL_ACTIVATE_ATR":      1.5,
    "TRAIL_LOCK_ATR":          0.0,
    "TRAIL_STOP_ATR":          0.0,
    "RISK_PERCENT":            15.0,
    "RISK_USD":                0.0,
    "LEVERAGE":                10,
    "ORDER_BALANCE_USD":       0.0,
    "DAILY_PROFIT_TARGET_USD": 110.0,
    "DAILY_LOSS_LIMIT_USD":    50.0,
    "DAILY_PROFIT_TARGET_PCT": 0.0,
    "DAILY_LOSS_LIMIT_PCT":    0.0,
    "ATR_1H_PCT_MIN":          0.05,
    "ATR_1H_PCT_MAX":          5.0,
    "EMA_1H_MIN_SEP":          0.001,
    "RSI_1H_LONG_MIN":         45,
    "RSI_1H_LONG_MAX":         78,
    "RSI_1H_SHORT_MIN":        22,
    "RSI_1H_SHORT_MAX":        55,
    "BREAKOUT_PERIOD":         7,
    "EMA_TREND_SLOPE_BARS":    7,
    "TRADE_COOLDOWN_1H":       1,
    "VOL_RATIO_MIN":           0.3,
    "REQUIRE_MACD_CONFIRM":    False,
    "BREAKOUT_ATR_BUFFER":     0.0,
    "EMA_TREND_DISTANCE_MIN":  0.0,
    # Quant enhancement defaults (all off — overridden per candidate)
    "REGIME_FILTER_ENABLED":   False,
    "REGIME_STRONG_ADX":       28.0,
    "REGIME_HIGH_VOL_PCT":     4.5,
    "HIGH_VOL_SIZE_SCALE":     0.5,
    "DYNAMIC_TP_ENABLED":      False,
    "DYNAMIC_TP_STRONG_MULT":  1.5,
    "DYNAMIC_TP_WEAK_MULT":    0.7,
    "VOL_SIZING_ENABLED":      False,
    "VOL_SIZING_MAX_SCALE":    1.25,
    "VOL_SIZING_MIN_SCALE":    0.50,
    "BODY_ATR_RATIO_MIN":      0.0,
}

# ── Parameter candidates (tried in order until goal is met) ──────────────────
# Key findings from sweep:
#   - TRAIL=1.0 HURTS all years (converts TP wins → BE exits, drops WR 27%→19%)
#   - Best so far: RISK=8%, ADX≥20, SLOPE=0.15, TRAIL=1.5 → Y2=-6.6% (20 trades in Y2:
#     ~5 TP + ~15 SL). Need to fix that -6.6%.
#
# Math: with ~5 TP, ~15 SL, RISK=8%:
#   TP_payout = RISK × (TP_mult/SL_mult)
#   TP=5.0 → 3.33× risk per win  →  Y2=-6.6%
#   TP=6.0 → 4.00× risk per win  →  EV improves ~+20% per trade
#   TP=7.0 → 4.67× risk per win  →  EV improves ~+40% per trade
#
# Primary fix: raise ATR_TP_MULTIPLIER (more payout per win, WR barely changes).
# Secondary: lower ADX slightly (more trades during Y2 recovery Dec22-May23).
PARAM_CANDIDATES: List[Dict] = [
    # 0: Original 3yr-proven
    {**_BASE},

    # 1: Halved risk baseline
    {**_BASE, "RISK_PERCENT": 8.0},

    # 2: Best known: RISK=8, ADX=20, SLOPE=0.15 → Y2=-6.6%
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0},

    # 3: Raise TP to 5.5 (more payout per win, same entry quality)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 5.5},

    # 4: Raise TP to 6.0 — key fix: EV per trade +20%, ~same win rate
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0},

    # 5: TP=6.5
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.5},

    # 6: TP=7.0 — most payout per win
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0},

    # 7: Lower ADX=17 → more trades in Y2 recovery phase (Dec22-May23)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 17.0},

    # 8: Lower ADX=17 + TP=6 combo
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 17.0, "ATR_TP_MULTIPLIER": 6.0},

    # 9: TP=6 + slightly wider ATR (1.20) for better trend quality
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "ATR_RATIO_MIN": 1.20},

    # 10: TP=6 + longer breakout (14h) = more significant breakout levels
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14},

    # 11: RISK=10% + ADX=20 + TP=6 (slightly more aggressive, better compounding)
    {**_BASE, "RISK_PERCENT": 10.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0},

    # 12: Very high TP to guarantee Y2 positive regardless
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 8.0},

    # 13: TP=6, SLOPE=0.20 (stronger slope filter)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "EMA_SLOPE_MIN_PCT": 0.20},

    # ── 6-year targeted candidates (Year-1 fix: May-Sep 2020 choppy period) ──

    # 14: ADX=25 — only strongest trends, fewer but better quality trades
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0},

    # 15: ADX=25 + BREAKOUT=14 (proven breakout + stricter trend filter)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14},

    # 16: ADX=25 + SLOPE=0.20 (dual strict filter)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "EMA_SLOPE_MIN_PCT": 0.20},

    # 17: ADX=25 + BREAKOUT=14 + SLOPE=0.20 (all strict)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "EMA_SLOPE_MIN_PCT": 0.20},

    # 18: SLOPE=0.25 — very strong trend slope requirement
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "EMA_SLOPE_MIN_PCT": 0.25},

    # 19: BREAKOUT=21 (3-week highs/lows = stronger level)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 21},

    # 20: BREAKOUT=21 + ADX=25
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 21},

    # 21: ATR_RATIO_MIN=1.30 (only enter when trend has strong momentum)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "ATR_RATIO_MIN": 1.30},

    # 22: ADX=30 — very strict, only the clearest trends
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 6.0},

    # 23: ADX=30 + BREAKOUT=14
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14},

    # 24: ADX=25 + TP=7.0 (strict trend + higher payout per win)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0},

    # 25: ADX=25 + BREAKOUT=14 + TP=7.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14},

    # 26: VOL_RATIO=0.8 — require above-average volume on breakout
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "VOL_RATIO_MIN": 0.8},

    # 27: VOL_RATIO=0.8 + ADX=25 + BREAKOUT=14
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "VOL_RATIO_MIN": 0.8},

    # 28: SLOPE=0.25 + ADX=25 + BREAKOUT=14 (maximum strictness)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "EMA_SLOPE_MIN_PCT": 0.25},

    # 29: ATR_RATIO_MIN=1.25 + ADX=25 + BREAKOUT=14
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "ATR_RATIO_MIN": 1.25},

    # 30: COOLDOWN=2 (wait 2h after each trade — avoid whipsaw re-entries)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "TRADE_COOLDOWN_1H": 2},

    # 31: ADX=25 + SLOPE=0.20 + BREAKOUT=21
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 21, "EMA_SLOPE_MIN_PCT": 0.20},

    # ── 7-year focused: attempt 36 (ADX=25 B14 TP=7 LOCK=4) near-miss ─────────
    # Year 2 (May 2020-May 2021) = -13.3% [18t: 3TP 12SL 3BE]  ← needs fix
    # Year 4 (May 2022-May 2023) = -0.8%  [13t: 2TP 7SL 4BE]   ← barely negative

    # 45: ADX=30 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=4.0 (ADX=30 cuts bad Y2 entries)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0},

    # 46: ADX=30 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=3.0 (Y4 fix: LOCK=3 helped Y4)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.0},

    # 47: ADX=28 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=4.0 (intermediate ADX)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 28.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0},

    # 48: ADX=25 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=3.5 (between 3 and 4)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.5},

    # 49: ATR_RATIO=1.30 — only enter when momentum is accelerating (filters choppy Y2)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "ATR_RATIO_MIN": 1.30},

    # 50: ATR_RATIO=1.35
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "ATR_RATIO_MIN": 1.35},

    # 51: ATR_RATIO=1.25 + ADX=30 + TRAIL_LOCK=4.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "ATR_RATIO_MIN": 1.25},

    # 52: BREAKOUT=21 + ADX=25 + TP=7.0 + TRAIL_LOCK=4.0 (3-week breakout)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 21, "TRAIL_LOCK_ATR": 4.0},

    # 53: BREAKOUT=28 + ADX=25 + TP=7.0 + TRAIL_LOCK=4.0 (4-week breakout)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 28, "TRAIL_LOCK_ATR": 4.0},

    # 54: BREAKOUT=21 + ADX=30 + TP=7.0 + TRAIL_LOCK=4.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 21, "TRAIL_LOCK_ATR": 4.0},

    # 55: ADX=30 + BREAKOUT=14 + TP=6.0 + TRAIL_LOCK=4.0 (lower TP → more TP exits in Y2)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0},

    # 56: ADX=30 + BREAKOUT=14 + TP=8.0 + TRAIL_LOCK=4.0 (higher payout per win)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 8.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0},

    # 57: ADX=25 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=4.0 + SLOPE=0.20
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "EMA_SLOPE_MIN_PCT": 0.20},

    # 58: VOL_RATIO=0.8 (confirm breakout with above-avg volume) + attempt 36 base
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "VOL_RATIO_MIN": 0.8},

    # ── TRAIL_LOCK fix: attempt 26 base (ADX=25 BREAKOUT=14 TP=7.0) had Years 1-5 ✓
    # but Year 6 = -32.5% due to 13 BE exits @ 0%. TRAIL_LOCK converts BE→+1ATR wins.

    # 32: ADX=25 + BREAKOUT=14 + TP=7.0 + TRAIL_LOCK=2.0 (lock 1×ATR after 2×ATR move)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.0},

    # 33: TRAIL_LOCK=2.5
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.5},

    # 34: TRAIL_LOCK=3.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.0},

    # 35: TRAIL_LOCK=4.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0},

    # 36: TRAIL_LOCK=5.0 (very late lock — last chance before TP at 7×)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 5.0},

    # 37: BREAKOUT=21 + TP=7.0 + ADX=25 (fewer trades, stronger levels)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 21},

    # 38: BREAKOUT=21 + TP=7.0 + ADX=25 + TRAIL_LOCK=2.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 21, "TRAIL_LOCK_ATR": 2.0},

    # 39: Base winning (ADX=20, BREAKOUT=14, TP=6.0) + TRAIL_LOCK=2.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 6.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.0},

    # 40: ADX=20, BREAKOUT=14, TP=7.0, TRAIL_LOCK=2.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.0},

    # 41: ADX=25, BREAKOUT=14, TP=6.5, TRAIL_LOCK=2.0 (mid-point TP)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 6.5,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.0},

    # 42: ADX=25, BREAKOUT=14, TP=7.0 + TRAIL_LOCK=3.0 + SLOPE=0.20
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.0, "EMA_SLOPE_MIN_PCT": 0.20},

    # 43: ADX=25, BREAKOUT=14, TP=8.0 + TRAIL_LOCK=3.0 (maximum payout, lock early)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 8.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.0},

    # 44: ADX=25, BREAKOUT=14, TP=7.0, TRAIL_LOCK=2.0, ATR_ACTIVATE=2.0 (activate later)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 2.0, "TRAIL_ACTIVATE_ATR": 2.0},

    # ── 7-year: true trailing stop (TRAIL_STOP_ATR) + 6yr-winning base ───────
    # Closest miss: ADX=25 B14 TP=7 LOCK=4 → Year2=-13.3% Year4=-0.8%
    # True trailing stop: after BE, SL follows price at -N×ATR from peak.
    # This converts SL-near-miss trades into locked profits.

    # 59: TRAIL_STOP=1.5 (same ATR as SL) — tight trail, strong profit capture
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_STOP_ATR": 1.5},

    # 60: TRAIL_STOP=1.0 — very tight trail
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_STOP_ATR": 1.0},

    # 61: TRAIL_STOP=2.0 — wider trail, less interference with TP exits
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_STOP_ATR": 2.0},

    # 62: TRAIL_STOP=1.5, no TRAIL_LOCK (let trail handle everything)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_STOP_ATR": 1.5},

    # 63: TRAIL_STOP=1.5 + ADX=30 (stricter entry + trailing)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 30.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_STOP_ATR": 1.5},

    # 64: TRAIL_STOP=2.0, no LOCK, ADX=25 — moderate trail only
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_STOP_ATR": 2.0},

    # 65: TRAIL_ACTIVATE=1.0 + TRAIL_LOCK=4.0 (earlier BE activation)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_ACTIVATE_ATR": 1.0},

    # 66: TRAIL_ACTIVATE=1.2 + TRAIL_LOCK=4.0 (intermediate)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0, "TRAIL_ACTIVATE_ATR": 1.2},

    # 67: TRAIL_ACTIVATE=1.0 + TRAIL_STOP=1.5 (both earlier activation and trail)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "TRAIL_ACTIVATE_ATR": 1.0, "TRAIL_STOP_ATR": 1.5},

    # 68: VOL_RATIO=0.8 + TP=7.0 (attempt27's vol filter but higher payout)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0,
     "VOL_RATIO_MIN": 0.8},

    # 69: VOL=0.8 + TP=7.0 + TRAIL_LOCK=4.0
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0,
     "VOL_RATIO_MIN": 0.8, "TRAIL_LOCK_ATR": 4.0},

    # 70: VOL=0.8 + TP=7.0 + TRAIL_STOP=1.5
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0,
     "VOL_RATIO_MIN": 0.8, "TRAIL_STOP_ATR": 1.5},

    # 71: Fine TRAIL_LOCK sweep: 3.9 (Year4 needs +0.8% from 4.0 base)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.9},

    # 72: TRAIL_LOCK=3.8
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 3.8},

    # 73: TRAIL_STOP=1.5 + TRAIL_ACTIVATE=1.5 + no LOCK (trail does everything)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 20.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_STOP_ATR": 1.5},

    # ══════════════════════════════════════════════════════════════════════════
    # QUANTITATIVE ENHANCEMENT CANDIDATES  (74 onwards)
    # Base: best known 6yr config -> ADX=25 BP=14 TP=7 LOCK=4 RISK=8%
    # Each candidate isolates or combines one enhancement to measure impact.
    # ══════════════════════════════════════════════════════════════════════════

    # ── Feature isolation ─────────────────────────────────────────────────────

    # 74: Regime filter only — skip RANGING bars
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True},

    # 75: Dynamic TP only — extend TP in STRONG_TREND, tighten in WEAK_TREND
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "DYNAMIC_TP_ENABLED": True},

    # 76: Vol-adjusted sizing only — smaller position when ATR is expanded
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "VOL_SIZING_ENABLED": True},

    # 77: Body quality filter only — skip doji candles at breakout
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "BODY_ATR_RATIO_MIN": 0.2},

    # 78: MACD confirm only — require histogram direction matches breakout
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REQUIRE_MACD_CONFIRM": True},

    # 79: Volume quality gate only — require > 80% average volume
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "VOL_RATIO_MIN": 0.8},

    # ── Stacking enhancements (additive) ─────────────────────────────────────

    # 80: Regime + Dynamic TP (most impactful pair)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True},

    # 81: Regime + Dynamic TP + Vol-sizing
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True},

    # 82: Regime + Dynamic TP + Vol-sizing + Body filter (full stack)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True, "BODY_ATR_RATIO_MIN": 0.2},

    # 83: Full stack + MACD confirm (most selective)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True, "BODY_ATR_RATIO_MIN": 0.2,
     "REQUIRE_MACD_CONFIRM": True},

    # 84: Full stack + Vol filter 0.8 (volume quality instead of body filter)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True, "VOL_RATIO_MIN": 0.8},

    # ── Dynamic TP tuning (strong/weak multipliers) ───────────────────────────

    # 85: Regime + Dynamic TP with stronger extension (2.0x in STRONG_TREND)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "DYNAMIC_TP_STRONG_MULT": 2.0, "DYNAMIC_TP_WEAK_MULT": 0.7},

    # 86: Regime + Dynamic TP with moderate extension (1.3x)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "DYNAMIC_TP_STRONG_MULT": 1.3, "DYNAMIC_TP_WEAK_MULT": 0.8},

    # ── Regime threshold tuning ───────────────────────────────────────────────

    # 87: Stricter strong-trend threshold (ADX 32 for extended TP)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "REGIME_STRONG_ADX": 32.0},

    # 88: Looser strong-trend threshold (ADX 24 for extended TP)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "REGIME_STRONG_ADX": 24.0},

    # ── Vol-sizing scale tuning ───────────────────────────────────────────────

    # 89: Aggressive vol-scaling (tighter band: 0.4x – 1.5x)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True,
     "VOL_SIZING_MAX_SCALE": 1.5, "VOL_SIZING_MIN_SCALE": 0.4},

    # 90: Conservative vol-scaling (wider band: 0.6x – 1.1x)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True,
     "VOL_SIZING_MAX_SCALE": 1.1, "VOL_SIZING_MIN_SCALE": 0.6},

    # ── Body filter tuning ────────────────────────────────────────────────────

    # 91: Softer body filter (0.15 ATR)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "BODY_ATR_RATIO_MIN": 0.15},

    # 92: Tighter body filter (0.30 ATR)
    {**_BASE, "RISK_PERCENT": 8.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "BODY_ATR_RATIO_MIN": 0.30},

    # ── Best-effort: all signals quality-filtered, max compounding ────────────

    # 93: RISK=10% + full stack (more aggressive compounding with better signals)
    {**_BASE, "RISK_PERCENT": 10.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True, "BODY_ATR_RATIO_MIN": 0.2},

    # 94: RISK=12% + full stack (even more aggressive)
    {**_BASE, "RISK_PERCENT": 12.0, "ADX_MIN": 25.0, "ATR_TP_MULTIPLIER": 7.0,
     "BREAKOUT_PERIOD": 14, "TRAIL_LOCK_ATR": 4.0,
     "REGIME_FILTER_ENABLED": True, "DYNAMIC_TP_ENABLED": True,
     "VOL_SIZING_ENABLED": True, "BODY_ATR_RATIO_MIN": 0.2},
]


def _apply_params(params: Dict) -> None:
    """Apply a parameter dictionary to the live ``config`` module.

    Sets each key in ``params`` as an attribute on ``config``, then ensures
    ``MIN_CANDLES_1H`` is at least ``EMA_TREND + 10`` so the strategy has
    enough warm-up bars.

    Args:
        params: Mapping of config attribute names to their new values.
    """
    for key, value in params.items():
        if hasattr(config, key):
            setattr(config, key, value)
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)


def _year_end_balance(equity: pd.Series, cutoff: pd.Timestamp) -> float:
    """Return the equity-curve value at or immediately before ``cutoff``.

    Args:
        equity: Time-indexed equity curve from a backtest result.
        cutoff: Upper bound timestamp (inclusive).

    Returns:
        Last equity value on or before ``cutoff``, or the final equity value
        if ``cutoff`` is beyond the series end.
    """
    subset = equity[equity.index <= cutoff]
    return float(subset.iloc[-1]) if len(subset) else float(equity.iloc[-1])


def _year_trade_breakdown(result, cutoffs: List[pd.Timestamp]) -> List[Dict]:
    """Compute per-year trade statistics from a backtest result.

    Slices ``result.trades`` into annual windows defined by ``cutoffs`` and
    counts exit types and trade directions for each year.

    Args:
        result: Backtest result object with ``equity_curve`` and ``trades``
            attributes.
        cutoffs: List of year-boundary timestamps (``len(cutoffs) == years - 1``).

    Returns:
        List of dicts — one per year — each containing:
          ``n``, ``tp``, ``sl``, ``be``, ``long``, ``short`` counts.
    """
    equity     = result.equity_curve
    boundaries = [equity.index[0]] + list(cutoffs) + [equity.index[-1]]
    rows: List[Dict] = []

    for yi in range(len(boundaries) - 1):
        t_start = boundaries[yi]
        t_end   = boundaries[yi + 1]
        year_trades = [t for t in result.trades if t_start <= t.entry_time < t_end]
        rows.append({
            "n":     len(year_trades),
            "tp":    sum(1 for t in year_trades if t.close_reason == "TP"),
            "sl":    sum(1 for t in year_trades if t.close_reason == "SL"),
            "be":    sum(1 for t in year_trades if t.close_reason == "BE"),
            "long":  sum(1 for t in year_trades if t.side == "LONG"),
            "short": sum(1 for t in year_trades if t.side == "SHORT"),
        })
    return rows


def _print_result(
    params: Dict,
    balances: List[float],
    year_ok: List[bool],
    stats: Dict,
    attempt: int,
    total: int,
    result=None,
    cutoffs: Optional[List[pd.Timestamp]] = None,
    years: int = YEARS,
) -> None:
    """Print a formatted backtest result summary to stdout.

    Args:
        params:   Parameter dictionary used for this backtest run.
        balances: List of year-boundary balances (length ``years + 1``).
        year_ok:  Boolean list indicating whether each year was profitable.
        stats:    Statistics dictionary from ``backtest.run().stats``.
        attempt:  Current candidate index (1-based).
        total:    Total number of candidates being tried.
        result:   Backtest result object (optional; needed for trade breakdown).
        cutoffs:  Year-boundary timestamps (optional; needed for trade breakdown).
        years:    Number of years in the backtest window.
    """
    goal_met = all(year_ok)
    risk     = params.get("RISK_PERCENT", "?")
    adx      = params.get("ADX_MIN", 0)
    slope    = params.get("EMA_SLOPE_MIN_PCT", 0)
    tp       = params.get("ATR_TP_MULTIPLIER", 5.0)
    leverage = params.get("LEVERAGE", 10)
    ord_bal  = params.get("ORDER_BALANCE_USD", 0.0)
    sizing   = (f"ORDER_BAL=${ord_bal:.0f}×{leverage}lev"
                if ord_bal > 0 else f"RISK={risk}% LEV={leverage}×")

    # Collect active quant-enhancement flags for display
    flags: List[str] = []
    if params.get("REGIME_FILTER_ENABLED"):
        flags.append("REGIME")
    if params.get("DYNAMIC_TP_ENABLED"):
        flags.append("DYN_TP")
    if params.get("VOL_SIZING_ENABLED"):
        flags.append("VOL_SZ")
    if params.get("BODY_ATR_RATIO_MIN", 0) > 0:
        flags.append(f"BODY{params['BODY_ATR_RATIO_MIN']:.2f}")
    if params.get("REQUIRE_MACD_CONFIRM"):
        flags.append("MACD")
    flags_str = "  [" + " ".join(flags) + "]" if flags else ""

    print()
    print("=" * 72)
    print(f"  {years}-YEAR BACKTEST  [attempt {attempt}/{total}]"
          f"  {sizing}  ADX≥{adx}  SLOPE≥{slope}  TP×{tp}{flags_str}")
    print("=" * 72)

    yr_rows = _year_trade_breakdown(result, cutoffs) if result and cutoffs else []
    for i in range(years):
        b_start = balances[i]
        b_end   = balances[i + 1]
        ret     = (b_end - b_start) / b_start * 100
        detail  = ""
        if i < len(yr_rows):
            yr     = yr_rows[i]
            detail = (f"  [{yr['n']}t: TP={yr['tp']} SL={yr['sl']} BE={yr['be']}"
                      f" | {yr['long']}L {yr['short']}S]")
        print(f"  Year {i+1}  ${b_start:>10,.2f}  →  ${b_end:>10,.2f}"
              f"  {'+' if ret >= 0 else ''}{ret:.1f}%  "
              f"{'✓' if year_ok[i] else '✗'}{detail}")

    print("─" * 72)
    print(f"  Trades       : {stats.get('total_trades', 0)}"
          f"  (TP={stats.get('tp_exits', 0)} SL={stats.get('sl_exits', 0)}"
          f" BE={stats.get('be_exits', 0)})")
    print(f"  Win rate     : {stats.get('win_rate', 0):.1f}%")
    print(f"  Max drawdown : {stats.get('max_drawdown_pct', 0):.1f}%")
    print(f"  CAGR         : {stats.get('cagr_pct', 0):+.1f}%/yr")
    print(f"  Profit factor: {stats.get('profit_factor', 0):.2f}")
    print(f"  Sharpe       : {stats.get('sharpe', 0):.2f}")
    print("─" * 72)
    print(f"  GOAL {'MET ✓' if goal_met else 'NOT MET ✗ — trying next params…'}")
    print("=" * 72)


def main(
    days: int = DAYS,
    initial_balance: float = INITIAL_BALANCE,
    years: int = YEARS,
    symbol: str = "BTCUSDT",
) -> bool:
    """Run the parameter sweep and return whether the all-years goal was met.

    Iterates through ``PARAM_CANDIDATES`` in order, applying each set of
    parameters, running a full backtest, and stopping on the first candidate
    where every year is profitable.  Generates a chart for the best result.

    Args:
        days:            Number of calendar days of historical data to use.
        initial_balance: Starting balance in USDT.
        years:           Number of calendar years in the backtest window.
        symbol:          Trading pair symbol (e.g. ``"BTCUSDT"``).

    Returns:
        ``True`` if at least one candidate achieved the all-years goal,
        ``False`` otherwise.
    """
    logger.info(f"Fetching {days}-day data ({years} year{'s' if years != 1 else ''})…")
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol=symbol, days=days))

    t0 = pd.Timestamp(df_5m["open_time"].iloc[0], unit="ms")
    tN = pd.Timestamp(df_5m["open_time"].iloc[-1], unit="ms")
    logger.info(f"Period: {t0:%Y-%m-%d} → {tN:%Y-%m-%d}"
                f"  5M={len(df_5m):,}  1H={len(df_1h):,}")

    best_result:   Optional[object]          = None
    best_params:   Optional[Dict]            = None
    best_balances: Optional[List[float]]     = None
    best_cutoffs:  Optional[List[pd.Timestamp]] = None
    best_goal_met: bool                      = False
    final_goal_met: bool                     = False

    for attempt, params in enumerate(PARAM_CANDIDATES, 1):
        _apply_params(params)
        logger.info(
            f"Attempt {attempt}/{len(PARAM_CANDIDATES)} — "
            f"RISK={params['RISK_PERCENT']}%  ADX≥{params['ADX_MIN']}"
            f"  SLOPE≥{params['EMA_SLOPE_MIN_PCT']}"
        )

        result = backtest.run(df_5m, df_1h, initial_balance=initial_balance, mode="1h")
        stats  = result.stats
        equity = result.equity_curve

        eq_start = equity.index[0]
        # For 1-year runs there are no intermediate cutoffs
        cutoffs  = [eq_start + pd.DateOffset(years=y) for y in range(1, years)]
        balances = (
            [initial_balance]
            + [_year_end_balance(equity, c) for c in cutoffs]
            + [float(equity.iloc[-1])]
        )

        year_ok  = [balances[i + 1] > balances[i] for i in range(years)]
        goal_met = all(year_ok)

        _print_result(
            params, balances, year_ok, stats,
            attempt, len(PARAM_CANDIDATES),
            result=result, cutoffs=cutoffs, years=years,
        )

        if goal_met:
            if not best_goal_met or balances[-1] > (best_balances[-1] if best_balances else 0):
                best_result   = result
                best_params   = params
                best_balances = balances
                best_cutoffs  = cutoffs
                best_goal_met = True
            final_goal_met = True
            break
        elif best_result is None:
            best_result   = result
            best_params   = params
            best_balances = balances
            best_cutoffs  = cutoffs

    chart = visualize.plot(
        best_result,
        show=False,
        year_marks=best_cutoffs,
        year_balances=best_balances,
    )
    print(f"\n  Chart → {chart}")
    print()

    return final_goal_met


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the backtest runner.

    Returns:
        Parsed :class:`argparse.Namespace` with attributes:
          ``days``, ``balance``, ``symbol``, ``leverage``, ``order_balance``.
    """
    parser = argparse.ArgumentParser(description="BTCUSDT backtest runner")
    parser.add_argument(
        "--days", type=int, default=None,
        help="Number of calendar days to backtest (default: 5×365=1825)",
    )
    parser.add_argument(
        "--balance", type=float, default=None,
        help="Starting balance in USD (default: 1000)",
    )
    parser.add_argument(
        "--symbol", type=str, default="BTCUSDT",
    )
    parser.add_argument(
        "--leverage", type=int, default=None,
        help="Futures leverage multiplier (default: 10).  "
             "Used to cap max position size (risk-based) or to set "
             "notional when --order-balance is given.",
    )
    parser.add_argument(
        "--order-balance", type=float, default=None,
        help="Fixed margin per order in USD (default: 0 = disabled).  "
             "When set, each trade uses ORDER_BALANCE × LEVERAGE notional "
             "regardless of SL distance.  Example: --order-balance 100 "
             "--leverage 10 → $1 000 notional per trade.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Override module-level defaults from CLI
    if args.balance is not None:
        INITIAL_BALANCE = args.balance          # noqa: F841 (used via closure in main())
    if args.days is not None:
        DAYS  = args.days                       # noqa: F841
        YEARS = max(1, round(args.days / 365))  # noqa: F841

    # Apply leverage / order-balance overrides to every param candidate
    if args.leverage is not None:
        for p in PARAM_CANDIDATES:
            p["LEVERAGE"] = args.leverage
        _BASE["LEVERAGE"] = args.leverage
    if args.order_balance is not None:
        for p in PARAM_CANDIDATES:
            p["ORDER_BALANCE_USD"] = args.order_balance
        _BASE["ORDER_BALANCE_USD"] = args.order_balance

    ok = main(
        days=DAYS,
        initial_balance=INITIAL_BALANCE,
        years=YEARS,
        symbol=args.symbol,
    )
    sys.exit(0 if ok else 1)
