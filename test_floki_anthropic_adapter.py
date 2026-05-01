"""FLO-419 Phase 2 — verification for the Anthropic adapter + the live API path.

Two tiers:

  * Unit (no API key required): exercises the pure conversion functions.
    Run: `python test_floki_anthropic_adapter.py`

  * Live (requires ANTHROPIC_API_KEY in .env): runs ONE real call to
    Anthropic with the actual SYSTEM_PROMPT and a minimal user message.
    Confirms the wire format works end-to-end and the response unwraps
    correctly. Pass `--live` flag.
    Run: `python test_floki_anthropic_adapter.py --live`

Exits non-zero on failure. Print-driven for fast diff-on-fail.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(ROOT / ".env", override=True)
except Exception:
    pass


def _expect(label: str, actual, expected) -> None:
    if actual != expected:
        print(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


def _expect_truthy(label: str, value) -> None:
    if not value:
        print(f"FAIL [{label}]: expected truthy, got {value!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


# ---------------------------------------------------------------------------
# Unit tests — pure conversion
# ---------------------------------------------------------------------------

def test_tools_conversion():
    from floki_anthropic_adapter import convert_tools_oai_to_anthropic
    oai_tools = [
        {"type": "function", "function": {
            "name": "get_indicators",
            "description": "Get H1 indicators.",
            "parameters": {"type": "object", "properties": {"timeframe": {"type": "string"}}},
        }},
    ]
    out = convert_tools_oai_to_anthropic(oai_tools)
    _expect("tools: count", len(out), 1)
    _expect("tools: name", out[0]["name"], "get_indicators")
    _expect("tools: description", out[0]["description"], "Get H1 indicators.")
    _expect_truthy("tools: input_schema present", "input_schema" in out[0])
    _expect("tools: input_schema.type", out[0]["input_schema"]["type"], "object")
    print()

    # Pass-through Anthropic-shape (the codebase's _tool_schemas already authors this)
    anth_native = [{"name": "x", "description": "y", "input_schema": {"type": "object", "properties": {}}}]
    out2 = convert_tools_oai_to_anthropic(anth_native)
    _expect("tools: pass-through count", len(out2), 1)
    _expect("tools: pass-through name", out2[0]["name"], "x")
    print()


def test_messages_simple():
    from floki_anthropic_adapter import convert_messages_oai_to_anthropic
    msgs = [
        {"role": "system", "content": "You are Floki."},
        {"role": "user", "content": "Hello."},
    ]
    sys_text, anth_msgs = convert_messages_oai_to_anthropic(msgs)
    _expect("simple: system text", sys_text, "You are Floki.")
    _expect("simple: messages count", len(anth_msgs), 1)
    _expect("simple: msg[0] role", anth_msgs[0]["role"], "user")
    _expect("simple: msg[0] content", anth_msgs[0]["content"], "Hello.")
    print()


def test_messages_with_system_blocks():
    """The codebase wraps system in [{type:text, text, cache_control}]. Conversion
    should extract the text into the system parameter."""
    from floki_anthropic_adapter import convert_messages_oai_to_anthropic
    msgs = [
        {"role": "system", "content": [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "go"},
    ]
    sys_text, _ = convert_messages_oai_to_anthropic(msgs)
    _expect("blocked-sys: system text extracted", sys_text, "SYS")
    print()


def test_tool_call_roundtrip():
    """assistant{tool_calls=[A,B]} + tool{A} + tool{B} → 1 assistant block + 1 user block."""
    from floki_anthropic_adapter import convert_messages_oai_to_anthropic
    msgs = [
        {"role": "user", "content": "kick off"},
        {"role": "assistant", "content": "thinking", "tool_calls": [
            {"id": "tc1", "type": "function", "function": {"name": "get_a", "arguments": '{"x": 1}'}},
            {"id": "tc2", "type": "function", "function": {"name": "get_b", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "tc1", "content": '{"r": 1}'},
        {"role": "tool", "tool_call_id": "tc2", "content": '{"r": 2}'},
        {"role": "assistant", "content": "final"},
    ]
    sys_text, anth_msgs = convert_messages_oai_to_anthropic(msgs)
    _expect("toolloop: system empty", sys_text, "")
    _expect("toolloop: msg count", len(anth_msgs), 4)
    _expect("toolloop: msg[0] role", anth_msgs[0]["role"], "user")
    _expect("toolloop: msg[1] role", anth_msgs[1]["role"], "assistant")
    # assistant must have text + 2 tool_use blocks
    blocks = anth_msgs[1]["content"]
    _expect("toolloop: assistant blocks count", len(blocks), 3)
    _expect("toolloop: assistant block[0].type", blocks[0]["type"], "text")
    _expect("toolloop: assistant block[1].type", blocks[1]["type"], "tool_use")
    _expect("toolloop: assistant block[1].id", blocks[1]["id"], "tc1")
    _expect("toolloop: assistant block[1].name", blocks[1]["name"], "get_a")
    _expect("toolloop: assistant block[1].input", blocks[1]["input"], {"x": 1})
    _expect("toolloop: assistant block[2].id", blocks[2]["id"], "tc2")
    # user message with 2 tool_results
    _expect("toolloop: msg[2] role", anth_msgs[2]["role"], "user")
    _expect("toolloop: msg[2] block count", len(anth_msgs[2]["content"]), 2)
    _expect("toolloop: msg[2].block[0].type", anth_msgs[2]["content"][0]["type"], "tool_result")
    _expect("toolloop: msg[2].block[0].tool_use_id", anth_msgs[2]["content"][0]["tool_use_id"], "tc1")
    _expect("toolloop: msg[3] role", anth_msgs[3]["role"], "assistant")
    print()


def test_image_conversion():
    from floki_anthropic_adapter import convert_messages_oai_to_anthropic
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "Charts:"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]},
    ]
    _, anth_msgs = convert_messages_oai_to_anthropic(msgs)
    _expect("image: msg count", len(anth_msgs), 1)
    blocks = anth_msgs[0]["content"]
    _expect("image: blocks count", len(blocks), 2)
    _expect("image: block[1].type", blocks[1]["type"], "image")
    _expect("image: block[1].source.type", blocks[1]["source"]["type"], "base64")
    _expect("image: block[1].source.media_type", blocks[1]["source"]["media_type"], "image/png")
    _expect("image: block[1].source.data", blocks[1]["source"]["data"], "AAAA")
    print()


def test_response_wrap():
    """Anthropic Message → OpenAI-shaped object with .choices[0].message.* and .usage.*"""
    from floki_anthropic_adapter import anthropic_to_oai_response
    fake_resp = SimpleNamespace(
        id="msg_123",
        model="claude-opus-4-6-20250514",
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="thinking..."),
            SimpleNamespace(type="tool_use", id="toolu_1", name="get_indicators", input={"tf": "H1"}),
        ],
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=50,
            cache_read_input_tokens=80, cache_creation_input_tokens=0,
        ),
    )
    wrapped = anthropic_to_oai_response(fake_resp)
    _expect("wrap: choices count", len(wrapped.choices), 1)
    _expect("wrap: finish_reason", wrapped.choices[0].finish_reason, "tool_calls")
    _expect("wrap: message.content", wrapped.choices[0].message.content, "thinking...")
    _expect("wrap: tool_calls count", len(wrapped.choices[0].message.tool_calls), 1)
    tc = wrapped.choices[0].message.tool_calls[0]
    _expect("wrap: tc.id", tc.id, "toolu_1")
    _expect("wrap: tc.function.name", tc.function.name, "get_indicators")
    _expect("wrap: tc.function.arguments", tc.function.arguments, '{"tf": "H1"}')
    _expect("wrap: usage.completion_tokens", wrapped.usage.completion_tokens, 50)
    _expect("wrap: usage.prompt_tokens_details.cached_tokens",
            wrapped.usage.prompt_tokens_details.cached_tokens, 80)
    print()


def test_cache_control():
    from floki_anthropic_adapter import system_with_cache, cache_last_tool
    sys_blocks = system_with_cache("YOU ARE FLOKI.")
    _expect("cache: sys is list", isinstance(sys_blocks, list), True)
    _expect("cache: sys block count", len(sys_blocks), 1)
    _expect("cache: sys cache_control", sys_blocks[0]["cache_control"]["type"], "ephemeral")
    _expect("cache: sys ttl 1h", sys_blocks[0]["cache_control"]["ttl"], "1h")
    tools = [{"name": "a", "description": "", "input_schema": {}},
             {"name": "b", "description": "", "input_schema": {}}]
    cached_tools = cache_last_tool(tools)
    _expect("cache: tool count unchanged", len(cached_tools), 2)
    _expect_truthy("cache: first tool no cache_control", "cache_control" not in cached_tools[0])
    _expect_truthy("cache: last tool has cache_control", "cache_control" in cached_tools[-1])
    print()


# ---------------------------------------------------------------------------
# Live API test
# ---------------------------------------------------------------------------

def test_live_api():
    print("--- LIVE Anthropic API call ---")
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("FAIL [live]: ANTHROPIC_API_KEY missing from env")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key, timeout=120)

    # Use the real SYSTEM_PROMPT to verify the actual wire format works
    from agent_prompts import SYSTEM_PROMPT
    print(f"[live] system_prompt len: {len(SYSTEM_PROMPT)} chars")

    # Minimal user message — just ask for a WAIT decision JSON, no plans needed
    user_msg = (
        "Cycle context: market data unavailable for this test. Author your "
        "decision JSON for this cycle. Return decision='WAIT' with confidence "
        "of your choice and a 1-line reasoning. Do NOT submit plans. Do not "
        "call any tools. Output ONLY the JSON object."
    )

    # Use a tiny tool list to verify the tool schema path even if the model
    # doesn't call it (tests cache_last_tool wiring).
    tools = [{
        "name": "get_current_price",
        "description": "Return current XAUUSD price.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    }]

    from floki_anthropic_adapter import call_anthropic_with_oai_kwargs

    # Build OpenAI-shaped kwargs the way ai_agent does
    oai_messages = [
        {"role": "system", "content": [{"type": "text", "text": SYSTEM_PROMPT,
                                         "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": user_msg},
    ]
    oai_tools_wire = [{"type": "function", "function": {
        "name": "get_current_price",
        "description": "Return current XAUUSD price.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }}]

    print("[live] calling Anthropic API (this takes ~10-30s)...")
    import time as _time
    t0 = _time.time()
    resp = call_anthropic_with_oai_kwargs(
        client,
        model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-6"),
        messages=oai_messages,
        tools=oai_tools_wire,
        max_completion_tokens=2048,
        temperature=1.0,
        timeout=120,
    )
    elapsed = _time.time() - t0
    print(f"[live] call returned in {elapsed:.1f}s")

    # Verify shape
    _expect("live: choices present", hasattr(resp, "choices") and len(resp.choices) >= 1, True)
    msg = resp.choices[0].message
    _expect("live: message.content present", isinstance(msg.content, str) and len(msg.content) > 0, True)
    print(f"[live] response.content[:300]: {(msg.content or '')[:300]!r}")
    print(f"[live] usage: in={resp.usage.prompt_tokens} out={resp.usage.completion_tokens} "
          f"cached={resp.usage.prompt_tokens_details.cached_tokens}")

    # Confirm response is JSON-parseable
    raw = msg.content or ""
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        print(f"FAIL [live]: response is not JSON: {raw[:300]!r}")
        sys.exit(1)
    parsed = json.loads(raw[start:end + 1])
    decision = parsed.get("decision")
    confidence = parsed.get("confidence")
    print(f"[live] parsed decision: {decision!r}  confidence: {confidence!r}")
    _expect_truthy("live: parsed has decision", decision is not None)
    print()
    print("LIVE TEST PASSED")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== FLO-419 Phase 2 — Anthropic adapter verification ===\n")
    print("[unit] tests")
    test_tools_conversion()
    test_messages_simple()
    test_messages_with_system_blocks()
    test_tool_call_roundtrip()
    test_image_conversion()
    test_response_wrap()
    test_cache_control()
    print("ALL UNIT TESTS PASSED\n")

    if "--live" in sys.argv:
        test_live_api()
    else:
        print("[skip] live API test (pass --live to run)")


if __name__ == "__main__":
    main()
