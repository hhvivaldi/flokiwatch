"""
DB WRITER - Persistent History in SQLite
Complements state_writer (JSON for dashboard) with history for charts and statistics.
Never throws exceptions outward — same pattern as state_writer.
"""

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional

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
                gpt_adjustment INTEGER,
                utc_hour INTEGER,
                session_name TEXT,
                hold_forced INTEGER,
                original_decision TEXT,
                hold_reason TEXT,
                rsi_14 REAL,
                macd REAL,
                macd_signal REAL,
                macd_hist REAL,
                ema_9 REAL,
                ema_21 REAL,
                ema_50 REAL,
                bb_upper REAL,
                bb_middle REAL,
                bb_lower REAL,
                bb_position REAL,
                price_vs_ema50_pct REAL,
                adx_14 REAL,
                plus_di REAL,
                minus_di REAL,
                atr_14 REAL,
                volume_ratio REAL,
                volume_classification TEXT,
                momentum_direction TEXT,
                consecutive_count INTEGER,
                consecutive_direction TEXT,
                breakout_detected INTEGER,
                breakout_type TEXT,
                mtf_d1_direction TEXT,
                mtf_h4_direction TEXT,
                mtf_alignment TEXT,
                mtf_confidence_adjustment REAL,
                volume_gate_ratio REAL,
                volume_gate_status TEXT,
                volume_gate_confidence_adjustment REAL,
                ml_h1_prob REAL,  -- calibrated ensemble scores normalized to 0-1 (not raw model probabilities)
                ml_h4_prob REAL,  -- calibrated ensemble scores normalized to 0-1 (not raw model probabilities)
                ml_direction TEXT
            )
        """)

        # Migration: add analyses columns if missing (safe no-op if they already exist)
        analyses_columns_to_add = [
            ("utc_hour", "INTEGER"),
            ("session_name", "TEXT"),
            ("hold_forced", "INTEGER"),
            ("original_decision", "TEXT"),
            ("hold_reason", "TEXT"),
            ("rsi_14", "REAL"),
            ("macd", "REAL"),
            ("macd_signal", "REAL"),
            ("macd_hist", "REAL"),
            ("ema_9", "REAL"),
            ("ema_21", "REAL"),
            ("ema_50", "REAL"),
            ("bb_upper", "REAL"),
            ("bb_middle", "REAL"),
            ("bb_lower", "REAL"),
            ("bb_position", "REAL"),
            ("price_vs_ema50_pct", "REAL"),
            ("adx_14", "REAL"),
            ("plus_di", "REAL"),
            ("minus_di", "REAL"),
            ("atr_14", "REAL"),
            ("volume_ratio", "REAL"),
            ("volume_classification", "TEXT"),
            ("momentum_direction", "TEXT"),
            ("consecutive_count", "INTEGER"),
            ("consecutive_direction", "TEXT"),
            ("breakout_detected", "INTEGER"),
            ("breakout_type", "TEXT"),
            ("mtf_d1_direction", "TEXT"),
            ("mtf_h4_direction", "TEXT"),
            ("mtf_alignment", "TEXT"),
            ("mtf_confidence_adjustment", "REAL"),
            ("volume_gate_ratio", "REAL"),
            ("volume_gate_status", "TEXT"),
            ("volume_gate_confidence_adjustment", "REAL"),
            ("ml_h1_prob", "REAL"),
            ("ml_h4_prob", "REAL"),
            ("ml_direction", "TEXT"),
        ]

        for col_name, col_type in analyses_columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE analyses ADD COLUMN {col_name} {col_type}")
                conn.commit()
            except sqlite3.OperationalError:
                pass

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
                comment TEXT,
                breakeven_activated INTEGER
            )
        """)

        # Migration: add breakeven_activated column if missing
        try:
            cursor.execute("ALTER TABLE trades ADD COLUMN breakeven_activated INTEGER")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

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

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                brain_decision TEXT,
                brain_score REAL,
                brain_confidence REAL,
                agent_decision TEXT,
                agent_confidence INTEGER,
                agent_reasoning TEXT,
                agent_key_factors TEXT,
                agent_concerns TEXT,
                tp_entry_strategy TEXT,
                tp_entry_price REAL,
                tp_entry_rationale TEXT,
                tp_stop_loss REAL,
                tp_stop_loss_rationale TEXT,
                tp_take_profit REAL,
                tp_take_profit_rationale TEXT,
                tp_risk_reward_ratio REAL,
                tp_timing TEXT,
                tp_moment_assessment TEXT,
                agreement INTEGER,
                executed TEXT,
                outcome TEXT,
                prompt_version TEXT,
                prompt_hash TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                latency_ms INTEGER
            )
        """)

        # Migration: add agent_decisions columns if missing (safe no-op if they already exist)
        agent_decisions_columns_to_add = [
            ("tp_entry_strategy", "TEXT"),
            ("tp_entry_price", "REAL"),
            ("tp_entry_rationale", "TEXT"),
            ("tp_stop_loss", "REAL"),
            ("tp_stop_loss_rationale", "TEXT"),
            ("tp_take_profit", "REAL"),
            ("tp_take_profit_rationale", "TEXT"),
            ("tp_risk_reward_ratio", "REAL"),
            ("tp_timing", "TEXT"),
            ("tp_moment_assessment", "TEXT"),
        ]

        for col_name, col_type in agent_decisions_columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE agent_decisions ADD COLUMN {col_name} {col_type}")
                conn.commit()
            except sqlite3.OperationalError:
                pass

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS agent_proactive_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                h1_close_time TEXT,
                agent_decision TEXT,
                agent_confidence INTEGER,
                agent_reasoning TEXT,
                agent_key_factors TEXT,
                agent_concerns TEXT,
                raw_response TEXT,
                tp_entry_strategy TEXT,
                tp_entry_price REAL,
                tp_entry_rationale TEXT,
                tp_stop_loss REAL,
                tp_stop_loss_rationale TEXT,
                tp_take_profit REAL,
                tp_take_profit_rationale TEXT,
                tp_risk_reward_ratio REAL,
                tp_timing TEXT,
                tp_moment_assessment TEXT,
                prompt_version TEXT,
                prompt_hash TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                latency_ms INTEGER,
                adjustment_new_sl REAL,
                adjustment_new_tp REAL,
                adjustment_reason TEXT,
                close_reason TEXT
            )
        """)

        # Migration: add agent_proactive_analyses columns if missing (safe no-op if they already exist)
        agent_proactive_columns_to_add = [
            ("raw_response", "TEXT"),
            ("tp_entry_strategy", "TEXT"),
            ("tp_entry_price", "REAL"),
            ("tp_entry_rationale", "TEXT"),
            ("tp_stop_loss", "REAL"),
            ("tp_stop_loss_rationale", "TEXT"),
            ("tp_take_profit", "REAL"),
            ("tp_take_profit_rationale", "TEXT"),
            ("tp_risk_reward_ratio", "REAL"),
            ("tp_timing", "TEXT"),
            ("tp_moment_assessment", "TEXT"),
            ("adjustment_new_sl", "REAL"),
            ("adjustment_new_tp", "REAL"),
            ("adjustment_reason", "TEXT"),
            ("close_reason", "TEXT"),
        ]

        for col_name, col_type in agent_proactive_columns_to_add:
            try:
                cursor.execute(f"ALTER TABLE agent_proactive_analyses ADD COLUMN {col_name} {col_type}")
                conn.commit()
            except sqlite3.OperationalError:
                pass

        conn.commit()
        conn.close()
        db_abs_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        log.warning("SQLite history DB initialized: " + db_abs_path)
    except Exception as e:
        log.debug(f"db_writer: failed to initialize DB: {e}")


