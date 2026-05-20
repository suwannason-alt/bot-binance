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
RISK_PERCENT      = float(os.getenv("RISK_PERCENT", "2.0"))   # % of balance risked per trade
LEVERAGE          = int(os.getenv("LEVERAGE", "5"))

# SL / TP as multiples of ATR
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 3.0   # 2:1 RR → break-even ~37% win rate on 1H (low commission)

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
EMA_1H_MIN_SEP       = 0.002   # EMA20 must lead EMA50 by ≥ 0.2%  (flat market filter)
RSI_1H_LONG_MIN      = 48      # 1H RSI minimum for LONG entry
RSI_1H_LONG_MAX      = 72      # 1H RSI maximum for LONG entry (not overbought)
RSI_1H_SHORT_MAX     = 52      # 1H RSI maximum for SHORT entry
RSI_1H_SHORT_MIN     = 28      # 1H RSI minimum for SHORT entry (not oversold)
ATR_1H_PCT_MIN       = 0.30    # 1H ATR must be ≥ 0.3% of price  (not dead market)
ATR_1H_PCT_MAX       = 5.0     # 1H ATR must be ≤ 5.0% of price  (not extreme spike)
TRADE_COOLDOWN_1H    = 2       # minimum 1H bars between entries
EMA_TREND_SLOPE_BARS = 10      # lookback bars for EMA200 slope direction filter
VOL_RATIO_MIN        = 0.6    # minimum volume ratio vs 20-bar MA (avoids dead markets)

# ── Trading fee (Binance Futures taker) ──────────────────────────────────────
TRADING_FEE = 0.0005       # 0.0500% per fill (matches user spec)
SLIPPAGE    = 0.0002       # 0.02% market impact per fill
