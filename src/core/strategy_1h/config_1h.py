"""
1H momentum-breakout strategy configuration — HARDCODED parameters.

This module owns every tunable of the 1H breakout strategy.  Values are literals
(not ``os.getenv`` reads): strategy parameters are part of the *code*, not the
deployment ``.env``.  Server / secret / exchange values are inherited from the
shared :mod:`config` via ``from config import *`` (so ``config_1h.LEVERAGE``,
``config_1h.SYMBOL``, ``config_1h.TRADING_FEE``, the indicator periods, etc. all
resolve here exactly as before).

The 1H engine reads this module as its ``config`` (``import config_1h as config``)
and the backtest CLI / WFO write their overrides here, so live, backtest, and WFO
all agree on a single source of truth for the 1H path.

The literal values below reproduce the proven live profile (capital-preservation
trail, RISK=4%) — see the project memory ``project_live_trail_profile`` and the
``run_backtest`` ``_BASE`` dict.  Backtest sweeps override these at runtime via
``run_backtest._apply_config`` / ``compare_mtf``.
"""
from config import *  # noqa: F401,F403 — inherit shared server/exchange/indicator base

# ── Risk / position sizing ────────────────────────────────────────────────────
# Priority chain: EQUITY_PERCENT>0 → ORDER_BALANCE_USD>0 → RISK_USD>0 → RISK_PERCENT.
# Live profile: EQUITY_PERCENT=0 activates RISK_PERCENT mode; each SL costs 4% of
# balance regardless of ATR (capital-preservation profile).
EQUITY_PERCENT    = 0.0
RISK_PERCENT      = 4.0     # % of balance risked per trade (SL-distance sized)
ORDER_BALANCE_USD = 0.0
RISK_USD          = 0.0
# LEVERAGE inherited from shared config (account-level).

# ── SL / TP as multiples of ATR ───────────────────────────────────────────────
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 6.0   # 4:1 RR → break-even ~20% win rate; proven 5yr all-pass

# ── 1H strategy thresholds ────────────────────────────────────────────────────
EMA_1H_MIN_SEP       = 0.001   # EMA20 must lead EMA50 by ≥ 0.1% (flat-market filter)
RSI_1H_LONG_MIN      = 45      # 1H RSI minimum for LONG entry
RSI_1H_LONG_MAX      = 78      # 1H RSI maximum for LONG entry (not overbought)
RSI_1H_SHORT_MAX     = 55      # 1H RSI maximum for SHORT entry
RSI_1H_SHORT_MIN     = 22      # 1H RSI minimum for SHORT entry (not oversold)
ATR_1H_PCT_MIN       = 0.05    # 1H ATR must be ≥ 0.05% of price (not dead market)
ATR_1H_PCT_MAX       = 5.0     # 1H ATR must be ≤ 5.0% of price  (not extreme spike)
TRADE_COOLDOWN_1H    = 1       # minimum 1H bars between entries
EMA_TREND_SLOPE_BARS = 7       # lookback bars for EMA200 slope direction filter
VOL_RATIO_MIN        = 0.3     # minimum volume ratio vs 20-bar MA (avoids dead markets)
TRAIL_ACTIVATE_ATR   = 1.5     # move SL to break-even when price moves X×ATR in favor (0=off)
TRAIL_LOCK_ATR       = 2.0     # lock in 1×ATR profit when price moves X×ATR in favor (0=off)
TRAIL_STOP_ATR       = 1.2     # after activate: trail SL at -N×ATR below price peak (0=off)
TRAIL_STEP_ATR       = 1.0     # STEP trail ratchet interval (×ATR): every TRAIL_STEP_ATR of
                               # additional favour locks another TRAIL_STEP_ATR of profit
                               # (1.0 = legacy BE→+1ATR→+2ATR ladder; smaller = tighter lock)
# Profit-based STEP trailing (capital-preservation / max-win-rate regime).  When
# True it SUPERSEDES the classic cascade and the adaptive funnel (dispatch order:
# step → adaptive → classic).  Semantics: once price moves TRAIL_ACTIVATE_ATR×ATR
# in favour the SL steps to break-even; thereafter every *additional* 1.0×ATR of
# favourable excursion ratchets the SL forward by another 1.0×ATR of locked profit
# (BE → +1ATR → +2ATR → …).  Stateless (recomputed from current favour + ratchet),
# so no new position/serialization state.  TRAIL_LOCK/STOP are ignored in this mode.
#
# OPT-IN (default False — same convention as ADAPTIVE_TRAILING_ENABLED) so the
# backtest/grid tools that configure a classic/BE trail aren't silently switched.
# LIVE turns it ON explicitly in ``main()`` (the new production default exit logic);
# ``backtest.run_portfolio`` turns it ON for the multi-asset portfolio run.
STEP_TRAILING_ENABLED = False
BREAKOUT_PERIOD      = 14      # bars lookback for rolling high/low breakout (WFO auto-tunes)
REQUIRE_MACD_CONFIRM = False   # if True, breakout also requires MACD hist direction

