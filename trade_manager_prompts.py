"""FLO-403 Phase 2 — Trade Manager Agent prompts.

Lean by directive (CEO): ≤500 token system + ~350 token user template
per cycle. The TM is the EXECUTOR tier; the analytical-suite breadth
(charts, S/R, Fib, Pivots, Recipe Book, Luna, Rex, ML, history) that
drives Floki's plan authoring is intentionally absent — different
cognitive task = different data surface.

See data/_design/FLO-403_Phase2_Trade_Manager_Design.md §5 for the
canonical scope rationale.
"""
from __future__ import annotations

import json
from typing import Any, Optional


# =============================================================================
# System prompt — locked at top of every cycle, eligible for cache amortisation
# =============================================================================

SYSTEM_PROMPT = """\
You are the Trade Manager for FlokiWatch's XAU/USD trading bot. You
supervise OPEN positions only — you never author plans, never open
positions, and never call any tool not in your roster.

DECISION SPACE (return ONE strict JSON):
  HOLD_TRADE   — actively decided to wait; thesis intact
  ADJUST_TRADE — change SL/TP; requires {new_sl, new_tp}
  CLOSE_TRADE  — thesis broken or critical news
  NO_OP        — nothing requires LLM judgment; let Snow + monitor.py
                 handle it deterministically

OUTPUT (JSON, no prose, no markdown fences):
{
  "decision": "HOLD_TRADE" | "ADJUST_TRADE" | "CLOSE_TRADE" | "NO_OP",
  "reason": "<<=80 chars>",
  "new_sl": <float, only for ADJUST_TRADE>,
  "new_tp": <float, only for ADJUST_TRADE>
}

DEFAULT BIAS — NO_OP. Snow's plan exit/management contingencies
already handle most thesis-break / target-hit / BE-lock cases, and
monitor.py runs deterministic BE/trail/drawdown logic every tick.
You exist for the LLM-judgment edge cases those rules miss — usually
that's CLOSE_TRADE on an unanticipated regime flip or an Echo CRITICAL
news event the plan didn't pre-encode.

DON'T preempt Snow. If the position has a Snow `exit` contingency
that will fire on the same condition you're seeing, return NO_OP and
let Snow take it. Closing redundantly creates audit-trail noise.

DON'T author. You receive a 1-line plan thesis as context; do not
critique it, do not propose a new plan. If the thesis is broken,
CLOSE_TRADE with the reason; the next Floki cycle (30-min schedule)
will re-author.

If the position list is empty: return NO_OP immediately.
"""


# =============================================================================
# User prompt builder — per-cycle, populated from TradeManagerTools output
# =============================================================================


