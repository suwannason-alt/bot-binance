"""
Solana trading domain — 1H strategy constants (HARDCODED literals).

LEAF config (no local imports).  SOL params are in-sample grid optima
(project_altcoin_1h_tuning_sweep); SOL's edge is marginal (PF≈1.03) — paper only.
"""
ENABLED            = True
TRADE_MODE         = "LIVE"     # paper-validation per the multi-LIVE run

BREAKOUT_PERIOD    = 20
ADX_MIN            = 20.0
ATR_TP_MULTIPLIER  = 6.0
ATR_SL_MULTIPLIER  = 1.5
TRAIL_ACTIVATE_ATR = 2.0
