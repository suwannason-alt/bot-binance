# Multi-Asset ATR_RATIO Sweep Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `sweep_assets.py`, an offline research tool that screens Binance perpetual pairs for compatibility with the loose `ATR_RATIO_MIN = 1.10` volatility gate using out-of-sample, per-asset `1.10`-vs-`1.15` head-to-head validation.

**Architecture:** A single standalone script imports the existing `fetch_data`, `backtest`, and `config` modules in-process. For each candidate it fetches data, splits it 70/30 by time, runs a 2×2 matrix (`{1.10, 1.15} × {train, test}`) of `backtest.run()` calls, reads `BacktestResult.stats`, and prints one verdict table. No existing file is modified.

**Tech Stack:** Python 3, pandas, numpy, asyncio. Tests are **standalone assert-based scripts** run with `python test_sweep_assets.py` (the project convention — pytest is NOT installed). Spec: `docs/superpowers/specs/2026-06-03-multi-asset-atr-sweep-design.md`.

---

## File Structure

- **Create: `sweep_assets.py`** — the sweep tool. Pure helpers (`split_by_time`, `verdict`, `build_row`, `sort_rows`, `format_matrix`), an in-process runner (`run_single`), an orchestrator (`evaluate_asset`), and a CLI (`parse_args`, `main`).
- **Create: `test_sweep_assets.py`** — standalone test script following `test_signal_diagnostics.py` style: a set of `test_*()` functions and a `main()` that runs them all, prints PASS/FAIL per test, and `sys.exit(1)` on any failure.
- **Modify:** none.

### Verified facts (do not re-derive)
- `fetch_data.fetch_all(symbol, days) -> (df_5m, df_1h)` is **async**; both DataFrames have an integer-ms `open_time` column. fetch_data.py:259.
- `backtest.run(df_5m, df_1h, initial_balance=1000.0, mode="1h") -> BacktestResult`. backtest.py:472.
- `BacktestResult.stats` is a dict containing at least: `profit_factor`, `max_drawdown_pct` (negative %), `cagr_pct`, `win_rate`, `wins`, `losses`. backtest.py:1238-1265. `profit_factor` may be `float("inf")` when there are no losing trades.
- Config attributes to set (module-level globals): `config.ATR_RATIO_MIN`, `config.RISK_PERCENT`, `config.ATR_TP_MULTIPLIER`, `config.ATR_SL_MULTIPLIER`, `config.BREAKOUT_PERIOD`, `config.WFO_ENABLED`. config.py:94-381.

---

## Task 1: Scaffolding + `verdict()` (pure qualification logic)

**Files:**
- Create: `sweep_assets.py`
- Create: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test + the standalone test harness**

Create `test_sweep_assets.py`:

```python
"""Standalone test suite for sweep_assets.py.

Convention (matches test_signal_diagnostics.py): no pytest. Each test_* function
raises AssertionError on failure; main() runs them all and exits non-zero if any
fail.

Run:  python test_sweep_assets.py
"""
import sys
import types

import numpy as np
import pandas as pd

import sweep_assets as sa


def test_verdict_pass():
    # 1.10 PF beats baseline, drawdown within floor → PASS
    assert sa.verdict(pf_115_test=1.0, pf_110_test=1.2, maxdd_110_test=-30.0) == "PASS"


def test_verdict_equal_is_pass():
    # Equal PF is not a degradation → PASS
    assert sa.verdict(1.2, 1.2, -30.0) == "PASS"


def test_verdict_fail_on_degradation():
    # 1.10 PF worse than baseline → FAIL
    assert sa.verdict(1.5, 1.2, -30.0) == "FAIL"


def test_verdict_ruin_on_drawdown():
    # Drawdown breaches -50% floor → RUIN
    assert sa.verdict(1.0, 1.2, -60.0) == "RUIN"


def test_verdict_ruin_overrides_fail():
    # Both degraded AND ruinous → RUIN takes precedence
    assert sa.verdict(1.5, 1.2, -60.0) == "RUIN"


TESTS = [
    test_verdict_pass,
    test_verdict_equal_is_pass,
    test_verdict_fail_on_degradation,
    test_verdict_ruin_on_drawdown,
    test_verdict_ruin_overrides_fail,
]


def main() -> int:
    failures = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(TESTS) - failures}/{len(TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'sweep_assets'` (or `AttributeError: module 'sweep_assets' has no attribute 'verdict'` once the file exists but is empty).

