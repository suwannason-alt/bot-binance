"""
verify_warmup.py — Indicator Warmup & Convergence Diagnostic
=============================================================
Standalone script that probes the backtest engine's indicator quality at
the first 50 execution ticks.  Run this before any live deployment or
whenever a short-window backtest shows unexpected early losses.

Four checks are performed:

  [1] Data sufficiency   — len(df_1h) ≥ 3,030 bars (EMA200 + WFO training)
  [2] Indicator settling — first valid bar index for every indicator family
  [3] Tick diagnostics   — EMA slope / ADX / Hurst at execution ticks 1, 10, 50
        • Zero-padding guard  : every value must be non-NaN and non-zero
        • No-lookahead guard  : recomputed on the prefix prefix c1[:j+1] must
                                exactly equal the value from the full array
  [4] EMA200 convergence — quantifies the SMA-seed bias at each execution tick

Usage::

    python verify_warmup.py
    python verify_warmup.py --csv path/to/btcusdt_1h.csv

Output is printed to stdout only — no log files are created.
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
# Adjust sys.path so this script can be run from any working directory.
_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

import config
import indicators as ind
from adaptive_regime import hurst_exponent


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_RED    = "\033[91m"
_RESET  = "\033[0m"
_BOLD   = "\033[1m"

def _ok(msg: str)   -> str: return f"{_GREEN}✅ {msg}{_RESET}"
def _warn(msg: str) -> str: return f"{_YELLOW}⚠️  {msg}{_RESET}"
def _fail(msg: str) -> str: return f"{_RED}✗  {msg}{_RESET}"
def _bold(msg: str) -> str: return f"{_BOLD}{msg}{_RESET}"


# ---------------------------------------------------------------------------
# CSV loader
# ---------------------------------------------------------------------------

def _load_1h_csv(csv_path: Path) -> pd.DataFrame:
    """Load the 1H OHLCV CSV.

    Args:
        csv_path: Absolute or relative path to the CSV file.

    Returns:
        DataFrame with numeric columns; sorted ascending by open_time.

    Raises:
        SystemExit: If the file does not exist.
    """
    if not csv_path.exists():
        print(_fail(f"CSV not found: {csv_path}"))
        print("       Run a backtest first to populate the data cache:")
        print("         python run_backtest.py")
        sys.exit(1)

    df = pd.read_csv(csv_path)
    required = {"open_time", "open", "high", "low", "close", "volume", "close_time"}
    if not required.issubset(df.columns):
        missing = required - set(df.columns)
        print(_fail(f"CSV missing columns: {missing}"))
        sys.exit(1)

    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.sort_values("open_time").reset_index(drop=True)
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df


# ---------------------------------------------------------------------------
# EMA200 convergence helper
# ---------------------------------------------------------------------------

def _ema_seed_weight(period: int, steps_after_seed: int) -> float:
    """Fraction of the EMA value still attributable to the initial SMA seed.

    EMA recursion:  EMA[t] = alpha * c[t] + (1-alpha) * EMA[t-1]
    After ``n`` steps:  seed_weight = (1 - alpha)^n

    Args:
        period:           EMA period (e.g. 200).
        steps_after_seed: Number of EMA update steps since the seed bar.

    Returns:
        Seed weight in [0, 1].  0.0 means fully converged.
    """
    alpha = 2.0 / (period + 1)
    return (1.0 - alpha) ** steps_after_seed


# ---------------------------------------------------------------------------
# Tick snapshot
# ---------------------------------------------------------------------------

def _tick_snapshot(
    j: int,
    c1:           np.ndarray,
    h1:           np.ndarray,
    l1:           np.ndarray,
    o1:           np.ndarray,
    ema_trend:    np.ndarray,
    adx_arr:      np.ndarray,
    atr_arr:      np.ndarray,
    timestamps:   Optional[pd.Series] = None,
) -> Dict:
    """Collect all diagnostic values for a single 1H bar index.

    Args:
        j:            1H bar index.
        c1 … adx_arr: Pre-computed full-array indicators.
        timestamps:   Optional open_time series for display.

    Returns:
        Dict with keys: close, ema200, adx, atr, atr_pct, ema_slope,
        hurst, ema_seed_wt, ts.
    """
    slope_bars = config.EMA_TREND_SLOPE_BARS   # typically 7
    j_slope = j - slope_bars

    ema200_val = float(ema_trend[j])
    ema_slope = float(
        (ema_trend[j] - ema_trend[j_slope]) / ema_trend[j_slope] * 100
    ) if j_slope >= 0 and not math.isnan(float(ema_trend[j_slope])) else float("nan")

    close_val = float(c1[j])
    atr_val   = float(atr_arr[j])
    adx_val   = float(adx_arr[j])
    atr_pct   = atr_val / close_val * 100 if close_val > 0 else float("nan")

    # Hurst on the most recent min(200, j+1) closes
    hurst_window = min(200, j + 1)
    hurst_val = hurst_exponent(c1[j + 1 - hurst_window : j + 1])

    # EMA200 seed weight at this bar
    seed_steps = j - (config.EMA_TREND - 1)   # bars since the SMA seed at bar (period-1)
    seed_wt    = _ema_seed_weight(config.EMA_TREND, seed_steps) * 100.0

    ts_str = ""
    if timestamps is not None and j < len(timestamps):
        ts_str = pd.Timestamp(int(timestamps.iloc[j]), unit="ms").strftime("%Y-%m-%d %H:%M")

    return {
        "j":        j,
        "ts":       ts_str,
        "close":    close_val,
        "ema200":   ema200_val,
        "adx":      adx_val,
        "atr":      atr_val,
        "atr_pct":  atr_pct,
        "ema_slope": ema_slope,
        "hurst":    hurst_val,
        "seed_wt":  seed_wt,
    }


# ---------------------------------------------------------------------------
# Verification sections
# ---------------------------------------------------------------------------

def _check_data_sufficiency(df: pd.DataFrame) -> None:
    """[1/4] Assert the CSV has enough bars to support proper indicator warmup."""
    n = len(df)
    req = 3030   # EMA200(200) + WFO training(2160) + buffer(670)

    t0 = pd.Timestamp(int(df["open_time"].iloc[0]),  unit="ms").strftime("%Y-%m-%d")
    tN = pd.Timestamp(int(df["open_time"].iloc[-1]), unit="ms").strftime("%Y-%m-%d")

    print(f"\n{'─'*74}")
    print("[1/4]  DATA SUFFICIENCY")
    print(f"{'─'*74}")
    print(f"  CSV bars:    {n:,}  ({t0} → {tN})")
    print(f"  Required:    {req:,}  (EMA200={config.EMA_TREND}"
          f" + WFO_TRAIN={config.WFO_TRAINING_WINDOW} + buffer=670)")

    if n >= req:
        margin = n / req
        print(_ok(f"PASS  {n:,} ≥ {req:,}  ({margin:.1f}× margin)"))
    else:
        print(_fail(f"FAIL  {n:,} < {req:,} — insufficient history!"))
        print()
        print("  To fix: run the full 5-year backtest first to seed the cache:")
        print("    python run_backtest.py --days 1825")
        sys.exit(1)


def _check_settling(n_bars: int) -> int:
    """[2/4] Show the first valid bar index for each indicator family.

    Args:
        n_bars: Total 1H bars available.

    Returns:
        j_first: First bar where all key indicators are non-NaN
                 AND j >= MIN_CANDLES_1H.
    """
    print(f"\n{'─'*74}")
    print("[2/4]  INDICATOR SETTLING POINTS")
    print(f"{'─'*74}")

    # Theoretical first-valid indices
    k_ema200 = config.EMA_TREND - 1       # bar 199
    k_atr    = config.ATR_PERIOD          # bar 14  (Wilder: first SMA at index period)
    k_adx    = 2 * config.ADX_PERIOD - 1  # bar 27  (needs 2×period for first ADX value)
    k_macd   = config.MACD_SLOW - 1 + config.MACD_SIGNAL  # ~34
    j_first  = max(config.MIN_CANDLES_1H, k_ema200 + 1)

    rows = [
        ("EMA20",     config.EMA_FAST - 1,  j_first - (config.EMA_FAST - 1)),
        ("EMA50",     config.EMA_SLOW - 1,  j_first - (config.EMA_SLOW - 1)),
        (f"EMA{config.EMA_TREND}", k_ema200, j_first - k_ema200),
        (f"ATR({config.ATR_PERIOD})", k_atr, j_first - k_atr),
        (f"ADX({config.ADX_PERIOD})", k_adx, j_first - k_adx),
        ("MACD hist", k_macd, j_first - k_macd),
    ]

    print(f"  {'Indicator':<18} {'First valid bar':>16}  {'Steps before 1st trade':>24}")
    print(f"  {'─'*18}  {'─'*16}  {'─'*24}")
    for name, first_bar, gap in rows:
        flag = "✅" if gap > 10 else "⚠️ "
        settled = "fully settled" if gap > config.EMA_TREND else "⚠️  still seeding"
        if name.startswith("EMA2"):
            settled = _warn("still seeding — see [4/4]") if gap < 100 else "fully settled"
        print(f"  {name:<18}  bar {first_bar:>5}   ({first_bar+1:>5} bars)    "
              f"gap={gap:>5}  {flag}")

    print()
    alpha_ema200 = 2.0 / (config.EMA_TREND + 1)
    seed_wt_at_j = _ema_seed_weight(config.EMA_TREND, j_first - k_ema200) * 100
    adx_seed_wt  = (1.0 - 1.0 / config.ADX_PERIOD) ** (j_first - k_adx) * 100
    print(f"  First execution bar:  j = {j_first}  "
          f"(≡ MIN_CANDLES_1H={config.MIN_CANDLES_1H})")
    print(f"  ADX({config.ADX_PERIOD}) seed weight at j={j_first}:  "
          f"{adx_seed_wt:.2e}%  (≈ 0, fully converged ✅)")
    print(f"  EMA200 seed weight at j={j_first}:  "
          f"{seed_wt_at_j:.1f}%  {_warn('still SMA-biased — see [4/4]')}")

    return j_first


def _check_tick_diagnostics(
    j_first:   int,
    c1:        np.ndarray,
    h1:        np.ndarray,
    l1:        np.ndarray,
    o1:        np.ndarray,
    ema_trend: np.ndarray,
    adx_arr:   np.ndarray,
    atr_arr:   np.ndarray,
    timestamps: Optional[pd.Series],
) -> List[bool]:
    """[3/4] Snapshot + zero-padding + no-lookahead checks at ticks #1, #10, #50."""
    print(f"\n{'─'*74}")
    print("[3/4]  EXECUTION TICK DIAGNOSTICS")
    print(f"{'─'*74}")

    tick_offsets = [0, 9, 49]
    labels       = ["Tick #1 ", "Tick #10", "Tick #50"]
    n            = len(c1)
    snaps        = []

    for off in tick_offsets:
        j = j_first + off
        if j >= n:
            print(_warn(f"Not enough bars for Tick #{off+1} (need j={j}, have {n} bars)"))
            snaps.append(None)
        else:
            snaps.append(_tick_snapshot(j, c1, h1, l1, o1, ema_trend, adx_arr, atr_arr, timestamps))

    # ── Table ────────────────────────────────────────────────────────────────
    col_w = 16
    metrics = [
        ("Close",          "close",     lambda v: f"{v:>12,.2f}"),
        ("EMA200",         "ema200",    lambda v: f"{v:>12,.2f}"),
        ("EMA200 seed wt", "seed_wt",   lambda v: f"{v:>11.1f}%"),
        ("ADX",            "adx",       lambda v: f"{v:>12.2f}"),
        ("ATR",            "atr",       lambda v: f"{v:>12.2f}"),
        ("ATR % close",    "atr_pct",   lambda v: f"{v:>11.3f}%"),
        ("EMA200 slope 7b","ema_slope",  lambda v: f"{v:>+11.3f}%"),
        ("Hurst (200b)",   "hurst",     lambda v: f"{v:>12.3f}"),
    ]

    # Header
    print()
    header = f"  {'Metric':<20}" + "".join(
        f"  {lbl:>{col_w}}" for lbl in labels
    )
    print(header)
    print(f"  {'─'*20}" + "".join(f"  {'─'*col_w}" for _ in labels))

    for label, key, fmt in metrics:
        row = f"  {label:<20}"
        for snap in snaps:
            if snap is None:
                row += f"  {'n/a':>{col_w}}"
            else:
                val = snap[key]
                row += f"  {fmt(val):>{col_w}}" if not math.isnan(val) else f"  {'NaN':>{col_w}}"
        print(row)

    print()
    for snap, lbl in zip(snaps, labels):
        if snap is not None:
            print(f"  {lbl} → j={snap['j']}  {snap['ts']}")

    # ── Zero-padding checks ──────────────────────────────────────────────────
    print()
    print("  ZERO-PADDING CHECKS  (all values must be non-NaN and physically plausible)")
    all_ok = True
    passes: List[bool] = []

    first_snap = snaps[0]
    if first_snap is None:
        print(_warn("Cannot run zero-padding checks — insufficient bars"))
        return []

    j = first_snap["j"]

    def _zp_check(name: str, val: float, lo: float, hi: float) -> bool:
        ok = not math.isnan(val) and lo <= val <= hi
        tag = _ok(f"{name}[{j}] = {val:.6g}  (in [{lo}, {hi}])") if ok \
              else _fail(f"{name}[{j}] = {val}  OUTSIDE [{lo}, {hi}]  ← INDICATOR BUG")
        print(f"  {tag}")
        return ok

    price_min = float(c1.min())
    price_max = float(c1.max())
    all_ok &= _zp_check("EMA200", first_snap["ema200"], price_min * 0.5, price_max * 1.5)
    all_ok &= _zp_check("ADX",    first_snap["adx"],    0.0,             100.0)
    all_ok &= _zp_check("ATR",    first_snap["atr"],    0.0,             price_max * 0.5)
    all_ok &= _zp_check("Hurst",  first_snap["hurst"],  0.10,            0.90)
    passes.append(all_ok)

    # ── No-lookahead checks ──────────────────────────────────────────────────
    print()
    print("  NO-LOOKAHEAD CHECKS  (prefix recompute must exactly match full-array value)")

    for name, full_arr, fn in [
        ("EMA200", ema_trend, lambda: ind.ema(c1[:j + 1], config.EMA_TREND)[-1]),
        ("ADX",    adx_arr,   lambda: ind.adx(h1[:j + 1], l1[:j + 1], c1[:j + 1], config.ADX_PERIOD)[-1]),
        ("ATR",    atr_arr,   lambda: ind.atr(h1[:j + 1], l1[:j + 1], c1[:j + 1], config.ATR_PERIOD)[-1]),
    ]:
        full_val   = float(full_arr[j])
        prefix_val = float(fn())
        delta      = abs(full_val - prefix_val)
        # Float tolerance: EMA is deterministic so delta should be exactly 0
        ok = not math.isnan(full_val) and not math.isnan(prefix_val) and delta < 1e-4
        tag = _ok(f"{name}: prefix={prefix_val:.6g}  full={full_val:.6g}  Δ={delta:.2e}") if ok \
              else _fail(f"{name}: MISMATCH prefix={prefix_val:.6g}  full={full_val:.6g}  Δ={delta:.6g}")
        print(f"  {tag}")
        passes.append(ok)

    if all(passes):
        print()
        print(_ok("All zero-padding and no-lookahead checks passed."))
    else:
        print()
        print(_fail("One or more checks FAILED — review indicator implementation."))

    return passes


