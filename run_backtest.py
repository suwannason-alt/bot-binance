"""
Backtest runner with auto-iteration.

Runs until final balance > TARGET (default $1100) or MAX_ITERATIONS exhausted.
Each run is logged to backtest_results/backtest_log.txt.
"""
import argparse
import asyncio
import logging
import os
from datetime import datetime

import fetch_data
import backtest
import visualize

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_backtest")

LOG_DIR  = "backtest_results"
LOG_FILE = os.path.join(LOG_DIR, "backtest_log.txt")
TARGET_BALANCE = 1100.0
MAX_ITERATIONS = 12

# ── Parameter schedule ────────────────────────────────────────────────────────
# All entries are config overrides applied on top of v3 baseline.
# ATR_SL_MULTIPLIER × ATR_TP_MULTIPLIER always satisfy tp > sl.
PARAM_SCHEDULE = [
    # 1: Wider SL gives trades more room (fewer premature stops → higher WR)
    #    2.0×SL + 5.0×TP = 2.5 RR → breakeven WR 28.6%  (current WR ~32% → PF>1)
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5},
    # 2: Same wider SL with 6×TP = 3.0 RR → breakeven 25%
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 6.0, "EMA_TREND_SLOPE_BARS": 5},
    # 3: 2.5×SL + 6.0×TP = 2.4 RR, very wide room (WR should jump)
    {"ATR_SL_MULTIPLIER": 2.5, "ATR_TP_MULTIPLIER": 6.0, "EMA_TREND_SLOPE_BARS": 5},
    # 4: Wider SL + higher risk to amplify wins
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5,
     "RISK_PERCENT": 2.5},
    # 5: Narrow slope filter (3 bars) — only strongest trend momentum
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 3},
    # 6: Baseline SL + very wide TP (tests if WR holds at 6×TP)
    {"ATR_SL_MULTIPLIER": 1.5, "ATR_TP_MULTIPLIER": 6.0, "EMA_TREND_SLOPE_BARS": 5},
    # 7: Relaxed vol filter — let through more setups
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5,
     "VOL_RATIO_MIN": 0.4},
    # 8: Relax EMA sep — more signals per day (but weaker trend confirmation)
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5,
     "EMA_1H_MIN_SEP": 0.001},
    # 9: Best SL/TP + tighter RSI (only clear momentum)
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5,
     "RSI_1H_LONG_MIN": 52, "RSI_1H_SHORT_MAX": 48},
    # 10: Best SL/TP + strong RSI range [50-70]
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 5,
     "RSI_1H_LONG_MIN": 50, "RSI_1H_LONG_MAX": 70},
    # 11: Wider SL + lower vol threshold + slope=5
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 6.0, "EMA_TREND_SLOPE_BARS": 5,
     "VOL_RATIO_MIN": 0.4, "RISK_PERCENT": 2.0},
    # 12: Max combo: slope=3 + wide SL/TP + tighter RSI + 2.5% risk
    {"ATR_SL_MULTIPLIER": 2.0, "ATR_TP_MULTIPLIER": 5.0, "EMA_TREND_SLOPE_BARS": 3,
     "RSI_1H_LONG_MIN": 50, "RSI_1H_SHORT_MAX": 50, "RISK_PERCENT": 2.5},
]


def _apply_params(params: dict):
    import config
    for k, v in params.items():
        if hasattr(config, k):
            setattr(config, k, v)
    # Ensure MIN_CANDLES_1H is ≥ EMA_TREND (200)
    import config as cfg
    cfg.MIN_CANDLES_1H = max(cfg.MIN_CANDLES_1H, cfg.EMA_TREND + 10)


def _param_summary(params: dict) -> str:
    return "baseline" if not params else "  ".join(f"{k}={v}" for k, v in params.items())


def _log(text: str):
    """Write to log file and stdout."""
    os.makedirs(LOG_DIR, exist_ok=True)
    print(text)
    with open(LOG_FILE, "a") as f:
        f.write(text + "\n")


