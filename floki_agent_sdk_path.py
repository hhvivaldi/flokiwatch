"""FLO-426 — Floki via claude-agent-sdk (subscription auth).

Parallel path to `floki_anthropic_adapter.call_anthropic_with_oai_kwargs`.
Routes Floki's planner through the Claude Code subprocess + custom MCP
tools, billing against the Max subscription pool instead of the
Anthropic API credit pool.

Public entry point — `decide_via_agent_sdk()` — returns the same dict
shape that `ai_agent._call_openai_with_tools` returns, so the existing
`_parse_response_with_retry` consumes it unchanged.

Pre-flight reference: `memory/project_agent_sdk_migration_preflight.md`.

KNOWN BEHAVIORAL DIFFS vs the direct-API path:
- 5-min cache TTL (no public knob), vs 1h on direct path.
- No FLO-420 chart pruning (charts ride history; ~+250k cache_read/cycle).
- Tool execution order is whatever the model emits. The FLO-409 action-tool
  priority sort (submit_plan_to_snow before cancel_plan within one batch)
  is NOT replicated here — the system prompt already nudges Floki to call
  submit_decision last, but if a batch contains a destructive + creative
  pair the destructive may run first. Re-evaluate after smoke tests.
- Cross-provider fallback (FLO-299 OpenRouter/Qwen) is not internal to
  the SDK. `ai_agent.decide()` catches SDK exceptions and routes back
  to the direct-API path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional


# --------------------------------------------------------------------------
# Schema conversion
# --------------------------------------------------------------------------

_JSON_TYPE_MAP = {
    "string": str,
    "number": float,
    "integer": int,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _props_to_sdk_schema(props: Dict[str, Any]) -> Dict[str, type]:
    """Convert JSON-Schema `properties` block to the {key: type} dict the SDK's
    @tool decorator wants. Nested object/array shapes degrade to dict/list;
    the model still receives the full description text via `description`."""
    out: Dict[str, type] = {}
    for k, v in (props or {}).items():
        t = (v or {}).get("type", "string")
        if isinstance(t, list):  # JSON schema allows multi-type
            t = next((x for x in t if x != "null"), "string")
        out[k] = _JSON_TYPE_MAP.get(t, str)
    return out


# --------------------------------------------------------------------------
# Tool factory
# --------------------------------------------------------------------------

def _build_tool_closure(
    tool_obj,                       # SdkMcpTool, returned by @tool(...)
    *,
    name: str,
    instance: Any,
    submit_state: Dict[str, Any],
    chart_state: Dict[str, Any],
    trace: List[Dict[str, Any]],
):
    """Unused in current design — see make_sdk_tools below for the in-line
    factory that uses the decorator directly."""
    raise NotImplementedError


def _result_to_mcp_content(result: Any) -> List[Dict[str, Any]]:
    """Serialise an AgentTools method return into MCP content blocks."""
    try:
        text = json.dumps(result, default=str)
    except Exception:
        text = str(result)
    return [{"type": "text", "text": text}]


def _chart_result_to_mcp_content(metadata: Dict[str, Any], chart_images: Dict[str, str]) -> List[Dict[str, Any]]:
    """get_chart_screenshots returns metadata + the caller has the actual
    base64 PNGs in `chart_images`. Emit one image block per available TF
    plus a final text block carrying the metadata `note` field."""
    blocks: List[Dict[str, Any]] = []
    if isinstance(metadata, dict) and metadata.get("success"):
        tfs = metadata.get("timeframes") or []
        for tf in tfs:
            key = f"{tf.lower()}_b64"
            b64 = chart_images.get(key)
            if not b64:
                continue
            # Some upstream paths store with `data:image/png;base64,` prefix; strip if present.
            if b64.startswith("data:"):
                _, _, b64 = b64.partition(",")
            blocks.append({"type": "image", "data": b64, "mimeType": "image/png"})
    blocks.append({"type": "text", "text": json.dumps(metadata, default=str)})
    return blocks


def make_sdk_tools(
    instance: Any,
    schemas: List[Dict[str, Any]],
    *,
    submit_state: Dict[str, Any],
    chart_state: Dict[str, Any],
    trace: List[Dict[str, Any]],
    submit_decision_schema: Optional[Dict[str, Any]] = None,
) -> List[Any]:
    """Build @tool closures bound to `instance` methods.

    schemas: list of Anthropic-style dicts {"name", "description", "input_schema"}
             (the shape `ai_agent._tool_schemas()` returns).
    submit_state: caller-owned dict that the submit_decision tool fills with
                  `args` and `called=True` so we can extract the final output.
    chart_state: dict with key "images" → the `_chart_images` mapping (so
                 get_chart_screenshots can attach real image bytes).
    trace: list the closures append `{name, input, result, latency_ms}` to.
    submit_decision_schema: OpenAI-format submit_decision tool def
                            (`SUBMIT_DECISION_TOOL`). Registered alongside
                            instance methods.
    """
    from claude_agent_sdk import tool  # local import — keeps module importable without SDK

    out: List[Any] = []

    # --- 1. submit_decision (loop terminator) -------------------------------
    if submit_decision_schema is not None:
        sd_fn = submit_decision_schema["function"]
        sd_props = sd_fn.get("parameters", {}).get("properties", {}) or {}
        sd_schema = _props_to_sdk_schema(sd_props)

        @tool("submit_decision", sd_fn["description"], sd_schema)
        async def _submit_decision(args):
            submit_state["called"] = True
            submit_state["args"] = dict(args or {})
            trace.append({
                "name": "submit_decision",
                "input": dict(args or {}),
                "result": {"acknowledged": True},
                "latency_ms": 0,
            })
            return {"content": [{"type": "text", "text": json.dumps({"acknowledged": True})}]}

        out.append(_submit_decision)

    # --- 2. instance methods -----------------------------------------------
    for s in schemas:
        name = s["name"]
        desc = s.get("description", "")
        props = (s.get("input_schema") or {}).get("properties", {}) or {}
        sdk_schema = _props_to_sdk_schema(props)

        method = getattr(instance, name, None)
        if method is None:
            # Schema lists a tool the AgentTools instance doesn't expose —
            # skip silently to mirror the existing dispatch's getattr fall-through.
            continue

        is_async = asyncio.iscoroutinefunction(method)
        is_chart_tool = (name == "get_chart_screenshots")

        out.append(_make_one(tool, name, desc, sdk_schema, method, is_async, is_chart_tool, chart_state, trace))

    return out


def _make_one(tool_decorator, name, desc, sdk_schema, method, is_async, is_chart_tool, chart_state, trace):
    """Per-tool closure scope. Captures method/name in a fresh frame so
    loop-variable rebinding doesn't collapse all closures onto the last tool."""

    @tool_decorator(name, desc, sdk_schema)
    async def _wrapped(args):
        t0 = time.time()
        try:
            call_args = dict(args or {})
            if is_async:
                result = await method(**call_args)
            else:
                result = method(**call_args)
        except Exception as e:
            err = {"success": False, "reason": f"exception: {type(e).__name__}: {e}"}
            trace.append({
                "name": name, "input": dict(args or {}),
                "result": err, "latency_ms": int((time.time() - t0) * 1000),
            })
            return {
                "content": [{"type": "text", "text": json.dumps(err)}],
                "is_error": True,
            }

        trace.append({
            "name": name, "input": dict(args or {}),
            "result": result, "latency_ms": int((time.time() - t0) * 1000),
        })

        if is_chart_tool:
            blocks = _chart_result_to_mcp_content(result, chart_state.get("images") or {})
        else:
            blocks = _result_to_mcp_content(result)
        return {"content": blocks}

    return _wrapped


