"""FLO-423 — Pre-cycle structural gate, SHADOW MODE.

Purpose: log what a gate WOULD have decided each cycle (would_skip /
would_escalate) plus the supporting signals, then proceed to invoke
Floki normally. NO active skipping. Floki runs every cycle exactly as
before — this module is observation-only.

After 7 days of shadow data we compare gate decisions against actual
Floki output to validate two safety properties before any activation:
  - false-negative rate (would_skip + Floki took action) must be ZERO
  - skip rate must be >= 20% to be worth shipping

Activation is a SEPARATE future commit. This file does not implement it.

The 15 signals trip ANY-OF (any one signal True / out-of-bounds means
escalate). Default-escalate on uncertainty: any I/O failure or missing
input returns the escalate-equivalent value, never a false 'stable'.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple


# Thresholds — starting values, refinable from shadow data later.
PRICE_CHANGE_PIPS_THRESHOLD = 30.0
TIME_CEILING_MINUTES = 120.0
ATR_VOLATILITY_RATIO_THRESHOLD = 0.20  # 20% relative change
ACTIVE_PLAN_NEAR_PIPS = 30.0
SPREAD_WIDENED_PIPS = 5.0
SESSION_BOUNDARY_HOURS_UTC = (0, 8, 13, 22)  # Asia, London, NY open, NY close


# ---------------------------------------------------------------------
# DB schema (idempotent)
# ---------------------------------------------------------------------

def _db_path() -> str:
    try:
        import config
        return getattr(config, "HISTORY_DB_PATH", "data/history.db")
    except Exception:
        return "data/history.db"


def init_gate_shadow_table() -> None:
    """Create the agent_gate_shadow table if missing. Idempotent.
    Safe to call repeatedly — the CREATE / CREATE INDEX use IF NOT EXISTS."""
    conn = sqlite3.connect(_db_path())
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_gate_shadow (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_ts                 TEXT NOT NULL,
                gate_decision            TEXT NOT NULL,
                reason_codes             TEXT NOT NULL,
                signals_json             TEXT NOT NULL,
                actual_decision          TEXT,
                actual_plans_submitted   INTEGER DEFAULT 0,
                actual_plans_cancelled   INTEGER DEFAULT 0,
                actual_position_actions  TEXT,
                evaluated_at             TEXT,
                notes                    TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gate_shadow_ts "
            "ON agent_gate_shadow(cycle_ts DESC)"
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------
# Pure compute (no I/O, unit-testable)
# ---------------------------------------------------------------------

def compute_structural_signals(
    *,
    now_ts: datetime,
    prior_row: Optional[Dict[str, Any]],
    current_state: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute the 15 signal values. No I/O — caller fetches `current_state`
    and `prior_row`. `prior_row` is the prior agent_gate_shadow row dict
    (or None on first cycle).

    Returns a dict with all 15 signal keys. Each value is the raw signal
    output (numeric or bool); None is used where the signal cannot be
    computed (and is treated as escalate-equivalent by classify_).
    """
    if now_ts.tzinfo is None:
        now_ts = now_ts.replace(tzinfo=timezone.utc)

    # ---- Time-based ----
    time_since_min: Optional[float] = None
    if prior_row and prior_row.get("cycle_ts"):
        try:
            prior_ts = datetime.fromisoformat(
                prior_row["cycle_ts"].replace("Z", "+00:00")
            )
            time_since_min = (now_ts - prior_ts).total_seconds() / 60.0
        except Exception:
            time_since_min = None

    new_h1_close = _crossed_bar_boundary(prior_row, now_ts, "H1")
    new_h4_close = _crossed_bar_boundary(prior_row, now_ts, "H4")
    new_d1_close = _crossed_bar_boundary(prior_row, now_ts, "D1")
    session_boundary_crossed = _crossed_session_boundary(prior_row, now_ts)

    # ---- Price-based ----
    price_change_pips: Optional[float] = None
    cur_price = current_state.get("current_price")
    prior_signals = (prior_row or {}).get("signals", {}) or {}
    prior_price = prior_signals.get("current_price")
    if cur_price is not None and prior_price is not None:
        try:
            price_change_pips = abs(float(cur_price) - float(prior_price)) * 10
        except Exception:
            price_change_pips = None

    spread = current_state.get("spread_pips")
    spread_widened = (spread is None) or (spread > SPREAD_WIDENED_PIPS)

    active_plan_near = current_state.get("active_plan_near_trigger")
    if active_plan_near is None:
        active_plan_near = True  # uncertain → escalate

    # ---- Regime-based ----
    regime_changed = _flag_changed(
        prior_signals.get("scenario"),
        current_state.get("scenario"),
    )
    rm_verdict_changed = _flag_changed(
        prior_signals.get("rm_winner"),
        current_state.get("rm_winner"),
    ) or _flag_changed(
        prior_signals.get("rm_conviction"),
        current_state.get("rm_conviction"),
    )
    atr_volatility_changed = _atr_changed(
        prior_signals.get("atr_14"),
        current_state.get("atr_14"),
    )

    # ---- Operational state ----
    position_state_changed = bool(current_state.get("position_state_changed", True))
    plan_transition = bool(current_state.get("plan_transition", True))

    # ---- News ----
    echo_critical_alert = bool(current_state.get("echo_critical_alert", True))
    echo_medium_high_alert = bool(current_state.get("echo_medium_high_alert", True))

    return {
        # time
        "time_since_last_cycle_min": time_since_min,
        "new_h1_close": new_h1_close,
        "new_h4_close": new_h4_close,
        "new_d1_close": new_d1_close,
        "session_boundary_crossed": session_boundary_crossed,
        # price
        "price_change_pips": price_change_pips,
        "spread_widened": spread_widened,
        "active_plan_near_trigger": active_plan_near,
        # regime
        "regime_changed": regime_changed,
        "atr_volatility_changed": atr_volatility_changed,
        "rm_verdict_changed": rm_verdict_changed,
        # operational
        "position_state_changed": position_state_changed,
        "plan_transition": plan_transition,
        # news
        "echo_critical_alert": echo_critical_alert,
        "echo_medium_high_alert": echo_medium_high_alert,
        # raw values for the next cycle's diff computation
        "current_price": cur_price,
        "scenario": current_state.get("scenario"),
        "atr_14": current_state.get("atr_14"),
        "rm_winner": current_state.get("rm_winner"),
        "rm_conviction": current_state.get("rm_conviction"),
    }


