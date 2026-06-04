# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Autonomous algorithmic trading bot for Binance USDT-M Perpetual Futures. Single
strategy: **1H momentum breakout** (close breaks N-bar rolling high/low, gated by
EMA200 trend, EMA20/50 momentum, ADX, ATR-expansion, and volume filters), with
Walk-Forward Optimization on by default. The README is the authoritative spec for
strategy params, performance numbers, deployment, and safety — read it for
anything user-facing. This file covers code structure and dev workflow.

## Commands

```bash
# Run the bot (WFO on by default; never starts cold — warm-starts ~3030 1H bars)
python main.py                 # live/paper per .env PAPER_TRADING
python main.py --no-wfo        # classic fixed BREAKOUT_PERIOD=14
python main.py --forecast      # + Markov regime gate
python main.py --help

# Backtest (uses cached CSVs in data/; 6-year window is the default & max useful)
python run_backtest.py
python run_backtest.py --no-wfo --days 1825 --risk 6 --tp 7.0 --adx 25
python run_backtest.py --all   # benchmark sweep of feature combos

# Diagnostics / research
python verify_warmup.py        # indicator seeding + no-lookahead checks (run before deploying config changes)
python sweep_assets.py         # multi-asset ATR_RATIO 1.10-vs-1.15 out-of-sample screen

# Tests — NO pytest. Each file is a standalone runner; exits non-zero on failure.
python test_sweep_assets.py
python test_signal_diagnostics.py
```

There is no separate lint/build step. Run a single test by editing the file's
`main()` or calling the `test_*` function directly via `python -c`.

## Architecture

Two parallel execution paths share the same strategy logic but have **separate
signal entry points** — keep them in sync when changing strategy behavior:

- **Live/paper** (`main.py` → `strategy.evaluate_1h_live`) — event-driven off
  WebSocket bar closes.
- **Backtest** (`run_backtest.py` → `backtest.run`) — vectorized loop over CSV
  bars. `backtest.py` reimplements sizing, trailing, daily limits, and fills
  rather than importing the live trader. A strategy change must be mirrored in
  both `strategy.py` and `backtest.py` or live and backtest will diverge.

Live data flow:
```
ws_client.BinanceWS (5M + 1H + markPrice streams)
  → data_store.MarketState / CandleBuffer (rolling OHLCV, MAX_CANDLES=600)
  → main.on_1h_close()  [the live loop — see README §1 for the per-bar sequence]
       ├─ regime_forecast.MarkovRegimeForecaster (optional entry gate)
       ├─ walk_forward_optimizer.WalkForwardOptimizer (retune BREAKOUT_PERIOD)
       ├─ strategy.evaluate_1h_live() → Signal
       ├─ trader.Trader (order placement, equity fetch, daily P&L)
       └─ state_manager.StateManager (SQLite, saves every 6 bars)
```

Startup is always warm: `warm_start.WarmStart` fetches ~3030 1H bars, dry-runs the
WFO + forecaster to hydrate them, and seeds the candle buffer so indicators are
warm on tick #1. On restart within 48h it takes the fast SQLite recovery path.

`indicators.py` is the shared NumPy indicator library (EMA/RSI/ATR/ADX/BB) used by
both paths. `adaptive_regime.py` and `regime_forecast.py` are optional regime
layers; per the project audit, quant add-ons (DynTP, regime/vol sizing) stay OFF
for max growth.

## Key conventions

- **All tunables live in `config.py`** and are env-overridable (`.env`). Do not
  hardcode strategy params elsewhere; add a `config` constant + env read.
- **Position sizing has a priority chain** (`strategy.position_size_usdt`,
  mirrored in `backtest._position_qty`): `EQUITY_PERCENT>0` → fixed
  `ORDER_BALANCE_USD` → `RISK_PERCENT` (production: `EQUITY_PERCENT=0` activates
  RISK_PERCENT=8%, so every SL costs exactly 8% of balance). Changing the chain
  requires editing both functions.
- **Console output only — no text log files.** Print backtest/diagnostic results
  to stdout.
- BTCUSDT perpetual futures history starts Sep 2019 (~6.7yr max). Backtests beyond
  ~7 years create phantom empty years.
- `docs/superpowers/` holds design specs and plans for in-flight work.
