import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

from logger import log


@dataclass
class RexResult:
    agree: bool
    reasoning: str
    concerns: Any
    suggested_adjustment: str
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agree": bool(self.agree),
            "reasoning": str(self.reasoning or "").strip(),
            "concerns": self.concerns if self.concerns is not None else [],
            "suggested_adjustment": str(self.suggested_adjustment or "").strip(),
        }


def _build_prompt(floki_summary: Dict[str, Any]) -> str:
    summary_json = json.dumps(floki_summary or {}, ensure_ascii=False, default=str)
    return (
        "You are Rex, a junior Gold trader working under Floki (senior, 20 years experience). "
        "Your job is to CHALLENGE his reasoning and point out risks. You are NOT the decision maker — Floki decides. "
        "But speak up when you see something wrong. Be specific with data. If Floki makes a good rebuttal, acknowledge it. "
        "Respond with ONLY valid JSON: {agree: true/false, reasoning: '...', concerns: [...], suggested_adjustment: '...'}\n\n"
        "DATA:\n"
        f"{summary_json}"
    )


def validate_with_rex(floki_summary: Dict[str, Any], *, timeout_seconds: int = 20) -> Dict[str, Any]:
    """Ask Rex for a debate response to Floki's intended trade (GPT-4o).

    Non-blocking rule: this function must never raise; callers must treat failures as neutral.

    Returns:
        {
          "success": True/False,
          "agree": True/False/None,
          "reasoning": str,
          "concerns": list,
          "suggested_adjustment": str,
          "latency_ms": int,
          "model": str
        }
    """
    start = time.time()
    try:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "reason": "OPENAI_API_KEY not set"}

        model = os.environ.get("REX_MODEL", "gpt-4o").strip() or "gpt-4o"

        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
        except Exception as e:
            return {"success": False, "reason": f"openai_client_unavailable: {e}"}

        prompt = _build_prompt(floki_summary)

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are Rex."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=450,
                timeout=timeout_seconds,
            )
        except Exception as e:
            return {"success": False, "reason": f"openai_request_failed: {e}"}

        content = None
        try:
            content = resp.choices[0].message.content
        except Exception:
            content = None

        if not content:
            return {"success": False, "reason": "empty_response"}

        parsed = None
        try:
            parsed = json.loads(content)
        except Exception:
            try:
                # Try to salvage JSON substring
                s = str(content)
                i = s.find("{")
                j = s.rfind("}")
                if i != -1 and j != -1 and j > i:
                    parsed = json.loads(s[i : j + 1])
            except Exception:
                parsed = None

        if not isinstance(parsed, dict):
            return {"success": False, "reason": "invalid_json"}

        agree = parsed.get("agree")
        if isinstance(agree, str):
            agree = agree.strip().lower() in ("true", "yes", "1")
        agree_b = bool(agree) if agree is not None else None

        reasoning = str(parsed.get("reasoning") or "").strip()
        concerns = parsed.get("concerns")
        if concerns is None:
            concerns = []
        if not isinstance(concerns, list):
            concerns = [str(concerns)]
        concerns = [str(c or "").strip() for c in concerns if str(c or "").strip()]

        suggested_adjustment = str(parsed.get("suggested_adjustment") or "").strip()

        latency_ms = int((time.time() - start) * 1000)
        return {
            "success": True,
            "agree": agree_b,
            "reasoning": reasoning,
            "concerns": concerns,
            "suggested_adjustment": suggested_adjustment,
            "latency_ms": latency_ms,
            "model": model,
            "raw": content,
        }
    except Exception as e:
        try:
            log.debug(f"rex_validator: unexpected error (non-blocking): {e}")
        except Exception:
            pass
        return {"success": False, "reason": "unexpected_error"}
