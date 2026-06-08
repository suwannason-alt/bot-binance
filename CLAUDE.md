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

All commands run **from the repo root**. The project uses a modular layout
(`src/core/`, `backtesting/`, `scripts/`, `tests/`) but keeps **flat absolute
imports** (`import config`, `import backtest`): every entry point carries a small
`sys.path` bootstrap header that adds those dirs to the path, so no `-m`/package
syntax is needed and the run commands below are just plain `python <path>`.

```bash
# Run the bot (WFO on by default; never starts cold — warm-starts ~3030 1H bars)
python main.py                 # live/paper per .env PAPER_TRADING  (stays at root)
python main.py --no-wfo        # classic fixed BREAKOUT_PERIOD=14
python main.py --forecast      # + Markov regime gate
python main.py --help

# Single-asset WFO backtest — the BTC flagship story (RISK=8%, 6-year default & max useful)
python backtesting/run_backtest.py
python backtesting/run_backtest.py --no-wfo --days 1825 --risk 6 --tp 7.0 --adx 25
python backtesting/run_backtest.py --all   # benchmark sweep of feature combos

# Primary multi-asset backtest — per-symbol domain profiles, STEP trail, RISK=0.5%/sleeve
python scripts/backtest_1h.py                        # all ENABLED symbols (BTC/ETH/SOL), 5yr
python scripts/backtest_1h.py --symbols BTCUSDT      # single symbol
python scripts/backtest_1h.py --symbols BTCUSDT ETHUSDT SOLUSDT --days 1095

# Diagnostics / research
python scripts/verify_warmup.py    # indicator seeding + no-lookahead checks (run before deploying config changes)
python scripts/sweep_assets.py     # multi-asset ATR_RATIO 1.10-vs-1.15 out-of-sample screen
python backtesting/compare_mtf.py --no-caps  # 1H baseline vs MTF-stop A/B (rejected; diagnostic)

# Tests — NO pytest. Each file is a standalone runner; exits non-zero on failure.
python tests/test_sweep_assets.py
python tests/test_signal_diagnostics.py
python tests/test_trail_parity.py     # live/backtest sync contract (run after strategy edits)
python tests/test_trail_dedupe.py
```

There is no separate lint/build step. Run a single test by editing the file's
`main()` or calling the `test_*` function directly via `python -c`. A root
`conftest.py` mirrors the path bootstrap so bare `pytest` also works if installed.

## Repository layout

```
main.py                 entry point (root); .env, CLAUDE.md, README.md, Dockerfile here
conftest.py             pytest path bootstrap (mirrors the per-entry shim)
src/core/shared/        cross-asset infra: config (server/secrets/exchange/indicator base),
                        trader, order_manager (cross-asset margin arbiter), notifier,
                        data_store, ws_client (multi-symbol fan-out), indicators,
                        adaptive_regime, state_manager
src/core/strategy_1h/   shared 1H momentum-breakout ENGINE (the ONLY strategy): config_1h
                        (assembles CONFIG_MATRIX), strategy, asset_processor (per-symbol
                        live processor), warm_start, walk_forward_optimizer, regime_forecast
src/core/btc|eth|sol/   feature-based trading DOMAINS: config.py (ENABLED/TRADE_MODE + tuned
                        1H params — the source of truth) + processor.py (thin shell ⇒ shared
                        engine). Imported package-qualified (`from btc import config`).
backtesting/            backtest (+run_portfolio), run_backtest, compare_mtf, fetch_data, visualize
scripts/                backtest_1h (primary multi/single-asset backtest, STEP trail),
                        grid_trail_search, grid_trail_analysis, sweep_assets,
                        verify_warmup, probe_ws
tests/                  test_trail_parity, test_trail_dedupe, test_order_manager,
                        test_ws_fanout, test_signal_diagnostics, test_sweep_assets
```

**Config.** Only **server/secret/exchange** values live in `.env` (read by shared
`config.py`). 1H strategy tunables are **hardcoded literals** in `config_1h.py`
(`from config import *` inherits the shared base). **Per-asset profiles are the source
of truth in the domain configs** (`btc/config.py`, `eth/config.py`, `sol/config.py` —
each holds `ENABLED` / `TRADE_MODE` + tuned breakout/ADX/TP/SL/trail); `config_1h`
ASSEMBLES `CONFIG_MATRIX` from them, and `config.apply_symbol(sym)` writes a symbol's
tuned knobs onto the flat globals. Edit a per-asset knob in its domain config, not in
the matrix. CLI/WFO overrides in `run_backtest`/`compare_mtf` write to `config_1h`
(the module `backtest.py` reads) — keep writer and reader the same module or parity breaks.

**Imports stay flat** (`import strategy`, not `from src.core.strategy_1h import
strategy`); the trading DOMAINS are the one exception — imported package-qualified
(`from btc import config`, `from btc.processor import Processor`) so the per-domain
`config.py`/`processor.py` never collide with the flat `import config`. Files run
directly (`main.py`, `backtesting/*`, `scripts/*`, `tests/*`) carry a `sys.path` header
that adds `{root, src/core, src/core/shared, src/core/strategy_1h, backtesting, scripts}`
to the path (`src/core` makes the domains importable as packages). Library modules
(everything under `src/core/`, plus backtest/fetch_data/visualize) need no header —
they're imported only after an entry point sets the path. When adding a new runnable
script, copy the header from an
existing one (all share the identical 6-dir tuple).

**`__file__`-relative paths are anchored to the repo root**, not the module's dir:
`shared/config.py` (now three levels deep → `parents[3]`) loads `<root>/.env` and
defaults the state DB to `<root>/bot_state.db`; `fetch_data.py` points `DATA_DIR` at
`<root>/data`. Keep this when moving files — a bare `dirname(__file__)` would break
`.env`/`data/`/DB discovery from subdirs.

The Dockerfile copies only `main.py` + `src/` + `backtesting/` (the runtime import
closure); `tests/` and `scripts/` are intentionally excluded from the image.

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

- **Strategy tunables live in `strategy_1h/config_1h.py`** as hardcoded literals
  (per-asset overrides in `CONFIG_MATRIX`). Only server/secret/exchange values are
  env-driven, in shared `shared/config.py`. Add a new 1H knob to `config_1h.py`, not
  to `.env` or `config.py`.
- **Position sizing has a priority chain** (`strategy.position_size_usdt`,
  mirrored in `backtest._position_qty`): `EQUITY_PERCENT>0` → fixed
  `ORDER_BALANCE_USD` → `RISK_PERCENT` (live 1H profile: `EQUITY_PERCENT=0`
  activates `RISK_PERCENT=4`, so every SL costs 4% of balance). Changing the chain
  requires editing both functions.
- **Console output only — no text log files.** Print backtest/diagnostic results
  to stdout.
- BTCUSDT perpetual futures history starts Sep 2019 (~6.7yr max). Backtests beyond
  ~7 years create phantom empty years.
- `docs/superpowers/` holds design specs and plans for in-flight work.
