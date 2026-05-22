# BTC/USDT Futures Trading Bot

Algorithmic trading bot for Binance USDT-M Futures.  
Strategy: **1H momentum breakout** with a fixed **$20/day profit target**.

---

## Strategy Overview

The bot trades BTC/USDT perpetual futures on Binance using a 1-hour breakout system that stops trading each day once the net profit reaches $20 or the loss limit is hit.

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Timeframe | 1 Hour | ATR is ~5× larger than 5M → fees are only ~9% of risk vs ~33% on 5M |
| Entry | Close breaks above N-bar high (LONG) or below N-bar low (SHORT) | Momentum breakout |
| Trend filter | Price above/below EMA200 | Macro regime alignment |
| Momentum filter | EMA20 > EMA50 (≥0.1% separation) | Confirms trend direction |
| RSI filter | 45–78 for LONG, 22–55 for SHORT | Avoids overbought/oversold entries |
| Volume filter | Current volume ≥ 0.3× 20-bar average | Avoids low-conviction moves |
| Stop Loss | 2× ATR below/above entry | Sized to ATR to adapt to volatility |
| Take Profit | 4× ATR above/below entry | 2:1 R:R — break-even at 37.5% WR |
| **Risk per trade** | **2.5% of balance** | Scales with balance for compounding growth |
| Leverage | 5× | Achieves required notional without over-leveraging |
| **Daily profit target** | **$20 USD** | Stop trading once day PnL ≥ +$20 |
| **Daily loss limit** | **$25 USD** | Stop trading once day PnL ≤ −$25 (~1 SL at $1000 balance) |

### Fee Math

| Item | Rate | Impact at $1000 balance (2.5% risk = $25/trade) |
|------|------|------------------------------------------------|
| Commission | 0.05% per fill (taker) | ~$0.71 entry + $0.71 exit |
| Slippage | 0.02% per fill | ~$0.29 each side |
| **Round-trip cost** | **~0.14%** | ~$1.75 total |
| Net TP win | $25 × 2 − $1.75 | **≈ $48.25** (> $20 target ✓) |
| Net SL loss | −$25 − $0.89 | **≈ −$25.89** |

### Backtest Results — 1 Year (May 2025 → May 2026, $1,000 start)

**Best config: RISK_PCT=2.5%, DAILY_PROFIT_TARGET=$20, DAILY_LOSS_LIMIT=$25, leverage=5×**

| Metric | Value |
|--------|-------|
| **Total Return** | **+65.7%** ($1,000 → $1,657) |
| **Win Rate** | **43.0%** (TP=68, SL=90) |
| **Max Drawdown (MDD)** | **−29.2%** |
| **Total Trades** | **158** |
| Sharpe Ratio | 1.10 |
| Profit Factor | 1.24 |
| CAGR | +66.0% |
| Avg Win | +$71.74 |
| Avg Loss | −$43.81 |
| **Days hitting $20 target** | **68/358 (19.0%)** |
| Days hitting −$25 loss limit | 90/358 |
| Days with no signal | 200/358 |

> **Why percentage risk outperforms fixed-dollar:** With RISK_PCT=2.5%, each TP win grows as the account grows — by the time balance reaches $1,200, TP wins are ~$58 while the daily target stays fixed at $20. The first profitable trade each day still triggers the halt, but the gross win per profit day scales with the compounding balance, boosting total return.

> **Mathematical ceiling on daily $20 profit:** Signals fire on ~48% of days. With 43% WR, ~19% of days reach the $20 target. This is the realistic ceiling for this strategy structure.

---

## Requirements

- Python 3.11+
- Binance Futures account (or use paper trading mode)

---

## Installation

```bash
git clone <repo>
cd trading-bot
pip install -r requirements.txt
```

---

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

**`.env` file:**

```env
# Binance API credentials (leave empty for paper trading)
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# Set to "false" to enable real orders
PAPER_TRADING=true

# Symbol
SYMBOL=BTCUSDT

# Leverage (3x–5x recommended)
LEVERAGE=5
```

**Key parameters in `config.py`:**

```python
# Risk per trade — percentage scales with growing balance (best for compounding)
RISK_PERCENT = 2.5        # 2.5% of balance per trade ($25 at $1000, grows as account does)
RISK_USD     = 0.0        # set > 0 to use fixed-dollar risk instead (e.g. 20.0)

# Daily limits — BOTH trigger immediate halt for the day
DAILY_PROFIT_TARGET_USD = 20.0  # stop when day PnL >= +$20
DAILY_LOSS_LIMIT_USD    = 25.0  # stop when day PnL <= -$25

# Strategy (proven best from backtest)
ATR_SL_MULTIPLIER = 2.0   # SL = 2× ATR
ATR_TP_MULTIPLIER = 4.0   # TP = 4× ATR (2:1 R:R)
BREAKOUT_PERIOD   = 7     # rolling 7-bar high/low window
LEVERAGE          = 5
```

---

## How to Run the Backtest

### Step 1 — Fetch historical data

```bash
python fetch_data.py
```

Downloads 1-year of BTCUSDT klines (5M + 1H) from Binance into `data/`.

### Step 2 — Run the parameter sweep

