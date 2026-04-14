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
            import MetaTrader5 as _mt5
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
            "trade_history": getattr(bot_instance, "closed_trades_today", []) or [],
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

        # FLO-190: Inject debate results for dashboard
        try:
            _debate = getattr(bot_instance, "_last_debate_result", None)
            if isinstance(_debate, dict):
                from datetime import datetime as _dt_deb
                _deb_out = {
                    "status": _debate.get("status", "DISABLED"),
                    "skip_reason": _debate.get("skip_reason"),
                    "timestamp": _dt_deb.utcnow().isoformat(timespec="seconds") + "Z",
                }
                if _debate.get("status") == "INJECTED":
                    _deb_out["rex_bull"] = _debate.get("rex_bull", {})
                    _deb_out["rex_bear"] = _debate.get("rex_bear", {})
                last_analysis["debate"] = _deb_out
        except Exception:
            pass

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

        # FLO-194: Inject Research Manager verdict for dashboard
        try:
            _verdict = getattr(bot_instance, "_last_verdict_result", None)
            if isinstance(_verdict, dict):
                from datetime import datetime as _dt_verd
                _v_out = {
                    "status": _verdict.get("status", "DISABLED"),
                    "timestamp": _dt_verd.utcnow().isoformat(timespec="seconds") + "Z",
                }
                if _verdict.get("status") == "OK":
                    for _vk in ("winner", "reasoning", "recommendation", "entry", "sl",
                                "target", "trigger_buy", "trigger_sell", "conviction"):
                        _v_out[_vk] = _verdict.get(_vk)
                last_analysis["verdict"] = _v_out
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
