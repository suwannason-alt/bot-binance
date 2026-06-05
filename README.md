# BTCUSDT Futures Trading Bot

Autonomous algorithmic trading bot for **Binance USDT-M Perpetual Futures**.  
Strategy: **1H momentum breakout** with **Walk-Forward Optimization (WFO) on by default**.  
Verified over **6 years of live Binance USDT-M Futures data** (Sep 2019 → May 2026).

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
| **Position sizing** | `(balance × 8%) ÷ (entry × ATR_SL_dist)` — risk-constant sizing (see §2) |
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
  │    ├─ Selects best BREAKOUT_PERIOD from [7, 10, 14, 21, 28]
  │    └─ Dynamic lookback: shrinks window to 336 bars (14 days) when ATR ≥ 2× mean
  │         (faster adaptation at cold start or volatility regime shifts)
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
  4. Initial cooldown gate (if INITIAL_COOLDOWN_BARS > 0) → suppress entries while settling
  5. Pre-entry guards: daily_halted? session filter? consecutive SL? funding rate?
  6. Fetch total equity from Binance (wallet_balance + unrealized_pnl)
  7. Evaluate 1H breakout signal → place order (market or limit)
  8. Save state to SQLite (every 6 bars)
```

### Position Sizing — Risk-Constant Model

The bot uses **SL-distance-based risk sizing** as the production default. Every stop-loss hit always costs exactly 8% of balance regardless of market volatility:

| Priority | Mode | Formula | Active when |
|----------|------|---------|------------|
| **0 (default)** | **Equity-percent** | `qty = (equity × EQUITY_PERCENT% × leverage) / entry` | `EQUITY_PERCENT > 0` |
| 1 | Fixed margin | `qty = (ORDER_BALANCE_USD × leverage) / entry` | `EQUITY_PERCENT=0`, `ORDER_BALANCE_USD > 0` |
| **2 (production)** | **Risk-percent** | `qty = (balance × 8%) / (entry × sl_dist_pct)` | `EQUITY_PERCENT=0`, `ORDER_BALANCE_USD=0` |

**Production `.env` sets `EQUITY_PERCENT=0`**, activating the RISK_PERCENT=8% mode (Priority 2).  
This is the proven maximum-CAGR configuration — see §2 for the performance comparison.

In **live mode**, equity = `totalWalletBalance + totalUnrealizedProfit` fetched from Binance at the moment of order placement.

---

## 2. Live Performance Verdict

**Data period:** Sep 2019 → May 2026 (6 years — full BTCUSDT perpetual futures history)  
**Starting balance:** $1,000 USDT  
**Leverage:** 10× | **TP:** 6.0× ATR | **SL:** 1.5× ATR | **ADX ≥ 20**

### Production results (`EQUITY_PERCENT=0`, `RISK_PERCENT=8%` — current `.env`)

| Mode | Command | CAGR | Max DD | 6-yr Return | Verdict |
|------|---------|------|--------|-------------|---------|
| **WFO** *(default)* | `python main.py` | **+66.5%/yr** | −53% | **$1k → $21k** | ✅ **RECOMMENDED** |
| Classic | `python main.py --no-wfo` | **+72.7%/yr** | −53% | **$1k → $26k** | ✅ Higher CAGR |

> **Why WFO is recommended over classic:** WFO's value is anti-overfitting protection for live trading — it re-selects BREAKOUT_PERIOD every 30 days from live data, preventing parameter staleness when market regime shifts. Classic sometimes beats it in backtests because 14 bars happened to be optimal for specific historical windows. For production, WFO is safer.

### Sizing model comparison — why RISK_PERCENT=8% beats EQUITY_PERCENT=35%

Both modes target similar effective leverage at average ATR, but diverge in volatile periods:

| ATR at entry | EQUITY=35% per-SL loss | RISK=8% per-SL loss |
|---|---|---|
| 1.5% (normal) | **7.9%** | **8.0%** ← equivalent |
| 3.0% (volatile) | **15.7%** | **8.0%** |
| 5.0% (extreme) | **26.3%** | **8.0%** |

`RISK_PERCENT=8%` scales position size **inversely with ATR**, so every SL hit costs exactly 8% of balance. `EQUITY_PERCENT=35%` uses fixed leverage — larger ATR = larger loss per SL. This explains the CAGR gap.

| Sizing mode | CAGR (WFO) | CAGR (Classic) | Max DD |
|-------------|-----------|----------------|--------|
| RISK=8% *(production)* | **+66.5%/yr** | **+72.7%/yr** | −53% |
| EQUITY=35% | +58.3%/yr | +68.6%/yr | −38% |

> EQUITY=35% has lower drawdown but significantly lower CAGR. RISK=8% is the proven maximum-growth configuration and is set in production `.env`.

### Year-by-year breakdown (RISK=8%, WFO — production default)

| Year | Period | Start | End | Return | Trades | TP / SL / BE |
|------|--------|-------|-----|--------|--------|-------------|
| Y1 | Sep 2019 – Sep 2020 | $1,000 | $620 | **−38.0% ✗** | 24 | 3 / 14 / 7 |
| Y2 | Sep 2020 – Sep 2021 | $620 | $2,522 | **+306.8% ✓** | 25 | 8 / 8 / 9 |
| Y3 | Sep 2021 – Sep 2022 | $2,522 | $2,847 | **+12.9% ✓** | 21 | 4 / 10 / 7 |
| Y4 | Sep 2022 – Sep 2023 | $2,847 | $8,551 | **+200.3% ✓** | 19 | 7 / 8 / 4 |
| Y5 | Sep 2023 – Sep 2024 | $8,551 | $13,087 | **+53.1% ✓** | 39 | 6 / 11 / 22 |
| Y6 | Sep 2024 – May 2026 | $13,087 | $20,975 | **+60.3% ✓** | 30 | 7 / 13 / 10 |

**Total: 158 trades — Win rate 22.2% — Profit Factor 1.53 — Sharpe 0.82**

### Year-by-year breakdown (RISK=8%, Classic `--no-wfo`)

| Year | Period | Start | End | Return | Trades | TP / SL / BE |
|------|--------|-------|-----|--------|--------|-------------|
| Y1 | Sep 2019 – Sep 2020 | $1,000 | $620 | **−38.0% ✗** | 24 | 3 / 14 / 7 |
| Y2 | Sep 2020 – Sep 2021 | $620 | $2,522 | **+306.8% ✓** | 25 | 8 / 8 / 9 |
| Y3 | Sep 2021 – Sep 2022 | $2,522 | $3,107 | **+23.2% ✓** | 21 | 4 / 9 / 8 |
| Y4 | Sep 2022 – Sep 2023 | $3,107 | $9,331 | **+200.3% ✓** | 19 | 7 / 8 / 4 |
| Y5 | Sep 2023 – Sep 2024 | $9,331 | $18,805 | **+101.5% ✓** | 39 | 7 / 11 / 21 |
| Y6 | Sep 2024 – May 2026 | $18,805 | $26,155 | **+39.1% ✓** | 28 | 6 / 12 / 10 |

**Total: 156 trades — Win rate 22.4% — Profit Factor 1.56 — Sharpe 0.86**

### Year 1 context (Sep 2019 – Sep 2020)

Year 1 covers the **earliest available BTCUSDT perpetual futures data** — a period of flat price action followed by the COVID crash in March 2020 (BTC dropped ~54% in 48 hours). With only 3 winning trades out of 24 and heavy whipsaw, the −38% is historically specific to that regime. Years 2–6 recovered everything and compounded strongly.

**For live trading starting in 2026:** you will not be entering during this historical cold-start. The relevant reference years are Y5–Y6 (+53%/+60% WFO, +101%/+39% Classic).

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

| Variable | Production `.env` | Effect |
|----------|------------------|--------|
| `EQUITY_PERCENT` | `0` | **Must be 0** to activate RISK_PERCENT mode. Any value > 0 takes priority and disables risk-constant sizing. |
| `RISK_PERCENT` | `8.0` | % of balance risked per trade — active when `EQUITY_PERCENT=0`. Each SL costs exactly 8% regardless of ATR. |
| `LEVERAGE` | `10` | Futures leverage multiplier |
| `WFO_ENABLED` | `true` | Auto-tune BREAKOUT_PERIOD every 30 days (default: on) |
| `WFO_FAST_ENABLED` | `true` | Shrink WFO training window to 14 days when ATR spikes ≥ 2× mean — faster regime adaptation |
| `DAILY_PROFIT_TARGET_USD` | `110.0` | Stop taking new entries after this daily gain (resets at UTC midnight) |
| `DAILY_LOSS_LIMIT_USD` | `50.0` | Stop taking new entries after this daily loss (resets at UTC midnight) |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Circuit breaker: N SLs in a row halts entries for the day |
| `FUNDING_RATE_MAX` | `0.001` | Skip new entries above 0.10%/8h funding rate |
| `INITIAL_COOLDOWN_BARS` | `0` | Suppress entries for first N 1H bars on fresh startup (0 = disabled; warm start already seeds indicators) |
| `BOT_STATE_DB_PATH` | `bot_state.db` | SQLite state database path (auto-created) |

> **Circuit breaker note:** The daily loss/profit limits are fixed-USD values calibrated to a ~$1k–$2k starting balance. As your balance compounds, consider periodically adjusting these thresholds (or switching to `DAILY_LOSS_LIMIT_PCT` / `DAILY_PROFIT_TARGET_PCT` in `.env`) to maintain proportional risk control.

### Trading XAUUSDT (Gold) instead of BTC

The bot is symbol-agnostic — set `SYMBOL=XAUUSDT` and it trades the Binance USDT-M
gold perpetual end-to-end (data fetch, warm-start, WFO, live orders). A ready-made
template is in [`.env.xauusdt.example`](.env.xauusdt.example).

**Per-symbol exchange precision is automatic.** `config.py` carries a
`SYMBOL_PROFILES` map that supplies the correct lot/tick/notional rules per
contract, so you do **not** hand-set them:

| Contract | `PRICE_TICK` | Lot / step | Min notional |
|----------|-------------|------------|--------------|
| BTCUSDT  | `0.10`      | `0.001`    | `$5`         |
| XAUUSDT  | `0.01`      | `0.001`    | `$5`         |

> ⚠️ **Do not pin `PRICE_TICK` / `QTY_STEP` / `MIN_ORDER_QTY` in `.env` when
> switching symbols.** An explicit env value overrides the profile — leaving
> BTC's `PRICE_TICK=0.10` in place would get every gold order rejected (gold
> ticks at `0.01`). Remove those lines and let the profile resolve them. A new
> `MIN_NOTIONAL` guard (default `$5`) also skips orders too small for the
> contract — relevant for tiny accounts, since `0.001 oz × $4,400 ≈ $4.40 < $5`.

**Strategy parameters: gold runs an AGGRESSIVE, high-frequency profile.** Unlike
BTC's filters, the gold strategy params live in
`SYMBOL_PROFILES["XAUUSDT"]["strategy"]` and apply automatically when
`SYMBOL=XAUUSDT` (they are **not** read from `.env`). The active gold matrix:

| Param | BTC (default) | XAUUSDT (gold profile) |
|-------|:-------------:|:----------------------:|
| `ATR_TP_MULTIPLIER` | 6.0 | **8.0** |
| `ATR_SL_MULTIPLIER` | 1.5 | 1.5 |
| `ADX_MIN` | 20.0 | **25.0** |
| `ATR_RATIO_MIN` | 1.15 | **1.10** |
| `EMA_SLOPE_MIN_PCT` | 0.15 | **0.0** |

> ⚠️ **This profile is deliberately overfit.** XAUUSDT onboarded **2025-12-11**,
> so only ~6 months of history exist — one continuous bull run ($4,200 → $5,570),
> no bear/sustained-chop regime, almost no short-side data. A 70/30 train/test
> sweep leaves only 1–9 trades in the test window, so these params are fit to a
> single bull market and **may not generalize when gold ranges or falls**. They
> were chosen for **maximum absolute return and trade frequency**, accepting a
> deeper drawdown. WFO still adapts `BREAKOUT_PERIOD` live. Re-evaluate after gold
> sees a full down-cycle; to revert to the conservative profile, edit the
> `strategy` block in `config.py`.

Full-window WFO backtest on all ~6 months of gold, production sizing
(`EQUITY_PERCENT=0`, `RISK_PERCENT=8%`):

| Profile | Return (6 mo) | PF | MaxDD | Trades | Status |
|---------|--------------:|---:|------:|-------:|--------|
| **Aggressive** (`ATR_RATIO_MIN=1.10`, `EMA_SLOPE_MIN_PCT=0`, `ADX≥25`, `TP=8`) | **+48%** | 1.46 | **−37%** | 28 | **Active gold default** — max return / frequency. |
| Conservative (`ATR_RATIO_MIN=1.15`, `EMA_SLOPE_MIN_PCT=0.15`, `TP=6`) | +39% | 2.10 | −18% | 9 | Best risk-adjusted; safer fallback. |

```bash
# Validate on the gold feed before going live
python run_backtest.py --symbol XAUUSDT --days 176     # full available history
SYMBOL=XAUUSDT python main.py                          # paper/live per PAPER_TRADING
```

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

### Indicator warmup diagnostics

Run `verify_warmup.py` to validate that all indicators are properly seeded before deployment:

```bash
python verify_warmup.py
```

This diagnostic checks:
- Data sufficiency (≥ 3,030 1H bars available)
- EMA200 convergence at the first execution bar
- ADX full convergence (Wilder smoothing saturates within the warmup window)
- No-lookahead guarantee (each indicator recomputed on prefix-only slices)
- WFO cold-start timing (first retune window coverage)

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

> **Data coverage note:** BTCUSDT perpetual futures launched September 2019. The maximum available history is ~6.7 years (Sep 2019 → present). The default backtest window is **6 years** (`YEARS=6`, `DAYS=2190`). Requesting more than 7 years creates phantom years with no data — avoid.

```bash
# Default: 6-year WFO backtest (matches live production config, max available data)
python run_backtest.py

