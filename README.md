# Multi-Asset 1H Breakout Futures Bot

Autonomous algorithmic trading bot for **Binance USDT-M Perpetual Futures**, built on a
**Feature-Based Architecture**: one shared **1H momentum-breakout engine** drives multiple
trading **domains** (`btc/`, `eth/`, `sol/`), each with its own tuned profile and per-asset
`ENABLED` / `TRADE_MODE` switches. **Walk-Forward Optimization (WFO) is on by default** for
the primary symbol, and a **Profit-Based STEP Trailing SL** locks in gains as a trade runs.

The flagship **BTCUSDT** profile is verified over **6 years** of live Binance USDT-M data
(Sep 2019 → May 2026). ETH/SOL are newer, in-sample profiles — see §1 and §2 for the
honest validation status before risking capital.

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

### Feature-Based Architecture

One **shared 1H breakout engine** is reused by per-asset **domains**. A domain is just a
folder with two files: a `config.py` (the tuned profile + on/off switches — the source of
truth) and a `processor.py` (a thin shell that binds the symbol to the shared engine — no
duplicated strategy logic).

```
src/core/
├── shared/         cross-asset infra (config, trader, order_manager, ws_client,
│                   notifier, data_store, indicators, state_manager)
├── strategy_1h/    the ONE shared 1H breakout engine (strategy, config_1h,
│                   asset_processor, walk_forward_optimizer, warm_start, regime_forecast)
├── btc/  config.py + processor.py   ← BTC domain  (proven flagship profile)
├── eth/  config.py + processor.py   ← ETH domain  (in-sample profile)
└── sol/  config.py + processor.py   ← SOL domain  (in-sample, marginal edge)
```

Each domain `config.py` is assembled into `config_1h.CONFIG_MATRIX`; at startup
`config.apply_symbol(sym)` writes that symbol's tuned knobs onto the engine globals.
**Edit a per-asset knob in its domain `config.py`, never in the matrix.**

#### Per-asset switches — `ENABLED` and `TRADE_MODE`

Every domain `config.py` carries two feature flags:

| Flag | Values | Effect |
|------|--------|--------|
| `ENABLED` | `True` / `False` | Master switch — does the bot spin up this asset's processor at all? Also gates inclusion in the multi-asset backtest. |
| `TRADE_MODE` | `"LIVE"` / `"EVAL_ONLY"` | `LIVE` places real orders (subject to `PAPER_TRADING`); `EVAL_ONLY` runs the full signal pipeline but only **logs** would-be entries (dry-run diagnostics). |

> **`TRADE_MODE` × `PAPER_TRADING` — read this before going live.** `TRADE_MODE="LIVE"`
> only places *real-money* orders when `PAPER_TRADING=false` in `.env`. With
> `PAPER_TRADING=true` (the default) a `LIVE` asset still simulates fills locally. The
> current shipped state is **all three symbols `ENABLED` + `LIVE`** (set during paper
> validation). Flipping `PAPER_TRADING=false` therefore arms BTC **and** ETH/SOL at once
> — if you only want the proven BTC profile trading real money, set ETH/SOL to
> `TRADE_MODE="EVAL_ONLY"` first. The multi-LIVE order path has been construction- and
> paper-validated but has **not** yet executed against a real exchange.

The primary symbol (`config.SYMBOL`, BTC) keeps the proven WFO-driven live loop; every
other `ENABLED` symbol is driven by an `AssetProcessor` on the **same** WebSocket socket
(multi-symbol fan-out). A central `OrderManager` arbitrates margin across LIVE sleeves so
the shared account is never oversubscribed.

### Profit-Based STEP Trailing SL

The production live exit logic is a **profit-locking STEP trail** that ratchets the stop
forward as a trade gains, locking in realized-favourable movement one ATR-step at a time:

```
favour = how far price has moved in your favour, measured in ATR
  favour < TRAIL_ACTIVATE_ATR×ATR   → stop stays at the initial SL
  favour ≥ TRAIL_ACTIVATE_ATR×ATR   → stop jumps to BREAK-EVEN
  each additional +1.0×ATR of favour → stop ratchets forward by +1.0×ATR locked profit
                                        (BE → +1ATR → +2ATR → …)
```