def record_analysis(last_analysis: Dict[str, Any]) -> None:
    """Record a Brain analysis entry."""
    try:
        if not last_analysis:
            return

        gpt = last_analysis.get("gpt_validation") or {}

        mtf = last_analysis.get("mtf_trend") or {}
        volume_gate = last_analysis.get("volume_gate") or {}
        indicators = last_analysis.get("indicators") or {}
        ml_meta = last_analysis.get("ml") or {}

        conn = _get_connection()
        conn.execute(
            """INSERT INTO analyses
               (timestamp, decision, final_score, confidence, confidence_level,
                tech_score, news_score, ml_score, momentum_score, calendar_score,
                current_price, volatility_status, scenario, scenario_description,
                gpt_action, gpt_adjustment,
                utc_hour, session_name, hold_forced, original_decision, hold_reason,
                rsi_14, macd, macd_signal, macd_hist,
                ema_9, ema_21, ema_50,
                bb_upper, bb_middle, bb_lower, bb_position,
                price_vs_ema50_pct,
                adx_14, plus_di, minus_di, atr_14,
                volume_ratio, volume_classification,
                momentum_direction, consecutive_count, consecutive_direction,
                breakout_detected, breakout_type,
                mtf_d1_direction, mtf_h4_direction, mtf_alignment, mtf_confidence_adjustment,
                volume_gate_ratio, volume_gate_status, volume_gate_confidence_adjustment,
                ml_h1_prob, ml_h4_prob, ml_direction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?, ?,
                       ?,
                       ?, ?, ?, ?,
                       ?, ?,
                       ?, ?, ?,
                       ?, ?,
                       ?, ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?)""",
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
                last_analysis.get("utc_hour"),
                last_analysis.get("session_name"),
                1 if last_analysis.get("hold_forced") else 0 if last_analysis.get("hold_forced") is not None else None,
                last_analysis.get("original_decision"),
                last_analysis.get("hold_reason"),
                indicators.get("rsi_14"),
                indicators.get("macd"),
                indicators.get("macd_signal"),
                indicators.get("macd_hist"),
                indicators.get("ema_9"),
                indicators.get("ema_21"),
                indicators.get("ema_50"),
                indicators.get("bb_upper"),
                indicators.get("bb_middle"),
                indicators.get("bb_lower"),
                indicators.get("bb_position"),
                indicators.get("price_vs_ema50_pct"),
                indicators.get("adx_14"),
                indicators.get("plus_di"),
                indicators.get("minus_di"),
                indicators.get("atr_14"),
                indicators.get("volume_ratio"),
                indicators.get("volume_classification"),
                indicators.get("momentum_direction"),
                indicators.get("consecutive_count"),
                indicators.get("consecutive_direction"),
                indicators.get("breakout_detected"),
                indicators.get("breakout_type"),
                mtf.get("d1_direction"),
                mtf.get("h4_direction"),
                mtf.get("alignment"),
                mtf.get("confidence_adjustment"),
                volume_gate.get("volume_ratio"),
                volume_gate.get("status"),
                volume_gate.get("confidence_adjustment"),
                ml_meta.get("h1_prob"),
                ml_meta.get("h4_prob"),
                ml_meta.get("direction"),
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


def update_trade_open_price(
    new_ticket: int,
    direction: str,
    actual_open_price: float,
) -> None:
    """
    Update pending trade record (ticket=0) with actual MT5 fill price and ticket.
    Called after EA confirms execution.
    
    Matches on direction and recent open_time to handle edge case of
    multiple pending trades in different directions.
    """
    try:
        conn = _get_connection()
        # Use subquery to find the most recent pending trade for this direction
        cursor = conn.execute(
            """UPDATE trades 
               SET ticket = ?, open_price = ?
               WHERE id = (
                   SELECT id FROM trades 
                   WHERE ticket = 0 
                     AND direction = ?
                     AND open_time > datetime('now', '-10 minutes')
                   ORDER BY open_time DESC
                   LIMIT 1
               )""",
            (new_ticket, actual_open_price, direction),
        )
        if cursor.rowcount > 0:
            log.debug(f"db_writer: updated pending trade → ticket={new_ticket}, open_price={actual_open_price}")
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to update trade open price: {e}")


def record_trade_close(
    ticket: int,
    close_price: Optional[float],
    profit: Optional[float],
    close_reason: str,
    close_time: Optional[str] = None,
    breakeven_activated: Optional[bool] = None,
) -> None:
    """Update trade with close data."""
    try:
        conn = _get_connection()
        be_int = 1 if breakeven_activated else (0 if breakeven_activated is False else None)
        conn.execute(
            """UPDATE trades
               SET close_price = ?, profit = ?, close_reason = ?, close_time = ?, breakeven_activated = ?
               WHERE ticket = ?""",
            (
                close_price,
                profit,
                close_reason,
                close_time or datetime.now().isoformat(),
                be_int,
                ticket,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.debug(f"db_writer: failed to record trade close: {e}")


def record_agent_decision(
    brain_decision: str,
    brain_score: float,
    brain_confidence: float,
    agent_result: Dict[str, Any],
    executed: str,
    agreement: bool,
    outcome: str = "PENDING",
) -> None:
    """
    Record an AI Agent decision for shadow mode comparison.
    
    Args:
        brain_decision: Brain's decision (BUY/SELL/HOLD or HOLD_FORCED)
        brain_score: Brain's final score
        brain_confidence: Brain's confidence
        agent_result: AgentResult.to_dict() output
        executed: Which decision was executed (brain/agent)
        agreement: Whether Agent agrees with Brain (pre-calculated by caller)
        outcome: Trade outcome (PENDING/WIN/LOSS/BE) - filled post-hoc
    """
    try:
        import json
        
        agent_decision = agent_result.get("decision", "DEFER_TO_BRAIN")
        agreement_int = 1 if agreement else 0

        tp = agent_result.get("trade_plan") or {}
        
        conn = _get_connection()
        conn.execute(
            """INSERT INTO agent_decisions
               (timestamp, brain_decision, brain_score, brain_confidence,
                agent_decision, agent_confidence, agent_reasoning,
                agent_key_factors, agent_concerns,
                tp_entry_strategy, tp_entry_price, tp_entry_rationale,
                tp_stop_loss, tp_stop_loss_rationale,
                tp_take_profit, tp_take_profit_rationale,
                tp_risk_reward_ratio, tp_timing, tp_moment_assessment,
                agreement, executed, outcome,
                prompt_version, prompt_hash, model, input_tokens, output_tokens, latency_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                       ?, ?, ?,
                       ?, ?,
                       ?, ?,
                       ?, ?, ?,
                       ?, ?, ?,
                       ?, ?, ?, ?, ?, ?)""",
            (
                agent_result.get("timestamp", datetime.now().isoformat()),
                brain_decision,
                brain_score,
                brain_confidence,
                agent_decision,
                agent_result.get("confidence", 0),
                agent_result.get("reasoning", ""),
                json.dumps(agent_result.get("key_factors", [])),
                json.dumps(agent_result.get("concerns", [])),
                tp.get("entry_strategy"),
                tp.get("entry_price"),
                tp.get("entry_rationale"),
                tp.get("stop_loss"),
                tp.get("stop_loss_rationale"),
                tp.get("take_profit"),
                tp.get("take_profit_rationale"),
                tp.get("risk_reward_ratio"),
                tp.get("timing"),
                tp.get("moment_assessment"),
                agreement_int,
                executed,
                outcome,
                agent_result.get("prompt_version", ""),
                agent_result.get("prompt_hash", ""),
                agent_result.get("model", ""),
                agent_result.get("input_tokens", 0),
                agent_result.get("output_tokens", 0),
                agent_result.get("latency_ms", 0),
            ),
        )
        conn.commit()
        conn.close()
        log.info(f"Agent decision recorded: {agent_decision} (agreement={agreement})")
    except Exception as e:
        log.debug(f"db_writer: failed to record agent decision: {e}")


def record_agent_proactive_analysis(
    h1_close_time: str,
    agent_result: Dict[str, Any],
) -> None:
    """Record a proactive hourly Agent analysis (diagnostic only)."""
    try:
        import json

        tp = agent_result.get("trade_plan") or {}
        adj = agent_result.get("adjustment") or {}

        conn = _get_connection()
        conn.execute(
            """INSERT INTO agent_proactive_analyses
               (timestamp, h1_close_time,
                agent_decision, agent_confidence, agent_reasoning,
                agent_key_factors, agent_concerns, raw_response,
                tp_entry_strategy, tp_entry_price, tp_entry_rationale,
                tp_stop_loss, tp_stop_loss_rationale,
                tp_take_profit, tp_take_profit_rationale,
                tp_risk_reward_ratio, tp_timing, tp_moment_assessment,
                prompt_version, prompt_hash, model, input_tokens, output_tokens, latency_ms,
                adjustment_new_sl, adjustment_new_tp, adjustment_reason, close_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?,
                       ?,
                       ?, ?, ?,
                       ?, ?,
                       ?, ?,
                       ?, ?, ?,
                       ?, ?, ?, ?, ?, ?,
                       ?, ?, ?, ?)""",
            (
                agent_result.get("timestamp", datetime.now().isoformat()),
                h1_close_time,
                agent_result.get("decision", "DEFER_TO_BRAIN"),
                agent_result.get("confidence", 0),
                agent_result.get("reasoning", ""),
                json.dumps(agent_result.get("key_factors", [])),
                json.dumps(agent_result.get("concerns", [])),
                agent_result.get("raw_response"),
                tp.get("entry_strategy"),
                tp.get("entry_price"),
                tp.get("entry_rationale"),
                tp.get("stop_loss"),
                tp.get("stop_loss_rationale"),
                tp.get("take_profit"),
                tp.get("take_profit_rationale"),
                tp.get("risk_reward_ratio"),
                tp.get("timing"),
                tp.get("moment_assessment"),
                agent_result.get("prompt_version", ""),
                agent_result.get("prompt_hash", ""),
                agent_result.get("model", ""),
                agent_result.get("input_tokens", 0),
                agent_result.get("output_tokens", 0),
                agent_result.get("latency_ms", 0),
                adj.get("new_sl"),
                adj.get("new_tp"),
                adj.get("reason"),
                agent_result.get("close_reason"),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning(f"db_writer: failed to record proactive analysis: {e}")


def get_recent_proactive_decisions(limit: int = 5) -> List[Dict[str, Any]]:
    """
    Query last N proactive decisions for Agent memory.
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT timestamp, agent_decision, agent_confidence, tp_entry_price, tp_stop_loss, tp_take_profit, tp_risk_reward_ratio
               FROM agent_proactive_analyses
               ORDER BY id DESC
               LIMIT ?""",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            timestamp_str, decision, confidence, entry, sl, tp, rr = row
            results.append({
                "timestamp": timestamp_str,
                "decision": decision,
                "confidence": confidence,
                "entry": entry,
                "sl": sl,
                "tp": tp,
                "rr": rr
            })
        
        return results
    except Exception as e:
        log.debug(f"db_writer: failed to get recent proactive decisions: {e}")
        return []


def get_recent_agent_decisions(limit: int = 5) -> List[Dict[str, Any]]:
    """
    Query last N agent decisions for Agent memory.
    
    Returns list of dicts with: timestamp, trigger, decision, reasoning_summary
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """SELECT timestamp, brain_decision, agent_decision, agent_reasoning
               FROM agent_decisions
               ORDER BY id DESC
               LIMIT ?""",
            (limit,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            timestamp_str, brain_decision, agent_decision, reasoning = row
            # Determine trigger type from brain_decision
            if brain_decision and "HOLD_FORCED" in brain_decision:
                trigger = "HOLD_FORCED"
            else:
                trigger = "SIGNAL"
            
            # Truncate reasoning to 100 chars
            reasoning_summary = ""
            if reasoning:
                reasoning_summary = reasoning[:100] + "..." if len(reasoning) > 100 else reasoning
            
            results.append({
                "timestamp": timestamp_str,
                "trigger": trigger,
                "decision": agent_decision or "UNKNOWN",
                "reasoning_summary": reasoning_summary,
            })
        
        return results
    except Exception as e:
        log.debug(f"db_writer: failed to get recent agent decisions: {e}")
        return []


def get_trade_feedback(limit: int = 5) -> Dict[str, Any]:
    """
    Get recent trades with Agent decision accuracy.
    
    Joins trades with agent_decisions by timestamp proximity (±5 min).
    Returns last N trades with agent decision info and accuracy stats.
    """
    try:
        conn = _get_connection()
        cursor = conn.cursor()
        
        # Get recent closed trades
        cursor.execute(
            """SELECT ticket, direction, profit, close_reason, open_time
               FROM trades
               WHERE close_time IS NOT NULL
               ORDER BY id DESC
               LIMIT ?""",
            (limit,)
        )
        trades = cursor.fetchall()
        
        last_trades = []
        correct_rejects = 0
        incorrect_rejects = 0
        correct_opens = 0
        incorrect_opens = 0
        
        for trade in trades:
            ticket, direction, profit, close_reason, open_time = trade
            pnl = profit or 0
            trade_won = pnl > 0
            
            # Find matching agent decision (within 5 minutes of trade open)
            agent_decision = None
            agent_was_right = None
            
            if open_time:
                cursor.execute(
                    """SELECT agent_decision FROM agent_decisions
                       WHERE datetime(timestamp) BETWEEN datetime(?, '-5 minutes') AND datetime(?, '+5 minutes')
                       ORDER BY ABS(julianday(timestamp) - julianday(?))
                       LIMIT 1""",
                    (open_time, open_time, open_time)
                )
                match = cursor.fetchone()
                if match:
                    agent_decision = match[0]
                    
                    # Determine if agent was right
                    if agent_decision in ("REJECT", "WAIT"):
                        # Agent rejected - right if trade lost
                        agent_was_right = not trade_won
                        if agent_was_right:
                            correct_rejects += 1
                        else:
                            incorrect_rejects += 1
                    elif agent_decision in ("BUY", "SELL", "STRONG_BUY", "STRONG_SELL", "AGREE"):
                        # Agent approved - right if trade won
                        agent_was_right = trade_won
                        if agent_was_right:
                            correct_opens += 1
                        else:
                            incorrect_opens += 1
            
            last_trades.append({
                "ticket": ticket,
                "direction": direction,
                "pnl": round(pnl, 2),
                "close_reason": close_reason,
                "agent_decision": agent_decision,
                "agent_was_right": agent_was_right,
            })
        
        conn.close()
        
        total_decisions = correct_rejects + incorrect_rejects + correct_opens + incorrect_opens
        
        return {
            "last_trades": last_trades,
            "agent_accuracy": {
                "total_decisions": total_decisions,
                "correct_rejects": correct_rejects,
                "incorrect_rejects": incorrect_rejects,
                "correct_opens": correct_opens,
                "incorrect_opens": incorrect_opens,
            }
        }
    except Exception as e:
        log.debug(f"db_writer: failed to get trade feedback: {e}")
        return {
            "last_trades": [],
            "agent_accuracy": {
                "total_decisions": 0,
                "correct_rejects": 0,
                "incorrect_rejects": 0,
                "correct_opens": 0,
                "incorrect_opens": 0,
            }
        }


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