# Classic mode — fixed BREAKOUT_PERIOD=14, no WFO
python run_backtest.py --no-wfo

# 5-year window
python run_backtest.py --days 1825

# 3-year window (faster iteration during param tuning)
python run_backtest.py --days 1095

# WFO + Markov regime forecast
python run_backtest.py --forecast

# Run all feature combinations (benchmark sweep)
python run_backtest.py --all

# Custom risk / TP parameters
python run_backtest.py --risk 6 --tp 7.0 --adx 25

# Tighter goal criteria
python run_backtest.py --min-cagr 30 --max-dd 60

# Allow 1 bad year out of 6 (year-fraction threshold = 83%)
python run_backtest.py --year-frac 0.8

# Show all available flags
python run_backtest.py --help
```

### Validate warmup before deploying config changes

```bash
# Run diagnostic to verify indicator seeding and no-lookahead guarantees
python verify_warmup.py
```

### Multi-asset ATR_RATIO sweep (`sweep_assets.py`)

Offline research tool that screens crypto perpetuals for compatibility with the
loose `ATR_RATIO_MIN = 1.10` volatility gate. The `1.10` gate yields higher CAGR
than the proven `1.15` but ruins BTC on choppy years; this sweep finds assets
where it survives — using a method that does **not** manufacture false winners
by picking the best in-sample backtest.

For each candidate it splits history **70/30 by time** and runs a 2×2 matrix
(`{1.10, 1.15} × {train, test}`) with `RISK=8%`, `TP=6.0`, `SL=1.5`, `BREAKOUT=14`,
**WFO off** (to isolate `ATR_RATIO`). The verdict uses the **test** window only:

- **PASS** — `1.10` test profit factor ≥ the `1.15` baseline, and MaxDD stays
  above the ruin floor.
- **FAIL** — `1.10` degrades vs `1.15` out-of-sample.
- **RUIN** — `1.10` test MaxDD breaches the floor (default −50%); overrides all.
- **NODATA** — a window produced no trades; row renders with `—` and sorts last.

The `Decay` column (`PF1.10test − PF1.10train`) exposes overfitting: a large
negative value means the gate looked strong in-sample but fell apart
out-of-sample. Sorted by ΔPF, then MaxDD. Console-only output, no log files.

```bash
# Full 5-year sweep across the default candidate pool (BTC included as control)
python sweep_assets.py