# --------------------------------------------------------------------------
# System prompt — write once per process (Windows argv limit workaround)
# --------------------------------------------------------------------------

_SYSTEM_PROMPT_PATH: Optional[str] = None


def _ensure_system_prompt_file(system_text: str) -> str:
    """Write SYSTEM_PROMPT to disk so we can pass `--system-prompt-file`.
    Floki's prompt is 70k chars which exceeds Windows CreateProcess argv
    limit when passed inline."""
    global _SYSTEM_PROMPT_PATH
    if _SYSTEM_PROMPT_PATH and os.path.exists(_SYSTEM_PROMPT_PATH):
        # Update if changed (paranoid: hash compare avoided to keep cheap)
        try:
            with open(_SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
                if f.read() == system_text:
                    return _SYSTEM_PROMPT_PATH
        except Exception:
            pass
    path = os.path.abspath(os.path.join("data", "_floki_system_prompt.txt"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(system_text)
    _SYSTEM_PROMPT_PATH = path
    return path


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------

async def decide_via_agent_sdk(
    *,
    system_prompt: str,
    user_message: str,
    instance: Any,                      # AgentTools instance
    schemas: List[Dict[str, Any]],      # ai_agent._tool_schemas() output
    submit_decision_schema: Dict[str, Any],
    model: str,
    timeout: float,
    max_turns: int = 40,
    chart_images: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Run one Floki cycle through the Agent SDK.

    Returns the same dict shape as `_call_openai_with_tools`:
        {
            "content": <submit_decision args JSON str>,
            "input_tokens": int, "output_tokens": int,
            "model": str, "tool_trace": list,
        }
    """
    # Force subscription auth: mask the API key from the subprocess. Restored
    # in the `finally` so the rest of the bot keeps working if anything else
    # is still on the direct-API path (Rex, fallback, etc.).
    saved_api_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            TextBlock,
            ToolUseBlock,
            create_sdk_mcp_server,
        )

        submit_state: Dict[str, Any] = {"called": False, "args": None}
        chart_state: Dict[str, Any] = {"images": chart_images or {}}
        trace: List[Dict[str, Any]] = []
        last_text_parts: List[str] = []

        sdk_tools = make_sdk_tools(
            instance=instance,
            schemas=schemas,
            submit_state=submit_state,
            chart_state=chart_state,
            trace=trace,
            submit_decision_schema=submit_decision_schema,
        )

        # Allowed-tools list — must be the FULL mcp__name list so the SDK
        # exposes them to the model. Anything not allowed is hidden.
        allowed = ["mcp__floki__submit_decision"] + [
            f"mcp__floki__{s['name']}" for s in schemas
            if getattr(instance, s["name"], None) is not None
        ]

        server = create_sdk_mcp_server(name="floki", version="1.0.0", tools=sdk_tools)

        sp_path = _ensure_system_prompt_file(system_prompt)

        options = ClaudeAgentOptions(
            model=model,
            mcp_servers={"floki": server},
            allowed_tools=allowed,
            system_prompt={"type": "file", "path": sp_path},
            setting_sources=[],
            env={"ANTHROPIC_API_KEY": ""},
            max_turns=max_turns,
            # FLO-427 follow-up: 6 chart screenshots @ 86-103KB each base64
            # to ~800KB, which exceeds the SDK's default 1MB JSON wire-message
            # buffer. Bump to 8MB so chart-bearing tool results fit.
            max_buffer_size=8 * 1024 * 1024,
        )

        usage = {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0}
        result_model = model
        t0 = time.time()

        async def _run():
            async with ClaudeSDKClient(options=options) as client:
                await client.query(user_message)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        text_buf: List[str] = []
                        for b in msg.content:
                            if isinstance(b, TextBlock):
                                text_buf.append(b.text)
                            elif isinstance(b, ToolUseBlock):
                                # Tool execution happens inside the SDK; we log
                                # to trace from the @tool wrapper, not here.
                                pass
                        if text_buf:
                            last_text_parts.append(" ".join(text_buf))
                    elif isinstance(msg, ResultMessage):
                        u = getattr(msg, "usage", {}) or {}
                        usage["input"] = int(u.get("input_tokens", 0) or 0)
                        usage["output"] = int(u.get("output_tokens", 0) or 0)
                        usage["cache_create"] = int(u.get("cache_creation_input_tokens", 0) or 0)
                        usage["cache_read"] = int(u.get("cache_read_input_tokens", 0) or 0)

        await asyncio.wait_for(_run(), timeout=timeout)

        # Build the response dict. Priority for `content`:
        #   1. submit_decision args (the canonical path)
        #   2. final assistant text (fallback — _parse_response_with_retry will
        #      try to extract JSON from it)
        if submit_state.get("called") and submit_state.get("args") is not None:
            content_str = json.dumps(submit_state["args"], default=str)
        else:
            content_str = (last_text_parts[-1] if last_text_parts else "").strip()

        return {
            "content": content_str,
            "input_tokens": usage["input"] + usage["cache_create"] + usage["cache_read"],
            "output_tokens": usage["output"],
            "context_tokens": usage["cache_read"],
            "model": result_model,
            "tool_trace": trace,
            "_sdk_usage": dict(usage),
            "_sdk_elapsed_s": time.time() - t0,
            "_sdk_submit_called": bool(submit_state.get("called")),
        }
    finally:
        if saved_api_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_api_key