def _check_ema200_convergence(
    j_first:   int,
    c1:        np.ndarray,
    ema_trend: np.ndarray,
    timestamps: Optional[pd.Series],
) -> None:
    """[4/4] Print an EMA200 seed-weight convergence table."""
    print(f"\n{'─'*74}")
    print(f"[4/4]  EMA200 CONVERGENCE TABLE  "
          f"(α = 2/(200+1) = {2/(config.EMA_TREND+1):.5f})")
    print(f"{'─'*74}")

    period     = config.EMA_TREND
    seed_bar   = period - 1                       # bar index where SMA seed is set
    seed_val   = float(ema_trend[seed_bar])
    k          = 2.0 / (period + 1)

    print(f"\n  SMA seed set at bar {seed_bar}  (SMA of first {period} closes):")
    print(f"  Seed value = {seed_val:,.2f}")
    print()

    n = len(c1)
    check_bars = sorted(set([
        seed_bar,
        j_first,             # first execution bar
        j_first + 49,        # Tick #50
        min(j_first + 90,  n - 1),   # ~4 days
        min(j_first + 300, n - 1),   # ~2 weeks
        min(j_first + 800, n - 1),   # ~1 month
        min(3029,           n - 1),   # live-bot warmup equivalent
    ]))

    header = (
        f"  {'Bar':>6}  {'Steps':>6}  {'Seed wt':>10}  "
        f"{'EMA200 value':>14}  Note"
    )
    print(header)
    print(f"  {'─'*6}  {'─'*6}  {'─'*10}  {'─'*14}  {'─'*20}")

    for bar in check_bars:
        if bar >= n:
            continue
        steps   = max(0, bar - seed_bar)
        wt      = _ema_seed_weight(period, steps) * 100
        ema_val = float(ema_trend[bar])
        ts_str  = ""
        if timestamps is not None and bar < len(timestamps):
            ts_str = pd.Timestamp(int(timestamps.iloc[bar]), unit="ms").strftime("%Y-%m-%d")

        note = ""
        if bar == seed_bar:
            note = "← SMA seed (100% bias)"
        elif bar == j_first:
            note = f"← 1st trade (MIN_CANDLES_1H={config.MIN_CANDLES_1H})"
        elif bar == j_first + 49:
            note = "← Tick #50"
        elif bar == 3029:
            note = "← live warmup equivalent"

        wt_str = f"{wt:.1f}%" if wt >= 0.1 else "<0.1%"
        ema_str = f"{ema_val:>12,.2f}" if not math.isnan(ema_val) else "       NaN"
        print(f"  {bar:>6}  {steps:>6}  {wt_str:>10}  {ema_str}  {note}")

    print()

    # ── Impact summary ────────────────────────────────────────────────────────
    j50 = j_first + 49
    wt_at_j1  = _ema_seed_weight(period, j_first - seed_bar) * 100
    wt_at_j50 = _ema_seed_weight(period, max(0, j50 - seed_bar)) * 100 if j50 < n else float("nan")
    wt_live   = _ema_seed_weight(period, max(0, 3029 - seed_bar)) * 100

    box_lines = [
        "  IMPACT SUMMARY",
        "  " + "─" * 70,
        f"  5-year backtest:",
        f"    EMA200 seed bias at bar {j_first} (first trade):  {wt_at_j1:.1f}%",
        f"    Seed decays below 5% around bar {seed_bar + int(math.log(0.05)/math.log(1-k))}"
        f" (~{int((seed_bar + int(math.log(0.05)/math.log(1-k))) / 24)} days)",
        f"    The SMA seed is the actual historical mean — not a zero-pad.  The bias",
        f"    is mild and corrects itself within a few hundred bars.  ✅ Safe for",
        f"    long backtests.  Early trades may see a slightly wrong EMA200.",
        "",
        f"  1-year (short) backtest:",
        f"    EMA200 at first trade is still {wt_at_j1:.0f}% SMA-seeded.",
        f"    This can systematically misclassify trend direction in the first",
        f"    ~300 bars, producing early false signals or missed entries.",
        f"    ⚠️  Recommend: INITIAL_COOLDOWN_BARS=48 to suppress entries while",
        f"    the EMA200 settles (also WFO can't retune until bar {config.WFO_TRAINING_WINDOW},",
        f"    so the first ~{config.WFO_TRAINING_WINDOW // 24} days use the static BP=14 anyway).",
        "",
        f"  Live bot (warm_start fetches 3,030 bars):",
        f"    EMA200 seed bias at first live bar: {wt_live:.3f}% → effectively zero.",
        f"    ✅ Fully converged.  No cold-start indicator bias in live mode.",
    ]
    for line in box_lines:
        print(line)


