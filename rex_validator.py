import json
import os
import time
import re
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
        "You have access to the same market data as Floki. USE IT. Reference specific levels, indicators, timeframes.\n\n"
        "NEVER respond with generic concerns. Every concern must reference specific data.\n\n"
        "Respond naturally in 2-4 sentences. Then list 1-3 specific concerns with data. Then suggest ONE specific adjustment if needed. "
        "End with a clear AGREE or DISAGREE on its own line.\n\n"
        "CONTEXT (JSON):\n"
        f"{summary_json}"
    )


def _rex_system_prompt() -> str:
    return (
        "You are Rex, a 28-year-old junior gold trader with 5 years of experience. "
        "You work under Floki, a senior trader with 20 years of experience. "
        "Your job is to challenge his reasoning and protect the team from bad trades.\n\n"

        "CRITICAL RULE: You must NEVER repeat a concern that Floki has already addressed with data. "
        "If Floki rebuts your point with specific evidence, acknowledge it and move to a NEW concern. "
        "Each turn must bring FRESH analysis. If you find yourself writing the same concern twice, STOP and find something new to say.\n\n"

        "Your personality:\n"
        "- You're sharp, direct, and not afraid to push back on Floki even though he's senior\n"
        "- You speak naturally like a real trader — ask questions, use specific numbers, point to specific candles or levels\n"
        "- When Floki makes a good rebuttal with data, you acknowledge it honestly — don't repeat the same concern\n"
        "- You focus on RISK — what could go wrong, what Floki might be missing\n"
        "- You're NOT a decision maker. Floki decides. But you make damn sure he's thought it through\n"
        "- You have access to the same market data as Floki. USE IT. Reference specific levels, indicators, timeframes.\n\n"
        "NEVER respond with generic concerns. Every concern must reference specific data.\n\n"
        "Speak naturally. Do NOT use headers like 'CONCERNS:' or bullet point lists. "
        "Talk like you're standing next to Floki at the trading desk. "
        "End your response with one word on its own line: AGREE or DISAGREE.\n\n"
        "FORMATTING REMINDER: Do NOT use headers like 'CONCERNS:' or 'SUGGESTED ADJUSTMENT:'. Do NOT use bullet points or numbered lists. "
        "Write everything as flowing conversation paragraphs. The ONLY formatting allowed is your final line which must be just: AGREE or DISAGREE"
    )


def _parse_rex_response(text: str) -> RexResult:
    raw = str(text or "").strip()
    if not raw:
        return RexResult(agree=False, reasoning="", concerns=[], suggested_adjustment="", raw=text)

    m = re.search(r"\b(AGREE|DISAGREE)\b\s*[\.!\)]*\s*$", raw, flags=re.IGNORECASE)
    if m:
        agree = m.group(1).strip().upper() == "AGREE"
        body = raw[: m.start()].strip()
    else:
        agree = False
        body = raw

    concerns = []
    try:
        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        idx = None
        for i, ln in enumerate(lines):
            if ln.lower().startswith("concerns"):
                idx = i
                break
        if idx is not None:
            after = lines[idx + 1 :]
            for ln in after:
                if ln.lower().startswith("adjust") or ln.lower().startswith("suggest"):
                    break
                s = re.sub(r"^[-*\d\.\)]+\s*", "", ln).strip()
                if s:
                    concerns.append(s)
                if len(concerns) >= 3:
                    break
        else:
            for ln in lines:
                if re.match(r"^\d+[\.)]\s+", ln) or ln.startswith("-") or ln.startswith("*"):
                    s = re.sub(r"^[-*\d\.\)]+\s*", "", ln).strip()
                    if s:
                        concerns.append(s)
                if len(concerns) >= 3:
                    break
    except Exception:
        concerns = []

    suggested_adjustment = ""
    try:
        mm = re.search(
            r"(?im)^(?:adjustment|suggested\s*adjustment|suggestion|adjust):\s*(.+?)\s*$",
            body,
        )
        if mm:
            suggested_adjustment = str(mm.group(1) or "").strip()
    except Exception:
        suggested_adjustment = ""

    return RexResult(
        agree=agree,
        reasoning=body,
        concerns=concerns,
        suggested_adjustment=suggested_adjustment,
        raw=raw,
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
                    {"role": "system", "content": _rex_system_prompt()},
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

        parsed = _parse_rex_response(content)

        latency_ms = int((time.time() - start) * 1000)
        return {
            "success": True,
            "agree": bool(parsed.agree),
            "reasoning": str(parsed.reasoning or "").strip(),
            "concerns": parsed.concerns if isinstance(parsed.concerns, list) else [],
            "suggested_adjustment": str(parsed.suggested_adjustment or "").strip(),
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
