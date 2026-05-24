import os
from dotenv import load_dotenv

load_dotenv()

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

SYMBOL      = os.getenv("SYMBOL", "BTCUSDT")
SYMBOL_CCXT = f"{SYMBOL[:3]}/{SYMBOL[3:]}"

WS_URL = (
    "wss://fstream.binance.com/stream?streams="
    f"{SYMBOL.lower()}@kline_5m/"
    f"{SYMBOL.lower()}@kline_1h/"
    f"{SYMBOL.lower()}@markPrice/"
    f"{SYMBOL.lower()}@aggTrade"
)

# ── Risk ──────────────────────────────────────────────────────────────────────
RISK_PERCENT      = float(os.getenv("RISK_PERCENT", "8.0"))   # % of balance risked per trade
LEVERAGE          = int(os.getenv("LEVERAGE", "10"))           # futures leverage multiplier

# Fixed order-balance sizing (overrides RISK_PERCENT / RISK_USD when > 0)
# Example: ORDER_BALANCE_USD=100, LEVERAGE=10 → each order has $1 000 notional exposure
# qty = (ORDER_BALANCE_USD × LEVERAGE) / entry_price
ORDER_BALANCE_USD = float(os.getenv("ORDER_BALANCE_USD", "0.0"))

# SL / TP as multiples of ATR
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 6.0   # 4:1 RR → break-even ~20% win rate; proven 5yr all-pass config

# ── Warm-up periods ───────────────────────────────────────────────────────────
MIN_CANDLES_5M = int(os.getenv("MIN_CANDLES_5M", "60"))
MIN_CANDLES_1H = int(os.getenv("MIN_CANDLES_1H", "210"))  # ≥ EMA_TREND
MAX_CANDLES    = 600

# ── Indicator periods ─────────────────────────────────────────────────────────
EMA_FAST   = 20
EMA_SLOW   = 50
EMA_TREND  = 200     # 1H EMA200 — primary regime filter
RSI_PERIOD = 14
MACD_FAST  = 12
MACD_SLOW  = 26
MACD_SIGNAL = 9
ATR_PERIOD = 14
BB_PERIOD  = 20
BB_STD     = 2.0

# ── 1H strategy thresholds ────────────────────────────────────────────────────
EMA_1H_MIN_SEP       = 0.001   # EMA20 must lead EMA50 by ≥ 0.1%  (flat market filter)
RSI_1H_LONG_MIN      = 45      # 1H RSI minimum for LONG entry
RSI_1H_LONG_MAX      = 78      # 1H RSI maximum for LONG entry (not overbought)
RSI_1H_SHORT_MAX     = 55      # 1H RSI maximum for SHORT entry
RSI_1H_SHORT_MIN     = 22      # 1H RSI minimum for SHORT entry (not oversold)
ATR_1H_PCT_MIN       = 0.05    # 1H ATR must be ≥ 0.05% of price (not dead market)
ATR_1H_PCT_MAX       = 5.0     # 1H ATR must be ≤ 5.0% of price  (not extreme spike)
TRADE_COOLDOWN_1H    = 1       # minimum 1H bars between entries
TRADE_COOLDOWN_5M    = 12      # minimum 5M bars between 5M scalp entries (~1 hour)
BREAKOUT_PERIOD_5M   = 84      # rolling high/low window for 5M breakout (84×5M = 7h)
EMA_TREND_SLOPE_BARS = 7       # lookback bars for EMA200 slope direction filter
VOL_RATIO_MIN        = 0.3     # minimum volume ratio vs 20-bar MA (avoids dead markets)
TRAIL_ACTIVATE_ATR   = 1.5     # move SL to break-even when price moves X×ATR in favor (0=off)
TRAIL_LOCK_ATR       = 0.0     # lock in 1×ATR profit when price moves X×ATR in favor (0=off)
TRAIL_STOP_ATR       = 0.0     # after TRAIL_ACTIVATE: trail SL at -N×ATR below price peak (0=off)
BREAKOUT_PERIOD      = 14      # bars lookback for rolling high/low breakout signal
REQUIRE_MACD_CONFIRM = False   # if True, breakout also requires MACD hist direction

