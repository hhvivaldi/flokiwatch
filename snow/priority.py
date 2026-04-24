"""Snow priority resolver — FLO-347 Phase 5a (pure math module).

Implements RFC §8 "option-b-refined" effective-priority formula plus
deterministic tie-break. Pure: no side effects, no logging, no DB,
no executor. Safe to import from anywhere in snow/.

Formula (RFC §8.1):
    effective = base + min(base - 1, override * 10)

Where `base` is per-action (powers of 2; see `_BASE_BY_ACTION`) and
`override` is the contingency's `priority` field (schema-bounded to
[1, 10]).

The `min(base - 1, override * 10)` clip ensures override CANNOT push
a contingency into the next category. Combined with the category-gap
doubling rule (base_{n+1} >= 2 * base_n), strict inter-category
ordering holds by construction.

Tie-break (RFC §8.3), applied in order:
    1. Same plan → earlier `plan_list_order` wins
    2. Cross plan → older `created_at` wins
    3. Fallback → lexicographic `plan_id`
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# RFC §8.1 — action base priorities (powers of 2)
# ---------------------------------------------------------------------------

_BASE_BY_ACTION: dict[str, int] = {
    "close_full":            128,
    "close_partial":         64,
    "cancel_plan":           32,
    "adjust_sl":             16,
    "adjust_tp":             16,
    "move_sl_to_price":      16,
    "trail_sl":              16,
    "move_sl_to_breakeven":  8,
    "alert_floki":           4,
    "escalate_to_floki":     4,
    # `execute_market` is entry-only and never competes with management /
    # exit actions on the same tick (different plan statuses). Assigned
    # base=1 for defensive robustness if ever routed through resolve();
    # excluded from the category-doubling invariant below.
    "execute_market":        1,
}

# Ordered descending. `execute_market` (base=1) intentionally NOT in this
# sequence — entry actions do not compete with management/exit and are
# not part of the RFC §8.1 category invariant.
_DOUBLING_SEQUENCE: tuple[int, ...] = (128, 64, 32, 16, 8, 4)


def _verify_doubling_rule() -> None:
    """Import-time invariant: `base_{n+1} >= 2 * base_n`.

    Guarantees the strict category ordering the RFC promises. If a
    future base table edit breaks the invariant, this raises at module
    import (fail-fast) rather than at runtime on some later tick.
    """
    for high, low in zip(_DOUBLING_SEQUENCE, _DOUBLING_SEQUENCE[1:]):
        if high < 2 * low:
            raise AssertionError(
                f"Priority bases violate RFC §8.1 doubling rule: "
                f"{high} < 2 * {low}"
            )


_verify_doubling_rule()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def action_base(action_type: str) -> int:
    """Return the base priority for `action_type`.

    Raises KeyError for unknown types. Callers should have already run
    the payload through snow.schema's discriminated union.
    """
    return _BASE_BY_ACTION[action_type]


def effective_priority(action_type: str, override: int) -> int:
    """Compute RFC §8.1 option-b-refined effective priority.

    Formula: `base + min(base - 1, override * 10)`.

    `override` is the `Contingency.priority` field (Pydantic-bounded
    to [1, 10]). Values outside that range still compute deterministically
    but are never produced by a validated plan.
    """
    base = action_base(action_type)
    return base + min(base - 1, override * 10)


@dataclass
class FireEvent:
    """A contingency (or entry) whose conditions all-true'd this tick.

    The Snow loop accumulates these across plans, then `resolve()`
    orders them for dispatch. `payload` is opaque to priority.py —
    actions.py (Phase 5b) uses it to carry the dispatch context.

    Fields:
      plan_id          — plan this fire belongs to
      created_at       — plan's ISO-8601 UTC created_at (tie-break #2)
      contingency_name — audit label; not used in ordering
      action_type      — drives `action_base` lookup
      override         — `Contingency.priority` (schema-bounded [1, 10])
      plan_list_order  — ordering within one plan (tie-break #1);
                         lower = earlier in management/exit/entry list.
                         Convention: entry = -1, management = 0..N-1,
                         exit = 1000 + 0..N-1 (management fires before exit
                         at identical priority, per RFC §3 lifecycle).
      payload          — opaque; action dispatcher reads
    """
    plan_id:         str
    created_at:      str
    contingency_name: str
    action_type:     str
    override:        int
    plan_list_order: int
    payload:         Any = None

    @property
    def effective_priority(self) -> int:
        return effective_priority(self.action_type, self.override)


def resolve(fires: list[FireEvent]) -> list[FireEvent]:
    """Sort `fires` by effective priority (desc) with deterministic
    tie-break. Returns a new list; input is not mutated.

    Sort keys (all ascending → descending for priority via negation):
      1. -effective_priority     (higher priority first)
      2. plan_list_order         (earlier in plan first)
      3. created_at              (older plan first)
      4. plan_id                 (lexicographic fallback)
    """
    return sorted(
        fires,
        key=lambda f: (
            -f.effective_priority,
            f.plan_list_order,
            f.created_at,
            f.plan_id,
        ),
    )


__all__ = [
    "FireEvent",
    "action_base",
    "effective_priority",
    "resolve",
]