# Faster screen on a shorter window
python sweep_assets.py --days 730

# Specific pairs only
python sweep_assets.py --candidates ETHUSDT,SOLUSDT,AVAXUSDT

# Custom split / ruin floor
python sweep_assets.py --split 0.6 --ruin-floor -40

# Show all flags
python sweep_assets.py --help
```

> **Heads-up:** uses live Binance fetch + the `data/` cache. A cold full
> 5m+1h fetch for the whole pool is millions of bars — expect tens of minutes
> the first time; cached re-runs are fast. Default pool: `BTCUSDT, ETHUSDT,
> SOLUSDT, BNBUSDT, AVAXUSDT, LINKUSDT, NEARUSDT`.

Run the tool's unit tests (standalone, no pytest required):

```bash
python test_sweep_assets.py
```

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

> **Scaling circuit breakers:** The fixed-USD daily limits ($50/$110) are calibrated for a ~$1k–$2k starting balance. As your balance grows, update these to stay proportional — or use the percentage-based alternatives (`DAILY_LOSS_LIMIT_PCT`, `DAILY_PROFIT_TARGET_PCT`) in `.env`.

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
├── run_backtest.py          Single-call autonomous runner (WFO on, 6-year default)
├── sweep_assets.py          Multi-asset ATR_RATIO 1.10-vs-1.15 out-of-sample sweep
├── test_sweep_assets.py     Standalone unit tests for sweep_assets.py (no pytest)
├── visualize.py             Equity curve + trade marker charts
├── verify_warmup.py         Warmup diagnostic — checks indicator seeding & no-lookahead
│
├── config.py                All strategy & risk parameters (env-driven)
├── indicators.py            NumPy indicator library (EMA, RSI, ATR, ADX, BB)
├── adaptive_regime.py       Hurst exponent + BBW + ADX composite regime scorer
│
├── walk_forward_optimizer.py  WFO engine — BREAKOUT_PERIOD self-tuning + dynamic lookback
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
│   ├── btcusdt_1h.csv       Cached 1H OHLCV (Sep 2019 → present, auto-updated)
│   └── btcusdt_5m.csv       Cached 5M OHLCV (Sep 2019 → present, auto-updated)
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
- The 1H breakout strategy fires approximately **15–40 signals per year** — do not expect a trade every day
- With `RISK_PERCENT=8%` (production default): every stop-loss always costs exactly **8% of your balance**, regardless of volatility. At $1k balance that is $80 per SL; at $10k balance it is $800 per SL.
- The strategy's worst drawdown in 6 years was **−53%** (both WFO and Classic). Ensure you can withstand this before running live.
- Year 1 of the backtest (Sep 2019 – Sep 2020) lost −38% — this reflects the COVID crash and early BTC futures market structure, not a repeating annual pattern.
- **Start with a small balance** (e.g., $200–500 USDT) and scale up only after validating live performance over several months
- **Never risk capital you cannot afford to lose**
