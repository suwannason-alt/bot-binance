# BTCUSDT Futures Trading Bot

Autonomous algorithmic trading bot for **Binance USDT-M Perpetual Futures**.  
Strategy: **1H momentum breakout** with optional **Walk-Forward Optimization (WFO)**.  
Verified over **5 years of live Binance data** (May 2021 → May 2026).

---

## Table of Contents

1. [Project Architecture Overview](#1-project-architecture-overview)
2. [Live Performance Verdict](#2-live-performance-verdict)
3. [Prerequisites & Installation](#3-prerequisites--installation)
4. [Environment Configuration](#4-environment-configuration)
5. [Warm-Start & Hydration](#5-warm-start--hydration)
6. [Production Execution Commands](#6-production-execution-commands)
7. [Backtest Commands](#7-backtest-commands)
8. [Safety Measures & Emergency Kill Switch](#8-safety-measures--emergency-kill-switch)
9. [File Structure](#9-file-structure)
10. [Risk Warning](#10-risk-warning)

---

## 1. Project Architecture Overview

### Strategy Core — 1H Breakout

The engine trades BTCUSDT perpetual futures on Binance using a **1-hour close-break breakout signal**:

| Component | Details |
|-----------|---------|
| **Entry trigger** | Close > N-bar rolling high (LONG) or Close < N-bar rolling low (SHORT) |
| **Trend alignment** | Price above/below EMA 200 |
| **Momentum confirmation** | EMA20 > EMA50 with ≥ 0.1% separation |
| **Volatility filter** | ADX ≥ 20 (blocks choppy/ranging markets) |
| **Volume filter** | Current volume ≥ 0.3× 20-bar average |
| **Stop Loss** | 1.5× ATR from entry |
| **Take Profit** | 6.0× ATR from entry (4:1 RR — break-even at 20% win rate) |
| **Risk per trade** | 8% of current balance (scales with compounding) |
| **Leverage** | 10× |
| **Breakout period** | 14 bars (or auto-tuned by WFO every 30 days) |

### Autonomous Features

```
main.py
  │
  ├─ StateManager (SQLite)        — crash recovery, saves state every 6 bars
  │
  ├─ WarmStart                    — on every startup:
  │    ├─ Fetch ~3,030 historical 1H bars (~126 days)
  │    ├─ Dry-run hydration loop  — advances WFO + Markov forecaster
  │    └─ Seed CandleBuffer       — indicators warm on tick #1 (no cold start)
  │
  ├─ WalkForwardOptimizer (WFO)   — optional, enabled via WFO_ENABLED=true
  │    ├─ Retunes every 720 1H bars (≈ 30 days)
  │    ├─ Trains over last 2,160 bars (≈ 90 days)
  │    └─ Selects best BREAKOUT_PERIOD from [7, 10, 14, 21, 28]
  │
  └─ MarkovRegimeForecaster        — optional, enabled via REGIME_FORECAST_ENABLED=true
       ├─ Classifies each bar as TREND / CHOPPY / QUIET
       ├─ Maintains 300-bar first-order Markov transition matrix
       └─ Suppresses entries when choppy_prob ≥ 65%
```

### Live Trading Loop (per 1H bar close)

```
on_1h_close()
  1. Append bar to LiveHistory (sliding 3,600-bar window)
  2. Update Markov forecaster (if enabled) → entry gate + size scale
  3. WFO retune check (if enabled) → update active BREAKOUT_PERIOD
  4. Pre-entry guards: daily_halted? session filter? consecutive SL? funding rate?
  5. Evaluate 1H breakout signal → place order (market or limit)
  6. Save state to SQLite (every 6 bars)
```

---

## 2. Live Performance Verdict

**Test period:** May 2021 → May 2026 (5 years)  
**Starting balance:** $1,000 USDT  
**Risk:** 8% per trade, 10× leverage, ADX ≥ 20, TP × 6.0

| Mode | Command | CAGR | Max DD | 5-yr Return | All Years+ | Verdict |
|------|---------|------|--------|-------------|-----------|---------|
| **Classic** | `python main.py` | **+112%/yr** | −49% | **$1k → $42k** | ✅ 5/5 | ✅ **RECOMMENDED** |
| WFO only | `python main.py --wfo` | +101%/yr | −53% | $1k → $32k | ✅ 5/5 | ✅ Safe option |
| Forecast only | `--forecast` | +63%/yr | −50% | $1k → $11k | ✅ 5/5 | ⚠️ Blocks too many entries |
| Adaptive TP/SL | `--adaptive` | +21%/yr | −42% | $1k → $2.5k | ❌ 3/5 | ❌ Not recommended |
| All features | `--all` | +15%/yr | −21% | $1k → $2k | ❌ 4/5 | ❌ Not recommended |

> **Why Classic beats the autonomous features:** The 5-year backtest audit
> (May 2026) shows that the Adaptive TP funnel (4×→10× ATR) reduces TP hits
> from 32 down to 2 in 5 years — almost every trade exits at break-even or
> stop-loss. The fixed 6×ATR TP is already well-calibrated for BTC volatility
> regimes. All quant features should remain OFF for maximum growth.

### Year-by-Year (Classic mode)

| Year | Start | End | Return | Trades |
|------|-------|-----|--------|--------|
| Y1 (May 2021–22) | $1,000 | $4,068 | +307% | 25 |
| Y2 (May 2022–23) | $4,068 | $5,011 | +23% | 21 |
| Y3 (May 2023–24) | $5,011 | $15,050 | +200% | 19 |
| Y4 (May 2024–25) | $15,050 | $30,331 | +102% | 39 |
| Y5 (May 2025–26) | $30,331 | $42,187 | +39% | 28 |

---

## 3. Prerequisites & Installation

### Requirements

- Python 3.11+
- Binance Futures account with USDT margin wallet
- A Linux VPS (Ubuntu 22.04 LTS recommended) with static IP for API key whitelisting

### Install

```bash
# 1. Clone the repository
git clone <your-repo-url> trading-bot
cd trading-bot

# 2. Create a virtual environment (strongly recommended)
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

**`requirements.txt` dependencies:**

| Package | Purpose |
|---------|---------|
| `websockets` | Binance WebSocket streams (klines, mark price, aggTrade) |
| `ccxt` | REST API for order placement and account queries |
| `python-dotenv` | Load `.env` file into environment |
| `numpy` | Vectorised indicator calculations (EMA, ATR, ADX) |
| `pandas` | OHLCV DataFrame processing and CSV caching |
| `aiohttp` | Async HTTP for warm-start historical data fetch |

---

## 4. Environment Configuration

### Step 1 — Copy the template

```bash
cp .env.example .env
```

### Step 2 — Fill in your credentials

```bash
nano .env        # or: vim .env
```

**Minimum required changes for live trading:**

```env
# Must change these three:
PAPER_TRADING=false
BINANCE_API_KEY=<your real key>
BINANCE_API_SECRET=<your real secret>
```

**Strongly recommended additions:**

```env
# Telegram alerts (get notified of every trade and error)
TELEGRAM_BOT_TOKEN=123456789:AAF...
TELEGRAM_CHAT_ID=987654321

# Hard stop after 3 consecutive SL hits
MAX_CONSECUTIVE_LOSSES=3
POST_SL_COOLDOWN_1H=3
```

### Step 3 — Secure the file

```bash
chmod 600 .env      # owner read/write only — other users cannot read it
```

### Binance API Key Setup (important)

1. Log in to Binance → Profile → **API Management** → **Create API**
2. Label it (e.g., "trading-bot-vps")
3. Enable: ✅ **Enable Futures**
4. Disable: ❌ **Enable Withdrawals** (never needed)
5. Under **IP Access Restrictions** → select **Restrict access to trusted IPs only** → add your server's IP
6. Copy the **API Key** and **Secret Key** → paste into `.env`

> The secret is shown **once**. If you lose it, delete the key and create a new one.

### Configuration Reference

All strategy parameters live in `config.py` and are tunable via `.env` overrides.
The most important production settings:

| Variable | Default | Effect |
|----------|---------|--------|
| `RISK_PERCENT` | `8.0` | % of balance risked per trade |
| `DAILY_PROFIT_TARGET_USD` | `110.0` | Stop trading after this daily gain |
| `DAILY_LOSS_LIMIT_USD` | `50.0` | Stop trading after this daily loss |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Circuit breaker: N SLs in a row halts the day |
| `FUNDING_RATE_MAX` | `0.001` | Skip entries above 0.10%/8h funding |
| `WFO_ENABLED` | `false` | Auto-tune BREAKOUT_PERIOD (safe to enable) |

---

## 5. Warm-Start & Hydration

The bot **never starts cold.** On every startup it executes a three-phase warm start before connecting to any live WebSocket feed:

```
Startup sequence
────────────────
[1] StateManager.load()
      └─ If fresh state exists (< 48h old):  WarmStart.recover()
            → restore WFO params + forecaster buffer from SQLite
            → fetch only the bars needed to refresh LiveHistory
            → total startup time: ~2–5 seconds

      └─ If stale / first run:  WarmStart.run()
            → calculate required lookback: 3,030 1H bars (≈ 126 days)
            → fetch from Binance REST API  (cached to CSV after first fetch)
            → dry-run hydration loop:      0.43 seconds
              ├─ advances WFO optimizer    → 2 retunings on first run
              └─ fills Markov buffer       → 300 observations
            → total startup time: ~30–90 seconds (network-bound)

[2] WarmStart.hydrate_candle_buffer()
      → seeds the last 600 bars into MarketState.buf_1h
      → EMA200, RSI, ADX, ATR all fully warm on tick #1

[3] StateManager.save()
      → persists hydrated state to bot_state.db immediately
      → subsequent restarts use the fast recovery path
```

**Sample startup log:**

```
[INFO] main: No saved state — running full warm start …
[INFO] warm_start: Fetching 3030 1H bars for BTCUSDT (127 days)…
[INFO] warm_start: Hydrating WFO + forecaster over 3030 bars…
[INFO] warm_start: ─────────────────────────────────────────
[INFO] warm_start:   Lookback bars  : 3030  (126.2 days)
[INFO] warm_start:   Fetch time     : 28.4 s
[INFO] warm_start:   Hydrate time   :  0.43 s
[INFO] warm_start:   WFO retunings  : 2
[INFO] warm_start:   Active BP      : 14 bars
[INFO] warm_start:   Forecaster obs : 300
[INFO] warm_start:   LiveHistory    : 3030 bars
[INFO] main: Starting trading bot [LIVE] symbol=BTCUSDT
```

> On subsequent restarts (crash recovery), the fetch step is skipped and startup
> completes in **under 5 seconds**.

---

## 6. Production Execution Commands

### Verify paper trading first (always)

```bash
# Run in paper mode for at least 24–48h before enabling live orders.
# Paper mode receives real market data but simulates all fills locally.
PAPER_TRADING=true python main.py
```

### Classic mode (recommended for maximum growth)

```bash
python main.py
```

### WFO mode (auto-tunes BREAKOUT_PERIOD every 30 days)

```bash
WFO_ENABLED=true python main.py
# or set WFO_ENABLED=true in .env and run:
python main.py
```

### Run in the background with `nohup` (simple, no dependencies)

```bash
# Start detached, log to trading-bot.log
nohup python main.py >> trading-bot.log 2>&1 &

# Check it's running
ps aux | grep main.py

# Follow live logs
tail -f trading-bot.log

# Stop it gracefully (sends SIGTERM → bot saves state before exiting)
kill -SIGTERM $(pgrep -f "python main.py")
```

### Run as a `systemd` service (recommended for production VPS)

Create the service unit file:

```bash
sudo nano /etc/systemd/system/trading-bot.service
```

Paste this content (adjust paths to match your setup):

```ini
[Unit]
Description=BTCUSDT Futures Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/trading-bot
EnvironmentFile=/home/ubuntu/trading-bot/.env
ExecStart=/home/ubuntu/trading-bot/.venv/bin/python main.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal
SyslogIdentifier=trading-bot

# Graceful shutdown — gives the bot 15s to save state before SIGKILL
TimeoutStopSec=15
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-bot     # auto-start on server reboot
sudo systemctl start trading-bot

# Check status
sudo systemctl status trading-bot

# Follow live logs
sudo journalctl -u trading-bot -f

# Restart after a config change
sudo systemctl restart trading-bot
```

### Run with `pm2` (alternative process manager)

```bash
# Install pm2 globally
npm install -g pm2

# Start the bot
pm2 start main.py --name trading-bot --interpreter python3

# Auto-start on server reboot
pm2 save
pm2 startup

# Follow logs
pm2 logs trading-bot

# Restart
pm2 restart trading-bot

# Stop
pm2 stop trading-bot
```

---

## 7. Backtest Commands

All backtests use locally cached CSV data (fetched once, stored in `data/`).

```bash
# Classic 5-year backtest (baseline, ~30 seconds)
python run_backtest.py

# WFO mode — auto-tunes BREAKOUT_PERIOD every 30 days
python run_backtest.py --wfo

# 6-year window (extra stress test)
python run_backtest.py --days 2190

# Custom risk/TP parameters
python run_backtest.py --risk 6 --tp 7.0 --sl 1.5 --adx 25

# Show all available flags
python run_backtest.py --help
```

---

## 8. Safety Measures & Emergency Kill Switch

### Graceful shutdown (saves open position state)

```bash
# Send SIGTERM — the bot saves state and logs a clean session summary before exiting.
# Open positions are NOT closed — they continue on Binance with their SL/TP orders.
kill -SIGTERM $(pgrep -f "python main.py")

# With systemd:
sudo systemctl stop trading-bot

# With pm2:
pm2 stop trading-bot
```

### Hard stop (process killed immediately)

```bash
# Use only if SIGTERM is not responding.
# State is NOT saved — next startup triggers a full warm start.
kill -9 $(pgrep -f "python main.py")
```

### Close all open positions manually on Binance

After stopping the bot, log in to Binance Futures and:

1. Navigate to **Futures** → **Positions** tab
2. Click **Close All** (or close individual positions)
3. Alternatively, cancel all open SL/TP orders under **Open Orders** first

> The bot never places orders larger than one position per symbol. There will
> be at most **one open BTC/USDT position** at any time.

### Disable the API key immediately (emergency)

If you suspect the API key is compromised:

1. Binance → Profile → **API Management**
2. Find the key → click **Delete**
3. Generate a new key → update `.env` → restart the bot

The bot cannot withdraw funds — keys are Futures-only with no withdrawal permission.

### Reset bot state (start fresh after strategy change)

```bash
# From the project directory
python -c "from state_manager import StateManager; StateManager().clear()"
# Next startup will trigger a full warm start (~30–90 seconds)
```

### Built-in automatic circuit breakers

These fire automatically without any manual intervention:

| Guard | Default | Behaviour |
|-------|---------|-----------|
| Daily profit target | $110 USDT | Halt all new entries until UTC midnight |
| Daily loss limit | $50 USDT | Halt all new entries until UTC midnight |
| Consecutive SL breaker | 3 in a row | Halt all new entries until UTC midnight |
| Funding rate guard | 0.10%/8h | Skip new entries (position stays open) |
| Post-SL cooldown | 3 bars | Wait 3h after any stop-loss before re-entering |
| Session filter | Off (24/7) | Configurable UTC hour window for entries |

### Monitoring checklist for live operation

```bash
# 1. Confirm bot is running
sudo systemctl status trading-bot    # or: ps aux | grep main.py

# 2. Check last N log lines for errors
sudo journalctl -u trading-bot -n 50

# 3. Verify state database is being written
ls -lh bot_state.db     # should update every ~6 hours (every 6 bars)

# 4. Check Binance position directly (do not rely solely on bot logs)
# Log in to Binance Futures → Positions tab

# 5. Watch for the heartbeat log line every 15 minutes
# "[INFO] main: heartbeat | ..."  — if absent for > 30min, the bot may be stalled
```

---

## 9. File Structure

```
trading-bot/
│
├── main.py                  Bot entry point — live/paper trading loop
├── strategy.py              Signal logic: evaluate_1h_live(), evaluate_1h_signal()
├── trader.py                Order execution, daily P&L tracking, halt logic
│
├── backtest.py              Vectorised backtesting engine
├── run_backtest.py          Single-call autonomous runner (no parameter sweep)
├── visualize.py             Equity curve + trade marker charts
│
├── config.py                All strategy & risk parameters (env-driven)
├── indicators.py            NumPy indicator library (EMA, RSI, ATR, ADX, BB)
├── adaptive_regime.py       Hurst exponent + BBW + ADX composite regime scorer
│
├── walk_forward_optimizer.py  WFO engine — BREAKOUT_PERIOD self-tuning
├── regime_forecast.py         Markov regime forecaster (TREND/CHOPPY/QUIET)
├── state_manager.py           SQLite crash-recovery persistence
├── warm_start.py              Historical pre-loader + dry-run hydration
│
├── data_store.py            Rolling OHLCV buffers for live candle state
├── ws_client.py             Binance WebSocket client (5M + 1H + markPrice)
├── fetch_data.py            Historical data downloader (Binance REST API)
│
├── data/
│   ├── btcusdt_1h.csv       Cached 1H OHLCV (auto-updated on startup)
│   └── btcusdt_5m.csv       Cached 5M OHLCV (used for backtest only)
│
├── backtest_results/
│   └── report.png           Equity curve chart from last backtest run
│
├── bot_state.db             SQLite state database (created at runtime)
├── .env                     Your live credentials (NEVER commit this file)
├── .env.example             Configuration template — copy to .env
├── requirements.txt         Python dependencies
└── .gitignore               Excludes .env, bot_state.db, data/, __pycache__/
```

---

## 10. Risk Warning

> **Trading futures with leverage involves substantial risk of loss.**  
> Past backtest results do not guarantee future performance.

- Always run in **paper trading mode first** (`PAPER_TRADING=true`) for at least 24–48 hours to verify your setup
- The 1H breakout strategy fires on approximately **10–40 signals per year** — do not expect a trade every day
- A single trade can lose up to 8% of your account balance (at the default `RISK_PERCENT=8.0`)
- The strategy's worst drawdown in 5 years was **−49%** — ensure you can withstand this psychologically and financially before running live
- **Start with a small balance** (e.g., $200–500 USDT) and scale up only after validating live performance over several months
- **Never risk capital you cannot afford to lose**
