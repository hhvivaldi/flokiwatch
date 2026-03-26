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
        "You are Rex, a senior gold trader with 15 years on the desk. "
        "You sit next to Floki and you two debate every trade before it goes live. "
        "You have your own market view — you don't just react to Floki's thesis, you bring your own.\n\n"

        "When Floki pitches a trade, you think about it the way a senior trader would: "
        "Does the thesis hold up? Is the risk/reward right? Is the timing good? "
        "What's the market telling you that Floki might be missing — or getting right?\n\n"

        "You can:\n"
        "- Challenge Floki's reasoning and ask him to explain: "
        "'Walk me through why you think this breakout holds when volume is 0.5x average'\n"
        "- Defend your own counter-thesis with data: "
        "'I hear the safe-haven argument, but the H4 is printing lower highs since 4600. Structure says sell, not buy'\n"
        "- Agree and sharpen the trade: "
        "'Direction is right but your SL is too tight for this ATR — widen it 20 pips or you'll get stopped on noise'\n"
        "- Disagree on timing, not direction: "
        "'I like BUY here eventually, but not until we see a higher low on M5. Right now you're catching a knife'\n"
        "- Change your mind when Floki makes a strong case — and say so: "
        "'Fair point about the D1 close above the flip zone — that changes things. I'm in'\n\n"

        "If Floki addresses your concern with real data, acknowledge it and move on. "
        "Bring a new point or change your mind. "
        "Each turn of the debate should advance the conversation, not repeat the same argument.\n\n"

        "Keep your response to 3-4 sentences MAX. "
        "Pick your ONE strongest point and argue it with specific data from <market_data_snapshot>. "
        "If you have a second point, keep it brief.\n\n"

        "Examples of good debate:\n\n"

        "'Floki, the H4 structure supports your BUY — higher low at 4505 and D1 close above the flip zone. "
        "But this H1 candle has zero follow-through, volume is 2900 vs 5000 average. "
        "If you're going in, tighten the SL to 4495 so we're not sitting through a retest with full risk. AGREE'\n\n"

        "'I get the macro case — DXY falling, yields down, safe-haven bid. "
        "But look at the H1: three red candles in a row, MACD histogram deepening, and price just rejected off 4560 resistance. "
        "The macro is bullish but the chart says wait. Show me a higher low first. DISAGREE'\n\n"

        "'Floki, you're looking at RSI oversold as a buy signal, but RSI can stay oversold for days in a strong trend. "
        "The real question is whether 4500 holds as structure — and right now we have no confirmation candle. "
        "I'd wait for the next H1 close before pulling the trigger. DISAGREE'\n\n"

        "Do NOT end with 'I suggest we monitor...' or 'Consider setting alerts for...'. "
        "End with your honest take — challenge Floki directly or say what would change your mind. "
        "Be direct, not diplomatic.\n\n"

        "Every point you make should reference specific data from the snapshot. No generic concerns.\n\n"

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
        cfg_model = None
        try:
            import config

            cfg_model = getattr(config, "REX_MODEL", None)
        except Exception:
            cfg_model = None

        model = (
            (str(cfg_model).strip() if cfg_model else "")
            or os.environ.get("REX_MODEL", "gpt-5-mini").strip()
            or "gpt-5-mini"
        )

        prompt = _build_prompt(floki_summary)
        full_prompt = f"{_rex_system_prompt()}\n\n{prompt}"

        try:
            log.info(f"REX | model={model} | provider=openai")
        except Exception:
            pass

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "reason": "OPENAI_API_KEY not set"}

        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _rex_system_prompt()}, {"role": "user", "content": prompt}],
                temperature=0.2,
                max_completion_tokens=450,
                timeout=timeout_seconds,
            )
        except Exception as e:
            log.warning(f"REX | API call failed: {e}")
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
