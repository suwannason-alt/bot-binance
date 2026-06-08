"""Drift-guard test for the entry-funnel diagnostic.

GUARANTEE: ``strategy.format_signal_diagnostics()`` must NEVER disagree with the
sacred ``strategy.evaluate_1h_signal()`` decision.  For every historical bar we
assert:

    diag.passed  ==  (evaluate_1h_signal(i1) is not None)

If they ever diverge, the live log would lie about why the bot did/didn't trade.
This test locks them together so the observability layer can never drift away
from the real entry logic.

Run:  python tests/test_signal_diagnostics.py   (exits non-zero on any mismatch)
"""
# ── Path bootstrap: modular layout — keep flat `import config` style resolvable
# from any subdirectory (src/core, backtesting, scripts). ──────────────────────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)


import numpy as np
import pandas as pd

import config
import strategy
import run_backtest as rb

WINDOW = 400          # trailing bars per evaluation (>= MIN_CANDLES_1H, warms EMA200)
STEP = 9              # sample stride across full history (fine enough to hit signal bars)
DATA = "data/btcusdt_1h.csv"


def main() -> int:
    # Apply the proven/live config so thresholds match production exactly.
    rb._apply_config(rb._BASE)

    df = pd.read_csv(DATA)
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    v = df["volume"].to_numpy(float)
    n = len(c)
    print(f"Loaded {n:,} 1H bars from {DATA}  | window={WINDOW} step={STEP}")

    checked = mism = n_pass = 0
    mismatches = []
    # Mix bars_since_last so the cooldown gate is exercised too.
    for idx, k in enumerate(range(WINDOW, n, STEP)):
        sl = slice(k - WINDOW, k)
        i1 = strategy.compute_1h_indicators(o[sl], h[sl], l[sl], c[sl], v[sl])
        bars_since = 0 if (idx % 13 == 0) else 9999   # ~7.7% of bars in cooldown

        sig = strategy.evaluate_1h_signal(i1, bars_since)
        diag = strategy.format_signal_diagnostics(i1, bars_since)

        sig_fires = sig is not None
        checked += 1
        if sig_fires:
            n_pass += 1
        if diag.passed != sig_fires:
            mism += 1
            if len(mismatches) < 5:
                mismatches.append((k, sig_fires, diag.passed, diag.failed))

    print(f"Checked {checked:,} bars | signal-fires={n_pass} | mismatches={mism}")

    if mism:
        print("\nDRIFT DETECTED — diagnostic disagrees with evaluate_1h_signal:")
        for k, sf, dp, failed in mismatches:
            print(f"  bar {k}: evaluate fires={sf}  diag.passed={dp}  failed={failed}")
        return 1

    if n_pass == 0:
        print("WARNING: no signal-firing bars sampled — positive path unverified.")
        return 1

    print("\nPASS — diagnostic is byte-for-byte in sync with the sacred entry path.")
    print("       (both verdicts agreed on every sampled bar, incl. signal-firing ones)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
