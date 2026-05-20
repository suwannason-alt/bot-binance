"""
Backtest visualization — dark-themed multi-panel report.

Panels:
  1. BTC 1H price with long/short entry markers and TP/SL exit markers
  2. Equity curve with profit/loss fill vs. starting balance
  3. Drawdown chart (% from equity peak)
  4. Monthly returns bar chart
  5. Stats summary text box

Saves: backtest_results/report.png
"""
from __future__ import annotations

import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")  # non-interactive backend; switch to TkAgg/Qt5Agg for live window
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd

from backtest import BacktestResult

OUTPUT_DIR = "backtest_results"

# ── Colour palette (GitHub dark) ──────────────────────────────────────────────
BG       = "#0d1117"
PANEL_BG = "#161b22"
BORDER   = "#30363d"
TEXT     = "#c9d1d9"
MUTED    = "#8b949e"
BLUE     = "#58a6ff"
GREEN    = "#3fb950"
RED      = "#f85149"
YELLOW   = "#d29922"


def _style_ax(ax, title: str = ""):
    ax.set_facecolor(PANEL_BG)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
    ax.tick_params(colors=MUTED, labelsize=7)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    if title:
        ax.set_title(title, color=TEXT, fontsize=9, pad=6)
    ax.grid(axis="y", color=BORDER, linewidth=0.4, linestyle="--")
    ax.grid(axis="x", color=BORDER, linewidth=0.2, linestyle=":")


