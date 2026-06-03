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

import backtest
import config
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


def test_split_by_time_frac_one_no_crash():
    # frac=1.0 must not raise IndexError; index clamps to the last bar.
    df_5m, df_1h = _synthetic_feeds()
    tr5, tr1, te5, te1 = sa.split_by_time(df_5m, df_1h, frac=1.0)
    # boundary clamps to the last 1h bar (index 9 → open_time 9*3_600_000),
    # so exactly the last 1h bar lands in the test window.
    assert len(te1) == 1
    assert len(tr1) == 9
    assert len(tr1) + len(te1) == len(df_1h)


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


TESTS = [
    test_verdict_pass,
    test_verdict_equal_is_pass,
    test_verdict_fail_on_degradation,
    test_verdict_ruin_on_drawdown,
    test_verdict_ruin_overrides_fail,
    test_split_by_time_aligned,
    test_split_by_time_indices_reset,
    test_split_by_time_frac_one_no_crash,
    test_build_row_computes_delta_and_verdict,
    test_sort_rows_by_delta_then_drawdown,
    test_format_matrix_has_header_and_sorted_rows,
    test_format_matrix_handles_inf_profit_factor,
    test_run_single_sets_config_and_disables_wfo,
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
