"""
Bot configuration — all parameters read from environment variables.

Environment variable loading priority:
  1. Shell environment   (``export VAR=value``)
  2. ``.env`` file       (loaded via python-dotenv)
  3. Hard-coded default  (shown as the second argument of each ``os.getenv``)

Key configuration sections:
  - API credentials and symbol
  - Risk and position sizing
  - Indicator periods
  - 1H strategy entry thresholds
  - Trailing stop cascade
  - Daily profit / loss limits
  - Live trading safety guards
  - Telegram notifications
  - Quantitative enhancements (all ``OFF`` by default — enable via ``.env``)

Production safety contract
--------------------------
When ``PAPER_TRADING=false`` the module performs a hard validation of all
critical secrets at import time.  A missing API key raises ``EnvironmentError``
immediately — the process exits before touching any exchange endpoint.
"""
import os
import sys
from dotenv import load_dotenv

load_dotenv()

# ── Runtime environment ───────────────────────────────────────────────────────
# Values: "production" | "staging" | "development"
ENV = os.getenv("ENV", "production").lower()

API_KEY    = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() == "true"

# ── Production safety guard ───────────────────────────────────────────────────
# Hard-crash at import time if critical secrets are absent in live mode.
# This prevents the bot from starting with empty credentials and silently
# rejecting every order (or worse, being rejected mid-position).
if not PAPER_TRADING:
    _missing = [name for name, val in [
        ("BINANCE_API_KEY",    API_KEY),
        ("BINANCE_API_SECRET", API_SECRET),
    ] if not val or val.startswith("PASTE_YOUR")]
    if _missing:
        print(
            f"\n  ╔══ STARTUP ABORTED ═══════════════════════════════════════╗\n"
            f"  ║  PAPER_TRADING=false but the following required            ║\n"
            f"  ║  environment variables are missing or still set to their   ║\n"
            f"  ║  placeholder values:                                       ║\n"
            + "".join(
                f"  ║    ✗  {k:<52}║\n" for k in _missing
            ) +
            f"  ║                                                            ║\n"
            f"  ║  Fix:  edit .env → set real API credentials,               ║\n"
            f"  ║        then restart the bot.                               ║\n"
            f"  ╚════════════════════════════════════════════════════════════╝\n",
            file=sys.stderr,
        )
        sys.exit(1)

SYMBOL      = os.getenv("SYMBOL", "BTCUSDT")
SYMBOL_CCXT = f"{SYMBOL[:3]}/{SYMBOL[3:]}"

# ── Telegram notifications ────────────────────────────────────────────────────
# Leave TELEGRAM_BOT_TOKEN empty to disable all Telegram alerts.
# Alert level mirrors Python logging levels: DEBUG/INFO/WARNING/ERROR/CRITICAL.
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN",  "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID",    "")
TELEGRAM_ALERT_LEVEL = os.getenv("TELEGRAM_ALERT_LEVEL", "INFO").upper()
TELEGRAM_ENABLED     = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ── State persistence ─────────────────────────────────────────────────────────
# Path to the SQLite database used by StateManager for crash recovery.
# Relative paths are resolved from the directory that contains config.py.
BOT_STATE_DB_PATH = os.getenv(
    "BOT_STATE_DB_PATH",
    os.path.join(os.path.dirname(__file__), "bot_state.db"),
)

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

# Consecutive-loss circuit breaker: halt today after N stop-losses in a row.
# Prevents over-trading into an adverse market. Resets each UTC midnight.
# 0 = off.  Recommended: 3 (stops a bad run before it compounds).
MAX_CONSECUTIVE_LOSSES   = int(os.getenv("MAX_CONSECUTIVE_LOSSES",   "3"))

# Post-SL cooldown: wait extra 1H bars before re-entering after a stop-loss.
# Avoids immediately re-entering the same whipsaw that just hit your stop.
# 0 = use normal TRADE_COOLDOWN_1H.  Recommended: 3 (= 3 h minimum gap after SL).
POST_SL_COOLDOWN_1H      = int(os.getenv("POST_SL_COOLDOWN_1H",      "3"))

# Session time filter: only open new positions between [START, END) UTC hours.
# 0,0 = off (trade 24/7, proven maximum-growth mode).
# Example: SESSION_FILTER_START_UTC=7  SESSION_FILTER_END_UTC=22
#          → entries only 07:00–22:00 UTC (London + New York overlap).
SESSION_FILTER_START_UTC = int(os.getenv("SESSION_FILTER_START_UTC", "0"))
SESSION_FILTER_END_UTC   = int(os.getenv("SESSION_FILTER_END_UTC",   "0"))