def _log_run(iteration: int, params: dict, stats: dict):
    s = stats
    ret_sign = "+" if s.get("total_return_pct", 0) >= 0 else ""
    text = (
        f"\n{'='*70}\n"
        f"  Run #{iteration:02d}   {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"  Params : {_param_summary(params)}\n"
        f"{'─'*70}\n"
        f"  Trades       : {s.get('total_trades', 0):>8}\n"
        f"  Win Rate     : {s.get('win_rate', 0):>7.1f}%"
        f"  (TP={s.get('tp_exits',0)} SL={s.get('sl_exits',0)} EOD={s.get('eod_exits',0)})\n"
        f"  Start Balance: ${s.get('initial_balance', 0):>10,.2f}\n"
        f"  End Balance  : ${s.get('final_balance', 0):>10,.2f}\n"
        f"  Total Return : {ret_sign}{s.get('total_return_pct', 0):>8.2f}%\n"
        f"  CAGR         : {s.get('cagr_pct', 0):>+8.2f}%\n"
        f"  Max Drawdown : {s.get('max_drawdown_pct', 0):>8.2f}%\n"
        f"  Sharpe Ratio : {s.get('sharpe', 0):>8.2f}\n"
        f"  Profit Factor: {s.get('profit_factor', 0):>8.2f}\n"
        f"  Gross Profit : ${s.get('gross_profit', 0):>10,.2f}\n"
        f"  Gross Loss   : ${s.get('gross_loss', 0):>10,.2f}\n"
        f"  Avg Win      : ${s.get('avg_win', 0):>+10.2f}\n"
        f"  Avg Loss     : ${s.get('avg_loss', 0):>+10.2f}\n"
        f"  Best Trade   : ${s.get('best_trade', 0):>+10.2f}\n"
        f"  Worst Trade  : ${s.get('worst_trade', 0):>+10.2f}\n"
        f"{'='*70}"
    )
    _log(text)


def _sep(msg=""):
    w = 70
    pad = (w - len(msg) - 2) // 2 if msg else 0
    line = f"{'─'*pad} {msg} {'─'*pad}" if msg else "─" * w
    _log(f"\n{line}\n")


async def main(days: int, initial_balance: float, symbol: str, target: float):
    os.makedirs(LOG_DIR, exist_ok=True)
    header = (
        f"\n{'#'*70}\n"
        f"# BACKTEST SESSION  {datetime.now():%Y-%m-%d %H:%M:%S}\n"
        f"# Symbol={symbol}  Days={days}  "
        f"StartBalance=${initial_balance:.0f}  Target=${target:.0f}\n"
        f"{'#'*70}"
    )
    _log(header)

    df_5m, df_1h = await fetch_data.fetch_all(symbol=symbol, days=days)
    d0 = datetime.fromtimestamp(df_5m["open_time"].iloc[0]  / 1000).strftime("%Y-%m-%d")
    d1 = datetime.fromtimestamp(df_5m["close_time"].iloc[-1] / 1000).strftime("%Y-%m-%d")
    logger.info(f"Period: {d0} → {d1}  |  5M={len(df_5m):,}  1H={len(df_1h):,}")

    best_result  = None
    best_balance = 0.0

    for iteration in range(1, MAX_ITERATIONS + 1):
        params = PARAM_SCHEDULE[min(iteration - 1, len(PARAM_SCHEDULE) - 1)]
        _apply_params(params)

        _sep(f"Iteration {iteration}/{MAX_ITERATIONS}")
        logger.info(f"Running backtest — {_param_summary(params)}")

        result   = backtest.run(df_5m, df_1h, initial_balance=initial_balance)
        stats    = result.stats
        final    = stats.get("final_balance", 0.0)
        n_trades = stats.get("total_trades", 0)

        _log_run(iteration, params, stats)

        if final > best_balance:
            best_balance = final
            best_result  = result

        if n_trades == 0:
            logger.warning("No trades generated — strategy filters too strict")

        if final >= target:
            _sep(f"TARGET REACHED — iteration {iteration}")
            _log(f"  Final balance : ${final:,.2f}  (target: ${target:,.2f})")
            break

        if iteration < MAX_ITERATIONS:
            logger.info(
                f"Balance ${final:.2f} < ${target:.2f} — "
                f"trying iteration {iteration + 1}"
            )
    else:
        _sep("Max iterations reached")
        msg = f"Best balance: ${best_balance:,.2f}  (target: ${target:.2f})"
        if best_balance < target:
            msg += f"\nNOTE: Target not reached. Best strategy produced ${best_balance:.2f}."
        _log(f"  {msg}")

    if best_result is not None:
        logger.info("Generating chart for best result…")
        chart_path = visualize.plot(best_result, show=False)
        _log(f"\n  Chart → {chart_path}\n  Log   → {LOG_FILE}\n")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days",    type=int,   default=365)
    p.add_argument("--balance", type=float, default=1000.0)
    p.add_argument("--symbol",  type=str,   default="BTCUSDT")
    p.add_argument("--target",  type=float, default=TARGET_BALANCE)
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(
        days=args.days,
        initial_balance=args.balance,
        symbol=args.symbol,
        target=args.target,
    ))
