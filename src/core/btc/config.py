"""
Bitcoin trading domain — 1H strategy constants (HARDCODED literals).

A LEAF config module: it imports nothing local, so ``config_1h`` can assemble the
multi-asset ``CONFIG_MATRIX`` from the three domain configs without a circular import.
Edit a BTC knob HERE; ``config_1h.CONFIG_MATRIX["BTCUSDT"]`` mirrors it automatically.
"""
ENABLED            = True       # master switch: spin up this asset's processor?
TRADE_MODE         = "LIVE"     # "LIVE" (real orders) | "EVAL_ONLY" (dry-run diagnostics)

# ── Optimal 1H parameters (proven flagship profile) ──────────────────────────
BREAKOUT_PERIOD    = 14
ADX_MIN            = 20.0
ATR_TP_MULTIPLIER  = 6.0
ATR_SL_MULTIPLIER  = 1.5
TRAIL_ACTIVATE_ATR = 1.2        # BE trigger; STEP trail then locks +1ATR per +1ATR
