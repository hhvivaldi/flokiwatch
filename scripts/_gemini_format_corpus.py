"""FLO-408 Phase 1 — real-Gemini format corpus harness.

CEO directive 2026-04-30: stop incremental Gemini fixes. Make REAL
Gemini API calls offline, capture every emitted shape, auto-diff
against the Pydantic schema, then design a comprehensive normalizer
in one commit (Phase 2) based on the captured corpus.

This script is the Phase 1 deliverable. It:

  1. Imports the production system prompt (agent_prompts.SYSTEM_PROMPT)
     and tool schemas (AIAgent._openai_tools) verbatim — the same
     wire-format the bot would send.
  2. Sends 5 representative trigger archetypes to Gemini:
        * range_bound          — price mid-range, both directions valid
        * trending_bearish     — H4/D1 bearish + countertrend bounce candidate
        * trending_bullish     — uptrend + breakout setup
        * volatile_news        — pre-news high vol, narrow window
        * multi_scenario       — three distinct paths at once
  3. For each archetype, runs N attempts (default 2) so the corpus
     captures Gemini's variance across the same context.
  4. Captures EVERY tool_call emitted (raw arguments string, parsed
     dict, the tool name, the run id).
  5. For submit_plan_to_snow specifically, runs the captured args
     through:
        * snow.validator.validate_plan (pre-FLO-400-decoder + post)
        * agent_tools.AgentTools._scan_null_object_paths (Layer B)
     and records every divergence.
  6. Auto-aggregates findings into structured patterns
     (e.g. "X of Y submissions emitted null at entry.conditions[*]").
  7. Writes the full corpus + findings to
     data/_audits/gemini_format_corpus_<timestamp>.json.

Cost: 5 triggers × N runs × ~1-3 tool_call rounds per attempt = ≤30
Gemini API calls per harness run. Safe within the 250 RPD budget.

Usage:
    python scripts/_gemini_format_corpus.py --runs 2
    python scripts/_gemini_format_corpus.py --runs 1 --triggers range_bound,trending_bearish

Dev box only — DO NOT run from production bot host (would burn the
shared Gemini quota the bot needs).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow imports from project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()


# =============================================================================
# Trigger archetypes — minimal user-message payloads that motivate Floki to
# author plans without requiring the full mandatory tool suite to be called
# (would need MT5 + tool fixtures we don't have offline). Each archetype
# describes the "post-suite" state explicitly so Gemini can go straight to
# submit_plan_to_snow.
# =============================================================================


_TRIGGERS: Dict[str, str] = {
    "range_bound": (
        "Cycle context (post-suite, all 10 mandatory tools already called):\n"
        "Price: bid=4540.5 ask=4540.7 mid=4540.6\n"
        "Regime: RANGING (ADX 18, ATR 12 pips, last 24h range 4525-4555)\n"
        "Active Snow plans: 0\n"
        "Open positions: 0\n"
        "Key levels: H1 support 4525, H1 resistance 4555, M15 mid 4540\n"
        "Indicators: H1 RSI 52, M5 RSI 48, MACD flat, EMA stack neutral\n"
        "Recipe Book consulted: range\n\n"
        "Submit Snow plan(s) covering the cycle's deliverable. Multiple "
        "plans are valid for distinct scenarios — see <plans> CONCURRENT "
        "PLANS rule. Each plan must be a complete, schema-valid dict."
    ),
    "trending_bearish": (
        "Cycle context (post-suite):\n"
        "Price: bid=4565.0 ask=4565.2 mid=4565.1\n"
        "Regime: TRENDING_BEARISH (ADX 32, M15 lower-highs, H4 EMA stack bearish)\n"
        "Active Snow plans: 0\n"
        "Open positions: 0\n"
        "Key levels: H1 resistance 4571/4581, H1 support 4553/4543, "
        "D1 floor 4512\n"
        "Indicators: H1 RSI 48 (room down), M5 RSI 62 reclaiming "
        "(local momentum), MACD H1 hist -0.6 rising\n"
        "Recipe Book consulted: trend\n\n"
        "Submit Snow plan(s). Note the with-trend SELL setup AND the "
        "potential countertrend BUY at 4543/4553 reclaim — cover both "
        "if structurally distinct."
    ),
    "trending_bullish": (
        "Cycle context (post-suite):\n"
        "Price: bid=4710.0 ask=4710.2 mid=4710.1\n"
        "Regime: TRENDING_BULLISH (ADX 28, H1 EMA9 > 21 > 50, "
        "M5 full bullish stack)\n"
        "Active Snow plans: 0\n"
        "Open positions: 0\n"
        "Key levels: H1 resistance 4715/4720, H1 support 4700/4690, "
        "D1 ceiling 4735\n"
        "Indicators: H1 RSI 64 (room up), M5 RSI 70, MACD H1 hist "
        "+1.2 rising, M15 ADX 28 rising\n"
        "Recipe Book consulted: trend\n\n"
        "Submit Snow plan(s) for the breakout setup above 4715, plus "
        "any pullback continuation if you see a confluence at 4700/4690."
    ),
    "volatile_news": (
        "Cycle context (post-suite):\n"
        "Price: bid=4602.5 ask=4603.0 mid=4602.75 (wide spread, M5 ATR "
        "doubled in last 30 min)\n"
        "Regime: VOLATILE (ADX 19 falling, ATR rising, candle bodies "
        "alternating)\n"
        "Active Snow plans: 0\n"
        "Open positions: 0\n"
        "Key levels: pre-FOMC range 4595-4610 (FOMC release in 25 min)\n"
        "Indicators: H1 RSI 50, M5 RSI swinging 35-65, MACD "
        "indecisive, M15 ATR 18 pips (1.6× normal)\n"
        "Recipe Book consulted: range\n\n"
        "Submit Snow plan(s). Pre-news context — paired plans for both "
        "breakout directions are the canonical shape; encode "
        "tight invalidation."
    ),
    "multi_scenario": (
        "Cycle context (post-suite):\n"
        "Price: bid=4548.0 ask=4548.3 mid=4548.15\n"
        "Regime: TRENDING_BEARISH (transitioning to RANGING, ADX 22 "
        "falling)\n"
        "Active Snow plans: 0\n"
        "Open positions: 0\n"
        "Key levels: D1 4543 support shelf (4-touch), H1 resistance "
        "4555/4571/4581, H4 ceiling 4605, D1 floor 4512\n"
        "Indicators: M5 RSI 55 climbing, H1 stochastic exhausted "
        "near top, M15 MACD hist crossing zero\n"
        "Recipe Book consulted: trend\n\n"
        "Submit MULTIPLE Snow plans — the chart presents at least "
        "three distinct scenarios (continuation SELL at 4571/4581, "
        "breakdown SELL at 4543, structural-bounce BUY at 4543 reclaim). "
        "Encode all that have clean structure."
    ),
}


# =============================================================================
# Production tool schema reuse — call AIAgent._openai_tools without booting
# the full agent. We construct a minimal stub `self` that has only the
# methods _openai_tools needs (which is just _tool_schemas).
# =============================================================================


def _build_production_tools() -> List[Dict[str, Any]]:
    """Return the exact openai_tools list the production bot ships."""
    from ai_agent import AIAgent
    # _openai_tools calls self._tool_schemas() which is a hard-coded
    # method (no instance state needed). _tool_schemas reads no
    # attributes, so we can call them via the unbound method on a
    # fake self.
    # _tool_schemas calls a few internal helpers (e.g. _macro_tools_
    # if_needed) that are also instance methods reading no state. Stub
    # them out so we can drive _tool_schemas without booting AIAgent.
    class _StubAgent:
        def _macro_tools_if_needed(self):
            return AIAgent._macro_tools_if_needed(self)
    stub = _StubAgent()
    schemas = AIAgent._tool_schemas(stub)
    tools: List[Dict[str, Any]] = []
    for t in schemas:
        name = t.get("name")
        desc = t.get("description")
        schema = t.get("input_schema")
        if not name or not isinstance(schema, dict):
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": desc or "",
                "parameters": copy.deepcopy(schema),
            },
        })
    # Append SUBMIT_DECISION_TOOL terminator.
    from ai_agent import SUBMIT_DECISION_TOOL
    tools.append(copy.deepcopy(SUBMIT_DECISION_TOOL))
    return tools


def _build_system_prompt() -> str:
    """Return the production system prompt verbatim."""
    from agent_prompts import SYSTEM_PROMPT
    return SYSTEM_PROMPT


# =============================================================================
# Capture + diff
# =============================================================================


@dataclass
class ToolCallCapture:
    trigger_name: str
    run_id: int
    tool_name: str
    raw_arguments_str: Optional[str]
    parsed_arguments: Optional[Dict[str, Any]]
    parse_error: Optional[str] = None
    # For submit_plan_to_snow specifically:
    layer_b_null_paths: List[str] = field(default_factory=list)
    pydantic_ok: Optional[bool] = None
    pydantic_errors: List[str] = field(default_factory=list)
    flo400_decoder_changed_paths: List[str] = field(default_factory=list)


@dataclass
class HarnessRun:
    started_at: str
    runs_per_trigger: int
    triggers_used: List[str]
    model: str
    api_base: str
    captures: List[ToolCallCapture] = field(default_factory=list)
    api_call_count: int = 0
    errors: List[str] = field(default_factory=list)


def _diff_submit_plan(args: Dict[str, Any]) -> Dict[str, Any]:
    """Run a captured submit_plan_to_snow args dict through the Layer B
    null-scan + Pydantic validate + FLO-400 decoder. Returns structured
    findings."""
    from agent_tools import AgentTools
    from snow.validator import (
        validate_plan,
        _decode_known_string_paths,
    )

    # Layer B: null-path scan
    null_paths = AgentTools._scan_null_object_paths(args)

    # Plan extraction (handle wrapper-or-direct)
    plan = args.get("plan") if isinstance(args.get("plan"), dict) else args

    # FLO-400 decoder: check if any paths got decoded
    decoded = _decode_known_string_paths(plan)
    flo400_changed: List[str] = []
    if isinstance(plan, dict):
        # If any of the FLO-400 paths changed shape, flag them.
        if plan.get("analysis", {}).get("context_tags") != decoded.get(
            "analysis", {}
        ).get("context_tags"):
            flo400_changed.append("analysis.context_tags")
        e_old = plan.get("entry", {}).get("conditions", []) or []
        e_new = decoded.get("entry", {}).get("conditions", []) or []
        for i in range(min(len(e_old), len(e_new))):
            if e_old[i] != e_new[i]:
                flo400_changed.append(f"entry.conditions[{i}]")
        for key in ("management", "exit"):
            old = plan.get(key, []) or []
            new = decoded.get(key, []) or []
            for i in range(min(len(old), len(new))):
                if old[i] != new[i]:
                    flo400_changed.append(f"{key}[{i}]")

    # Pydantic + business-rule validation (the full chain)
    try:
        ok, _, errors = validate_plan(plan)
    except Exception as e:
        ok = False
        errors = [f"validate_plan raised {type(e).__name__}: {e}"]

    return {
        "layer_b_null_paths": null_paths,
        "pydantic_ok": ok,
        "pydantic_errors": errors,
        "flo400_decoder_changed_paths": flo400_changed,
    }


# =============================================================================
# Gemini call
# =============================================================================


def _gemini_client():
    from openai import OpenAI
    base = os.environ.get(
        "GEMINI_BASE_URL",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY not set — populate .env or export it"
        )
    return OpenAI(api_key=key, base_url=base), base


def _model_name() -> str:
    return os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")


def _send_one(
    client, model: str, system_prompt: str, user_message: str, tools: list,
    timeout: int = 90,
):
    """Send one request to Gemini, return the raw response object."""
    return client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        tools=tools,
        max_completion_tokens=4000,
        temperature=1.0,
        timeout=timeout,
    )


# =============================================================================
# Main run loop
# =============================================================================


def run_harness(
    triggers: List[str], runs_per_trigger: int,
) -> HarnessRun:
    client, base = _gemini_client()
    model = _model_name()
    tools = _build_production_tools()
    system_prompt = _build_system_prompt()

    out = HarnessRun(
        started_at=datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        runs_per_trigger=runs_per_trigger,
        triggers_used=triggers,
        model=model,
        api_base=base,
    )

    for trigger_name in triggers:
        if trigger_name not in _TRIGGERS:
            out.errors.append(f"unknown trigger: {trigger_name}")
            continue
        user_msg = _TRIGGERS[trigger_name]

        for run_id in range(1, runs_per_trigger + 1):
            print(
                f"[{out.api_call_count + 1}] {trigger_name}#{run_id}...",
                end=" ", flush=True,
            )
            try:
                resp = _send_one(
                    client, model, system_prompt, user_msg, tools,
                )
                out.api_call_count += 1
            except Exception as e:
                err_str = str(e)
                if "429" in err_str:
                    print("429 — quota exhausted, halting harness")
                    out.errors.append(
                        f"{trigger_name}#{run_id}: 429 quota — "
                        f"halting; {err_str[:200]}"
                    )
                    return out
                print(f"ERROR: {type(e).__name__}: {err_str[:80]}")
                out.errors.append(
                    f"{trigger_name}#{run_id}: {type(e).__name__}: "
                    f"{err_str[:300]}"
                )
                continue

            # Capture every tool_call from the response.
            try:
                msg = resp.choices[0].message
            except Exception:
                print("EMPTY")
                continue

            tcs = msg.tool_calls or []
            if not tcs:
                print(f"no_tool_calls (content_len={len(msg.content or '')})")
                continue

            print(f"got {len(tcs)} tool_call(s)")
            for tc in tcs:
                tname = tc.function.name
                raw = tc.function.arguments
                parsed: Optional[Dict[str, Any]] = None
                parse_error: Optional[str] = None
                try:
                    parsed = json.loads(raw) if raw else {}
                except Exception as e:
                    parse_error = (
                        f"{type(e).__name__}: {str(e)[:120]}"
                    )

                cap = ToolCallCapture(
                    trigger_name=trigger_name,
                    run_id=run_id,
                    tool_name=tname,
                    raw_arguments_str=raw,
                    parsed_arguments=parsed,
                    parse_error=parse_error,
                )

                # For submit_plan_to_snow specifically, run the diff suite.
                if (
                    tname == "submit_plan_to_snow"
                    and parsed is not None
                ):
                    diff = _diff_submit_plan(parsed)
                    cap.layer_b_null_paths = diff["layer_b_null_paths"]
                    cap.pydantic_ok = diff["pydantic_ok"]
                    cap.pydantic_errors = diff["pydantic_errors"]
                    cap.flo400_decoder_changed_paths = diff[
                        "flo400_decoder_changed_paths"
                    ]

                out.captures.append(cap)

            # Be polite to the API — small pause between calls
            time.sleep(0.5)

    return out


# =============================================================================
# Findings aggregation
# =============================================================================


def _aggregate_findings(run: HarnessRun) -> Dict[str, Any]:
    """Group captures by failure-mode pattern. Each pattern gets a
    count + sample paths."""
    by_tool: Dict[str, int] = {}
    submit_attempts = 0
    submit_pydantic_ok = 0
    submit_with_null_paths = 0
    submit_with_flo400_paths = 0
    null_path_counts: Dict[str, int] = {}
    flo400_path_counts: Dict[str, int] = {}
    pydantic_error_signatures: Dict[str, int] = {}
    parse_errors = 0

    for c in run.captures:
        by_tool[c.tool_name] = by_tool.get(c.tool_name, 0) + 1
        if c.parse_error:
            parse_errors += 1
        if c.tool_name == "submit_plan_to_snow":
            submit_attempts += 1
            if c.pydantic_ok:
                submit_pydantic_ok += 1
            if c.layer_b_null_paths:
                submit_with_null_paths += 1
                for p in c.layer_b_null_paths:
                    # Generalize index to [*]
                    import re
                    gp = re.sub(r"\[\d+\]", "[*]", p)
                    null_path_counts[gp] = null_path_counts.get(gp, 0) + 1
            if c.flo400_decoder_changed_paths:
                submit_with_flo400_paths += 1
                for p in c.flo400_decoder_changed_paths:
                    import re
                    gp = re.sub(r"\[\d+\]", "[*]", p)
                    flo400_path_counts[gp] = flo400_path_counts.get(gp, 0) + 1
            for e in c.pydantic_errors or []:
                # Truncate error to a signature (first 100 chars)
                sig = (e or "")[:120]
                pydantic_error_signatures[sig] = (
                    pydantic_error_signatures.get(sig, 0) + 1
                )

    return {
        "tool_call_distribution": by_tool,
        "raw_args_parse_errors": parse_errors,
        "submit_plan_to_snow": {
            "attempts": submit_attempts,
            "pydantic_ok": submit_pydantic_ok,
            "with_null_paths": submit_with_null_paths,
            "with_flo400_decodable_paths": submit_with_flo400_paths,
            "null_path_pattern_counts": dict(
                sorted(
                    null_path_counts.items(),
                    key=lambda kv: kv[1], reverse=True,
                )
            ),
            "flo400_decodable_path_counts": dict(
                sorted(
                    flo400_path_counts.items(),
                    key=lambda kv: kv[1], reverse=True,
                )
            ),
            "pydantic_error_signature_counts": dict(
                sorted(
                    pydantic_error_signatures.items(),
                    key=lambda kv: kv[1], reverse=True,
                )
            ),
        },
    }


# =============================================================================
# CLI
# =============================================================================


def main():
    parser = argparse.ArgumentParser(
        description="FLO-408 Phase 1 — real-Gemini format corpus harness",
    )
    parser.add_argument(
        "--runs", type=int, default=2,
        help="Runs per trigger archetype (default 2)",
    )
    parser.add_argument(
        "--triggers", type=str, default="",
        help=(
            "Comma-separated trigger names (default: all 5). "
            "Available: " + ",".join(_TRIGGERS.keys())
        ),
    )
    parser.add_argument(
        "--out", type=str, default="",
        help=(
            "Output path (default: data/_audits/"
            "gemini_format_corpus_<timestamp>.json)"
        ),
    )
    args = parser.parse_args()

    triggers = (
        args.triggers.split(",") if args.triggers
        else list(_TRIGGERS.keys())
    )

    print(f"Harness run starting: {len(triggers)} triggers × {args.runs} runs")
    print(f"  Triggers: {triggers}")
    print(f"  Estimated API calls: {len(triggers) * args.runs}")
    print()

    run = run_harness(triggers, args.runs)

    findings = _aggregate_findings(run)

    out_path = (
        args.out or
        f"data/_audits/gemini_format_corpus_"
        f"{run.started_at.replace(':', '-')}.json"
    )
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "run": asdict(run),
                "findings": findings,
            },
            f, indent=2, ensure_ascii=False,
        )

    print()
    print("=" * 60)
    print(f"Corpus written: {out_path}")
    print(f"API calls used: {run.api_call_count}")
    print(f"Captures: {len(run.captures)}")
    print(f"Errors: {len(run.errors)}")
    print()
    print("FINDINGS SUMMARY")
    print(json.dumps(findings, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
