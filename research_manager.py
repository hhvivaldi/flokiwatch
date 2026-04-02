"""
FLO-203: Research Manager v2 — full context (Bull + Bear + Luna + Echo + Sage).
Reads 5 reports from the team and forms an independent verdict.
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
    "You are a senior research director at a gold (XAU/USD) trading firm. "
    "Every cycle you receive up to 5 reports from your team:\n"
    "1. Rex Bull \u2014 argues gold will go UP (BUY)\n"
    "2. Rex Bear \u2014 argues gold will go DOWN (SELL)\n"
    "3. Luna \u2014 macro environment assessment (SAFE/CAUTION/DANGER + directional bias)\n"
    "4. Echo \u2014 news sentiment summary (BULLISH vs BEARISH headline count)\n"
    "5. Sage \u2014 recent trading performance (win rates by direction)\n\n"
    "Your job: Read ALL reports. Form your OWN opinion. Do NOT just pick the best "
    "argument between Bull and Bear. Consider the FULL picture:\n"
    "- If momentum says SELL but macro says BULLISH, that is a DIVERGENCE \u2014 "
    "flag it in reasoning and lower conviction\n"
    "- If performance data says SELL has poor win rate, be cautious about recommending SELL\n"
    "- If most news headlines are BULLISH, that context matters even if short-term "
    "momentum is bearish\n\n"
    "You MUST choose a direction: ENTER_BUY or ENTER_SELL. There is no NO_TRADE option. "
    "Use conviction (1-10) to express certainty. Low conviction (1-4) = weak signal. "
    "High conviction (7-10) = strong signal.\n"
    "Always define entry, SL, target.\n"
    "Be DECISIVE but INFORMED.\n\n"
    "Return this exact JSON schema:\n"
    '{"winner":"BULL" or "BEAR",'
    '"reasoning":"1-2 sentences why, referencing which reports influenced you",'
    '"recommendation":"ENTER_BUY" or "ENTER_SELL",'
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
    if parsed.get("recommendation") not in ("ENTER_BUY", "ENTER_SELL"):
        return False
    if not isinstance(parsed.get("reasoning"), str) or not parsed["reasoning"]:
        return False
    try:
        c = int(parsed.get("conviction", 0))
        return 1 <= c <= 10
    except Exception:
        return False


def _build_user_message(
    bull: Dict[str, Any],
    bear: Dict[str, Any],
    luna_brief: Optional[Dict[str, Any]] = None,
    echo_summary: Optional[Dict[str, Any]] = None,
    sage_note: Optional[str] = None,
) -> str:
    """Build user prompt from all 5 reports."""
    parts = []

    # REPORT 1: Rex Bull
    bull_conv = bull.get("conviction", "?")
    bull_case = bull.get("case", "no argument")
    bull_text = f"REPORT 1 \u2014 Rex Bull (argues gold goes UP \u2014 conviction {bull_conv}/10):\n{bull_case}"
    if bull.get("entry") is not None:
        bull_text += f"\nEntry: ${bull['entry']} SL: ${bull.get('sl', '?')} Target: ${bull.get('target', '?')}"
    parts.append(bull_text)

    # REPORT 2: Rex Bear
    bear_conv = bear.get("conviction", "?")
    bear_case = bear.get("case", "no argument")
    bear_text = f"REPORT 2 \u2014 Rex Bear (argues gold goes DOWN \u2014 conviction {bear_conv}/10):\n{bear_case}"
    if bear.get("entry") is not None:
        bear_text += f"\nEntry: ${bear['entry']} SL: ${bear.get('sl', '?')} Target: ${bear.get('target', '?')}"
    parts.append(bear_text)

    # REPORT 3: Luna Macro Brief
    if luna_brief and isinstance(luna_brief, dict):
        env = luna_brief.get("environment", "?")
        risk = luna_brief.get("risk_level", "?")
        bias = luna_brief.get("directional_bias", "?")
        bias_conf = luna_brief.get("bias_confidence", "?")
        patterns = luna_brief.get("patterns") or luna_brief.get("patterns_detected") or []
        regime = luna_brief.get("regime") or luna_brief.get("market_regime") or "?"
        luna_text = (
            f"REPORT 3 \u2014 Luna Macro Brief:\n"
            f"Environment: {env} | Risk: {risk}/10 | Bias: {bias} ({bias_conf}/10)\n"
            f"Patterns: {', '.join(patterns) if isinstance(patterns, list) and patterns else 'none'}\n"
            f"Regime: {regime}"
        )
        parts.append(luna_text)

    # REPORT 4: Echo News Summary
    if echo_summary and isinstance(echo_summary, dict):
        bullish_n = echo_summary.get("bullish_count", 0)
        bearish_n = echo_summary.get("bearish_count", 0)
        headline = echo_summary.get("key_headline", "")
        echo_text = f"REPORT 4 \u2014 Echo News Summary:\nBULLISH headlines: {bullish_n} | BEARISH headlines: {bearish_n}"
        if headline:
            echo_text += f"\nKey headline: {headline}"
        parts.append(echo_text)

    # REPORT 5: Sage Performance
    if sage_note and isinstance(sage_note, str) and sage_note.strip():
        parts.append(f"REPORT 5 \u2014 Sage Performance:\n{sage_note.strip()}")

    return "\n\n".join(parts)


def run_research_manager(
    bull: Dict[str, Any],
    bear: Dict[str, Any],
    luna_brief: Optional[Dict[str, Any]] = None,
    echo_summary: Optional[Dict[str, Any]] = None,
    sage_note: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    FLO-203: Research Manager v2 — reads 5 reports and forms verdict.
    Returns verdict dict or None on failure.
    """
    t0 = time.time()
    api_key = _get_api_key()
    if not api_key:
        log.warning("RESEARCH_MANAGER | SKIPPED \u2014 no API key")
        return None

    model = _get_model()
    user_msg = _build_user_message(bull, bear, luna_brief, echo_summary, sage_note)

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