It is **stateless** (recomputed from entry, favour and ATR each tick — no extra position
fields) and **monotonic** (the stop only ever moves in the profitable direction). Live, it
is recomputed on every `@markPrice` tick (~1–3 s); in the backtest, intra-hour ratchets
are stepped on the high-resolution **5M** bars so a stop can fire mid-hour, not only on the
1H close. `STEP_TRAILING_ENABLED` is the master switch — **on** in live + the multi-asset
backtest, **off** for the single-asset `run_backtest.py` flagship story (which uses the
classic trail). See §8 for how to run each.

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

The bot uses **SL-distance-based risk sizing** as the production default. Every stop-loss hit always costs exactly `RISK_PERCENT`% of balance regardless of market volatility:

| Priority | Mode | Formula | Active when |
|----------|------|---------|------------|
| **0 (default)** | **Equity-percent** | `qty = (equity × EQUITY_PERCENT% × leverage) / entry` | `EQUITY_PERCENT > 0` |
| 1 | Fixed margin | `qty = (ORDER_BALANCE_USD × leverage) / entry` | `EQUITY_PERCENT=0`, `ORDER_BALANCE_USD > 0` |
| **2 (production)** | **Risk-percent** | `qty = (balance × RISK_PERCENT%) / (entry × sl_dist_pct)` | `EQUITY_PERCENT=0`, `ORDER_BALANCE_USD=0` |

`EQUITY_PERCENT=0` (the default) activates the risk-percent mode (Priority 2). `RISK_PERCENT`
is a **hardcoded literal in `strategy_1h/config_1h.py`, not an `.env` knob** — the shipped
value is **`4%`** (the capital-preservation default that pairs with the STEP trail). The §2
flagship tables were generated at the **max-growth `8%`** setting (reproduce with
`python backtesting/run_backtest.py --risk 8`); raise the literal to `8.0` only if you
deliberately want that higher-CAGR / higher-drawdown profile.

In **live mode**, equity = `totalWalletBalance + totalUnrealizedProfit` fetched from Binance at the moment of order placement.

---

## 2. Live Performance Verdict

> **Which numbers am I reading?** This section reports the **single-asset BTC flagship**
> backtest at the **max-growth `RISK_PERCENT=8%`** setting with the classic trail — this is
> the genuinely-validated 6-year story, reproduced by `python backtesting/run_backtest.py`.
> The **shipped live default is different and more conservative**: `config_1h.py` ships
> `RISK_PERCENT=4%` with the profit-locking **STEP** trail (a deliberate capital-preservation
> pivot). The **multi-asset portfolio** (BTC+ETH+SOL, `RISK=0.5%`/sleeve, STEP trail) is a
> separate, newer, **first-look** result — see the end of this section and treat it as a
> hypothesis, not a validated track record.

### BTC flagship — single-asset, RISK=8%, classic trail

**Data period:** Sep 2019 → May 2026 (6 years — full BTCUSDT perpetual futures history)  
**Starting balance:** $1,000 USDT  
**Leverage:** 10× | **TP:** 6.0× ATR | **SL:** 1.5× ATR | **ADX ≥ 20**

### Flagship results (`RISK_PERCENT=8%` max-growth setting — `run_backtest.py --risk 8`)

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
| RISK=8% *(max-growth)* | **+66.5%/yr** | **+72.7%/yr** | −53% |
| EQUITY=35% | +58.3%/yr | +68.6%/yr | −38% |

> EQUITY=35% has lower drawdown but significantly lower CAGR. RISK=8% is the proven maximum-growth configuration; the bot **ships the more conservative `RISK=4%`** (see §1) — set the literal to `8.0` in `config_1h.py` to reproduce these figures.

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

### Multi-asset portfolio — first-look (BTC+ETH+SOL, RISK=0.5%/sleeve, STEP trail)

Three independent $1,000 sleeves, each on its own domain profile, under the profit-locking
STEP trail, 5-year window (2021 → 2026). Reproduce with `python scripts/backtest_1h.py`
(figures as of 2026-06-08; the incremental data fetch rewrites the cache daily, so the
exact totals drift by a few dollars run-to-run):

| Asset | Trades | Win% | PF | MaxDD% | Net$ | TP/SL/BE |
|-------|-------:|-----:|---:|-------:|-----:|---------|
| BTC | 212 | 41.0 | 1.46 | −4.1 | +160.78 | 8/77/127 |
| ETH | 257 | 39.7 | 1.42 | −4.9 | +138.53 | 11/71/175 |
| SOL | 314 | 29.9 | 1.03 | −14.0 | −3.84 | 30/174/110 |
| **PORTFOLIO** | **783** | **36.1** | **1.23** | **−4.7** | **+295.46** | (start $3,000 → $3,295) |