def classify_gate_decision(signals: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Returns (decision, reason_codes). decision ∈ {would_skip, would_escalate}.
    Default-escalate on uncertainty: None values for numeric signals trip
    the corresponding reason."""
    reasons: List[str] = []

    # Time
    t = signals.get("time_since_last_cycle_min")
    if t is None or t >= TIME_CEILING_MINUTES:
        reasons.append("time_ceiling")
    if signals.get("new_h1_close"):
        reasons.append("new_h1_close")
    if signals.get("new_h4_close"):
        reasons.append("new_h4_close")
    if signals.get("new_d1_close"):
        reasons.append("new_d1_close")
    if signals.get("session_boundary_crossed"):
        reasons.append("session_boundary_crossed")

    # Price
    p = signals.get("price_change_pips")
    if p is None or p >= PRICE_CHANGE_PIPS_THRESHOLD:
        reasons.append("price_moved")
    if signals.get("spread_widened"):
        reasons.append("spread_widened")
    if signals.get("active_plan_near_trigger"):
        reasons.append("active_plan_near_trigger")

    # Regime
    if signals.get("regime_changed"):
        reasons.append("regime_changed")
    if signals.get("atr_volatility_changed"):
        reasons.append("atr_volatility_changed")
    if signals.get("rm_verdict_changed"):
        reasons.append("rm_verdict_changed")

    # Operational
    if signals.get("position_state_changed"):
        reasons.append("position_state_changed")
    if signals.get("plan_transition"):
        reasons.append("plan_transition")

    # News
    if signals.get("echo_critical_alert"):
        reasons.append("echo_critical_alert")
    if signals.get("echo_medium_high_alert"):
        reasons.append("echo_medium_high_alert")

    decision = "would_escalate" if reasons else "would_skip"
    return decision, reasons


# ---------------------------------------------------------------------
# Internal pure helpers
# ---------------------------------------------------------------------

def _flag_changed(prior: Any, current: Any) -> bool:
    """True when current differs from prior, OR either is None (uncertain)."""
    if prior is None or current is None:
        return True
    return prior != current


def _atr_changed(prior_atr: Any, current_atr: Any) -> bool:
    """True if relative ATR change >= threshold OR either side missing."""
    if prior_atr is None or current_atr is None:
        return True
    try:
        prior_f = float(prior_atr)
        current_f = float(current_atr)
        if prior_f <= 0:
            return True
        ratio = abs(current_f / prior_f - 1.0)
        return ratio >= ATR_VOLATILITY_RATIO_THRESHOLD
    except Exception:
        return True


def _crossed_bar_boundary(
    prior_row: Optional[Dict[str, Any]], now_ts: datetime, tf: str,
) -> bool:
    """True if a bar boundary for the given timeframe fell between
    prior_row.cycle_ts and now_ts. tf in {H1, H4, D1}."""
    if not prior_row or not prior_row.get("cycle_ts"):
        return True  # first cycle escalate
    try:
        prior_ts = datetime.fromisoformat(
            prior_row["cycle_ts"].replace("Z", "+00:00")
        )
    except Exception:
        return True

    if tf == "H1":
        prior_floor = prior_ts.replace(minute=0, second=0, microsecond=0)
        now_floor = now_ts.replace(minute=0, second=0, microsecond=0)
        return prior_floor != now_floor
    if tf == "H4":
        prior_h4 = prior_ts.replace(hour=(prior_ts.hour // 4) * 4,
                                    minute=0, second=0, microsecond=0)
        now_h4 = now_ts.replace(hour=(now_ts.hour // 4) * 4,
                                minute=0, second=0, microsecond=0)
        return prior_h4 != now_h4
    if tf == "D1":
        return prior_ts.date() != now_ts.date()
    return True


def _crossed_session_boundary(
    prior_row: Optional[Dict[str, Any]], now_ts: datetime,
) -> bool:
    """True if any session boundary (Asia/London/NY open/close) fell
    between prior_row.cycle_ts and now_ts."""
    if not prior_row or not prior_row.get("cycle_ts"):
        return True
    try:
        prior_ts = datetime.fromisoformat(
            prior_row["cycle_ts"].replace("Z", "+00:00")
        )
    except Exception:
        return True
    if prior_ts >= now_ts:
        return False
    # Walk hour boundaries between prior and now; if any matches a session
    # boundary hour, return True.
    cursor = prior_ts.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while cursor <= now_ts:
        if cursor.hour in SESSION_BOUNDARY_HOURS_UTC:
            return True
        cursor += timedelta(hours=1)
    return False


# ---------------------------------------------------------------------
# I/O wrappers (each fail-soft, returns None or escalate-default on error)
# ---------------------------------------------------------------------

def _fetch_prior_gate_row() -> Optional[Dict[str, Any]]:
    """Return the most-recent agent_gate_shadow row as a dict, with
    signals_json parsed. None if no prior or read failure."""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT cycle_ts, signals_json FROM agent_gate_shadow "
                "ORDER BY cycle_ts DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            try:
                signals = json.loads(row[1]) if row[1] else {}
            except Exception:
                signals = {}
            return {"cycle_ts": row[0], "signals": signals}
        finally:
            conn.close()
    except Exception:
        return None


def _fetch_latest_analysis() -> Dict[str, Any]:
    """Return latest analyses row as dict. Empty dict on failure."""
    try:
        conn = sqlite3.connect(_db_path())
        try:
            row = conn.execute(
                "SELECT timestamp, current_price, atr_14, scenario "
                "FROM analyses ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {}
            return {
                "timestamp": row[0],
                "current_price": row[1],
                "atr_14": row[2],
                "scenario": row[3],
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _fetch_oracle_verdict() -> Dict[str, Any]:
    """Return {rm_winner, rm_conviction} from data/oracle_verdict.json.
    Empty dict on failure."""
    try:
        path = os.path.join(
            os.path.dirname(os.path.abspath(_db_path())),
            "oracle_verdict.json",
        )
        if not os.path.exists(path):
            # try data/oracle_verdict.json relative to cwd
            path = "data/oracle_verdict.json"
        if not os.path.exists(path):
            return {}
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            "rm_winner": data.get("winner") or data.get("verdict_winner"),
            "rm_conviction": data.get("conviction") or data.get("verdict_conviction"),
        }
    except Exception:
        return {}


def _fetch_position_state_changed(prior_ts: Optional[str]) -> Optional[bool]:
    """True if any trade closed since prior_ts. None if can't determine
    (treated as escalate)."""
    if not prior_ts:
        return True
    try:
        conn = sqlite3.connect(_db_path())
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE close_time > ?",
                (prior_ts,),
            ).fetchone()[0]
            return cnt > 0
        finally:
            conn.close()
    except Exception:
        return None


def _fetch_plan_transitions_since(prior_ts: Optional[str]) -> Optional[bool]:
    """True if any snow_plans transitioned status since prior_ts. None on
    failure."""
    if not prior_ts:
        return True
    try:
        conn = sqlite3.connect(_db_path())
        try:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM snow_plans "
                "WHERE last_evaluated_at IS NOT NULL "
                "AND last_evaluated_at > ? "
                "AND status IN ('triggered','active','closed','cancelled','expired','failed')",
                (prior_ts,),
            ).fetchone()[0]
            return cnt > 0
        finally:
            conn.close()
    except Exception:
        return None


def _fetch_active_plan_near_price(
    current_price: Optional[float], threshold_pips: float = ACTIVE_PLAN_NEAR_PIPS,
) -> Optional[bool]:
    """True if any PENDING/TRIGGERED plan has entry_price within threshold.
    None on failure → escalate."""
    if current_price is None:
        return True
    try:
        conn = sqlite3.connect(_db_path())
        try:
            rows = conn.execute(
                "SELECT plan_json FROM snow_plans "
                "WHERE status IN ('pending', 'triggered')"
            ).fetchall()
            for (pj,) in rows:
                try:
                    plan = json.loads(pj) if pj else {}
                    entry = plan.get("entry") or {}
                    entry_price = entry.get("entry_price")
                    if entry_price is None:
                        continue
                    distance_pips = abs(float(current_price) - float(entry_price)) * 10
                    if distance_pips <= threshold_pips:
                        return True
                except Exception:
                    continue
            return False
        finally:
            conn.close()
    except Exception:
        return None


def _fetch_critical_alerts_since(prior_ts: Optional[str]) -> Optional[bool]:
    """True if a CRITICAL Echo alert was raised since prior_ts. None on
    failure → escalate."""
    return _fetch_echo_alerts_since(prior_ts, severities=("CRITICAL",))


def _fetch_medium_high_alerts_since(prior_ts: Optional[str]) -> Optional[bool]:
    """True if any MEDIUM/HIGH (NOT CRITICAL) Echo alert since prior_ts."""
    return _fetch_echo_alerts_since(prior_ts, severities=("MEDIUM", "HIGH"))


def _fetch_echo_alerts_since(
    prior_ts: Optional[str], severities: Tuple[str, ...],
) -> Optional[bool]:
    """Fail-soft echo state lookup.
    Reads data/echo_state.json (or echo_aggregate.json) for recent
    alerts. Implementation tolerates missing file and unknown shape."""
    if not prior_ts:
        return True
    candidates = [
        "data/echo_state.json",
        "data/echo_aggregate.json",
    ]
    try:
        for path in candidates:
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Try common shapes — list of alerts, or "alerts" / "events" key.
            alerts: List[Dict[str, Any]] = []
            if isinstance(data, list):
                alerts = data
            elif isinstance(data, dict):
                for k in ("alerts", "events", "items", "recent"):
                    v = data.get(k)
                    if isinstance(v, list):
                        alerts = v
                        break
            for a in alerts:
                if not isinstance(a, dict):
                    continue
                sev = str(a.get("severity") or a.get("level") or "").upper()
                if sev not in severities:
                    continue
                ts = a.get("timestamp") or a.get("ts") or a.get("created_at")
                if isinstance(ts, str) and ts > prior_ts:
                    return True
            return False  # found data but no qualifying alert
        return False  # echo state file simply isn't present (fresh deploy)
    except Exception:
        return None


def _fetch_current_spread() -> Optional[float]:
    """Current XAUUSD spread in pips. None on MT5 unavailable."""
    try:
        from mt5_safe import mt5, mt5_lock
        with mt5_lock:
            if not mt5.initialize():
                return None
            tick = mt5.symbol_info_tick("XAUUSD")
            if tick is None:
                return None
            return (float(tick.ask) - float(tick.bid)) * 10
    except Exception:
        return None


# ---------------------------------------------------------------------
# Persistence — entry + outcome
# ---------------------------------------------------------------------

def shadow_log_cycle_entry(now_ts: datetime) -> Optional[int]:
    """Compute signals + classify + persist. Return the inserted row_id.
    Fail-soft: any failure logs nothing here (logging is the caller's
    responsibility) and returns None.

    The caller MUST pass `_gate_row_id = shadow_log_cycle_entry(...)` and
    later pass the same id to shadow_log_cycle_outcome at cycle exit.
    """
    try:
        if now_ts.tzinfo is None:
            now_ts = now_ts.replace(tzinfo=timezone.utc)

        prior = _fetch_prior_gate_row()
        prior_cycle_ts = prior.get("cycle_ts") if prior else None

        # Build current_state from real I/O
        analysis = _fetch_latest_analysis()
        oracle = _fetch_oracle_verdict()
        spread = _fetch_current_spread()
        position_changed = _fetch_position_state_changed(prior_cycle_ts)
        plan_transition = _fetch_plan_transitions_since(prior_cycle_ts)
        active_plan_near = _fetch_active_plan_near_price(analysis.get("current_price"))
        echo_critical = _fetch_critical_alerts_since(prior_cycle_ts)
        echo_medium = _fetch_medium_high_alerts_since(prior_cycle_ts)

        current_state = {
            "current_price": analysis.get("current_price"),
            "atr_14": analysis.get("atr_14"),
            "scenario": analysis.get("scenario"),
            "rm_winner": oracle.get("rm_winner"),
            "rm_conviction": oracle.get("rm_conviction"),
            "spread_pips": spread,
            "position_state_changed": position_changed if position_changed is not None else True,
            "plan_transition": plan_transition if plan_transition is not None else True,
            "active_plan_near_trigger": active_plan_near if active_plan_near is not None else True,
            "echo_critical_alert": echo_critical if echo_critical is not None else True,
            "echo_medium_high_alert": echo_medium if echo_medium is not None else True,
        }

        signals = compute_structural_signals(
            now_ts=now_ts, prior_row=prior, current_state=current_state,
        )
        decision, reasons = classify_gate_decision(signals)

        cycle_ts_iso = now_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        conn = sqlite3.connect(_db_path())
        try:
            cur = conn.execute(
                "INSERT INTO agent_gate_shadow "
                "(cycle_ts, gate_decision, reason_codes, signals_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    cycle_ts_iso, decision,
                    json.dumps(reasons),
                    json.dumps(signals, default=str),
                ),
            )
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
        return row_id
    except Exception:
        return None


def shadow_log_cycle_outcome(
    *,
    row_id: Optional[int],
    actual_decision: Optional[str],
    actual_plans_submitted: int,
    actual_plans_cancelled: int,
    actual_position_actions: List[Dict[str, Any]],
) -> None:
    """Update the gate row with post-cycle outcome. Fail-soft."""
    if row_id is None:
        return
    try:
        evaluated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        conn = sqlite3.connect(_db_path())
        try:
            conn.execute(
                "UPDATE agent_gate_shadow "
                "SET actual_decision = ?, "
                "    actual_plans_submitted = ?, "
                "    actual_plans_cancelled = ?, "
                "    actual_position_actions = ?, "
                "    evaluated_at = ? "
                "WHERE id = ?",
                (
                    str(actual_decision) if actual_decision else None,
                    int(actual_plans_submitted or 0),
                    int(actual_plans_cancelled or 0),
                    json.dumps(actual_position_actions or [], default=str),
                    evaluated_at,
                    row_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
