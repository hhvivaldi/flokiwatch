"""FLO-404 enforcement — post-decision SLOT ACCOUNTING validator and
next-cycle reminder.

Feedback loop only; never blocks decisions. Pattern mirrors
boss_notes.py / floki_lessons.py — a small module with three pure
functions plus tiny atomic-write persistence:

  * check_reasoning(reasoning, active_plan_count) -> list[int]
        Returns list of missing slot numbers (e.g., [2, 3, 4]).
  * write_warning(active_plan_count, missing_slots) -> None
        Persists a warning so the next cycle's user_message can
        inject a reminder. Idempotent.
  * render_reminder() -> str
        Reads + deletes the warning state, returns a <reminder>
        block to prepend to user_message. Empty string if no
        warning pending.

The mandate from agent_prompts.py SLOT ACCOUNTING section is the
literal format "Slot N empty: [reason]." — one line per empty slot
from N = active_plan_count + 1 up to N = 4. The validator does
exact-substring match on `f"Slot {n} empty:"`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional


_STATE_PATH = os.path.join("data", "floki_slot_accounting_warning.json")


def check_reasoning(reasoning: str, active_plan_count: int) -> List[int]:
    """Return list of missing slot numbers when active_plan_count < 4.

    Empty list means accounting is complete OR not required (full
    4-plan ceiling reached). The prompt's mandated format is
    "Slot N empty:" — literal match, case-sensitive.
    """
    try:
        ac = int(active_plan_count)
    except (TypeError, ValueError):
        return []
    if ac >= 4:
        return []
    rl = str(reasoning or "")
    expected = list(range(ac + 1, 5))  # N+1 .. 4
    return [n for n in expected if f"Slot {n} empty:" not in rl]


def write_warning(active_plan_count: int, missing_slots: List[int]) -> None:
    """Persist a warning record. Atomic write via temp + os.replace
    per the FlokiWatch convention. Failure is silent — this is a
    feedback loop, not a critical path."""
    try:
        try:
            ac = int(active_plan_count)
        except (TypeError, ValueError):
            ac = 0
        ms = [int(n) for n in (missing_slots or [])]
        os.makedirs(os.path.dirname(_STATE_PATH) or ".", exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "active_plan_count": ac,
            "missing_slots": ms,
        }
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass  # silent — feedback loop


def consume_warning() -> Optional[Dict]:
    """Read AND delete the warning state in one operation.

    Deletion ensures the reminder appears once per missing-accounting
    cycle, not on every subsequent cycle. If a missing-accounting
    cycle happens again, the validator writes a fresh warning.
    """
    try:
        if not os.path.exists(_STATE_PATH):
            return None
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        try:
            os.remove(_STATE_PATH)
        except Exception:
            pass
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def render_reminder() -> str:
    """Return a `<reminder>` block to prepend to user_message, or
    empty string if no warning is pending. Consumes the state."""
    w = consume_warning()
    if not w:
        return ""
    active = w.get("active_plan_count", 0)
    missing = w.get("missing_slots", [])
    if not missing:
        return ""
    slot_list = ", ".join(str(n) for n in missing)
    slot_lines = "\n".join(
        f'  Slot {n} empty: [reason — scenario considered and why it didn\'t qualify].'
        for n in missing
    )
    return (
        f"<reminder>\n"
        f"Last cycle did not include the mandatory SLOT ACCOUNTING ledger "
        f"required by the <plans> SLOT ACCOUNTING rule. "
        f"Plans active last cycle: {active}/4. You did not justify slots: {slot_list}. "
        f"This cycle, include in your submit_decision reasoning the explicit lines:\n"
        f"  Plans active: N/4.\n{slot_lines}\n"
        f"This is a feedback loop, not a block — but skipping the accounting "
        f"will trigger the same reminder until you encode it.\n"
        f"</reminder>"
    )