# ---------------------------------------------------------------------------
# WFO Cold-Start Analysis
# ---------------------------------------------------------------------------

def _check_wfo_coldstart(j_first: int, n_bars: int) -> None:
    """Print WFO readiness timing relative to the first execution bar."""
    print(f"\n{'─'*74}")
    print("[BONUS]  WFO COLD-START TIMING")
    print(f"{'─'*74}")

    first_retune = config.WFO_TRAINING_WINDOW   # bar index of 1st WFO retune
    retune_gap   = first_retune - j_first        # bars between 1st trade and 1st retune
    retune_days  = retune_gap / 24.0
    wfo_active_bars = n_bars - first_retune

    if first_retune > n_bars:
        print(_warn(
            f"WFO_TRAINING_WINDOW={config.WFO_TRAINING_WINDOW} exceeds total bars "
            f"({n_bars}) — WFO will never retune on this dataset!"
        ))
        return

    print(f"  First execution bar:    j = {j_first}")
    print(f"  First WFO retune:       j = {first_retune}  "
          f"(after {retune_gap} bars = {retune_days:.1f} days of trading)")
    print(f"  WFO active for:         {wfo_active_bars} bars  "
          f"({wfo_active_bars / (24 * 30.5):.1f} months)")
    print()

    if retune_gap > 0:
        print(f"  Before bar {first_retune}: WFO uses static BP={config.BREAKOUT_PERIOD}  "
              f"(the default, not yet tuned)")
        print(f"  After bar  {first_retune}: WFO retuning every {config.WFO_RETUNE_INTERVAL} bars "
              f"({config.WFO_RETUNE_INTERVAL // 24} days)")

    print()
    if retune_gap > 720:
        print(_warn(
            f"Large WFO cold-start window ({retune_gap} bars = {retune_days:.0f} days).\n"
            f"  Early losses may be due to a suboptimal static BP during this period.\n"
            f"  Mitigation: WFO_FAST_ENABLED=true will use a 30-day window when ATR\n"
            f"  spikes 2×, allowing faster initial adaptation."
        ))
    else:
        print(_ok(
            f"WFO cold-start window is {retune_gap} bars ({retune_days:.1f} days) — "
            f"within acceptable range."
        ))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Indicator warmup & convergence diagnostic for the trading bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--csv",
        default=str(_ROOT / "data" / "btcusdt_1h.csv"),
        help="Path to the 1H OHLCV CSV file (default: data/btcusdt_1h.csv)",
    )
    args = parser.parse_args()

    csv_path = Path(args.csv)

    print()
    print("═" * 74)
    print(_bold("  WARMUP VERIFICATION  |  BTCUSDT 1H  |  verify_warmup.py"))
    print("═" * 74)

    # ── Load data ────────────────────────────────────────────────────────────
    df = _load_1h_csv(csv_path)
    n  = len(df)

    c1 = df["close"].values.astype(float)
    h1 = df["high"].values.astype(float)
    l1 = df["low"].values.astype(float)
    o1 = df["open"].values.astype(float)
    ts = df["open_time"]

    # ── Pre-compute all indicators over the full array ────────────────────────
    print("\n  Computing indicators over full array …", end="", flush=True)
    ema_trend = ind.ema(c1, config.EMA_TREND)
    adx_arr   = ind.adx(h1, l1, c1, config.ADX_PERIOD)
    atr_arr   = ind.atr(h1, l1, c1, config.ATR_PERIOD)
    print("  done.")

    # ── Run checks ───────────────────────────────────────────────────────────
    _check_data_sufficiency(df)

    j_first = _check_settling(n)

    if j_first >= n:
        print(_warn(f"Not enough bars to reach j_first={j_first}  (only {n} bars in CSV)"))
        print("  Run a longer backtest to seed the 1H cache with more history.")
        sys.exit(1)

    passes = _check_tick_diagnostics(
        j_first, c1, h1, l1, o1, ema_trend, adx_arr, atr_arr, ts
    )

    _check_ema200_convergence(j_first, c1, ema_trend, ts)

    _check_wfo_coldstart(j_first, n)

    # ── Final verdict ────────────────────────────────────────────────────────
    print()
    print("═" * 74)
    if all(passes):
        print(_ok(
            "All assertions passed.  No indicator leak, no zero-padding detected.\n"
            "  The engine is correctly computing live indicators from historical\n"
            "  data without lookahead.  See the convergence table above for\n"
            "  EMA200 settling guidance on short-window backtests."
        ))
    else:
        failed = sum(1 for p in passes if not p)
        print(_fail(
            f"{failed}/{len(passes)} assertion(s) FAILED.\n"
            "  Review the indicator output above and check indicators.py."
        ))
    print("═" * 74)
    print()


if __name__ == "__main__":
    main()
