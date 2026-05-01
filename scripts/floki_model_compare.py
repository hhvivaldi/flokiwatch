"""FLO-419 Phase 2 — Floki model-comparison harness (Path B, text-only).

CEO directive 2026-05-01: compare which LLM authors the best plans
given identical inputs.

Usage:
    python scripts/floki_model_compare.py --latest
    python scripts/floki_model_compare.py --cycle 2026-05-01T15:46:50Z
    python scripts/floki_model_compare.py --latest --models gemini,claude

What this does (Path B):
    1. Reads one cycle from agent_proactive_analyses (latest by default).
    2. Reconstructs the user-message from the cycle's tool_trace —
       each tool call's result formatted as readable text. NO live tools
       are exposed to the test models. NO chart images are attached
       (Path A would do that; v1 ships text-only because chart bytes
       are in-memory only at runtime, not on disk).
    3. Calls each model with the SAME current SYSTEM_PROMPT plus the
       reconstructed user message. response_format=json_object where
       supported.
    4. Saves one JSON per model to data/_audits/model_compare/
       and a side-by-side markdown summary.

What this is NOT (yet):
    - Path A (full agentic loop with tool replay) — that's a follow-up.
    - Live or shadow trading — these calls only ever produce JSON; nothing
      reaches Snow / executor / MT5.
    - A correctness oracle — comparing 4 reasoning samples to each other,
      not to ground truth. CEO judges quality by reading the outputs.

Models (default set):
    - gemini-3.1-pro-preview   (current Floki primary)
    - gpt-5.5                  (OpenAI flagship)
    - gpt-5.4                  (OpenAI prior)
    - claude-opus-4-7          (Anthropic flagship)

Cost (approx, full 4-model run on one cycle):
    - Gemini 3 Pro:   ~$0.30
    - GPT-5.4 / 5.5:  ~$0.30-0.50 each
    - Claude Opus:    ~$3.50-5
    - Total:          ~$5-7 per cycle compared
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow running from anywhere in repo
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Load .env so OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY are present
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(ROOT / ".env")
except Exception:
    pass

OUT_DIR = ROOT / "data" / "_audits" / "model_compare"
OUT_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = ROOT / "data" / "history.db"

# ---------------------------------------------------------------------------
# Cycle loading
# ---------------------------------------------------------------------------

def load_cycle(timestamp: Optional[str] = None) -> Dict[str, Any]:
    """Load one agent_proactive_analyses row. timestamp=None → latest.
    timestamp can be exact-ISO or a prefix (e.g. '2026-05-01T15:46')."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if timestamp:
            row = conn.execute(
                "SELECT * FROM agent_proactive_analyses WHERE timestamp LIKE ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (timestamp + "%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM agent_proactive_analyses ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        if not row:
            raise SystemExit(f"No cycle found for timestamp filter: {timestamp!r}")
        return dict(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# User message reconstruction from tool_trace
# ---------------------------------------------------------------------------

# Tools whose results we render verbatim. Some are noisy and we trim;
# any tool not listed here is rendered with its name + truncated result.
_RENDER_TOOLS_FULL = {
    "list_active_plans",
    "get_open_positions",
    "get_market_regime",
    "get_sr_zones",
    "get_indicators",
    "get_fibonacci_levels",
    "get_pivot_points",
    "get_chart_patterns",
    "get_tick_pressure",
    "get_volume_profile",
    "get_session_context",
    "get_market_context",
    "get_macro",
    "get_calendar",
    "get_headlines",
    "get_echo_alerts",
    "get_luna_brief",
    "get_analyst_research",
    "get_rex_monitor",
    "get_ml_prediction",
    "get_account_info",
    "get_trade_history",
    "get_trade_lessons",
    "get_recent_reflexions",
    "read_session_memory",
    "get_current_price",
    "get_candles",
    "get_snow_recipe_book",
    "get_snow_primitives_reference",
}

_TRUNCATE_LIMIT = 8000  # per-tool result text cap to keep total token count sane


def _format_tool_call(call: Dict[str, Any]) -> str:
    name = call.get("name", "?")
    inp = call.get("input") or {}
    res = call.get("result")
    inp_str = json.dumps(inp, ensure_ascii=False) if inp else "{}"
    res_str = json.dumps(res, ensure_ascii=False, default=str) if res is not None else "null"
    if len(res_str) > _TRUNCATE_LIMIT:
        res_str = res_str[:_TRUNCATE_LIMIT] + f"... [truncated, full size {len(res_str)}]"
    return f"=== {name} ===\ninput: {inp_str}\nresult: {res_str}"


def _build_user_message(cycle: Dict[str, Any]) -> str:
    """Reconstruct the cycle's user-side payload from tool_trace.
    Excludes write tools (submit_plan_to_snow, cancel_plan, set_next_check)
    so the comparison models don't think those calls already happened —
    they're being asked to author from scratch.
    """
    SKIP_TOOL_NAMES = {"submit_plan_to_snow", "cancel_plan", "set_next_check",
                       "submit_decision", "write_session_memory", "write_trading_journal",
                       "acknowledge_boss_notes", "place_pending_order"}
    try:
        trace = json.loads(cycle.get("tool_trace") or "[]")
    except Exception:
        trace = []

    blocks: List[str] = []
    blocks.append(
        f"# Cycle context replay\n"
        f"# Original cycle timestamp: {cycle.get('timestamp')}\n"
        f"# Original Floki decision: {cycle.get('agent_decision')} "
        f"({cycle.get('agent_confidence')}%)\n"
        f"# Original model: {cycle.get('model')}\n"
        f"# This is a comparison test — ignore the original decision; author your own.\n"
    )
    blocks.append("Below are the tool results that Floki saw on this cycle. They are the "
                  "ONLY market data available to you for this comparison. Author your decision "
                  "JSON based on this data and the system prompt's PLAN-AUTHORING DISCIPLINE.\n")

    n_tools = 0
    for call in trace:
        nm = call.get("name", "")
        if nm in SKIP_TOOL_NAMES:
            continue
        if nm not in _RENDER_TOOLS_FULL and not nm.startswith("get_"):
            # Skip unknown tools (mostly write/no-op)
            continue
        blocks.append(_format_tool_call(call))
        n_tools += 1

    blocks.append(
        "\n# YOUR TASK\n"
        "Author your decision JSON exactly as defined in the system prompt. "
        "Output ONLY the JSON object — no prose, no markdown fences. Start with '{' end with '}'. "
        "Apply ALL four PLAN-AUTHORING DISCIPLINE checks in confidence_reason for any plans you submit. "
        "Note: in this comparison test, plans you 'submit' do not actually fire — your output is captured "
        "as JSON for human review, not dispatched to Snow. Author with the same rigor as production."
    )
    msg = "\n\n".join(blocks)
    return msg


# ---------------------------------------------------------------------------
# Model adapters
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    """Find the first {...} block and parse, with trailing-comma repair fallback."""
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    clean = raw[start:end + 1]
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        import re as _re
        repaired = _re.sub(r",\s*([}\]])", r"\1", clean)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            return None


def _call_openai_compat(api_key: str, base_url: str, model: str,
                        system_prompt: str, user_msg: str,
                        reasoning_effort: Optional[str] = None,
                        max_completion_tokens: int = 16384,
                        timeout: int = 300) -> Dict[str, Any]:
    """OpenAI-compatible call (works for Gemini compat layer + OpenAI proper)."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "max_completion_tokens": max_completion_tokens,
        "temperature": 1.0,
        "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    t0 = time.time()
    try:
        resp = client.chat.completions.create(**kwargs)
        latency_ms = int((time.time() - t0) * 1000)
        choice = resp.choices[0]
        raw = choice.message.content or ""
        return {
            "ok": True,
            "raw_response": raw,
            "parsed_json": _extract_json(raw),
            "latency_ms": latency_ms,
            "input_tokens": getattr(resp.usage, "prompt_tokens", None) if resp.usage else None,
            "output_tokens": getattr(resp.usage, "completion_tokens", None) if resp.usage else None,
            "finish_reason": choice.finish_reason,
            "error": None,
        }
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "raw_response": "", "parsed_json": None,
                "latency_ms": latency_ms, "input_tokens": None,
                "output_tokens": None, "finish_reason": None,
                "error": f"{type(e).__name__}: {e}"}


def _call_anthropic(api_key: str, model: str, system_prompt: str, user_msg: str,
                    max_tokens: int = 16384, timeout: int = 300) -> Dict[str, Any]:
    """Anthropic native call. Claude doesn't accept response_format=json_object,
    but the system prompt already mandates JSON output."""
    from anthropic import Anthropic
    client = Anthropic(api_key=api_key, timeout=timeout)
    t0 = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
            temperature=1.0,
        )
        latency_ms = int((time.time() - t0) * 1000)
        # Concatenate text parts (Claude can return multiple content blocks)
        raw = "".join(
            block.text for block in resp.content
            if getattr(block, "type", "") == "text"
        )
        return {
            "ok": True,
            "raw_response": raw,
            "parsed_json": _extract_json(raw),
            "latency_ms": latency_ms,
            "input_tokens": getattr(resp.usage, "input_tokens", None),
            "output_tokens": getattr(resp.usage, "output_tokens", None),
            "finish_reason": resp.stop_reason,
            "error": None,
        }
    except Exception as e:
        latency_ms = int((time.time() - t0) * 1000)
        return {"ok": False, "raw_response": "", "parsed_json": None,
                "latency_ms": latency_ms, "input_tokens": None,
                "output_tokens": None, "finish_reason": None,
                "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Per-model recipes
# ---------------------------------------------------------------------------

MODEL_REGISTRY = {
    "gemini": {
        "model": os.environ.get("COMPARE_GEMINI_MODEL", "gemini-3.1-pro-preview"),
        "vendor": "openai_compat",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GEMINI_API_KEY",
        "reasoning_effort": "high",
    },
    "gpt-5-5": {
        "model": os.environ.get("COMPARE_GPT55_MODEL", "gpt-5.5"),
        "vendor": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "reasoning_effort": "high",
    },
    "gpt-5-4": {
        "model": os.environ.get("COMPARE_GPT54_MODEL", "gpt-5.4"),
        "vendor": "openai_compat",
        "base_url": "https://api.openai.com/v1",
        "api_key_env": "OPENAI_API_KEY",
        "reasoning_effort": "high",
    },
    "claude": {
        "model": os.environ.get("COMPARE_CLAUDE_MODEL", "claude-opus-4-7"),
        "vendor": "anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
}


def run_model(label: str, recipe: Dict[str, Any],
              system_prompt: str, user_msg: str) -> Dict[str, Any]:
    api_key = os.environ.get(recipe["api_key_env"], "")
    if not api_key:
        return {"ok": False, "error": f"missing env {recipe['api_key_env']}"}
    if recipe["vendor"] == "anthropic":
        result = _call_anthropic(api_key, recipe["model"], system_prompt, user_msg)
    else:
        result = _call_openai_compat(
            api_key=api_key,
            base_url=recipe["base_url"],
            model=recipe["model"],
            system_prompt=system_prompt,
            user_msg=user_msg,
            reasoning_effort=recipe.get("reasoning_effort"),
        )
    result["label"] = label
    result["model"] = recipe["model"]
    return result


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_run(label: str, cycle_ts: str, result: Dict[str, Any]) -> Path:
    safe_ts = cycle_ts.replace(":", "-").replace(".", "-")
    fname = f"{safe_ts}__{label}.json"
    out = OUT_DIR / fname
    out.write_text(
        json.dumps({
            "cycle_timestamp": cycle_ts,
            "label": label,
            **result,
        }, indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    return out


def write_summary(cycle_ts: str, results: List[Dict[str, Any]]) -> Path:
    safe_ts = cycle_ts.replace(":", "-").replace(".", "-")
    md_path = OUT_DIR / f"{safe_ts}__SUMMARY.md"

    lines = [f"# Model comparison — cycle `{cycle_ts}`", ""]
    lines.append("| label | model | ok | latency | in tok | out tok | finish | decision | conf | plans | error |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        pj = r.get("parsed_json") or {}
        decision = pj.get("decision", "—") if isinstance(pj, dict) else "—"
        conf = pj.get("confidence", "—") if isinstance(pj, dict) else "—"
        # rough plan count: count submit_plan_to_snow tool_calls if present, else look for 'plans' in JSON
        plan_count = "—"
        if isinstance(pj, dict):
            plans = pj.get("plans") or pj.get("trade_plan")
            if isinstance(plans, list):
                plan_count = str(len(plans))
            elif plans:
                plan_count = "1"
        err = (r.get("error") or "").split("\n")[0][:60] if r.get("error") else ""
        lines.append(
            f"| {r['label']} | {r.get('model','?')} | "
            f"{'✓' if r.get('ok') else '✗'} | "
            f"{r.get('latency_ms','?')}ms | "
            f"{r.get('input_tokens','?')} | "
            f"{r.get('output_tokens','?')} | "
            f"{r.get('finish_reason','?')} | "
            f"{decision} | {conf} | {plan_count} | {err} |"
        )

    lines.append("")
    lines.append("## Per-model output")
    for r in results:
        lines.append(f"\n### {r['label']} (`{r.get('model','?')}`)\n")
        if not r.get("ok"):
            lines.append(f"**ERROR:** {r.get('error')}\n")
            continue
        pj = r.get("parsed_json") or {}
        if isinstance(pj, dict):
            for k in ("decision", "confidence", "reasoning", "key_factors", "concerns"):
                v = pj.get(k)
                if v is None:
                    continue
                if isinstance(v, (list, dict)):
                    lines.append(f"**{k}**: ```json\n{json.dumps(v, indent=2, ensure_ascii=False)}\n```")
                else:
                    lines.append(f"**{k}**: {v}")
            lines.append("")
        else:
            lines.append("(no JSON parsed; see raw_response in the per-model artifact)")
        raw = r.get("raw_response") or ""
        if raw:
            preview = raw[:1500]
            lines.append(f"\n<details><summary>raw response (first 1500 chars)</summary>\n\n```\n{preview}\n```\n</details>\n")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Floki model-comparison harness (Path B)")
    p.add_argument("--cycle", help="Cycle timestamp prefix (e.g. 2026-05-01T15:46). Default: latest.", default=None)
    p.add_argument("--latest", action="store_true", help="Use the most recent cycle (default behaviour).")
    p.add_argument("--models", default="gemini,gpt-5-5,gpt-5-4,claude",
                   help="Comma-separated subset of MODEL_REGISTRY keys.")
    p.add_argument("--max-input-chars", type=int, default=400_000,
                   help="Hard cap on user-message size (chars). Default 400k ≈ 100k tokens.")
    p.add_argument("--dry-run", action="store_true",
                   help="Build the user message and print sizes; do NOT call any model.")
    args = p.parse_args()

    print(f"[1/4] Loading cycle (filter={args.cycle or 'LATEST'})...")
    cycle = load_cycle(args.cycle)
    cycle_ts = cycle["timestamp"]
    print(f"      cycle: {cycle_ts}  decision={cycle.get('agent_decision')} "
          f"conf={cycle.get('agent_confidence')}  model={cycle.get('model')}")

    print("[2/4] Loading SYSTEM_PROMPT from agent_prompts.py...")
    from agent_prompts import SYSTEM_PROMPT
    print(f"      system_prompt len: {len(SYSTEM_PROMPT)} chars")

    print("[3/4] Reconstructing user message from tool_trace...")
    user_msg = _build_user_message(cycle)
    print(f"      user_msg len: {len(user_msg)} chars")
    if len(user_msg) > args.max_input_chars:
        print(f"      WARNING: exceeds --max-input-chars={args.max_input_chars}; truncating")
        user_msg = user_msg[:args.max_input_chars] + "\n\n[... TRUNCATED ...]"

    if args.dry_run:
        # Persist the reconstructed input so CEO can review what models will see
        snap = {
            "cycle_timestamp": cycle_ts,
            "system_prompt_len": len(SYSTEM_PROMPT),
            "user_message_len": len(user_msg),
            "user_message_preview": user_msg[:5000],
        }
        out = OUT_DIR / f"{cycle_ts.replace(':','-').replace('.','-')}__INPUT_PREVIEW.json"
        out.write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\n--dry-run: saved input preview to {out}")
        return

    labels = [s.strip() for s in args.models.split(",") if s.strip()]
    print(f"[4/4] Calling {len(labels)} models: {labels}")
    results: List[Dict[str, Any]] = []
    for label in labels:
        if label not in MODEL_REGISTRY:
            print(f"      SKIP {label}: not in MODEL_REGISTRY")
            continue
        recipe = MODEL_REGISTRY[label]
        print(f"      → {label} ({recipe['model']}) ...", end="", flush=True)
        result = run_model(label, recipe, SYSTEM_PROMPT, user_msg)
        out_file = save_run(label, cycle_ts, result)
        if result.get("ok"):
            pj = result.get("parsed_json") or {}
            dec = pj.get("decision", "?") if isinstance(pj, dict) else "?"
            print(f" OK | {result.get('latency_ms')}ms | {dec} | "
                  f"{result.get('input_tokens')}+{result.get('output_tokens')} tok | {out_file.name}")
        else:
            print(f" FAIL | {result.get('error')}")
        results.append(result)

    summary = write_summary(cycle_ts, results)
    print(f"\nSummary: {summary}")
    print(f"Per-model artifacts in: {OUT_DIR}")


if __name__ == "__main__":
    main()
