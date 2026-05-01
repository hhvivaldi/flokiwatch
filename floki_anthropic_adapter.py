"""FLO-419 Phase 2 — Anthropic native SDK adapter for Floki.

Translates between the codebase's OpenAI-format kwargs/responses and Anthropic's
native messages API. Lets `ai_agent._sync_call_on` route to Anthropic without
rewriting the agentic loop or the messages list.

Why an adapter not a refactor: Floki's call surface (build messages OpenAI-style,
loop on tool_calls, append role=tool messages) is invested heavily in OpenAI's
shape. The agentic loop, the tool execution dispatch, the message-rebuild logic,
the OpenRouter fallback — all assume OpenAI format. Converting on the wire keeps
all of that working unchanged. The conversion is a pure function and tested in
isolation.

What this does:
  1. `convert_messages_oai_to_anthropic(messages)` — extract system, group
     consecutive role=tool messages into a single user{content=[tool_result...]}
     message, convert image_url to image source, convert assistant tool_calls
     to tool_use content blocks.
  2. `convert_tools_oai_to_anthropic(tools)` — drop the {type:function,
     function:{...}} wrapping; Anthropic uses {name, description, input_schema}
     directly. The codebase already authors schemas in this shape via
     `ai_agent._tool_schemas`, so this is mostly an unwrap.
  3. `apply_cache_control(...)` — add ephemeral cache_control breakpoints on
     the system content and on the last tool. With the extended-cache-ttl beta
     header, cached items live 1h — across Floki's 30-min cycle cadence.
  4. `anthropic_to_oai_response(resp)` — wrap Anthropic's content blocks +
     stop_reason + usage into a SimpleNamespace tree that quacks like
     `OpenAI().chat.completions.create()` output (.choices[0].message.{content,
     tool_calls}, .choices[0].finish_reason, .usage.prompt_tokens etc.).

Cache observability: cache_creation_input_tokens and cache_read_input_tokens
are surfaced in `usage.prompt_tokens_details.cached_tokens` for parity with the
existing FLO-296 Alibaba-cache logging path in ai_agent.py.

Tested standalone via `floki_anthropic_adapter_test.py` with a real Anthropic
API call against the live system prompt + a minimal user message before any
production wiring lands.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Tool schema conversion
# ---------------------------------------------------------------------------

def convert_tools_oai_to_anthropic(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Strip the OpenAI {type:function, function:{...}} wrapping. Anthropic
    accepts {name, description, input_schema} directly — same shape as the
    project's _tool_schemas authoring style."""
    if not tools:
        return []
    out: List[Dict[str, Any]] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            f = t["function"]
            schema = f.get("parameters") or {"type": "object", "properties": {}}
            out.append({
                "name": f.get("name", "unknown_tool"),
                "description": f.get("description", "") or "",
                "input_schema": schema,
            })
        else:
            # Already Anthropic-shaped — pass through
            anth = {
                "name": t.get("name", "unknown_tool"),
                "description": t.get("description", "") or "",
                "input_schema": t.get("input_schema") or t.get("parameters") or {"type": "object", "properties": {}},
            }
            out.append(anth)
    return out


# ---------------------------------------------------------------------------
# Messages conversion: OpenAI list → Anthropic (system, messages)
# ---------------------------------------------------------------------------

def _block_text(text: str) -> Dict[str, Any]:
    return {"type": "text", "text": text}


def _block_tool_use(tc: Any) -> Dict[str, Any]:
    """tc is OpenAI tool_call shape: {id, type:"function", function:{name, arguments}}.
    Accept both dict and pydantic-like attribute access (the codebase rebuilds
    assistant messages as dicts via _rebuild_assistant_message)."""
    if isinstance(tc, dict):
        tc_id = tc.get("id")
        fn = tc.get("function") or {}
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
        raw_args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", None)
    else:
        tc_id = getattr(tc, "id", None)
        fn = getattr(tc, "function", None)
        name = getattr(fn, "name", None) if fn else None
        raw_args = getattr(fn, "arguments", None) if fn else None
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    elif isinstance(raw_args, dict):
        args = raw_args
    else:
        args = {}
    return {
        "type": "tool_use",
        "id": tc_id or "",
        "name": name or "unknown_tool",
        "input": args,
    }


