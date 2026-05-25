# BTCUSDT Futures Trading Bot

Autonomous algorithmic trading bot for **Binance USDT-M Perpetual Futures**.  
Strategy: **1H momentum breakout** with **Walk-Forward Optimization (WFO) on by default**.  
Verified over **5 years of live Binance data** (May 2021 → May 2026).

---

## Table of Contents

1. [Project Architecture Overview](#1-project-architecture-overview)
2. [Live Performance Verdict](#2-live-performance-verdict)
3. [Prerequisites & Installation](#3-prerequisites--installation)
4. [Environment Configuration](#4-environment-configuration)
5. [Warm-Start & Hydration](#5-warm-start--hydration)
6. [Production Execution Commands](#6-production-execution-commands)
7. [Docker Deployment](#7-docker-deployment)
8. [Backtest Commands](#8-backtest-commands)
9. [Safety Measures & Emergency Kill Switch](#9-safety-measures--emergency-kill-switch)
10. [File Structure](#10-file-structure)
11. [Risk Warning](#11-risk-warning)

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
| **Position sizing** | `equity × 35% × 10× leverage ÷ entry_price` (see §2) |
| **Leverage** | 10× |
| **Breakout period** | Auto-tuned by WFO every 30 days (or fixed at 14 bars with `--no-wfo`) |

### Autonomous Features

```
main.py
  │
  ├─ StateManager (SQLite)        — crash recovery, saves state every 6 bars
  │
  ├─ WarmStart                    — on every startup:
  │    ├─ Fetch ~3,030 historical 1H bars (~126 days)
  │    ├─ Dry-run hydration loop  — advances WFO + Markov forecaster
  │    └─ Seed CandleBuffer       — indicators fully warm on tick #1 (no cold start)
  │
  ├─ WalkForwardOptimizer (WFO)   — ON by default (--no-wfo to disable)
  │    ├─ Retunes every 720 1H bars (≈ 30 days)
  │    ├─ Trains over last 2,160 bars (≈ 90 days)
  │    └─ Selects best BREAKOUT_PERIOD from [7, 10, 14, 21, 28]
  │
  └─ MarkovRegimeForecaster        — optional, enable via --forecast or REGIME_FORECAST_ENABLED=true
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
  5. Fetch total equity from Binance (wallet_balance + unrealized_pnl)
  6. Evaluate 1H breakout signal → place order (market or limit)
  7. Save state to SQLite (every 6 bars)
```

### Position Sizing — Dynamic Equity Model

The bot uses a **fixed-fraction equity** sizing model with three fallback tiers:

| Priority | Mode | Formula | Active when |
|----------|------|---------|------------|
| **0 (default)** | **Equity-percent** | `qty = (equity × 35% × 10×) / entry` | `EQUITY_PERCENT > 0` |
| 1 | Fixed margin | `qty = (ORDER_BALANCE_USD × 10×) / entry` | `EQUITY_PERCENT=0` and `ORDER_BALANCE_USD > 0` |
| 2 | Risk-percent | `qty = (balance × 8%) / (entry × sl_dist)` | `EQUITY_PERCENT=0`, `ORDER_BALANCE_USD=0` |

In **live mode**, `equity` = `totalWalletBalance + totalUnrealizedProfit` fetched from the Binance API at the moment of order placement — unrealised PnL on any open position is included in the sizing calculation.

---

## 2. Live Performance Verdict

**Test period:** May 2021 → May 2026 (5 years)  
**Starting balance:** $1,000 USDT  
**Leverage:** 10×  |  **TP:** 6.0× ATR  |  **SL:** 1.5× ATR  |  **ADX ≥ 20**

### Production-default results (`EQUITY_PERCENT=35`)

| Mode | Command | CAGR | Max DD | 5-yr Return | All Years+ | Verdict |
|------|---------|------|--------|-------------|-----------|---------|
| **WFO** *(default)* | `python main.py` | **+58.3%/yr** | −37.9% | **$1k → $9.8k** | ✅ 5/5 | ✅ **DEFAULT** |
| Classic | `python main.py --no-wfo` | **+68.6%/yr** | −34.7% | **$1k → $13.5k** | ✅ 5/5 | ✅ Lower drawdown |

### Maximum-growth reference (`EQUITY_PERCENT=0`, `RISK_PERCENT=8`)

| Mode | CAGR | Max DD | 5-yr Return | All Years+ | Verdict |
|------|------|--------|-------------|-----------|---------|
| Classic | +112%/yr | −49% | $1k → $42k | ✅ 5/5 | ✅ Maximum CAGR |
| WFO | +101%/yr | −53% | $1k → $32k | ✅ 5/5 | ✅ RECOMMENDED |

> **EQUITY_PERCENT vs RISK_PERCENT — key trade-off:**
>
> `EQUITY_PERCENT=35` allocates a fixed fraction of equity to each trade.  
> `RISK_PERCENT=8` scales position size inversely with current ATR, so each SL hit always costs exactly 8% of balance.
>
> They are equivalent at average ATR (~1.5%), but diverge in volatile regimes:
>
> | ATR at entry | EQUITY=35% per-SL loss | RISK=8% per-SL loss |
> |---|---|---|
> | 1.5% (normal) | **7.9%** | **8.0%** ← equivalent |
> | 3.0% (volatile) | **15.7%** | **8.0%** |
> | 5.0% (extreme) | **26.3%** | **8.0%** |
>
> RISK_PERCENT=8% is the proven maximum-CAGR configuration. Set `EQUITY_PERCENT=0` in `.env` to activate it.

### Year-by-year breakdown

#### EQUITY_PERCENT=35% + WFO *(production default)*

| Year | Start | End | Return | Trades | TP / SL / BE |
|------|-------|-----|--------|--------|-------------|
| Y1 (May 2021–22) | $1,000 | $2,553 | +155.3% ✅ | 25 | 8 / 8 / 9 |
| Y2 (May 2022–23) | $2,553 | $2,867 | +12.3% ✅ | 22 | 4 / 11 / 7 |
| Y3 (May 2023–24) | $2,867 | $5,013 | +74.9% ✅ | 19 | 7 / 8 / 4 |
| Y4 (May 2024–25) | $5,013 | $7,776 | +55.1% ✅ | 42 | 7 / 11 / 24 |
| Y5 (May 2025–26) | $7,776 | $9,829 | +26.4% ✅ | 30 | 6 / 14 / 10 |

Total: **138 trades** — Win rate 23.2% — Profit Factor 1.60 — Sharpe 0.88

#### EQUITY_PERCENT=35% + Classic (`--no-wfo`)

| Year | Start | End | Return | Trades | TP / SL / BE |
|------|-------|-----|--------|--------|-------------|
| Y1 (May 2021–22) | $1,000 | $2,553 | +155.3% ✅ | 25 | 8 / 8 / 9 |
| Y2 (May 2022–23) | $2,553 | $3,334 | +30.6% ✅ | 21 | 4 / 9 / 8 |
| Y3 (May 2023–24) | $3,334 | $5,831 | +74.9% ✅ | 19 | 7 / 8 / 4 |
| Y4 (May 2024–25) | $5,831 | $9,780 | +67.7% ✅ | 41 | 7 / 11 / 23 |
| Y5 (May 2025–26) | $9,780 | $13,482 | +37.9% ✅ | 29 | 6 / 13 / 10 |

Total: **135 trades** — Win rate 23.7% — Profit Factor 1.72 — Sharpe 0.97

### Why classic beats WFO (in backtests)

WFO retunes BREAKOUT_PERIOD every 30 days — this is **anti-overfitting protection** for live trading, not a backtest optimiser. In backtests WFO sometimes picks a worse period for the next 30-day window because it cannot see the future. In live trading its key value is **avoiding stale parameters**: markets that shift regime get a fresh BREAKOUT_PERIOD within 30 days instead of running the same 14-bar window indefinitely. WFO is the recommended default for production.

---

## 3. Prerequisites & Installation

### Requirements

- Python 3.11+ (Python 3.12.7 used in Docker image)
- Binance Futures account with USDT margin wallet
- A Linux VPS (Ubuntu 22.04 LTS recommended) with static IP for API key whitelisting
- Docker + Docker Compose (optional, for containerised deployment)

### Install (bare Python)

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
| `EQUITY_PERCENT` | `35.0` | Equity % used as margin per trade (35% × 10× leverage = 3.5× effective position). Set to `0` to use `RISK_PERCENT` mode instead. |
| `RISK_PERCENT` | `8.0` | % of balance risked per trade — **only active when `EQUITY_PERCENT=0`** |
| `LEVERAGE` | `10` | Futures leverage multiplier |
| `WFO_ENABLED` | `true` | Auto-tune BREAKOUT_PERIOD every 30 days (default: on) |
| `DAILY_PROFIT_TARGET_USD` | `110.0` | Stop trading after this daily gain |
| `DAILY_LOSS_LIMIT_USD` | `50.0` | Stop trading after this daily loss |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Circuit breaker: N SLs in a row halts the day |
| `FUNDING_RATE_MAX` | `0.001` | Skip new entries above 0.10%/8h funding rate |
| `BOT_STATE_DB_PATH` | `bot_state.db` | SQLite state database path (auto-created) |

---

## 5. Warm-Start & Hydration

The bot **never starts cold.** On every startup it executes a three-phase warm start before connecting to any live WebSocket feed:

```
Startup sequence
────────────────
[1] StateManager.load()   (path from BOT_STATE_DB_PATH)
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
[INFO] main: Autonomous mode: WFO(retune=720bars/train=2160bars)
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

### Default mode — WFO enabled (recommended for production)

```bash
# WFO is ON by default — no flags needed.
python main.py
```

### Classic mode — fixed BREAKOUT_PERIOD=14

```bash
# Disable WFO: higher CAGR in backtest, no adaptive period selection.
python main.py --no-wfo
```

### Additional runtime flags

```bash
# WFO + Markov regime forecast (suppresses entries in predicted-choppy bars)
python main.py --forecast

# Show all available flags
python main.py --help
```

### Run in the background with `nohup` (simple, no dependencies)

```bash
# Start detached, log to trading-bot.log (WFO on by default)
nohup python main.py >> trading-bot.log 2>&1 &

# Check it's running
ps aux | grep main.py

# Follow live logs
tail -f trading-bot.log

# Stop gracefully (sends SIGTERM → bot saves state before exiting)
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

# Graceful shutdown — gives the bot 30s to save state before SIGKILL
TimeoutStopSec=30
KillSignal=SIGTERM

[Install]
WantedBy=multi-user.target
```

> WFO is on by default — no flags needed in `ExecStart`.  
> To use classic mode: `ExecStart=... python main.py --no-wfo`

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

# Start the bot (WFO on by default)
pm2 start main.py --name trading-bot --interpreter python3

# Auto-start on server reboot
pm2 save
pm2 startup

# Follow logs
pm2 logs trading-bot

# Restart / Stop
pm2 restart trading-bot
pm2 stop trading-bot
```

---

## 7. Docker Deployment

### Build and start

```bash
# First time — build image and start in the background
docker compose up -d --build

# Follow live logs
docker compose logs -f

# Stop gracefully (SIGTERM → 30s grace period → saves state)
docker compose stop

# Restart after a code change
docker compose build --no-cache && docker compose up -d
```

### How it works

```
Dockerfile (multi-stage)
  │
  ├─ Stage 1 (builder)  — python:3.12.7-slim-bullseye
  │    ├─ apt: gcc, libffi-dev  (compile C-extension wheels)
  │    └─ pip install -r requirements.txt → /opt/venv
  │
  └─ Stage 2 (runtime)  — python:3.12.7-slim-bullseye
       ├─ apt: libffi8, ca-certificates, tzdata  (runtime only)
       ├─ COPY --from=builder /opt/venv /opt/venv  (no compiler in final image)
       ├─ Non-root user: botuser (uid 1001)
       ├─ WORKDIR /app  →  COPY *.py ./
       ├─ VOLUME /app/state  ← StateManager SQLite DB
       ├─ VOLUME /app/data   ← OHLCV CSV cache
       └─ ENTRYPOINT ["python", "main.py"]
```

### Named volumes — data persistence

| Volume | Mount | Contents | Survives |
|--------|-------|---------|---------|
| `bot_state` | `/app/state` | SQLite state DB (`bot_state.db`) | Restarts, rebuilds, `docker compose down` |
| `market_data` | `/app/data` | 1H + 5M OHLCV CSV cache | Restarts, rebuilds, `docker compose down` |

> Volumes are destroyed **only** by `docker compose down -v` (explicit volume removal).
> Never baked into the image — a `docker compose build` does not touch live data.

### Override the execution mode

In `docker-compose.yml`, the `command:` field passes CLI args after the entrypoint:

```yaml
# WFO on (default — leave blank)
command: []

# Classic mode
command: ["--no-wfo"]

# WFO + Markov forecast
command: ["--forecast"]
```

Alternatively, set env vars inline:

```yaml
environment:
  WFO_ENABLED: "false"     # classic static mode
```

### One-off Docker commands

```bash
# Open an interactive shell inside the container
docker compose run --rm binance-bot bash

# Reset the state DB (next startup triggers full warm start)
docker compose run --rm binance-bot \
  python -c "from state_manager import StateManager; \
             StateManager('/app/state/bot_state.db').clear()"

# View current state DB (inspect WFO log, last trade)
docker compose run --rm binance-bot \
  python -c "from state_manager import StateManager; \
             import json; s = StateManager('/app/state/bot_state.db').load(); \
             print(json.dumps(s, indent=2, default=str))"

# Hard kill (state NOT saved)
docker compose kill
```

### Resource limits

The container is capped at **512 MB RAM** (configurable in `docker-compose.yml`).  
Typical usage: 80–150 MB during WFO hydration; 40–60 MB steady-state.

---

## 8. Backtest Commands

All backtests use locally cached CSV data (fetched once, stored in `data/`).

```bash
# Default: 5-year WFO backtest (matches live production default)
python run_backtest.py

# Classic mode — fixed BREAKOUT_PERIOD=14, no WFO
python run_backtest.py --no-wfo

# WFO + Markov regime forecast
python run_backtest.py --forecast

# Run all feature combinations (benchmark sweep)
python run_backtest.py --all

# Custom date range / risk parameters
python run_backtest.py --days 2190              # 6-year window
python run_backtest.py --risk 6 --tp 7.0 --adx 25

# Switch to RISK_PERCENT=8% sizing (set EQUITY_PERCENT=0 first)
EQUITY_PERCENT=0 python run_backtest.py --no-wfo

# Show all available flags
python run_backtest.py --help
```

> The backtest reads `EQUITY_PERCENT` from the environment (or `.env`).  
> Change it at the command line (`EQUITY_PERCENT=0 python run_backtest.py`) without editing `.env`.

---

## 9. Safety Measures & Emergency Kill Switch

### Graceful shutdown (saves open position state)

```bash
# Send SIGTERM — the bot saves state and logs a clean session summary before exiting.
# Open positions are NOT closed — they continue on Binance with their SL/TP orders.
kill -SIGTERM $(pgrep -f "python main.py")

# With systemd:
sudo systemctl stop trading-bot

# With pm2:
pm2 stop trading-bot

# With Docker:
docker compose stop
```

### Hard stop (process killed immediately)

```bash
# Use only if SIGTERM is not responding.
# State is NOT saved — next startup triggers a full warm start.
kill -9 $(pgrep -f "python main.py")
docker compose kill    # Docker equivalent
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
# Bare Python
python -c "from state_manager import StateManager; StateManager().clear()"

# Docker
docker compose run --rm binance-bot \
  python -c "from state_manager import StateManager; \
             StateManager('/app/state/bot_state.db').clear()"
```

Next startup triggers a full warm start (~30–90 seconds).

### Built-in automatic circuit breakers

These fire automatically without any manual intervention:

| Guard | Default | Behaviour |
|-------|---------|-----------|
| Daily profit target | $110 USDT | Halt all new entries until UTC midnight |
| Daily loss limit | $50 USDT | Halt all new entries until UTC midnight |
| Consecutive SL breaker | 3 in a row | Halt all new entries until UTC midnight |
| Post-SL cooldown | 3 bars (3h) | Wait before re-entering after any stop-loss |
| Funding rate guard | 0.10%/8h | Skip new entries (open position unaffected) |
| Session filter | Off (24/7) | Configurable UTC hour window for entries |

### Monitoring checklist for live operation

```bash
# 1. Confirm bot is running
sudo systemctl status trading-bot    # or: ps aux | grep main.py
docker compose ps                    # Docker

# 2. Check last N log lines for errors
sudo journalctl -u trading-bot -n 50
docker compose logs --tail 50        # Docker

# 3. Verify state database is being written
ls -lh bot_state.db                  # should update every ~6 hours
docker compose run --rm binance-bot ls -lh /app/state/bot_state.db   # Docker

# 4. Check Binance position directly (do not rely solely on bot logs)
# Log in to Binance Futures → Positions tab

# 5. Watch for the heartbeat log line every 15 minutes
# "[INFO] main: heartbeat | ..."  — if absent for > 30 min, the bot may be stalled
```

---

## 10. File Structure

```
trading-bot/
│
├── main.py                  Bot entry point — live/paper trading loop + CLI flags
├── strategy.py              Signal logic: evaluate_1h_live(), position_size_usdt()
├── trader.py                Order execution, equity fetch, daily P&L tracking
│
├── backtest.py              Vectorised backtesting engine
├── run_backtest.py          Single-call autonomous runner (WFO on by default)
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
├── Dockerfile               Multi-stage build (python:3.12.7-slim-bullseye)
├── docker-compose.yml       Named volumes, restart policy, log rotation
├── .dockerignore            Excludes .env, data/, __pycache__ from build context
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

## 11. Risk Warning

> **Trading futures with leverage involves substantial risk of loss.**  
> Past backtest results do not guarantee future performance.

- Always run in **paper trading mode first** (`PAPER_TRADING=true`) for at least 24–48 hours to verify your setup
- The 1H breakout strategy fires approximately **10–40 signals per year** — do not expect a trade every day
- With the default `EQUITY_PERCENT=35`: at normal ATR (~1.5%), each SL costs ~8% of balance; at ATR=3% (volatile periods), each SL can cost ~16% of balance
- The strategy's worst drawdown in 5 years was **−38% (WFO default)** or **−35% (classic)** — ensure you can withstand this before running live
- **Start with a small balance** (e.g., $200–500 USDT) and scale up only after validating live performance over several months
- **Never risk capital you cannot afford to lose**
