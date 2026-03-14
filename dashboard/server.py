import json
import os
import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

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
OFFLINE_AFTER_SECONDS = int(os.environ.get("DASHBOARD_OFFLINE_AFTER_SECONDS", "60"))


app = FastAPI(title="XAUUSD Bot Dashboard", version="0.1")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/image", StaticFiles(directory=str(APP_DIR / "image")), name="image")


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
    now = datetime.now().isoformat()
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


@app.get("/")
def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/about")
def about():
    return FileResponse(str(STATIC_DIR / "about.html"))


@app.get("/roadmap")
def roadmap():
    return FileResponse(str(STATIC_DIR / "roadmap.html"))


@app.get("/history")
def history():
    return FileResponse(str(STATIC_DIR / "history.html"))


@app.get("/trade-room")
def trade_room():
    return FileResponse(str(STATIC_DIR / "trade_room.html"))


@app.get("/api/health")
def health():
    if not STATE_FILE.exists():
        return JSONResponse({"ok": False, "file_age_seconds": None, "state_file": str(STATE_FILE)})
    age = _file_age_seconds(STATE_FILE)
    return JSONResponse({"ok": age <= OFFLINE_AFTER_SECONDS, "file_age_seconds": round(age, 2), "state_file": str(STATE_FILE)})


@app.get("/api/state")
def state():
    if not STATE_FILE.exists():
        return JSONResponse(_offline_state("missing_state_file", file_age_seconds=10**9))

    age = _file_age_seconds(STATE_FILE)

    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return JSONResponse(_offline_state("invalid_json", file_age_seconds=age))

    # Always include file age meta for UI (LAST DATA)
    payload.setdefault("_meta", {})
    payload["_meta"].update({"file_age_seconds": round(age, 2)})

    expected_interval = payload.get("_expected_update_interval_seconds") or 60
    offline_threshold = expected_interval + 60

    if age > offline_threshold:
        payload.setdefault("bot", {})
        payload["bot"]["status"] = "OFFLINE"
        payload["bot"]["running"] = False
        payload["_meta"].update({"reason": "stale_state_file"})
    else:
        payload["_meta"].setdefault("reason", "ok")

    return JSONResponse(payload)


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
        query = """
            SELECT t.*, 
                   (SELECT confidence FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as confidence,
                   (SELECT scenario FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as scenario,
                   (SELECT scenario_description FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as scenario_description
            FROM trades t 
            WHERE t.close_time IS NOT NULL 
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
        
        peak_equity = 0.0
        current_equity = 0.0
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
                "time": trade.get("close_time"),
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
                    fmt = "%Y-%m-%dT%H:%M:%S"
                    # Handle possible fractional seconds in open_time
                    ot = datetime.fromisoformat(open_time_str)
                    ct = datetime.fromisoformat(close_time_str)
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

        cutoff_date = "2026-02-16"
        live_trades = [t for t in trades if (t.get("open_time") or "") >= cutoff_date]
        live_stats = _calc_stats(live_trades)
        live_stats["max_drawdown"] = round(_calc_max_drawdown(live_trades), 2)

        return JSONResponse({
            "global_stats": global_stats,
            "live_stats": live_stats,
            "monthly_stats": monthly_stats,
            "equity_curve": equity_curve,
            "trades": trades
        })
        
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

        created_at = datetime.now().isoformat()
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
