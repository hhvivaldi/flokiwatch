"""Post-cycle decision flag counters — observability-only.

Computes 6 observational booleans per Floki cycle that resulted in an
open_trade / close_trade / adjust_trade call, and persists to the
floki_decision_flags table.

Purpose: build incidence baseline for future N>=30 pattern analysis.

Invariants:
  - Pure observability. No behavior change.
  - Reads existing DB only (agent_proactive_analyses.tool_trace,
    agent_decisions.tool_trace, trades, trade_adjustments, trade_snapshots).
  - Never raises to caller. All errors logged at DEBUG and swallowed.
  - Idempotent via UNIQUE(cycle_ts, ticket, action). Re-runs are safe.
  - Hooked post-cycle AFTER record_agent_proactive_analysis /
    record_agent_decision succeeds — NOT in the LLM/tool critical path.

Flags (definitions fixed per spec — do not refine without a new ticket):
  - skipped_oracle_in_luna_danger:
      Luna.environment == "DANGER" (from get_luna_brief result in cycle)
      AND no get_oracle_verdict call in tool_trace.
  - skipped_rex_in_luna_danger:
      Luna.environment == "DANGER" AND no call to any of
      {debate_with_rex, get_rex_debate, get_rex_monitor}.
  - contradicted_own_recent_wait:
      The most recent prior Floki decision (either table) within a 30-min
      window strictly before cycle_ts has agent_decision == "WAIT".
  - partial_timeframe_analysis:
      Tool_trace does not contain BOTH "D1" and "H4" in any tool input
      (timeframe or timeframes list).
  - low_volume_entry:
      H1 volume ratio < 0.5. Source priority:
        1) get_indicators H1 result.volume_ratio (currently not emitted —
           future-proofing in case the tool starts returning it).
        2) get_market_regime.evidence entry matching "Volume X.XXx avg".
  - sl_widened_on_loss:
      Any adjust_trade in this cycle has |new_sl - entry| > |old_sl - entry|
      AND trade_snapshots profit_pips at cycle_ts < 0. old_sl resolution:
      latest trade_adjustments row before cycle_ts if any, else trades.sl.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import config
from logger import log
from tz_utils import utc_iso


# -----------------------------------------------------------------------------
# Schema — one table, two indexes. Idempotent CREATE IF NOT EXISTS.
# -----------------------------------------------------------------------------

_SCHEMA_SQL: List[str] = [
    """
    CREATE TABLE IF NOT EXISTS floki_decision_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_ts TEXT NOT NULL,
        ticket INTEGER,
        action TEXT NOT NULL,
        skipped_oracle_in_luna_danger INTEGER NOT NULL DEFAULT 0,
        skipped_rex_in_luna_danger INTEGER NOT NULL DEFAULT 0,
        contradicted_own_recent_wait INTEGER NOT NULL DEFAULT 0,
        partial_timeframe_analysis INTEGER NOT NULL DEFAULT 0,
        low_volume_entry INTEGER NOT NULL DEFAULT 0,
        sl_widened_on_loss INTEGER NOT NULL DEFAULT 0,
        computed_at TEXT NOT NULL,
        source_table TEXT NOT NULL,
        UNIQUE(cycle_ts, ticket, action)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fdf_cycle_ts ON floki_decision_flags(cycle_ts)",
    "CREATE INDEX IF NOT EXISTS idx_fdf_ticket  ON floki_decision_flags(ticket)",
]


_DB_PATH = getattr(config, "HISTORY_DB_PATH", "data/history.db")

_VOLUME_RE = re.compile(r"Volume\s+([0-9]*\.?[0-9]+)\s*x\s+avg", re.IGNORECASE)

_ACTION_TOOLS = ("execute_trade", "close_trade", "adjust_trade")
_ORACLE_TOOLS = ("get_oracle_verdict",)
_REX_TOOLS = ("debate_with_rex", "get_rex_debate", "get_rex_monitor")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


def init_schema() -> None:
    """Ensure the floki_decision_flags table + indexes exist. Safe to call repeatedly."""
    try:
        conn = _connect()
        try:
            for stmt in _SCHEMA_SQL:
                conn.execute(stmt)
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        try:
            log.debug(f"FLAGS | init_schema error (non-blocking): {e}")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Per-flag pure-ish computations
# -----------------------------------------------------------------------------

def _tool_names(trace: List[Dict[str, Any]]) -> List[str]:
    return [t.get("name") for t in trace if isinstance(t, dict)]


def _find_tool_results(trace: List[Dict[str, Any]], name: str) -> List[Dict[str, Any]]:
    return [t for t in trace if isinstance(t, dict) and t.get("name") == name]


