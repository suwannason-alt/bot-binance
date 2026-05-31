"""SQLite-backed bot state persistence.

Provides crash-recovery for the autonomous live trading bot.  All mutable
strategy state — WFO parameters, Markov forecaster buffer, cooldown counters,
active position metadata — is persisted after each 1H bar and restored on
startup.

Design principles
-----------------
* **Single source of truth**: one SQLite file on local disk.  No network
  dependency; survives VPS reboots, process crashes, and OOM kills.
* **Atomic writes**: every ``save()`` is a single ``INSERT OR REPLACE`` wrapped
  in an implicit SQLite transaction.  A crash mid-write leaves the *previous*
  valid row intact.
* **Stale detection**: state older than ``MAX_STALE_HOURS`` is rejected and the
  bot performs a fresh warm start.  Prevents resuming with outdated WFO
  parameters after a multi-day outage.
* **Human-readable**: the payload column is JSON, readable with any SQLite
  browser for debugging.

Schema
------
::

    CREATE TABLE bot_state (
        id      INTEGER PRIMARY KEY,   -- always 1 (single-row table)
        version TEXT NOT NULL,         -- schema migration guard
        saved_at TEXT NOT NULL,        -- ISO-8601 UTC timestamp
        payload TEXT NOT NULL          -- JSON blob of BotStatePayload
    );

    CREATE TABLE wfo_log (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        bar      INTEGER NOT NULL,
        bp       INTEGER NOT NULL,
        pf       REAL    NOT NULL,
        n_trades INTEGER NOT NULL,
        saved_at TEXT    NOT NULL
    );

Usage::

    from state_manager import StateManager

    sm = StateManager()                         # opens / creates bot_state.db

    sm.save(wfo=wfo, forecaster=fc,             # persist after each 1H bar
            strat_state=ss, bars_since_last=5,
            position=trader.position,
            balance=trader.balance)

    saved = sm.load()                           # None if DB is empty or stale
    if saved:
        wfo.params.breakout_period = saved["wfo_active_bp"]
        ...
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("state_manager")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCHEMA_VERSION = "2"          # increment on breaking schema changes
_DB_FILENAME    = "bot_state.db"
_MAX_STALE_HOURS = 48.0        # state older than this triggers a fresh warm start


# ---------------------------------------------------------------------------
# StateManager
# ---------------------------------------------------------------------------

class StateManager:
    """Crash-recovery state store backed by a local SQLite database.

    Args:
        db_path: Path to the SQLite file.  Created on first use.
                 Defaults to ``bot_state.db`` in the project root.
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        if db_path is None:
            db_path = os.path.join(
                os.path.dirname(__file__), _DB_FILENAME
            )
        self._db_path = db_path
        self._init_schema()

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        """Create tables if they do not already exist."""
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    id       INTEGER PRIMARY KEY,
                    version  TEXT NOT NULL,
                    saved_at TEXT NOT NULL,
                    payload  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS wfo_log (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    bar      INTEGER NOT NULL,
                    bp       INTEGER NOT NULL,
                    pf       REAL    NOT NULL,
                    n_trades INTEGER NOT NULL,
                    saved_at TEXT    NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        """Open a WAL-mode SQLite connection, commit on exit, always close.

        sqlite3.Connection used as a plain context manager only commits or
        rolls back — it never closes the connection.  Every call to _connect()
        must therefore close explicitly; this context manager enforces that.
        """
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(
        self,
        wfo,                          # Optional[WalkForwardOptimizer]
        forecaster,                   # Optional[MarkovRegimeForecaster]
        strat_state,                  # StrategyState
        bars_since_last: int,
        position,                     # Optional[trader.Position]
        balance: float,
        bar_counter: int = 0,
    ) -> None:
        """Persist current bot state to SQLite.

        Serialises all mutable strategy objects into a single JSON blob and
        upserts the single-row ``bot_state`` table.  Appends a row to
        ``wfo_log`` when the WFO breakout period has changed since the last
        save.

        Args:
            wfo:             Walk-forward optimizer instance (or ``None``).
            forecaster:      Markov regime forecaster instance (or ``None``).
            strat_state:     Current :class:`~backtest.StrategyState`.
            bars_since_last: 1H bars elapsed since the last closed trade.
            position:        Active :class:`~trader.Position` (or ``None``).
            balance:         Current USDT account balance.
            bar_counter:     Absolute 1H bar index since the warm-start epoch.
        """
        now_iso = datetime.now(timezone.utc).isoformat()

        # ── Serialise WFO ─────────────────────────────────────────────────────
        wfo_dict: Dict[str, Any] = {}
        if wfo is not None:
            wfo_dict = {
                "active_bp":         wfo.params.breakout_period,
                "last_retune_bar":   wfo._last_retune_bar,
                "profit_factor":     wfo.params.profit_factor,
                "n_trades":          wfo.params.n_trades,
                "updated_bar":       wfo.params.updated_bar,
                "log": [
                    {
                        "bar": e.bar, "bp": e.bp,
                        "pf":  round(e.pf, 4),
                        "n":   e.n,
                    }
                    for e in wfo.log[-50:]   # keep last 50 retunings
                ],
            }

        # ── Serialise Markov forecaster ───────────────────────────────────────
        fc_dict: Dict[str, Any] = {}
        if forecaster is not None:
            fc_dict = {
                "buffer":        list(forecaster._buffer),
                "current_state": int(forecaster.current_state),
                "lookback":      forecaster._lookback,
                "alpha":         forecaster._alpha,
            }

        # ── Serialise strategy state ──────────────────────────────────────────
        ss_dict = {
            "active_bp":          strat_state.active_bp,
            "current_regime":     strat_state.current_regime,
            "trend_prob":         round(strat_state.trend_prob, 4),
            "choppy_prob":        round(strat_state.choppy_prob, 4),
            "entry_allowed":      strat_state.entry_allowed,
            "size_scale":         round(strat_state.size_scale, 4),
            "effective_cooldown": strat_state.effective_cooldown,
        }

        # ── Serialise active position ─────────────────────────────────────────
        pos_dict: Optional[Dict[str, Any]] = None
        if position is not None and not getattr(position, "closed", True):
            pos_dict = {
                "side":             position.side,
                "entry":            float(position.entry),
                "qty":              float(position.qty),
                "sl":               float(position.sl),
                "tp":               float(position.tp),
                "initial_atr":      float(getattr(position, "initial_atr", 0.0)),
                "trail_activated":  bool(getattr(position, "trail_activated",  False)),
                "lock_activated":   bool(getattr(position, "lock_activated",  False)),
                "trail_peak":       float(getattr(position, "trail_peak",      0.0)),
                "open_time":        str(getattr(position, "open_time", "")),
                "sl_order_id":      getattr(position, "sl_order_id", None),
            }

        payload: Dict[str, Any] = {
            "wfo":               wfo_dict,
            "forecaster":        fc_dict,
            "strat_state":       ss_dict,
            "bars_since_last":   bars_since_last,
            "position":          pos_dict,
            "balance":           round(balance, 4),
            "bar_counter":       bar_counter,
        }

        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_state (id, version, saved_at, payload) "
                "VALUES (1, ?, ?, ?)",
                (_SCHEMA_VERSION, now_iso, json.dumps(payload)),
            )

        logger.debug(
            "State saved: BP=%d  forecaster_buf=%d  bal=%.2f  bar=%d",
            wfo_dict.get("active_bp", 14),
            len(fc_dict.get("buffer", [])),
            balance,
            bar_counter,
        )

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        """Load and deserialise the most recently saved bot state.

        Returns ``None`` in any of these cases:
          * The database has no saved state (first run).
          * The saved version does not match ``_SCHEMA_VERSION``
            (schema migration required).
          * The state is older than ``_MAX_STALE_HOURS`` (stale market
            conditions — fresh warm start is safer).

        Returns:
            Deserialised state dictionary with keys ``wfo``, ``forecaster``,
            ``strat_state``, ``bars_since_last``, ``position``, ``balance``,
            ``bar_counter``; or ``None``.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT version, saved_at, payload FROM bot_state WHERE id = 1"
            ).fetchone()

        if row is None:
            logger.info("StateManager: no saved state found — fresh start")
            return None

        version, saved_at_iso, payload_json = row

        # Schema version check
        if version != _SCHEMA_VERSION:
            logger.warning(
                "StateManager: schema mismatch (saved=%s current=%s) — fresh start",
                version, _SCHEMA_VERSION,
            )
            return None

        # Stale check
        try:
            saved_dt = datetime.fromisoformat(saved_at_iso)
            if saved_dt.tzinfo is None:
                saved_dt = saved_dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - saved_dt).total_seconds() / 3600.0
            if age_hours > _MAX_STALE_HOURS:
                logger.warning(
                    "StateManager: state is %.1f h old (max %.0f h) — fresh start",
                    age_hours, _MAX_STALE_HOURS,
                )
                return None
            logger.info(
                "StateManager: restoring state (age=%.1f h  BP=%s)",
                age_hours,
                json.loads(payload_json).get("wfo", {}).get("active_bp", "?"),
            )
        except Exception as e:
            logger.warning("StateManager: stale-check failed (%s) — fresh start", e)
            return None

        try:
            return json.loads(payload_json)
        except json.JSONDecodeError as e:
            logger.error("StateManager: JSON decode failed: %s — fresh start", e)
            return None

    # ------------------------------------------------------------------
    # WFO log helpers
    # ------------------------------------------------------------------

    def append_wfo_log(self, bar: int, bp: int, pf: float, n: int) -> None:
        """Append a single WFO retune entry to the structured ``wfo_log`` table.

        Args:
            bar: 1H bar index of the retune.
            bp:  Selected breakout period.
            pf:  Achieved Profit Factor.
            n:   Number of training-window trades.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO wfo_log (bar, bp, pf, n_trades, saved_at) VALUES (?, ?, ?, ?, ?)",
                (bar, bp, round(pf, 4), n, now_iso),
            )

    def load_wfo_log(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return the most recent WFO retune history from the structured table.

        Args:
            limit: Maximum number of entries to return (most recent first).

        Returns:
            List of dicts with keys ``bar``, ``bp``, ``pf``, ``n_trades``,
            ``saved_at``, ordered newest-first.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT bar, bp, pf, n_trades, saved_at FROM wfo_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"bar": r[0], "bp": r[1], "pf": r[2], "n_trades": r[3], "saved_at": r[4]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Delete all saved state (triggers a fresh warm start on next run).

        Use this to reset the bot to a clean slate, e.g. after changing the
        trading strategy parameters.
        """
        with self._connect() as conn:
            conn.execute("DELETE FROM bot_state")
            conn.execute("DELETE FROM wfo_log")
        logger.info("StateManager: all state cleared")

    def db_path(self) -> str:
        """Return the absolute path to the SQLite database file."""
        return self._db_path

    def is_empty(self) -> bool:
        """Return ``True`` if no state has been saved yet."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM bot_state"
            ).fetchone()
        return row[0] == 0