def _convert_image_url_block(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """OpenAI image_url block → Anthropic image source block.
    Accepts data URLs (data:image/png;base64,...) only (matches our chart-attach path)."""
    iu = block.get("image_url") or {}
    url = iu.get("url") if isinstance(iu, dict) else None
    if not url:
        return None
    if url.startswith("data:"):
        # data:<mime>;base64,<payload>
        try:
            header, payload = url.split(",", 1)
            media_type = header.split(";")[0].split(":", 1)[1]  # e.g. image/png
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": payload},
            }
        except Exception:
            return None
    # Anthropic supports URL sources too as of 2024-12; pass through as URL source.
    return {"type": "image", "source": {"type": "url", "url": url}}


def _convert_user_content(content: Any) -> Any:
    """Convert OpenAI user/system content (str or list of blocks) to Anthropic
    content list. Strings pass through (Anthropic accepts string content for
    plain text). Multimodal lists are translated block-by-block."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: List[Dict[str, Any]] = []
        for blk in content:
            if not isinstance(blk, dict):
                # Stringify unknown shape — defensive
                out.append({"type": "text", "text": str(blk)})
                continue
            t = blk.get("type")
            if t == "text":
                out.append({"type": "text", "text": blk.get("text", "")})
            elif t == "image_url":
                conv = _convert_image_url_block(blk)
                if conv is not None:
                    out.append(conv)
            elif t == "image":
                # Already Anthropic-shaped
                out.append(blk)
            else:
                # Unknown — preserve as text
                out.append({"type": "text", "text": json.dumps(blk, ensure_ascii=False, default=str)})
        return out if out else ""
    # Unknown — stringify
    return str(content)


def _content_to_str(content: Any) -> str:
    """Pull plain-text from OpenAI content (str or list)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                parts.append(str(blk.get("text", "")))
            elif isinstance(blk, str):
                parts.append(blk)
        return "\n".join(p for p in parts if p)
    return str(content)


def convert_messages_oai_to_anthropic(
    messages: List[Dict[str, Any]],
) -> Tuple[Any, List[Dict[str, Any]]]:
    """Return (system_content, anthropic_messages_list).

    Behavioural notes:
      - All system messages are extracted and joined into the system parameter
        (Anthropic doesn't allow system in the messages list).
      - Consecutive role=tool messages are GROUPED into a single user message
        with multiple tool_result blocks (Anthropic protocol). They follow
        the assistant message that emitted the tool_use blocks.
      - Assistant messages with tool_calls become content blocks: optional
        text + one tool_use per call. Plain assistant text becomes a single
        text block.
      - User messages with multimodal content (image_url) are converted to
        image source blocks. String content passes through unchanged.
    """
    system_parts: List[str] = []
    out: List[Dict[str, Any]] = []
    pending_tool_results: List[Dict[str, Any]] = []

    def _flush_pending_results():
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")

        if role == "system":
            system_parts.append(_content_to_str(msg.get("content")))
            continue

        if role == "tool":
            # Group with subsequent tool messages until we see anything else.
            tool_call_id = msg.get("tool_call_id") or ""
            content = msg.get("content")
            if isinstance(content, (dict, list)):
                content_str = json.dumps(content, ensure_ascii=False, default=str)
            else:
                content_str = str(content) if content is not None else ""
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_call_id,
                "content": content_str,
            })
            continue

        # Anything other than tool flushes pending tool_results into a user msg
        _flush_pending_results()

        if role == "assistant":
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []
            blocks: List[Dict[str, Any]] = []
            text = _content_to_str(content)
            if text:
                blocks.append(_block_text(text))
            for tc in tool_calls:
                blocks.append(_block_tool_use(tc))
            if not blocks:
                # Anthropic rejects empty content; emit a placeholder
                blocks.append(_block_text(""))
            out.append({"role": "assistant", "content": blocks})
            continue

        if role == "user":
            out.append({"role": "user", "content": _convert_user_content(msg.get("content"))})
            continue

        # Unknown role — best-effort treat as user text
        out.append({"role": "user", "content": _content_to_str(msg.get("content")) or ""})

    # Trailing tool messages (shouldn't happen, but defensive)
    _flush_pending_results()

    system_content = "\n\n".join(p for p in system_parts if p)
    return system_content, out


