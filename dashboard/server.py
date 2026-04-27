"""
DASHBOARD API SERVER

FLO-286 / CLAUDE.md Rule 22: All timestamps in API responses MUST be UTC ISO
with explicit "Z" suffix. Use _safe_iso_timestamp() (which delegates to
tz_utils.to_utc_iso) for any timestamp emitted to the frontend. The frontend
(dashboard/static/tz.js) converts UTC → user-local for display via
window.displayTime() and friends. Never serve broker time, never serve naive ISO.
"""

import json
import os
import sqlite3
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from datetime import timedelta

import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Ensure project root is importable when running as `python dashboard/server.py`
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import agent_reflection
from config import INITIAL_BALANCE

try:
    from dotenv import load_dotenv
    _dotenv_available = True
except ImportError:
    _dotenv_available = False

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

if _dotenv_available:
    try:
        load_dotenv(dotenv_path=str((APP_DIR / ".." / ".env").resolve()), override=False)
    except Exception:
        pass

STATE_FILE = Path(os.environ.get("DASHBOARD_STATE_FILE", str(APP_DIR / ".." / "data" / "bot_state.json"))).resolve()
HISTORY_DB = Path(os.environ.get("HISTORY_DB_PATH", str(APP_DIR / ".." / "data" / "history.db"))).resolve()
WATCH_CONDITIONS_FILE = Path(
    os.environ.get(
        "AGENT_WATCH_CONDITIONS_FILE",
        str(APP_DIR / ".." / "data" / "agent_wake_conditions.json"),
    )
).resolve()
SAGE_REPORT_FILE = Path(os.environ.get("SAGE_REPORT_FILE", str(APP_DIR / ".." / "data" / "sage_report.json"))).resolve()
SAGE_LAST_RUN_FILE = Path(os.environ.get("SAGE_LAST_RUN_FILE", str(APP_DIR / ".." / "data" / "sage_last_run.json"))).resolve()
OFFLINE_AFTER_SECONDS = int(os.environ.get("DASHBOARD_OFFLINE_AFTER_SECONDS", "60"))


app = FastAPI(title="XAUUSD Bot Dashboard", version="0.1")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/image", StaticFiles(directory=str(APP_DIR / "image")), name="image")


# ── WebSocket connection manager ──────────────────────
class _WSManager:
    def __init__(self):
        self.active: list = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

_ws_manager = _WSManager()


def _file_age_seconds(path: Path) -> float:
    st = path.stat()
    return max(0.0, datetime.now().timestamp() - st.st_mtime)