# ── Regime / trend-strength filters ──────────────────────────────────────────
EMA_SLOPE_MIN_PCT = 0.15   # EMA200 must have moved ≥ 0.15% over EMA_TREND_SLOPE_BARS (0=off)
ATR_RATIO_MIN     = 1.10   # current ATR must be ≥ 1.10× its 20-bar SMA — expanding vol
ADX_PERIOD        = 14     # ADX lookback period
ADX_MIN           = 20.0   # require ADX ≥ 20 to trade; blocks choppy/ranging markets
EMA_TREND_DISTANCE_MIN = 0.0  # % distance from EMA200 required (0=off)

# ── Breakout quality filter ───────────────────────────────────────────────────
BREAKOUT_ATR_BUFFER = 0.0   # close must exceed rolling high by N×ATR (0 = off)
BODY_ATR_RATIO_MIN  = 0.0   # breakout candle body / ATR floor (0 = off)

# ── Daily circuit breakers ────────────────────────────────────────────────────
DAILY_PROFIT_TARGET_PCT = 0.0
DAILY_LOSS_LIMIT_PCT    = 0.0
DAILY_PROFIT_TARGET_USD = 110.0  # stop trading once daily PnL ≥ $110
DAILY_LOSS_LIMIT_USD    = 50.0   # stop trading once daily PnL ≤ -$50

# ── Consecutive-loss / post-SL guards ─────────────────────────────────────────
MAX_CONSECUTIVE_LOSSES = 3   # halt today after N stop-losses in a row (0=off)
POST_SL_COOLDOWN_1H    = 3   # wait extra 1H bars before re-entering after a SL (0=off)

# ── Session time filter ───────────────────────────────────────────────────────
SESSION_FILTER_START_UTC = 0   # 0,0 = trade 24/7 (proven maximum-growth mode)
SESSION_FILTER_END_UTC   = 0

# ── Limit-order entry (maker-rebate fee optimisation) ─────────────────────────
USE_LIMIT_ENTRY     = False
LIMIT_ENTRY_TIMEOUT = 45   # seconds before market fallback

# ── Initial cooldown (indicator settling guard) ───────────────────────────────
INITIAL_COOLDOWN_BARS = 0  # 0 = off (live bot is warm-started)

# ══════════════════════════════════════════════════════════════════════════════
# WALK-FORWARD OPTIMIZATION  (ON by default — disable via --no-wfo)
# ══════════════════════════════════════════════════════════════════════════════
WFO_ENABLED          = True
WFO_RETUNE_INTERVAL  = 720    # bars between retuning (~30 days)
WFO_TRAINING_WINDOW  = 2160   # bars of training history (~90 days)
WFO_MIN_TRADES       = 4      # min trades required to accept a BP
WFO_CHOPPY_COOLDOWN  = 6      # extended cooldown (bars) when forecast is choppy
WFO_FAST_ENABLED         = True
WFO_FAST_TRAINING_WINDOW = 336   # 14 days
WFO_FAST_ATR_MULT        = 2.0   # ATR spike multiplier

# ══════════════════════════════════════════════════════════════════════════════
# QUANTITATIVE ENHANCEMENTS  (all OFF — proven maximum-growth mode)
# ══════════════════════════════════════════════════════════════════════════════
REGIME_FILTER_ENABLED = False
REGIME_STRONG_ADX     = 28.0
REGIME_HIGH_VOL_PCT   = 4.5
HIGH_VOL_SIZE_SCALE   = 0.5

DYNAMIC_TP_ENABLED     = False
DYNAMIC_TP_STRONG_MULT = 1.5
DYNAMIC_TP_WEAK_MULT   = 0.7

VOL_SIZING_ENABLED   = False
VOL_SIZING_MAX_SCALE = 1.25
VOL_SIZING_MIN_SCALE = 0.50

# ── Adaptive regime framework (OFF) ───────────────────────────────────────────
ADAPTIVE_REGIME_ENABLED = False
ADAPTIVE_MIN_SCORE   = 0.25
ADAPTIVE_TP_BASE     = 4.0
ADAPTIVE_TP_MAX_EXT  = 2.5
ADAPTIVE_SL_MAX_WIDEN = 1.8
ADAPTIVE_SIZE_MIN    = 0.30
ADAPTIVE_BUFFER_MAX  = 0.50
ADAPTIVE_ADX_RELAX   = 8.0

# ── Adaptive trailing stop (OFF) ──────────────────────────────────────────────
ADAPTIVE_TRAILING_ENABLED = False
ADAPTIVE_TRAIL_MIN_ATR    = 0.35

# ══════════════════════════════════════════════════════════════════════════════
# MARKOV REGIME FORECAST  (OFF — enable via --forecast)
# ══════════════════════════════════════════════════════════════════════════════
REGIME_FORECAST_ENABLED    = False
FORECAST_CHOPPY_THRESHOLD  = 0.65
FORECAST_MIN_TREND_PROB    = 0.35
FORECAST_MIN_CONFIDENCE    = 0.30

