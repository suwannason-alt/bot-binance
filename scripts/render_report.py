"""
Render the BTCUSDT 1H STEP-trail optimization report to ``result.png``.

Companion to ``scripts/optimize_btc_step_1h.py``.  The full 1,344-combo sweep takes
~35 min, so this renderer does NOT re-run it — the swept Top-3 / STEP-dominance figures
are embedded as the recorded results.  What it DOES compute live (≈12 s) is the genuine
artifact: the recommended-config 5-year equity curve and the TRAIL_ACTIVATE_ATR
sensitivity strip, both via the parity-proven ``backtest.run`` at the locked RISK=4%.

Output: ``<repo_root>/result.png`` (GitHub-dark theme, 150 dpi).

Usage::

    python scripts/render_report.py
"""
from __future__ import annotations

# ── Path bootstrap (modular layout — keep flat imports resolvable) ─────────────
import sys
import pathlib
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _seg in ("", "src/core", "src/core/shared", "src/core/strategy_1h", "backtesting", "scripts"):
    _dir = str(_REPO_ROOT / _seg) if _seg else str(_REPO_ROOT)
    if _dir not in sys.path:
        sys.path.insert(0, _dir)

import logging
logging.disable(logging.WARNING)

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import matplotlib.dates as mdates
import pandas as pd

import backtest
import config_1h as config

# ── GitHub-dark palette (matches backtesting/visualize.py) ─────────────────────
BG, PANEL_BG, BORDER = "#0d1117", "#161b22", "#30363d"
TEXT, MUTED          = "#c9d1d9", "#8b949e"
BLUE, GREEN, RED     = "#58a6ff", "#3fb950", "#f85149"
YELLOW, PURPLE       = "#d29922", "#bc8cff"

RISK = 4.0

# ── Recorded full-sweep results (from optimize_btc_step_1h.py, 1,344 backtests) ─
# Top 3 by MaxDD-min within the gated universe (PF≥1.2, net>0, trades≥floor).
TOP3 = [
    # BP, ADX, TP, SL, STEP, trades, win%, PF, MaxDD%, Net$, CAGR%, "TP/SL/BE"
    (10, 34, 5.0, 2.5, 1.5, 118, 32.2, 1.22, -18.66, 183, 3.42, "9/28/81"),
    (10, 34, 4.0, 2.5, 1.5, 120, 32.5, 1.23, -18.69, 207, 3.84, "19/28/73"),
    (14, 34, 4.0, 2.5, 1.5, 114, 31.6, 1.23, -18.74, 197, 3.66, "18/26/70"),
]
# Baseline (unoptimized: BP14/ADX20/TP6/SL1.5, activate=1.0/step=1.0) for contrast.
BASELINE = dict(pf=1.23, dd=-35.4, net=474, cagr=8.1)

# Recommended "knee" config (best risk-adjusted; ACTIVATE resolved by the strip below).
KNEE = dict(BP=10, ADX=34.0, TP=4.0, SL=2.0, STEP=1.5, ACTIVATE=1.5)
ACTIVATE_GRID = [1.0, 1.5, 2.0, 2.5, 3.0]


def _pin() -> None:
    config.WFO_ENABLED = False
    config.STEP_TRAILING_ENABLED = True
    config.ADAPTIVE_TRAILING_ENABLED = False
    config.EQUITY_PERCENT = 0.0
    config.ORDER_BALANCE_USD = 0.0
    config.RISK_USD = 0.0
    config.RISK_PERCENT = RISK
    config.DAILY_PROFIT_TARGET_USD = 0.0
    config.DAILY_LOSS_LIMIT_USD = 0.0
    config.DAILY_PROFIT_TARGET_PCT = 0.0
    config.DAILY_LOSS_LIMIT_PCT = 0.0
    config.BREAKOUT_PERIOD = KNEE["BP"]
    config.ADX_MIN = KNEE["ADX"]
    config.ATR_TP_MULTIPLIER = KNEE["TP"]
    config.ATR_SL_MULTIPLIER = KNEE["SL"]
    config.TRAIL_STEP_ATR = KNEE["STEP"]
    config.MIN_CANDLES_1H = max(config.MIN_CANDLES_1H, config.EMA_TREND + 10)


def _compute_live():
    """Run the knee config + the ACTIVATE sensitivity strip; return (result, strip)."""
    df5 = pd.read_csv(_REPO_ROOT / "data" / "btcusdt_5m.csv")
    df1 = pd.read_csv(_REPO_ROOT / "data" / "btcusdt_1h.csv")
    _pin()
    strip = []
    knee_res = None
    for act in ACTIVATE_GRID:
        config.TRAIL_ACTIVATE_ATR = act
        res = backtest.run(df5, df1, initial_balance=1000.0, mode="1h")
        s = res.stats
        strip.append((act, s["total_trades"], s["win_rate"], s["profit_factor"],
                      s["max_drawdown_pct"], s["final_balance"] - 1000.0, s["cagr_pct"],
                      f"{s['tp_exits']}/{s['sl_exits']}/{s['be_exits']}"))
        if act == KNEE["ACTIVATE"]:
            knee_res = res
    return knee_res, strip, (df5, df1)


