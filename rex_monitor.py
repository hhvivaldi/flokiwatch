"""
REX MONITOR — Proactive Market Scanning (FLO-211)

Runs independently every 30 min. Calls Rex's 4 unique tools (no LLM),
classifies findings deterministically, writes to data/rex_monitor.json.
Floki pulls via get_rex_monitor tool. Same pull model as Luna's brief.

No reflexion search in v1 (deferred — latency + uncertain quality).
"""

import json
import os
import time
from datetime import datetime, timezone
from tz_utils import utc_iso  # FLO-309
from pathlib import Path
from typing import Any, Dict, List, Optional

from logger import log

DATA_DIR = Path(__file__).parent / "data"
MONITOR_FILE = DATA_DIR / "rex_monitor.json"


# ---------------------------------------------------------------------------
# Tool Runners — call AgentTools methods, handle failures independently
# ---------------------------------------------------------------------------

def _run_divergence_scan(agent_tools: Any) -> Dict[str, Any]:
    try:
        return agent_tools.rex_divergence_scan()
    except Exception as e:
        log.warning(f"REX_MONITOR | divergence_scan failed: {e}")
        return {"success": False, "reason": str(e)}


def _run_correlation_check(agent_tools: Any) -> Dict[str, Any]:
    try:
        return agent_tools.rex_correlation_check()
    except Exception as e:
        log.warning(f"REX_MONITOR | correlation_check failed: {e}")
        return {"success": False, "reason": str(e)}


def _run_regime_history(agent_tools: Any) -> Dict[str, Any]:
    try:
        return agent_tools.rex_regime_history()
    except Exception as e:
        log.warning(f"REX_MONITOR | regime_history failed: {e}")
        return {"success": False, "reason": str(e)}


def _run_session_performance(agent_tools: Any) -> Dict[str, Any]:
    try:
        return agent_tools.rex_session_performance()
    except Exception as e:
        log.warning(f"REX_MONITOR | session_performance failed: {e}")
        return {"success": False, "reason": str(e)}


# ---------------------------------------------------------------------------
# Current Session Detection
# ---------------------------------------------------------------------------

def _current_session() -> str:
    """Return current trading session name based on UTC hour."""
    hour = datetime.now(timezone.utc).hour
    if 22 <= hour or hour < 7:
        return "asian"
    elif 7 <= hour < 13:
        return "london"
    return "ny"


# ---------------------------------------------------------------------------
# Deterministic Finding Classifier
# ---------------------------------------------------------------------------

def _classify_findings(
    divergences: Dict[str, Any],
    correlations: Dict[str, Any],
    regime: Dict[str, Any],
    performance: Dict[str, Any],
    prev_regime: Optional[str],
) -> List[Dict[str, Any]]:
    """Classify tool outputs into structured findings. No LLM — pure rules."""
    findings: List[Dict[str, Any]] = []

    # --- Divergence findings (HIGH) ---
    if divergences.get("success"):
        for tf_name in ("H4", "D1"):
            tf_data = divergences.get("divergences", {}).get(tf_name)
            if not isinstance(tf_data, dict):
                continue

            rsi_div = tf_data.get("rsi", "none")
            rsi_val = tf_data.get("rsi_value")
            macd_div = tf_data.get("macd_divergence", "none")

            if rsi_div not in ("none", "insufficient_data"):
                findings.append({
                    "type": "DIVERGENCE",
                    "severity": "HIGH",
                    "timeframe": tf_name,
                    "detail": (
                        f"{tf_name} RSI {rsi_div} divergence"
                        + (f" (RSI={rsi_val:.1f})" if rsi_val is not None else "")
                    ),
                    "implication": "bearish" if "bearish" in rsi_div else "bullish",
                    "source": "rex_divergence_scan",
                })

            if macd_div not in ("none", "insufficient_data"):
                findings.append({
                    "type": "DIVERGENCE",
                    "severity": "HIGH",
                    "timeframe": tf_name,
                    "detail": f"{tf_name} MACD {macd_div} divergence",
                    "implication": "bearish" if "bearish" in macd_div else "bullish",
                    "source": "rex_divergence_scan",
                })

    # --- Correlation findings (HIGH if BROKEN, LOW if WEAK) ---
    if correlations.get("success"):
        pair_labels = {
            "gold_dxy": "Gold-DXY",
            "gold_silver": "Gold-Silver",
            "gold_10y": "Gold-10Y",
        }
        for pair, label in pair_labels.items():
            pair_data = correlations.get("correlations", {}).get(pair)
            if not isinstance(pair_data, dict):
                continue
            status = pair_data.get("status")
            corr_val = pair_data.get("correlation")
            normal = pair_data.get("normal")
            if status == "BROKEN" and corr_val is not None:
                findings.append({
                    "type": "CORRELATION_BREAK",
                    "severity": "HIGH",
                    "detail": f"{label} correlation {corr_val:+.2f} (normal: {normal}) — BROKEN",
                    "implication": "unusual_driver",
                    "source": "rex_correlation_check",
                })
            elif status == "WEAK" and corr_val is not None:
                findings.append({
                    "type": "CORRELATION_WEAK",
                    "severity": "LOW",
                    "detail": f"{label} correlation {corr_val:+.2f} (normal: {normal}) — weakening",
                    "implication": "monitor",
                    "source": "rex_correlation_check",
                })

    # --- Regime change findings (graduated severity) ---
    if regime.get("success"):
        current = regime.get("current_regime")
        duration = regime.get("duration_minutes")
        first_scan_after_change = prev_regime is not None and prev_regime != current

        if duration is not None and current:
            if first_scan_after_change or (duration is not None and duration < 30):
                findings.append({
                    "type": "REGIME_CHANGE",
                    "severity": "HIGH",
                    "detail": f"Regime changed to {current} {duration}m ago",
                    "implication": "regime_transition",
                    "source": "rex_regime_history",
                })
            elif duration < 120:
                findings.append({
                    "type": "REGIME_CHANGE",
                    "severity": "MEDIUM",
                    "detail": f"Regime changed to {current} {duration}m ago",
                    "implication": "regime_transition",
                    "source": "rex_regime_history",
                })
            # > 120 min: omit finding

    # --- Session performance findings ---
    if performance.get("success"):
        current_sess = _current_session()
        sess_data = performance.get("performance", {}).get(current_sess, {})
        for direction, stats in sess_data.items():
            if not isinstance(stats, dict):
                continue
            n = stats.get("n", 0)
            wr = stats.get("wr", 50)
            if n >= 5 and wr < 25:
                findings.append({
                    "type": "SESSION_WARNING",
                    "severity": "MEDIUM",
                    "detail": f"{current_sess.capitalize()} {direction}: {wr:.0f}% WR over {n} trades",
                    "implication": f"avoid_{direction.lower()}_in_{current_sess}",
                    "source": "rex_session_performance",
                })
            elif n >= 5 and wr > 75:
                findings.append({
                    "type": "SESSION_HOT",
                    "severity": "LOW",
                    "detail": f"{current_sess.capitalize()} {direction}: {wr:.0f}% WR over {n} trades",
                    "implication": f"{direction.lower()}_favored_in_{current_sess}",
                    "source": "rex_session_performance",
                })

    return findings


