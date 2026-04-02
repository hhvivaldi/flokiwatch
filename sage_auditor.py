import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import config
from logger import log


POPULATION_B_MIN_TICKET = 8
POPULATION_B_MIN_OPEN_TIME = "2026-02-16"

# FLO-189: Only analyze Floki trades — exclude legacy agent_gemini/brain/NULL
FLOKI_SOURCE_FILTER = "decision_source IN ('floki_agent', 'agent_floki')"


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _safe_str(v: Any) -> str:
    try:
        return str(v or "")
    except Exception:
        return ""


def _parse_iso8601_to_utc(s: str) -> Optional[datetime]:
    try:
        t = (s or "").strip()
        if not t:
            return None
        t = t.replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _duration_minutes(open_time: str, close_time: str) -> Optional[float]:
    try:
        ot = _parse_iso8601_to_utc(open_time)
        ct = _parse_iso8601_to_utc(close_time)
        if ot is None or ct is None:
            return None
        return max(0.0, (ct - ot).total_seconds() / 60.0)
    except Exception:
        return None


def _weekday_hour(open_time: str) -> Tuple[Optional[int], Optional[int]]:
    try:
        ot = _parse_iso8601_to_utc(open_time)
        if ot is None:
            return None, None
        return int(ot.weekday()), int(ot.hour)
    except Exception:
        return None, None


def _price_delta_pips(direction: str, open_price: Any, close_price: Any) -> Optional[float]:
    try:
        op = float(open_price)
        cp = float(close_price)
        d = (cp - op) if str(direction or "").upper().strip() == "BUY" else (op - cp)
        return d * 10.0
    except Exception:
        return None


def _risk_reward_fields(open_price: Any, sl: Any, tp: Any) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        if open_price is None or sl is None or tp is None:
            return None, None, None
        op = float(open_price)
        sl_f = float(sl)
        tp_f = float(tp)
        if sl_f == 0.0 or tp_f == 0.0:
            return None, None, None
        risk_to_sl = abs(op - sl_f)
        reward_to_tp = abs(tp_f - op)
        if risk_to_sl <= 0:
            return None, None, None
        rr_planned = reward_to_tp / risk_to_sl
        return rr_planned, risk_to_sl, reward_to_tp
    except Exception:
        return None, None, None


def _confidence_level(sample_size: int) -> str:
    n = _safe_int(sample_size, 0)
    if n < 10:
        return "LOW_CONFIDENCE"
    if n < 20:
        return "MEDIUM_CONFIDENCE"
    return "HIGH_CONFIDENCE"