def _get_history_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(HISTORY_DB), timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_trade_reports_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS trade_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket INTEGER UNIQUE,
            created_at TEXT NOT NULL,
            model TEXT,
            input_hash TEXT,
            report_json TEXT
        )"""
    )
    conn.commit()


def _sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _redact_secrets(text: str) -> str:
    if not text:
        return text
    t = str(text)
    # Redact any OpenAI-style secret that starts with "sk-"
    if "sk-" in t:
        parts = t.split("sk-")
        out = parts[0]
        for p in parts[1:]:
            # Replace token body up to next whitespace / quote / punctuation with ***
            cut = len(p)
            for i, ch in enumerate(p):
                if ch.isspace() or ch in ('"', "'", ")", "]", "}", ",", ":"):
                    cut = i
                    break
            out += "sk-***" + p[cut:]
        return out
    return t


def _normalize_openai_error(err: str) -> str:
    msg = (err or "").lower()
    if "invalid_api_key" in msg or "incorrect api key" in msg or "api key provided" in msg:
        return "invalid_api_key"
    if "rate limit" in msg or "429" in msg:
        return "rate_limited"
    if "timeout" in msg:
        return "timeout"
    if "quota" in msg or "insufficient_quota" in msg:
        return "insufficient_quota"
    return "gpt_failed"


def _calc_pips(direction: str, open_price: float, close_price: float) -> float:
    if not open_price or not close_price:
        return 0.0
    if direction == "BUY":
        return (close_price - open_price) * 10
    if direction == "SELL":
        return (open_price - close_price) * 10
    return 0.0


def _calc_rr(direction: str, open_price: float, sl: float, tp: float) -> Dict[str, Any]:
    try:
        if not open_price or not sl or not tp:
            return {"risk": None, "reward": None, "rr": None}
        if direction == "BUY":
            risk = max(0.0, open_price - sl)
            reward = max(0.0, tp - open_price)
        elif direction == "SELL":
            risk = max(0.0, sl - open_price)
            reward = max(0.0, open_price - tp)
        else:
            return {"risk": None, "reward": None, "rr": None}
        rr = (reward / risk) if risk and reward else None
        return {"risk": risk, "reward": reward, "rr": rr}
    except Exception:
        return {"risk": None, "reward": None, "rr": None}


def _safe_json_loads(value: Any, default: Any) -> Any:
    try:
        if value is None:
            return default
        if isinstance(value, (dict, list)):
            return value
        s = str(value).strip()
        if not s:
            return default
        return json.loads(s)
    except Exception:
        return default


def _as_clean_text(v: Any, max_len: int = 2000) -> str:
    """FLO-207: Convert value to human-readable text. Extracts summary from dicts
    instead of dumping raw JSON."""
    try:
        if v is None:
            return ""
        if isinstance(v, dict):
            # Try to extract a readable string from common keys
            for key in ("summary", "content", "message", "text", "description"):
                val = v.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()[:max_len] if max_len else val.strip()
            # Fallback: join string values
            parts = [str(val) for val in v.values() if isinstance(val, (str, int, float)) and str(val).strip()]
            if parts:
                return " | ".join(parts)[:max_len] if max_len else " | ".join(parts)
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            # Join list items as readable text
            parts = [str(item).strip() for item in v if str(item).strip()]
            if parts:
                return "; ".join(parts)[:max_len] if max_len else "; ".join(parts)
            return json.dumps(v, ensure_ascii=False)
        s = str(v)
        if max_len and len(s) > max_len:
            return s[:max_len]
        return s
    except Exception:
        return ""


def _default_sage_report() -> Dict[str, Any]:
    return {
        "report_date": None,
        "trade_count_analyzed": 0,
        "period_start": None,
        "period_end": None,
        "recommendations": [],
        "insights": [],
        "metadata": {},
    }


def _default_sage_last_run() -> Dict[str, Any]:
    return {"last_run_date": None, "last_run_time": None}


def _read_sage_payload() -> Dict[str, Any]:
    report = _default_sage_report()
    last_run = _default_sage_last_run()

    try:
        if SAGE_REPORT_FILE.exists():
            loaded_report = _safe_json_loads(SAGE_REPORT_FILE.read_text(encoding="utf-8"), default={})
            if isinstance(loaded_report, dict):
                report.update(loaded_report)
    except Exception:
        pass

    try:
        if SAGE_LAST_RUN_FILE.exists():
            loaded_last_run = _safe_json_loads(SAGE_LAST_RUN_FILE.read_text(encoding="utf-8"), default={})
            if isinstance(loaded_last_run, dict):
                last_run.update(loaded_last_run)
    except Exception:
        pass

    has_report = bool(report.get("report_date") or report.get("trade_count_analyzed") or report.get("recommendations") or report.get("insights"))
    has_last_run = bool(last_run.get("last_run_time") or last_run.get("last_run_date"))

    return {
        "status": "ACTIVE" if (has_report or has_last_run) else "STANDBY",
        "report": report,
        "last_run": last_run,
    }


def _safe_iso_timestamp(ts: Any) -> str:
    """FLO-286: Normalize any timestamp to UTC ISO with explicit Z suffix.

    All API responses send timestamps in this format. The browser then converts
    to user local time via window.displayTime() helper (CLAUDE.md Rule 22).
    """
    try:
        if ts is None:
            return ""
        from tz_utils import to_utc_iso
        if isinstance(ts, datetime):
            return to_utc_iso(ts)
        return to_utc_iso(str(ts))
    except Exception:
        return ""


def _read_agent_session_memory() -> Dict[str, Any]:
    try:
        p = (APP_DIR / ".." / "data" / "agent_session_memory.json").resolve()
        if not p.exists():
            return {}
        return _safe_json_loads(p.read_text(encoding="utf-8"), default={}) or {}
    except Exception:
        return {}


def _extract_tools_used(tool_trace: Any) -> List[str]:
    tools = []
    for entry in (tool_trace or []):
        try:
            e = entry or {}
            name = e.get("name")
            if not name:
                continue

            rendered = str(name)
            inp = e.get("input") or {}

            # Enrich key tools with compact args for dashboard visibility
            try:
                if name == "get_candles":
                    tf = inp.get("timeframe") or inp.get("tf")
                    if tf:
                        rendered = f"get_candles({tf})"
                elif name == "get_indicators":
                    tf = inp.get("timeframe") or inp.get("tf")
                    if tf:
                        rendered = f"get_indicators({tf})"
                elif name == "get_sr_zones":
                    tf = inp.get("timeframe") or inp.get("tf")
                    if tf:
                        rendered = f"get_sr_zones({tf})"
                elif name == "get_headlines":
                    rendered = "get_headlines"
                elif name == "get_macro":
                    rendered = "get_macro"
                elif name == "get_current_price":
                    rendered = "get_current_price"
            except Exception:
                rendered = str(name)

            tools.append(rendered)
        except Exception:
            continue
    # preserve order, de-dup
    seen = set()
    out = []
    for t in tools:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _build_trade_room_messages(limit: int = 50) -> List[Dict[str, Any]]:
    if not HISTORY_DB.exists():
        return []

    limit = max(1, min(int(limit or 50), 200))

    try:
        conn = _get_history_conn()

        # 1) Latest analyses
        analyses = []
        try:
            rows = conn.execute(
                "SELECT * FROM agent_proactive_analyses ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            analyses = [dict(r) for r in rows]
        except Exception:
            analyses = []

        # 1b) Agent events (Simba feed messages)
        events = []
        try:
            erows = conn.execute(
                "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            events = [dict(r) for r in erows]
        except Exception:
            events = []

        # 2) Recent closed trades
        trades = []
        try:
            trows = conn.execute(
                "SELECT ticket, direction, profit, close_reason, open_time, close_time FROM trades WHERE close_time IS NOT NULL ORDER BY close_time DESC LIMIT ?",
                (min(50, limit),),
            ).fetchall()
            trades = [dict(r) for r in trows]
        except Exception:
            trades = []

        conn.close()

        session_mem = _read_agent_session_memory()
        session_thesis = None
        try:
            session_thesis = session_mem.get("thesis")
        except Exception:
            session_thesis = None

        messages: List[Dict[str, Any]] = []

        # Build from events
        for ev in events:
            try:
                ev_id = ev.get("id")
                ts = _safe_iso_timestamp(ev.get("timestamp"))
                et = str(ev.get("event_type") or "").strip().upper()
                author = str(ev.get("author") or "SIMBA").strip().upper() or "SIMBA"
                content = _as_clean_text(ev.get("content"), max_len=4000).strip()
                payload = _safe_json_loads(ev.get("payload_json"), default={})
                if not isinstance(payload, dict):
                    payload = {}

                if not ts or not content:
                    continue

                # FLO-146: Filter out Rex intermediate tool-call text
                if author == "REX" and (
                    "Calling functions." in content
                    or '{"name":"functions.' in content
                    or "I'll call function" in content
                    or "I'll call the function" in content
                ):
                    continue

                messages.append(
                    {
                        "id": f"e:{ev_id}",
                        "timestamp": ts,
                        "author": author,
                        "type": et or "EVENT",
                        "content": content[:4000],
                        "metadata": payload,
                    }
                )
            except Exception:
                continue

        # Build from analyses
        for a in analyses:
            a_id = a.get("id")
            ts = _safe_iso_timestamp(a.get("timestamp"))
            decision = a.get("agent_decision")
            confidence = a.get("agent_confidence")

            tool_trace = _safe_json_loads(a.get("tool_trace"), default=[])
            if not isinstance(tool_trace, list):
                tool_trace = []
            tools_used = _extract_tools_used(tool_trace)

            reasoning = _as_clean_text(a.get("agent_reasoning"), max_len=6000).strip()
            factors = _safe_json_loads(a.get("agent_key_factors"), default=[])
            concerns = _safe_json_loads(a.get("agent_concerns"), default=[])

            if not isinstance(factors, list):
                factors = []
            if not isinstance(concerns, list):
                concerns = []

            factors = [str(x) for x in factors if x is not None][:6]
            concerns = [str(x) for x in concerns if x is not None][:4]

            # a) FLOKI ANALYSIS
            analysis_content = reasoning
            if factors:
                analysis_content = (analysis_content + "\n\nKEY FACTORS:\n- " + "\n- ".join(factors)).strip()

            if analysis_content:
                messages.append(
                    {
                        "id": f"a:{a_id}:analysis",
                        "timestamp": ts,
                        "author": "FLOKI",
                        "type": "ANALYSIS",
                        "content": analysis_content[:6000],
                        "metadata": {
                            "decision": decision,
                            "confidence": confidence,
                            "tools_used": tools_used,
                            "rex_agrees": None,
                            "concerns": concerns,
                            "session_thesis": session_thesis,
                        },
                    }
                )

            # b) Debate tool calls => FLOKI + REX messages
            debate_idx = 0
            for entry in tool_trace:
                try:
                    if (entry or {}).get("name") != "debate_with_rex":
                        continue

                    debate_idx += 1
                    inp = (entry or {}).get("input") or {}
                    res = (entry or {}).get("result") or {}

                    my_reasoning = _as_clean_text(inp.get("my_reasoning"), max_len=6000).strip()
                    my_dir = _as_clean_text(inp.get("my_direction"), max_len=50).strip()
                    my_conf = inp.get("my_confidence")
                    key_data = _as_clean_text(inp.get("key_data"), max_len=1200).strip()

                    floki_debate = my_reasoning
                    if my_dir:
                        floki_debate = (f"DIR: {my_dir}\n" + floki_debate).strip()
                    if key_data:
                        floki_debate = (floki_debate + "\n\nKEY DATA:\n" + key_data).strip()

                    rex_agree = res.get("agree")
                    rex_reasoning = _as_clean_text(res.get("reasoning"), max_len=6000).strip()
                    rex_concerns = res.get("concerns") or []
                    if not isinstance(rex_concerns, list):
                        rex_concerns = []
                    rex_concerns = [str(x) for x in rex_concerns if x is not None][:6]
                    rex_adj = _as_clean_text(res.get("suggested_adjustment"), max_len=600).strip()

                    rex_content = rex_reasoning
                    if rex_concerns:
                        rex_content = (rex_content + "\n\nCONCERNS:\n- " + "\n- ".join(rex_concerns)).strip()
                    if rex_adj:
                        rex_content = (rex_content + "\n\nSUGGESTED ADJUSTMENT:\n" + rex_adj).strip()

                    messages.append(
                        {
                            "id": f"a:{a_id}:debate:{debate_idx}:floki",
                            "timestamp": ts,
                            "author": "FLOKI",
                            "type": "DEBATE",
                            "content": floki_debate[:6000],
                            "metadata": {
                                "decision": decision,
                                "confidence": my_conf,
                                "tools_used": tools_used,
                                "rex_agrees": rex_agree,
                                "session_thesis": session_thesis,
                            },
                        }
                    )
                    messages.append(
                        {
                            "id": f"a:{a_id}:debate:{debate_idx}:rex",
                            "timestamp": ts,
                            "author": "REX",
                            "type": "DEBATE",
                            "content": rex_content[:6000],
                            "metadata": {
                                "rex_agrees": rex_agree,
                                "concerns": rex_concerns,
                                "suggested_adjustment": rex_adj,
                                "session_thesis": session_thesis,
                            },
                        }
                    )
                except Exception:
                    continue

            # c) FLOKI DECISION
            if decision:
                readable_decision = str(decision).replace("_", " ")
                conf_text = f" · {confidence}%" if confidence is not None else ""
                decision_content = f"{readable_decision}{conf_text}".strip()

                messages.append(
                    {
                        "id": f"a:{a_id}:decision",
                        "timestamp": ts,
                        "author": "FLOKI",
                        "type": "DECISION",
                        "content": decision_content[:2500],
                        "metadata": {
                            "decision": decision,
                            "confidence": confidence,
                            "tools_used": tools_used,
                            "session_thesis": session_thesis,
                        },
                    }
                )

        # Build from trades
        for t in trades:
            close_time = _safe_iso_timestamp(t.get("close_time"))
            if not close_time:
                continue
            ticket = t.get("ticket")
            direction = t.get("direction")
            profit = t.get("profit")
            close_reason = t.get("close_reason")

            pnl = None
            try:
                pnl = float(profit) if profit is not None else None
            except Exception:
                pnl = None

            pnl_text = "—"
            if pnl is not None:
                pnl_text = f"${pnl:+.2f}"

            content = f"CLOSED #{ticket} {direction} PnL {pnl_text} (reason: {close_reason})"

            messages.append(
                {
                    "id": f"t:{ticket}:close",
                    "timestamp": close_time,
                    "author": "FLOKI",
                    "type": "TRADE_RESULT",
                    "content": content[:2000],
                    "metadata": {
                        "ticket": ticket,
                        "direction": direction,
                        "profit": profit,
                        "close_reason": close_reason,
                        "open_time": t.get("open_time"),
                        "close_time": close_time,
                        "session_thesis": session_thesis,
                    },
                }
            )

        # Sort by timestamp desc and return top limit
        def _ts_key(m: Dict[str, Any]) -> str:
            # ISO string compare works for standard timestamps; fallback to empty
            return str(m.get("timestamp") or "")

        messages.sort(key=_ts_key, reverse=True)
        return messages[:limit]
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return []


GPT_TRADE_REPORT_SYSTEM_PROMPT = (
    "You are a professional XAUUSD trade analyst. You will receive a CLOSED trade record and a nearby pre-trade system snapshot. "
    "Write an objective, concise post-trade report in ENGLISH ONLY. Base your analysis only on the provided fields. "
    "Do not invent indicators or facts not present. Avoid vague statements. Provide actionable improvements. "
    "Return valid JSON with keys: summary (string), what_went_well (array of strings), what_went_wrong (array of strings), "
    "key_risks_observed (array of strings), suggested_improvements (array of strings), confidence_in_assessment (low|medium|high)."
)


def _call_gpt_trade_report(payload: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "OPENAI_API_KEY missing"}
    if not _openai_available:
        return {"ok": False, "error": "openai package not installed"}

    try:
        client = OpenAI(api_key=api_key)
        model = os.environ.get("GPT_MODEL", "gpt-4o-mini")
        timeout = int(os.environ.get("GPT_TRADE_REPORT_TIMEOUT", "20"))
        temperature = float(os.environ.get("GPT_TRADE_REPORT_TEMPERATURE", "0.2"))

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": GPT_TRADE_REPORT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            timeout=timeout,
        )

        raw = resp.choices[0].message.content
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"ok": False, "error": "invalid_response"}

        def _as_list(v):
            if v is None:
                return []
            if isinstance(v, list):
                return [str(x) for x in v][:10]
            return [str(v)][:10]

        report = {
            "summary": str(data.get("summary", ""))[:600],
            "what_went_well": _as_list(data.get("what_went_well")),
            "what_went_wrong": _as_list(data.get("what_went_wrong")),
            "key_risks_observed": _as_list(data.get("key_risks_observed")),
            "suggested_improvements": _as_list(data.get("suggested_improvements")),
            "confidence_in_assessment": str(data.get("confidence_in_assessment", "medium")).lower(),
        }
        if report["confidence_in_assessment"] not in ("low", "medium", "high"):
            report["confidence_in_assessment"] = "medium"

        return {"ok": True, "model": model, "report": report}
    except Exception as e:
        # Never pass raw OpenAI errors back to the client (can include key fragments)
        safe = _normalize_openai_error(str(e))
        return {"ok": False, "error": safe}


def _offline_state(reason: str, file_age_seconds: float) -> Dict[str, Any]:
    # FLO-286: UTC + Z suffix (was naive datetime.now())
    from tz_utils import utc_iso
    now = utc_iso()
    return {
        "timestamp": now,
        "bot": {
            "status": "OFFLINE",
            "mode": "UNKNOWN",
            "running": False,
            "session_start": None,
            "session_analyses": 0,
            "uptime_seconds": None,
        },
        "market": {"is_open": None, "reason": "", "next_open": None},
        "account": {
            "balance": None,
            "equity": None,
            "margin": None,
            "free_margin": None,
            "profit": None,
            "leverage": None,
            "currency": None,
        },
        "daily_stats": {"date": now.split("T")[0], "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "pnl_percent": 0.0},
        "last_analysis": {},
        "positions": [],
        "trade_history": [],
        "_meta": {"reason": reason, "file_age_seconds": file_age_seconds},
    }


_NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"), headers=_NO_CACHE)


@app.get("/about")
def about():
    return FileResponse(str(STATIC_DIR / "about.html"), headers=_NO_CACHE)


@app.get("/roadmap")
def roadmap():
    return FileResponse(str(STATIC_DIR / "roadmap.html"), headers=_NO_CACHE)


@app.get("/history")
def history():
    return FileResponse(str(STATIC_DIR / "history.html"), headers=_NO_CACHE)


@app.get("/trade-room")
def trade_room():
    return FileResponse(str(STATIC_DIR / "trade_room.html"), headers=_NO_CACHE)


@app.get("/api/health")
def health():
    if not STATE_FILE.exists():
        return JSONResponse({"ok": False, "file_age_seconds": None, "state_file": str(STATE_FILE)})
    age = _file_age_seconds(STATE_FILE)
    return JSONResponse({"ok": age <= OFFLINE_AFTER_SECONDS, "file_age_seconds": round(age, 2), "state_file": str(STATE_FILE)})


def _sanitize_for_json(obj):
    """Replace NaN/Inf floats with None (invalid JSON, crashes JSONResponse)."""
    import math
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def _read_state() -> Dict[str, Any]:
    """Read bot_state.json and enrich with _meta. Used by REST and WebSocket."""
    if not STATE_FILE.exists():
        return _offline_state("missing_state_file", file_age_seconds=10**9)
    age = _file_age_seconds(STATE_FILE)
    try:
        payload = _sanitize_for_json(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return _offline_state("invalid_json", file_age_seconds=age)
    return _enrich_state(payload, age)


def _read_state_from_bytes(content: bytes) -> Dict[str, Any]:
    """Same as _read_state but from already-read bytes (avoids double file read)."""
    age = _file_age_seconds(STATE_FILE)
    try:
        payload = _sanitize_for_json(json.loads(content.decode("utf-8")))
    except Exception:
        return _offline_state("invalid_json", file_age_seconds=age)
    return _enrich_state(payload, age)


def _enrich_state(payload: dict, age: float) -> dict:
    payload.setdefault("_meta", {})
    payload["_meta"].update({"file_age_seconds": round(age, 2)})
    expected_interval = payload.get("_expected_update_interval_seconds") or 60
    if age > expected_interval + 60:
        payload.setdefault("bot", {})
        payload["bot"]["status"] = "OFFLINE"
        payload["bot"]["running"] = False
        payload["_meta"]["reason"] = "stale_state_file"
    else:
        payload["_meta"].setdefault("reason", "ok")
    return payload


@app.get("/api/state")
def state():
    return JSONResponse(_read_state())


# ── WebSocket: push state changes to connected clients ──
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await _ws_manager.connect(ws)
    try:
        await ws.send_json(_read_state())
        while True:
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        _ws_manager.disconnect(ws)


async def _watch_state_file():
    """Background task: detect bot_state.json changes via MD5, broadcast to WebSocket clients."""
    last_hash = ""
    while True:
        await asyncio.sleep(2)
        try:
            if not STATE_FILE.exists() or not _ws_manager.active:
                continue
            content = STATE_FILE.read_bytes()
            h = hashlib.md5(content).hexdigest()
            if h == last_hash:
                continue
            last_hash = h
            await _ws_manager.broadcast(_read_state_from_bytes(content))
        except Exception:
            pass


@app.on_event("startup")
async def _start_file_watcher():
    asyncio.create_task(_watch_state_file())


@app.get("/api/trade-room")
def trade_room_api(limit: int = 50):
    try:
        msgs = _build_trade_room_messages(limit=limit)
        # FLO-206: Include Floki journal notes
        journal = []
        try:
            sm = _read_agent_session_memory()
            for n in (sm.get("notes") or []):
                if isinstance(n, dict):
                    journal.append({"text": n.get("note", n.get("text", "")), "time": n.get("time", ""), "source": n.get("source", "floki")})
                elif isinstance(n, str):
                    journal.append({"text": n, "time": "", "source": "floki"})
        except Exception:
            pass
        return JSONResponse(_sanitize_for_json({"messages": msgs, "journal": journal}))
    except Exception:
        return JSONResponse({"messages": [], "journal": []})


# ──────────────────────────────────────────────────────────────────
# Snow plan inspection endpoints (FLO-377)
# ──────────────────────────────────────────────────────────────────

# Statuses considered "active" for the default Snow plans listing.
# Mirrors snow.snow_loop's get_active_plans contract.
_SNOW_ACTIVE_STATUSES = ("pending", "triggered", "active", "closing")
_SNOW_TERMINAL_STATUSES = ("closed", "expired", "cancelled", "failed")
_SNOW_ALL_STATUSES = _SNOW_ACTIVE_STATUSES + _SNOW_TERMINAL_STATUSES


def _snow_summarize_plan_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Pure shaping helper — same fields as state_writer's summary so
    the dashboard can reuse rendering across the home card and the
    Trade Room page. Adds outcome columns when present.
    """
    try:
        pj = json.loads(row.get("plan_json") or "{}")
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
        "closed_at": row.get("closed_at"),
        "trade_ticket": row.get("trade_ticket"),
        "outcome_pips": row.get("outcome_pips"),
        "outcome_usd": row.get("outcome_usd"),
        "thesis_short": (thesis[:140] + "…") if len(thesis) > 140 else thesis,
        "confidence": analysis.get("confidence"),
        "regime_assumed": analysis.get("regime_assumed"),
        "setup_type": analysis.get("setup_type"),
        "context_tags": analysis.get("context_tags"),
        "n_management": len(pj.get("management") or []),
        "n_exit": len(pj.get("exit") or []),
    }


