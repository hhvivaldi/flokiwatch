import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

STATE_FILE = Path(os.environ.get("DASHBOARD_STATE_FILE", str(APP_DIR / ".." / "data" / "bot_state.json"))).resolve()
HISTORY_DB = Path(os.environ.get("HISTORY_DB_PATH", str(APP_DIR / ".." / "data" / "history.db"))).resolve()
OFFLINE_AFTER_SECONDS = int(os.environ.get("DASHBOARD_OFFLINE_AFTER_SECONDS", "60"))


app = FastAPI(title="XAUUSD Bot Dashboard", version="0.1")

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/image", StaticFiles(directory=str(APP_DIR / "image")), name="image")


def _file_age_seconds(path: Path) -> float:
    st = path.stat()
    return max(0.0, datetime.now().timestamp() - st.st_mtime)


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
    if not HISTORY_DB.exists():
        return JSONResponse([])
    try:
        conn = sqlite3.connect(str(HISTORY_DB), timeout=3)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT timestamp, decision, final_score FROM analyses ORDER BY id DESC LIMIT 5"
        ).fetchall()
        conn.close()
        result = [{"timestamp": r["timestamp"], "decision": r["decision"], "score": r["final_score"]} for r in rows]
        result.reverse()
        return JSONResponse(result)
    except Exception:
        return JSONResponse([])


@app.get("/api/history-data")
def history_data():
    if not HISTORY_DB.exists():
        return JSONResponse({"error": "History DB not found"})
        
    try:
        conn = sqlite3.connect(str(HISTORY_DB), timeout=3)
        conn.row_factory = sqlite3.Row
        
        # Get all closed trades and join with closest previous analysis
        query = """
            SELECT t.*, 
                   (SELECT confidence FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as confidence,
                   (SELECT scenario FROM analyses a WHERE a.timestamp <= t.open_time ORDER BY a.timestamp DESC LIMIT 1) as scenario
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
            
            if profit > 0.5:
                outcome = "win"
                global_wins += 1
                global_gross_profit += profit
            elif profit < -0.5:
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
        }

        return JSONResponse({
            "global_stats": global_stats,
            "monthly_stats": monthly_stats,
            "equity_curve": equity_curve,
            "trades": trades
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)})