# ---------------------------------------------------------------------------
# Cache_control breakpoints
# ---------------------------------------------------------------------------

def system_with_cache(system_text: str, cache_ttl: str = "1h") -> Any:
    """Wrap the system text in a single content block with cache_control set.
    Anthropic accepts a string OR a list of content blocks for `system`.
    Use the list form to attach cache_control. cache_ttl="1h" requires the
    extended-cache-ttl-2025-04-11 beta header (set at call time).
    """
    if not system_text:
        return ""
    return [{
        "type": "text",
        "text": system_text,
        "cache_control": {"type": "ephemeral", "ttl": cache_ttl},
    }]


def cache_last_tool(tools: List[Dict[str, Any]], cache_ttl: str = "1h") -> List[Dict[str, Any]]:
    """Add cache_control to the last tool in the array. With ephemeral TTL,
    everything BEFORE the breakpoint (and including the breakpoint) is cached.
    Tool schemas are static across cycles → ideal cache target."""
    if not tools:
        return tools
    out = list(tools)
    out[-1] = dict(out[-1])  # don't mutate caller's dict
    out[-1]["cache_control"] = {"type": "ephemeral", "ttl": cache_ttl}
    return out


# ---------------------------------------------------------------------------
# Response wrapping: Anthropic → OpenAI shape
# ---------------------------------------------------------------------------

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "pause_turn": "stop",
}


def _make_tool_call(idx: int, block: Any) -> SimpleNamespace:
    """Build an OpenAI-shaped tool_call object from an Anthropic tool_use block."""
    block_id = getattr(block, "id", None) or (block.get("id") if isinstance(block, dict) else None) or f"tc_{idx}"
    name = getattr(block, "name", None) or (block.get("name") if isinstance(block, dict) else None) or "unknown_tool"
    raw_input = getattr(block, "input", None) if not isinstance(block, dict) else block.get("input")
    if raw_input is None:
        raw_input = {}
    args_str = json.dumps(raw_input, ensure_ascii=False, default=str)
    function = SimpleNamespace(name=name, arguments=args_str)
    return SimpleNamespace(id=block_id, type="function", function=function, index=idx)


