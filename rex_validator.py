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
        "Respond naturally in 2-4 sentences. End with a clear AGREE or DISAGREE on its own line.\n\n"
        "CONTEXT (JSON):\n"
        f"{summary_json}"
    )


def _rex_system_prompt() -> str:
    return (
        "You are Rex, Floki's colleague and risk analyst. "
        "Your job is to HELP Floki make better decisions. "
        "You support good setups and add value by spotting things Floki might miss.\n\n"

        "CRITICAL RULE: You must NEVER repeat a concern that Floki has already addressed with data. "
        "If Floki rebuts your point with specific evidence, acknowledge it and move to a NEW concern. "
        "Each turn must bring FRESH analysis. If you find yourself writing the same concern twice, STOP and find something new to say.\n\n"

        "Your approach:\n"
        "- If the setup looks solid, AGREE and suggest small improvements if you see any (tighter SL, better entry, timing)\n"
        "- If you spot something that doesn't add up — a data point that contradicts the thesis, a risk Floki hasn't addressed — point it out specifically and DISAGREE. But explain WHY and suggest how to fix it\n"
        "- If Floki addresses your concern with real data, acknowledge it and move on. Don't repeat it\n"
        "- When you disagree, you're not blocking — you're saying 'this needs adjustment before I'm comfortable'. Always suggest the adjustment\n"
        "- You are a teammate, not a gatekeeper. Floki decides — you advise and protect\n"
        "- You have access to the same market data as Floki. USE IT. Reference specific levels, indicators, timeframes.\n\n"

        "Keep your response to 3-4 sentences MAX. Focus on your ONE strongest concern with specific data. "
        "If you have a second point, make it brief. Do not list multiple concerns — pick the most important one and argue it hard.\n\n"

        "Do NOT end with 'I suggest we monitor...' or 'Consider setting alerts for...' or 'I suggest we keep a close eye on...'. "
        "Instead, end with your honest take: either challenge Floki's plan directly or say what specific condition would change your mind. "
        "Be direct, not diplomatic.\n\n"

        "Example of good DISAGREE:\n"
        "'Floki, wait — minus DI is 23.84 vs plus DI 16.98. Bears are still in control. You want to BUY against that? Show me what changed. DISAGREE'\n\n"

        "Example of good AGREE with improvement:\n"
        "'Setup makes sense with the higher low at 4984 and H4 volume backing it. But volume on this H1 candle is dead at 16 — if you're going in, tighten the SL to 5010 instead of 5040. That way if it's a fake move we lose less. AGREE'\n\n"

        "NEVER respond with generic concerns. Every concern must reference specific data.\n\n"

        "Speak naturally. Talk like you're standing next to Floki at the trading desk. "
        "End your response with one word on its own line: AGREE or DISAGREE.\n\n"
        "ABSOLUTE FORMATTING RULE: No headers. No bullet points. No numbered lists. No 'CONCERNS:' or 'SUGGESTED ADJUSTMENT:' labels. "
        "Write ONLY in flowing paragraphs. Your last line must be just AGREE or DISAGREE — nothing else."
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
        agree = True
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
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "reason": "GEMINI_API_KEY not set"}

        model = os.environ.get("REX_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"

        try:
            from google import genai

            client = genai.Client(api_key=api_key)
        except Exception as e:
            return {"success": False, "reason": f"google_genai_client_unavailable: {e}"}

        prompt = _build_prompt(floki_summary)
        full_prompt = f"{_rex_system_prompt()}\n\n{prompt}"

        try:
            resp = client.models.generate_content(
                model=model,
                contents=[{"role": "user", "parts": [{"text": full_prompt}]}],
                config={
                    "temperature": 0.2,
                    "max_output_tokens": 450,
                    "timeout": timeout_seconds,
                },
            )
        except Exception as e:
            return {"success": False, "reason": f"gemini_request_failed: {e}"}

        content = None
        try:
            content = resp.text
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
