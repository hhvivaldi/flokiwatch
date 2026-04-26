"""Price-level evaluators: price_above, price_below (stateless),
price_crossed_level (stateful, FLO-359 Phase 8b commit 5).

All use the mid-price from LiveData. Stateless evaluators return False
if the mid-price is unavailable; the stateful evaluator preserves
state across a missing tick (None → False, no state mutation).
"""
from __future__ import annotations

from tz_utils import utc_iso

from snow.evaluators.context import EvalContext
from snow.schema import PriceAbove, PriceBelow, PriceCrossedLevel


def evaluate_price_above(cond: PriceAbove, ctx: EvalContext) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    return price > cond.level


def evaluate_price_below(cond: PriceBelow, ctx: EvalContext) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    return price < cond.level


# =============================================================================
# Stateful: price_crossed_level (FLO-359 Phase 8b commit 5)
# =============================================================================

def evaluate_price_crossed_level(
    cond: PriceCrossedLevel,
    ctx: EvalContext,
    state,  # ConditionStateRow
) -> bool:
    """One-shot latch. Returns True forever after the first tick where
    mid-price crosses `cond.level` in `cond.direction`.

    Detection rule:
      direction == "above": prev <  level  AND  curr >= level
      direction == "below": prev >  level  AND  curr <= level

    The asymmetry (strict on the prev side, inclusive on the curr
    side) captures "price tagged the level" semantics — a tick that
    lands exactly on the level still counts as a successful cross
    from the previous side. Matches the RFC §3.3 "tag and bounce"
    use-case.

    Cold-start (`state.prev_value is None`): seed prev=current and
    return False. The documented one-tick false-negative window
    (RFC §5.2) — also applies after a rehydrate that dropped the
    row as stale.

    Latched (`state.latched is True`): return True without re-checking
    prev/curr. Latch persists until the plan transitions to a terminal
    status (then `state_cache.forget_plan` clears the row). Per CEO
    Q3 decision: no mid-plan reset.

    Missing data (`curr is None`): return False AND do NOT mutate
    state (preserve prev for the next tick when data returns). The
    latch state is not affected — a previously-latched row keeps
    returning True even if the price feed drops, because the latch
    check is consulted before the price read.
    """
    if state.latched is True:
        state.last_seen_at = utc_iso()
        return True

    curr = ctx.live_data.price("mid")
    if curr is None:
        return False

    if state.prev_value is None:
        state.prev_value = float(curr)
        state.last_seen_at = utc_iso()
        return False

    prev = float(state.prev_value)
    level = float(cond.level)
    crossed = False
    if cond.direction == "above":
        crossed = prev < level and curr >= level
    elif cond.direction == "below":
        crossed = prev > level and curr <= level

    if crossed:
        state.latched = True
    state.prev_value = float(curr)
    state.last_seen_at = utc_iso()
    return bool(state.latched)