# ── Table drawing helper ──────────────────────────────────────────────────────
def _draw_table(ax, title, col_labels, rows, col_x, highlight=None, row_colors=None):
    ax.set_facecolor(PANEL_BG)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    for sp in ax.spines.values():
        sp.set_visible(False)
    ax.set_title(title, color=TEXT, fontsize=10, pad=8, loc="left", fontweight="bold")
    n = len(rows)
    top = 0.90
    row_h = top / (n + 1)
    # header
    for x, lab in zip(col_x, col_labels):
        ax.text(x, top, lab, color=MUTED, fontsize=7.5, fontweight="bold",
                ha="right", va="center", family="monospace")
    ax.plot([0.01, 0.99], [top - row_h * 0.5] * 2, color=BORDER, lw=0.8)
    for r, row in enumerate(rows):
        y = top - row_h * (r + 1)
        if highlight is not None and r == highlight:
            ax.add_patch(plt.Rectangle((0.01, y - row_h * 0.42), 0.98, row_h * 0.84,
                                       color="#1f6feb22", ec=BLUE, lw=0.8, zorder=0))
        rc = (row_colors[r] if row_colors else TEXT)
        for x, cell in zip(col_x, row):
            ax.text(x, y, str(cell), color=rc, fontsize=8, ha="right", va="center",
                    family="monospace")