> ⚠️ **This is a hypothesis, not a validated edge.** The STEP trail is **UNVALIDATED** on
> these params (the ETH/SOL grid sweep used a break-even-only trail; BTC was never swept).
> ETH/SOL profiles are **in-sample** grid optima pinned at the TP=6.0 grid ceiling (a
> boundary optimum — a classic overfitting tell), SOL's edge is **marginal** (PF ≈ 1.03,
> −14% DD), and the LIVE multi-asset order path has **never run against a real exchange**.
> Paper-validate before committing capital.

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
# Discord alerts — hourly "Entry funnel" status report on every 1H bar close,
# plus trade open/close notifications. Leave blank to disable (notifier no-ops).
# Create one in Discord: Channel → Edit → Integrations → Webhooks → New Webhook.
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/<id>/<token>

# Hard stop after 3 consecutive SL hits
MAX_CONSECUTIVE_LOSSES=3
POST_SL_COOLDOWN_1H=3
```

> **Hourly status report:** when `DISCORD_WEBHOOK_URL` is set, the bot posts the
> full Entry-funnel diagnostics (the same gate-by-gate breakdown the console
> logs print) to the channel on **every** 1H bar close — even on no-signal bars
> — inside a monospaced ` ```text ` block. The notifier is fail-safe: a Discord
> outage is logged and swallowed, never stalling the trading loop.

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

Configuration is split three ways after the feature-based refactor:

| Where | What lives here | Editable via `.env`? |
|-------|-----------------|----------------------|
| `.env` → `shared/config.py` | Server / secret / exchange + run-safety knobs (keys, leverage, daily limits, funding gate, WFO toggles, Discord) | ✅ yes — the table below |
| `strategy_1h/config_1h.py` | 1H **engine** tunables as hardcoded literals (`RISK_PERCENT`, `STEP_TRAILING_ENABLED`, sizing chain) | ❌ no — edit the literal |
| `btc/eth/sol/config.py` | **Per-asset** profile + `ENABLED` / `TRADE_MODE` (breakout / ADX / TP / SL / trail-activate) — the source of truth assembled into `CONFIG_MATRIX` | ❌ no — edit the domain file |

The `.env`-driven production settings (read by `shared/config.py`):

| Variable | Production `.env` | Effect |
|----------|------------------|--------|
| `LEVERAGE` | `10` | Futures leverage multiplier |
| `WFO_ENABLED` | `true` | Auto-tune BREAKOUT_PERIOD every 30 days (default: on) |
| `WFO_FAST_ENABLED` | `true` | Shrink WFO training window to 14 days when ATR spikes ≥ 2× mean — faster regime adaptation |
| `DAILY_PROFIT_TARGET_USD` | `110.0` | Stop taking new entries after this daily gain (resets at UTC midnight) |
| `DAILY_LOSS_LIMIT_USD` | `50.0` | Stop taking new entries after this daily loss (resets at UTC midnight) |
| `MAX_CONSECUTIVE_LOSSES` | `3` | Circuit breaker: N SLs in a row halts entries for the day |
| `FUNDING_RATE_MAX` | `0.001` | Skip new entries above 0.10%/8h funding rate |
| `INITIAL_COOLDOWN_BARS` | `0` | Suppress entries for first N 1H bars on fresh startup (0 = disabled; warm start already seeds indicators) |
| `DISCORD_WEBHOOK_URL` | _(blank)_ | Discord channel webhook for the hourly Entry-funnel report + trade alerts. Blank = notifications disabled. |
| `BOT_STATE_DB_PATH` | `bot_state.db` | SQLite state database path (auto-created) |

> **Sizing knobs are NOT here.** `EQUITY_PERCENT`, `RISK_PERCENT`, `STEP_TRAILING_ENABLED`
> and the per-trade sizing chain are **hardcoded literals in `strategy_1h/config_1h.py`**
> (shipped: `EQUITY_PERCENT=0`, `RISK_PERCENT=4` — every SL costs 4% of balance). Editing
> them in `.env` has **no effect**; change the literal. Per-asset breakout/ADX/TP/SL/trail
> live in the domain `config.py` files (see §1).

> **Circuit breaker note:** The daily loss/profit limits are fixed-USD values calibrated to a ~$1k–$2k starting balance. As your balance compounds, consider periodically adjusting these thresholds (or switching to `DAILY_LOSS_LIMIT_PCT` / `DAILY_PROFIT_TARGET_PCT` in `.env`) to maintain proportional risk control.

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
python scripts/verify_warmup.py
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
python backtesting/run_backtest.py