- [ ] **Step 3: Write minimal implementation**

Create `sweep_assets.py`:

```python
"""Multi-asset ATR_RATIO sweep — offline research tool.

Screens Binance perpetual pairs for compatibility with the loose
ATR_RATIO_MIN = 1.10 volatility gate, using out-of-sample, per-asset
1.10-vs-1.15 head-to-head validation. See
docs/superpowers/specs/2026-06-03-multi-asset-atr-sweep-design.md.

Run:  python sweep_assets.py            # full sweep, 5y, default candidates
      python sweep_assets.py --days 730 # custom window
"""
from __future__ import annotations

RUIN_FLOOR = -50.0      # MaxDD (%) below this disqualifies an asset
SPLIT_FRAC = 0.70       # train fraction; remainder is out-of-sample test


def verdict(pf_115_test: float, pf_110_test: float,
            maxdd_110_test: float, ruin_floor: float = RUIN_FLOOR) -> str:
    """Classify an asset from its TEST-window metrics.

    RUIN  — 1.10 drawdown breaches the ruin floor (overrides everything).
    FAIL  — 1.10 profit factor degrades vs the 1.15 baseline.
    PASS  — 1.10 holds its own on profit factor and survives the floor.
    """
    if maxdd_110_test < ruin_floor:
        return "RUIN"
    if pf_110_test < pf_115_test:
        return "FAIL"
    return "PASS"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `5/5 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: 🎯 sweep_assets verdict logic + test harness"
```

---

## Task 2: `split_by_time()` (aligned train/test split)

**Files:**
- Modify: `sweep_assets.py`
- Modify: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test**

Add to `test_sweep_assets.py` (above the `TESTS` list):

```python
def _synthetic_feeds():
    """10 hourly bars + aligned 120 five-minute bars, ms open_time."""
    h_ms = 3_600_000
    df_1h = pd.DataFrame({"open_time": [i * h_ms for i in range(10)],
                          "close": np.arange(10, dtype=float)})
    m_ms = 300_000
    df_5m = pd.DataFrame({"open_time": [i * m_ms for i in range(120)],
                          "close": np.arange(120, dtype=float)})
    return df_5m, df_1h


def test_split_by_time_aligned():
    df_5m, df_1h = _synthetic_feeds()
    tr5, tr1, te5, te1 = sa.split_by_time(df_5m, df_1h, frac=0.7)
    # 1h: boundary = open_time at index int(10*0.7)=7 → 7 train rows, 3 test rows
    assert len(tr1) == 7
    assert len(te1) == 3
    # No bar lost, no overlap
    assert len(tr1) + len(te1) == len(df_1h)
    assert tr1["open_time"].max() < te1["open_time"].min()
    # 5m split at the SAME wall-clock boundary (7 * 3_600_000)
    boundary = 7 * 3_600_000
    assert tr5["open_time"].max() < boundary <= te5["open_time"].min()
    assert len(tr5) + len(te5) == len(df_5m)


def test_split_by_time_indices_reset():
    df_5m, df_1h = _synthetic_feeds()
    _, _, te5, te1 = sa.split_by_time(df_5m, df_1h, frac=0.7)
    assert list(te1.index) == list(range(len(te1)))
    assert list(te5.index) == list(range(len(te5)))
```

Add both function names to the `TESTS` list.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `AttributeError: module 'sweep_assets' has no attribute 'split_by_time'`.

- [ ] **Step 3: Write minimal implementation**

Add to `sweep_assets.py`:

```python
from typing import Tuple

import pandas as pd