def main() -> None:
    print("Computing recommended-config equity curve + ACTIVATE strip (RISK=4%)…")
    knee_res, strip, _ = _compute_live()
    eq = knee_res.equity_curve
    s = knee_res.stats
    t0, tN = eq.index[0], eq.index[-1]

    fig = plt.figure(figsize=(15, 16), facecolor=BG)
    gs = gridspec.GridSpec(
        4, 2, figure=fig, height_ratios=[0.5, 2.3, 1.5, 1.4],
        hspace=0.42, wspace=0.12, left=0.05, right=0.97, top=0.95, bottom=0.04,
    )

    # ── Banner ────────────────────────────────────────────────────────────────
    axb = fig.add_subplot(gs[0, :]); axb.axis("off")
    axb.text(0.0, 0.78, "BTCUSDT · 1H STEP-Trail Optimization", color=TEXT,
             fontsize=20, fontweight="bold", va="center")
    axb.text(0.0, 0.30,
             f"RISK locked 4.0%  ·  {t0.date()} → {tN.date()} (4.99 yr)  ·  "
             f"1,344 backtests (768 broad + 576 boundary-extend)  ·  WFO off · caps off · STEP trail on",
             color=MUTED, fontsize=10, va="center")
    axb.text(0.999, 0.78, "result.png", color=BORDER, fontsize=10, va="center", ha="right",
             family="monospace")

    # ── Equity curve (recommended knee config, ACTIVATE=1.5) ──────────────────
    axe = fig.add_subplot(gs[1, :])
    axe.set_facecolor(PANEL_BG)
    for sp in axe.spines.values():
        sp.set_color(BORDER)
    axe.plot(eq.index, eq.values, color=GREEN, lw=1.4)
    axe.fill_between(eq.index, 1000.0, eq.values, color=GREEN, alpha=0.07)
    axe.axhline(1000.0, color=MUTED, lw=0.6, ls="--")
    axe.set_yscale("log")
    axe.set_title(
        f"Recommended config equity  —  BP{KNEE['BP']} / ADX{KNEE['ADX']:.0f} / "
        f"TP{KNEE['TP']:.0f} / SL{KNEE['SL']:.1f} / STEP{KNEE['STEP']:.1f} / "
        f"ACTIVATE{KNEE['ACTIVATE']:.1f}   "
        f"→  PF {s['profit_factor']:.2f} · MaxDD {s['max_drawdown_pct']:.1f}% · "
        f"CAGR {s['cagr_pct']:.1f}% · ${s['final_balance']:,.0f}",
        color=TEXT, fontsize=10.5, pad=8, loc="left", fontweight="bold")
    axe.tick_params(colors=MUTED, labelsize=8)
    axe.grid(color=BORDER, lw=0.4, ls="--", alpha=0.6)
    axe.xaxis.set_major_locator(mdates.YearLocator())
    axe.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    axe.set_ylabel("Equity (USDT, log)", color=MUTED, fontsize=8)
    axe.text(0.012, 0.93,
             f"WR {s['win_rate']:.0f}%   exits TP/SL/BE = "
             f"{s['tp_exits']}/{s['sl_exits']}/{s['be_exits']}   "
             f"({s['be_exits']*100//max(s['total_trades'],1)}% exit at BE-or-locked via ladder)",
             transform=axe.transAxes, color=MUTED, fontsize=8.5, va="top",
             family="monospace",
             bbox=dict(boxstyle="round,pad=0.4", fc=BG, ec=BORDER, lw=0.6))

    # ── Top-3 DD-min table ────────────────────────────────────────────────────
    ax1 = fig.add_subplot(gs[2, 0])
    cols = ["#", "BP", "ADX", "TP", "SL", "STEP", "Trd", "Win%", "PF", "MaxDD%", "Net$", "CAGR%"]
    colx = [0.05, 0.12, 0.20, 0.28, 0.36, 0.46, 0.56, 0.66, 0.74, 0.86, 0.95, 1.00]
    rows, rcolors = [], []
    for i, r in enumerate(TOP3, 1):
        rows.append([i, r[0], f"{r[1]:.0f}", f"{r[2]:.0f}", f"{r[3]:.1f}", f"{r[4]:.1f}",
                     r[5], f"{r[6]:.1f}", f"{r[7]:.2f}", f"{r[8]:.1f}", f"+{r[9]}", f"{r[10]:.1f}"])
        rcolors.append(TEXT)
    rows.append(["base", 14, "20", "6", "1.5", "1.0", "222", "35", f"{BASELINE['pf']:.2f}",
                 f"{BASELINE['dd']:.1f}", f"+{BASELINE['net']}", f"{BASELINE['cagr']:.1f}"])
    rcolors.append(MUTED)
    _draw_table(ax1, "Top 3 — MaxDD-min (gated)  vs  baseline",
                cols, rows, colx, row_colors=rcolors)
    ax1.text(0.01, -0.02, "⚠ literal DD-min is degenerate: net→~$190, CAGR ~3.5% (capital-preserve corner)",
             transform=ax1.transAxes, color=YELLOW, fontsize=7.5, va="top")

    # ── ACTIVATE sensitivity table ────────────────────────────────────────────
    ax2 = fig.add_subplot(gs[2, 1])
    cols2 = ["ACT", "Trd", "Win%", "PF", "MaxDD%", "Net$", "CAGR%", "TP/SL/BE"]
    colx2 = [0.10, 0.22, 0.34, 0.44, 0.58, 0.70, 0.82, 1.00]
    rows2, rc2 = [], []
    hl = None
    for idx, (act, trd, wr, pf, dd, net, cagr, exits) in enumerate(strip):
        rows2.append([f"{act:.1f}", trd, f"{wr:.1f}", f"{pf:.2f}", f"{dd:.1f}",
                      f"+{net:.0f}", f"{cagr:.1f}", exits])
        if act == KNEE["ACTIVATE"]:
            hl = idx; rc2.append(GREEN)
        else:
            rc2.append(TEXT)
    _draw_table(ax2, "TRAIL_ACTIVATE_ATR sensitivity @ knee (the master lever)",
                cols2, rows2, colx2, highlight=hl, row_colors=rc2)
    ax2.text(0.01, -0.02, "1.0→1.5 DOUBLES profit at identical DD; ≥2.0 DD climbs faster than return",
             transform=ax2.transAxes, color=GREEN, fontsize=7.5, va="top")

    # ── Findings / mechanism / honesty panel ──────────────────────────────────
    axf = fig.add_subplot(gs[3, :]); axf.axis("off"); axf.set_facecolor(PANEL_BG)
    axf.add_patch(plt.Rectangle((0, 0), 1, 1, transform=axf.transAxes,
                                fc=PANEL_BG, ec=BORDER, lw=0.8))
    lines = [
        ("KEY FINDINGS", TEXT, True),
        ("• TRAIL_STEP_ATR = 1.5 is the universal ratchet optimum — won every lens in BOTH stages "
         "(beat {0.5,1.0} broad, then {2.0,2.5,3.0} extended).", TEXT, False),
        ("• TRAIL_ACTIVATE_ATR (not in the swept 5; fixed=1.0 for the grid) is the real DD/return lever — "
         "1.5 doubles profit at the SAME −19.7% drawdown (= current live value).", TEXT, False),
        ("• Mechanism: at 4% risk a full SL costs 4% of balance. The ladder moves SL to BE after "
         "1.5×ATR then locks +1.5×ATR/step, so ~40% of trades exit at BE-or-better instead of −4%.", BLUE, False),
        ("  Win rate stays ~32–37% — the edge is a SHRUNK AVERAGE LOSS, not better direction. DD halves vs the −35% baseline.", BLUE, False),
        ("HONESTY: in-sample · single-asset (BTCUSDT) · best-of-1,344 · one contiguous 5yr window · no OOS/walk-forward. "
         "Mechanism robust; numbers are a fit. NOT shipped (btc/config.py untouched; TRAIL_STEP_ATR default=1.0).", MUTED, False),
    ]
    y = 0.90
    for txt, col, bold in lines:
        axf.text(0.02, y, txt, transform=axf.transAxes, color=col,
                 fontsize=9 if not bold else 9.5, va="top",
                 fontweight="bold" if bold else "normal", wrap=True)
        y -= 0.135 if not bold else 0.15

    out = _REPO_ROOT / "result.png"
    fig.savefig(out, dpi=150, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    print(f"✓ wrote {out}")


if __name__ == "__main__":
    main()