# Classic mode — fixed BREAKOUT_PERIOD=14, no WFO
python backtesting/run_backtest.py --no-wfo

# 5-year window
python backtesting/run_backtest.py --days 1825

# 3-year window (faster iteration during param tuning)
python backtesting/run_backtest.py --days 1095

# WFO + Markov regime forecast
python backtesting/run_backtest.py --forecast

# Run all feature combinations (benchmark sweep)
python backtesting/run_backtest.py --all

# Custom risk / TP parameters
python backtesting/run_backtest.py --risk 6 --tp 7.0 --adx 25

# Tighter goal criteria
python backtesting/run_backtest.py --min-cagr 30 --max-dd 60

# Allow 1 bad year out of 6 (year-fraction threshold = 83%)
python backtesting/run_backtest.py --year-frac 0.8

# Show all available flags
python backtesting/run_backtest.py --help
```

> `run_backtest.py` is the **single-asset BTC flagship** runner (WFO, RISK=8%, classic
> trail — the §2 story). For the **multi-asset STEP-trail portfolio**, use `backtest_1h.py`
> below.

### Multi-asset / portfolio backtest (`backtest_1h.py`)

The primary multi-asset backtest. Each selected symbol trades its **own domain profile**
(pulled from `btc/config.py` / `eth/config.py` / `sol/config.py` via `CONFIG_MATRIX`) as an
independent $1,000 sleeve at `RISK=0.5%`, under the profit-locking **STEP** trail, using the
high-resolution 5M bars for accurate intra-hour trailing. Pick any subset of symbols — a
single-symbol run is a true single-asset backtest of that domain's profile.

```bash
# All ENABLED symbols (BTC + ETH + SOL), 5-year window — per-symbol + portfolio table
python scripts/backtest_1h.py

# A single symbol (true single-asset run of that domain's profile)
python scripts/backtest_1h.py --symbols BTCUSDT

# An explicit subset / full portfolio
python scripts/backtest_1h.py --symbols BTCUSDT ETHUSDT SOLUSDT

# Shorter window (faster iteration)
python scripts/backtest_1h.py --days 1095

# Show all flags
python scripts/backtest_1h.py --help
```

You can also pin the default symbol set by editing the `SYMBOLS` variable at the top of
`scripts/backtest_1h.py` (`None` = every `ENABLED` asset in the `CONFIG_MATRIX`).

### Validate warmup before deploying config changes

```bash
# Run diagnostic to verify indicator seeding and no-lookahead guarantees
python scripts/verify_warmup.py
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
python scripts/sweep_assets.py

# Faster screen on a shorter window
python scripts/sweep_assets.py --days 730

# Specific pairs only
python scripts/sweep_assets.py --candidates ETHUSDT,SOLUSDT,AVAXUSDT

# Custom split / ruin floor
python scripts/sweep_assets.py --split 0.6 --ruin-floor -40

