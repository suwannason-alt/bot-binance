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
