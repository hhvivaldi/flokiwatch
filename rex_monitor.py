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
    """Classify tool outputs into structured findings. No LLM — pure rules.

    FLO-316 (Bug G-style follow-up to reduce Floki's compounding caution):
    emit observational findings only. Each finding is {type, observation, data}.
    Removed prior prescriptive fields:
      * severity (HIGH/MEDIUM/LOW) — Floki was treating any HIGH as
        "extra caution"; the aggregate across sources pushed his conviction
        bar upward. Now absent; Floki reads raw numbers.
      * implication (bullish / bearish / avoid_X / regime_transition) —
        prescriptive interpretation Floki parroted into reasoning.
      * source tag — simplified; tool-trace retains provenance if needed.
      * detail formatted with "BROKEN" / "weakening" sentiment words —
        replaced with neutral numeric phrasing.
    """
    findings: List[Dict[str, Any]] = []

    # --- Divergences (H4/D1 RSI + MACD) ---
    if divergences.get("success"):
        for tf_name in ("H4", "D1"):
            tf_data = divergences.get("divergences", {}).get(tf_name)
            if not isinstance(tf_data, dict):
                continue
            rsi_div = tf_data.get("rsi", "none")
            rsi_val = tf_data.get("rsi_value")
            macd_div = tf_data.get("macd_divergence", "none")

            if rsi_div not in ("none", "insufficient_data"):
                obs = f"{tf_name} RSI {rsi_div} divergence"
                if rsi_val is not None:
                    obs += f" (RSI={rsi_val:.1f})"
                findings.append({
                    "type": "DIVERGENCE",
                    "observation": obs,
                    "data": {"timeframe": tf_name, "indicator": "rsi",
                             "pattern": rsi_div, "rsi_value": rsi_val},
                })

            if macd_div not in ("none", "insufficient_data"):
                findings.append({
                    "type": "DIVERGENCE",
                    "observation": f"{tf_name} MACD {macd_div} divergence",
                    "data": {"timeframe": tf_name, "indicator": "macd",
                             "pattern": macd_div},
                })

    # --- Correlations (surface only when deviating from typical range) ---
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
            corr_val = pair_data.get("correlation")
            normal = pair_data.get("normal")
            # Gate kept: only surface correlations that DEVIATE from typical
            # (was BROKEN/WEAK in prior classifier). Normal/strong-normal
            # correlations are not news. The gate uses status internally
            # for efficiency; but the finding no longer carries the label.
            status = pair_data.get("status")
            if corr_val is None or status not in ("BROKEN", "WEAK"):
                continue
            typical_min = typical_max = None
            if isinstance(normal, (list, tuple)) and len(normal) == 2:
                typical_min, typical_max = normal[0], normal[1]
            if typical_min is not None:
                typical_txt = f"typical range {typical_min:+.2f} to {typical_max:+.2f}"
            elif isinstance(normal, (int, float)):
                typical_txt = f"typical {normal:+.2f}"
            else:
                typical_txt = ""
            obs = f"{label} correlation current {corr_val:+.2f}" + (
                f", {typical_txt}" if typical_txt else ""
            )
            findings.append({
                "type": "CORRELATION",
                "observation": obs,
                "data": {
                    "pair": pair,
                    "current": corr_val,
                    "typical_min": typical_min,
                    "typical_max": typical_max,
                    "typical_scalar": normal if isinstance(normal, (int, float)) else None,
                },
            })

    # --- Regime transitions (surface when recent; omit settled regimes) ---
    if regime.get("success"):
        current = regime.get("current_regime")
        duration = regime.get("duration_minutes")
        first_scan_after_change = prev_regime is not None and prev_regime != current

        if duration is not None and current:
            if first_scan_after_change or duration < 120:
                prev_txt = f" from {prev_regime}" if prev_regime and prev_regime != current else ""
                obs = f"Regime changed to {current}{prev_txt}, {duration} minutes ago"
                findings.append({
                    "type": "REGIME",
                    "observation": obs,
                    "data": {
                        "current": current,
                        "previous": prev_regime,
                        "age_minutes": duration,
                        "first_scan_after_change": first_scan_after_change,
                    },
                })

    # --- Session performance ---
    if performance.get("success"):
        current_sess = _current_session()
        sess_data = performance.get("performance", {}).get(current_sess, {})
        for direction, stats in sess_data.items():
            if not isinstance(stats, dict):
                continue
            n = stats.get("n", 0)
            wr = stats.get("wr", 50)
            pnl = float(stats.get("pnl", 0.0) or 0.0)
            if n < 5:
                continue
            # Surface extreme WR (either tail) or net-positive edge with
            # meaningful WR. Floki decides how to weight it — no SESSION_HOT
            # / SESSION_WARNING / SESSION_ENDORSEMENT prescriptive labels.
            if wr < 25 or wr > 75 or (wr > 45 and pnl > 0):
                obs = (
                    f"{current_sess.capitalize()} {direction}: {wr:.1f}% win rate "
                    f"over {n} trades, net {pnl:+.2f}"
                )
                findings.append({
                    "type": "SESSION",
                    "observation": obs,
                    "data": {
                        "session": current_sess,
                        "direction": direction,
                        "win_rate_pct": wr,
                        "n_trades": n,
                        "pnl": pnl,
                    },
                })

    return findings


def _classify_alert_level(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """FLO-316: gutted to no-op.

    Previously emitted prescriptive alert_level (QUIET/NORMAL/ELEVATED/CRITICAL)
    + alert_context + alert_hint labels that Floki parroted into reasoning
    as blanket "extra caution" regardless of the hint's actual content. The
    alert_hint "This is NOT a do-not-trade signal" was observed being
    ignored — Floki treated any CRITICAL as do-not-trade anyway.

    Retained as callable stub so any out-of-tree caller doesn't AttributeError.
    Callers in-tree (run_rex_monitor) no longer read the return dict.
    """
    return {"level": "", "context": None, "hint": None}


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
    """Load latest Rex monitor scan. Returns None if unavailable or stale
    (>60 min — 2× the 30-min scan interval, FLO-313).

    Rationale: the scheduler runs scans every REX_MONITOR_INTERVAL (1800s).
    When a scan runs even 1s late, a threshold equal to the interval opens
    a window where the previous scan is already "stale" and the new one
    hasn't been written yet. 2× interval gives a cushion for jittery scan
    timing without masking a truly dead backend (a 60+ min gap is real).
    """
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
                if age.total_seconds() > 3600:  # FLO-313: was 1800
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

    # Classify findings (FLO-316: observational-only {type, observation, data})
    findings = _classify_findings(divergences, correlations, regime, performance, prev_regime)

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

    # Carry forward debounce timestamp (Simba wake — now gated on findings_count
    # threshold instead of alert_level == CRITICAL; see agent_monitor.py).
    last_critical_wake_at = prev.get("last_critical_wake_at")

    # FLO-316: alert_level / alert_context / alert_hint REMOVED from payload.
    # Floki reads findings[].observation + findings_count, decides himself.
    payload = {
        "timestamp": now_iso,
        "scan_latency_ms": scan_ms,
        "findings": findings,
        "findings_count": len(findings),  # renamed from finding_count per FLO-316 spec
        "finding_count": len(findings),   # preserved for any inflight consumer
        "raw_data": raw_data,
        "last_critical_wake_at": last_critical_wake_at,
    }

    _save_monitor(payload)

    log.info(
        f"REX_MONITOR | scan complete | {scan_ms}ms | "
        f"findings={len(findings)}"
    )

    return payload
