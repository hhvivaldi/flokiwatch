import json
import os
import time
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from logger import log


@dataclass
class RexResult:
    """FLO-158: Rex returns insights, not agree/disagree."""
    insights: list  # [{type, observation, source, implication}, ...]
    risk_flags: list  # [str, ...]
    raw: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "insights": self.insights or [],
            "risk_flags": self.risk_flags or [],
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
        "You are Rex, a senior gold analyst with 15 years on the desk. "
        "You sit next to Floki and provide market intelligence before every trade. "
        "You are NOT a judge — you never approve or reject. "
        "You are an analyst who surfaces insights Floki might have missed.\n\n"

        "Your job: check the data yourself using your unique tools, then provide 0-3 INSIGHTS. "
        "Each insight is something Floki may not have seen — a pattern, a divergence, "
        "a historical precedent, a correlation shift.\n\n"

        "Focus on what YOUR unique tools reveal:\n"
        "- Session performance: how has this setup performed historically?\n"
        "- Divergences: is RSI or MACD diverging from price on H4/D1?\n"
        "- Regime history: when the market last transitioned like this, what happened?\n"
        "- Correlations: is the gold-DXY or gold-yields correlation normal or broken?\n"
        "- Past reflexions: have similar setups led to wins or losses recently?\n\n"

        "You also have standard tools (price, candles, indicators, S/R, fibonacci, positions) "
        "to verify Floki's data.\n\n"

        "Do NOT say AGREE or DISAGREE. Do NOT judge Floki's thesis. Instead:\n"
        "- If you see risk: FLAG — describe the risk with specific data\n"
        "- If you see opportunity: NOTE — describe the signal with specific data\n"
        "- If you see a pattern: HISTORY — describe the historical precedent\n\n"

        "ALWAYS provide at least 1 insight from your unique tool data. "
        "Even 'no divergence on H4/D1' or 'correlations normal' is useful context — it confirms or challenges Floki's thesis. "
        "Absence of signal IS a signal. Do not fabricate data, but do interpret what you found.\n\n"

        "CRITICAL: Before providing insights, you MUST call at least 2 of your unique tools: "
        "rex_session_performance, rex_divergence_scan, rex_regime_history, rex_reflexion_search, "
        "rex_correlation_check. These tools give you data Floki does NOT have — they are the reason "
        "you exist. Do NOT provide insights based only on get_indicators or get_candles — Floki "
        "already checked those. Your value is the UNIQUE data only you can access.\n\n"

        "NEVER use the words AGREE or DISAGREE anywhere in your response. You are not a judge. "
        "If you catch yourself writing AGREE or DISAGREE, stop and rephrase as a FLAG, NOTE, or HISTORY insight.\n\n"

        "Respond with valid JSON only:\n"
        "{\n"
        '  "insights": [\n'
        '    {"type": "FLAG|NOTE|HISTORY", "observation": "what you found", '
        '"source": "which tool/data", "implication": "what it means for the trade"}\n'
        "  ],\n"
        '  "risk_flags": ["short summary of each risk"]\n'
        "}\n\n"

        "0-3 insights maximum. 0-3 risk flags maximum. Quality over quantity.\n\n"

        "When ADX is high (>25) but +DI has crossed against the thesis direction, "
        "flag this as a potential trend reversal.\n\n"

        "Respond with raw JSON only. No markdown fences, no ```json blocks, no text before or after the JSON."
    )