@app.get("/api/snow/plans")
def snow_plans_api(
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
):
    """List Snow plans for dashboard inspection.

    Query parameters:
      status: "active" (default — pending/triggered/active/closing),
              "terminal" (closed/expired/cancelled/failed), or
              "all". Single status names also accepted (e.g. "pending").
      limit:  1-200 (default 50). Plans are returned newest-first.
      offset: pagination offset.
    """
    try:
        from snow import db as snow_db
        s = (status or "active").lower()
        if s == "active":
            statuses = _SNOW_ACTIVE_STATUSES
        elif s == "terminal":
            statuses = _SNOW_TERMINAL_STATUSES
        elif s == "all":
            statuses = _SNOW_ALL_STATUSES
        elif s in _SNOW_ALL_STATUSES:
            statuses = (s,)
        else:
            return JSONResponse(
                {"success": False,
                 "error": f"unknown status filter: {status!r}. Use "
                          f"'active' | 'terminal' | 'all' or one of "
                          f"{_SNOW_ALL_STATUSES}."},
                status_code=400,
            )
        limit = max(1, min(200, int(limit)))
        # Clamp offset so a malicious ?limit=200&offset=99999 can't pull
        # 100k rows from SQLite per request. 10k is generous for the
        # current Snow retention; revisit when policy changes.
        offset = max(0, min(10_000, int(offset)))

        # snow.db.list_plans_by_status doesn't support offset — fetch
        # limit+offset and slice. The dashboard rarely paginates past
        # a few hundred rows; if that changes, push offset into the SQL.
        rows = snow_db.list_plans_by_status(statuses, limit=limit + offset)
        rows = rows[offset:offset + limit]
        plans = [_snow_summarize_plan_row(r) for r in rows]
        return JSONResponse(_sanitize_for_json({
            "success": True, "count": len(plans), "plans": plans,
        }))
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/snow/plan/{plan_id}")
def snow_plan_detail_api(plan_id: str, limit_audit: int = 50):
    """Full detail for one plan: the parsed plan JSON, recent
    snow_triggers rows, recent snow_evaluations rows, and (if
    available) recent snow_execution_quality rows.

    Plan id format: ``PLAN-YYYYMMDD-NNN``. Unknown id → 404.
    """
    try:
        from snow import db as snow_db
        import re as _re
        if not _re.match(r"^PLAN-\d{8}-\d{3}$", plan_id or ""):
            return JSONResponse(
                {"success": False,
                 "error": f"invalid plan_id format: {plan_id!r}. "
                          f"Expected PLAN-YYYYMMDD-NNN."},
                status_code=400,
            )

        row = snow_db.get_plan(plan_id)
        if row is None:
            return JSONResponse(
                {"success": False, "error": f"plan {plan_id} not found"},
                status_code=404,
            )

        try:
            plan_full = json.loads(row.get("plan_json") or "{}")
        except Exception:
            plan_full = {}

        triggers = snow_db.list_triggers(plan_id=plan_id, limit=int(limit_audit))

        # Pull evaluations + execution_quality directly — both indexed on
        # plan_id, both small per plan.
        evaluations: List[Dict[str, Any]] = []
        execution_quality: List[Dict[str, Any]] = []
        try:
            conn = snow_db._connect()
            try:
                e_rows = conn.execute(
                    "SELECT id, plan_id, contingency_name, evaluated_at, "
                    "       event, conditions_snapshot "
                    "  FROM snow_evaluations "
                    " WHERE plan_id = ? "
                    " ORDER BY evaluated_at DESC, id DESC "
                    " LIMIT ?",
                    (plan_id, int(limit_audit)),
                ).fetchall()
                for r in e_rows:
                    d = dict(r)
                    try:
                        d["conditions_snapshot"] = (
                            json.loads(d["conditions_snapshot"])
                            if d.get("conditions_snapshot") else None
                        )
                    except Exception:
                        pass
                    evaluations.append(d)
                try:
                    eq_rows = conn.execute(
                        "SELECT * FROM snow_execution_quality "
                        " WHERE plan_id = ? "
                        " ORDER BY id DESC "
                        " LIMIT ?",
                        (plan_id, int(limit_audit)),
                    ).fetchall()
                    execution_quality = [dict(r) for r in eq_rows]
                except Exception:
                    execution_quality = []
            finally:
                conn.close()
        except Exception:
            pass

        return JSONResponse(_sanitize_for_json({
            "success": True,
            "summary": _snow_summarize_plan_row(row),
            "plan": plan_full,
            "triggers": triggers,
            "evaluations": evaluations,
            "execution_quality": execution_quality,
        }))
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )


@app.get("/api/agent-watch-conditions")
def agent_watch_conditions():
    try:
        if not WATCH_CONDITIONS_FILE.exists():
            return JSONResponse({"updated_at": None, "conditions": []})
        payload = _safe_json_loads(WATCH_CONDITIONS_FILE.read_text(encoding="utf-8"), default={})
        if not isinstance(payload, dict):
            payload = {}
        return JSONResponse(payload)
    except Exception:
        return JSONResponse({"updated_at": None, "conditions": []})


@app.get("/api/sage")
def sage_api():
    try:
        return JSONResponse(_read_sage_payload())
    except Exception:
        return JSONResponse(
            {
                "status": "STANDBY",
                "report": _default_sage_report(),
                "last_run": _default_sage_last_run(),
            }
        )


@app.get("/api/echo")
def echo_api():
    """Return Echo News Sentinel status for Trade Room card."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        alerts_file = data_dir / "echo_alerts.json"
        cost_file = data_dir / "echo_daily_cost.json"

        alerts = []
        if alerts_file.exists():
            try:
                alerts = json.loads(alerts_file.read_text(encoding="utf-8"))
                if not isinstance(alerts, list):
                    alerts = []
            except Exception:
                alerts = []

        # FLO-286: UTC day boundary (was local — counted "today" wrong when server != UTC)
        from tz_utils import trading_day_utc
        today = trading_day_utc()
        today_alerts = [a for a in alerts if (a.get("timestamp") or "").startswith(today)]
        critical_today = sum(1 for a in today_alerts if a.get("classification") == "CRITICAL")
        important_today = sum(1 for a in today_alerts if a.get("classification") == "IMPORTANT")

        cost_data = {}
        if cost_file.exists():
            try:
                cost_data = json.loads(cost_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Read last_scan from echo_status.json (written every cycle)
        status_file = data_dir / "echo_status.json"
        last_scan = None
        headlines_scanned = 0
        if status_file.exists():
            try:
                status_data = json.loads(status_file.read_text(encoding="utf-8"))
                last_scan = status_data.get("last_scan_at")
                headlines_scanned = status_data.get("headlines_scanned", 0)
            except Exception:
                pass
        if not last_scan and today_alerts:
            last_scan = today_alerts[-1].get("timestamp")

        enabled = bool(getattr(config, "ECHO_ENABLED", False)) if "config" in dir() else True
        try:
            from config import ECHO_ENABLED
            enabled = bool(ECHO_ENABLED)
        except Exception:
            pass

        # FLO-73: Story cluster stats
        clustered_count = sum(1 for a in today_alerts if a.get("headline_count", 1) > 1)
        total_headlines_in_clusters = sum(a.get("headline_count", 1) for a in today_alerts if a.get("headline_count", 1) > 1)
        stories_today = len(today_alerts)  # Each alert entry = one story (may contain multiple headlines)

        # FLO-72: Sentiment aggregate
        aggregate = None
        try:
            agg_file = data_dir / "echo_aggregate.json"
            if agg_file.exists():
                aggregate = json.loads(agg_file.read_text(encoding="utf-8"))
        except Exception:
            pass

        return JSONResponse({
            "status": "ACTIVE" if enabled else "DISABLED",
            "last_scan": last_scan,
            "alerts_today": len(today_alerts),
            "critical_today": critical_today,
            "important_today": important_today,
            "total_alerts": len(alerts),
            "daily_cost": cost_data.get("total_usd", 0),
            "daily_calls": cost_data.get("calls", 0),
            "sentiment_aggregate": aggregate,
            "stories_today": stories_today,
            "clustered_stories": clustered_count,
            "total_headlines_in_clusters": total_headlines_in_clusters,
        })
    except Exception:
        return JSONResponse({
            "status": "STANDBY",
            "last_scan": None,
            "alerts_today": 0,
            "critical_today": 0,
            "important_today": 0,
            "total_alerts": 0,
            "daily_cost": 0,
            "daily_calls": 0,
        })


@app.get("/api/echo-health")
def echo_health_api():
    """Return Echo RSS feed health for Trade Room."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        health_file = data_dir / "echo_feed_health.json"

        if not health_file.exists():
            return JSONResponse({"total_feeds": 0, "healthy": 0, "failing": 0, "failing_feeds": [], "feeds": {}})

        health = json.loads(health_file.read_text(encoding="utf-8"))
        if not isinstance(health, dict):
            return JSONResponse({"total_feeds": 0, "healthy": 0, "failing": 0, "failing_feeds": [], "feeds": {}})

        total = len(health)
        failing_feeds = []
        for name, entry in health.items():
            if isinstance(entry, dict) and entry.get("consecutive_failures", 0) >= 3:
                failing_feeds.append({
                    "name": name,
                    "consecutive_failures": entry["consecutive_failures"],
                    "last_error": entry.get("last_error", "unknown"),
                    "last_success": entry.get("last_success"),
                    "last_failure": entry.get("last_failure"),
                })

        return JSONResponse({
            "total_feeds": total,
            "healthy": total - len(failing_feeds),
            "failing": len(failing_feeds),
            "failing_feeds": failing_feeds,
            "feeds": health,
        })
    except Exception:
        return JSONResponse({"total_feeds": 0, "healthy": 0, "failing": 0, "failing_feeds": [], "feeds": {}})


@app.get("/api/luna-brief")
def luna_brief_api():
    """Return Luna macro analyst brief for Trade Room card."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        brief_file = data_dir / "luna_brief.json"

        if not brief_file.exists():
            return JSONResponse({"brief": None, "stale": True})

        brief = json.loads(brief_file.read_text(encoding="utf-8"))
        if not isinstance(brief, dict):
            return JSONResponse({"brief": None, "stale": True})

        # Check freshness — stale if older than 30 min
        stale = False
        ts = brief.get("timestamp")
        if ts:
            try:
                brief_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                now_utc = datetime.now(tz=brief_time.tzinfo) if brief_time.tzinfo else datetime.utcnow()
                age_min = (now_utc - brief_time).total_seconds() / 60
                stale = age_min > 30
                brief["age_minutes"] = round(age_min, 1)
            except Exception:
                pass

        # FLO-236: Include deep research if available
        deep = None
        try:
            dr_file = data_dir / "deep_research_cache.json"
            if dr_file.exists():
                dr = json.loads(dr_file.read_text(encoding="utf-8"))
                if isinstance(dr, dict) and dr.get("timestamp"):
                    dr_time = datetime.fromisoformat(dr["timestamp"].replace("Z", "+00:00"))
                    dr_age_h = (datetime.now(tz=dr_time.tzinfo) - dr_time).total_seconds() / 3600
                    if dr_age_h < 3:
                        deep = dr
        except Exception:
            pass

        return JSONResponse({"brief": brief, "stale": stale, "deep_research": deep})
    except Exception:
        return JSONResponse({"brief": None, "stale": True, "deep_research": None})


@app.get("/api/rex-monitor")
def rex_monitor_api():
    """Return Rex proactive monitor scan for Trade Room badge (FLO-214)."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        mon_file = data_dir / "rex_monitor.json"

        if not mon_file.exists():
            return JSONResponse({"monitor": None, "stale": True})

        monitor = json.loads(mon_file.read_text(encoding="utf-8"))
        if not isinstance(monitor, dict):
            return JSONResponse({"monitor": None, "stale": True})

        stale = False
        age_minutes = None
        ts = monitor.get("timestamp")
        if ts:
            try:
                scan_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                now_utc = datetime.now(tz=scan_time.tzinfo) if scan_time.tzinfo else datetime.utcnow()
                age_minutes = round((now_utc - scan_time).total_seconds() / 60, 1)
                stale = age_minutes > 30
            except Exception:
                pass

        # FLO-316: alert_level removed from Rex Monitor schema. Endpoint
        # surfaces findings_count + findings[] only; dashboard renders
        # observational data, no prescriptive badge color.
        return JSONResponse({
            "monitor": {
                "findings_count": monitor.get("findings_count", monitor.get("finding_count", 0)),
                "findings": monitor.get("findings", []),
                "timestamp": ts,
                "age_minutes": age_minutes,
            },
            "stale": stale,
        })
    except Exception:
        return JSONResponse({"monitor": None, "stale": True})