def _luna_environment_in_cycle(trace: List[Dict[str, Any]]) -> Optional[str]:
    for t in _find_tool_results(trace, "get_luna_brief"):
        r = t.get("result") or {}
        brief = r.get("brief") or {}
        env = brief.get("environment")
        if env:
            return str(env).upper()
    return None


def flag_skipped_oracle_in_luna_danger(trace: List[Dict[str, Any]]) -> bool:
    if _luna_environment_in_cycle(trace) != "DANGER":
        return False
    names = set(_tool_names(trace))
    return not any(n in names for n in _ORACLE_TOOLS)


def flag_skipped_rex_in_luna_danger(trace: List[Dict[str, Any]]) -> bool:
    if _luna_environment_in_cycle(trace) != "DANGER":
        return False
    names = set(_tool_names(trace))
    return not any(n in names for n in _REX_TOOLS)


def flag_contradicted_own_recent_wait(conn: sqlite3.Connection, cycle_ts: str) -> bool:
    try:
        cur_dt = datetime.fromisoformat(cycle_ts.replace("Z", ""))
    except Exception:
        return False
    window_start = (cur_dt - timedelta(minutes=30)).isoformat() + "Z"
    row = conn.execute(
        """
        SELECT agent_decision FROM (
            SELECT timestamp, agent_decision FROM agent_proactive_analyses
                WHERE timestamp < ? AND timestamp >= ?
            UNION ALL
            SELECT timestamp, agent_decision FROM agent_decisions
                WHERE timestamp < ? AND timestamp >= ?
        )
        ORDER BY timestamp DESC LIMIT 1
        """,
        (cycle_ts, window_start, cycle_ts, window_start),
    ).fetchone()
    if not row:
        return False
    return str(row["agent_decision"] or "").upper() == "WAIT"


def flag_partial_timeframe_analysis(trace: List[Dict[str, Any]]) -> bool:
    tfs_seen: set = set()
    for t in trace:
        if not isinstance(t, dict):
            continue
        inp = t.get("input") or {}
        if not isinstance(inp, dict):
            continue
        if isinstance(inp.get("timeframes"), list):
            for tf in inp["timeframes"]:
                tfs_seen.add(str(tf).upper())
        tf1 = inp.get("timeframe")
        if tf1:
            tfs_seen.add(str(tf1).upper())
    return not ({"D1", "H4"}.issubset(tfs_seen))


def flag_low_volume_entry(trace: List[Dict[str, Any]]) -> bool:
    # 1. Preferred source: get_indicators H1 volume_ratio (not currently emitted,
    #    kept for forward-compat if the tool starts returning it).
    for t in _find_tool_results(trace, "get_indicators"):
        inp = t.get("input") or {}
        if str(inp.get("timeframe", "")).upper() != "H1":
            continue
        r = t.get("result") or {}
        vr = r.get("volume_ratio")
        if isinstance(vr, (int, float)):
            return float(vr) < 0.5
    # 2. Fallback: regex against get_market_regime.evidence string.
    for t in _find_tool_results(trace, "get_market_regime"):
        r = t.get("result") or {}
        for line in (r.get("evidence") or []):
            m = _VOLUME_RE.search(str(line))
            if m:
                try:
                    return float(m.group(1)) < 0.5
                except Exception:
                    continue
    return False


def flag_sl_widened_on_loss(
    conn: sqlite3.Connection,
    trace: List[Dict[str, Any]],
    cycle_ts: str,
) -> bool:
    """Resolves old_sl via the trade_adjustments row produced by THIS cycle's
    adjust_trade tool call (match on ticket + new_sl + source='floki_adjust'
    within the cycle timestamp). Using trade_adjustments.old_sl directly
    avoids self-inclusion when the cycle's own adjustment is already
    persisted before the decision record is written."""
    for t in _find_tool_results(trace, "adjust_trade"):
        inp = t.get("input") or {}
        ticket = inp.get("ticket")
        new_sl = inp.get("new_sl")
        if new_sl is None:
            new_sl = inp.get("sl")
        if ticket is None or new_sl is None:
            continue
        try:
            new_sl_f = float(new_sl)
        except Exception:
            continue
        trade = conn.execute(
            "SELECT direction, open_price FROM trades WHERE ticket=?", (ticket,)
        ).fetchone()
        if not trade or trade["open_price"] is None:
            continue
        entry = float(trade["open_price"])
        adj = conn.execute(
            """SELECT old_sl, timestamp FROM trade_adjustments
               WHERE ticket=? AND source='floki_adjust' AND new_sl=?
                 AND timestamp<=?
               ORDER BY timestamp DESC LIMIT 1""",
            (ticket, new_sl_f, cycle_ts),
        ).fetchone()
        if not adj or adj["old_sl"] is None:
            continue
        old_sl = float(adj["old_sl"])
        old_dist = abs(old_sl - entry)
        new_dist = abs(new_sl_f - entry)
        if new_dist <= old_dist:
            continue
        adj_ts = adj["timestamp"]
        snap = conn.execute(
            """SELECT profit_pips FROM trade_snapshots
               WHERE ticket=? AND timestamp<=? ORDER BY timestamp DESC LIMIT 1""",
            (ticket, adj_ts),
        ).fetchone()
        if snap and snap["profit_pips"] is not None:
            try:
                if float(snap["profit_pips"]) < 0:
                    return True
            except Exception:
                continue
    return False


