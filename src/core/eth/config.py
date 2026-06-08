"""
Ethereum trading domain — 1H strategy constants (HARDCODED literals).

LEAF config (no local imports).  ETH params are in-sample grid optima from
project_altcoin_1h_tuning_sweep (TP pinned at the 6.0 ceiling) — treat as a
hypothesis pending paper validation.
"""
ENABLED            = True
TRADE_MODE         = "LIVE"     # paper-validation per the multi-LIVE run

BREAKOUT_PERIOD    = 10
ADX_MIN            = 20.0
ATR_TP_MULTIPLIER  = 6.0
ATR_SL_MULTIPLIER  = 2.0
TRAIL_ACTIVATE_ATR = 1.0