# Live position sync interval (seconds): how often to poll the exchange to detect
# when an SL/TP order filled outside our WebSocket feed.
# CRITICAL for live mode — without this the bot freezes after a remote SL fill.
LIVE_POSITION_SYNC_SECS  = int(os.getenv("LIVE_POSITION_SYNC_SECS",  "30"))

# ══════════════════════════════════════════════════════════════════════════════
# QUANTITATIVE ENHANCEMENTS  (all OFF by default — enable via .env or sweep)
# ══════════════════════════════════════════════════════════════════════════════

# ── 1. Market regime detection ────────────────────────────────────────────────
# Classifies every bar as STRONG_TREND / WEAK_TREND / RANGING / HIGH_VOL.
# RANGING  -> skip entry (most false breakouts originate here)
# HIGH_VOL -> allow entry but scale position size down by HIGH_VOL_SIZE_SCALE
REGIME_FILTER_ENABLED = os.getenv("REGIME_FILTER_ENABLED", "false").lower() == "true"
REGIME_STRONG_ADX     = float(os.getenv("REGIME_STRONG_ADX",   "28.0"))  # ADX >= this -> STRONG_TREND
REGIME_HIGH_VOL_PCT   = float(os.getenv("REGIME_HIGH_VOL_PCT", "4.5"))   # ATR% >= this -> HIGH_VOL
HIGH_VOL_SIZE_SCALE   = float(os.getenv("HIGH_VOL_SIZE_SCALE", "0.5"))   # position multiplier in HIGH_VOL

# ── 2. Dynamic TP / SL ────────────────────────────────────────────────────────
# STRONG_TREND: extend TP (let big winners run, don't exit too early)
# WEAK_TREND:   tighten TP (take profits before momentum fades)
# Formula: effective_TP = ATR_TP_MULTIPLIER x regime_mult
DYNAMIC_TP_ENABLED     = os.getenv("DYNAMIC_TP_ENABLED", "false").lower() == "true"
DYNAMIC_TP_STRONG_MULT = float(os.getenv("DYNAMIC_TP_STRONG_MULT", "1.5"))  # 7.0 -> 10.5x in STRONG
DYNAMIC_TP_WEAK_MULT   = float(os.getenv("DYNAMIC_TP_WEAK_MULT",   "0.7"))  # 7.0 ->  4.9x in WEAK

# ── 3. Volatility-adjusted position sizing ────────────────────────────────────
# Scales position size INVERSELY with current ATR regime so that expected
# dollar-risk per trade stays roughly constant regardless of volatility.
#   ATR_ratio 1.5x normal -> position x 0.67  (ATR expanded -> smaller position)
#   ATR_ratio 0.7x normal -> position x 1.25  (ATR compressed -> larger position, capped)
VOL_SIZING_ENABLED   = os.getenv("VOL_SIZING_ENABLED", "false").lower() == "true"
VOL_SIZING_MAX_SCALE = float(os.getenv("VOL_SIZING_MAX_SCALE", "1.25"))  # max upscale (low-vol)
VOL_SIZING_MIN_SCALE = float(os.getenv("VOL_SIZING_MIN_SCALE", "0.50"))  # max downscale (high-vol)

# ── 4. Candle body quality filter ─────────────────────────────────────────────
# Require the breakout candle to have a meaningful body (strong close, not a doji).
# body_atr_ratio = |close - open| / ATR  ->  < threshold = indecision -> skip.
# 0 = disabled.  Recommended range: 0.15 - 0.30.
BODY_ATR_RATIO_MIN = float(os.getenv("BODY_ATR_RATIO_MIN", "0.0"))

# ── 5. Limit-order entry (live trading fee optimisation) ──────────────────────
# Post limit at the breakout close price -> maker rebate (0.02%) instead of
# taker fee (0.05%).  Saves 60% on entry fees per trade.
# Falls back to market if not filled within LIMIT_ENTRY_TIMEOUT seconds.
USE_LIMIT_ENTRY     = os.getenv("USE_LIMIT_ENTRY", "false").lower() == "true"
LIMIT_ENTRY_TIMEOUT = int(os.getenv("LIMIT_ENTRY_TIMEOUT", "45"))   # seconds before market fallback

# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE REGIME FRAMEWORK  (OFF by default — enable via .env)
# ══════════════════════════════════════════════════════════════════════════════
# Replaces static threshold optimisation (94 PARAM_CANDIDATES) with a single
# self-calibrating strategy driven by three orthogonal market signals:
#   1. Hurst Exponent  — price serial correlation (trending vs mean-reverting)
#   2. ADX Momentum    — trend strength + slope direction
#   3. BBW Percentile  — where current volatility sits in its own history
#
# All four adaptive outputs (TP, SL, size, entry-buffer) are smooth continuous
# functions of a composite regime_score ∈ [0.0, 1.0].  No cliff edges.
# Setting ADAPTIVE_REGIME_ENABLED=false → identical to classic mode.

ADAPTIVE_REGIME_ENABLED = os.getenv("ADAPTIVE_REGIME_ENABLED", "false").lower() == "true"

# Minimum regime score to allow any entry.
# Below this threshold the market is considered pure noise → skip entirely.
# 0.25 blocks the noisiest ~25% of bars; 0.35 is more conservative.
ADAPTIVE_MIN_SCORE = float(os.getenv("ADAPTIVE_MIN_SCORE", "0.25"))

# ── TP range: tp_base (at score=0) → tp_base × tp_max_ext (at score=1) ──────
# Example defaults: TP ranges from 4.0x (choppy) to 10.0x (strong trend).
# This lets the strategy survive choppy periods (lower TP = more exits before
# reversal) while fully capturing trend moves (higher TP = let winners run).
ADAPTIVE_TP_BASE    = float(os.getenv("ADAPTIVE_TP_BASE",    "4.0"))   # min TP multiplier
ADAPTIVE_TP_MAX_EXT = float(os.getenv("ADAPTIVE_TP_MAX_EXT", "2.5"))   # factor at score=1 → max 10x

# ── SL range: sl_base (at score=1) → sl_base × sl_max_widen (at score=0) ──
# In choppy markets the SL widens slightly to avoid whipsaws;
# in strong trends it stays tight (ATR already reflects lower noise).
ADAPTIVE_SL_MAX_WIDEN = float(os.getenv("ADAPTIVE_SL_MAX_WIDEN", "1.8"))  # factor at score=0

# ── Position size floor ───────────────────────────────────────────────────────
# Minimum position fraction when regime score is near 0.
# 0.30 → 30% of normal size in the choppiest markets (still participates,
# just with reduced exposure to avoid serial SL losses).
ADAPTIVE_SIZE_MIN = float(os.getenv("ADAPTIVE_SIZE_MIN", "0.30"))

# ── Entry buffer ─────────────────────────────────────────────────────────────
# In choppy markets, require the close to exceed the rolling high by up to
# ADAPTIVE_BUFFER_MAX × ATR before triggering a breakout entry.
# At score=1 this collapses to 0 (any close above high is valid).
# Range 0.3-0.6 ATR is typical; 0 disables the adaptive buffer entirely.
ADAPTIVE_BUFFER_MAX = float(os.getenv("ADAPTIVE_BUFFER_MAX", "0.50"))

# ── Adaptive ADX minimum ─────────────────────────────────────────────────────
# At score=1 (Hurst + BBW confirm a strong trend), the ADX floor is relaxed
# by ADAPTIVE_ADX_RELAX points — allowing entries when a trend is clearly
# building even if ADX hasn't caught up yet (ADX lags by design).
# Effective ADX_MIN at score=1 = ADX_MIN - ADAPTIVE_ADX_RELAX
# Example: ADX_MIN=20, RELAX=8 → floor drops to 12 in confirmed strong trends.
ADAPTIVE_ADX_RELAX = float(os.getenv("ADAPTIVE_ADX_RELAX", "8.0"))

# ══════════════════════════════════════════════════════════════════════════════
# ADAPTIVE TRAILING STOP  (OFF by default — enable via .env)
# ══════════════════════════════════════════════════════════════════════════════
# Replaces the discrete 3-stage cascade (BE → lock → dynamic trail) with a
# single tightening funnel: trail distance shrinks continuously from the full
# SL distance at activation down to ADAPTIVE_TRAIL_MIN_ATR as price approaches TP.
#
# Activation threshold: same as TRAIL_ACTIVATE_ATR (price must move N×ATR in
# favour before the trail begins — prevents noise exits near entry).
#
# Mathematical model:
#   progress   = (current_peak − entry) / (tp − entry)     ∈ [0, 1]
#   trail_dist = ATR_SL_MULTIPLIER × (1 − progress)
#              + ADAPTIVE_TRAIL_MIN_ATR × progress          (linear lerp)
#   SL         = peak − trail_dist × ATR                   (LONG)
#              = peak + trail_dist × ATR                    (SHORT)
#
# Concrete example (LONG, entry=100, ATR=1, SL=1.5×, TP=9×, min_trail=0.35):
#   progress = 0.00 (just activated) → trail = 1.50×  → SL ≈ 100.0  (at BE)
#   progress = 0.50 (halfway to TP ) → trail = 0.925× → SL ≈ 104.0  (75% of move locked)
#   progress = 0.85 (85% to TP     ) → trail = 0.523× → SL ≈ 107.1  (85% locked)
#   progress = 1.00 (at TP         ) → trail = 0.35×  → SL ≈ 108.7  (95% locked)
#
# Effect: 'BE' exits are replaced by positive exits that capture 70-95% of the
# full TP potential, even when price reverses just short of the TP level.