# Show all flags
python scripts/sweep_assets.py --help
```

> **Heads-up:** uses live Binance fetch + the `data/` cache. A cold full
> 5m+1h fetch for the whole pool is millions of bars — expect tens of minutes
> the first time; cached re-runs are fast. Default pool: `BTCUSDT, ETHUSDT,
> SOLUSDT, BNBUSDT, AVAXUSDT, LINKUSDT, NEARUSDT`.

Run the tool's unit tests (standalone, no pytest required):

```bash
python tests/test_sweep_assets.py
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
├── main.py                  Bot entry point — live/paper trading loop + CLI flags (root)
├── conftest.py              Pytest path bootstrap (mirrors the per-entry sys.path shim)
│
├── src/core/                ── Live runtime engine (feature-based) ──
│   │
│   ├── shared/              ── cross-asset infrastructure ──
│   │   ├── config.py            Server/secret/exchange + indicator base (env-driven; loads <root>/.env)
│   │   ├── trader.py            Per-symbol order execution, equity fetch, daily P&L, trailing
│   │   ├── order_manager.py     Cross-asset margin arbiter (global ledger for LIVE sleeves)
│   │   ├── notifier.py          Async Discord webhook client (hourly funnel report + trade alerts)
│   │   ├── ws_client.py         Binance WebSocket client — multi-symbol fan-out (5M + 1H + markPrice)
│   │   ├── data_store.py        Rolling OHLCV buffers for live candle state
│   │   ├── indicators.py        NumPy indicator library (EMA, RSI, ATR, ADX, BB)
│   │   ├── adaptive_regime.py   Hurst exponent + BBW + ADX composite regime scorer
│   │   └── state_manager.py     SQLite crash-recovery persistence
│   │
│   ├── strategy_1h/         ── the ONE shared 1H breakout engine ──
│   │   ├── config_1h.py         Hardcoded 1H tunables; assembles CONFIG_MATRIX from the domains
│   │   ├── strategy.py          Signal logic: evaluate_1h_live(), position_size_usdt()
│   │   ├── asset_processor.py   Per-symbol live processor (drives each ENABLED secondary)
│   │   ├── warm_start.py        Historical pre-loader + dry-run hydration
│   │   ├── walk_forward_optimizer.py  WFO engine — BREAKOUT_PERIOD self-tuning + dynamic lookback
│   │   └── regime_forecast.py   Markov regime forecaster (TREND/CHOPPY/QUIET)
│   │
│   ├── btc/                 ── BTC domain  (proven flagship profile) ──
│   │   ├── config.py            ENABLED / TRADE_MODE + tuned 1H params (SOURCE OF TRUTH)
│   │   └── processor.py         Thin Processor(AssetProcessor) shell ⇒ shared engine
│   ├── eth/  config.py + processor.py   ── ETH domain (in-sample profile) ──
│   └── sol/  config.py + processor.py   ── SOL domain (in-sample, marginal edge) ──
│
├── backtesting/             ── Validation engines ──
│   ├── backtest.py          Vectorised engine — run() (single) + run_portfolio() (multi-asset)
│   ├── run_backtest.py      Single-asset BTC flagship runner (WFO on, RISK=8%, 6-year default)
│   ├── compare_mtf.py       1H baseline vs MTF-stop A/B (rejected experiment; diagnostic)
│   ├── fetch_data.py        Historical data downloader (Binance REST API; caches to <root>/data)
│   └── visualize.py         Equity curve + trade marker charts
│
├── scripts/                 ── Analytical / diagnostic utilities ──
│   ├── backtest_1h.py           Primary multi/single-asset backtest (domain profiles, STEP trail)
│   ├── grid_trail_search.py     TRAIL grid search (PF+MAR growth lens)
│   ├── grid_trail_analysis.py   TRAIL grid (capital-preservation lens + RISK sweep)
│   ├── sweep_assets.py          Multi-asset ATR_RATIO out-of-sample sweep
│   ├── verify_warmup.py         Warmup diagnostic — indicator seeding & no-lookahead
│   └── probe_ws.py              Standalone Binance WS connectivity probe
│
├── tests/                   ── Parity & regression suites (NO pytest; standalone runners) ──
│   ├── test_trail_parity.py     Live/backtest trailing-stop parity (incl. STEP ladder)
│   ├── test_trail_dedupe.py     Live SL one-tick dedupe guard
│   ├── test_order_manager.py    Cross-asset margin arbitration (incl. concurrent gather)
│   ├── test_ws_fanout.py        Multi-symbol WebSocket fan-out routing
│   ├── test_signal_diagnostics.py  Entry-funnel diagnostic drift guard
│   └── test_sweep_assets.py     Unit tests for sweep_assets.py
│
├── Dockerfile               Multi-stage build (python:3.12.7-slim-bullseye); COPYs main.py + src/ + backtesting/
├── docker-compose.yml       Named volumes, restart policy, log rotation
├── .dockerignore            Excludes .env, data/, __pycache__ from build context
│
├── data/                    Per-symbol 1H + 5M OHLCV CSV cache (auto-fetched & updated)
│   ├── btcusdt_1h.csv / btcusdt_5m.csv      (Sep 2019 → present)
│   └── ethusdt_*.csv / solusdt_*.csv        (created on first multi-asset backtest)
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
- The shipped default `RISK_PERCENT=4%`: every stop-loss always costs exactly **4% of your balance**, regardless of volatility (at $1k that is $40 per SL). The §2 flagship tables use the max-growth `8%` setting (double the per-SL loss, higher CAGR and deeper drawdowns) — opt into it deliberately by raising the literal in `config_1h.py`.
- The strategy's worst drawdown in 6 years was **−53%** (both WFO and Classic). Ensure you can withstand this before running live.
- Year 1 of the backtest (Sep 2019 – Sep 2020) lost −38% — this reflects the COVID crash and early BTC futures market structure, not a repeating annual pattern.
- **Start with a small balance** (e.g., $200–500 USDT) and scale up only after validating live performance over several months
- **Never risk capital you cannot afford to lose**
