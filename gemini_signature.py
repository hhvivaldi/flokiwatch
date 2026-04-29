"""FLO-389 — Gemini 3 thought_signature capture-and-replay helpers.

Gemini 3 models require the encrypted reasoning blob (`thought_signature`)
returned with each tool_call to be echoed back on the assistant message in
the next turn, otherwise the API rejects the request with HTTP 400
"Function call is missing a thought_signature".

The OpenAI-compat endpoint surfaces the blob at
`message.tool_calls[i].extra_content.google.thought_signature` (Pydantic
extra-field on the SDK object). Per Google's docs, on parallel tool calls
the signature attaches to the FIRST tool_call only — that asymmetry is
preserved naturally here because each tc keeps whatever it received.

Two pure helpers:
  - `rebuild_assistant_message`: reconstruct the assistant turn as a dict
    while optionally carrying `extra_content` through. Provider-conditional.
  - `strip_thought_signatures`: scrub `extra_content` from prior turns
    before falling back to a non-Gemini provider (Qwen/OpenRouter), where
    the field is either ignored or rejected.

Both functions are pure on their inputs — dict-shaped output makes the
wire format explicit and removes any reliance on the OpenAI SDK's
serialization of Pydantic extras on ChatCompletionMessage objects.
"""
from __future__ import annotations

from typing import Any, List


def rebuild_assistant_message(
    msg: Any, tool_calls: list, *, preserve_signatures: bool
) -> dict:
    """Build the assistant message dict that follows a tool-call response.

    Args:
        msg: SDK ChatCompletionMessage (used only for `.content` passthrough).
        tool_calls: iterable of SDK ChatCompletionMessageToolCall objects
            (post any singleton-clamp / submit-decision filtering).
        preserve_signatures: when True, copy `tc.extra_content` through to
            the rebuilt dict. Set True only when the active provider is
            Gemini — `extra_content` is a Google-only OpenAI-compat
            extension and is dead weight (or worse, a 400) elsewhere.

    Returns:
        dict shaped for `client.chat.completions.create(messages=...)`.
        Always carries `role`/`tool_calls`; carries `content` only when
        the model emitted prose alongside tool_calls (rare today but
        reserved by the API contract).
    """
    out_tcs: List[dict] = []
    for tc in tool_calls:
        d: dict = {
            "id": tc.id,
            "type": "function",
            "function": {
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            },
        }
        if preserve_signatures:
            extra = getattr(tc, "extra_content", None)
            if extra:
                d["extra_content"] = extra
        out_tcs.append(d)

    out: dict = {"role": "assistant", "tool_calls": out_tcs}
    content = getattr(msg, "content", None)
    if content:
        out["content"] = content
    return out


def strip_thought_signatures(messages: list) -> list:
    """Return a shallow copy of `messages` with `extra_content` removed
    from every assistant `tool_calls` entry.

    Called immediately before a fallback request to a non-Gemini provider
    (Qwen/OpenRouter) so prior Gemini-supplied signatures don't leak onto
    a wire that doesn't accept them.

    Pure on input: original list and dicts are not mutated. Tool-call
    entries that aren't dicts (e.g. raw SDK objects, defensive case) are
    passed through unchanged — the OpenAI SDK's own serialization handles
    those, and stripping them safely would require deeper copying.
    """
    out: List[Any] = []
    for m in messages:
        if (
            isinstance(m, dict)
            and m.get("role") == "assistant"
            and m.get("tool_calls")
        ):
            stripped_tcs: List[Any] = []
            for tc in m["tool_calls"]:
                if isinstance(tc, dict):
                    stripped_tcs.append(
                        {k: v for k, v in tc.items() if k != "extra_content"}
                    )
                else:
                    stripped_tcs.append(tc)
            out.append({**m, "tool_calls": stripped_tcs})
        else:
            out.append(m)
    return out