# ── Regime / trend-strength filters ──────────────────────────────────────────
# Both filters guard against trading in flat/choppy markets (no directional trend).
EMA_SLOPE_MIN_PCT = 0.15   # EMA200 must have moved ≥ 0.15% over EMA_TREND_SLOPE_BARS (0=off)
ATR_RATIO_MIN     = 1.15   # current ATR must be ≥ 1.15× its 20-bar SMA — expanding volatility
ADX_PERIOD        = 14     # ADX lookback period
ADX_MIN           = 20.0   # require ADX ≥ 20 to trade; blocks choppy/ranging markets
# EMA200 distance filter: require price to be at least X% away from EMA200.
# In choppy 2023, BTC oscillated within 0-2% of EMA200 → filter blocks near-EMA noise.
# In sustained 2024+ bull, price was 10-30% above EMA200 → does not filter.
EMA_TREND_DISTANCE_MIN = 0.0  # % — for LONG: close ≥ ema200*(1+X/100); SHORT: close ≤ ema200*(1-X/100) (0=off)

# ── Daily profit target ────────────────────────────────────────────────────────
DAILY_PROFIT_TARGET_PCT = 0.0    # stop trading once daily PnL ≥ N% of day_start_balance (0 = off)
DAILY_LOSS_LIMIT_PCT    = 0.0    # stop trading once daily PnL ≤ -N% of day_start_balance (0 = off)
# Fixed-dollar daily thresholds (override PCT if > 0)
# cap=$110: at $1000 balance (TP≈$100 < $110), allows 2nd trade → +EV. At Y2 ($1600+, TP≈$160 > $110), stops after 1 TP.
DAILY_PROFIT_TARGET_USD = 110.0  # stop trading once daily PnL ≥ $110
DAILY_LOSS_LIMIT_USD    = 50.0   # stop trading once daily PnL ≤ -$50 (fixed, not scaled with risk)
# Fixed-dollar risk per trade (override RISK_PERCENT if > 0)
# 0 = use RISK_PERCENT (recommended: percentage scales with growing balance → better compounding)
RISK_USD                = 0.0

# ── Position-sizing priority (highest → lowest) ───────────────────────────────
# 1. ORDER_BALANCE_USD > 0  →  qty = (ORDER_BALANCE_USD × LEVERAGE) / entry
# 2. RISK_USD > 0           →  qty = RISK_USD / (entry × sl_dist),  cap @ LEVERAGE × balance / entry
# 3. RISK_PERCENT           →  qty = (balance × RISK%) / (entry × sl_dist),  cap @ LEVERAGE × balance / entry

# ── Breakout quality filter ───────────────────────────────────────────────────
BREAKOUT_ATR_BUFFER = 0.0   # close must exceed rolling high by N×ATR (0 = off, try 0.1-0.3)

# ── Trading fee (Binance Futures taker) ──────────────────────────────────────
TRADING_FEE = 0.0005       # 0.0500% per fill (matches user spec)
SLIPPAGE    = 0.0002       # 0.02% market impact per fill

# ── Live trading safety ───────────────────────────────────────────────────────
# Funding rate guard: skip new entries when |funding_rate| exceeds this threshold.
# Binance pays/charges funding every 8 h. At 0.10 %/8 h you pay ~11 % APR just for holding.
FUNDING_RATE_MAX   = float(os.getenv("FUNDING_RATE_MAX", "0.001"))  # 0.10 %/8h (0 = off)

# BTC/USDT Futures minimum order size and precision (Binance exchange rules).
# Orders below MIN_ORDER_QTY or with wrong precision are rejected outright.
MIN_ORDER_QTY      = float(os.getenv("MIN_ORDER_QTY", "0.001"))     # BTC min lot size
QTY_STEP           = float(os.getenv("QTY_STEP",      "0.001"))     # BTC quantity step
PRICE_TICK         = float(os.getenv("PRICE_TICK",    "0.10"))      # BTC price tick size (0.1 USDT)

# Heartbeat: log a status line every N seconds even with no trade activity.
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "900"))    # 15 min
