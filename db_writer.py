"""
DB WRITER - Persistent History in SQLite
Complements state_writer (JSON for dashboard) with history for charts and statistics.
Never throws exceptions outward — same pattern as state_writer.
"""

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

import config
from logger import log


def _get_connection() -> sqlite3.Connection:
    """Open SQLite connection with WAL mode."""
    db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create database and tables if they don't exist. Call once at start()."""
    try:
        conn = _get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                decision TEXT,
                final_score REAL,
                confidence REAL,
                confidence_level TEXT,
                tech_score REAL,
                news_score REAL,
                ml_score REAL,
                momentum_score REAL,
                calendar_score REAL,
                current_price REAL,
                volatility_status TEXT,
                scenario TEXT,
                scenario_description TEXT,
                gpt_action TEXT,
                gpt_adjustment INTEGER
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket INTEGER UNIQUE,
                direction TEXT,
                volume REAL,
                open_price REAL,
                close_price REAL,
                sl REAL,
                tp REAL,
                profit REAL,
                close_reason TEXT,
                open_time TEXT,
                close_time TEXT,
                comment TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS account_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL,
                equity REAL,
                margin REAL,
                free_margin REAL,
                profit REAL
            )
        """)

        conn.commit()
        conn.close()
        log.info("SQLite history DB initialized: " + os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db")))
    except Exception as e:
        log.debug(f"db_writer: failed to initialize DB: {e}")


def record_analysis(last_analysis: Dict[str, Any]) -> None:
    """Record a Brain analysis entry."""
    try:
        if not last_analysis:
            return

        gpt = last_analysis.get("gpt_validation") or {}

        conn = _get_connection()
        conn.execute(
            """INSERT INTO analyses
               (timestamp, decision, final_score, confidence, confidence_level,
                tech_score, news_score, ml_score, momentum_score, calendar_score,
                current_price, volatility_status, scenario, scenario_description,
                gpt_action, gpt_adjustment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                last_analysis.get("timestamp", datetime.now().isoformat()),
                last_analysis.get("decision"),
                last_analysis.get("final_score"),
                last_analysis.get("confidence"),
                last_analysis.get("confidence_level"),
                last_analysis.get("tech_score"),
                last_analysis.get("news_score"),
                last_analysis.get("ml_score"),
                last_analysis.get("momentum_score"),
                last_analysis.get("calendar_score"),
                last_analysis.get("current_price"),
                last_analysis.get("volatility_status"),
                last_analysis.get("scenario"),
                last_analysis.get("scenario_description"),
                gpt.get("action"),
                gpt.get("adjustment"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to record analysis: {e}")


def record_trade_open(
    ticket: int,
    direction: str,
    volume: float,
    open_price: float,
    sl: float,
    tp: float,
    open_time: Optional[str] = None,
    comment: str = "",
) -> None:
    """Record trade open."""
    try:
        conn = _get_connection()
        conn.execute(
            """INSERT OR IGNORE INTO trades
               (ticket, direction, volume, open_price, sl, tp, open_time, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticket,
                direction,
                volume,
                open_price,
                sl,
                tp,
                open_time or datetime.now().isoformat(),
                comment,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to record trade open: {e}")


def record_trade_close(
    ticket: int,
    close_price: Optional[float],
    profit: Optional[float],
    close_reason: str,
    close_time: Optional[str] = None,
) -> None:
    """Update trade with close data."""
    try:
        conn = _get_connection()
        conn.execute(
            """UPDATE trades
               SET close_price = ?, profit = ?, close_reason = ?, close_time = ?
               WHERE ticket = ?""",
            (
                close_price,
                profit,
                close_reason,
                close_time or datetime.now().isoformat(),
                ticket,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to record trade close: {e}")


def record_account_snapshot(account_info: Optional[Dict[str, Any]]) -> None:
    """Record account state snapshot."""
    try:
        if not account_info:
            return

        conn = _get_connection()
        conn.execute(
            """INSERT INTO account_snapshots
               (timestamp, balance, equity, margin, free_margin, profit)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().isoformat(),
                account_info.get("balance"),
                account_info.get("equity"),
                account_info.get("margin"),
                account_info.get("free_margin"),
                account_info.get("profit"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to record account snapshot: {e}")
