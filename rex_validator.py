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


def _build_prompt_with_tools(floki_summary: Dict[str, Any]) -> str:
    """Build user prompt for tool-calling Rex — Floki's proposal only, no data snapshot."""
    floki = floki_summary.get("floki", {})
    direction = floki.get("direction", "?")
    confidence = floki.get("confidence", "?")
    reasoning = floki.get("reasoning", "")
    return (
        f"Floki proposes: {direction} at {confidence}% confidence.\n"
        f"His reasoning: {reasoning}\n\n"
        "Check the market data yourself using your tools, then respond with your assessment. "
        "End with AGREE or DISAGREE on its own line."
    )


def _build_prompt_legacy(floki_summary: Dict[str, Any]) -> str:
    """Build user prompt for snapshot-based Rex (fallback)."""
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

        "You have tools to check the market yourself. Look at the data before agreeing or disagreeing — "
        "don't rely only on what Floki tells you. If you disagree, show him what the data actually says.\n\n"

        "Keep it concise — pick your strongest point and argue it with specific data you checked. "
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

        "You are Floki's co-pilot, not just his challenger. When Floki shares his plan, help him refine it — "
        "suggest better entry levels, tighter stops, additional conditions. "
        "When Floki's plan conditions are met, acknowledge it.\n\n"

        "You can disagree and block. But you can also help. A good trading partner says "
        "'I like the direction but let's adjust the entry' not just 'DISAGREE — volume is low.'\n\n"

        "Trust your feel for the market too. If the price action tells you something the indicators don't, say it.\n\n"

        "When reviewing Floki's proposal, check the data yourself first — does your analysis match his? "
        "Then consider whether you agree with his interpretation — same data can mean different things. "
        "Finally, decide how you can help — sharpen the plan if you agree, or propose a specific alternative if you disagree.\n\n"

        "Do NOT end with 'I suggest we monitor...' or 'Consider setting alerts for...'. "
        "End with your honest take — challenge Floki directly or say what would change your mind. "
        "Be direct, not diplomatic.\n\n"

        "Every point you make should reference specific data you checked. No generic concerns.\n\n"

        "Speak naturally. Talk like you're standing next to Floki at the trading desk. "
        "End your response with one word on its own line: AGREE or DISAGREE.\n\n"
        "Write in flowing paragraphs, no lists or headers. End your response with AGREE or DISAGREE on its own line.\n\n"

        "Calibrate your concerns to market conditions:\n"
        "- Sunday night and Asian session have structurally low volume — this is normal, not a red flag. "
        "Do not block trades solely for low off-hours volume.\n"
        "- During extreme geopolitical events (war, crisis, major surprise), moves are conviction-driven "
        "not volume-driven. Standard volume thresholds are less relevant. Focus on structure and direction, "
        "not tick-by-tick participation.\n"
        "- If Luna's environment is DANGER or risk is ≥7/10, the market is in an unusual regime. "
        "Apply your analysis to the regime, not to peacetime norms.\n"
        "- When ADX is >25 and a clear trend is established, demanding 'confirmation candles' or "
        "'higher volume' before every entry delays trades that the trend has already confirmed.\n\n"

        "Your job is to make Floki's trades BETTER. Block when the thesis is fundamentally wrong. "
        "When the thesis is sound but execution needs work, say DISAGREE but offer a specific fix: "
        "better entry, tighter stop, wait for next candle."
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
        # Safe default: if Rex didn't clearly state AGREE/DISAGREE, treat as DISAGREE
        # This prevents truncated or malformed responses from silently approving trades
        log.warning("REX | No clear AGREE/DISAGREE found in response — defaulting to DISAGREE")
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