def _classify_alert_level(findings: List[Dict[str, Any]]) -> str:
    """Classify alert level from findings list."""
    high_sources = set()
    for f in findings:
        if f.get("severity") == "HIGH":
            high_sources.add(f.get("source", ""))

    if len(high_sources) >= 2:
        return "CRITICAL"
    if high_sources:
        return "ELEVATED"
    if findings:
        return "NORMAL"
    return "QUIET"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _load_previous() -> Dict[str, Any]:
    """Load previous rex_monitor.json for regime comparison and debounce."""
    try:
        if MONITOR_FILE.exists():
            data = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_monitor(payload: Dict[str, Any]) -> None:
    """Atomic write to rex_monitor.json."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(MONITOR_FILE) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, str(MONITOR_FILE))
    except Exception as e:
        log.warning(f"REX_MONITOR | save failed: {e}")


def load_rex_monitor() -> Optional[Dict[str, Any]]:
    """Load latest Rex monitor scan. Returns None if unavailable or stale (>30 min)."""
    try:
        if not MONITOR_FILE.exists():
            return None
        data = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        ts = data.get("timestamp")
        if ts:
            try:
                scan_time = datetime.fromisoformat(ts)
                if scan_time.tzinfo is None:
                    scan_time = scan_time.replace(tzinfo=timezone.utc)
                age = datetime.now(timezone.utc) - scan_time
                if age.total_seconds() > 1800:
                    return None
            except (ValueError, TypeError):
                pass
        return data
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def run_rex_monitor(agent_tools: Any) -> Dict[str, Any]:
    """
    Run Rex proactive monitor scan. Calls 4 tools, classifies findings,
    writes to rex_monitor.json. Returns the scan result dict.

    No LLM — deterministic classification only.
    """
    t0 = time.time()
    log.info("REX_MONITOR | starting scan...")

    # Load previous scan for regime comparison
    prev = _load_previous()
    prev_regime = None
    try:
        prev_regime = (prev.get("raw_data") or {}).get("regime", {}).get("current")
    except Exception:
        pass

    # Run all 4 tools (independent — each wrapped in try/except)
    divergences = _run_divergence_scan(agent_tools)
    correlations = _run_correlation_check(agent_tools)
    regime = _run_regime_history(agent_tools)
    performance = _run_session_performance(agent_tools)

    # Classify findings
    findings = _classify_findings(divergences, correlations, regime, performance, prev_regime)
    alert_level = _classify_alert_level(findings)

    scan_ms = int((time.time() - t0) * 1000)
    now_iso = utc_iso()  # FLO-309

    # Build raw data snapshot
    raw_data = {
        "divergences": divergences.get("divergences") if divergences.get("success") else {"error": divergences.get("reason")},
        "correlations": correlations.get("correlations") if correlations.get("success") else {"error": correlations.get("reason")},
        "regime": {
            "current": regime.get("current_regime"),
            "duration_min": regime.get("duration_minutes"),
            "transitions": regime.get("recent_transitions", []),
        } if regime.get("success") else {"error": regime.get("reason")},
        "session_performance": performance.get("performance") if performance.get("success") else {"error": performance.get("reason")},
    }

    # Carry forward debounce timestamp
    last_critical_wake_at = prev.get("last_critical_wake_at")

    payload = {
        "timestamp": now_iso,
        "scan_latency_ms": scan_ms,
        "findings": findings,
        "finding_count": len(findings),
        "alert_level": alert_level,
        "raw_data": raw_data,
        "last_critical_wake_at": last_critical_wake_at,
    }

    _save_monitor(payload)

    high_count = sum(1 for f in findings if f.get("severity") == "HIGH")
    log.info(
        f"REX_MONITOR | scan complete | {scan_ms}ms | "
        f"findings={len(findings)} (HIGH={high_count}) | alert={alert_level}"
    )

    return payload
