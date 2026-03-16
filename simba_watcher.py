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
        self.model = (model or os.environ.get("SIMBA_MODEL", "gpt-4o-mini")).strip() or "gpt-4o-mini"
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
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not api_key:
                return self._wake_fallback(start, "OPENAI_API_KEY not set")

            try:
                from openai import OpenAI

                client = OpenAI(api_key=api_key)
            except Exception as e:
                return self._wake_fallback(start, f"openai_client_unavailable: {e}")

            prompt = self._build_prompt(scanner_data, wake_conditions)

            try:
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": self._system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                    timeout=self.timeout_seconds,
                )
            except Exception as e:
                return self._wake_fallback(start, f"openai_request_failed: {e}")

            content = None
            try:
                content = resp.choices[0].message.content
            except Exception:
                content = None

            if not content:
                return self._wake_fallback(start, "empty_response")

            try:
                parsed = json.loads(content)
            except Exception as e:
                return self._wake_fallback(start, f"invalid_json: {e}")

            normalized = self._normalize_result(parsed)
            if normalized is None:
                return self._wake_fallback(start, "invalid_schema")

            latency_ms = int((time.time() - start) * 1000)
            normalized["model"] = self.model
            normalized["latency_ms"] = latency_ms
            return normalized
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
            "model": self.model,
            "latency_ms": latency_ms,
        }

    def _system_prompt(self) -> str:
        return (
            "You are Simba, a low-cost checklist verifier for a trading system. "
            "You do NOT analyze markets, do NOT give trade advice, and do NOT invent data. "
            "You ONLY verify whether ANY provided wake condition is met given the scanner data.\n\n"
            "Wake-condition types are typed and structured. Evaluate each one strictly using the provided fields.\n\n"
            "Return ONLY strict JSON with keys: decision, triggered, checked_count, met_count, summary.\n"
            "decision must be either SLEEP or WAKE.\n"
            "triggered must be an array of condition ids that are met (can be empty).\n"
            "checked_count and met_count must be integers.\n"
            "summary must be a short human-readable string explaining why you chose SLEEP/WAKE."
        )

    def _build_prompt(self, scanner_data: Dict[str, Any], wake_conditions: Dict[str, Any]) -> str:
        scanner_json = json.dumps(scanner_data or {}, ensure_ascii=False, default=str)
        wc_json = json.dumps(wake_conditions or {}, ensure_ascii=False, default=str)
        return (
            "Evaluate wake conditions against scanner data. If ANY condition is met, decision=WAKE. "
            "If none are met, decision=SLEEP.\n\n"
            "SCANNER_DATA (JSON):\n"
            f"{scanner_json}\n\n"
            "WAKE_CONDITIONS (JSON):\n"
            f"{wc_json}"
        )

    def _normalize_result(self, data: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(data, dict):
            return None

        decision = str(data.get("decision") or "").strip().upper()
        if decision not in ("SLEEP", "WAKE"):
            return None

        triggered_raw = data.get("triggered")
        triggered: List[str] = []
        if isinstance(triggered_raw, list):
            for x in triggered_raw:
                s = str(x or "").strip()
                if s:
                    triggered.append(s)
        triggered = triggered[:10]

        checked_count = data.get("checked_count")
        met_count = data.get("met_count")
        try:
            checked_i = int(checked_count)
        except Exception:
            checked_i = 0
        try:
            met_i = int(met_count)
        except Exception:
            met_i = 0

        summary = data.get("summary")
        summary_s = str(summary or "").strip()
        if not summary_s:
            summary_s = "condition check complete"

        return {
            "decision": decision,
            "triggered": triggered,
            "checked_count": max(0, checked_i),
            "met_count": max(0, met_i),
            "summary": summary_s[:240],
        }