# FLO-125: Rex tool schemas — 6 tools for independent analysis
_REX_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_price",
            "description": "Get current gold bid/ask/spread",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_candles",
            "description": "Get OHLCV candles. Timeframes: M5, H1, H4, D1. Max 20 candles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timeframe": {"type": "string", "enum": ["M5", "H1", "H4", "D1"]},
                    "count": {"type": "integer"},
                },
                "required": ["timeframe"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_indicators",
            "description": "Get technical indicators: RSI, MACD, EMA50, EMA200, ATR, ADX, Bollinger",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sr_zones",
            "description": "Get support/resistance zones nearest to current price",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_context",
            "description": "Get markets correlated with gold: metals (silver, platinum, gold/silver ratio), forex (dollar strength), indices, energy, crypto, futures (DXY, VIX, 10Y Bond)",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_luna_brief",
            "description": "Get Luna's macro analysis: environment (SAFE/CAUTION/DANGER), risk level, directional bias, detected patterns",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fibonacci_levels",
            "description": "Get Fibonacci retracement levels and swing high/low for H1, H4, D1",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_lessons",
            "description": "Get statistical lessons from Floki's past trades — AVOID patterns (losing setups) and PREFERRED patterns (winning setups)",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_open_positions",
            "description": "Get current open positions with ticket, direction, entry price, SL, TP, P&L",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]


def _get_rex_model() -> str:
    cfg_model = None
    try:
        import config
        cfg_model = getattr(config, "REX_MODEL", None)
    except Exception:
        pass
    return (
        (str(cfg_model).strip() if cfg_model else "")
        or os.environ.get("REX_MODEL", "gpt-5-mini").strip()
        or "gpt-5-mini"
    )


def _execute_rex_tool(agent_tools: Any, name: str, args: dict) -> str:
    """Execute a tool call from Rex, return JSON string result."""
    t0 = time.time()
    try:
        if name == "get_current_price":
            result = agent_tools.get_current_price()
        elif name == "get_candles":
            tf = args.get("timeframe", "H1")
            count = min(int(args.get("count", 10) or 10), 20)
            result = agent_tools.get_candles(tf, count)
        elif name == "get_indicators":
            result = agent_tools.get_indicators()
        elif name == "get_sr_zones":
            result = agent_tools.get_sr_zones()
        elif name == "get_market_context":
            result = agent_tools.get_market_context()
        elif name == "get_luna_brief":
            result = agent_tools.get_luna_brief()
        elif name == "get_fibonacci_levels":
            result = agent_tools.get_fibonacci_levels()
        elif name == "get_trade_lessons":
            result = agent_tools.get_trade_lessons()
        elif name == "get_open_positions":
            result = agent_tools.get_open_positions()
        else:
            result = {"error": f"unknown tool: {name}"}
        dt = int((time.time() - t0) * 1000)
        log.info(f"REX_TOOL | {name} | {dt}ms")
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        dt = int((time.time() - t0) * 1000)
        log.warning(f"REX_TOOL | {name} | {dt}ms | error={e}")
        return json.dumps({"error": str(e)})


def validate_with_rex(
    floki_summary: Dict[str, Any],
    *,
    timeout_seconds: int = 60,
    agent_tools: Any = None,
) -> Dict[str, Any]:
    """Ask Rex for a debate response with independent tool access (GPT-5 mini).

    FLO-125: Rex calls tools directly to verify Floki's claims.
    Falls back to snapshot-based approach if tools unavailable.

    Non-blocking rule: this function must never raise.
    """
    start = time.time()
    try:
        model = _get_rex_model()
        log.info(f"REX | model={model} | provider=openai | tools={'yes' if agent_tools else 'no'}")

        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return {"success": False, "reason": "OPENAI_API_KEY not set"}

        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        # If agent_tools available, use tool-calling flow
        if agent_tools is not None:
            result = _rex_tool_loop(client, model, floki_summary, agent_tools, start, timeout_seconds)
            if result:
                return result
            # Fallback level 2: snapshot-based
            log.info("REX | fallback_level=2 | using snapshot approach")

        # Snapshot-based approach (original behavior or fallback)
        return _validate_with_rex_legacy(client, model, floki_summary, start, timeout_seconds)

    except Exception as e:
        log.warning(f"REX | unexpected error: {e}")
        return {"success": False, "reason": "unexpected_error"}