ADAPTIVE_TRAILING_ENABLED = os.getenv("ADAPTIVE_TRAILING_ENABLED", "false").lower() == "true"
ADAPTIVE_TRAIL_MIN_ATR    = float(os.getenv("ADAPTIVE_TRAIL_MIN_ATR", "0.35"))

# ══════════════════════════════════════════════════════════════════════════════
# WALK-FORWARD OPTIMIZATION  (ON by default — disable via WFO_ENABLED=false
#                             in .env or via --no-wfo CLI flag)
# ══════════════════════════════════════════════════════════════════════════════
# Eliminates the need to hand-tune BREAKOUT_PERIOD.  Every WFO_RETUNE_INTERVAL
# bars the engine mini-backtests the previous WFO_TRAINING_WINDOW bars for each
# period in BREAKOUT_GRID = [7, 10, 14, 21, 28] and applies the highest
# Profit-Factor winner for the next interval.
#
# No lookahead bias: training ends at bar N-1; apply window starts at bar N.
# No commission / slippage in mini-backtest — it is a *relative* score, not
# a P&L forecast.
#
# Typical cadence (1H bars):
#   WFO_TRAINING_WINDOW = 2160  →  90 calendar days of history
#   WFO_RETUNE_INTERVAL  =  720  →  retune every 30 calendar days
#   WFO_MIN_TRADES       =    4  →  discard BPs with < 4 trades in training

WFO_ENABLED          = os.getenv("WFO_ENABLED", "true").lower() == "true"
WFO_RETUNE_INTERVAL  = int(os.getenv("WFO_RETUNE_INTERVAL",  "720"))   # bars between retuning
WFO_TRAINING_WINDOW  = int(os.getenv("WFO_TRAINING_WINDOW",  "2160"))  # bars of training history
WFO_MIN_TRADES       = int(os.getenv("WFO_MIN_TRADES",       "4"))     # min trades required to accept BP
WFO_CHOPPY_COOLDOWN  = int(os.getenv("WFO_CHOPPY_COOLDOWN",  "6"))     # extended cooldown (bars) when forecast is choppy

# ══════════════════════════════════════════════════════════════════════════════
# MARKOV REGIME FORECAST  (OFF by default — enable via .env or --forecast flag)
# ══════════════════════════════════════════════════════════════════════════════
# Classifies each 1H bar as TREND / CHOPPY / QUIET using ADX, ATR%, and the
# Hurst exponent.  Maintains a rolling 300-bar first-order Markov transition
# matrix to forecast the *next* bar's regime probabilities.
#
# Entry gates applied when enabled:
#   choppy_prob ≥ FORECAST_CHOPPY_THRESHOLD   → suppress entry, extend cooldown
#   trend_prob  ≥ FORECAST_MIN_TREND_PROB     → allow entry, scale size up
#   confidence  < FORECAST_MIN_CONFIDENCE     → ignore forecast, full size
#
# Parameters:
#   FORECAST_CHOPPY_THRESHOLD  — choppy probability above which entries are blocked.
#                                0.65 means "65% chance the next bar is choppy → skip".
#   FORECAST_MIN_TREND_PROB    — trend probability below which size is scaled to 50%.
#                                0.35 means "need ≥ 35% trend probability for full size".
#   FORECAST_MIN_CONFIDENCE    — max-probability below which size scaling is ignored.
#                                0.30 ≈ uniform prior; below this, the model says nothing.

REGIME_FORECAST_ENABLED    = os.getenv("REGIME_FORECAST_ENABLED", "false").lower() == "true"
FORECAST_CHOPPY_THRESHOLD  = float(os.getenv("FORECAST_CHOPPY_THRESHOLD", "0.65"))
FORECAST_MIN_TREND_PROB    = float(os.getenv("FORECAST_MIN_TREND_PROB",   "0.35"))
FORECAST_MIN_CONFIDENCE    = float(os.getenv("FORECAST_MIN_CONFIDENCE",   "0.30"))