def _parse_rex_response(text: str) -> RexResult:
    """FLO-158: Parse Rex JSON insights response."""
    raw = str(text or "").strip()
    if not raw:
        return RexResult(insights=[], risk_flags=[], raw=text)

    # Strip markdown fences (handles ```json, ``` , trailing ```)
    import re
    cleaned = raw.strip()
    cleaned = re.sub(r'^```(?:json)?\s*\n?', '', cleaned)
    cleaned = re.sub(r'\n?```\s*$', '', cleaned)
    cleaned = cleaned.strip()

    try:
        import json
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            insights = parsed.get("insights", [])
            if not isinstance(insights, list):
                insights = []
            # Validate each insight has required fields
            valid_insights = []
            for ins in insights[:3]:
                if isinstance(ins, dict) and ins.get("observation"):
                    valid_insights.append({
                        "type": str(ins.get("type", "NOTE")).upper(),
                        "observation": str(ins.get("observation", "")),
                        "source": str(ins.get("source", "")),
                        "implication": str(ins.get("implication", "")),
                    })
            risk_flags = parsed.get("risk_flags", [])
            if not isinstance(risk_flags, list):
                risk_flags = []
            risk_flags = [str(f) for f in risk_flags[:3]]
            return RexResult(insights=valid_insights, risk_flags=risk_flags, raw=raw)
    except Exception:
        pass

    # Fallback: treat entire response as a single NOTE insight
    log.warning("REX | Response not valid JSON — wrapping as single insight")
    return RexResult(
        insights=[{"type": "NOTE", "observation": raw[:500], "source": "rex_analysis", "implication": ""}],
        risk_flags=[],
        raw=raw,
    )