# -----------------------------------------------------------------------------
# Orchestrator — hook target from main.py
# -----------------------------------------------------------------------------

def compute_and_persist(cycle_ts: str, tool_trace_json: Any, source_table: str) -> None:
    """Compute 6 flags for one cycle and INSERT OR IGNORE per action call.
    Silent on error — never raises."""
    try:
        if not cycle_ts or not tool_trace_json:
            return
        init_schema()
        try:
            trace = tool_trace_json if isinstance(tool_trace_json, list) else json.loads(tool_trace_json)
        except Exception:
            return
        if not isinstance(trace, list):
            return
        action_calls = [
            t for t in trace if isinstance(t, dict) and t.get("name") in _ACTION_TOOLS
        ]
        if not action_calls:
            return
        conn = _connect()
        try:
            f_oracle = int(flag_skipped_oracle_in_luna_danger(trace))
            f_rex = int(flag_skipped_rex_in_luna_danger(trace))
            f_wait = int(flag_contradicted_own_recent_wait(conn, cycle_ts))
            f_partial = int(flag_partial_timeframe_analysis(trace))
            f_vol = int(flag_low_volume_entry(trace))
            f_sl = int(flag_sl_widened_on_loss(conn, trace, cycle_ts))
            now = utc_iso()
            for ac in action_calls:
                name = ac.get("name")
                inp = ac.get("input") or {}
                res = ac.get("result") or {}
                raw_tik = res.get("ticket") if isinstance(res, dict) else None
                if raw_tik is None and isinstance(inp, dict):
                    raw_tik = inp.get("ticket")
                try:
                    ticket = int(raw_tik) if raw_tik is not None else None
                except Exception:
                    ticket = None
                conn.execute(
                    """INSERT OR IGNORE INTO floki_decision_flags
                    (cycle_ts, ticket, action,
                     skipped_oracle_in_luna_danger, skipped_rex_in_luna_danger,
                     contradicted_own_recent_wait, partial_timeframe_analysis,
                     low_volume_entry, sl_widened_on_loss,
                     computed_at, source_table)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        cycle_ts, ticket, name,
                        f_oracle, f_rex, f_wait, f_partial, f_vol, f_sl,
                        now, source_table,
                    ),
                )
            conn.commit()
            try:
                log.info(
                    f"FLAGS | cycle={cycle_ts} src={source_table} "
                    f"actions={[a.get('name') for a in action_calls]} "
                    f"oracle={f_oracle} rex={f_rex} wait={f_wait} "
                    f"partial={f_partial} vol={f_vol} sl={f_sl}"
                )
            except Exception:
                pass
        finally:
            conn.close()
    except Exception as e:
        try:
            log.debug(f"FLAGS | compute_and_persist error (non-blocking): {e}")
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Backfill — one-time CLI-invokable
# -----------------------------------------------------------------------------

def backfill_since(iso_start: str) -> Dict[str, int]:
    init_schema()
    rows: List = []
    conn = _connect()
    try:
        for row in conn.execute(
            "SELECT timestamp, tool_trace FROM agent_proactive_analyses "
            "WHERE timestamp >= ? AND tool_trace IS NOT NULL",
            (iso_start,),
        ).fetchall():
            rows.append((row["timestamp"], row["tool_trace"], "proactive"))
        for row in conn.execute(
            "SELECT timestamp, tool_trace FROM agent_decisions "
            "WHERE timestamp >= ? AND tool_trace IS NOT NULL",
            (iso_start,),
        ).fetchall():
            rows.append((row["timestamp"], row["tool_trace"], "scheduled"))
    finally:
        conn.close()
    cycles = 0
    for ts, tr, src in sorted(rows):
        compute_and_persist(ts, tr, src)
        cycles += 1
    conn = _connect()
    try:
        n_rows = conn.execute("SELECT COUNT(*) c FROM floki_decision_flags").fetchone()["c"]
    finally:
        conn.close()
    return {"cycles_scanned": cycles, "rows_total": int(n_rows)}


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-04-17T00:00:00Z"
    result = backfill_since(start)
    print(f"Backfill since {start}: {result}")
