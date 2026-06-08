"""
Shared bot configuration — server, secrets, exchange rules, and the shared
indicator-math inputs that BOTH strategies depend on.

After the multi-strategy restructure the configuration is decoupled into three
modules:

  * ``config.py``     (this file)  — server / secrets / exchange / shared math.
                                      Still ``.env``-driven (these are deployment
                                      values, not strategy tunables).
  * ``config_1h.py``  — 1H momentum-breakout strategy parameters, HARDCODED as
                        literals (``from config import *`` inherits the shared
                        values above, then overrides with the strategy tunables).
                        Also holds the multi-asset ``CONFIG_MATRIX`` (per-symbol
                        ENABLED / TRADE_MODE / tuned params).

Only **server / secret** variables live in ``.env`` now.  Strategy parameters are
hardcoded in the per-strategy modules so each strategy is self-describing and the
two can never silently share a tunable.

Environment variable loading priority (for the values that REMAIN env-driven):
  1. Shell environment   (``export VAR=value``)
  2. ``.env`` file       (loaded via python-dotenv)
  3. Hard-coded default  (second argument of each ``os.getenv``)

Production safety contract
--------------------------
When ``PAPER_TRADING=false`` the module performs a hard validation of all
critical secrets at import time.  A missing API key raises ``SystemExit``
immediately — the process exits before touching any exchange endpoint.
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Repo root = three levels up from src/core/shared/config.py.  Anchoring to it
# keeps the `.env` and default state-DB locations correct regardless of the
# current working directory after the modular restructure (config now lives under
# src/core/shared/, no longer two levels from the root).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Load the root .env explicitly (was a bare load_dotenv() that walked up from cwd).
load_dotenv(_REPO_ROOT / ".env")

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

# ── Discord notifications ─────────────────────────────────────────────────────
# Create a Channel → Integrations → Webhook in Discord and paste its URL here.
# Leave DISCORD_WEBHOOK_URL empty to disable all Discord alerts (no-op).
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DISCORD_ENABLED     = bool(DISCORD_WEBHOOK_URL)

# ── State persistence ─────────────────────────────────────────────────────────
# Path to the SQLite database used by StateManager for crash recovery.
# Default is anchored to the repo root (NOT src/core/shared/) so it stays at the
# project top level; relative env overrides resolve from the current working dir.
BOT_STATE_DB_PATH = os.getenv(
    "BOT_STATE_DB_PATH",
    os.path.join(str(_REPO_ROOT), "bot_state.db"),
)

WS_URL = (
    "wss://fstream.binance.com/market/stream?streams="
    f"{SYMBOL.lower()}@kline_5m/"
    f"{SYMBOL.lower()}@kline_1h/"
    f"{SYMBOL.lower()}@markPrice/"
    f"{SYMBOL.lower()}@aggTrade"
)

# ── Account-level (shared by every strategy on this account) ──────────────────
LEVERAGE = int(os.getenv("LEVERAGE", "10"))   # futures leverage multiplier

# ── Warm-up periods (shared) ──────────────────────────────────────────────────
MIN_CANDLES_5M = int(os.getenv("MIN_CANDLES_5M", "60"))
MIN_CANDLES_1H = int(os.getenv("MIN_CANDLES_1H", "210"))  # ≥ EMA_TREND
MAX_CANDLES    = 600

# ── Indicator periods (shared math library inputs) ────────────────────────────
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

# ── Legacy 5M-timeframe constants used by backtest diagnostic modes ───────────
# (mtf_stop / 5m_breakout, kept shared so both the 1H engine's diagnostic modes
# and the new 5M strategy can reference a common cooldown / breakout window.)
TRADE_COOLDOWN_5M    = 12      # minimum 5M bars between 5M entries (~1 hour)
BREAKOUT_PERIOD_5M   = 84      # rolling high/low window for 5M breakout (84×5M = 7h)

# ── Trading fee (Binance Futures taker) — shared exchange cost ────────────────
TRADING_FEE = 0.0005       # 0.0500% per fill (matches user spec)
SLIPPAGE    = 0.0002       # 0.02% market impact per fill

# ── Exchange order rules (shared — Binance BTCUSDT perp) ──────────────────────
MIN_ORDER_QTY      = float(os.getenv("MIN_ORDER_QTY", "0.001"))     # BTC min lot size
QTY_STEP           = float(os.getenv("QTY_STEP",      "0.001"))     # BTC quantity step
PRICE_TICK         = float(os.getenv("PRICE_TICK",    "0.10"))      # BTC price tick (0.1 USDT)

# ── Live infra (shared) ───────────────────────────────────────────────────────
# Heartbeat: log a status line every N seconds even with no trade activity.
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "900"))    # 15 min
# Live position sync interval (seconds): how often to poll the exchange to detect
# when an SL/TP order filled outside our WebSocket feed.
LIVE_POSITION_SYNC_SECS  = int(os.getenv("LIVE_POSITION_SYNC_SECS",  "30"))
# Funding rate guard: skip new entries when |funding_rate| exceeds this threshold.
FUNDING_RATE_MAX   = float(os.getenv("FUNDING_RATE_MAX", "0.001"))  # 0.10 %/8h (0 = off)