def _get_connection() -> sqlite3.Connection:
    db_path = os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
    conn = sqlite3.connect(db_path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _total_trades_in_db(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS c FROM trades").fetchone()
    return _safe_int(row["c"] if row else 0, 0)


def _total_population_b_closed_trades(conn: sqlite3.Connection) -> int:
    try:
        q = (
            "SELECT COUNT(*) AS c "
            "FROM trades "
            "WHERE close_time IS NOT NULL "
            "  AND profit IS NOT NULL "
            "  AND ticket >= ? "
            "  AND open_time >= ? "
            f"  AND {FLOKI_SOURCE_FILTER}"
        )
        row = conn.execute(q, (POPULATION_B_MIN_TICKET, POPULATION_B_MIN_OPEN_TIME)).fetchone()
        return _safe_int(row["c"] if row else 0, 0)
    except Exception:
        return 0


def _query_population_b_closed_trades(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    q = (
        "SELECT ticket, direction, volume, open_price, close_price, sl, tp, profit, close_reason, open_time, close_time, comment, breakeven_activated, decision_source "
        "FROM trades "
        "WHERE close_time IS NOT NULL "
        "  AND profit IS NOT NULL "
        "  AND ticket >= ? "
        "  AND open_time >= ? "
        f"  AND {FLOKI_SOURCE_FILTER} "
        "ORDER BY open_time ASC"
    )
    rows = conn.execute(q, (POPULATION_B_MIN_TICKET, POPULATION_B_MIN_OPEN_TIME)).fetchall()
    return list(rows or [])


def _min_max_open_dates(trades: List[sqlite3.Row]) -> Tuple[str, str]:
    if not trades:
        today = datetime.utcnow().date().isoformat()
        return today, today

    opens: List[str] = []
    for r in trades:
        try:
            ot = str(r["open_time"] or "")
            if ot:
                opens.append(ot)
        except Exception:
            continue

    if not opens:
        today = datetime.utcnow().date().isoformat()
        return today, today

    # ISO 8601 strings sort lexicographically
    first = min(opens)
    last = max(opens)

    def _to_date(s: str) -> str:
        try:
            return s.split("T")[0]
        except Exception:
            return datetime.utcnow().date().isoformat()

    return _to_date(first), _to_date(last)


def _basic_insights(trades: List[sqlite3.Row]) -> List[Dict[str, Any]]:
    insights: List[Dict[str, Any]] = []
    if not trades:
        return insights

    # session_name is not stored on trades; infer from open_time hour
    def _utc_hour(open_time: str) -> Optional[int]:
        try:
            s = (open_time or "").replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.astimezone(timezone.utc).hour)
        except Exception:
            return None

    def _session_from_hour(h: Optional[int]) -> str:
        if h is None:
            return "unknown"
        if 0 <= h < 7:
            return "Asia"
        if 7 <= h < 13:
            return "London"
        if 13 <= h < 22:
            return "NewYork"
        return "OffHours"

    # Aggregate
    by_dir: Dict[str, List[float]] = {}
    by_session: Dict[str, List[float]] = {}
    by_close_reason: Dict[str, List[float]] = {}

    for r in trades:
        direction = str(r["direction"] or "").upper().strip() or "UNKNOWN"
        pnl = _safe_float(r["profit"], 0.0)
        ot = str(r["open_time"] or "")
        sess = _session_from_hour(_utc_hour(ot))
        reason = str(r["close_reason"] or "").strip() or "UNKNOWN"

        by_dir.setdefault(direction, []).append(pnl)
        by_session.setdefault(sess, []).append(pnl)
        by_close_reason.setdefault(reason, []).append(pnl)

    def _wr(vals: List[float]) -> float:
        if not vals:
            return 0.0
        wins = sum(1 for v in vals if v > 0)
        losses = sum(1 for v in vals if v < 0)
        decisive = wins + losses
        return (wins / decisive * 100.0) if decisive else 0.0

    # Direction insight
    for k, vals in sorted(by_dir.items(), key=lambda kv: len(kv[1]), reverse=True):
        n = len(vals)
        insights.append(
            {
                "category": "direction",
                "finding": f"{k}: WR {_wr(vals):.1f}% (n={n})",
                "sample_size": n,
                "confidence_level": _confidence_level(n),
            }
        )

    # Session insight
    for k, vals in sorted(by_session.items(), key=lambda kv: len(kv[1]), reverse=True):
        n = len(vals)
        insights.append(
            {
                "category": "session",
                "finding": f"{k}: WR {_wr(vals):.1f}% (n={n})",
                "sample_size": n,
                "confidence_level": _confidence_level(n),
            }
        )

    # Close reason insight
    for k, vals in sorted(by_close_reason.items(), key=lambda kv: len(kv[1]), reverse=True)[:5]:
        n = len(vals)
        insights.append(
            {
                "category": "close_reason",
                "finding": f"{k}: WR {_wr(vals):.1f}% (n={n})",
                "sample_size": n,
                "confidence_level": _confidence_level(n),
            }
        )

    return insights[:15]


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> bool:
    try:
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except Exception as e:
        log.warning(f"sage_auditor: failed to write json: {e}")
        return False


# ---------------------------------------------------------------------------
# Intraday Drawdown Alerts (FLO-68)
# ---------------------------------------------------------------------------

def check_intraday_drawdown(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Check today's cumulative P&L and loss streak after a trade close.
    If drawdown exceeds threshold or loss streak is too long, fire alert.

    Returns alert dict if triggered, None otherwise. Never raises.
    """
    try:
        path = db_path or os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row

        today_str = datetime.utcnow().strftime("%Y-%m-%d")

        # All trades closed today with known P&L
        rows = conn.execute(
            f"""SELECT profit, close_time FROM trades
               WHERE close_time IS NOT NULL
                 AND profit IS NOT NULL
                 AND close_time >= ?
                 AND {FLOKI_SOURCE_FILTER}
               ORDER BY close_time ASC""",
            (today_str,),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        # Daily P&L
        profits = [float(r["profit"]) for r in rows]
        daily_pnl = round(sum(profits), 2)
        trades_today = len(profits)
        wins = sum(1 for p in profits if p > 0)
        losses = sum(1 for p in profits if p < 0)

        # Consecutive loss streak (count from latest going backwards)
        streak = 0
        for p in reversed(profits):
            if p < 0:
                streak += 1
            else:
                break

        # Check thresholds
        drawdown_threshold = float(getattr(config, "SAGE_INTRADAY_DRAWDOWN_ALERT", -30))
        streak_threshold = int(getattr(config, "SAGE_INTRADAY_LOSS_STREAK_ALERT", 3))

        triggered_drawdown = daily_pnl <= drawdown_threshold
        triggered_streak = streak >= streak_threshold

        if not triggered_drawdown and not triggered_streak:
            return None

        # Build alert
        reasons = []
        if triggered_drawdown:
            reasons.append(f"daily P&L ${daily_pnl:+.2f} (threshold: ${drawdown_threshold:.0f})")
        if triggered_streak:
            reasons.append(f"{streak} consecutive losses (threshold: {streak_threshold})")

        alert = {
            "daily_pnl": daily_pnl,
            "trades_today": trades_today,
            "wins": wins,
            "losses": losses,
            "streak": streak,
            "triggered_drawdown": triggered_drawdown,
            "triggered_streak": triggered_streak,
            "reasons": reasons,
            "timestamp": datetime.utcnow().isoformat(),
        }

        log.warning(
            f"SAGE | INTRADAY ALERT: daily P&L ${daily_pnl:+.2f}, "
            f"streak {streak}, {trades_today} trades ({wins}W/{losses}L) — "
            f"{'; '.join(reasons)}"
        )

        # 1. Write to session memory
        try:
            _write_sage_alert_to_memory(alert)
        except Exception:
            pass

        # 2. Record agent event for Trade Room
        try:
            from db_writer import record_agent_event
            record_agent_event(
                event_type="SAGE_ALERT",
                content=(
                    f"INTRADAY DRAWDOWN ALERT: Daily P&L ${daily_pnl:+.2f} "
                    f"({trades_today} trades, {wins}W/{losses}L, {streak} consecutive losses). "
                    f"Consider reducing risk or pausing."
                ),
                payload=alert,
                author="SAGE",
            )
        except Exception as e:
            log.warning(f"SAGE | failed to record alert event: {e}")

        # 3. Discord alert (card to sage + errors channels)
        try:
            from discord_cards import build_sage_alert_card, send_built_card, send_card, COLORS
            card = build_sage_alert_card(daily_pnl, streak, trades_today, wins=wins, losses=losses)
            send_built_card(card)
            # Also send to errors channel
            card_errors = dict(card)
            card_errors["channel"] = "errors"
            send_built_card(card_errors)
        except Exception:
            pass

        return alert

    except Exception as e:
        log.warning(f"SAGE | intraday drawdown check failed (ignored): {e}")
        return None


def _write_sage_alert_to_memory(alert: Dict[str, Any]) -> None:
    """Write Sage intraday alert to session memory so Floki sees it."""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        mem_path = os.path.join(base_dir, "data", "agent_session_memory.json")

        payload: Dict[str, Any] = {}
        if os.path.exists(mem_path):
            try:
                with open(mem_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    payload = existing
            except Exception:
                pass

        today = datetime.utcnow().date().isoformat()
        if str(payload.get("session_date") or "") != today:
            payload["session_date"] = today
            payload["notes"] = []

        if not isinstance(payload.get("notes"), list):
            payload["notes"] = []

        # Remove prior sage_alert notes (keep only latest)
        payload["notes"] = [
            n for n in payload["notes"]
            if not (isinstance(n, dict) and n.get("source") == "sage_alert")
        ]

        now = datetime.utcnow()
        pnl = alert["daily_pnl"]
        streak = alert["streak"]
        trades = alert["trades_today"]
        wins = alert["wins"]
        losses = alert["losses"]

        payload["notes"].append({
            "time": now.strftime("%H:%M"),
            "note": (
                f"SAGE ALERT: Daily drawdown ${pnl:+.2f} "
                f"({trades} trades, {wins}W/{losses}L, {streak} consecutive losses). "
                f"Consider reducing risk or pausing."
            ),
            "source": "sage_alert",
        })
        payload["last_updated"] = now.isoformat(timespec="seconds")

        tmp = mem_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, mem_path)

    except Exception as e:
        log.warning(f"SAGE | failed to write alert to session memory: {e}")


# ---------------------------------------------------------------------------
# Weekly Trending Reports (FLO-69)
# ---------------------------------------------------------------------------

WEEKLY_REPORT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "sage_weekly_report.json"
)


def _session_from_utc_hour(h: Optional[int]) -> str:
    if h is None:
        return "unknown"
    if 0 <= h < 7:
        return "asian"
    if 7 <= h < 13:
        return "london"
    if 13 <= h < 22:
        return "ny"
    return "off_hours"


def _utc_hour_from_iso(ts: str) -> Optional[int]:
    try:
        s = (ts or "").replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.astimezone(timezone.utc).hour)
    except Exception:
        return None


def _week_stats(rows: list) -> Dict[str, Any]:
    """Compute stats for a list of trade rows."""
    if not rows:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0,
            "pnl": 0, "avg_pnl": 0, "best": 0, "worst": 0,
            "max_consecutive_losses": 0, "by_session": {},
        }

    profits = [_safe_float(r["profit"], 0) for r in rows]
    wins = sum(1 for p in profits if p > 0)
    losses = sum(1 for p in profits if p < 0)
    decisive = wins + losses
    win_rate = round(wins / decisive, 2) if decisive else 0
    total_pnl = round(sum(profits), 2)
    avg_pnl = round(total_pnl / len(profits), 2) if profits else 0
    best = round(max(profits), 2) if profits else 0
    worst = round(min(profits), 2) if profits else 0

    # Max consecutive losses
    max_streak = 0
    streak = 0
    for p in profits:
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # By session
    by_session: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        h = _utc_hour_from_iso(str(r["open_time"] or ""))
        sess = _session_from_utc_hour(h)
        if sess not in by_session:
            by_session[sess] = {"trades": 0, "wins": 0, "pnl": 0}
        pnl = _safe_float(r["profit"], 0)
        by_session[sess]["trades"] += 1
        if pnl > 0:
            by_session[sess]["wins"] += 1
        by_session[sess]["pnl"] = round(by_session[sess]["pnl"] + pnl, 2)
    for sess, s in by_session.items():
        s["win_rate"] = round(s["wins"] / s["trades"], 2) if s["trades"] else 0

    return {
        "trades": len(profits),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl": total_pnl,
        "avg_pnl": avg_pnl,
        "best": best,
        "worst": worst,
        "max_consecutive_losses": max_streak,
        "by_session": by_session,
    }


def generate_weekly_report(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Generate week-over-week comparison report.
    This week (last 7 days) vs last week (7-14 days ago).
    Returns report dict or None on failure. Never raises.
    """
    try:
        path = db_path or os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row

        now = datetime.utcnow()
        # FLO-189: Fixed calendar weeks (Monday 00:00 UTC boundaries)
        days_since_monday = now.weekday()  # 0=Mon, 6=Sun
        this_week_start = (now - __import__("datetime").timedelta(days=days_since_monday)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        last_week_start = (now - __import__("datetime").timedelta(days=days_since_monday + 7)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

        # FLO-189: Floki-only filter (replaces legacy agent_gemini inclusion)
        this_week_rows = conn.execute(
            f"""SELECT ticket, direction, profit, open_time, close_time, close_reason
               FROM trades
               WHERE close_time IS NOT NULL AND profit IS NOT NULL
                 AND close_time >= ? AND {FLOKI_SOURCE_FILTER}
               ORDER BY close_time ASC""",
            (this_week_start,),
        ).fetchall()

        last_week_rows = conn.execute(
            f"""SELECT ticket, direction, profit, open_time, close_time, close_reason
               FROM trades
               WHERE close_time IS NOT NULL AND profit IS NOT NULL
                 AND close_time >= ? AND close_time < ? AND {FLOKI_SOURCE_FILTER}
               ORDER BY close_time ASC""",
            (last_week_start, this_week_start),
        ).fetchall()

        conn.close()

        tw = _week_stats(this_week_rows)
        lw = _week_stats(last_week_rows)

        # Comparison
        wr_change = round(tw["win_rate"] - lw["win_rate"], 2) if lw["trades"] > 0 else None
        pnl_change = round(tw["pnl"] - lw["pnl"], 2) if lw["trades"] > 0 else None
        avg_change = round(tw["avg_pnl"] - lw["avg_pnl"], 2) if lw["trades"] > 0 else None

        if wr_change is not None and pnl_change is not None:
            if wr_change > 0.03 and pnl_change > 0:
                trend = "IMPROVING"
            elif wr_change < -0.03 and pnl_change < 0:
                trend = "DECLINING"
            else:
                trend = "STABLE"
        else:
            trend = "INSUFFICIENT_DATA"

        report = {
            "generated": now.isoformat(),
            "report_date": now.date().isoformat(),
            "this_week_start": this_week_start[:10],
            "last_week_start": last_week_start[:10],
            "this_week": tw,
            "last_week": lw,
            "comparison": {
                "win_rate_change": wr_change,
                "pnl_change": pnl_change,
                "avg_pnl_change": avg_change,
                "trend": trend,
            },
        }

        # Save
        _write_json_atomic(WEEKLY_REPORT_FILE, report)

        # Session memory
        try:
            from agent_memory import write_sage_insights

            best_sess = max(tw["by_session"].items(), key=lambda x: x[1].get("win_rate", 0))[0] if tw["by_session"] else "N/A"
            worst_sess = min(tw["by_session"].items(), key=lambda x: x[1].get("win_rate", 1))[0] if tw["by_session"] else "N/A"
            wr_pct = int(tw["win_rate"] * 100)
            wr_chg_str = f" ({wr_change:+.0%} vs last week)" if wr_change is not None else ""
            pnl_chg_str = f" ({pnl_change:+.2f} vs last week)" if pnl_change is not None else ""

            summary = (
                f"SAGE WEEKLY: {tw['trades']} trades, {wr_pct}% WR{wr_chg_str}, "
                f"P&L ${tw['pnl']:+.2f}{pnl_chg_str}. "
                f"Best session: {best_sess}. Worst: {worst_sess}. Trend: {trend}."
            )
            write_sage_insights(
                recommendations=[summary],
                trade_count=tw["trades"],
                report_date=now.date().isoformat(),
            )
        except Exception:
            pass

        # Agent event
        try:
            from db_writer import record_agent_event
            record_agent_event(
                event_type="SAGE_WEEKLY",
                content=(
                    f"Weekly report: {tw['trades']} trades, {int(tw['win_rate']*100)}% WR, "
                    f"P&L ${tw['pnl']:+.2f}. Trend: {trend}."
                ),
                payload=report,
                author="SAGE",
            )
        except Exception:
            pass

        # FLO-78: Discord card for weekly report
        try:
            from discord_cards import build_sage_weekly_card, send_built_card
            send_built_card(build_sage_weekly_card(tw, lw, report.get("comparison", {})))
        except Exception:
            pass

        log.info(
            f"SAGE | weekly report: {tw['trades']} trades, "
            f"{int(tw['win_rate']*100)}% WR, P&L ${tw['pnl']:+.2f}, trend={trend}"
        )
        return report

    except Exception as e:
        log.warning(f"SAGE | weekly report failed (ignored): {e}")
        return None


# ---------------------------------------------------------------------------
# Sage → Luna Feedback (FLO-70)
# ---------------------------------------------------------------------------

LUNA_INSIGHTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "sage_insights_for_luna.json"
)


def generate_luna_insights(db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Extract performance insights for Luna from last 14 days of trades.
    Session + day-of-week + direction performance. No AI calls.
    Returns insights dict or None on failure. Never raises.
    """
    try:
        from datetime import timedelta

        path = db_path or os.path.abspath(getattr(config, "HISTORY_DB_PATH", "data/history.db"))
        conn = sqlite3.connect(path, timeout=5)
        conn.row_factory = sqlite3.Row

        cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
        rows = conn.execute(
            f"""SELECT ticket, direction, profit, open_time, close_time
               FROM trades
               WHERE close_time IS NOT NULL AND profit IS NOT NULL
                 AND close_time >= ?
                 AND {FLOKI_SOURCE_FILTER}
               ORDER BY close_time ASC""",
            (cutoff,),
        ).fetchall()
        conn.close()

        if not rows:
            return None

        # Session performance
        session_perf: Dict[str, Dict[str, Any]] = {}
        # Direction performance
        dir_perf: Dict[str, Dict[str, Any]] = {}
        # Day-of-week performance
        dow_perf: Dict[str, Dict[str, Any]] = {}

        for r in rows:
            pnl = _safe_float(r["profit"], 0)
            is_win = pnl > 0
            direction = str(r["direction"] or "").upper().strip() or "UNKNOWN"

            # Session
            h = _utc_hour_from_iso(str(r["open_time"] or ""))
            sess = _session_from_utc_hour(h)
            if sess not in session_perf:
                session_perf[sess] = {"wins": 0, "total": 0, "pnl": 0}
            session_perf[sess]["total"] += 1
            if is_win:
                session_perf[sess]["wins"] += 1
            session_perf[sess]["pnl"] = round(session_perf[sess]["pnl"] + pnl, 2)

            # Direction
            if direction not in dir_perf:
                dir_perf[direction] = {"wins": 0, "total": 0, "pnl": 0}
            dir_perf[direction]["total"] += 1
            if is_win:
                dir_perf[direction]["wins"] += 1
            dir_perf[direction]["pnl"] = round(dir_perf[direction]["pnl"] + pnl, 2)

            # Day of week + AM/PM
            try:
                ot = str(r["open_time"] or "").replace("Z", "+00:00")
                dt = datetime.fromisoformat(ot)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                day_name = dt.strftime("%A").lower()
                period = "am" if dt.hour < 14 else "pm"
                dow_key = f"{day_name}_{period}"
                if dow_key not in dow_perf:
                    dow_perf[dow_key] = {"wins": 0, "total": 0}
                dow_perf[dow_key]["total"] += 1
                if is_win:
                    dow_perf[dow_key]["wins"] += 1
            except Exception:
                pass

        # Build output
        session_out = {}
        for sess, s in session_perf.items():
            wr = round(s["wins"] / s["total"], 2) if s["total"] else 0
            session_out[sess] = {
                "win_rate": wr,
                "sample": s["total"],
                "avg_pnl": round(s["pnl"] / s["total"], 2) if s["total"] else 0,
            }

        dir_out = {}
        for d, s in dir_perf.items():
            wr = round(s["wins"] / s["total"], 2) if s["total"] else 0
            dir_out[d] = {"win_rate": wr, "sample": s["total"]}

        dow_out = {}
        for key, s in dow_perf.items():
            if s["total"] >= 3:  # Only include with meaningful sample
                wr = round(s["wins"] / s["total"], 2) if s["total"] else 0
                dow_out[key] = {"win_rate": wr, "sample": s["total"]}

        # Danger patterns (< 35% WR with 3+ trades)
        danger = []
        for sess, s in session_out.items():
            if s["win_rate"] < 0.35 and s["sample"] >= 3:
                danger.append(f"{sess.capitalize()} session: {int(s['win_rate']*100)}% WR ({s['sample']} trades)")
        for key, s in dow_out.items():
            if s["win_rate"] < 0.35 and s["sample"] >= 3:
                danger.append(f"{key.replace('_', ' ').title()}: {int(s['win_rate']*100)}% WR ({s['sample']} trades)")

        # Best conditions (> 65% WR with 3+ trades)
        best = []
        for sess, s in session_out.items():
            if s["win_rate"] > 0.65 and s["sample"] >= 3:
                best.append(f"{sess.capitalize()} session: {int(s['win_rate']*100)}% WR ({s['sample']} trades)")
        for d, s in dir_out.items():
            if s["win_rate"] > 0.65 and s["sample"] >= 5:
                best.append(f"{d} direction: {int(s['win_rate']*100)}% WR ({s['sample']} trades)")

        insights = {
            "updated": datetime.utcnow().isoformat(),
            "session_performance": session_out,
            "direction_performance": dir_out,
            "day_of_week": dow_out,
            "danger_patterns": danger,
            "best_conditions": best,
        }

        _write_json_atomic(LUNA_INSIGHTS_FILE, insights)
        log.info(f"SAGE | Luna insights written | {len(danger)} dangers, {len(best)} best conditions")
        return insights

    except Exception as e:
        log.warning(f"SAGE | Luna insights generation failed (ignored): {e}")
        return None


@dataclass
class SageRunResult:
    ok: bool
    error: Optional[str] = None
    report_path: Optional[str] = None
    trade_count_analyzed: int = 0


async def _call_gemini_for_patterns(
    trades: List[sqlite3.Row],
    existing_insights: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, Any]]:
    """LLM step (Gemini): propose additional insights + recommendations.

    Hard rules (enforced again by code after parse):
    - Must include sample_size for each insight
    - confidence_level must match sample_size thresholds
    - categories must be in allowed set
    """

    start = time.time()
    meta: Dict[str, Any] = {"model": getattr(config, "SAGE_MODEL", "gemini-3-flash-preview"), "tokens": {"input": 0, "output": 0, "total": 0}, "latency_ms": 0}

    sage_api_key = str(getattr(config, "SAGE_API_KEY", "") or os.environ.get("SAGE_API_KEY", "") or "").strip()
    shared_api_key = str(os.environ.get("GEMINI_API_KEY", "") or "").strip()
    api_key = sage_api_key or shared_api_key
    if not api_key:
        return [], [], meta
    if not sage_api_key and shared_api_key:
        log.warning("SAGE | WARNING | Using shared GEMINI_API_KEY — set SAGE_API_KEY for independent cost tracking")

    try:
        from google import genai
    except Exception:
        return [], [], meta

    # Build compact trade rows
    rows = []
    for r in trades[-120:]:
        rr_planned, risk_to_sl, reward_to_tp = _risk_reward_fields(r["open_price"], r["sl"], r["tp"])
        weekday_i, hour_i = _weekday_hour(_safe_str(r["open_time"]))
        rows.append(
            {
                "ticket": _safe_int(r["ticket"], 0),
                "direction": _safe_str(r["direction"]),
                "volume": _safe_float(r["volume"], 0.0),
                "open_price": _safe_float(r["open_price"], 0.0),
                "close_price": _safe_float(r["close_price"], 0.0),
                "sl": _safe_float(r["sl"], 0.0),
                "tp": _safe_float(r["tp"], 0.0),
                "profit": _safe_float(r["profit"], 0.0),
                "close_reason": _safe_str(r["close_reason"]),
                "open_time": _safe_str(r["open_time"]),
                "close_time": _safe_str(r["close_time"]),
                "comment": _safe_str(r["comment"]),
                "breakeven_activated": _safe_int(r["breakeven_activated"], 0),
                "duration_minutes": _duration_minutes(_safe_str(r["open_time"]), _safe_str(r["close_time"])),
                "pips": _price_delta_pips(_safe_str(r["direction"]), r["open_price"], r["close_price"]),
                "rr_planned": rr_planned,
                "risk_to_sl": risk_to_sl,
                "reward_to_tp": reward_to_tp,
                "weekday": weekday_i,
                "hour": hour_i,
            }
        )

    allowed_categories = [
        "session",
        "direction",
        "hour",
        "weekday",
        "close_reason",
        "pattern",
        "behavioral",
    ]

    system = (
        "You are Sage, a senior trading performance analyst with 20 years of experience auditing institutional gold trading desks at Goldman Sachs and JP Morgan. You have reviewed thousands of trading journals and identified patterns that saved millions. You are NOT a trader — you are the analyst who sits behind the trader, reviews every trade at end of day, and delivers a brutally honest daily briefing. "
        "Your personality: blunt, precise, data-driven. You never sugarcoat. You never speculate beyond what the data shows. You respect small sample sizes — you flag them clearly and never make strong recommendations based on fewer than 10 trades. You write like a risk report: concise, numbered, actionable. "
        "YOUR AUDIENCE: Your briefing will be read by Floki, an AI trading agent (Gemini 3 Flash) that makes autonomous XAU/USD trading decisions. Floki reads your note at the start of each trading session. Your job is to make Floki a better trader tomorrow than it was today. "
        "ANALYSIS FRAMEWORK: "
        "1. Edge strength: overall win rate and profit factor trends. Compare recent 10 trades vs full sample — is the edge growing, stable, or decaying? "
        "2. Directional bias: BUY vs SELL performance. Which side has the edge right now? "
        "3. Session performance: London, New York, Asia — where does Floki perform best and worst? "
        "4. Exit quality: how are trades closing? EA cuts, trailing stops, take profits? Are exits too early (micro-wins) or too late (full SL hits)? "
        "5. Behavioral patterns: overtrading clusters, revenge trading after losses, position sizing anomalies "
        "6. Day-of-week effects: any days consistently underperforming? "
        "CONFIDENCE RULES (non-negotiable): "
        "- n < 10: LOW_CONFIDENCE — mention as observation only. NEVER recommend action based on this. "
        "- 10 <= n < 20: MEDIUM_CONFIDENCE — can suggest monitoring but not hard rules. "
        "- n >= 20: HIGH_CONFIDENCE — can recommend actionable changes. "
        "WHAT YOU MUST NOT DO: "
        "- Never recommend disabling an entire direction (BUY or SELL) based on fewer than 20 trades "
        "- Never recommend trading only on specific days based on fewer than 20 trades per day "
        "- Never recommend changes that would reduce trading frequency by more than 50% "
        "- Never invent data or extrapolate beyond the sample "
        "- Never give vague recommendations like 'consider implementing risk reduction' — be specific or say nothing "
        "OUTPUT FORMAT: "
        "Return valid JSON only with two keys: "
        "1. 'insights': array of objects with fields: category, finding, sample_size (integer), confidence_level "
        "2. 'recommendations': array of strings — maximum 5 recommendations, ordered by confidence level (highest first). Each must be a direct operational instruction that Floki can follow. Format: '[CONFIDENCE] Action. Reason (n=X).'. "
        "Example recommendation format: "
        "- '[HIGH] Prioritize SELL entries over BUY — SELL WR 66.7% vs BUY 37.5% over 20 trades.' "
        "- '[MEDIUM] Monitor London session performance — WR 42.9% (n=7), below average. Flag if it drops below 40% over 15+ trades.' "
        "- '[LOW] One instance of lot size doubling after loss detected (ticket #X). Watch for recurrence but no action needed yet.'"
    )

    wins = sum(1 for r in rows if isinstance(r, dict) and _safe_float(r.get("profit"), 0.0) > 0)
    losses = sum(1 for r in rows if isinstance(r, dict) and _safe_float(r.get("profit"), 0.0) < 0)
    breakevens = max(0, len(rows) - wins - losses)
    close_reason_counts: Dict[str, int] = {}
    for rr in rows:
        try:
            cr = str((rr or {}).get("close_reason") or "").strip() or "UNKNOWN"
            close_reason_counts[cr] = close_reason_counts.get(cr, 0) + 1
        except Exception:
            pass

    user = {
        "population_filter": {
            "ticket_min": POPULATION_B_MIN_TICKET,
            "open_time_min": POPULATION_B_MIN_OPEN_TIME,
            "closed_only": True,
        },
        "allowed_categories": allowed_categories,
        "existing_insights": existing_insights,
        "summary": {
            "trade_count": int(len(rows)),
            "wins": int(wins),
            "losses": int(losses),
            "breakevens": int(breakevens),
            "close_reasons": close_reason_counts,
            "recent_slice_hint": {"last_n": 20},
        },
        "trades_sample": rows,
    }

    client = genai.Client(api_key=api_key)
    model = getattr(config, "SAGE_MODEL", "gemini-3-flash-preview")

    try:
        resp = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=[{"role": "user", "parts": [{"text": system}]}, {"role": "user", "parts": [{"text": json.dumps(user, ensure_ascii=False)}]}],
        )
    except Exception:
        return [], [], meta

    latency_ms = int((time.time() - start) * 1000)
    meta["model"] = model
    meta["latency_ms"] = latency_ms

    # best-effort token usage
    try:
        usage = getattr(resp, "usage_metadata", None)
        if usage is not None:
            meta["tokens"]["total"] = _safe_int(getattr(usage, "total_token_count", 0), 0)
            meta["tokens"]["input"] = _safe_int(getattr(usage, "prompt_token_count", 0), 0)
            meta["tokens"]["output"] = max(0, meta["tokens"]["total"] - meta["tokens"]["input"])
    except Exception:
        pass

    text = ""
    try:
        text = getattr(resp, "text", "") or ""
    except Exception:
        text = ""

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    try:
        parsed = json.loads(text)
    except Exception:
        return [], [], meta

    if not isinstance(parsed, dict):
        return [], [], meta

    out_insights = parsed.get("insights")
    out_recs = parsed.get("recommendations")

    if not isinstance(out_insights, list):
        out_insights = []
    if not isinstance(out_recs, list):
        out_recs = []

    # sanitize and enforce hard rules
    clean_insights: List[Dict[str, Any]] = []
    for i in out_insights[:25]:
        if not isinstance(i, dict):
            continue
        cat = str(i.get("category") or "").strip()
        finding = str(i.get("finding") or "").strip()
        n = _safe_int(i.get("sample_size"), 0)
        if not finding or cat not in allowed_categories:
            continue
        clean_insights.append(
            {
                "category": cat,
                "finding": finding,
                "sample_size": n,
                "confidence_level": _confidence_level(n),
            }
        )

    clean_recs = [str(r).strip() for r in out_recs if str(r).strip()][:5]

    return clean_insights, clean_recs, meta


def _run_async_safely(coro):
    """Run a coroutine safely whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
        # Already in async context - run in a separate thread with its own loop
        import concurrent.futures
        result = None
        exc = None

        def _thread_target():
            nonlocal result, exc
            try:
                result = asyncio.run(coro)
            except Exception as e:
                exc = e

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_thread_target)
            fut.result()
        if exc is not None:
            raise exc
        return result
    except RuntimeError:
        # No running loop - safe to use asyncio.run
        return asyncio.run(coro)


def run_sage_auditor() -> SageRunResult:
    """Entry point for daily Sage run.

    Must never raise exceptions to caller.
    """
    try:
        conn = _get_connection()
        try:
            total_trades = _total_trades_in_db(conn)
            total_population = _total_population_b_closed_trades(conn)
            trades = _query_population_b_closed_trades(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        log.info(f"SAGE | Population: Agent-only | trades={len(trades)} (filtered from {total_population} total)")

        period_start, period_end = _min_max_open_dates(trades)

        base_insights = _basic_insights(trades)

        # LLM enrichment (optional)
        llm_insights: List[Dict[str, Any]] = []
        llm_recs: List[str] = []
        meta: Dict[str, Any] = {
            "model": getattr(config, "SAGE_MODEL", "gemini-3-flash-preview"),
            "tokens": {"input": 0, "output": 0, "total": 0},
            "latency_ms": 0,
        }
        try:
            llm_insights, llm_recs, meta = _run_async_safely(_call_gemini_for_patterns(trades, base_insights))
        except Exception as e:
            log.warning(f"SAGE | LLM enrichment failed: {e}")
            llm_insights, llm_recs = [], []

        # Merge insights (base first, then LLM; cap)
        insights = (base_insights + llm_insights)[:25]

        report = {
            "report_date": datetime.utcnow().date().isoformat(),
            "total_trades_in_db": int(total_trades),
            "trade_count_analyzed": int(len(trades)),
            "period_start": period_start,
            "period_end": period_end,
            "population_filter": {
                "name": "Population B",
                "rules": [
                    "ticket >= 8",
                    "open_time >= 2026-02-16",
                    "close_time IS NOT NULL",
                    "profit IS NOT NULL",
                ],
            },
            "insights": insights,
            "recommendations": llm_recs,
            "metadata": meta,
        }

        out_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "sage_report.json"))
        if not _write_json_atomic(out_path, report):
            return SageRunResult(ok=False, error="write_failed")

        # Persist key insights to session memory
        try:
            from agent_memory import write_sage_insights

            write_sage_insights(
                recommendations=llm_recs,
                trade_count=int(len(trades)),
                report_date=str(report.get("report_date") or ""),
            )
        except Exception:
            pass

        log.info(f"SAGE | report_written | trades={len(trades)} | path={out_path}")

        # FLO-150: Post summary to Trade Room feed
        try:
            from db_writer import record_agent_event
            _wr = report.get("win_rate")
            _pf = report.get("profit_factor")
            _recs = report.get("recommendations", [])
            _raw_rec = _recs[0] if _recs else None
            # FLO-207: Extract instruction text if recommendation is a dict
            if isinstance(_raw_rec, dict):
                _top_rec = _raw_rec.get("instruction", str(_raw_rec))
            elif _raw_rec:
                _top_rec = str(_raw_rec)
            else:
                _top_rec = "No recommendations"
            _summary = (
                f"Daily audit complete: {len(trades)} trades analyzed"
                f"{f', WR {_wr:.1f}%' if _wr is not None else ''}"
                f"{f', PF {_pf:.2f}' if _pf is not None else ''}. "
                f"Top recommendation: {_top_rec}"
            )
            record_agent_event("DAILY_REPORT", _summary[:2000], payload={"trades": len(trades), "win_rate": _wr, "profit_factor": _pf}, author="SAGE")
        except Exception:
            pass

        # FLO-70: Generate Luna insights (every daily run)
        try:
            generate_luna_insights()
        except Exception:
            pass

        # FLO-69/FLO-152: Weekly report on every daily run (was Fridays only)
        try:
            if True:  # was: datetime.utcnow().weekday() == 4
                generate_weekly_report()
        except Exception:
            pass

        return SageRunResult(ok=True, report_path=out_path, trade_count_analyzed=len(trades))
    except Exception as e:
        log.warning(f"SAGE | run_failed (ignored): {e}")
        return SageRunResult(ok=False, error=str(e))