```bash
python run_backtest.py
```

Runs 12 configurations and prints a ranked results table. All output is also saved to `backtest_results/backtest_log.txt`.

**Custom options:**

```bash
# Run with specific parameters
python run_backtest.py --days 365 --balance 1000

# Shorter period for quick testing
python run_backtest.py --days 90 --balance 1000
```

**Sample output:**

```
Run #02   2026-05-21 09:02:09
  Params : RISK_USD=20.0  DAILY_PROFIT_TARGET_USD=20.0  DAILY_LOSS_LIMIT_USD=20.0
  Trades       :      158
  Win Rate     :    43.0%  (TP=68 SL=90)
  Days profit  :   68/358  (19.0%)  [loss-limit days: 90]
  End Balance  : $  1,494.53
  Total Return : +   49.45%
  Max Drawdown :    -15.48%
  Sharpe Ratio :      1.35
  Profit Factor:      1.33
```

### Step 3 — Visualize results

```bash
python visualize.py
```

Opens interactive charts: equity curve, daily PnL histogram, trade entry/exit markers.

---

## How to Run the Live Bot

### Paper trading (no real money)

```bash
python main.py
```

Paper trading is the default (`PAPER_TRADING=true` in `.env`). Connects to Binance WebSocket, receives real market data, simulates fills locally.

### Live trading (real orders on Binance Futures)

1. Set `PAPER_TRADING=false` in your `.env`
2. Add your Binance API key and secret with Futures trading enabled
3. Ensure you have USDT in your Futures wallet (minimum $100 recommended)
4. Run:

```bash
python main.py
```

### What the bot does

**Each hour (on 1H candle close):**
1. Check if UTC date changed → reset daily tracking if so
2. Skip if `daily_halted` (profit target or loss limit already reached today)
3. Evaluate 1H breakout signal with all filters
4. If signal fires and no open position: place market entry + SL + TP

**On every mark-price tick (paper mode):**
- Check if SL or TP is hit → close position and update daily PnL
- If `day_pnl >= +$20`: set `daily_halted = True`, log "Daily PROFIT target reached"
- If `day_pnl <= -$20`: set `daily_halted = True`, log "Daily LOSS limit reached"

**Example log output:**
```
2026-05-21 14:00:00 [INFO] main: Starting trading bot [PAPER] symbol=BTCUSDT
2026-05-21 14:00:00 [INFO] main: Strategy: 1H breakout  RISK_PCT=2.5%  Leverage=5x  SL=2.0×ATR  TP=4.0×ATR
2026-05-21 14:00:00 [INFO] main: Daily targets: profit=$20  loss_limit=$25  BP=7bars  cooldown=1bars

2026-05-21 15:00:00 [INFO] main: SIGNAL  LONG | ema200=above ema_sep=0.45% rsi=61.2 bo_lvl=104800.00 atr%=0.92 rr=2.0
2026-05-21 15:00:00 [INFO] main:   Entry=104900.00  SL=103000.00 (1.81%)  TP=108700.00 (3.62%)  RR=2.0
2026-05-21 15:00:00 [INFO] trader: [PAPER] Opening LONG 0.000952 BTCUSDT @ 104900.00 | RISK_USD=$20

2026-05-21 17:23:00 [INFO] trader: [PAPER] Position closed via TP PnL=+38.42 USDT  Balance=1038.42  Day PnL=+38.42
2026-05-21 17:23:00 [INFO] trader: Daily PROFIT target reached: PnL=+38.42 >= 20.00  No more trades today.

2026-05-22 00:00:00 [INFO] trader: New day 2026-05-22 — balance=1038.42  all-time profit_days=1  loss_days=0
```

---

## File Structure

```
trading-bot/
├── main.py           — bot entry point (live/paper trading)
├── strategy.py       — signal logic: evaluate_1h_live(), evaluate_1h_signal()
├── trader.py         — order execution + daily P&L tracking + daily halt logic
├── backtest.py       — vectorized backtesting engine
├── run_backtest.py   — 12-config parameter sweep + results table
├── visualize.py      — equity curve + trade charts
├── config.py         — all strategy & risk parameters
├── indicators.py     — NumPy indicator library (EMA, RSI, MACD, ATR, BB)
├── data_store.py     — rolling OHLCV buffers for live candle state
├── ws_client.py      — Binance WebSocket client (5M + 1H klines + mark price)
├── fetch_data.py     — historical data downloader (Binance REST API)
├── data/             — CSV data files (btcusdt_1h.csv, btcusdt_5m.csv)
└── backtest_results/ — backtest output (backtest_log.txt, report.png)
```

---

## Risk Warning

This bot trades real money in live mode. Past backtest performance does not guarantee future results.

- Start with paper trading to verify your setup
- The 1H breakout strategy fires on ~48% of trading days — do not expect a trade every day
- On days with a signal, the $20 daily target is hit ~43% of the time (profit days)
- On ~43% of signal days you hit the −$20 loss limit and stop for the day
- Over 1 year: expect ~68 profit days and ~90 loss days — with 2.5% percentage risk, wins compound as balance grows, producing +65.7% total return vs fixed-dollar sizing
- Never risk capital you cannot afford to lose
