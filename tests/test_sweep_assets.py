"""Standalone test suite for sweep_assets.py.

Convention (matches test_signal_diagnostics.py): no pytest. Each test_* function
raises AssertionError on failure; main() runs them all and exits non-zero if any
fail.

Run:  python tests/test_sweep_assets.py
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

import types

import numpy as np
import pandas as pd

import backtest
import config_1h as config   # asset sweep drives the 1H strategy config
import fetch_data
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
        s115_train=_stats(1.50, -20.0, 70.0, 25, 15),
        s110_train=_stats(1.80, -30.0, 100.0, 45, 25),
        s115_test=_stats(1.40, -25.0, 80.0, 30, 20),
        s110_test=_stats(1.62, -38.0, 120.0, 50, 30),
    )
    assert row["symbol"] == "SOLUSDT"
    assert row["pf_115_test"] == 1.40
    assert row["pf_110_test"] == 1.62
    assert abs(row["delta_pf"] - 0.22) < 1e-9
    assert row["pf_110_train"] == 1.80
    # decay = pf110_test - pf110_train = 1.62 - 1.80 = -0.18 (overfit signal)
    assert abs(row["decay"] - (-0.18)) < 1e-9
    assert row["maxdd_110_test"] == -38.0
    assert row["cagr_110_test"] == 120.0
    assert row["trades_110_test"] == 80
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
        sa.build_row("BTCUSDT",
                     _stats(1.40, -20.0, 60.0, 30, 20), _stats(1.20, -40.0, 50.0, 40, 30),
                     _stats(1.42, -30.0, 50.0, 40, 30), _stats(1.18, -71.3, 40.0, 60, 50)),  # RUIN
        sa.build_row("SOLUSDT",
                     _stats(1.50, -20.0, 70.0, 25, 15), _stats(1.70, -25.0, 90.0, 45, 25),
                     _stats(1.40, -25.0, 80.0, 30, 20), _stats(1.62, -38.0, 120.0, 50, 30)),  # PASS
    ]
    out = sa.format_matrix(rows)
    assert "Asset" in out and "VERDICT" in out
    assert "BTCUSDT" in out and "SOLUSDT" in out
    assert "RUIN" in out and "PASS" in out
    assert out.index("SOLUSDT") < out.index("BTCUSDT")


def test_format_matrix_handles_inf_profit_factor():
    rows = [sa.build_row("ETHUSDT",
                         _stats(1.20, -15.0, 50.0, 20, 10), _stats(2.0, -18.0, 70.0, 30, 10),
                         _stats(1.30, -20.0, 60.0, 25, 15),
                         _stats(float("inf"), -22.0, 90.0, 40, 0))]
    out = sa.format_matrix(rows)
    assert "ETHUSDT" in out
    assert "inf" in out.lower()


def test_run_single_sets_config_and_disables_wfo():
    captured = {}

    def fake_run(df_5m, df_1h, initial_balance=1000.0, mode="1h"):
        captured["atr_ratio"] = config.ATR_RATIO_MIN
        captured["wfo"] = config.WFO_ENABLED
        captured["risk"] = config.RISK_PERCENT
        captured["tp"] = config.ATR_TP_MULTIPLIER
        captured["sl"] = config.ATR_SL_MULTIPLIER
        captured["breakout"] = config.BREAKOUT_PERIOD
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
    assert captured["sl"] == 1.5
    assert captured["breakout"] == 14
    assert captured["mode"] == "1h"


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
    assert row["pf_110_train"] == 1.7
    assert row["decay"] == 0.0
    assert row["verdict"] == "PASS"


def test_parse_args_defaults():
    args = sa.parse_args([])
    assert args.days == 1825
    assert abs(args.split - 0.70) < 1e-9
    assert args.ruin_floor == -50.0
    assert args.candidates == sa.DEFAULT_CANDIDATES


def test_parse_args_overrides():
    args = sa.parse_args(["--days", "730", "--split", "0.6",
                          "--candidates", "ETHUSDT,SOLUSDT",
                          "--ruin-floor", "-30"])
    assert args.days == 730
    assert abs(args.split - 0.6) < 1e-9
    assert args.candidates == ["ETHUSDT", "SOLUSDT"]
    assert args.ruin_floor == -30.0


def test_verdict_custom_floor_overrides_default():
    # With a looser floor of -99, a -60 drawdown is NOT ruin → PASS.
    assert sa.verdict(1.0, 1.2, -60.0, ruin_floor=-99.0) == "PASS"


def test_build_row_honors_ruin_floor():
    row = sa.build_row("XYZUSDT",
                       _stats(1.0, -10.0, 40.0, 20, 10),   # s115_train
                       _stats(1.3, -12.0, 60.0, 30, 10),   # s110_train
                       _stats(1.0, -25.0, 50.0, 30, 20),   # s115_test
                       _stats(1.2, -60.0, 80.0, 40, 20),   # s110_test
                       ruin_floor=-99.0)
    assert row["verdict"] == "PASS"


def test_evaluate_asset_honors_ruin_floor():
    df_5m, df_1h = _synthetic_feeds()

    async def fake_fetch_all(symbol="BTCUSDT", days=365):
        return df_5m, df_1h

    # 1.10 run has a -60 drawdown; 1.15 baseline is benign.
    def fake_run(d5, d1, initial_balance=1000.0, mode="1h"):
        if config.ATR_RATIO_MIN == 1.10:
            return types.SimpleNamespace(stats=_stats(1.7, -60.0, 90.0, 40, 20))
        return types.SimpleNamespace(stats=_stats(1.4, -25.0, 80.0, 30, 20))

    o_fetch, o_run = fetch_data.fetch_all, backtest.run
    fetch_data.fetch_all, backtest.run = fake_fetch_all, fake_run
    try:
        # Default -50 floor → RUIN; loosened -99 floor → PASS.
        ruin_row = sa.evaluate_asset("AAAUSDT", days=120, frac=0.7)
        pass_row = sa.evaluate_asset("BBBUSDT", days=120, frac=0.7, ruin_floor=-99.0)
    finally:
        fetch_data.fetch_all, backtest.run = o_fetch, o_run

    assert ruin_row["verdict"] == "RUIN"
    assert pass_row["verdict"] == "PASS"


def test_build_row_nodata_on_zero_trades():
    # A zero-trade backtest returns a dict missing profit_factor → NODATA, no crash.
    row = sa.build_row("NOTRD",
                       _stats(1.4, -20.0, 60.0, 30, 20),   # train 1.15
                       _stats(1.6, -25.0, 90.0, 40, 20),   # train 1.10
                       {},                                  # test 1.15 — no trades
                       {})                                  # test 1.10 — no trades
    assert row["verdict"] == "NODATA"
    assert row["delta_pf"] is None


def test_format_matrix_renders_nodata_and_sorts_last():
    good = sa.build_row("GOODUS",
                        _stats(1.4, -20.0, 60.0, 30, 20), _stats(1.7, -25.0, 90.0, 45, 25),
                        _stats(1.4, -25.0, 80.0, 30, 20), _stats(1.7, -30.0, 120.0, 50, 30))
    nod = sa.build_row("NODAT",
                       _stats(1.4, -20.0, 60.0, 30, 20), _stats(1.6, -25.0, 90.0, 40, 20),
                       {}, {})
    out = sa.format_matrix([nod, good])     # NODATA passed first
    assert "NODATA" in out
    assert "—" in out                        # dash for missing numerics
    assert "PF1.10tr" in out and "Decay" in out
    # NODATA row must sort to the bottom despite being first in input
    assert out.index("GOODUS") < out.index("NODAT")


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
    test_evaluate_asset_orchestrates_four_runs,
    test_parse_args_defaults,
    test_parse_args_overrides,
    test_verdict_custom_floor_overrides_default,
    test_build_row_honors_ruin_floor,
    test_evaluate_asset_honors_ruin_floor,
    test_build_row_nodata_on_zero_trades,
    test_format_matrix_renders_nodata_and_sorts_last,
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
