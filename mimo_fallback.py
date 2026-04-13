"""
MiMo → Gemini Flash fallback — FLO-294.

Why this exists: La Liga anti-piracy IP blocking on Spanish residential IPs
periodically returns HTTP 451 (Unavailable For Legal Reasons) from the MiMo
API gateway during football matches. Echo and Luna both depend on MiMo as
their primary LLM. We don't want to drop straight to deterministic local
fallback — Gemini Flash gives much higher-quality output for the same
prompts/format and is unaffected by the IP block.

Fallback order: MiMo (primary) → Gemini Flash (secondary) → local (last resort)

Cooldown: in-process dict, no persistence. On bot restart, MiMo gets one
free retry (which is the right behavior — the block may have lifted).
"""
import json
import os
import time
from typing import Any, Dict, Optional

from logger import log


_COOLDOWN: Dict[str, float] = {}  # agent_name -> unix timestamp when cooldown ends
_DEFAULT_COOLDOWN_MIN = 15
_GEMINI_MODEL = "gemini-3-flash-preview"


def is_in_cooldown(agent: str) -> bool:
    """True if MiMo should be skipped for this agent right now."""
    until = _COOLDOWN.get(agent, 0.0)
    return time.time() < until


def set_cooldown(agent: str, minutes: int = _DEFAULT_COOLDOWN_MIN, reason: str = "") -> None:
    """Mark MiMo as unavailable for `minutes` for this agent."""
    _COOLDOWN[agent] = time.time() + minutes * 60
    log.warning(
        f"{agent.upper()} | MiMo cooldown set for {minutes}min "
        f"— switching to Gemini Flash fallback ({reason})"
    )


def clear_cooldown_if_set(agent: str) -> None:
    """Clear cooldown after a successful MiMo call. Logs only if was set."""
    if agent in _COOLDOWN:
        _COOLDOWN.pop(agent, None)
        log.info(f"{agent.upper()} | MiMo recovered — switching back to primary")


def is_451_error(exc: BaseException) -> bool:
    """Detect the La Liga 451 block in any exception string."""
    s = str(exc)
    return "451" in s and ("Unavailable" in s or "Cross-border" in s or "MiFE" in s)


def call_gemini_json(
    system: str,
    user_text: str,
    *,
    agent: str,
    model: Optional[str] = None,
    timeout_s: int = 30,
    max_output_tokens: int = 2048,
) -> Optional[Dict[str, Any]]:
    """Call Gemini Flash with a system + user prompt and parse JSON response.

    Same contract as MiMo's `chat.completions.create(response_format=json_object)`:
    returns a parsed dict on success, None on any failure. Never raises.
    """
    api_key = (os.environ.get("GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        log.warning(f"{agent.upper()} | Gemini fallback unavailable: no GEMINI_API_KEY")
        return None

    try:
        from google import genai
        from google.genai import types as genai_types
    except Exception as e:
        log.warning(f"{agent.upper()} | Gemini SDK not installed ({e}) — fallback unavailable")
        return None

    use_model = model or _GEMINI_MODEL
    t0 = time.time()
    try:
        client = genai.Client(api_key=api_key)
        # Combine system + user as two user parts (Gemini doesn't have a
        # dedicated "system" role in generate_content). The first part acts
        # as a system instruction for the model.
        cfg = genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        )
        resp = client.models.generate_content(
            model=use_model,
            contents=[
                {"role": "user", "parts": [{"text": system}]},
                {"role": "user", "parts": [{"text": user_text}]},
            ],
            config=cfg,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            log.warning(f"{agent.upper()} | Gemini returned empty response ({elapsed_ms}ms)")
            return None
        # Defensive: strip ```json fences if the model added them
        if text.startswith("```"):
            text = text.split("```", 2)[1] if "```" in text[3:] else text.lstrip("`")
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip().rstrip("`").strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            log.warning(f"{agent.upper()} | Gemini returned invalid JSON ({elapsed_ms}ms): {e}")
            return None
        log.info(f"{agent.upper()} | Gemini fallback OK ({elapsed_ms}ms, model={use_model})")
        return parsed
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        log.error(f"{agent.upper()} | Gemini fallback FAILED ({elapsed_ms}ms): {e}")
        return None