def split_by_time(df_5m: pd.DataFrame, df_1h: pd.DataFrame,
                  frac: float = SPLIT_FRAC
                  ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Split both feeds at one shared wall-clock boundary.

    The boundary is the 1h ``open_time`` at the ``frac`` index, so train and
    test windows are identical across the 5m and 1h feeds.

    Returns ``(train_5m, train_1h, test_5m, test_1h)`` with reset indices.
    """
    boundary = int(df_1h["open_time"].iloc[int(len(df_1h) * frac)])
    tr1 = df_1h[df_1h["open_time"] < boundary].reset_index(drop=True)
    te1 = df_1h[df_1h["open_time"] >= boundary].reset_index(drop=True)
    tr5 = df_5m[df_5m["open_time"] < boundary].reset_index(drop=True)
    te5 = df_5m[df_5m["open_time"] >= boundary].reset_index(drop=True)
    return tr5, tr1, te5, te1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `7/7 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: ✂️ aligned train/test split"
```

---

## Task 3: `build_row()` + `sort_rows()` (metric extraction & ordering)

**Files:**
- Modify: `sweep_assets.py`
- Modify: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test**

Add to `test_sweep_assets.py`:

```python
def _stats(pf, dd, cagr, wins, losses):
    return {"profit_factor": pf, "max_drawdown_pct": dd,
            "cagr_pct": cagr, "wins": wins, "losses": losses}


def test_build_row_computes_delta_and_verdict():
    row = sa.build_row(
        symbol="SOLUSDT",
        s115_test=_stats(1.40, -25.0, 80.0, 30, 20),
        s110_test=_stats(1.62, -38.0, 120.0, 50, 30),
    )
    assert row["symbol"] == "SOLUSDT"
    assert row["pf_115_test"] == 1.40
    assert row["pf_110_test"] == 1.62
    assert abs(row["delta_pf"] - 0.22) < 1e-9
    assert row["maxdd_110_test"] == -38.0
    assert row["cagr_110_test"] == 120.0
    assert row["trades_110_test"] == 80          # wins + losses
    assert row["verdict"] == "PASS"


def test_sort_rows_by_delta_then_drawdown():
    rows = [
        {"symbol": "A", "delta_pf": 0.10, "maxdd_110_test": -40.0},
        {"symbol": "B", "delta_pf": 0.30, "maxdd_110_test": -55.0},
        {"symbol": "C", "delta_pf": 0.10, "maxdd_110_test": -20.0},
    ]
    ordered = [r["symbol"] for r in sa.sort_rows(rows)]
    # Primary: delta_pf desc → B first. Tie (A,C at 0.10): less-negative DD first → C before A.
    assert ordered == ["B", "C", "A"]
```

Add both names to `TESTS`. Note `build_row` takes only the **test-window** stats dicts — train-window stats are reported separately at the orchestration layer and are not needed for the verdict.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `AttributeError: module 'sweep_assets' has no attribute 'build_row'`.

- [ ] **Step 3: Write minimal implementation**

Add to `sweep_assets.py`:

```python
from typing import Any, Dict, List


def build_row(symbol: str, s115_test: Dict[str, Any],
              s110_test: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble one result row from the two TEST-window stats dicts."""
    pf115 = s115_test["profit_factor"]
    pf110 = s110_test["profit_factor"]
    dd110 = s110_test["max_drawdown_pct"]
    return {
        "symbol":          symbol,
        "pf_115_test":     pf115,
        "pf_110_test":     pf110,
        "delta_pf":        pf110 - pf115,
        "maxdd_110_test":  dd110,
        "cagr_110_test":   s110_test["cagr_pct"],
        "trades_110_test": s110_test["wins"] + s110_test["losses"],
        "verdict":         verdict(pf115, pf110, dd110),
    }


def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort by ΔPF descending, then by MaxDD (least-negative first)."""
    return sorted(rows, key=lambda r: (r["delta_pf"], r["maxdd_110_test"]),
                  reverse=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `9/9 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: 📊 row builder + ΔPF/DD ordering"
```

---

## Task 4: `format_matrix()` (console table)

**Files:**
- Modify: `sweep_assets.py`
- Modify: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test**

Add to `test_sweep_assets.py`:

```python
def test_format_matrix_has_header_and_sorted_rows():
    rows = [
        sa.build_row("BTCUSDT", _stats(1.42, -30.0, 50.0, 40, 30),
                     _stats(1.18, -71.3, 40.0, 60, 50)),   # FAIL+RUIN → RUIN
        sa.build_row("SOLUSDT", _stats(1.40, -25.0, 80.0, 30, 20),
                     _stats(1.62, -38.0, 120.0, 50, 30)),  # PASS
    ]
    out = sa.format_matrix(rows)
    assert "Asset" in out and "VERDICT" in out
    assert "BTCUSDT" in out and "SOLUSDT" in out
    assert "RUIN" in out and "PASS" in out
    # SOLUSDT (ΔPF +0.22) must appear before BTCUSDT (ΔPF -0.24)
    assert out.index("SOLUSDT") < out.index("BTCUSDT")


def test_format_matrix_handles_inf_profit_factor():
    rows = [sa.build_row("ETHUSDT", _stats(1.30, -20.0, 60.0, 25, 15),
                         _stats(float("inf"), -22.0, 90.0, 40, 0))]
    out = sa.format_matrix(rows)   # must not raise
    assert "ETHUSDT" in out
    assert "inf" in out.lower()
```

Add both names to `TESTS`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `AttributeError: module 'sweep_assets' has no attribute 'format_matrix'`.

- [ ] **Step 3: Write minimal implementation**

Add to `sweep_assets.py`:

```python
def _fmt_pf(pf: float) -> str:
    return "  inf" if pf == float("inf") else f"{pf:5.2f}"


def format_matrix(rows: List[Dict[str, Any]]) -> str:
    """Render the sorted verdict table as a single console string."""
    header = (
        f"{'Asset':<9} {'PF1.15t':>8} {'PF1.10t':>8} {'ΔPF':>7} "
        f"{'MaxDD1.10t':>11} {'CAGR1.10t':>10} {'Trades':>7} {'VERDICT':>8}"
    )
    sep = "-" * len(header)
    lines = [header, sep]
    for r in sort_rows(rows):
        dpf = r["delta_pf"]
        dpf_s = "    inf" if dpf == float("inf") else f"{dpf:+7.2f}"
        lines.append(
            f"{r['symbol']:<9} {_fmt_pf(r['pf_115_test']):>8} "
            f"{_fmt_pf(r['pf_110_test']):>8} {dpf_s:>7} "
            f"{r['maxdd_110_test']:>10.1f}% {r['cagr_110_test']:>9.1f}% "
            f"{r['trades_110_test']:>7d} {r['verdict']:>8}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `11/11 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: 🧾 verdict matrix formatter"
```

---

## Task 5: `run_single()` (in-process config + backtest call)

**Files:**
- Modify: `sweep_assets.py`
- Modify: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test**

This test monkeypatches `backtest.run` so it needs no network or real data. It captures the config state at call time to prove the knobs were applied and WFO disabled.

Add to `test_sweep_assets.py` (top-level imports `config` and `backtest`):

```python
import config
import backtest


def test_run_single_sets_config_and_disables_wfo():
    captured = {}

    def fake_run(df_5m, df_1h, initial_balance=1000.0, mode="1h"):
        captured["atr_ratio"] = config.ATR_RATIO_MIN
        captured["wfo"] = config.WFO_ENABLED
        captured["risk"] = config.RISK_PERCENT
        captured["tp"] = config.ATR_TP_MULTIPLIER
        captured["mode"] = mode
        return types.SimpleNamespace(stats={"profit_factor": 1.99})

    orig = backtest.run
    backtest.run = fake_run
    try:
        stats = sa.run_single(pd.DataFrame(), pd.DataFrame(), atr_ratio=1.10)
    finally:
        backtest.run = orig

    assert stats == {"profit_factor": 1.99}
    assert captured["atr_ratio"] == 1.10
    assert captured["wfo"] is False
    assert captured["risk"] == 8.0
    assert captured["tp"] == 6.0
    assert captured["mode"] == "1h"
```

Add the name to `TESTS`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `AttributeError: module 'sweep_assets' has no attribute 'run_single'`.

- [ ] **Step 3: Write minimal implementation**

Add to `sweep_assets.py` (add `import backtest` and `import config` near the top imports):

```python
import backtest
import config


def run_single(df_5m: pd.DataFrame, df_1h: pd.DataFrame, atr_ratio: float,
               initial_balance: float = 1000.0) -> Dict[str, Any]:
    """Apply the fixed sweep config + the given gate, run one backtest.

    Holds RISK=8, TP=6.0, SL=1.5, BREAKOUT=14 constant and forces WFO OFF so
    the experiment isolates ATR_RATIO_MIN. Returns ``BacktestResult.stats``.
    """
    config.ATR_RATIO_MIN     = atr_ratio
    config.RISK_PERCENT      = 8.0
    config.ATR_TP_MULTIPLIER = 6.0
    config.ATR_SL_MULTIPLIER = 1.5
    config.BREAKOUT_PERIOD   = 14
    config.WFO_ENABLED       = False
    result = backtest.run(df_5m, df_1h, initial_balance=initial_balance,
                          mode="1h")
    return result.stats
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `12/12 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: 🔧 in-process single-run config setter"
```

---

## Task 6: `evaluate_asset()` + CLI (`parse_args`, `main`)

**Files:**
- Modify: `sweep_assets.py`
- Modify: `test_sweep_assets.py`

- [ ] **Step 1: Write the failing test**

Monkeypatch both `fetch_data.fetch_all` (return synthetic feeds, no network) and `backtest.run` (return per-gate deterministic stats) to test the full orchestration of one asset.

Add to `test_sweep_assets.py` (top-level `import fetch_data`):

```python
import fetch_data


def test_evaluate_asset_orchestrates_four_runs():
    df_5m, df_1h = _synthetic_feeds()

    async def fake_fetch_all(symbol="BTCUSDT", days=365):
        return df_5m, df_1h

    # Return PF keyed on the active gate so we can assert wiring.
    def fake_run(d5, d1, initial_balance=1000.0, mode="1h"):
        pf = 1.7 if config.ATR_RATIO_MIN == 1.10 else 1.4
        return types.SimpleNamespace(stats=_stats(pf, -30.0, 90.0, 40, 20))

    o_fetch, o_run = fetch_data.fetch_all, backtest.run
    fetch_data.fetch_all, backtest.run = fake_fetch_all, fake_run
    try:
        row = sa.evaluate_asset("SOLUSDT", days=120, frac=0.7)
    finally:
        fetch_data.fetch_all, backtest.run = o_fetch, o_run

    assert row["symbol"] == "SOLUSDT"
    assert row["pf_115_test"] == 1.4
    assert row["pf_110_test"] == 1.7
    assert row["verdict"] == "PASS"


def test_parse_args_defaults():
    args = sa.parse_args([])
    assert args.days == 1825
    assert abs(args.split - 0.70) < 1e-9
    assert args.ruin_floor == -50.0
    assert args.candidates == sa.DEFAULT_CANDIDATES


def test_parse_args_overrides():
    args = sa.parse_args(["--days", "730", "--split", "0.6",
                          "--candidates", "ETHUSDT,SOLUSDT"])
    assert args.days == 730
    assert abs(args.split - 0.6) < 1e-9
    assert args.candidates == ["ETHUSDT", "SOLUSDT"]
```

Add all three names to `TESTS`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python test_sweep_assets.py`
Expected: FAIL — `AttributeError: module 'sweep_assets' has no attribute 'evaluate_asset'`.

- [ ] **Step 3: Write minimal implementation**

Add to `sweep_assets.py` (add `import argparse`, `import asyncio`, `import sys` to imports):

```python
import argparse
import asyncio
import sys

import fetch_data

DEFAULT_CANDIDATES = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT",
                      "AVAXUSDT", "LINKUSDT", "NEARUSDT"]


def evaluate_asset(symbol: str, days: int,
                   frac: float = SPLIT_FRAC) -> Dict[str, Any]:
    """Fetch, split, run the 2×2 matrix, and build the result row for one asset.

    The train-window runs execute (and surface fetch/data errors early) but only
    the TEST-window stats drive the verdict, per the spec's out-of-sample rule.
    """
    df_5m, df_1h = asyncio.run(fetch_data.fetch_all(symbol=symbol, days=days))
    tr5, tr1, te5, te1 = split_by_time(df_5m, df_1h, frac)

    run_single(tr5, tr1, atr_ratio=1.15)          # train baseline (context only)
    run_single(tr5, tr1, atr_ratio=1.10)          # train loose    (context only)
    s115_test = run_single(te5, te1, atr_ratio=1.15)
    s110_test = run_single(te5, te1, atr_ratio=1.10)
    return build_row(symbol, s115_test, s110_test)


def parse_args(argv=None) -> argparse.Namespace:
    """Parse CLI options for the sweep."""
    p = argparse.ArgumentParser(
        description="Multi-asset ATR_RATIO 1.10-vs-1.15 out-of-sample sweep.",
    )
    p.add_argument("--days", type=int, default=1825,
                   help="Calendar days of history per asset (default: 1825 = 5y).")
    p.add_argument("--split", type=float, default=SPLIT_FRAC,
                   help=f"Train fraction, 0-1 (default: {SPLIT_FRAC}).")
    p.add_argument("--ruin-floor", dest="ruin_floor", type=float,
                   default=RUIN_FLOOR,
                   help=f"MaxDD %% disqualify floor (default: {RUIN_FLOOR}).")
    p.add_argument("--candidates", type=lambda s: s.split(","),
                   default=DEFAULT_CANDIDATES,
                   help="Comma-separated symbols (default: built-in pool).")
    return p.parse_args(argv)


def main(argv=None) -> int:
    """Run the sweep across all candidates and print the verdict matrix."""
    args = parse_args(argv)
    global RUIN_FLOOR
    RUIN_FLOOR = args.ruin_floor
    rows: List[Dict[str, Any]] = []
    for symbol in args.candidates:
        print(f"\n=== {symbol} ===")
        try:
            rows.append(evaluate_asset(symbol, days=args.days, frac=args.split))
        except Exception as exc:  # noqa: BLE001 — keep sweeping other assets
            print(f"  SKIP {symbol}: {exc}")
    print("\n" + format_matrix(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: `verdict()` reads the module-level `RUIN_FLOOR`, which `main()` rebinds from `--ruin-floor` before any `evaluate_asset` call, so a custom floor is honored.

- [ ] **Step 4: Run test to verify it passes**

Run: `python test_sweep_assets.py`
Expected: `15/15 passed`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add sweep_assets.py test_sweep_assets.py
git commit -m "feat: 🚀 asset orchestrator + CLI"
```

---

## Task 7: End-to-end smoke run (BTC control, short window)

This task verifies the wired tool actually runs against real data and the live `backtest.run`. It uses a short window to stay fast and the already-cached BTC data.

**Files:** none (manual verification + README note).

- [ ] **Step 1: Run the sweep on the control asset only, short window**

Run: `python sweep_assets.py --candidates BTCUSDT --days 365`
Expected: fetches/uses cached BTC data, prints `=== BTCUSDT ===`, then a one-row matrix with header `Asset ... VERDICT` and a verdict in `{PASS, FAIL, RUIN}`. No traceback.

- [ ] **Step 2: Confirm config isolation did not leak**

Run: `python -c "import config; print(config.WFO_ENABLED, config.ATR_RATIO_MIN)"`
Expected: prints the module defaults from a fresh import (`True 1.15`) — confirming `sweep_assets` only mutates config within its own process, never on disk.

- [ ] **Step 3: Run the full test suite once more**

Run: `python test_sweep_assets.py`
Expected: `15/15 passed`, exit 0.

- [ ] **Step 4: Commit any doc note (if README updated)**

```bash
git add -A
git commit -m "docs: 📝 note sweep_assets usage" || echo "nothing to commit"
```

---

## Self-Review Notes (already reconciled)

- **Spec §3 architecture** → Tasks 1-6 (import-based, no existing-file edits; `config.ATR_RATIO_MIN` set in `run_single`, Task 5).
- **Spec §4 validation** → `split_by_time` (Task 2), 2×2 in `evaluate_asset` (Task 6, WFO off via Task 5), `verdict` test-window-only rule (Task 1, Task 3).
- **Spec §6 output** → `format_matrix`, ΔPF-then-DD sort (Tasks 3-4), no log files (console only).
- **Spec §7 candidates** → `DEFAULT_CANDIDATES` (Task 6).
- **Spec §9 defaults** → 70/30 split, −50% floor, held-constant knobs (Tasks 1, 5, 6).
- **Spec §8/§10 non-goals** → no `StateManager`, live-trading, or CLI changes to `run_backtest.py`; nothing in the plan touches them.
- **`inf` profit factor** edge case handled (Task 4) since `profit_factor` can be `float("inf")`.
- **Type consistency:** `run_single`→stats dict; `build_row(symbol, s115_test, s110_test)`; row keys (`delta_pf`, `maxdd_110_test`, `pf_115_test`, `pf_110_test`, `cagr_110_test`, `trades_110_test`, `verdict`) used identically across Tasks 3, 4, 6.
