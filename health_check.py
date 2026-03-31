"""
FLO-155: Automated health check — runs every 60 minutes.
Purely observational: logs WARNING/ALERT, never affects trading.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Tuple

from logger import log


_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR = os.path.join(_BASE_DIR, "data")


def _read_json(filename: str) -> dict:
    path = os.path.join(_DATA_DIR, filename)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _age_minutes(iso_ts: str) -> float:
    """Return age in minutes of an ISO timestamp. Returns 9999 on failure."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
    except Exception:
        return 9999.0


def _file_age_minutes(filename: str) -> float:
    path = os.path.join(_DATA_DIR, filename)
    try:
        mtime = os.path.getmtime(path)
        return (time.time() - mtime) / 60.0
    except Exception:
        return 9999.0


def _count_log_pattern(pattern: str, minutes: int = 60) -> int:
    """Count occurrences of pattern in today's log within the last N minutes."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(_BASE_DIR, "logs", f"trading_bot_{today}.log")
        if not os.path.exists(log_path):
            return 0
        cutoff = datetime.now() - timedelta(minutes=minutes)
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")
        count = 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if len(line) >= 19 and line[:16] >= cutoff_str and pattern in line:
                    count += 1
        return count
    except Exception:
        return 0


def run_health_check() -> Dict:
    """Run all 12 health checks. Returns summary dict."""
    results: List[Tuple[str, str, str]] = []  # (level, name, detail)

    # ── 1. Luna brief freshness ──
    luna = _read_json("luna_brief.json")
    luna_ts = luna.get("timestamp", "")
    luna_age = _age_minutes(luna_ts) if luna_ts else _file_age_minutes("luna_brief.json")
    if luna_age > 30:
        results.append(("WARNING", "Luna brief stale", f"{luna_age:.0f}m old"))
    else:
        results.append(("OK", "Luna brief fresh", f"{luna_age:.0f}m"))

    # ── 2. Floki last call ──
    nc = _read_json("agent_next_check.json")
    floki_age = 9999.0
    try:
        nc_ts = nc.get("next_check_at", "")
        if nc_ts:
            # next_check_at is in the future; use file mtime instead
            floki_age = _file_age_minutes("agent_next_check.json")
    except Exception:
        pass
    if floki_age > 60:
        results.append(("WARNING", "Floki not called", f"last activity {floki_age:.0f}m ago"))
    else:
        results.append(("OK", "Floki active", f"{floki_age:.0f}m ago"))

    # ── 3. Echo last scan ──
    echo_age = _file_age_minutes("echo_seen_hashes.json")
    if echo_age > 15:
        results.append(("WARNING", "Echo scan stale", f"{echo_age:.0f}m old"))
    else:
        results.append(("OK", "Echo scanning", f"{echo_age:.0f}m ago"))

    # ── 4. Sage report age ──
    sage = _read_json("sage_report.json")
    sage_ts = sage.get("report_date", "")
    if sage_ts:
        try:
            sage_dt = datetime.fromisoformat(sage_ts)
            sage_age_h = (datetime.now() - sage_dt).total_seconds() / 3600
            if sage_age_h > 48:
                results.append(("WARNING", "Sage report stale", f"{sage_age_h:.0f}h old"))
            else:
                results.append(("OK", "Sage report current", f"{sage_age_h:.0f}h"))
        except Exception:
            results.append(("WARNING", "Sage report date unparseable", sage_ts))
    else:
        results.append(("WARNING", "Sage report no date", ""))

    # ── 5. Brain in CONFLUENCE fallback ──
    confluence_count = _count_log_pattern("CONFLUENCE", minutes=10)
    brain_count = _count_log_pattern("CENTRAL SCANNER", minutes=10)
    if confluence_count > 0 and brain_count == 0:
        results.append(("ALERT", "Brain in CONFLUENCE fallback", f"{confluence_count} confluence lines, 0 brain"))
    else:
        results.append(("OK", "Brain running", f"CENTRAL SCANNER mode"))

    # ── 6. Sage session_memory contamination ──
    mem = _read_json("agent_session_memory.json")
    sage_notes = [n for n in mem.get("notes", []) if isinstance(n, dict) and str(n.get("source", "")).lower() == "sage"]
    contaminated = False
    for sn in sage_notes:
        text = str(sn.get("note", "")).lower()
        if "population" in text or "reconciled" in text or "ea-test" in text:
            contaminated = True
            break
    if contaminated:
        results.append(("ALERT", "Sage session_memory contaminated", "Contains Population A data"))
    elif sage_notes:
        results.append(("OK", "Sage insights in memory", f"{len(sage_notes)} note(s)"))
    else:
        results.append(("OK", "Sage insights clean", "no sage notes"))

    # ── 7. Regime vs price mismatch ──
    state = _read_json("bot_state.json")
    regime = (state.get("market_regime") or {}).get("regime", "")
    try:
        indicators = (state.get("last_analysis") or {}).get("indicators", {})
        atr = float(indicators.get("atr_14") or 30)
        # Check price move in last hour from log (approximate)
        price_now = float(state.get("last_known_price") or 0)
        # Simple check: if regime is RANGING but price is far from EMA50
        ema50 = float(indicators.get("ema_50") or price_now)
        dist = abs(price_now - ema50)
        if regime == "RANGING" and dist > atr * 2:
            results.append(("WARNING", "Regime mismatch", f"RANGING but price {dist:.1f}pts from EMA50 ({dist/atr:.1f}x ATR)"))
        else:
            results.append(("OK", "Regime consistent", f"{regime}"))
    except Exception:
        results.append(("OK", "Regime check skipped", ""))

    # ── 8. Balance mismatch ──
    try:
        state_bal = float((state.get("account") or {}).get("balance", 0))
        import MetaTrader5 as mt5
        mt5.initialize()
        acct = mt5.account_info()
        mt5_bal = acct.balance if acct else 0
        mt5.shutdown()
        if abs(state_bal - mt5_bal) > 0.01:
            results.append(("WARNING", "Balance mismatch", f"state=${state_bal} MT5=${mt5_bal}"))
        else:
            results.append(("OK", "Balance matches", f"${state_bal}"))
    except Exception:
        results.append(("OK", "Balance check skipped", "MT5 unavailable"))

    # ── 9. Floki API errors ──
    api_errors = _count_log_pattern("API call failed", minutes=60)
    quota_errors = _count_log_pattern("insufficient_quota", minutes=60)
    total_errors = api_errors + quota_errors
    if total_errors > 5:
        results.append(("ALERT", "Floki API errors high", f"{total_errors} in last hour"))
    else:
        results.append(("OK", "Floki API healthy", f"{total_errors} errors"))

    # ── 10. Rex timeout rate ──
    rex_total = _count_log_pattern("debate_with_rex", minutes=60)
    rex_timeouts = _count_log_pattern("REX | API call failed", minutes=60)
    if rex_total > 0 and rex_timeouts / max(rex_total, 1) > 0.5:
        results.append(("WARNING", "Rex timeout rate high", f"{rex_timeouts}/{rex_total} timed out"))
    else:
        results.append(("OK", "Rex responsive", f"{rex_timeouts}/{max(rex_total,1)} timeouts"))

    # ── 11. Simba conditions ──
    wc = _read_json("agent_wake_conditions.json")
    conditions = wc.get("conditions", [])
    market_open = (state.get("market") or {}).get("is_open", False)
    positions = state.get("positions", [])
    if market_open and not conditions and not positions:
        wc_age = _file_age_minutes("agent_wake_conditions.json")
        if wc_age > 120:
            results.append(("WARNING", "Simba no conditions", f"{wc_age:.0f}m without watch/wake"))
        else:
            results.append(("OK", "Simba conditions cleared recently", f"{wc_age:.0f}m ago"))
    else:
        results.append(("OK", "Simba monitoring", f"{len(conditions)} conditions"))

    # ── 12. Floki call rate ──
    floki_calls = _count_log_pattern("FLOKI | model=gpt-5.4", minutes=60)
    if floki_calls > 20:
        results.append(("WARNING", "Floki call rate high", f"{floki_calls} calls/hour"))
    else:
        results.append(("OK", "Floki call rate normal", f"{floki_calls}/hour"))

    # ── Summary ──
    ok = sum(1 for r in results if r[0] == "OK")
    warn = sum(1 for r in results if r[0] == "WARNING")
    alert = sum(1 for r in results if r[0] == "ALERT")

    for level, name, detail in results:
        log.info(f"HEALTH | {level:7s} | {name} ({detail})")
    log.info(f"HEALTH_SUMMARY | {ok} OK | {warn} WARNING | {alert} ALERT")

    return {"ok": ok, "warnings": warn, "alerts": alert, "checks": results}


_last_health_check_ts: float = 0.0
HEALTH_CHECK_INTERVAL = 3600  # 1 hour


def maybe_run_health_check() -> None:
    """Run health check if interval has elapsed. Called from main loop."""
    global _last_health_check_ts
    now = time.time()
    if (now - _last_health_check_ts) < HEALTH_CHECK_INTERVAL:
        return
    _last_health_check_ts = now
    try:
        run_health_check()
    except Exception as e:
        log.debug(f"HEALTH | check failed (ignored): {e}")
