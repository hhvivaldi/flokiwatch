"""FLO-419 Phase 2 -- 24h Claude vs Gemini cost & efficiency report.

Pulls per-cycle metrics from `agent_proactive_analyses` for two windows:
  * Claude:  cycles where model='claude-opus-4-6' (post-2026-05-01 restart)
  * Gemini:  cycles where model='gemini-3.1-pro-preview' (last 7 days)

Computes:
  * Average iterations per cycle (counted via tool_trace length excluding
    write tools so the count reflects "how many tool calls did the model
    make to reach a decision", not bookkeeping noise).
  * Average input / output tokens per cycle.
  * Per-cycle cost in USD using current Anthropic / Google pricing.
  * Cache-read share (Claude only -- Gemini doesn't expose per-cycle cache
    metrics in the same shape).
  * Cycle latency distribution (median, p90).

Honest about uncertainty:
  * `usage.input_tokens` in the agent_proactive_analyses table is the SUM
    across the agentic-loop turns (per the adapter's prompt_tokens
    aggregation in `floki_anthropic_adapter.anthropic_to_oai_response`).
    For Claude that includes uncached + cache_read + cache_creation.
  * The per-cycle cost is an UPPER bound when only the gross input is
    available; the lower bound (best-case caching) is also reported for
    Claude. The true number is between, closer to the lower bound on
    warm-cache cycles.

Usage:
    python scripts/floki_claude_24h_report.py
    python scripts/floki_claude_24h_report.py --since 2026-05-01T17:14:00Z
    python scripts/floki_claude_24h_report.py --output md > report.md
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "history.db"

# Anthropic Claude Opus 4.6 pricing (per CEO 2026-05-01)
CLAUDE_PRICING = {
    "input": 5.00,            # $/M  (uncached input)
    "output": 25.00,          # $/M
    "cache_read": 0.50,       # $/M  (10% of input)
    "cache_write_1h": 10.00,  # $/M  (2x input for 1h TTL)
}

# Gemini 3 Pro Preview pricing (Google API, <=200k context tier)
GEMINI_PRICING = {
    "input": 1.25,    # $/M
    "output": 10.00,  # $/M
}

# Tool names that are pure write/no-op and shouldn't count as analytical
# tool calls (used only by the iteration-count metric).
_WRITE_TOOLS = {
    "submit_plan_to_snow", "cancel_plan", "set_next_check",
    "submit_decision", "write_session_memory", "write_trading_journal",
    "acknowledge_boss_notes", "place_pending_order",
}


def _pull_cycles(since_iso: str) -> List[Dict[str, Any]]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT timestamp, model, agent_decision, agent_confidence, "
            "latency_ms, input_tokens, output_tokens, tool_trace "
            "FROM agent_proactive_analyses "
            "WHERE timestamp >= ? "
            "ORDER BY timestamp",
            (since_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _count_iterations(tool_trace_str: Optional[str]) -> Tuple[int, int, int]:
    """Return (analytical_calls, write_calls, total_calls)."""
    if not tool_trace_str:
        return (0, 0, 0)
    try:
        trace = json.loads(tool_trace_str)
    except Exception:
        return (0, 0, 0)
    if not isinstance(trace, list):
        return (0, 0, 0)
    write = sum(1 for e in trace if isinstance(e, dict) and e.get("name") in _WRITE_TOOLS)
    return (len(trace) - write, write, len(trace))


def _claude_cost_estimate(input_tokens: int, output_tokens: int,
                           cache_share_assumed: float = 0.85) -> Dict[str, float]:
    """Return both upper-bound (no caching, all input billed at $5/M) and
    lower-bound (cache_share_assumed of input billed as cache_read) costs.
    True cost lies between."""
    if not input_tokens:
        return {"upper": 0.0, "lower": 0.0, "output": 0.0}
    output_cost = (output_tokens or 0) * CLAUDE_PRICING["output"] / 1e6

    upper = (input_tokens * CLAUDE_PRICING["input"] / 1e6) + output_cost

    cached = input_tokens * cache_share_assumed
    uncached = input_tokens * (1 - cache_share_assumed)
    # Approximate split: assume 5% of "uncached" share is cache_creation
    create_share = 0.05
    lower = (
        cached * CLAUDE_PRICING["cache_read"] / 1e6
        + uncached * (1 - create_share) * CLAUDE_PRICING["input"] / 1e6
        + uncached * create_share * CLAUDE_PRICING["cache_write_1h"] / 1e6
        + output_cost
    )
    return {"upper": upper, "lower": lower, "output": output_cost}


def _gemini_cost(input_tokens: int, output_tokens: int) -> float:
    return (
        (input_tokens or 0) * GEMINI_PRICING["input"] / 1e6
        + (output_tokens or 0) * GEMINI_PRICING["output"] / 1e6
    )


def _summary_block(label: str, cycles: List[Dict[str, Any]],
                    is_claude: bool) -> str:
    if not cycles:
        return f"\n## {label}\n\nNo cycles in window.\n"

    iters_analytical: List[int] = []
    iters_total: List[int] = []
    tokens_in: List[int] = []
    tokens_out: List[int] = []
    latencies: List[int] = []
    cycle_costs_upper: List[float] = []
    cycle_costs_lower: List[float] = []
    cycle_costs_gemini: List[float] = []

    for c in cycles:
        a, _w, t = _count_iterations(c.get("tool_trace"))
        iters_analytical.append(a)
        iters_total.append(t)
        in_t = int(c.get("input_tokens") or 0)
        out_t = int(c.get("output_tokens") or 0)
        tokens_in.append(in_t)
        tokens_out.append(out_t)
        if c.get("latency_ms") is not None:
            latencies.append(int(c["latency_ms"]))
        if is_claude:
            est = _claude_cost_estimate(in_t, out_t)
            cycle_costs_upper.append(est["upper"])
            cycle_costs_lower.append(est["lower"])
        else:
            cycle_costs_gemini.append(_gemini_cost(in_t, out_t))

    def _stats(xs: List[float], fmt: str = "{:.0f}") -> str:
        if not xs:
            return "--"
        return (
            f"avg={fmt.format(statistics.mean(xs))} "
            f"med={fmt.format(statistics.median(xs))} "
            f"p90={fmt.format(sorted(xs)[max(0, int(len(xs) * 0.9) - 1)])} "
            f"max={fmt.format(max(xs))}"
        )

    out: List[str] = []
    out.append(f"\n## {label} ({len(cycles)} cycles)\n")
    out.append(f"- iterations (analytical only): {_stats(iters_analytical)}")
    out.append(f"- iterations (total tool_trace): {_stats(iters_total)}")
    out.append(f"- input tokens: {_stats(tokens_in)}")
    out.append(f"- output tokens: {_stats(tokens_out)}")
    out.append(f"- latency ms: {_stats(latencies)}")
    if is_claude:
        out.append(f"- cost UPPER (no caching) USD/cycle: {_stats(cycle_costs_upper, '${:.2f}')}")
        out.append(f"- cost LOWER (85% cache assumption) USD/cycle: {_stats(cycle_costs_lower, '${:.2f}')}")
        if cycle_costs_lower:
            total_low = sum(cycle_costs_lower)
            total_high = sum(cycle_costs_upper)
            cycles_per_day = (len(cycles) / max(1, _hours_span(cycles))) * 24
            out.append(f"- total window cost: ${total_low:.2f} - ${total_high:.2f}")
            out.append(
                f"- projected daily cost (warm steady-state): "
                f"${cycles_per_day * statistics.mean(cycle_costs_lower):.2f}/day"
            )
            out.append(
                f"- projected annual cost (warm steady-state): "
                f"${cycles_per_day * statistics.mean(cycle_costs_lower) * 365:.0f}/year"
            )
    else:
        out.append(f"- cost USD/cycle (Gemini 3 Pro pricing): {_stats(cycle_costs_gemini, '${:.4f}')}")
        if cycle_costs_gemini:
            total = sum(cycle_costs_gemini)
            cycles_per_day = (len(cycles) / max(1, _hours_span(cycles))) * 24
            out.append(f"- total window cost: ${total:.2f}")
            out.append(
                f"- projected daily cost: "
                f"${cycles_per_day * statistics.mean(cycle_costs_gemini):.2f}/day"
            )
    return "\n".join(out) + "\n"


def _hours_span(cycles: List[Dict[str, Any]]) -> float:
    if len(cycles) < 2:
        return 1.0
    try:
        first = datetime.fromisoformat(cycles[0]["timestamp"].replace("Z", "+00:00"))
        last = datetime.fromisoformat(cycles[-1]["timestamp"].replace("Z", "+00:00"))
        return max(1.0, (last - first).total_seconds() / 3600)
    except Exception:
        return 1.0


def _per_cycle_table(cycles: List[Dict[str, Any]], is_claude: bool, max_rows: int = 30) -> str:
    if not cycles:
        return ""
    rows = cycles[-max_rows:]
    lines = [
        "\n### Per-cycle detail (last {})\n".format(min(max_rows, len(cycles))),
        "| timestamp | iter (analytical/total) | in tok | out tok | latency ms | est cost |",
        "|---|---|---|---|---|---|",
    ]
    for c in rows:
        a, _w, t = _count_iterations(c.get("tool_trace"))
        in_t = int(c.get("input_tokens") or 0)
        out_t = int(c.get("output_tokens") or 0)
        if is_claude:
            est = _claude_cost_estimate(in_t, out_t)
            cost_str = f"${est['lower']:.2f}-${est['upper']:.2f}"
        else:
            cost_str = f"${_gemini_cost(in_t, out_t):.4f}"
        lines.append(
            f"| {c['timestamp']} | {a}/{t} | {in_t:,} | {out_t:,} | "
            f"{c.get('latency_ms') or '--'} | {cost_str} |"
        )
    return "\n".join(lines) + "\n"


def main():
    p = argparse.ArgumentParser(description="24h Claude vs Gemini cost & efficiency report")
    p.add_argument("--since", help="ISO timestamp (UTC) for Claude window. Default: 24h ago.",
                   default=None)
    p.add_argument("--gemini-since", help="ISO timestamp for Gemini comparison window. Default: 7d ago.",
                   default=None)
    p.add_argument("--output", choices=["text", "md"], default="text")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    claude_since = args.since or (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gemini_since = args.gemini_since or (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_cycles = _pull_cycles(min(claude_since, gemini_since))
    claude_cycles = [c for c in all_cycles
                     if (c.get("model") or "").startswith("claude")
                     and c["timestamp"] >= claude_since]
    gemini_cycles = [c for c in all_cycles
                     if "gemini" in (c.get("model") or "").lower()
                     and c["timestamp"] >= gemini_since]

    out: List[str] = []
    out.append(f"# Floki LLM cost & efficiency report")
    out.append(f"\nGenerated: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}")
    out.append(f"Claude window: {claude_since} to now")
    out.append(f"Gemini window: {gemini_since} to now (last 7 days for comparison)")
    out.append(f"\n## Pricing assumptions\n")
    out.append(f"- Claude Opus 4.6: input ${CLAUDE_PRICING['input']:.2f}/M | output "
               f"${CLAUDE_PRICING['output']:.2f}/M | cache read ${CLAUDE_PRICING['cache_read']:.2f}/M | "
               f"cache write 1h ${CLAUDE_PRICING['cache_write_1h']:.2f}/M")
    out.append(f"- Gemini 3 Pro Preview: input ${GEMINI_PRICING['input']:.2f}/M | output "
               f"${GEMINI_PRICING['output']:.2f}/M")
    out.append(_summary_block("Claude Opus 4.6", claude_cycles, is_claude=True))
    out.append(_summary_block("Gemini 3.1 Pro Preview", gemini_cycles, is_claude=False))
    out.append(_per_cycle_table(claude_cycles, is_claude=True))
    out.append(_per_cycle_table(gemini_cycles, is_claude=False))

    print("\n".join(out))


if __name__ == "__main__":
    main()