def anthropic_to_oai_response(resp: Any) -> Any:
    """Wrap an anthropic.types.Message into an OpenAI-shaped object so the
    rest of the codebase (.choices[0].message.{content,tool_calls},
    .choices[0].finish_reason, .usage.prompt_tokens) works unchanged."""
    # Extract text and tool_use blocks
    content_text_parts: List[str] = []
    tool_calls: List[SimpleNamespace] = []
    blocks = getattr(resp, "content", []) or []
    for i, block in enumerate(blocks):
        block_type = getattr(block, "type", None) if not isinstance(block, dict) else block.get("type")
        if block_type == "text":
            text_val = getattr(block, "text", None) if not isinstance(block, dict) else block.get("text")
            if text_val:
                content_text_parts.append(text_val)
        elif block_type == "tool_use":
            tool_calls.append(_make_tool_call(len(tool_calls), block))
        # ignore other block types (thinking, etc.)

    content_str = "".join(content_text_parts) if content_text_parts else None
    finish_reason = _STOP_REASON_MAP.get(getattr(resp, "stop_reason", "") or "", "stop")

    message = SimpleNamespace(
        role="assistant",
        content=content_str,
        tool_calls=tool_calls if tool_calls else None,
    )
    choice = SimpleNamespace(
        index=0,
        message=message,
        finish_reason=finish_reason,
    )

    # Usage: include cache fields under prompt_tokens_details for FLO-296 parity
    usage_obj = getattr(resp, "usage", None)
    in_tok = getattr(usage_obj, "input_tokens", 0) if usage_obj else 0
    out_tok = getattr(usage_obj, "output_tokens", 0) if usage_obj else 0
    cache_read = getattr(usage_obj, "cache_read_input_tokens", 0) if usage_obj else 0
    cache_create = getattr(usage_obj, "cache_creation_input_tokens", 0) if usage_obj else 0
    prompt_details = SimpleNamespace(cached_tokens=int(cache_read or 0))
    usage = SimpleNamespace(
        prompt_tokens=int(in_tok or 0) + int(cache_read or 0) + int(cache_create or 0),
        completion_tokens=int(out_tok or 0),
        total_tokens=int(in_tok or 0) + int(out_tok or 0) + int(cache_read or 0) + int(cache_create or 0),
        prompt_tokens_details=prompt_details,
        # surfaced extras for FLO-296 logging
        cache_read_input_tokens=int(cache_read or 0),
        cache_creation_input_tokens=int(cache_create or 0),
    )

    return SimpleNamespace(
        id=getattr(resp, "id", None),
        model=getattr(resp, "model", None),
        choices=[choice],
        usage=usage,
        # original Anthropic response for opt-in deep inspection
        _raw_anthropic=resp,
    )


# ---------------------------------------------------------------------------
# Top-level call wrapper
# ---------------------------------------------------------------------------

def call_anthropic_with_oai_kwargs(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    max_completion_tokens: int = 4096,
    temperature: float = 1.0,
    timeout: Optional[int] = None,
    cache_ttl: str = "1h",
    **_ignored,
) -> Any:
    """Single-call entry point. Accepts OpenAI-shaped kwargs (the kwargs dict
    `_sync_call_on` already builds), executes the Anthropic API call with
    cache_control + 1h cache TTL, returns an OpenAI-shaped response.

    Unknown kwargs from the OpenAI surface (`reasoning_effort`,
    `response_format`, etc.) are silently ignored — they don't apply to
    Anthropic and the system prompt already mandates JSON output.
    """
    system_text, anth_messages = convert_messages_oai_to_anthropic(messages)
    anth_tools = convert_tools_oai_to_anthropic(tools)
    if anth_tools:
        anth_tools = cache_last_tool(anth_tools, cache_ttl=cache_ttl)
    system_param = system_with_cache(system_text, cache_ttl=cache_ttl) if system_text else None

    create_kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_completion_tokens) if max_completion_tokens else 4096,
        "messages": anth_messages,
    }
    if system_param is not None:
        create_kwargs["system"] = system_param
    if anth_tools:
        create_kwargs["tools"] = anth_tools
    # Anthropic accepts temperature 0..1 (sometimes >1 with sampling); OpenAI
    # path used 1.0. Map directly.
    create_kwargs["temperature"] = float(temperature)
    if timeout is not None:
        create_kwargs["timeout"] = timeout
    # Beta headers — both flags to unblock any dashboard detector that
    # keys on the older prompt-caching opt-in. Base caching has been GA
    # for a while and works without the header, but the Anthropic
    # console's "you're not using prompt caching" indicator was firing
    # post-restart even though cache_read_input_tokens > 0 in the
    # responses. Belt + suspenders: include both betas.
    create_kwargs["extra_headers"] = {
        "anthropic-beta": "prompt-caching-2024-07-31,extended-cache-ttl-2025-04-11",
    }

    resp = client.messages.create(**create_kwargs)
    return anthropic_to_oai_response(resp)