@app.get("/api/sage-weekly")
def sage_weekly_api():
    """Return Sage weekly trending report."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        weekly_file = data_dir / "sage_weekly_report.json"
        if not weekly_file.exists():
            return JSONResponse({})
        data = json.loads(weekly_file.read_text(encoding="utf-8"))
        return JSONResponse(data if isinstance(data, dict) else {})
    except Exception:
        return JSONResponse({})


@app.get("/api/macro-history")
def macro_history_api():
    """Return rolling 5-day macro history from macro_history.json."""
    try:
        data_dir = Path(__file__).parent.parent / "data"
        history_file = data_dir / "macro_history.json"
        if not history_file.exists():
            return JSONResponse({})
        data = json.loads(history_file.read_text(encoding="utf-8"))
        return JSONResponse(data if isinstance(data, dict) else {})
    except Exception:
        return JSONResponse({})


@app.get("/api/indicator-history")
def indicator_history(hours: int = 6):
    if not HISTORY_DB.exists():
        return JSONResponse(
            {
                "rsi": [],
                "macd": [],
                "adx": [],
                "atr": [],
                "ema_distance": [],
                "volume_ratio": [],
            }
        )

    try:
        hrs = int(hours or 6)
    except Exception:
        hrs = 6
    hrs = max(1, min(hrs, 72))

    # Use ISO timestamps stored in DB; SQLite compares lexicographically for ISO8601.
    cutoff = (datetime.utcnow() - timedelta(hours=hrs)).isoformat(timespec="seconds")

    try:
        conn = _get_history_conn()
        q = (
            "SELECT timestamp, rsi_14, macd, adx_14, atr_14, price_vs_ema50_pct, volume_ratio "
            "FROM analyses WHERE timestamp >= ? ORDER BY timestamp ASC LIMIT 200"
        )
        rows = conn.execute(q, (cutoff,)).fetchall()
        conn.close()

        out = {
            "rsi": [],
            "macd": [],
            "adx": [],
            "atr": [],
            "ema_distance": [],
            "volume_ratio": [],
        }

        def _append(key: str, v: Any):
            try:
                if v is None:
                    return
                n = float(v)
                if n != n:
                    return
                out[key].append(n)
            except Exception:
                return

        for r in rows:
            _append("rsi", r[1])
            _append("macd", r[2])
            _append("adx", r[3])
            _append("atr", r[4])
            _append("ema_distance", r[5])
            _append("volume_ratio", r[6])

        return JSONResponse(out)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        return JSONResponse(
            {
                "rsi": [],
                "macd": [],
                "adx": [],
                "atr": [],
                "ema_distance": [],
                "volume_ratio": [],
            }
        )


@app.get("/api/agent-patterns")
def agent_patterns(limit: int = 3):
    try:
        limit = max(1, min(int(limit or 3), 10))

        payload = agent_reflection.read_patterns() or {}
        if not isinstance(payload, dict):
            return JSONResponse({"updated": None, "trade_count": 0, "patterns": []})

        patterns = payload.get("patterns")
        if not isinstance(patterns, list):
            patterns = []

        def _pf(p: Dict[str, Any]) -> float:
            try:
                return float(p.get("pf", 0.0) or 0.0)
            except Exception:
                return 0.0

        def _trades(p: Dict[str, Any]) -> int:
            try:
                return int(p.get("trades", 0) or 0)
            except Exception:
                return 0

        def _is_strong(p: Dict[str, Any]) -> bool:
            try:
                return str(p.get("insight") or "").strip().lower() == "strong edge"
            except Exception:
                return False

        def _is_avoid(p: Dict[str, Any]) -> bool:
            try:
                return str(p.get("insight") or "").strip().lower().startswith("avoid")
            except Exception:
                return False

        strong = [p for p in patterns if isinstance(p, dict) and _is_strong(p)]
        avoid = [p for p in patterns if isinstance(p, dict) and _is_avoid(p)]

        strong.sort(key=lambda p: (_pf(p), _trades(p)), reverse=True)
        avoid.sort(key=lambda p: (_pf(p), -_trades(p)))

        selected: List[Dict[str, Any]] = []
        for p in strong:
            if len(selected) >= limit:
                break
            selected.append(p)
        if len(selected) < limit:
            for p in avoid:
                if len(selected) >= limit:
                    break
                selected.append(p)

        if not selected:
            selected = [p for p in patterns if isinstance(p, dict)][:limit]

        out = {
            "updated": payload.get("updated"),
            "trade_count": payload.get("trade_count", 0),
            "patterns": selected,
        }
        return JSONResponse(out)
    except Exception:
        return JSONResponse({"updated": None, "trade_count": 0, "patterns": []})


@app.get("/api/recent-decisions")
def recent_decisions():
    try:
        conn = _get_history_conn()
        q = """SELECT timestamp, agent_decision, agent_confidence
               FROM agent_proactive_analyses
               ORDER BY id DESC
               LIMIT ?"""
        rows = conn.execute(q, (5,)).fetchall()
        conn.close()

        # app.js expects: [{timestamp, decision, score}]
        out = []
        for r in rows:
            out.append(
                {
                    "timestamp": r[0],
                    "decision": r[1],
                    "score": r[2],
                }
            )
        out.reverse()
        return JSONResponse(out)
    except Exception:
        return JSONResponse([])


@app.get("/api/history-data")
def history_data():
    if not HISTORY_DB.exists():
        return JSONResponse({"error": "History DB not found"})
        
    try:
        conn = _get_history_conn()
        
        # Get all closed trades and join with closest previous analysis
        # FLO-272: Post-Qwen cutoff — exclude pre-April-8 trades even if reconciliation re-inserts them
        query = """
            SELECT t.*,
                   (SELECT confidence FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as confidence,
                   (SELECT scenario FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as scenario,
                   (SELECT scenario_description FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as scenario_description
            FROM trades t
            WHERE t.close_time IS NOT NULL AND t.open_time >= '2026-04-08'
            ORDER BY t.close_time ASC
        """
        rows = conn.execute(query).fetchall()
        conn.close()
        
        trades = []
        monthly_stats_dict = {}
        
        global_gross_profit = 0.0
        global_gross_loss = 0.0
        global_total_profit = 0.0
        global_wins = 0
        global_losses = 0
        global_breakevens = 0
        global_be_activations = 0
        
        best_trade = None
        worst_trade = None
        
        peak_equity = float(INITIAL_BALANCE)
        current_equity = float(INITIAL_BALANCE)
        max_drawdown_dollars = 0.0
        equity_curve = []
        
        # Parse and process each trade
        for r in rows:
            trade = dict(r)
            
            # Formatting values safely
            profit = float(trade.get("profit") or 0.0)
            
            if profit > 0:
                outcome = "win"
                global_wins += 1
                global_gross_profit += profit
            elif profit < 0:
                outcome = "loss"
                global_losses += 1
                global_gross_loss += abs(profit)
            else:
                outcome = "breakeven"
                global_breakevens += 1
                
            global_total_profit += profit
            
            # Tracking best and worst trades
            if best_trade is None or profit > best_trade["profit"]:
                best_trade = trade
            if worst_trade is None or profit < worst_trade["profit"]:
                worst_trade = trade
                
            # Equity curve and Max Drawdown calculation
            current_equity += profit
            equity_curve.append({
                "time": _safe_iso_timestamp(trade.get("close_time")),  # FLO-286: UTC + Z
                "equity": current_equity
            })
            
            if current_equity > peak_equity:
                peak_equity = current_equity
            
            drawdown = peak_equity - current_equity
            if drawdown > max_drawdown_dollars:
                max_drawdown_dollars = drawdown
            
            # Pips Calculation (approximate from prices)
            direction = trade.get("direction")
            open_p = float(trade.get("open_price") or 0)
            close_p = float(trade.get("close_price") or 0)
            pips = 0.0
            if open_p and close_p:
                if direction == "BUY":
                    pips = (close_p - open_p) * 10
                elif direction == "SELL":
                    pips = (open_p - close_p) * 10
            trade["pips"] = round(pips, 1)
            
            # Duration calculation
            open_time_str = trade.get("open_time")
            close_time_str = trade.get("close_time")
            duration_minutes = 0
            if open_time_str and close_time_str:
                try:
                    # FLO-286: strip Z so result is naive (consistent with old/new data mix)
                    ot = datetime.fromisoformat(open_time_str.rstrip("Z"))
                    ct = datetime.fromisoformat(close_time_str.rstrip("Z"))
                    duration_minutes = max(0, int((ct - ot).total_seconds() / 60))
                except Exception:
                    pass
            trade["duration_minutes"] = duration_minutes
            trade["outcome"] = outcome
            
            # Breakeven activation tracking
            be_activated = trade.get("breakeven_activated")
            if be_activated == 1 or be_activated is True:
                global_be_activations += 1
                trade["breakeven_activated"] = True
            else:
                trade["breakeven_activated"] = False if be_activated == 0 else None

            # FLO-286: normalize all output timestamps to UTC ISO with Z suffix
            for _tk in ("open_time", "close_time"):
                if _tk in trade and trade[_tk] is not None:
                    trade[_tk] = _safe_iso_timestamp(trade[_tk])

            trades.append(trade)
            
            # Monthly grouping
            if close_time_str:
                month_key = close_time_str[:7] # YYYY-MM
                if month_key not in monthly_stats_dict:
                    monthly_stats_dict[month_key] = {
                        "month": month_key,
                        "trades": 0,
                        "wins": 0,
                        "losses": 0,
                        "breakevens": 0,
                        "profit": 0.0,
                        "gross_profit": 0.0,
                        "gross_loss": 0.0,
                        "max_drawdown": 0.0,
                        "peak_equity": 0.0,
                        "current_equity": 0.0,
                        "best_trade": None,
                        "worst_trade": None
                    }
                    
                ms = monthly_stats_dict[month_key]
                ms["trades"] += 1
                if outcome == "win":
                    ms["wins"] += 1
                    ms["gross_profit"] += profit
                elif outcome == "loss":
                    ms["losses"] += 1
                    ms["gross_loss"] += abs(profit)
                else:
                    ms["breakevens"] += 1
                
                ms["profit"] += profit
                
                if ms["best_trade"] is None or profit > ms["best_trade"]["profit"]:
                    ms["best_trade"] = trade
                if ms["worst_trade"] is None or profit < ms["worst_trade"]["profit"]:
                    ms["worst_trade"] = trade
                
                ms["current_equity"] += profit
                if ms["current_equity"] > ms["peak_equity"]:
                    ms["peak_equity"] = ms["current_equity"]
                dd = ms["peak_equity"] - ms["current_equity"]
                if dd > ms["max_drawdown"]:
                    ms["max_drawdown"] = dd

        def _calc_stats(trade_rows):
            total = len(trade_rows)
            wins = sum(1 for t in trade_rows if t.get("outcome") == "win")
            losses = sum(1 for t in trade_rows if t.get("outcome") == "loss")
            breakevens = sum(1 for t in trade_rows if t.get("outcome") == "breakeven")
            gross_profit = sum(float(t.get("profit") or 0.0) for t in trade_rows if t.get("outcome") == "win")
            gross_loss = sum(abs(float(t.get("profit") or 0.0)) for t in trade_rows if t.get("outcome") == "loss")
            total_profit = sum(float(t.get("profit") or 0.0) for t in trade_rows)
            win_rate = (wins / (wins + losses)) * 100 if (wins + losses) > 0 else 0.0
            profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0)
            avg_win = gross_profit / wins if wins > 0 else 0.0
            avg_loss = gross_loss / losses if losses > 0 else 0.0
            avg_duration = sum(t.get("duration_minutes", 0) for t in trade_rows) / total if total > 0 else 0.0
            be_count = sum(1 for t in trade_rows if t.get("breakeven_activated") is True)
            be_known = sum(1 for t in trade_rows if t.get("breakeven_activated") is not None)
            be_rate = (be_count / be_known * 100) if be_known > 0 else 0.0
            return {
                "total_trades": total,
                "wins": wins,
                "losses": losses,
                "breakevens": breakevens,
                "win_rate": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "total_profit": round(total_profit, 2),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "avg_duration_minutes": round(avg_duration, 0),
                "be_activation_count": be_count,
                "be_activation_rate": round(be_rate, 1),
            }

        def _calc_max_drawdown(trade_rows):
            peak = 0.0
            equity = 0.0
            max_dd = 0.0
            for t in trade_rows:
                equity += float(t.get("profit") or 0.0)
                if equity > peak:
                    peak = equity
                drawdown = peak - equity
                if drawdown > max_dd:
                    max_dd = drawdown
            return max_dd

        total_trades = len(trades)
        win_rate = (global_wins / (global_wins + global_losses)) * 100 if (global_wins + global_losses) > 0 else 0.0
        profit_factor = global_gross_profit / global_gross_loss if global_gross_loss > 0 else (global_gross_profit if global_gross_profit > 0 else 0)
        
        avg_win = global_gross_profit / global_wins if global_wins > 0 else 0.0
        avg_loss = global_gross_loss / global_losses if global_losses > 0 else 0.0
        
        avg_duration = sum(t["duration_minutes"] for t in trades) / total_trades if total_trades > 0 else 0
        
        # Format monthly stats list
        monthly_stats = []
        for mk in sorted(monthly_stats_dict.keys(), reverse=True):
            ms = monthly_stats_dict[mk]
            ms["win_rate"] = (ms["wins"] / (ms["wins"] + ms["losses"])) * 100 if (ms["wins"] + ms["losses"]) > 0 else 0.0
            ms["profit_factor"] = ms["gross_profit"] / ms["gross_loss"] if ms["gross_loss"] > 0 else (ms["gross_profit"] if ms["gross_profit"] > 0 else 0)
            monthly_stats.append(ms)

        # Reverse trades to have newest first for the UI table
        trades.reverse()

        # BE activation rate (only count trades with known BE status)
        trades_with_be_data = sum(1 for t in trades if t.get("breakeven_activated") is not None)
        be_activation_rate = (global_be_activations / trades_with_be_data * 100) if trades_with_be_data > 0 else 0.0

        global_stats = {
            "total_trades": total_trades,
            "wins": global_wins,
            "losses": global_losses,
            "breakevens": global_breakevens,
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 2),
            "total_profit": round(global_total_profit, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "max_drawdown": round(max_drawdown_dollars, 2),
            "avg_duration_minutes": round(avg_duration, 0),
            "best_trade_profit": round(best_trade["profit"], 2) if best_trade else 0.0,
            "worst_trade_profit": round(worst_trade["profit"], 2) if worst_trade else 0.0,
            "be_activation_count": global_be_activations,
            "be_activation_rate": round(be_activation_rate, 1),
        }

        # Post-Qwen-migration cleanup: all remaining trades are Qwen (≥ 2026-04-08).
        # No Population A/B distinction — single "All Trades" dataset.
        live_stats = _calc_stats(trades)
        live_stats["max_drawdown"] = round(_calc_max_drawdown(trades), 2)

        # Total P&L from bot_state.json balance minus INITIAL_BALANCE
        total_pnl = None
        current_balance = None
        try:
            if STATE_FILE.exists():
                with open(STATE_FILE, "r") as f:
                    bot_state = json.load(f)
                account = bot_state.get("account", {})
                current_balance = float(account.get("balance", 0))
                total_pnl = round(current_balance - INITIAL_BALANCE, 2)
        except Exception:
            pass

        # Main stat cards — override total_profit with balance-derived P&L
        card_stats = dict(live_stats)
        if total_pnl is not None:
            card_stats["total_profit"] = total_pnl
        card_stats["best_trade_profit"] = round(best_trade["profit"], 2) if best_trade else 0.0
        card_stats["worst_trade_profit"] = round(worst_trade["profit"], 2) if worst_trade else 0.0

        # Build equity curve from ALL trades, anchored to real balance.
        # DB profit doesn't include swap/commission, so sum(profit) != actual P&L.
        # Distribute the discrepancy evenly so curve starts at INITIAL_BALANCE
        # and ends at the real balance from bot_state.json.
        all_trades_chrono = sorted(trades, key=lambda x: x.get("close_time") or "")
        sum_all_profits = sum(float(t.get("profit") or 0.0) for t in all_trades_chrono)
        n_trades = len(all_trades_chrono)

        if current_balance is not None and n_trades > 0:
            real_pnl = current_balance - float(INITIAL_BALANCE)
            offset = real_pnl - sum_all_profits  # untracked swap/commission
            per_trade_adj = offset / n_trades
        else:
            per_trade_adj = 0.0

        running_balance = float(INITIAL_BALANCE)
        anchored_equity_curve = []
        for t in all_trades_chrono:
            running_balance += float(t.get("profit") or 0.0) + per_trade_adj
            anchored_equity_curve.append({
                "time": _safe_iso_timestamp(t.get("close_time")),  # FLO-286: UTC + Z
                "equity": round(running_balance, 2)
            })

        return JSONResponse({
            "global_stats": card_stats,
            "live_stats": live_stats,
            "monthly_stats": monthly_stats,
            "equity_curve": anchored_equity_curve,
            "trades": trades,
            "initial_balance": float(INITIAL_BALANCE),
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)})


@app.get("/api/live-readiness")
def live_readiness():
    """FLO-272: Live readiness panel — 6 metrics measuring trading system maturity."""
    if not HISTORY_DB.exists():
        return JSONResponse({"error": "History DB not found"})

    try:
        # FLO-272: Post-Qwen cutoff — only count trades from Qwen migration onward.
        # This makes the panel immune to reconciliation re-inserting old trades.
        QWEN_CUTOFF = "2026-04-08"
        conn = _get_history_conn()
        rows = conn.execute(
            "SELECT profit, close_time FROM trades "
            "WHERE close_price IS NOT NULL AND profit IS NOT NULL "
            "AND open_time >= ? "
            "ORDER BY close_time ASC",
            (QWEN_CUTOFF,),
        ).fetchall()
        conn.close()

        trades = [dict(r) for r in rows]

        def _compute_metrics(trade_list):
            """Compute the 4 time-windowed metrics for a given trade list."""
            if not trade_list:
                return {"profit_factor": 0, "win_rate": 0, "avg_win_loss": 0, "trades": 0}
            wins = [float(t["profit"]) for t in trade_list if float(t["profit"] or 0) > 0]
            losses = [float(t["profit"]) for t in trade_list if float(t["profit"] or 0) < 0]
            total_wins = sum(wins)
            total_losses = abs(sum(losses))
            pf = (total_wins / total_losses) if total_losses > 0 else (999.0 if total_wins > 0 else 0)
            total_wl = len(wins) + len(losses)
            wr = (len(wins) / total_wl * 100) if total_wl > 0 else 0
            avg_win = (sum(wins) / len(wins)) if wins else 0
            avg_loss = abs(sum(losses) / len(losses)) if losses else 0
            ratio = (avg_win / avg_loss) if avg_loss > 0 else (999.0 if avg_win > 0 else 0)
            return {
                "profit_factor": round(pf, 2),
                "win_rate": round(wr, 1),
                "avg_win_loss": round(ratio, 2),
                "trades": len(trade_list),
            }

        now = datetime.utcnow()
        cutoff_7d = now - timedelta(days=7)
        cutoff_14d = now - timedelta(days=14)

        def _parse_ct(ts):
            # FLO-286: After Phase 1, close_time may have "Z" suffix. Always parse
            # to a NAIVE datetime so comparisons with `now = datetime.utcnow()` work
            # without "can't compare offset-naive and offset-aware" errors.
            try:
                s = (ts or "").rstrip("Z").split(".")[0]
                return datetime.fromisoformat(s)
            except Exception:
                return None

        # Note: close_time in DB is broker time (UTC+offset). For trend windows we
        # compare relative — offset cancels out. Still convert for consistency.
        import config as _cfg
        _offset_h = int(getattr(_cfg, "MT5_SERVER_UTC_OFFSET", 2) or 2)

        last_7d_trades = []
        prev_7d_trades = []
        for t in trades:
            ct = _parse_ct(t.get("close_time"))
            if ct is None:
                continue
            ct_utc = ct - timedelta(hours=_offset_h)
            if ct_utc >= cutoff_7d:
                last_7d_trades.append(t)
            elif ct_utc >= cutoff_14d:
                prev_7d_trades.append(t)

        all_metrics = _compute_metrics(trades)
        last_7d = _compute_metrics(last_7d_trades)
        prev_7d = _compute_metrics(prev_7d_trades)

        # Max drawdown (inline — _calc_max_drawdown is nested in history_data)
        peak_eq = 0.0
        eq_running = 0.0
        max_dd_dollars = 0.0
        for t in sorted(trades, key=lambda x: x.get("close_time") or ""):
            eq_running += float(t.get("profit") or 0.0)
            if eq_running > peak_eq:
                peak_eq = eq_running
            dd = peak_eq - eq_running
            if dd > max_dd_dollars:
                max_dd_dollars = dd
        max_dd_pct = (max_dd_dollars / float(INITIAL_BALANCE) * 100) if INITIAL_BALANCE else 0

        # Days without P0 incident
        _p0_path = os.path.join(str(_ROOT), "data", "last_p0_incident.json")
        days_without_p0 = 0
        p0_ts = None
        if os.path.exists(_p0_path):
            try:
                with open(_p0_path, "r") as f:
                    p0_ts = json.load(f).get("timestamp")
            except Exception:
                pass
        if not p0_ts:
            # Fallback: earliest trade open_time
            if trades:
                conn2 = _get_history_conn()
                row = conn2.execute("SELECT MIN(open_time) FROM trades").fetchone()
                conn2.close()
                p0_ts = row[0] if row else None
        if p0_ts:
            try:
                p0_dt = datetime.fromisoformat(str(p0_ts).rstrip("Z").split(".")[0])  # FLO-286: strip Z
                days_without_p0 = max(0, (now - p0_dt).days)
            except Exception:
                pass

        # Classify each metric
        def _trend(curr, prev, higher_is_better=True):
            if prev == 0 or prev is None:
                return "stable"
            if curr is None:
                return "stable"
            delta_pct = (curr - prev) / abs(prev) * 100 if prev else 0
            if not higher_is_better:
                delta_pct = -delta_pct
            if delta_pct > 2:
                return "improving"
            if delta_pct < -2:
                return "declining"
            return "stable"

        def _level(value, minimum, ideal, higher_is_better=True):
            if higher_is_better:
                if value >= ideal:
                    return "ideal"
                if value >= minimum:
                    return "min"
                return "below"
            else:
                if value <= ideal:
                    return "ideal"
                if value <= minimum:
                    return "min"
                return "below"

        # FLO-318: dynamic Win Rate thresholds based on Avg Win/Loss ratio.
        # A fixed 50% floor is wrong for a system with R/R > 1: the
        # mathematical breakeven WR is 1/(1+ratio). With ratio=1.8 that's
        # 35.7%, so a 50% floor misclassifies a profitable edge as "below
        # minimum". Compute thresholds from the current ratio and surface
        # them on the card so the number is auditable (the frontend already
        # renders m.min and m.ideal — no JS change needed).
        _wl_ratio = float(all_metrics.get("avg_win_loss") or 0)
        # Fallback to the old fixed 50/55 only when the ratio is degenerate
        # (no data / no losses yet / suspiciously extreme). 0 < ratio < 10
        # is the band where the formula is meaningful.
        if 0 < _wl_ratio < 10:
            _breakeven = 1.0 / (1.0 + _wl_ratio) * 100.0  # percentage
            _wr_min = round(_breakeven + 5.0, 1)
            _wr_ideal = round(_breakeven + 12.0, 1)
        else:
            _wr_min, _wr_ideal = 50.0, 55.0

        metrics = {
            "profit_factor": {
                "value": all_metrics["profit_factor"],
                "min": 1.2, "ideal": 1.5,
                "level": _level(all_metrics["profit_factor"], 1.2, 1.5),
                "trend": _trend(last_7d["profit_factor"], prev_7d["profit_factor"]),
            },
            "win_rate": {
                "value": all_metrics["win_rate"],
                "min": _wr_min, "ideal": _wr_ideal,
                "level": _level(all_metrics["win_rate"], _wr_min, _wr_ideal),
                "trend": _trend(last_7d["win_rate"], prev_7d["win_rate"]),
            },
            "avg_win_loss": {
                "value": all_metrics["avg_win_loss"],
                "min": 1.5, "ideal": 2.0,
                "level": _level(all_metrics["avg_win_loss"], 1.5, 2.0),
                "trend": _trend(last_7d["avg_win_loss"], prev_7d["avg_win_loss"]),
            },
            "trades": {
                "value": all_metrics["trades"],
                "min": 50, "ideal": 100,
                "level": _level(all_metrics["trades"], 50, 100),
                "trend": _trend(last_7d["trades"], prev_7d["trades"]),
            },
            "max_drawdown": {
                "value": round(max_dd_pct, 1),
                "min": 10, "ideal": 5,
                "level": _level(max_dd_pct, 10, 5, higher_is_better=False),
                "trend": "stable",  # cumulative, not windowed
                "higher_is_better": False,
            },
            "days_without_p0": {
                "value": days_without_p0,
                "min": 14, "ideal": 30,
                "level": _level(days_without_p0, 14, 30),
                "trend": "improving" if days_without_p0 > 0 else "stable",
            },
        }

        criteria_met = sum(1 for m in metrics.values() if m["level"] in ("min", "ideal"))

        # Status: all 6 met = READY, at least half (3+) = MINIMUM, else NOT_READY
        if criteria_met == 6:
            status = "READY_FOR_LIVE"
        elif criteria_met >= 3:
            status = "MINIMUM_READY"
        else:
            status = "NOT_READY"

        return JSONResponse({
            "status": status,
            "criteria_met": criteria_met,
            "criteria_total": 6,
            "metrics": metrics,
            "last_updated": now.isoformat(),
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)})


@app.get("/api/journal")
def journal_data():
    """FLO-269: Trade journal — trades with MFE, capture rate, adjustments, counterfactual."""
    if not HISTORY_DB.exists():
        return JSONResponse({"error": "History DB not found"})

    try:
        import config as _cfg
        _offset = int(getattr(_cfg, "MT5_SERVER_UTC_OFFSET", 2) or 2)

        def _session(ts):
            try:
                d = datetime.fromisoformat((ts or "").rstrip("Z").split(".")[0])  # FLO-286
                h = (d.hour - _offset) % 24
                if 0 <= h < 7: return "Asian"
                if 7 <= h < 13: return "London"
                if 13 <= h < 22: return "NY"
                return "OffHours"
            except Exception:
                return "?"

        conn = _get_history_conn()
        rows = conn.execute(
            "SELECT ticket, direction, volume, open_price, close_price, sl, tp, "
            "profit, close_reason, open_time, close_time, mfe_points, mae_points, "
            "final_sl, breakeven_activated, decision_source, comment "  # FLO-301: comment for entry_type detection
            "FROM trades WHERE close_price IS NOT NULL AND profit IS NOT NULL "
            "ORDER BY close_time DESC LIMIT 30"
        ).fetchall()

        # Load adjustments
        adj_rows = conn.execute(
            "SELECT ticket, timestamp, old_sl, new_sl, old_tp, new_tp, source "
            "FROM trade_adjustments ORDER BY timestamp ASC"
        ).fetchall()

        # FLO-300: Load OPEN_* decisions so we can attach the opening confidence
        # to each trade. Pulled once and indexed by (direction, timestamp) so the
        # per-trade match is O(n) without repeated DB roundtrips.
        try:
            open_rows = conn.execute(
                "SELECT timestamp, agent_decision, agent_confidence "
                "FROM agent_proactive_analyses "
                "WHERE agent_decision IN ('OPEN_BUY', 'OPEN_SELL') "
                "ORDER BY timestamp ASC"
            ).fetchall()
        except Exception:
            open_rows = []
        conn.close()

        def _find_open_conf(direction, open_time_iso):
            """Closest OPEN_{direction} decision within 10 min of open_time."""
            if not direction or not open_time_iso:
                return None
            want = "OPEN_BUY" if str(direction).upper() == "BUY" else "OPEN_SELL"
            try:
                t_target = datetime.fromisoformat(open_time_iso.rstrip("Z").split(".")[0])
            except Exception:
                return None
            best = None
            best_delta = None
            for d in open_rows:
                if d["agent_decision"] != want:
                    continue
                try:
                    t_dec = datetime.fromisoformat(str(d["timestamp"]).rstrip("Z").split(".")[0])
                except Exception:
                    continue
                delta = abs((t_target - t_dec).total_seconds())
                if delta > 600:  # 10 min window
                    continue
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best = d["agent_confidence"]
            return best

        # Index adjustments by ticket
        adj_by_ticket = {}
        for a in adj_rows:
            ad = dict(a)
            tk = ad["ticket"]
            adj_by_ticket.setdefault(tk, []).append(ad)

        # Reports dir
        reports_dir = os.path.join(str(_ROOT), "data", "post_trade_reports")

        trades = []
        total_capture = []
        adj_helped = 0
        adj_hurt = 0
        adj_neutral = 0

        for r in rows:
            t = dict(r)
            ticket = t["ticket"]
            pnl = float(t.get("profit") or 0)
            mfe = t.get("mfe_points")
            mae = t.get("mae_points")
            entry = t.get("open_price") or 0
            orig_sl = t.get("sl")
            adjustments = adj_by_ticket.get(ticket, [])
            if adjustments:
                orig_sl = adjustments[0].get("old_sl") or orig_sl

            # FLO-290: capture = pips/pips (was dollars/pips — unit mismatch).
            # FLO-300: add display-friendly capture that clamps [−100%, 500%]
            # and substitutes "LOSS" when MFE is noise-floor small.
            from capture import (
                compute_capture_pct,
                pnl_pips,
                format_capture_display,
                is_capture_includable_in_average,
            )
            capture = compute_capture_pct(
                direction=t.get("direction"),
                entry_price=t.get("open_price"),
                close_price=t.get("close_price"),
                mfe_points=mfe,
            )
            _pp = pnl_pips(t.get("direction"), t.get("open_price"), t.get("close_price"))
            capture_display = format_capture_display(capture, mfe, _pp)
            # FLO-308: share the noise-floor filter with the display helper so
            # the aggregate matches what the UI shows — trades rendered as
            # "LOSS" never contribute their absurd raw % to the average.
            if is_capture_includable_in_average(capture, mfe, _pp):
                total_capture.append(capture)

            # FLO-301: detect PENDING vs MARKET from the MT5 comment column.
            # Comment comes from execute_trade / place_pending_order — pending
            # orders carry "Pending-BUY_LIMIT" / "Pending-SELL_STOP" / etc.,
            # market orders carry "floki_agent" or "reconciled:Agent-*".
            _cmt = (t.get("comment") or "")
            entry_type = "PENDING" if ("pending" in _cmt.lower()) else "MARKET"

            # FLO-300/301: opening confidence ONLY for market orders. For
            # pending orders the fill happens hours after the decision in
            # different market conditions, so attributing a confidence % is
            # misleading. Frontend renders open_conf=None as "P.O." when
            # entry_type is PENDING.
            if entry_type == "MARKET":
                open_conf = _find_open_conf(t.get("direction"), t.get("open_time"))
            else:
                open_conf = None

            # Load counterfactual
            cf = None
            report_path = os.path.join(reports_dir, f"{ticket}.json")
            if os.path.exists(report_path):
                try:
                    with open(report_path, "r") as f:
                        cf = json.load(f).get("counterfactual")
                except Exception:
                    pass

            # Verdict — skip if no valid SL data (orig_sl=0 or None = missing data)
            verdict = None
            verdict_type = None  # "helped" | "hurt" | "neutral"
            direction_up = (t.get("direction") or "").upper()
            if cf and orig_sl is not None and float(orig_sl) > 0:
                if cf.get("original_sl_survived") is False and entry > 0:
                    orig_sl_f = float(orig_sl)
                    if direction_up == "BUY":
                        pnl_if_original = orig_sl_f - float(entry)
                    else:
                        pnl_if_original = float(entry) - orig_sl_f
                    diff = round(pnl - pnl_if_original, 2)
                    if diff > 0:
                        verdict = f"SAVED ${diff:.2f}"
                        verdict_type = "helped"
                        adj_helped += 1
                    elif diff < 0:
                        verdict = f"COST ${abs(diff):.2f}"
                        verdict_type = "hurt"
                        adj_hurt += 1
                    else:
                        verdict = "NEUTRAL"
                        verdict_type = "neutral"
                        adj_neutral += 1
                elif cf.get("tp_would_have_been_hit") and cf.get("tp_hit_pnl") is not None:
                    cost = round(float(cf["tp_hit_pnl"]) - pnl, 2)
                    if cost > 0:
                        verdict = f"COST ${cost:.2f}"
                        verdict_type = "hurt"
                        adj_hurt += 1
                    else:
                        verdict = "NEUTRAL"
                        verdict_type = "neutral"
                        adj_neutral += 1
                elif cf.get("original_sl_survived") is True:
                    verdict = "NEUTRAL"
                    verdict_type = "neutral"
                    adj_neutral += 1

            # Duration
            dur = None
            try:
                od = datetime.fromisoformat((t.get("open_time") or "").rstrip("Z").split(".")[0])  # FLO-286
                cd = datetime.fromisoformat((t.get("close_time") or "").rstrip("Z").split(".")[0])  # FLO-286
                dur = round((cd - od).total_seconds() / 60)
            except Exception:
                pass

            trades.append({
                "ticket": ticket,
                "direction": t.get("direction"),
                "session_open": _session(t.get("open_time")),
                "session_close": _session(t.get("close_time")),
                "pnl": pnl,
                "entry": entry,
                "orig_sl": orig_sl,
                "final_sl": t.get("final_sl"),
                "tp": t.get("tp"),
                "close_price": t.get("close_price"),
                "close_reason": t.get("close_reason"),
                "mfe": mfe,
                "mae": mae,
                "capture_pct": capture,          # raw numeric (kept for analysis)
                "capture_display": capture_display,  # FLO-300: clamped/LOSS for UI
                "entry_type": entry_type,        # FLO-301: "MARKET" | "PENDING"
                "open_conf": open_conf,          # FLO-300/301: market orders only; None for pending
                "pnl_pips": round(_pp, 1) if _pp is not None else None,
                "adj_count": len(adjustments),
                "adjustments": adjustments,
                "verdict": verdict,
                "verdict_type": verdict_type,
                "duration_min": dur,
                "counterfactual": cf,
            })

        total_adj = adj_helped + adj_hurt + adj_neutral

        # FLO-300/301: win rate by opening-confidence band — MARKET ORDERS ONLY.
        # Pending-order fills happen hours after the decision in potentially
        # different market conditions, so their "open confidence" isn't
        # comparable with market-order confidence. Bands: <50%, 50–65%, 65%+.
        bands = {
            "low":  {"min": 0,  "max": 50,  "trades": 0, "wins": 0},
            "mid":  {"min": 50, "max": 65,  "trades": 0, "wins": 0},
            "high": {"min": 65, "max": 101, "trades": 0, "wins": 0},
        }
        for t in trades:
            if t.get("entry_type") != "MARKET":
                continue   # FLO-301: exclude pending orders from band stats
            c = t.get("open_conf")
            if c is None:
                continue
            for b in bands.values():
                if b["min"] <= c < b["max"]:
                    b["trades"] += 1
                    if (t.get("pnl") or 0) > 0:
                        b["wins"] += 1
                    break
        def _band_dict(b):
            wr = round(b["wins"] / b["trades"] * 100) if b["trades"] else None
            return {"trades": b["trades"], "wins": b["wins"], "win_rate_pct": wr}

        # FLO-301: entry-type win-rate comparison (market vs pending).
        _et = {"MARKET": {"trades": 0, "wins": 0},
               "PENDING": {"trades": 0, "wins": 0}}
        for t in trades:
            k = t.get("entry_type")
            if k not in _et:
                continue
            _et[k]["trades"] += 1
            if (t.get("pnl") or 0) > 0:
                _et[k]["wins"] += 1
        def _et_dict(e):
            wr = round(e["wins"] / e["trades"] * 100) if e["trades"] else None
            return {"trades": e["trades"], "wins": e["wins"], "win_rate_pct": wr}

        stats = {
            "avg_capture": round(sum(total_capture) / len(total_capture), 1) if total_capture else None,
            "adj_helped_pct": round(adj_helped / total_adj * 100) if total_adj > 0 else None,
            "adj_hurt_pct": round(adj_hurt / total_adj * 100) if total_adj > 0 else None,
            "total_trades": len(trades),
            "trades_with_cf": sum(1 for t in trades if t["counterfactual"]),
            "win_rate_by_conf": {  # FLO-300 — market orders only (FLO-301)
                "low":  _band_dict(bands["low"]),
                "mid":  _band_dict(bands["mid"]),
                "high": _band_dict(bands["high"]),
            },
            "win_rate_by_entry_type": {  # FLO-301
                "market":  _et_dict(_et["MARKET"]),
                "pending": _et_dict(_et["PENDING"]),
            },
        }

        return JSONResponse({"trades": trades, "stats": stats})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)})


@app.get("/api/trade-report")
def trade_report(ticket: int, force_refresh: int = 0):
    if not HISTORY_DB.exists():
        return JSONResponse({"ok": False, "error": "History DB not found"})

    try:
        conn = _get_history_conn()
        _ensure_trade_reports_table(conn)

        tr = conn.execute("SELECT * FROM trades WHERE ticket = ? LIMIT 1", (ticket,)).fetchone()
        if not tr:
            conn.close()
            return JSONResponse({"ok": False, "error": "trade_not_found"})
        trade = dict(tr)

        open_time = trade.get("open_time")
        analysis = None
        if open_time:
            a = conn.execute(
                "SELECT * FROM analyses WHERE timestamp <= ? ORDER BY timestamp DESC LIMIT 1",
                (open_time,),
            ).fetchone()
            if a:
                analysis = dict(a)

        direction = trade.get("direction")
        open_p = float(trade.get("open_price") or 0.0)
        close_p = float(trade.get("close_price") or 0.0)
        sl = float(trade.get("sl") or 0.0)
        tp = float(trade.get("tp") or 0.0)
        profit = float(trade.get("profit") or 0.0)

        pips = _calc_pips(direction, open_p, close_p)
        rr = _calc_rr(direction, open_p, sl, tp)

        payload = {
            "trade": {
                "ticket": trade.get("ticket"),
                "direction": direction,
                "volume": trade.get("volume"),
                "open_price": trade.get("open_price"),
                "close_price": trade.get("close_price"),
                "sl": trade.get("sl"),
                "tp": trade.get("tp"),
                "profit": trade.get("profit"),
                "pips": round(pips, 1),
                "close_reason": trade.get("close_reason"),
                "open_time": trade.get("open_time"),
                "close_time": trade.get("close_time"),
                "rr": rr,
                "comment": trade.get("comment"),
            },
            "snapshot": {
                "analysis_timestamp": (analysis or {}).get("timestamp"),
                "decision": (analysis or {}).get("decision"),
                "final_score": (analysis or {}).get("final_score"),
                "confidence": (analysis or {}).get("confidence"),
                "confidence_level": (analysis or {}).get("confidence_level"),
                "tech_score": (analysis or {}).get("tech_score"),
                "ml_score": (analysis or {}).get("ml_score"),
                "momentum_score": (analysis or {}).get("momentum_score"),
                "news_score": (analysis or {}).get("news_score"),
                "calendar_score": (analysis or {}).get("calendar_score"),
                "volatility_status": (analysis or {}).get("volatility_status"),
                "scenario": (analysis or {}).get("scenario"),
                "scenario_description": (analysis or {}).get("scenario_description"),
                "gpt_action": (analysis or {}).get("gpt_action"),
                "gpt_adjustment": (analysis or {}).get("gpt_adjustment"),
            },
        }

        input_hash = _sha256_text(json.dumps(payload, sort_keys=True, ensure_ascii=False))

        cached_row = conn.execute(
            "SELECT created_at, model, input_hash, report_json FROM trade_reports WHERE ticket = ? LIMIT 1",
            (ticket,),
        ).fetchone()

        if cached_row and not force_refresh:
            cached = dict(cached_row)
            if cached.get("input_hash") == input_hash and cached.get("report_json"):
                conn.close()
                try:
                    report_obj = json.loads(cached.get("report_json"))
                except Exception:
                    report_obj = None
                return JSONResponse(
                    {
                        "ok": True,
                        "ticket": ticket,
                        "cached": True,
                        "created_at": cached.get("created_at"),
                        "model": cached.get("model"),
                        "report": report_obj,
                    }
                )

        result = _call_gpt_trade_report(payload)
        if not result.get("ok"):
            conn.close()
            return JSONResponse({"ok": False, "error": result.get("error", "gpt_failed")})

        # FLO-286: UTC for trade_reports.created_at
        from tz_utils import utc_iso
        created_at = utc_iso()
        model = result.get("model")
        report_json = json.dumps(result.get("report"), ensure_ascii=False)
        conn.execute(
            "INSERT OR REPLACE INTO trade_reports (ticket, created_at, model, input_hash, report_json) VALUES (?, ?, ?, ?, ?)",
            (ticket, created_at, model, input_hash, report_json),
        )
        conn.commit()
        conn.close()

        return JSONResponse(
            {
                "ok": True,
                "ticket": ticket,
                "cached": False,
                "created_at": created_at,
                "model": model,
                "report": result.get("report"),
            }
        )
    except Exception as e:
        try:
            conn.close()
        except Exception:
            pass
        return JSONResponse({"ok": False, "error": _redact_secrets(str(e))})
