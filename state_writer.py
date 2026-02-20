import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

import config
from logger import log
from safety_checks import is_market_open
from executor import executor
from db_writer import record_account_snapshot


_last_valid_account_info: Optional[Dict[str, Any]] = None


def _safe_iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return None


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp_path, path)


def write_state(bot_instance: Any) -> None:
    """Write the bot's operational state for dashboard consumption.

    Must never throw exceptions outward (cannot block the bot).
    """
    try:
        now = datetime.now()

        market_open, market_reason, next_open = is_market_open()

        global _last_valid_account_info
        account_info = None
        try:
            if getattr(bot_instance, "executes_trades", False):
                account_info = executor.get_account_info()
        except Exception:
            account_info = None

        if account_info and account_info.get("balance") is not None:
            _last_valid_account_info = account_info
        elif _last_valid_account_info is not None:
            account_info = _last_valid_account_info
            log.debug("state_writer: MT5 account_info unavailable, using cache")

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

        # last_known_price: persistent in bot_instance, never cleared.
        last_known_price = getattr(bot_instance, "last_known_price", None)
        current_price = last_analysis.get("current_price")
        if current_price is not None:
            last_known_price = float(current_price)
            bot_instance.last_known_price = last_known_price

        positions = []
        try:
            if getattr(bot_instance, "executes_trades", False):
                positions = [
                    {
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
                    }
                    for p in executor.get_open_positions()
                ]
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

        state = {
            "timestamp": now.isoformat(),
            "_expected_update_interval_seconds": expected_interval,
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
            "last_analysis": last_analysis,
            "positions": positions,
            "trade_history": getattr(bot_instance, "closed_trades_today", []) or [],
        }

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