# FLO-158: Rex tools — 6 standard + 5 unique (11 total)
_REX_TOOLS = [
    # Standard tools (verify Floki's data)
    {"type": "function", "function": {"name": "get_current_price", "description": "Get current gold bid/ask/spread", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_candles", "description": "Get OHLCV candles. Use H1 or H4 only (M5 and D1 are rarely needed). Max 15 candles.", "parameters": {"type": "object", "properties": {"timeframe": {"type": "string", "enum": ["M5", "H1", "H4", "D1"]}, "count": {"type": "integer"}}, "required": ["timeframe"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_indicators", "description": "Get technical indicators: RSI, MACD, EMA50, EMA200, ATR, ADX, Bollinger, +DI, -DI", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_sr_zones", "description": "Get support/resistance zones nearest to current price", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_fibonacci_levels", "description": "Get Fibonacci retracement levels and swing high/low for H1, H4, D1", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "get_open_positions", "description": "Get current open positions with ticket, direction, entry price, SL, TP, P&L", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    # Unique Rex tools (not available to Floki)
    {"type": "function", "function": {"name": "rex_session_performance", "description": "Get win rate and P&L breakdown by session (Asian/London/NY) and direction (BUY/SELL) for recent agent trades (last 30 days)", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rex_divergence_scan", "description": "Scan for RSI and MACD divergences on H4 and D1 timeframes — detects when price and indicators are telling different stories", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rex_regime_history", "description": "Get market regime transition history — current regime, duration, and past transitions", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rex_reflexion_search", "description": "Semantic search past trade reflexions — find similar setups and what happened. Query with natural language.", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "Natural language description of the trade pattern"}}, "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "rex_correlation_check", "description": "Check real-time 24h correlations: gold vs DXY, gold vs silver, gold vs 10Y bonds. Shows if correlations are normal or broken.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}},
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
        or os.environ.get("REX_MODEL", "gpt-4o").strip()
        or "gpt-4o"
    )


def _execute_rex_tool(agent_tools: Any, name: str, args: dict) -> str:
    """Execute a tool call from Rex, return JSON string result."""
    t0 = time.time()
    try:
        if name == "get_current_price":
            result = agent_tools.get_current_price()
        elif name == "get_candles":
            tf = args.get("timeframe", "H1")
            count = min(int(args.get("count", 10) or 10), 15)
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
        # FLO-169: Rex unique tools (were missing since FLO-158)
        elif name == "rex_session_performance":
            result = agent_tools.rex_session_performance()
        elif name == "rex_divergence_scan":
            result = agent_tools.rex_divergence_scan()
        elif name == "rex_regime_history":
            result = agent_tools.rex_regime_history()
        elif name == "rex_reflexion_search":
            result = agent_tools.rex_reflexion_search(query=args.get("query", ""))
        elif name == "rex_correlation_check":
            result = agent_tools.rex_correlation_check()
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
    timeout_seconds: int = 90,
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
            return {"success": False, "insights": [], "risk_flags": [], "reason": "OPENAI_API_KEY not set"}

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
        return {"success": False, "insights": [], "risk_flags": [], "reason": "unexpected_error"}


def _rex_tool_loop(
    client: Any,
    model: str,
    floki_summary: Dict[str, Any],
    agent_tools: Any,
    start: float,
    timeout_seconds: int,
) -> Optional[Dict[str, Any]]:
    """Run Rex's tool-calling loop. Returns result dict or None (triggers fallback)."""
    MAX_TOOL_ROUNDS = 2  # 2 tool rounds + 1 forced-text round = 3 API calls max
    PER_CALL_TIMEOUT = 30

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
                "max_completion_tokens": 3000,
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
                except Exception as e:
                    log.warning(f"REX | tool arg parse failed | tool={fname} | error={e}")
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
            "insights": parsed.insights,
            "risk_flags": parsed.risk_flags,
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
            max_completion_tokens=3000,
            timeout=min(timeout_seconds, 45),
        )

        content = None
        try:
            content = resp.choices[0].message.content
        except Exception:
            content = None

        if not content:
            return {"success": False, "insights": [], "risk_flags": [], "reason": "empty_response"}

        parsed = _parse_rex_response(content)
        latency_ms = int((time.time() - start) * 1000)
        return {
            "success": True,
            "insights": parsed.insights,
            "risk_flags": parsed.risk_flags,
            "latency_ms": latency_ms,
            "model": model,
            "raw": content,
            "rex_tools_called": [],
        }
    except Exception as e:
        log.warning(f"REX | legacy fallback failed: {e}")
        return {"success": False, "insights": [], "risk_flags": [], "reason": f"legacy_failed: {e}"}


# =========================================================================
# FLO-190: Rex Bull / Rex Bear structured debate
# =========================================================================

_REX_BULL_PROMPT = (
    "You are a bullish XAU/USD researcher. Your job is to find EVERY reason why "
    "gold will GO UP from here. Argue for BUY. Find bullish signals: support levels, "
    "oversold conditions, macro tailwinds, reversal patterns, buying pressure. "
    "You MUST argue for BUY \u2014 that is your role. Never argue for SELL.\n\n"
    "Return ONLY valid JSON:\n"
    '{"direction":"BUY","case":"2-3 sentences why gold goes up",'
    '"entry":<price>,"sl":<price>,"target":<price>,"conviction":<1-10>}'
)

_REX_BEAR_PROMPT = (
    "You are a bearish XAU/USD researcher. Your job is to find EVERY reason why "
    "gold will GO DOWN from here. Argue for SELL. Find bearish signals: resistance levels, "
    "overbought conditions, macro headwinds, breakdown patterns, selling pressure. "
    "You MUST argue for SELL \u2014 that is your role. Never argue for BUY.\n\n"
    "Return ONLY valid JSON:\n"
    '{"direction":"SELL","case":"2-3 sentences why gold goes down",'
    '"entry":<price>,"sl":<price>,"target":<price>,"conviction":<1-10>}'
)

BULL_BEAR_TIMEOUT = 15  # seconds total for both calls


def _parse_json_response(raw: str) -> Optional[dict]:
    """Strip markdown fences and parse JSON. Returns None on failure."""
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


def _validate_bull(parsed: dict) -> bool:
    """Check Rex Bull has all required fields."""
    if not isinstance(parsed, dict):
        return False
    if not isinstance(parsed.get("direction"), str):
        return False
    if not isinstance(parsed.get("case"), str):
        return False
    try:
        c = int(parsed.get("conviction", 0))
        return 1 <= c <= 10
    except Exception:
        return False


def _validate_bear(parsed: dict) -> bool:
    """Check Rex Bear has all required fields (same format as Bull)."""
    if not isinstance(parsed, dict):
        return False
    if not isinstance(parsed.get("direction"), str):
        return False
    if not isinstance(parsed.get("case"), str):
        return False
    try:
        c = int(parsed.get("conviction", 0))
        return 1 <= c <= 10
    except Exception:
        return False


def _build_debate_context(data: Dict[str, Any]) -> str:
    """Build the user message for both Rex Bull and Rex Bear from available data."""
    parts = []
    try:
        price = data.get("price")
        if price is not None:
            parts.append(f"Current price: ${price}")
    except Exception:
        pass
    try:
        regime = data.get("regime")
        if regime:
            parts.append(f"Regime: {regime}")
    except Exception:
        pass
    try:
        ind = data.get("indicators", {})
        if ind:
            ind_parts = []
            for k in ("rsi_14", "adx_14", "macd_hist", "macd_signal", "ema_9", "ema_21", "ema_50", "atr_14", "bb_position", "volume_ratio", "plus_di", "minus_di"):
                v = ind.get(k)
                if v is not None:
                    ind_parts.append(f"{k}={v}")
            if ind_parts:
                parts.append(f"Indicators: {', '.join(ind_parts)}")
    except Exception:
        pass
    # FLO-232: Multi-TF indicators (M15/H1/H4/D1)
    try:
        mtf = data.get("multi_tf", {})
        if mtf:
            mtf_lines = []
            for tf in ("M15", "H1", "H4", "D1"):
                tfd = mtf.get(tf, {})
                if not tfd:
                    continue
                mtf_lines.append(
                    f"{tf}: RSI={tfd.get('rsi')} ({tfd.get('rsi_direction', '?')}), "
                    f"MACD {tfd.get('macd_direction', '?')}, ADX={tfd.get('adx')}, "
                    f"EMA align: {tfd.get('ema_alignment', '?')}, "
                    f"price {tfd.get('price_vs_ema50', '?')} EMA50, "
                    f"{tfd.get('price_vs_ema200', '?')} EMA200"
                )
            if mtf_lines:
                parts.append("Multi-timeframe:\n" + "\n".join(mtf_lines))
    except Exception:
        pass
    try:
        ms = data.get("market_structure")
        if ms:
            parts.append(f"Market structure:\n{ms}")
    except Exception:
        pass
    try:
        luna = data.get("luna")
        if luna:
            parts.append(f"Luna: environment={luna.get('environment')}, bias={luna.get('directional_bias')}, risk={luna.get('risk_level')}")
    except Exception:
        pass
    try:
        echo = data.get("echo_summary")
        if echo:
            parts.append(f"Echo alerts: {echo}")
    except Exception:
        pass
    try:
        candles = data.get("candle_summary")
        if candles:
            parts.append(f"Recent candles:\n{candles}")
    except Exception:
        pass
    # FLO-211: Rex monitor findings (if fresh)
    try:
        rex_mon = data.get("rex_monitor")
        if isinstance(rex_mon, dict) and rex_mon.get("findings"):
            age = rex_mon.get("age_minutes", "?")
            lines = [f"Rex monitor (scanned {age}m ago):"]
            for f in rex_mon.get("findings", [])[:5]:
                lines.append(f"  - [{f.get('type', '?')}] {f.get('detail', '')}")
            parts.append("\n".join(lines))
    except Exception:
        pass
    return "\n\n".join(parts) if parts else "No data available."


def _call_rex_side(system_prompt: str, user_msg: str, label: str) -> Dict[str, Any]:
    """Make a single Rex Bull or Rex Bear API call. Returns result dict."""
    t0 = time.time()
    try:
        from openai import OpenAI
        model = _get_rex_model()
        client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_completion_tokens=500,
            timeout=12,
        )

        latency_ms = int((time.time() - t0) * 1000)
        content = ""
        input_tokens = 0
        output_tokens = 0
        try:
            content = resp.choices[0].message.content or ""
            input_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        except Exception:
            pass

        parsed = _parse_json_response(content)
        if parsed is None:
            log.warning(f"{label} | JSON_FAIL | raw={content[:200]} | {latency_ms}ms")
            return {"success": False, "reason": "json_fail", "raw": content[:200], "latency_ms": latency_ms}

        log.info(f"{label} | OK | {latency_ms}ms | {input_tokens}+{output_tokens} tokens")
        return {
            "success": True,
            "parsed": parsed,
            "raw": content,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
        }
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        log.warning(f"{label} | ERROR | {e} | {latency_ms}ms")
        return {"success": False, "reason": str(e), "latency_ms": latency_ms}


def run_bull_bear_debate(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    FLO-190: Run Rex Bull and Rex Bear in parallel.
    Returns debate dict with both sides, or status="SKIPPED" on failure.
    Both must succeed or neither is used (Rule 1).
    """
    t0 = time.time()
    user_msg = _build_debate_context(data)

    bull_result = None
    bear_result = None

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            bull_future = executor.submit(_call_rex_side, _REX_BULL_PROMPT, user_msg, "REX_BULL")
            bear_future = executor.submit(_call_rex_side, _REX_BEAR_PROMPT, user_msg, "REX_BEAR")

            try:
                bull_result = bull_future.result(timeout=BULL_BEAR_TIMEOUT)
            except Exception:
                bull_result = {"success": False, "reason": "timeout"}

            try:
                bear_result = bear_future.result(timeout=max(1, BULL_BEAR_TIMEOUT - (time.time() - t0)))
            except Exception:
                bear_result = {"success": False, "reason": "timeout"}
    except Exception as e:
        log.warning(f"REX_DEBATE | executor error: {e}")
        return {"status": "SKIPPED", "skip_reason": f"executor_error: {e}", "timestamp": time.time()}

    total_ms = int((time.time() - t0) * 1000)

    # Rule 1: Both must succeed
    if not bull_result.get("success"):
        reason = f"Bull failed: {bull_result.get('reason', '?')}"
        log.info(f"REX_DEBATE | SKIPPED | {reason} | {total_ms}ms")
        return {"status": "SKIPPED", "skip_reason": reason, "timestamp": time.time()}

    if not bear_result.get("success"):
        reason = f"Bear failed: {bear_result.get('reason', '?')}"
        log.info(f"REX_DEBATE | SKIPPED | {reason} | {total_ms}ms")
        return {"status": "SKIPPED", "skip_reason": reason, "timestamp": time.time()}

    bull_parsed = bull_result["parsed"]
    bear_parsed = bear_result["parsed"]

    # Rule 2: Validate required fields
    if not _validate_bull(bull_parsed):
        reason = f"Bull validation failed: {json.dumps(bull_parsed)[:200]}"
        log.info(f"REX_DEBATE | SKIPPED | {reason} | {total_ms}ms")
        return {"status": "SKIPPED", "skip_reason": reason, "timestamp": time.time()}

    if not _validate_bear(bear_parsed):
        reason = f"Bear validation failed: {json.dumps(bear_parsed)[:200]}"
        log.info(f"REX_DEBATE | SKIPPED | {reason} | {total_ms}ms")
        return {"status": "SKIPPED", "skip_reason": reason, "timestamp": time.time()}

    # Success — both sides valid
    bull_conv = int(bull_parsed.get("conviction", 5))
    bear_conv = int(bear_parsed.get("conviction", 5))

    log.info(
        f"REX_DEBATE | INJECTED | bull_conv={bull_conv} bear_conv={bear_conv} | {total_ms}ms | "
        f"bull_tokens={bull_result.get('input_tokens', 0)}+{bull_result.get('output_tokens', 0)} "
        f"bear_tokens={bear_result.get('input_tokens', 0)}+{bear_result.get('output_tokens', 0)}"
    )

    return {
        "status": "INJECTED",
        "rex_bull": bull_parsed,
        "rex_bear": bear_parsed,
        "bull_model": bull_result.get("model"),
        "bear_model": bear_result.get("model"),
        "bull_latency_ms": bull_result.get("latency_ms"),
        "bear_latency_ms": bear_result.get("latency_ms"),
        "bull_tokens": {"input": bull_result.get("input_tokens", 0), "output": bull_result.get("output_tokens", 0)},
        "bear_tokens": {"input": bear_result.get("input_tokens", 0), "output": bear_result.get("output_tokens", 0)},
        "total_ms": total_ms,
        "timestamp": time.time(),
        "skip_reason": None,
    }
