"""
Backtest visualization — dark-themed multi-panel report.

Panels:
  Hero banner — Starting wallet → Final balance | Total return | Net profit
  1. BTC 1H price with trade entry/exit markers
  2. Equity curve (wallet balance over time)
  3. Drawdown chart
  4. Daily P&L bars with target line
  5. Stats summary text boxes

Saves: backtest_results/report.png
"""
from __future__ import annotations

import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
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
PURPLE   = "#bc8cff"


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


def plot(result: BacktestResult, show: bool = False,
         year_marks: list | None = None,
         year_balances: list | None = None) -> str:
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    trades  = result.trades
    equity  = result.equity_curve
    df_1h   = result.df_1h
    stats   = result.stats
    initial = stats.get("initial_balance", 1000.0)
    final   = equity.iloc[-1]
    net     = final - initial
    ret_pct = (final - initial) / initial * 100
    ret_color = GREEN if ret_pct >= 0 else RED

    period_start = equity.index[0].strftime("%Y-%m-%d")
    period_end   = equity.index[-1].strftime("%Y-%m-%d")
    n_days = (equity.index[-1] - equity.index[0]).days

    # ── Figure layout ─────────────────────────────────────────────────────────
    # Row 0: hero banner (thin)
    # Row 1: price chart (tall)
    # Row 2: equity curve (tall)
    # Row 3: drawdown | daily P&L (medium)
    fig = plt.figure(figsize=(22, 20), facecolor=BG)
    gs = gridspec.GridSpec(
        4, 2,
        figure=fig,
        height_ratios=[0.55, 2.2, 2.2, 1.8],
        hspace=0.48,
        wspace=0.28,
        left=0.06, right=0.97,
        top=0.96, bottom=0.06,
    )

    ax_hero   = fig.add_subplot(gs[0, :])
    ax_price  = fig.add_subplot(gs[1, :])
    ax_equity = fig.add_subplot(gs[2, :])
    ax_dd     = fig.add_subplot(gs[3, 0])
    ax_daily  = fig.add_subplot(gs[3, 1])

    # ── 0. Hero banner ────────────────────────────────────────────────────────
    ax_hero.set_facecolor(PANEL_BG)
    for spine in ax_hero.spines.values():
        spine.set_color(BORDER)
    ax_hero.set_xticks([])
    ax_hero.set_yticks([])

    # Starting wallet
    ax_hero.text(0.01, 0.78, "STARTING BALANCE", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=7.5, fontweight="bold", va="top")
    ax_hero.text(0.01, 0.32, f"${initial:,.2f}", transform=ax_hero.transAxes,
                 color=TEXT, fontsize=20, fontweight="bold", va="top", fontfamily="monospace")

    # Arrow
    ax_hero.text(0.195, 0.32, "→", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=22, va="top")

    # Final balance
    ax_hero.text(0.23, 0.78, "FINAL BALANCE", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=7.5, fontweight="bold", va="top")
    ax_hero.text(0.23, 0.32, f"${final:,.2f}", transform=ax_hero.transAxes,
                 color=ret_color, fontsize=20, fontweight="bold", va="top", fontfamily="monospace")

    # Net profit
    ax_hero.text(0.46, 0.78, "NET PROFIT", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=7.5, fontweight="bold", va="top")
    ax_hero.text(0.46, 0.32, f"${net:+,.2f}", transform=ax_hero.transAxes,
                 color=ret_color, fontsize=20, fontweight="bold", va="top", fontfamily="monospace")

    # Growth rate
    ax_hero.text(0.645, 0.78, "GROWTH RATE", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=7.5, fontweight="bold", va="top")
    ax_hero.text(0.645, 0.32, f"{ret_pct:+.2f}%", transform=ax_hero.transAxes,
                 color=ret_color, fontsize=20, fontweight="bold", va="top", fontfamily="monospace")

    # CAGR
    ax_hero.text(0.80, 0.78, "CAGR", transform=ax_hero.transAxes,
                 color=MUTED, fontsize=7.5, fontweight="bold", va="top")
    ax_hero.text(0.80, 0.32, f"{stats.get('cagr_pct', 0):+.2f}%/yr", transform=ax_hero.transAxes,
                 color=ret_color, fontsize=20, fontweight="bold", va="top", fontfamily="monospace")

    # Period
    ax_hero.text(0.01, 0.01, f"Period: {period_start} → {period_end}  ({n_days} days)",
                 transform=ax_hero.transAxes, color=MUTED, fontsize=7.5, va="bottom")

    # Key metrics in one line
    wrate = stats.get("win_rate", 0)
    mdd   = stats.get("max_drawdown_pct", 0)
    pf    = stats.get("profit_factor", 0)
    sharpe = stats.get("sharpe", 0)
    dph   = stats.get("days_profit_hit", 0)
    dtot  = stats.get("total_days", 0)
    ax_hero.text(
        0.23, 0.01,
        f"Trades: {stats.get('total_trades',0)}  |  Win Rate: {wrate:.1f}%  |  "
        f"Max DD: {mdd:.1f}%  |  Sharpe: {sharpe:.2f}  |  PF: {pf:.2f}  |  "
        f"Profit Days: {dph}/{dtot} ({dph/dtot*100:.1f}%)" if dtot else "",
        transform=ax_hero.transAxes, color=TEXT, fontsize=7.5, va="bottom",
    )

    # ── 1. Price chart ────────────────────────────────────────────────────────
    _style_ax(ax_price)
    times_1h = pd.to_datetime(df_1h["close_time"], unit="ms")
    ax_price.plot(times_1h, df_1h["close"], color=BLUE, lw=0.6, label="BTC/USDT 1H", alpha=0.9)

    long_e = [(t.entry_time, t.entry_price) for t in trades if t.side == "LONG"]
    short_e = [(t.entry_time, t.entry_price) for t in trades if t.side == "SHORT"]
    tp_ex = [(t.exit_time, t.exit_price) for t in trades if t.close_reason == "TP"]
    sl_ex = [(t.exit_time, t.exit_price) for t in trades if t.close_reason == "SL"]

    if long_e:
        ax_price.scatter(*zip(*long_e), color=GREEN, marker="^", s=28, zorder=5, edgecolors="none", alpha=0.85)
    if short_e:
        ax_price.scatter(*zip(*short_e), color=RED, marker="v", s=28, zorder=5, edgecolors="none", alpha=0.85)
    if tp_ex:
        ax_price.scatter(*zip(*tp_ex), color=GREEN, marker="o", s=12, zorder=5, alpha=0.65, edgecolors="none")
    if sl_ex:
        ax_price.scatter(*zip(*sl_ex), color=RED, marker="x", s=18, zorder=5, alpha=0.65, linewidths=0.8)

    # Year boundaries on price chart + per-year LONG/SHORT counts
    if year_marks:
        boundaries = [equity.index[0]] + list(year_marks) + [equity.index[-1]]
        count_lines = []
        for yi in range(len(boundaries) - 1):
            t_start = boundaries[yi]
            t_end   = boundaries[yi + 1]
            label   = f"Y{yi + 1}"
            yt = [t for t in trades if t_start <= t.entry_time < t_end]
            n_long  = sum(1 for t in yt if t.side == "LONG")
            n_short = sum(1 for t in yt if t.side == "SHORT")
            count_lines.append(f"{label}:  {n_long:>3} LONG  {n_short:>3} SHORT  ({n_long + n_short} total)")
        for ym in year_marks:
            ax_price.axvline(ym, color=YELLOW, lw=0.9, linestyle="--", alpha=0.55)
        ax_price.text(
            0.01, 0.02, "\n".join(count_lines),
            transform=ax_price.transAxes,
            color=TEXT, fontsize=7.5, va="bottom", ha="left",
            fontfamily="monospace",
            bbox=dict(boxstyle="round,pad=0.45", facecolor=PANEL_BG,
                      edgecolor=BORDER, alpha=0.90),
        )

    ax_price.legend(handles=[
        mpatches.Patch(color=GREEN, label="Long entry / TP exit"),
        mpatches.Patch(color=RED,   label="Short entry / SL exit"),
    ], fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER, labelcolor=TEXT, loc="upper left")
    ax_price.set_ylabel("Price (USDT)", color=MUTED, fontsize=8)
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax_price.set_title(
        f"BTCUSDT Price  |  {len(trades)} trades ({len(long_e)} Long, {len(short_e)} Short)",
        color=TEXT, fontsize=9, pad=6,
    )

    # ── 2. Equity / Wallet curve ──────────────────────────────────────────────
    _style_ax(ax_equity)
    ax_equity.plot(equity.index, equity.values, color=GREEN, lw=1.5, zorder=3, label="Wallet Balance")
    ax_equity.axhline(initial, color=MUTED, lw=0.7, linestyle="--", alpha=0.7, label=f"Start ${initial:,.0f}")
    ax_equity.fill_between(equity.index, initial, equity.values,
                           where=(equity.values >= initial), alpha=0.20, color=GREEN)
    ax_equity.fill_between(equity.index, initial, equity.values,
                           where=(equity.values < initial), alpha=0.20, color=RED)

    # Annotate final value on chart
    ax_equity.annotate(
        f"  ${final:,.2f}\n  ({ret_pct:+.1f}%)",
        xy=(equity.index[-1], final),
        color=ret_color, fontsize=8, fontweight="bold", va="center",
    )

    # Monthly milestones
    monthly = equity.resample("ME").last()
    ax_equity.scatter(monthly.index, monthly.values,
                      color=YELLOW, s=18, zorder=4, alpha=0.7, edgecolors="none")

    # Year markers on equity curve (works for any number of years)
    if year_marks and year_balances and len(year_balances) >= 2:
        n_years   = len(year_balances) - 1
        year_ends = list(year_marks) + [equity.index[-1]]
        year_bals = year_balances[1:]            # end balance for each year
        year_labels = [f"Y{i+1}" for i in range(n_years)]
        for i, (ym, yb, yl) in enumerate(zip(year_ends, year_bals, year_labels)):
            ax_equity.axvline(ym, color=YELLOW, lw=0.8, linestyle="--", alpha=0.6)
            b_prev = year_balances[i]
            r = (yb - b_prev) / b_prev * 100
            c = GREEN if r >= 0 else RED
            offset_x = -52 if i % 2 == 0 else 6
            ax_equity.annotate(
                f"{yl}: ${yb:,.0f}\n({r:+.0f}%)",
                xy=(ym, yb), xytext=(offset_x, 12),
                textcoords="offset points",
                color=c, fontsize=7, fontweight="bold",
                arrowprops=dict(arrowstyle="-", color=c, lw=0.6),
            )

    ax_equity.set_ylabel("Wallet Balance (USDT)", color=MUTED, fontsize=8)
    ax_equity.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax_equity.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER, labelcolor=TEXT)
    ax_equity.set_title(
        f"Wallet Balance  |  ${initial:,.0f}  →  ${final:,.2f}  ({ret_pct:+.2f}%  |  Net: ${net:+,.2f})",
        color=ret_color, fontsize=9, pad=6,
    )

    # ── 3. Drawdown ───────────────────────────────────────────────────────────
    _style_ax(ax_dd)
    roll_max = equity.cummax()
    dd = (equity - roll_max) / roll_max * 100
    ax_dd.fill_between(dd.index, dd.values, 0, color=RED, alpha=0.45)
    ax_dd.plot(dd.index, dd.values, color=RED, lw=0.7)
    ax_dd.axhline(0, color=BORDER, lw=0.5)
    ax_dd.set_ylabel("Drawdown %", color=MUTED, fontsize=8)
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%b '%y"))
    ax_dd.tick_params(axis="x", labelrotation=30)
    ax_dd.set_title(f"Drawdown  |  Max: {dd.min():.1f}%", color=TEXT, fontsize=9, pad=6)

    # ── 4. Daily P&L bars ─────────────────────────────────────────────────────
    _style_ax(ax_daily)
    daily_pnl = result.daily_pnl
    if len(daily_pnl) > 0:
        bar_colors = [GREEN if v >= 0 else RED for v in daily_pnl.values]
        ax_daily.bar(range(len(daily_pnl)), daily_pnl.values, color=bar_colors, width=0.8, alpha=0.75)
        # daily $20 profit target line
        target_usd = config.DAILY_PROFIT_TARGET_USD
        if target_usd > 0 and len(daily_pnl) > 0:
            # convert to % for chart: approximate using initial balance
            approx_target_pct = target_usd / initial * 100
            ax_daily.axhline(approx_target_pct, color=YELLOW, lw=1.1, linestyle="--",
                             label=f"+${target_usd:.0f} daily target")
        ax_daily.axhline(0, color=MUTED, lw=0.5)
        n_bars = len(daily_pnl)
        step = max(1, n_bars // 16)
        tick_idx = list(range(0, n_bars, step))
        tick_labels = [daily_pnl.index[k].strftime("%b\n'%y") for k in tick_idx]
        ax_daily.set_xticks(tick_idx)
        ax_daily.set_xticklabels(tick_labels, fontsize=6, color=MUTED)
        ax_daily.set_ylabel("Daily P&L %", color=MUTED, fontsize=8)
        ax_daily.legend(fontsize=7, facecolor=PANEL_BG, edgecolor=BORDER, labelcolor=TEXT)

        dph_  = stats.get("days_profit_hit", 0)
        dlh_  = stats.get("days_loss_hit", 0)
        dtot_ = stats.get("total_days", len(daily_pnl))
        ax_daily.set_title(
            f"Daily P&L  |  Profit days: {dph_}/{dtot_} ({dph_/dtot_*100:.1f}%)  "
            f"Loss days: {dlh_}/{dtot_}",
            color=TEXT, fontsize=9, pad=6,
        )
    else:
        ax_daily.set_title("Daily P&L", color=TEXT, fontsize=9, pad=6)

    # ── Suptitle ──────────────────────────────────────────────────────────────
    n_years_label = round((equity.index[-1] - equity.index[0]).days / 365.25)
    fig.suptitle(
        f"{n_years_label}-Year Backtest Report  |  BTCUSDT Futures  |  "
        f"{period_start} → {period_end}  |  Leverage: {config.LEVERAGE}×  "
        f"|  Risk/trade: {config.RISK_PERCENT}%",
        color=TEXT, fontsize=10.5, y=0.99, fontweight="bold",
    )

    outpath = os.path.join(OUTPUT_DIR, "report.png")
    plt.savefig(outpath, dpi=150, bbox_inches="tight", facecolor=BG)
    if show:
        plt.show()
    plt.close(fig)
    return outpath


import config
