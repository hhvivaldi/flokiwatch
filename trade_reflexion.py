"""
FLO-137: Post-trade reflexion engine.
After a trade closes, gathers context and calls GPT-5.4 to extract lessons.
Runs in a daemon thread — never blocks the main loop.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from logger import log


REFLEXION_SYSTEM_PROMPT = """You are a trade analyst reviewing a completed XAU/USD trade. Given the thesis at entry, Rex's debate, market conditions, and the actual outcome — analyze what happened.

Return JSON only:
{
  "was_thesis_correct": true/false,
  "what_actually_happened": "1-2 sentences on price action",
  "lesson": "1 sentence — the key takeaway for future trades",
  "pattern_tags": ["tag1", "tag2"],
  "confidence_calibration": "overconfident/accurate/underconfident",
  "would_take_again": true/false,
  "what_would_change": "1 sentence or null"
}

pattern_tags: use lowercase snake_case. Examples: false_breakout, trend_continuation, news_reversal, sl_too_tight, tp_too_ambitious, good_entry_bad_exit, round_number_rejection, asian_session_trap."""


def _build_user_prompt(action: Dict, conditions: Dict, agent_row: Optional[Dict]) -> str:
    """Build the user prompt with all available trade context."""
    thesis = conditions.get("thesis_at_open") or {}
    rex = conditions.get("rex_at_open") or {}

    parts = []
    parts.append(f"TRADE: {action.get('direction', '?')} XAU/USD")
    parts.append(f"Entry: {action.get('open_price', '?')} | Exit: {action.get('close_price', '?')}")
    parts.append(f"P&L: ${action.get('profit', 0):.2f} | Close reason: {action.get('reason', '?')}")

    if thesis:
        parts.append(f"\nTHESIS AT ENTRY:")
        parts.append(f"Direction bias: {thesis.get('direction_bias', '?')}")
        parts.append(f"Conditions: {thesis.get('conditions', '?')}")
        parts.append(f"Key levels: {thesis.get('key_levels', '?')}")

    if rex:
        parts.append(f"\nREX DEBATE:")
        parts.append(f"Agreed: {rex.get('agree', '?')}")
        reasoning = rex.get("reasoning", "")
        if reasoning:
            parts.append(f"Rex said: {reasoning[:500]}")

    if agent_row:
        parts.append(f"\nAGENT REASONING AT ENTRY:")
        parts.append(f"{(agent_row.get('agent_reasoning') or '')[:500]}")
        concerns = agent_row.get("agent_concerns", "")
        if concerns:
            parts.append(f"Concerns: {concerns[:300]}")

    # Indicator snapshot
    for key in ("rsi_h1", "adx_h1", "atr_h1", "session", "luna_environment", "luna_bias"):
        val = conditions.get(key)
        if val is not None:
            parts.append(f"{key}: {val}")

    return "\n".join(parts)


def _load_trade_conditions(ticket: int) -> Dict:
    """Load trade_conditions/{ticket}.json if it exists."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "trade_conditions", f"{ticket}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("conditions_at_open", {})
    except Exception as e:
        log.debug(f"REFLEXION | failed to load conditions for ticket {ticket}: {e}")
    return {}


def _get_agent_decision_row(ticket: int, open_time: str) -> Optional[Dict]:
    """Find the agent_decisions row closest to the trade open time."""
    try:
        from db_writer import get_agent_decision_near_time
        return get_agent_decision_near_time(open_time)
    except Exception as e:
        log.debug(f"REFLEXION | failed to query agent_decisions: {e}")
    return None


def _call_reflexion_llm(system: str, user: str) -> Dict:
    """Call GPT-5.4 for reflexion analysis. Returns parsed JSON + metadata."""
    import config
    from openai import OpenAI

    model = getattr(config, "FLOKI_MODEL", "gpt-4o")
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", getattr(config, "OPENAI_API_KEY", "")))

    start = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.3,
        max_tokens=500,
        response_format={"type": "json_object"},
        timeout=30,
    )
    latency_ms = int((time.time() - start) * 1000)

    text = resp.choices[0].message.content or "{}"
    # Strip markdown fences
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text[:-3].strip()

    tokens = (resp.usage.prompt_tokens + resp.usage.completion_tokens) if resp.usage else 0

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = {"lesson": "reflexion_parse_error", "pattern_tags": []}

    return {"parsed": parsed, "raw": text, "model": model, "tokens": tokens, "latency_ms": latency_ms}


def run_trade_reflexion(action: Dict) -> None:
    """Main entry point — runs the full reflexion pipeline for a closed trade.
    Called in a daemon thread from main.py."""
    ticket = action.get("ticket")
    if not ticket:
        return

    try:
        log.info(f"REFLEXION | starting for ticket={ticket}")

        # Gather context
        conditions = _load_trade_conditions(ticket)
        open_time = action.get("open_time") or conditions.get("open_time", "")
        agent_row = _get_agent_decision_row(ticket, open_time) if open_time else None

        user_prompt = _build_user_prompt(action, conditions, agent_row)

        # Call LLM
        result = _call_reflexion_llm(REFLEXION_SYSTEM_PROMPT, user_prompt)
        parsed = result["parsed"]

        # Extract fields
        lesson = parsed.get("lesson", "")
        tags = parsed.get("pattern_tags", [])
        thesis_summary = ""
        thesis = conditions.get("thesis_at_open", {})
        if thesis:
            thesis_summary = f"{thesis.get('direction_bias', '?')}: {thesis.get('conditions', '')}"[:500]

        # Store in DB
        from db_writer import record_trade_reflexion
        record_trade_reflexion(
            ticket=ticket,
            direction=action.get("direction", ""),
            entry_price=action.get("open_price", 0),
            exit_price=action.get("close_price", 0),
            pnl=action.get("profit", 0),
            close_reason=action.get("reason", ""),
            thesis_summary=thesis_summary,
            reflexion_json=result["raw"],
            lesson=lesson,
            pattern_tags=json.dumps(tags),
            model=result["model"],
            tokens=result["tokens"],
            latency_ms=result["latency_ms"],
        )

        log.info(f"REFLEXION | ticket={ticket} | lesson={lesson[:80]} | tags={tags}")

    except Exception as e:
        log.warning(f"REFLEXION | failed for ticket={ticket}: {e}")


def run_trade_reflexion_async(action: Dict) -> None:
    """Launch reflexion in a daemon thread. Never blocks the main loop."""
    t = threading.Thread(target=run_trade_reflexion, args=(action,), daemon=True)
    t.start()
