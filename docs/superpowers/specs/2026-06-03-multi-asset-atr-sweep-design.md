# Multi-Asset ATR_RATIO Sweep — Design Spec

**Date:** 2026-06-03
**Status:** Approved for planning
**Author:** Quant session (brainstorming)

---

## 1. Problem & Goal

`ATR_RATIO_MIN = 1.10` (a looser volatility-expansion gate than the proven
`1.15`) produces superior CAGR in isolation but fails catastrophically on
`BTCUSDT` — its reflexive, mean-reverting microstructure turns the extra
entries into stop-hunted fakeouts, driving ruin-level (~−70%) drawdown in
certain years.

**Goal:** Identify which Binance Futures perpetual pairs — if any — have the
trend-persistence to run the loose `1.10` gate *without* the BTC failure mode,
using a validation method that does **not** manufacture false winners through
in-sample asset-picking.

This spec covers **only** the offline research tool (`sweep_assets.py`) and its
methodology. Live multi-asset deployment is explicitly a **non-goal** here (see
§8) and would be a separate project.

---

## 2. Structural Finding (the methodology constraint)

The naive approach — "run `1.10` on 7 assets, deploy the top 2-3 by historical
Profit Factor / MaxDD" — is **selection-on-in-sample-backtest**. With 7
candidates and one in-sample window, multiple-comparisons statistics
*guarantee* apparent winners by chance alone. This is the same class of error
recorded in the project's prior quant audit, where in-sample-attractive
features (DynTP, regime filter) proved neutral-to-harmful under scrutiny.

Two guardrails are therefore **mandatory** in the tool, not optional polish:

1. **Out-of-sample evaluation.** Every reported qualification metric is
   computed on a held-out *test* window the selection never touched.
2. **Per-asset `1.10`-vs-`1.15` head-to-head.** Each asset is its own control.
   The **delta** between `1.10` and `1.15` is the signal — not `1.10`'s
   absolute number. Without the baseline, "safe for 1.10" is unfalsifiable: it
   cannot be distinguished from "this asset trends well at any setting."

---

## 3. Architecture

**Standalone script `sweep_assets.py`, import-based (not subprocess).**

The script imports the existing modules directly and drives them in-process:

```
sweep_assets.py
  ├─ fetch_data.fetch_all(symbol, days)   → (df_5m, df_1h)   [cached to data/]
  ├─ config.<PARAM> = ...                 → set per-run knobs in-process
  └─ backtest.run(df_5m, df_1h, ...)      → BacktestResult
                                            └─ .stats : dict
```

`BacktestResult.stats` (backtest.py:1240-1261) already exposes every metric we
need — `profit_factor`, `max_drawdown_pct`, `cagr_pct`, `win_rate`,
`gross_profit`, `gross_loss`, trade counts. **No refactor of `backtest.py`,
`run_backtest.py`, or `main()` is required.** The subprocess alternative was
rejected: `main()` returns only a `bool` plus console text, forcing brittle
stdout parsing.

**ATR_RATIO override.** No `--atr-ratio` CLI flag exists today. The sweep sets
`config.ATR_RATIO_MIN` in-process immediately before each `backtest.run()`
call. This is fully non-breaking and requires no edits to existing files.

**Variable isolation.** WFO (walk-forward `BREAKOUT_PERIOD` optimizer) is ON by
default in `run_backtest.py`. The sweep runs with **WFO OFF** (static
`BREAKOUT_PERIOD = 14`) so the experiment isolates `ATR_RATIO_MIN` and does not
confound it with the optimizer's behavior.

---

## 4. Validation Methodology

### 4.1 Train/test split
Each asset's history is split by time into an in-sample **train** head and an
out-of-sample **test** tail. **Default split: 70/30.** The split is applied to
both `df_5m` and `df_1h` at the same wall-clock boundary so the two feeds stay
aligned.

### 4.2 The 2×2 per asset
For every candidate, run four backtests:

|              | train window | test window |
|--------------|:------------:|:-----------:|
| `1.15` (base)|      ✓       |      ✓      |
| `1.10` (test)|      ✓       |      ✓      |

Held constant across all four: `RISK_PERCENT = 8.0`, `ATR_TP_MULTIPLIER = 6.0`,
`ATR_SL_MULTIPLIER = 1.5`, `BREAKOUT_PERIOD = 14`, WFO OFF, `mode = "1h"`.

### 4.3 Qualification rule (evaluated on the TEST window only)
An asset is flagged a **Safe 1.10 pair** iff **both** hold on the test window:

1. **No degradation vs baseline:** `PF(1.10, test) ≥ PF(1.15, test)`.
2. **Survives the ruin floor:** `MaxDD(1.10, test) ≥ −50%` (default floor).