def _rex_tool_loop(
    client: Any,
    model: str,
    floki_summary: Dict[str, Any],
    agent_tools: Any,
    start: float,
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Run Rex's tool-calling loop. Returns result dict or None (triggers fallback)."""
    MAX_TOOL_ROUNDS = 3  # 3 tool rounds + 1 forced-text round = 4 API calls max
    PER_CALL_TIMEOUT = 20

    prompt = _build_prompt_with_tools(floki_summary)
    messages = [
        {"role": "system", "content": _rex_system_prompt()},
        {"role": "user", "content": prompt},
    ]

    tool_calls_made = []

    for iteration in range(MAX_TOOL_ROUNDS + 1):
        if (time.time() - start) > timeout_seconds:
            log.warning(f"REX | tool loop timeout after {iteration} iterations")
            break

        try:
            # Last iteration: no tools, force text
            use_tools = iteration < MAX_TOOL_ROUNDS
            kwargs = {
                "model": model,
                "messages": messages,
                "max_completion_tokens": 2000,
                "timeout": PER_CALL_TIMEOUT,
            }
            if use_tools:
                kwargs["tools"] = _REX_TOOLS

            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            log.warning(f"REX | API call failed (iteration {iteration}): {e}")
            return None

        if not resp.choices:
            log.warning("REX | API returned empty choices — skipping iteration")
            return None

        msg = resp.choices[0].message
        finish = resp.choices[0].finish_reason

        # Truncation guard: if response was cut off, don't parse partial text
        if finish == "length" and not (msg.tool_calls):
            log.warning("REX | Response truncated (finish_reason=length) — treating as inconclusive")
            return None

        # Tool calls requested
        if msg.tool_calls:
            # Append assistant message with tool calls
            messages.append(msg)

            for tc in msg.tool_calls:
                fname = tc.function.name
                try:
                    fargs = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except Exception:
                    fargs = {}

                result_str = _execute_rex_tool(agent_tools, fname, fargs)
                tool_calls_made.append(fname)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_str,
                })
            continue

        # Text response — we have Rex's answer
        content = msg.content
        if not content or not content.strip():
            # Fallback level 1: force text without tools
            if use_tools:
                log.info("REX | fallback_level=1 | empty response, retrying without tools")
                continue
            log.warning("REX | empty response after forced text call")
            return None

        parsed = _parse_rex_response(content)
        latency_ms = int((time.time() - start) * 1000)
        log.info(f"REX | tool loop complete | iterations={iteration + 1} | tools_called={tool_calls_made} | {latency_ms}ms")
        return {
            "success": True,
            "agree": bool(parsed.agree),
            "reasoning": str(parsed.reasoning or "").strip(),
            "concerns": parsed.concerns if isinstance(parsed.concerns, list) else [],
            "suggested_adjustment": str(parsed.suggested_adjustment or "").strip(),
            "latency_ms": latency_ms,
            "model": model,
            "raw": content,
            "rex_tools_called": tool_calls_made,
        }

    log.warning("REX | tool loop exhausted without text response")
    return None


def _validate_with_rex_legacy(
    client: Any,
    model: str,
    floki_summary: Dict[str, Any],
    start: float,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Snapshot-based Rex validation (original approach, used as fallback)."""
    try:
        prompt = _build_prompt_legacy(floki_summary)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _rex_system_prompt()}, {"role": "user", "content": prompt}],
            max_completion_tokens=2000,
            timeout=min(timeout_seconds, 20),
        )

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
            "rex_tools_called": [],
        }
    except Exception as e:
        log.warning(f"REX | legacy fallback failed: {e}")
        return {"success": False, "reason": f"legacy_failed: {e}"}
