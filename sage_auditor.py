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


def _query_population_b_closed_trades(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    q = (
        "SELECT ticket, direction, profit, close_reason, open_time, close_time "
        "FROM trades "
        "WHERE close_time IS NOT NULL "
        "  AND profit IS NOT NULL "
        "  AND ticket >= ? "
        "  AND open_time >= ? "
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
    meta: Dict[str, Any] = {"model": getattr(config, "FLOKI_MODEL", ""), "tokens": {"input": 0, "output": 0, "total": 0}, "latency_ms": 0}

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return [], [], meta

    try:
        from google import genai
    except Exception:
        return [], [], meta

    # Build compact trade rows
    rows = []
    for r in trades[-120:]:
        rows.append(
            {
                "ticket": _safe_int(r["ticket"], 0),
                "direction": str(r["direction"] or ""),
                "profit": _safe_float(r["profit"], 0.0),
                "close_reason": str(r["close_reason"] or ""),
                "open_time": str(r["open_time"] or ""),
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
        "You are Sage, a read-only performance auditor for an XAUUSD trading bot. "
        "You must analyze only the provided closed trade data. "
        "For EVERY insight you report, you MUST report sample size as an integer field sample_size (n). "
        "Confidence must follow HARD RULES: n<10 => LOW_CONFIDENCE, 10<=n<20 => MEDIUM_CONFIDENCE, n>=20 => HIGH_CONFIDENCE. "
        "Any LOW_CONFIDENCE insight is informational only and must not be framed as a decision rule. "
        "Return JSON ONLY with keys: insights (array) and recommendations (array)."
    )

    user = {
        "population_filter": {
            "ticket_min": POPULATION_B_MIN_TICKET,
            "open_time_min": POPULATION_B_MIN_OPEN_TIME,
            "closed_only": True,
        },
        "allowed_categories": allowed_categories,
        "existing_insights": existing_insights,
        "trades_sample": rows,
    }

    client = genai.Client(api_key=api_key)
    model = getattr(config, "FLOKI_MODEL", "gemini-3-flash-preview")

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

    clean_recs = [str(r).strip() for r in out_recs if str(r).strip()][:10]

    return clean_insights, clean_recs, meta


def run_sage_auditor() -> SageRunResult:
    """Entry point for daily Sage run.

    Must never raise exceptions to caller.
    """
    try:
        conn = _get_connection()
        try:
            total_trades = _total_trades_in_db(conn)
            trades = _query_population_b_closed_trades(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

        period_start, period_end = _min_max_open_dates(trades)

        base_insights = _basic_insights(trades)

        # LLM enrichment (optional)
        llm_insights: List[Dict[str, Any]] = []
        llm_recs: List[str] = []
        meta: Dict[str, Any] = {
            "model": getattr(config, "FLOKI_MODEL", "gemini-3-flash-preview"),
            "tokens": {"input": 0, "output": 0, "total": 0},
            "latency_ms": 0,
        }
        try:
            llm_insights, llm_recs, meta = asyncio.run(_call_gemini_for_patterns(trades, base_insights))
        except Exception:
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

            write_sage_insights(insights)
        except Exception:
            pass

        log.info(f"SAGE | report_written | trades={len(trades)} | path={out_path}")
        return SageRunResult(ok=True, report_path=out_path, trade_count_analyzed=len(trades))
    except Exception as e:
        log.warning(f"SAGE | run_failed (ignored): {e}")
        return SageRunResult(ok=False, error=str(e))
