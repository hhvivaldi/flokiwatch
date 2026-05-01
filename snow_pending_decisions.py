"""FLO-418 — render Snow's awaiting-Floki-decision block for the
user prompt.

Mirrors the boss_notes.render_block pattern: queries the DB for any
pending plans flagged with `awaiting_decision`, formats a single
<snow_pending_decisions> XML block listing each one with the three
options Floki can take.

Empirical motivation (2026-05-01): FLO-417's hard block on opposing
positions cost ~$2.68 of opportunity on PLAN-010. CEO directive:
inform Floki and let him decide per-instance.
"""
from __future__ import annotations

from typing import Any


def _format_one(item: dict[str, Any]) -> str:
    plan_id = item.get("plan_id", "?")
    awaiting = item.get("awaiting_decision") or {}
    plan = item.get("plan") or {}
    direction = awaiting.get("attempted_direction", "?")
    opposing = awaiting.get("opposing_tickets") or []
    opposite = "BUY" if direction == "SELL" else (
        "SELL" if direction == "BUY" else "?"
    )
    noticed_at = awaiting.get("noticed_at", "?")

    entry = (plan.get("entry") or {})
    entry_price = entry.get("entry_price")
    sl = entry.get("initial_sl")
    tp = entry.get("initial_tp")

    opp_str = ", ".join(f"#{t}" for t in opposing) if opposing else "?"
    plan_summary = (
        f"entry={entry_price}, SL={sl}, TP={tp}"
        if entry_price is not None else "see plan body"
    )
    return (
        f"  - {plan_id} {direction} ({plan_summary}) is ready to fire but "
        f"opposing {opposite} position(s) {opp_str} are live "
        f"(detected {noticed_at}).\n"
        f"      Options:\n"
        f"        (a) `cancel_plan(plan_id=\"{plan_id}\", reason=\"...\")` — "
        f"abandon this branch.\n"
        f"        (b) `close_trade(ticket=...)` for the opposing position — "
        f"Snow auto-fires {plan_id} on the next 5s tick.\n"
        f"        (c) `override_opposing_block(plan_id=\"{plan_id}\", "
        f"reason=\"...\")` — allow both positions simultaneously "
        f"(net-zero exposure, double spread)."
    )


def render_block() -> str | None:
    """Return the <snow_pending_decisions> block string, or None if no
    plans are awaiting. Defensive — never raises."""
    try:
        from snow.db import list_plans_with_awaiting_decision
        items = list_plans_with_awaiting_decision()
    except Exception:
        return None
    if not items:
        return None
    body_lines = [_format_one(it) for it in items]
    body = "\n".join(body_lines)
    return (
        "<snow_pending_decisions>\n"
        "Snow detected opposing-position scenarios on the plan(s) below "
        "and is HOLDING the entry, awaiting your decision (FLO-418). "
        "Each plan stays alive in PENDING until you act:\n"
        f"{body}\n"
        "</snow_pending_decisions>"
    )