The train-window numbers are reported for context (and to expose
train→test decay) but are **not** part of the pass/fail decision. Absolute
leaderboard rank is shown but is **never** the selection criterion.

`VERDICT` values:
- `PASS` — both conditions met.
- `FAIL` — `1.10` degrades vs `1.15` on test (ΔPF < 0).
- `RUIN` — `MaxDD(1.10, test)` breaches the −50% floor (overrides FAIL/PASS).

---

## 5. Data Handling

- **Resolution: full 5m + 1h, production-exact** (per decision 2026-06-03), to
  match live intra-bar SL/TP fill semantics exactly.
- **Volume reality:** ~6 candidates × ~5 yr × 5m ≈ 3M bars / ~2,000 Binance
  requests. The sweep fetches **sequentially**, relying on `fetch_data`'s
  CSV cache (`data/<symbol>_<interval>.csv`) so re-runs are cheap and partial
  progress survives interruption.
- **Recommended execution:** run as a background batch; expect tens of minutes
  of fetch on a cold cache.
- **Period default:** `days = 1825` (5 years), overridable.

---

## 6. Output

A single console table (**no log files** — standing project preference). One
row per asset:

```
Asset    PF(1.15t)  PF(1.10t)  ΔPF   MaxDD(1.10t)  CAGR(1.10t)  Trades  VERDICT
BTCUSDT     1.42      1.18    -0.24     -71.3%        +XX%        NNN     RUIN
SOLUSDT     1.55      1.71    +0.16     -38.0%        +XX%        NNN     PASS
...
```

Sorted by `ΔPF` descending, then by `MaxDD` (least-negative first). Column
values are pulled directly from `BacktestResult.stats`. The control row
(`BTCUSDT`) is always included to anchor interpretation.

---

## 7. Candidate Pool (default `CANDIDATES`)

```python
CANDIDATES = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT',
              'AVAXUSDT', 'LINKUSDT', 'NEARUSDT']
```

- **Control:** `BTCUSDT` (the known `1.10` failure).
- **Majors:** `ETHUSDT`, `SOLUSDT` — deeper/cleaner macro trends and stronger
  breakout extension than BTC (hypothesis to be tested, not assumed).
- **High-volume large caps:** `BNBUSDT`, `AVAXUSDT`, `LINKUSDT`, `NEARUSDT`.

The list is a hypothesis; the test-window data decides, and the narrative
justification carries no weight in the verdict.

---

## 8. Multi-Asset Risk & Deployment (analysis only — out of scope to build)

These findings are recorded to inform a *future* deployment project. They are
**not** implemented by `sweep_assets.py`.

### 8.1 Per-asset risk is not independent
Crypto alts run ≈0.7–0.9 correlated to BTC. Naively cutting `RISK_PERCENT`
8%→4% per asset does **not** halve portfolio risk: in a joint crypto drawdown,
three correlated alts at 4% behave like ≈10–12%+ effective single-bet heat, not
12%-spread-thin. Any deployment must size against **portfolio heat**, not
treat each symbol as independent.

### 8.2 Concurrency: the current architecture is single-position
`StateManager` persists exactly **one** active `position` (state_manager.py:152,
216) — a single serialized position dict, with no per-symbol keying and no
position list. The live bot is therefore **single-symbol, single-position
today.** Running multiple pairs concurrently is **not** a config change; it
requires:
- per-symbol state rows (keyed by symbol) or a position collection in the DB
  schema, and
- a portfolio-level risk governor enforcing aggregate heat (§8.1).

This is flagged as a hard prerequisite for any multi-asset live deployment and
is explicitly out of scope for this research tool.

---

## 9. Defaults Summary

| Knob | Default | Overridable |
|------|---------|-------------|
| Train/test split | 70 / 30 | yes |
| Ruin floor (MaxDD) | −50% | yes |
| `RISK_PERCENT` | 8.0 | held constant |
| `ATR_TP_MULTIPLIER` | 6.0 | held constant |
| `ATR_SL_MULTIPLIER` | 1.5 | held constant |
| `BREAKOUT_PERIOD` | 14 (WFO off) | held constant |
| `days` | 1825 (5 yr) | yes |
| Loose gate under test | 1.10 | configurable |
| Baseline gate | 1.15 | configurable |

---

## 10. Non-Goals

- No live trading, order routing, or `StateManager` changes.
- No new CLI flags on `run_backtest.py`.
- No parameter optimization beyond the fixed `1.10`-vs-`1.15` comparison (this
  is a screen, not an optimizer).
- No automatic deployment of "passing" assets — output is advisory.
