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


TESTS = [
    test_verdict_pass,
    test_verdict_equal_is_pass,
    test_verdict_fail_on_degradation,
    test_verdict_ruin_on_drawdown,
    test_verdict_ruin_overrides_fail,
    test_split_by_time_aligned,
    test_split_by_time_indices_reset,
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
