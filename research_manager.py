"""
FLO-194: Research Manager agent.
Receives Rex Bull and Rex Bear arguments, picks the winner,
produces a clear verdict with trigger levels for Floki.
Uses Gemini (same API as Sage, separate model variable).
"""

import json
import os
import re
import time
from typing import Any, Dict, Optional

from logger import log

RESEARCH_MANAGER_TIMEOUT = 10  # seconds

_SYSTEM_PROMPT = (
    "You are a senior research manager at a gold (XAU/USD) trading firm. "
    "You receive two opposing arguments:\n"
    "- Rex Bull argues gold will go UP (BUY)\n"
    "- Rex Bear argues gold will go DOWN (SELL)\n\n"
    "Your job:\n"
    "1. Pick who has the stronger argument.\n"
    "2. If BULL wins: recommendation = ENTER_BUY with entry, SL, target.\n"
    "3. If BEAR wins: recommendation = ENTER_SELL with entry, SL, target.\n"
    "4. If NEITHER is convincing: recommendation = NO_TRADE with trigger levels.\n"
    "5. Be DECISIVE. Pick a side.\n\n"
    "Return this exact JSON schema:\n"
    '{"winner":"BULL" or "BEAR",'
    '"reasoning":"1-2 sentences why this side wins",'
    '"recommendation":"ENTER_BUY" or "ENTER_SELL" or "NO_TRADE",'
    '"entry":price_or_null,"sl":price_or_null,"target":price_or_null,'
    '"trigger_buy":"price+condition or null","trigger_sell":"price+condition or null",'
    '"conviction":1_to_10}'
)


def _get_model() -> str:
    try:
        import config
        return getattr(config, "RESEARCH_MANAGER_MODEL", None) or "gemini-3-flash-preview"
    except Exception:
        return "gemini-3-flash-preview"


def _get_api_key() -> str:
    try:
        import config
        key = getattr(config, "SAGE_API_KEY", "") or ""
        if not key:
            key = os.environ.get("SAGE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "") or ""
        return str(key).strip()
    except Exception:
        return os.environ.get("SAGE_API_KEY", "") or os.environ.get("GEMINI_API_KEY", "") or ""


def _parse_json(raw: str) -> Optional[dict]:
    """Strip markdown fences and parse JSON."""
    if not raw:
        return None
    try:
        text = raw.strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
    except Exception:
        pass
    return None


def _validate(parsed: dict) -> bool:
    """Check required fields."""
    if not isinstance(parsed, dict):
        return False
    if parsed.get("winner") not in ("BULL", "BEAR"):
        return False
    if parsed.get("recommendation") not in ("ENTER_BUY", "ENTER_SELL", "NO_TRADE"):
        return False
    if not isinstance(parsed.get("reasoning"), str) or not parsed["reasoning"]:
        return False
    try:
        c = int(parsed.get("conviction", 0))
        return 1 <= c <= 10
    except Exception:
        return False


def _build_user_message(bull: Dict[str, Any], bear: Dict[str, Any]) -> str:
    """Build the user prompt from Bull and Bear results."""
    parts = []

    # Bull argument (argues gold goes UP → BUY)
    bull_conv = bull.get("conviction", "?")
    bull_case = bull.get("case", "no argument")
    bull_text = f"Rex Bull (argues gold goes UP — conviction {bull_conv}/10):\nCase: {bull_case}"
    if bull.get("entry") is not None:
        bull_text += f"\nEntry: ${bull['entry']} SL: ${bull.get('sl', '?')} Target: ${bull.get('target', '?')}"
    parts.append(bull_text)

    # Bear argument (argues gold goes DOWN → SELL)
    bear_conv = bear.get("conviction", "?")
    bear_case = bear.get("case", "no argument")
    bear_text = f"Rex Bear (argues gold goes DOWN — conviction {bear_conv}/10):\nCase: {bear_case}"
    if bear.get("entry") is not None:
        bear_text += f"\nEntry: ${bear['entry']} SL: ${bear.get('sl', '?')} Target: ${bear.get('target', '?')}"
    parts.append(bear_text)

    return "\n\n".join(parts)


def run_research_manager(bull: Dict[str, Any], bear: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    FLO-194: Run Research Manager to pick winner between Bull and Bear.
    Returns verdict dict or None on failure.
    """
    t0 = time.time()
    api_key = _get_api_key()
    if not api_key:
        log.warning("RESEARCH_MANAGER | SKIPPED — no API key")
        return None

    model = _get_model()
    user_msg = _build_user_message(bull, bear)

    try:
        from google import genai
        client = genai.Client(api_key=api_key)

        resp = client.models.generate_content(
            model=model,
            contents=user_msg,
            config={
                "system_instruction": _SYSTEM_PROMPT,
                "response_mime_type": "application/json",
                "max_output_tokens": 2000,
            },
        )

        latency_ms = int((time.time() - t0) * 1000)

        # Token usage
        input_tokens = 0
        output_tokens = 0
        try:
            usage = getattr(resp, "usage_metadata", None)
            if usage:
                input_tokens = getattr(usage, "prompt_token_count", 0) or 0
                total = getattr(usage, "total_token_count", 0) or 0
                output_tokens = max(0, total - input_tokens)
        except Exception:
            pass

        # Parse response
        text = ""
        try:
            text = getattr(resp, "text", "") or ""
        except Exception:
            text = ""

        if not text:
            log.warning(f"RESEARCH_MANAGER | EMPTY_RESPONSE | {latency_ms}ms")
            return None

        parsed = _parse_json(text)
        if parsed is None:
            log.warning(f"RESEARCH_MANAGER | JSON_FAIL | raw={text[:200]} | {latency_ms}ms")
            return None

        if not _validate(parsed):
            log.warning(f"RESEARCH_MANAGER | VALIDATION_FAIL | parsed={json.dumps(parsed)[:200]} | {latency_ms}ms")
            return None

        winner = parsed["winner"]
        rec = parsed["recommendation"]
        conv = int(parsed.get("conviction", 5))

        log.info(
            f"RESEARCH_MANAGER | OK | winner={winner} | rec={rec} | conv={conv} | "
            f"{latency_ms}ms | {input_tokens}+{output_tokens} tokens"
        )

        return {
            "status": "OK",
            "winner": winner,
            "reasoning": parsed["reasoning"],
            "recommendation": rec,
            "entry": parsed.get("entry"),
            "sl": parsed.get("sl"),
            "target": parsed.get("target"),
            "trigger_buy": parsed.get("trigger_buy"),
            "trigger_sell": parsed.get("trigger_sell"),
            "conviction": conv,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "timestamp": time.time(),
        }

    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        if latency_ms >= RESEARCH_MANAGER_TIMEOUT * 1000:
            log.warning(f"RESEARCH_MANAGER | TIMEOUT | {latency_ms}ms")
        else:
            log.warning(f"RESEARCH_MANAGER | ERROR | {e} | {latency_ms}ms")
        return None