def build_user_prompt(context: dict) -> str:
    """Render the per-cycle user message from a context dict assembled
    by TradeManager._gather_context. Missing fields render as 'n/a'
    rather than crashing — defensive on the data path so a single bad
    indicator read doesn't kill the cycle.
    """
    pos = context.get("position") or {}
    plan = context.get("plan") or {}
    market = context.get("market") or {}
    indicators = market.get("indicators") or {}
    m5 = indicators.get("M5") or {}
    m15 = indicators.get("M15") or {}
    trigger = context.get("trigger") or {}

    def _fmt(v: Any) -> str:
        if v is None:
            return "n/a"
        if isinstance(v, float):
            return f"{v:.4f}".rstrip("0").rstrip(".") or "0"
        return str(v)

    closes = m5.get("recent_closes") or []
    closes_str = "[" + ",".join(_fmt(c) for c in closes) + "]"

    echo_list = market.get("echo_critical_since_open") or []
    echo_str = json.dumps(echo_list) if echo_list else "[]"

    contingencies = plan.get("contingencies_remaining") or []

    return (
        "<position>\n"
        f"ticket: {_fmt(pos.get('ticket'))}\n"
        f"direction: {_fmt(pos.get('direction'))}\n"
        f"volume: {_fmt(pos.get('volume'))}\n"
        f"entry: {_fmt(pos.get('entry'))}\n"
        f"current_sl: {_fmt(pos.get('sl'))}\n"
        f"current_tp: {_fmt(pos.get('tp'))}\n"
        f"age_minutes: {_fmt(pos.get('age_minutes'))}\n"
        f"managed_by: {_fmt(pos.get('managed_by'))}\n"
        f"unrealised_pips: {_fmt(pos.get('unrealised_pips'))}\n"
        f"unrealised_usd: {_fmt(pos.get('current_pnl'))}\n"
        f"mfe_pips: {_fmt(pos.get('mfe_pips'))}\n"
        f"mae_pips: {_fmt(pos.get('mae_pips'))}\n"
        "</position>\n\n"
        "<plan>\n"
        f"plan_id: {_fmt(plan.get('plan_id'))}\n"
        f"thesis: {_fmt(plan.get('thesis'))}\n"
        f"contingencies_remaining: {json.dumps(contingencies)}\n"
        "</plan>\n\n"
        "<market>\n"
        f"current_price: {_fmt(market.get('current_price'))}\n"
        f"regime_changed: {_fmt(market.get('regime_changed'))}\n"
        f"M5_RSI: {_fmt(m5.get('RSI'))}\n"
        f"M5_Stoch_K: {_fmt(m5.get('Stoch_K'))}\n"
        f"M5_Stoch_D: {_fmt(m5.get('Stoch_D'))}\n"
        f"M5_ATR: {_fmt(m5.get('ATR'))}\n"
        f"M15_RSI: {_fmt(m15.get('RSI'))}\n"
        f"M15_Stoch_K: {_fmt(m15.get('Stoch_K'))}\n"
        f"M15_Stoch_D: {_fmt(m15.get('Stoch_D'))}\n"
        f"M15_MACD_histogram: {_fmt(m15.get('MACD_histogram'))}\n"
        f"M15_ATR: {_fmt(m15.get('ATR'))}\n"
        f"recent_M5_closes: {closes_str}\n"
        f"echo_critical_since_open: {echo_str}\n"
        "</market>\n\n"
        "<trigger>\n"
        f"type: {_fmt(trigger.get('type'))}\n"
        f"data: {json.dumps(trigger.get('data') or {})}\n"
        "</trigger>\n\n"
        "Return your decision JSON."
    )


# =============================================================================
# Decision JSON parser — strict, fails to None (caller treats as NO_OP)
# =============================================================================

_VALID_DECISIONS = frozenset({
    "HOLD_TRADE", "ADJUST_TRADE", "CLOSE_TRADE", "NO_OP",
})


def parse_decision_json(raw: str) -> Optional[dict]:
    """Strict parse. Returns None on any failure (caller maps None → NO_OP).

    Accepts:
      - Bare JSON object
      - JSON wrapped in ```json ... ``` markdown fences (defensive
        passthrough — Qwen tends to honour the no-fence instruction
        but not always)
      - Leading/trailing whitespace

    Rejects:
      - Non-JSON
      - JSON without `decision` key
      - decision not in VALID set
      - ADJUST_TRADE missing new_sl OR new_tp
      - new_sl / new_tp not a number when present
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    # Strip markdown fence if present
    if s.startswith("```"):
        # Drop opening fence (```json or ```)
        first_newline = s.find("\n")
        if first_newline >= 0:
            s = s[first_newline + 1:]
        # Drop closing fence
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    s = s.strip()
    try:
        parsed = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    decision = parsed.get("decision")
    if decision not in _VALID_DECISIONS:
        return None
    out: dict = {
        "decision": decision,
        "reason": str(parsed.get("reason") or "")[:200],
    }
    if decision == "ADJUST_TRADE":
        new_sl = parsed.get("new_sl")
        new_tp = parsed.get("new_tp")
        if not isinstance(new_sl, (int, float)) or not isinstance(new_tp, (int, float)):
            return None
        out["new_sl"] = float(new_sl)
        out["new_tp"] = float(new_tp)
    return out


__all__ = [
    "SYSTEM_PROMPT",
    "build_user_prompt",
    "parse_decision_json",
]
