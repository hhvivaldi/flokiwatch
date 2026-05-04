"""
DEPRECATED 2026-05-04 — Dead code.

This module was an earlier Simba implementation, superseded by
agent_monitor.AgentMonitor before the FLO-403 routing changes. No
production code imports it (verified 2026-05-04 via grep across
*.py -- zero `from simba_watcher import` / `import simba_watcher`
references outside test files). The canonical Simba implementation,
also now deprecated, lives in agent_monitor.py.

Safe to delete in a future cleanup ticket. Kept for now to avoid
churning git history during the broader Simba deprecation
(FLO-419 follow-up, CEO directive 2026-05-04).
"""
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from logger import log


@dataclass
class SimbaResult:
    decision: str
    triggered: List[str]
    checked_count: int
    met_count: int
    summary: str
    model: str
    latency_ms: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "triggered": self.triggered,
            "checked_count": self.checked_count,
            "met_count": self.met_count,
            "summary": self.summary,
            "model": self.model,
            "latency_ms": self.latency_ms,
        }


class SimbaWatcher:
    def __init__(self, *, model: Optional[str] = None, timeout_seconds: int = 10):
        try:
            self.timeout_seconds = int(timeout_seconds)
        except Exception:
            self.timeout_seconds = 10

    def check_conditions(self, scanner_data: Dict[str, Any], wake_conditions: Dict[str, Any]) -> Dict[str, Any]:
        """Return a SimbaResult-like dict.

        Safety rule: MUST NEVER raise.
        Fallback rule: Any failure => WAKE (safe default).
        """
        start = time.time()

        try:
            result = self._evaluate(scanner_data or {}, wake_conditions or {})
            latency_ms = int((time.time() - start) * 1000)
            result["latency_ms"] = latency_ms
            return result
        except Exception as e:
            try:
                log.debug(f"simba_watcher: unexpected error (non-blocking): {e}")
            except Exception:
                pass
            return self._wake_fallback(start, "unexpected_error")

    def _wake_fallback(self, start: float, reason: str) -> Dict[str, Any]:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "decision": "WAKE",
            "triggered": [],
            "checked_count": 0,
            "met_count": 0,
            "summary": f"fallback — {reason}",
            "latency_ms": latency_ms,
        }

    def _evaluate(self, scanner_data: Dict[str, Any], wake_conditions: Dict[str, Any]) -> Dict[str, Any]:
        conditions = wake_conditions.get("conditions") if isinstance(wake_conditions, dict) else None
        if not isinstance(conditions, list) or not conditions:
            return {
                "decision": "SLEEP",
                "triggered": [],
                "checked_count": 0,
                "met_count": 0,
                "summary": "no conditions",
            }

        current_price = self._safe_float(scanner_data.get("current_price"))

        indicators = scanner_data.get("indicators")
        if not isinstance(indicators, dict):
            indicators = {}

        patterns = scanner_data.get("patterns")
        if not isinstance(patterns, dict):
            patterns = {}

        current_rsi = self._safe_float(
            scanner_data.get("current_rsi")
            if scanner_data.get("current_rsi") is not None
            else indicators.get("rsi")
        )
        current_adx = self._safe_float(
            scanner_data.get("current_adx")
            if scanner_data.get("current_adx") is not None
            else indicators.get("adx")
        )
        current_volume = self._safe_float(scanner_data.get("current_volume") or scanner_data.get("volume"))
        h1_volume = self._safe_float(scanner_data.get("h1_volume") or scanner_data.get("last_h1_tick_volume"))

        triggered: List[str] = []
        checked = 0
        met = 0

        for idx, c in enumerate(conditions, start=1):
            if not isinstance(c, dict):
                continue
            checked += 1
            ctype = str(c.get("type") or "").strip()
            cid = str(c.get("id") or f"c{idx}").strip() or f"c{idx}"

            is_met = False

            if ctype == "price_above":
                lvl = self._safe_float(c.get("level") if c.get("level") is not None else c.get("value"))
                if current_price is not None and lvl is not None:
                    is_met = current_price > lvl
            elif ctype == "price_below":
                lvl = self._safe_float(c.get("level") if c.get("level") is not None else c.get("value"))
                if current_price is not None and lvl is not None:
                    is_met = current_price < lvl
            elif ctype == "h1_volume_above":
                thr = self._safe_float(c.get("threshold") if c.get("threshold") is not None else c.get("value"))
                if h1_volume is not None and thr is not None:
                    is_met = h1_volume > thr
            elif ctype == "indicator_above":
                ind = str(c.get("indicator") or "").strip().lower()
                thr = self._safe_float(c.get("threshold") if c.get("threshold") is not None else c.get("value"))
                cur = self._safe_float(indicators.get(ind)) if ind else None
                if cur is not None and thr is not None:
                    is_met = cur > thr
            elif ctype == "indicator_below":
                ind = str(c.get("indicator") or "").strip().lower()
                thr = self._safe_float(c.get("threshold") if c.get("threshold") is not None else c.get("value"))
                cur = self._safe_float(indicators.get(ind)) if ind else None
                if cur is not None and thr is not None:
                    is_met = cur < thr
            elif ctype == "scanner_pattern":
                pat = str(c.get("pattern") or "").strip()
                if pat:
                    is_met = bool(patterns.get(pat))
            elif ctype == "rsi_above":
                lvl = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                if current_rsi is not None and lvl is not None:
                    is_met = current_rsi > lvl
            elif ctype == "rsi_below":
                lvl = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                if current_rsi is not None and lvl is not None:
                    is_met = current_rsi < lvl
            elif ctype == "volume_above":
                thr = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                if current_volume is not None and thr is not None:
                    is_met = current_volume > thr
            elif ctype == "adx_above":
                thr = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                if current_adx is not None and thr is not None:
                    is_met = current_adx > thr
            else:
                is_met = False

            if is_met:
                met += 1
                triggered.append(cid)

        decision = "WAKE" if triggered else "SLEEP"
        price_str = f"{current_price:.2f}" if isinstance(current_price, (int, float)) else "n/a"
        summary = (
            f"met {met}/{checked} | price={price_str}"
            if decision == "WAKE"
            else f"0/{checked} met | price={price_str}"
        )

        return {
            "decision": decision,
            "triggered": triggered[:10],
            "checked_count": max(0, int(checked)),
            "met_count": max(0, int(met)),
            "summary": str(summary)[:240],
        }

    def _safe_float(self, x: Any) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None
