"""Time-window evaluators: duration_exceeds, time_between.

Both use `ctx.now` if set (deterministic in tests) or wall-clock UTC.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from snow.evaluators.context import EvalContext
from snow.schema import DurationExceeds, TimeBetween


def _utcnow(ctx: EvalContext) -> datetime:
    if ctx.now is not None:
        dt = ctx.now
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _parse_entered_at(ctx: EvalContext) -> Optional[datetime]:
    """Resolve the plan's entry timestamp. Snow loop writes `entered_at`
    on the snow_plans row and is expected to hydrate that onto the Plan
    object before dispatch — but Plan itself has no `entered_at` field
    in the v1 schema. We look in two places:
      1. `ctx.plan.entered_at` if an attribute exists (future-compatible).
      2. `ctx.plan.created_at` as a fallback (pre-entry this won't
         evaluate true; post-entry the created→now delta approximates
         trade duration).
    Returns timezone-aware UTC datetime, or None if unparseable.
    """
    raw = getattr(ctx.plan, "entered_at", None) or ctx.plan.created_at
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate_duration_exceeds(
    cond: DurationExceeds, ctx: EvalContext
) -> bool:
    """True iff (now - plan entry time) >= cond.minutes.

    Pre-entry (ticket is None) we still return False conservatively —
    the plan hasn't started. Post-entry we use `entered_at` once the
    Snow loop starts hydrating it (Phase 4); until then we fall back
    to `created_at`, which slightly over-estimates duration but never
    under-estimates (safer for an exit-style contingency)."""
    if ctx.ticket is None:
        return False
    entered = _parse_entered_at(ctx)
    if entered is None:
        return False
    now = _utcnow(ctx)
    elapsed_minutes = (now - entered).total_seconds() / 60.0
    return elapsed_minutes >= cond.minutes


def evaluate_time_between(cond: TimeBetween, ctx: EvalContext) -> bool:
    """True iff the current UTC time (HH:MM) lies in [start_utc, end_utc],
    inclusive on BOTH ends.

    Cross-midnight semantics (advisor item #6):
      * start <= end: single window [start, end]. At `start` or `end` exactly → True.
      * start  > end: wrap-around. Window = [start, 23:59] ∪ [00:00, end].
        E.g. start=22:00, end=06:00 covers 22:00–23:59 and 00:00–06:00.
        At 07:00 → False. At 22:00 exactly → True. At 06:00 exactly → True.

    Zero-width `start == end` is rejected at validator time, so we do
    not need to handle it here."""
    now = _utcnow(ctx)
    now_hhmm = now.strftime("%H:%M")
    start = cond.start_utc
    end = cond.end_utc
    if start <= end:
        return start <= now_hhmm <= end
    # Cross-midnight
    return now_hhmm >= start or now_hhmm <= end