# ══════════════════════════════════════════════════════════════════════════════
# MULTI-ASSET CONFIG MATRIX  (per-symbol hardcoded optimal 1H parameters)
# ══════════════════════════════════════════════════════════════════════════════
# Each symbol carries its own breakout/ADX/TP/SL/trail tuning.  The module-level
# literals above remain the BTCUSDT default so every existing flat reader
# (strategy.py, backtest.py, WFO, run_backtest) keeps working untouched; calling
# ``apply_symbol(sym)`` overwrites those flat attributes in place with the matrix
# entry, so the engine — which reads ``config.BREAKOUT_PERIOD`` etc. — transparently
# trades that symbol's profile.  Only the five swept knobs are per-symbol; the full
# quality-gate suite, risk chain, and WFO toggle are shared and set by the caller.
#
# ETH/SOL values are the in-sample grid optima from project_altcoin_1h_tuning_sweep
# (243-combo 5yr search); BTC is the proven flagship profile.  NB those altcoin
# winners pin TP at the 6.0 grid ceiling (boundary optimum) and are in-sample — see
# the memory before treating them as validated.
# Each entry carries two orchestration flags plus the five tuned engine knobs:
#   ENABLED    — master switch: if False the asset's processor never spins up.
#   TRADE_MODE — "LIVE" (route real orders via the order_manager/trader) or
#                "EVAL_ONLY" (dry-run: evaluate + log diagnostics, place no orders).
# ``apply_symbol`` writes ONLY the engine knobs onto the flat module globals; the two
# flags stay metadata (never leak into the config namespace the engine reads).
#
# SINGLE SOURCE OF TRUTH = the feature-based domain configs (`src/core/btc/config.py`,
# `eth/config.py`, `sol/config.py`).  CONFIG_MATRIX is ASSEMBLED from them here so the
# engine / backtest / WFO keep reading one flat matrix.  Edit a per-asset knob (or its
# ENABLED / TRADE_MODE flag) in the DOMAIN config, not in this dict.
_ENGINE_KEYS = ("BREAKOUT_PERIOD", "ADX_MIN", "ATR_TP_MULTIPLIER",
                "ATR_SL_MULTIPLIER", "TRAIL_ACTIVATE_ATR")
_MATRIX_KEYS = ("ENABLED", "TRADE_MODE") + _ENGINE_KEYS

# Domain configs are LEAF modules (import nothing local) → no circular import.  The
# entry-point sys.path bootstrap puts `src/core` on the path so these package imports
# resolve (`from btc import config`).
from btc import config as _btc_cfg   # noqa: E402
from eth import config as _eth_cfg   # noqa: E402
from sol import config as _sol_cfg   # noqa: E402


def _domain_entry(mod) -> dict:
    """Project a domain config module into a CONFIG_MATRIX entry dict."""
    return {k: getattr(mod, k) for k in _MATRIX_KEYS}


CONFIG_MATRIX = {
    "BTCUSDT": _domain_entry(_btc_cfg),
    "ETHUSDT": _domain_entry(_eth_cfg),
    "SOLUSDT": _domain_entry(_sol_cfg),
}


def apply_symbol(symbol: str) -> bool:
    """Overwrite the flat 1H-strategy knobs in place with ``symbol``'s matrix entry.

    Writes ONLY the five engine knobs (``_ENGINE_KEYS``) onto this module's globals so
    every path that imported ``config_1h as config`` (engine, backtest, WFO) sees the
    per-symbol profile immediately; the ``ENABLED`` / ``TRADE_MODE`` flags are NOT
    written into the config namespace.  Returns ``True`` if the symbol was found in
    :data:`CONFIG_MATRIX`, ``False`` otherwise (flat defaults left untouched).

    Concurrency note: callers must run ``apply_symbol`` and the (synchronous) signal
    evaluation that reads these globals back-to-back with no ``await`` in between.  The
    live WS dispatch is strictly sequential (one frame handled to completion before the
    next is read), so per-symbol evaluations never interleave and the shared globals
    are safe without a lock.
    """
    entry = CONFIG_MATRIX.get(symbol)
    if entry is None:
        return False
    for key in _ENGINE_KEYS:
        if key in entry:
            globals()[key] = entry[key]
    return True


def enabled_symbols() -> list:
    """Return the matrix symbols whose ``ENABLED`` flag is set (insertion order)."""
    return [s for s, e in CONFIG_MATRIX.items() if e.get("ENABLED")]


def trade_mode(symbol: str) -> str:
    """Return ``"LIVE"`` / ``"EVAL_ONLY"`` for ``symbol`` (defaults to ``"EVAL_ONLY"``)."""
    return CONFIG_MATRIX.get(symbol, {}).get("TRADE_MODE", "EVAL_ONLY")
