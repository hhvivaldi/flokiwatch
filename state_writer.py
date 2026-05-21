"""
STATE WRITER - JSON snapshot for dashboard.

FLO-286 / CLAUDE.md Rule 22: every timestamp written here MUST be UTC ISO
with explicit "Z" suffix. Use tz_utils.utc_iso(); never call datetime.now().
The frontend (dashboard/static/tz.js) converts UTC → user-local for display.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional, List

import config
from logger import log
from safety_checks import is_market_open
from executor import executor
from db_writer import record_account_snapshot
from tz_utils import utc_now, utc_iso


_last_valid_account_info: Optional[Dict[str, Any]] = None
_fast_decisions_cache: List[Dict[str, Any]] = []
_fast_decisions_last_ts: Optional[str] = None


def _safe_iso(dt: Optional[datetime]) -> Optional[str]:
    """FLO-286: Always returns UTC ISO with Z suffix. Naive datetimes assumed UTC."""
    if dt is None:
        return None
    try:
        return utc_iso(dt)
    except Exception:
        return None


def _sanitize_for_json(obj):
    """Replace NaN/Inf floats with None (invalid in JSON, crashes FastAPI)."""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _recompute_today_pnl_from_db(now) -> float:
    """FLO-447 — today's realized P&L from authoritative DB sources.

    Sums in order of precedence:
      1. snow_plans.outcome_usd for plans closed today (correctly
         aggregates partial + runner OUT deals via the runtime_reconcile
         path's value-weighted close-price computation).
      2. trades.profit for trades closed today whose ticket is NOT
         referenced by any snow_plans.trade_ticket (captures ghost-guard
         closes and any other non-Snow positions).

    "Today" boundary is UTC 00:00:00 — matches `_today_realized_pnl_usd`
    in agent_tools.py used by the FLO-439 gate.

    Returns 0.0 on any error; caller falls through to the bot's
    counter as a fallback.
    """
    import os as _os
    import sqlite3 as _sqlite3
    db_path = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "data", "history.db"
    )
    if not _os.path.exists(db_path):
        return 0.0
    today_iso = now.strftime("%Y-%m-%dT00:00:00")
    total = 0.0
    snow_tickets: set = set()
    with _sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        # Plan-linked closes
        cur.execute(
            """SELECT trade_ticket, outcome_usd FROM snow_plans
               WHERE closed_at >= ? AND outcome_usd IS NOT NULL""",
            (today_iso,),
        )
        for ticket, usd in cur.fetchall():
            try:
                total += float(usd)
                if ticket is not None:
                    snow_tickets.add(int(ticket))
            except (TypeError, ValueError):
                continue
        # Non-Snow closes (ghost guards, manual exits)
        cur.execute(
            """SELECT ticket, profit FROM trades
               WHERE close_time >= ? AND profit IS NOT NULL""",
            (today_iso,),
        )
        for ticket, profit in cur.fetchall():
            try:
                t = int(ticket) if ticket is not None else None
            except (TypeError, ValueError):
                t = None
            if t is None or t in snow_tickets:
                continue
            try:
                total += float(profit)
            except (TypeError, ValueError):
                continue
    return total


def _build_trade_history(bot_instance: Any, now) -> list:
    """FLO-447 — return a complete list of today's closed trades.

    Precedence:
      1. snow_plans.outcome_usd is the authoritative aggregate per ticket
         (sums partial + runner closes via value-weighted close-price
         reconciliation). When a ticket has a snow_plans entry today,
         use ONLY that entry and SUPPRESS any in-memory fragments for
         the same ticket (the in-memory `closed_trades_today` is
         monitor-side per-event and can hold a single partial fragment
         that misleadingly looks like the whole trade's P&L).
      2. For tickets not covered by snow_plans (ghost-guard closes,
         non-Snow positions), use the in-memory entry; if absent, use
         the trades.profit row directly.
    Output sorted close_time DESC so most recent shows first.
    """
    import os as _os
    import sqlite3 as _sqlite3

    in_memory = list(getattr(bot_instance, "closed_trades_today", None) or [])

    db_path = _os.path.join(
        _os.path.dirname(_os.path.abspath(__file__)), "data", "history.db"
    )
    today_iso = now.strftime("%Y-%m-%dT00:00:00")

    snow_entries: list = []
    snow_tickets: set = set()
    trades_fallback: list = []

    if _os.path.exists(db_path):
        try:
            with _sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """SELECT sp.id, sp.trade_ticket, sp.outcome_usd,
                              sp.outcome_pips, sp.closed_at,
                              tr.direction, tr.volume, tr.open_price,
                              tr.close_price, tr.close_reason
                       FROM snow_plans sp
                       LEFT JOIN trades tr ON tr.ticket = sp.trade_ticket
                       WHERE sp.closed_at >= ? AND sp.outcome_usd IS NOT NULL""",
                    (today_iso,),
                )
                for r in cur.fetchall():
                    (plan_id, ticket, outcome_usd, outcome_pips, closed_at,
                     direction, volume, open_price, close_price, reason) = r
                    if ticket is None:
                        continue
                    snow_tickets.add(int(ticket))
                    snow_entries.append({
                        "ticket": int(ticket),
                        "direction": direction,
                        "volume": volume,
                        "open_price": open_price,
                        "close_price": close_price,
                        "profit": float(outcome_usd) if outcome_usd is not None else None,
                        "close_time": closed_at,
                        "reason": reason,
                        "plan_id": plan_id,
                        "source": "snow_plans",
                    })

                cur.execute(
                    """SELECT ticket, direction, volume, open_price, close_price,
                              profit, close_reason, close_time
                       FROM trades
                       WHERE close_time >= ? AND profit IS NOT NULL""",
                    (today_iso,),
                )
                for r in cur.fetchall():
                    if r[0] is None:
                        continue
                    t = int(r[0])
                    if t in snow_tickets:
                        continue
                    trades_fallback.append({
                        "ticket": t,
                        "direction": r[1],
                        "volume": r[2],
                        "open_price": r[3],
                        "close_price": r[4],
                        "profit": float(r[5]) if r[5] is not None else None,
                        "close_time": r[7],
                        "reason": r[6],
                        "source": "trades",
                    })
        except Exception:
            pass

    # Filter the in-memory list to entries whose ticket is NOT in snow_tickets
    # (snow_plans is authoritative for plan-linked tickets; in-memory
    # entries for the same ticket are partial-close fragments).
    in_memory_filtered = [
        r for r in in_memory
        if (lambda t: t is None or t not in snow_tickets)(
            (lambda x: int(x) if x is not None else None)(r.get("ticket"))
            if isinstance(r, dict) else None
        )
    ]

    # Dedupe in_memory_filtered vs trades_fallback by (ticket, close_time)
    seen = set()
    for r in in_memory_filtered:
        try:
            seen.add((int(r.get("ticket")), str(r.get("close_time") or "")[:19]))
        except Exception:
            continue
    trades_fallback_deduped = []
    for e in trades_fallback:
        key = (e["ticket"], str(e.get("close_time") or "")[:19])
        if key in seen:
            continue
        trades_fallback_deduped.append(e)
        seen.add(key)

    combined = snow_entries + in_memory_filtered + trades_fallback_deduped
    combined.sort(key=lambda x: str(x.get("close_time") or ""), reverse=True)
    return combined


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_sanitize_for_json(payload), f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, path)


def _get_ea_bridge_status() -> Dict[str, Any]:
    """Get EA Bridge status for dashboard display."""
    try:
        enabled = getattr(config, "USE_EA_BRIDGE", False)
        if not enabled:
            return {"enabled": False, "online": False, "spread_pips": None}
        
        from ea_bridge import is_ea_online, read_ea_status
        stale_threshold = getattr(config, "EA_STALE_THRESHOLD_SECONDS", 60)
        online = is_ea_online(stale_threshold)
        
        # Always try to read spread, even if stale (spread from minutes ago is still useful)
        spread_pips = None
        status = read_ea_status(stale_threshold)
        if status:
            spread_pips = status.spread_pips
        
        return {
            "enabled": True,
            "online": online,
            "spread_pips": spread_pips,
        }
    except Exception:
        return {"enabled": False, "online": False, "spread_pips": None}


def write_state(bot_instance: Any) -> None:
    """Write the bot's operational state for dashboard consumption.

    Must never throw exceptions outward (cannot block the bot).
    """
    try:
        # FLO-286: UTC for all timestamps (was naive datetime.now() in local time)
        now = utc_now()

        market_open, market_reason, next_open = is_market_open()

        global _last_valid_account_info
        account_info = None
        try:
            if getattr(bot_instance, "executes_trades", False):
                account_info = executor.get_account_info()
        except Exception:
            account_info = None

        is_fresh_account = False
        if account_info and account_info.get("balance") is not None:
            _last_valid_account_info = account_info
            is_fresh_account = True
        elif _last_valid_account_info is not None:
            account_info = _last_valid_account_info
            log.debug("state_writer: MT5 account_info unavailable, using cache")

        if is_fresh_account:
            record_account_snapshot(account_info)

        daily_stats = getattr(bot_instance, "daily_stats", {}) or {}
        balance_for_pct = None
        if account_info and account_info.get("balance") is not None:
            balance_for_pct = float(account_info["balance"])
        else:
            balance_for_pct = float(getattr(config, "CAPITAL_INICIAL", 0) or 0)

        # FLO-447 (2026-05-19) — recompute today's realized P&L from
        # authoritative DB sources instead of the per-event counter the
        # bot increments in _monitor_cycle. Two failure modes the counter
        # missed:
        #   (a) Snow's `close_partial` fires the partial via executor
        #       directly; monitor_cycle never sees the action, so the
        #       counter is never incremented for the partial.
        #   (b) When the same ticket has multiple OUT deals (partial +
        #       runner-close), the `UPDATE trades SET profit=?` writer
        #       overwrites the profit on the second close rather than
        #       accumulating, so `trades.profit` only holds whichever
        #       close updated last.
        # Both cases are correctly captured in snow_plans.outcome_usd
        # via the runtime_reconcile path (sums all OUT deals VW). Sum
        # that column for today's closed plans, plus trades.profit for
        # today's closed non-plan-linked trades (e.g. ghost-guard
        # closes that don't have a snow_plans row). PLAN-20260518-004
        # surfaced this: actual P&L $+66.51 vs daily_stats.pnl $+40.59.
        try:
            pnl = _recompute_today_pnl_from_db(now)
        except Exception:
            pnl = float(daily_stats.get("pnl", 0.0) or 0.0)
        pnl_percent = (pnl / balance_for_pct * 100) if balance_for_pct else 0.0

        last_analysis = getattr(bot_instance, "last_analysis", None) or {}
        if isinstance(last_analysis, dict):
            # Ensure dashboard never shows stale reactive Agent output
            last_analysis = dict(last_analysis)

            # Normalize fast decisions to `fast_decisions[]` (last 3, newest-first)
            try:
                global _fast_decisions_cache, _fast_decisions_last_ts

                existing = last_analysis.get("fast_decisions")
                if isinstance(existing, list):
                    # Keep only dict entries and cap.
                    _fast_decisions_cache = [x for x in existing if isinstance(x, dict)][:3]

                latest = last_analysis.get("fast_decision")
                if isinstance(latest, dict):
                    ts = latest.get("timestamp")
                    if ts and ts != _fast_decisions_last_ts:
                        # De-dup by timestamp.
                        _fast_decisions_cache = [x for x in _fast_decisions_cache if x.get("timestamp") != ts]
                        _fast_decisions_cache.insert(0, latest)
                        _fast_decisions_cache = _fast_decisions_cache[:3]
                        _fast_decisions_last_ts = ts

                last_analysis["fast_decisions"] = _fast_decisions_cache
                last_analysis.pop("fast_decision", None)
            except Exception as e:
                log.debug(f"state_writer: failed to normalize fast_decisions: {e}")

        # last_known_price: persistent in bot_instance, never cleared.
        last_known_price = getattr(bot_instance, "last_known_price", None)
        current_price = last_analysis.get("current_price")
        if current_price is not None:
            last_known_price = float(current_price)
            bot_instance.last_known_price = last_known_price

        # FLO-129: Daily change % from MT5 session_close
        price_daily_change_pct = None
        prev_d1_close = None
        try:
            from mt5_safe import mt5 as _mt5  # FLO-348
            _xau_info = _mt5.symbol_info("XAUUSD")
            if _xau_info and last_known_price:
                _sc = getattr(_xau_info, "session_close", 0)
                if _sc and _sc > 0:
                    prev_d1_close = round(float(_sc), 2)
                    price_daily_change_pct = round(((last_known_price - _sc) / _sc) * 100, 2)
        except Exception as e:
            log.debug(f"state_writer: MT5 session_close error: {e}")

        positions = []
        try:
            if getattr(bot_instance, "executes_trades", False):
                from monitor import monitor
                open_positions = executor.get_open_positions()
                positions = []
                for p in open_positions:
                    be_info = monitor.get_be_info(p.ticket)
                    positions.append({
                        "ticket": p.ticket,
                        "direction": p.direction,
                        "volume": p.volume,
                        "open_price": p.open_price,
                        "current_price": p.current_price,
                        "sl": p.sl,
                        "tp": p.tp,
                        "profit": p.profit,
                        "profit_pips": p.profit_pips,
                        "open_time": _safe_iso(p.open_time),
                        "comment": p.comment,
                        "phase": monitor.get_position_phase(p.ticket),
                        "be_trigger_pips": be_info.get("be_trigger_pips"),
                        "be_remaining_pips": be_info.get("be_remaining_pips"),
                    })
        except Exception as e:
            log.debug(f"state_writer: error getting positions: {e}")

        bot_status = "OPERATIONAL" if getattr(bot_instance, "running", False) else "OFFLINE"

        session_start = getattr(bot_instance, "session_start_time", None)
        uptime_seconds = None
        if session_start:
            try:
                uptime_seconds = int((now - session_start).total_seconds())
            except Exception:
                uptime_seconds = None

        if market_open:
            expected_interval = int(getattr(config, "ANALYSIS_INTERVAL_SECONDS", 300))
        elif market_reason and "Weekend" in market_reason:
            expected_interval = 300
        else:
            expected_interval = 60

        # FLO-298: Maintenance mode flag. True when Floki's primary model (Qwen)
        # is unavailable (Arrearage / billing / auth / 451). Dashboard shows a
        # clean banner instead of technical errors; Floki's decision card is
        # hidden. Source of truth: AIAgent._qwen_unavailable, set/cleared by
        # ai_agent.py's request-handler (FLO-297).
        maintenance_mode = False
        try:
            from ai_agent import get_agent as _get_agent
            _ai = _get_agent()
            maintenance_mode = bool(getattr(_ai, "_qwen_unavailable", False))
        except Exception:
            maintenance_mode = False

        state = {
            "timestamp": utc_iso(now),  # FLO-286: Z suffix, was +00:00
            "_expected_update_interval_seconds": expected_interval,
            "maintenance_mode": maintenance_mode,  # FLO-298
            "bot": {
                "status": bot_status,
                "mode": getattr(bot_instance, "mode", getattr(config, "TRADING_MODE", "UNKNOWN")),
                "running": bool(getattr(bot_instance, "running", False)),
                "session_start": _safe_iso(session_start),
                "session_analyses": int(getattr(bot_instance, "session_analyses", 0) or 0),
                "uptime_seconds": uptime_seconds,
            },
            "market": {
                "is_open": bool(market_open),
                "reason": market_reason or "",
                "next_open": _safe_iso(next_open),
            },
            "account": account_info
            or {
                "balance": None,
                "equity": None,
                "margin": None,
                "free_margin": None,
                "profit": None,
                "leverage": None,
                "currency": None,
            },
            "daily_stats": {
                "date": str(daily_stats.get("date")) if daily_stats.get("date") else str(now.date()),
                "trades": int(daily_stats.get("trades", 0) or 0),
                "wins": int(daily_stats.get("wins", 0) or 0),
                "losses": int(daily_stats.get("losses", 0) or 0),
                "breakevens": int(daily_stats.get("breakevens", 0) or 0),
                "pnl": pnl,
                "pnl_percent": round(pnl_percent, 2),
            },
            "last_known_price": last_known_price,
            "price_daily_change_pct": price_daily_change_pct,
            "prev_d1_close": prev_d1_close,
            "last_analysis": last_analysis,
            "positions": positions,
            # FLO-447 — supplement in-memory closed_trades_today with any
            # DB-confirmed closes today that the bot's monitor missed
            # (Snow partials/runners + ghost-guard closes). Dedupes by
            # close_time + ticket so reruns don't double-count.
            "trade_history": _build_trade_history(bot_instance, now),
            "ea_bridge": _get_ea_bridge_status(),
            "ml_enabled": bool(getattr(config, "ML_ENABLED", False)),  # FLO-187
            "multi_tf_indicators": {},  # FLO-221: populated below from agent data
            "agent_memory": None,
        }

        # Force null even if older state formats injected data elsewhere
        state["agent_memory"] = None

        # FLO-221: Multi-TF indicators from agent data
        try:
            _agent_data = getattr(bot_instance, "_last_agent_data", None)
            if isinstance(_agent_data, dict):
                _mtf = _agent_data.get("multi_tf_indicators")
                if isinstance(_mtf, dict) and _mtf:
                    state["multi_tf_indicators"] = _mtf
        except Exception:
            pass

        # FLO-223: Pivot Points from agent data
        try:
            _agent_data2 = getattr(bot_instance, "_last_agent_data", None)
            if isinstance(_agent_data2, dict):
                _pp = _agent_data2.get("pivot_points")
                if isinstance(_pp, dict) and _pp:
                    state["pivot_points"] = _pp
        except Exception:
            pass

        # FLO-122: Inject market_context from MT5 (shared fetcher with 60s cache)
        try:
            from market_context_fetcher import fetch_market_context
            _mc = fetch_market_context()
            if isinstance(_mc, dict) and _mc:
                state["market_context"] = _mc
        except Exception as e:
            log.debug(f"state_writer: market_context fetch error: {e}")

        # FLO-139: Inject market regime from bot instance
        try:
            _regime = getattr(bot_instance, "_last_regime_context", None)
            if isinstance(_regime, dict) and _regime.get("regime"):
                _src = "fast" if "Fast detection" in str(_regime.get("evidence", [])) else "ADX"
                state["market_regime"] = {
                    "regime": _regime["regime"],
                    "confidence": _regime.get("confidence"),
                    "duration": _regime.get("duration_display"),
                    "stability": _regime.get("stability"),
                    "adx": _regime.get("adx"),
                    "atr_ratio": _regime.get("atr_ratio"),
                    "transition": _regime.get("transition"),
                    "src": _src,
                }
            # FLO-452 — surface the D1 trend score for Floki's STEP-0 check,
            # the validator gate, and the specialist Technical voter.
            if isinstance(_regime, dict) and _regime.get("d1_trend_score") is not None:
                state["d1_trend_score"] = _regime.get("d1_trend_score")
        except Exception as e:
            log.debug(f"state_writer: market_regime error: {e}")

        # FLO-143: Inject Floki's next scheduled check time
        try:
            _nc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_next_check.json")
            if os.path.exists(_nc_path):
                with open(_nc_path, "r", encoding="utf-8") as _ncf:
                    _nc = json.load(_ncf)
                if isinstance(_nc, dict) and _nc.get("next_check_at"):
                    state["floki_next_check_at"] = _nc["next_check_at"]
        except Exception:
            pass

        # FLO-146 Bug 1: Inject active thesis
        try:
            _thesis_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "active_thesis.json")
            if os.path.exists(_thesis_path):
                with open(_thesis_path, "r", encoding="utf-8") as _tf:
                    _thesis = json.load(_tf)
                if isinstance(_thesis, dict) and _thesis.get("direction_bias"):
                    state["active_thesis"] = {
                        "direction_bias": _thesis.get("direction_bias"),
                        "key_levels": _thesis.get("key_levels", []),
                        "conditions": _thesis.get("conditions", []),
                        "decision": _thesis.get("decision"),
                        "confidence": _thesis.get("confidence"),
                        "timestamp": _thesis.get("timestamp"),
                        "price_at_decision": _thesis.get("price_at_decision"),
                    }
        except Exception:
            pass

        # FLO-146 Bug 2+3: Inject Simba wake conditions
        try:
            _wc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_wake_conditions.json")
            if os.path.exists(_wc_path):
                with open(_wc_path, "r", encoding="utf-8") as _wcf:
                    _wc = json.load(_wcf)
                if isinstance(_wc, dict):
                    conditions = _wc.get("conditions", [])
                    state["wake_conditions"] = {
                        "count": len(conditions) if isinstance(conditions, list) else 0,
                        "conditions": conditions if isinstance(conditions, list) else [],
                        "max_sleep_minutes": _wc.get("max_sleep_minutes"),
                        "last_wake_at": _wc.get("last_wake_at"),
                    }
        except Exception:
            pass

        # FLO-263: Pending orders for dashboard
        try:
            if getattr(config, "PENDING_ORDERS_ENABLED", False):
                _pending = executor.get_pending_orders()
                state["pending_orders"] = _pending if _pending else []
            else:
                state["pending_orders"] = []
        except Exception:
            state["pending_orders"] = []

        # FLO-376: Snow plan summary for dashboard card.
        # Reads snow_plans directly via snow.db helpers (no schema
        # duplication). Best-effort — any failure leaves `snow` absent
        # and the frontend hides the card. Capped at 3 active plans
        # (sorted newest-first) + the most-recent CLOSED plan with
        # non-null outcome.
        try:
            from snow import db as _snow_db
            from snow.schema import PlanStatus as _PlanStatus
            import json as _json
            _SNOW_NON_TERMINAL = (
                _PlanStatus.PENDING.value, _PlanStatus.TRIGGERED.value,
                _PlanStatus.ACTIVE.value, _PlanStatus.CLOSING.value,
            )

            def _snow_plan_summary(row: Dict[str, Any]) -> Dict[str, Any]:
                try:
                    pj = _json.loads(row.get("plan_json") or "{}")
                except Exception:
                    pj = {}
                analysis = pj.get("analysis") or {}
                entry = pj.get("entry") or {}
                thesis = str(analysis.get("thesis") or "")
                return {
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "schema_version": row.get("schema_version"),
                    "direction": entry.get("direction"),
                    "volume": entry.get("volume"),
                    "initial_sl": entry.get("initial_sl"),
                    "initial_tp": entry.get("initial_tp"),
                    "created_at": row.get("created_at"),
                    "expires_at": row.get("expires_at"),
                    "entered_at": row.get("entered_at"),
                    "trade_ticket": row.get("trade_ticket"),
                    "thesis_short": (thesis[:140] + "…") if len(thesis) > 140 else thesis,
                    "confidence": analysis.get("confidence"),
                    "regime_assumed": analysis.get("regime_assumed"),
                    "setup_type": analysis.get("setup_type"),  # v3+ only
                    "context_tags": analysis.get("context_tags"),  # v3+ only
                    "n_management": len(pj.get("management") or []),
                    "n_exit": len(pj.get("exit") or []),
                }

            active_rows = _snow_db.list_plans_by_status(
                _SNOW_NON_TERMINAL, limit=3,
            )
            active_summaries = [_snow_plan_summary(r) for r in active_rows]

            # Most-recent CLOSED plan with a non-null outcome.
            last_closed_summary = None
            closed_rows = _snow_db.list_plans_by_status(
                (_PlanStatus.CLOSED.value,), limit=10,
            )
            for r in closed_rows:
                if r.get("outcome_pips") is None:
                    continue
                summary = _snow_plan_summary(r)
                summary["closed_at"] = r.get("closed_at")
                summary["outcome_pips"] = r.get("outcome_pips")
                summary["outcome_usd"] = r.get("outcome_usd")
                # Duration in minutes — entered_at → closed_at if both present.
                try:
                    from datetime import datetime as _dt
                    def _parse(s):
                        if s and s.endswith("Z"):
                            s = s[:-1] + "+00:00"
                        return _dt.fromisoformat(s) if s else None
                    e_ts = _parse(r.get("entered_at"))
                    c_ts = _parse(r.get("closed_at"))
                    if e_ts and c_ts:
                        summary["duration_min"] = round(
                            (c_ts - e_ts).total_seconds() / 60.0, 1,
                        )
                    else:
                        summary["duration_min"] = None
                except Exception:
                    summary["duration_min"] = None
                last_closed_summary = summary
                break

            state["snow"] = {
                "active_count": len(active_summaries),
                "active_plans": active_summaries,
                "last_closed": last_closed_summary,
                "schema_version_current": getattr(
                    __import__("snow"), "SCHEMA_VERSION", None,
                ),
            }
        except Exception as _se:
            log.debug(f"state_writer: snow summary skipped: {_se}")
            # Don't surface `snow` at all — frontend hides on absence.

        # Diagnostic: surface data_needs from agent result
        try:
            _pa = last_analysis.get("proactive_analysis") if isinstance(last_analysis, dict) else None
            _ad = last_analysis.get("agent_decision") if isinstance(last_analysis, dict) else None
            _dn = None
            if isinstance(_pa, dict):
                _dn = _pa.get("data_needs")
            if not _dn and isinstance(_ad, dict):
                _dn = _ad.get("data_needs")
            if _dn:
                last_analysis["data_needs"] = _dn
        except Exception:
            pass

        _atomic_write_json(getattr(config, "DASHBOARD_STATE_FILE", "data/bot_state.json"), state)

    except Exception as e:
        log.debug(f"state_writer: failed to write state: {e}")


def add_closed_trade(bot_instance: Any, trade: Dict[str, Any]) -> None:
    """Accumulate a closed trade in the bot state (daily memory for dashboard)."""
    try:
        if not hasattr(bot_instance, "closed_trades_today") or bot_instance.closed_trades_today is None:
            bot_instance.closed_trades_today = []
        bot_instance.closed_trades_today.append(trade)
    except Exception as e:
        log.debug(f"state_writer: failed to add closed trade: {e}")