def plot(result: BacktestResult, show: bool = False) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    trades    = result.trades
    equity    = result.equity_curve
    df_1h     = result.df_1h
    stats     = result.stats
    initial   = stats.get("initial_balance", 1000.0)

    # ── Build figure ──────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(20, 16), facecolor=BG)
    gs  = gridspec.GridSpec(
        3, 2,
        figure=fig,
        hspace=0.45,
        wspace=0.3,
        left=0.06, right=0.97,
        top=0.93,  bottom=0.10,
    )

    ax_price  = fig.add_subplot(gs[0, :])   # full-width price chart
    ax_equity = fig.add_subplot(gs[1, :])   # full-width equity curve
    ax_dd     = fig.add_subplot(gs[2, 0])   # drawdown
    ax_month  = fig.add_subplot(gs[2, 1])   # monthly returns

    for ax in [ax_price, ax_equity, ax_dd, ax_month]:
        _style_ax(ax)

    # ── 1. Price chart ────────────────────────────────────────────────────────
    times_1h = pd.to_datetime(df_1h["close_time"], unit="ms")
    ax_price.plot(times_1h, df_1h["close"], color=BLUE, lw=0.7, label="BTCUSDT 1H")

    for t in trades:
        entry_color = GREEN if t.side == "LONG" else RED
        entry_marker = "^" if t.side == "LONG" else "v"
        ax_price.scatter(t.entry_time, t.entry_price,
                         color=entry_color, marker=entry_marker, s=35, zorder=5,
                         edgecolors="none")
        exit_color = GREEN if t.close_reason == "TP" else (RED if t.close_reason == "SL" else YELLOW)
        ax_price.scatter(t.exit_time, t.exit_price,
                         color=exit_color, marker="o", s=15, zorder=5, alpha=0.8,
                         edgecolors="none")

    # Legend for markers
    ax_price.legend(handles=[
        mpatches.Patch(color=GREEN, label="Long entry"),
        mpatches.Patch(color=RED,   label="Short entry"),
        mpatches.Patch(color=GREEN, label="TP exit"),
        mpatches.Patch(color=RED,   label="SL exit"),
    ], fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER, labelcolor=TEXT, loc="upper left")
    ax_price.set_ylabel("Price (USDT)", color=MUTED, fontsize=8)
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    _style_ax(ax_price, f"BTCUSDT — {len(trades)} trades over backtest period")

    # ── 2. Equity curve ───────────────────────────────────────────────────────
    ax_equity.plot(equity.index, equity.values, color=GREEN, lw=1.3, zorder=3)
    ax_equity.axhline(initial, color=MUTED, lw=0.6, linestyle="--")
    ax_equity.fill_between(equity.index, initial, equity.values,
                           where=(equity.values >= initial), alpha=0.18, color=GREEN)
    ax_equity.fill_between(equity.index, initial, equity.values,
                           where=(equity.values < initial), alpha=0.18, color=RED)

    final = equity.iloc[-1]
    ret_pct = (final - initial) / initial * 100
    ret_color = GREEN if ret_pct >= 0 else RED
    ax_equity.set_ylabel("Balance (USDT)", color=MUTED, fontsize=8)
    ax_equity.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    _style_ax(
        ax_equity,
        f"Equity Curve — Start: ${initial:,.0f}  →  "
        f"Final: ${final:,.2f}  ({ret_pct:+.1f}%)",
    )
    ax_equity.title.set_color(ret_color)

    # ── 3. Drawdown ───────────────────────────────────────────────────────────
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max * 100
    ax_dd.fill_between(dd.index, dd.values, 0, color=RED, alpha=0.45)
    ax_dd.plot(dd.index, dd.values, color=RED, lw=0.7)
    ax_dd.axhline(0, color=BORDER, lw=0.5)
    ax_dd.set_ylabel("%", color=MUTED, fontsize=8)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%b %y"))
    ax_dd.tick_params(axis="x", labelrotation=30)
    _style_ax(ax_dd, f"Drawdown   Max: {dd.min():.1f}%")

    # ── 4. Monthly returns ────────────────────────────────────────────────────
    monthly = equity.resample("ME").last().pct_change().mul(100).dropna()
    bar_colors = [GREEN if v >= 0 else RED for v in monthly.values]
    ax_month.bar(range(len(monthly)), monthly.values, color=bar_colors, width=0.7)
    ax_month.axhline(0, color=MUTED, lw=0.5)
    labels = [d.strftime("%b\n%y") for d in monthly.index]
    ax_month.set_xticks(range(len(labels)))
    ax_month.set_xticklabels(labels, fontsize=6.5, color=MUTED)
    ax_month.set_ylabel("%", color=MUTED, fontsize=8)
    positive_months = (monthly.values > 0).sum()
    _style_ax(ax_month, f"Monthly Returns  ({positive_months}/{len(monthly)} positive)")

    # ── 5. Stats box ──────────────────────────────────────────────────────────
    s = stats
    col1 = (
        f"Total Trades :  {s.get('total_trades', 0)}\n"
        f"Win Rate     :  {s.get('win_rate', 0):.1f}%\n"
        f"TP exits     :  {s.get('tp_exits', 0)}\n"
        f"SL exits     :  {s.get('sl_exits', 0)}\n"
        f"Avg bars held:  {s.get('avg_bars_held', 0):.0f}"
    )
    col2 = (
        f"Total Return :  {s.get('total_return_pct', 0):+.2f}%\n"
        f"CAGR         :  {s.get('cagr_pct', 0):+.2f}%\n"
        f"Max Drawdown :  {s.get('max_drawdown_pct', 0):.2f}%\n"
        f"Sharpe Ratio :  {s.get('sharpe', 0):.2f}\n"
        f"Profit Factor:  {s.get('profit_factor', 0):.2f}"
    )
    col3 = (
        f"Gross Profit :  ${s.get('gross_profit', 0):,.2f}\n"
        f"Gross Loss   :  ${s.get('gross_loss', 0):,.2f}\n"
        f"Avg Win      :  ${s.get('avg_win', 0):+.2f}\n"
        f"Avg Loss     :  ${s.get('avg_loss', 0):+.2f}\n"
        f"Best / Worst :  ${s.get('best_trade', 0):+.2f} / ${s.get('worst_trade', 0):+.2f}"
    )

    fig.text(0.06, 0.04, col1, color=TEXT, fontsize=8, fontfamily="monospace",
             va="top", bbox=dict(boxstyle="round,pad=0.5", facecolor=PANEL_BG, edgecolor=BORDER))
    fig.text(0.37, 0.04, col2, color=TEXT, fontsize=8, fontfamily="monospace",
             va="top", bbox=dict(boxstyle="round,pad=0.5", facecolor=PANEL_BG, edgecolor=BORDER))
    fig.text(0.68, 0.04, col3, color=TEXT, fontsize=8, fontfamily="monospace",
             va="top", bbox=dict(boxstyle="round,pad=0.5", facecolor=PANEL_BG, edgecolor=BORDER))

    period_start = equity.index[0].strftime("%Y-%m-%d")
    period_end   = equity.index[-1].strftime("%Y-%m-%d")
    fig.suptitle(
        f"Backtest Report  |  BTCUSDT  |  {period_start} → {period_end}  "
        f"|  Start: ${initial:,.0f}  |  Leverage: {config.LEVERAGE}×  "
        f"|  Risk/trade: {config.RISK_PERCENT}%",
        color=TEXT, fontsize=10, y=0.975,
    )

    outpath = os.path.join(OUTPUT_DIR, "report.png")
    plt.savefig(outpath, dpi=150, bbox_inches="tight", facecolor=BG)
    if show:
        plt.show()
    plt.close(fig)
    return outpath


import config  # local import to avoid circular at module level
