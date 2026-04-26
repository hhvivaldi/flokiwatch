"""Dispatch — route a Pydantic Condition object to the right evaluator fn.

One public entry point: `evaluate_condition(cond, ctx) -> bool`.

Contract:
  * Takes the Pydantic condition object (NOT a raw dict), so downstream
    evaluators get typed field access. The Snow loop deserialises
    `plan_json` into a Pydantic Plan before dispatch, so this matches
    the natural call topology.
  * Unknown `cond.type` → returns False AND logs a WARNING.  Unknown
    types in production usually mean schema drift or a plan surviving
    across an incompatible deploy; the WARN surfaces it to the operator
    while the False keeps the loop fail-safe (RFC §6.5).
  * Any exception inside the evaluator → returns False AND logs a
    WARNING with the exception. Evaluators are expected to return False
    on missing data, not raise, so an escaping exception is a bug worth
    surfacing — but still fail-safe at runtime.

Read-only after init: the `_DISPATCH` table is built once at import and
never mutated. No locking needed for reads across threads.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from snow.evaluators.context import EvalContext
from snow.evaluators.indicator import (
    evaluate_atr,
    evaluate_bollinger_position,
    evaluate_ema_relation,
    evaluate_indicator_crossover,
    evaluate_indicator_divergence,
    evaluate_indicator_was,
    evaluate_macd_histogram,
    evaluate_rsi,
    evaluate_stochastic,
)
from snow.evaluators.position import (
    evaluate_mae_reached,
    evaluate_mfe_reached,
    evaluate_profit_pips,
    evaluate_profit_retraced_from_peak,
)
from snow.evaluators.price import evaluate_price_above, evaluate_price_below
from snow.evaluators.structural import (
    evaluate_price_at_fibonacci,
    evaluate_price_at_pivot,
    evaluate_price_at_sr_zone,
)
from snow.evaluators.time_window import (
    evaluate_duration_exceeds,
    evaluate_time_between,
)


_log = logging.getLogger(__name__)


# Discriminator string → evaluator callable.
# The string values MUST match the `type` Literal on each class in
# snow.schema. Keep this mapping grouped by category for navigation.
_DISPATCH: dict[str, Callable[[Any, EvalContext], bool]] = {
    # price.py
    "price_above":            evaluate_price_above,
    "price_below":            evaluate_price_below,
    # indicator.py
    "rsi":                    evaluate_rsi,
    "macd_histogram":         evaluate_macd_histogram,
    "ema_relation":           evaluate_ema_relation,
    "atr":                    evaluate_atr,
    # structural.py
    "price_at_sr_zone":       evaluate_price_at_sr_zone,
    "price_at_fibonacci":     evaluate_price_at_fibonacci,
    # position.py (stateful)
    "profit_pips":            evaluate_profit_pips,
    "mfe_reached":            evaluate_mfe_reached,
    "mae_reached":            evaluate_mae_reached,
    "profit_retraced_from_peak": evaluate_profit_retraced_from_peak,
    # time_window.py
    "duration_exceeds":       evaluate_duration_exceeds,
    "time_between":           evaluate_time_between,
    # Phase 7.3 (FLO-355) — Cat A indicator additions
    "bollinger_position":     evaluate_bollinger_position,
    "stochastic":             evaluate_stochastic,
    "price_at_pivot":         evaluate_price_at_pivot,
    "indicator_divergence":   evaluate_indicator_divergence,
}


# FLO-359 Phase 8b commit 3 — stateful evaluator registry. Signature is
# `(cond, ctx, state) -> bool` where `state` is a `ConditionStateRow`
# allocated/looked-up via `ctx.state_cache`. Stateful types live in
# their own table so stateless evaluators stay 2-arg and untouched.
_DISPATCH_STATEFUL: dict[str, Callable[[Any, EvalContext, Any], bool]] = {
    "indicator_crossover":    evaluate_indicator_crossover,
    "indicator_was":          evaluate_indicator_was,
}


def evaluate_condition(
    cond: Any,
    ctx: EvalContext,
    *,
    plan_id: Optional[str] = None,
    contingency_name: Optional[str] = None,
    condition_index: Optional[int] = None,
) -> bool:
    """Evaluate a single condition against the context. Fail-safe: any
    unknown type or escaping exception returns False (with a WARN log)
    so the Snow loop never crashes on a bad condition.

    Stateful types (members of `_DISPATCH_STATEFUL`) need
    `(plan_id, contingency_name, condition_index)` to look up their
    `ConditionStateRow` in `ctx.state_cache`. The Snow loop passes
    these via kwargs; tests calling stateful evaluators directly are
    expected to pass them too. Missing location or missing
    `ctx.state_cache` for a stateful type → False (fail-safe + WARN).
    """
    cond_type = getattr(cond, "type", None)
    if not isinstance(cond_type, str):
        _log.warning(
            "snow.evaluators: condition with no .type attribute: %r", cond
        )
        return False

    stateful_fn = _DISPATCH_STATEFUL.get(cond_type)
    if stateful_fn is not None:
        return _evaluate_stateful(
            cond_type, stateful_fn, cond, ctx,
            plan_id, contingency_name, condition_index,
        )

    fn = _DISPATCH.get(cond_type)
    if fn is None:
        _log.warning(
            "snow.evaluators: unknown condition type %r — "
            "schema drift or plan from incompatible deploy", cond_type
        )
        return False
    try:
        return bool(fn(cond, ctx))
    except Exception as e:
        _log.warning(
            "snow.evaluators: %s raised %s: %s — returning False "
            "(evaluators should handle missing data internally)",
            cond_type, type(e).__name__, e
        )
        return False


def _evaluate_stateful(
    cond_type: str,
    fn: Callable[[Any, EvalContext, Any], bool],
    cond: Any,
    ctx: EvalContext,
    plan_id: Optional[str],
    contingency_name: Optional[str],
    condition_index: Optional[int],
) -> bool:
    """Internal routing for stateful evaluators. Fetches/allocates the
    state row from `ctx.state_cache`, calls the evaluator, returns the
    fire bool. Marks the plan dirty (per-row stamp=False) so the loop's
    next flush picks up the row's mutations."""
    if (
        ctx.state_cache is None
        or plan_id is None
        or contingency_name is None
        or condition_index is None
    ):
        _log.warning(
            "snow.evaluators: stateful %r without location/cache — "
            "plan_id=%r contingency=%r idx=%r cache=%s; returning False",
            cond_type, plan_id, contingency_name, condition_index,
            "set" if ctx.state_cache is not None else "None",
        )
        return False
    try:
        row = ctx.state_cache.get_or_create(
            plan_id=plan_id,
            contingency_name=contingency_name,
            condition_index=condition_index,
            cond_type=cond_type,
        )
        result = bool(fn(cond, ctx, row))
        # Per-row staleness semantics: stamp the row directly, mark the
        # plan dirty without touching sibling rows' last_seen_at.
        ctx.state_cache.mark_updated(plan_id, stamp=False)
        return result
    except Exception as e:
        _log.warning(
            "snow.evaluators: stateful %s raised %s: %s — returning False",
            cond_type, type(e).__name__, e,
        )
        return False


# Expose the registry for diagnostics — NOT for mutation. A defensive
# copy is returned to callers.
def registered_condition_types() -> list[str]:
    """Return the list of condition-type strings this dispatch knows
    how to route. Useful for admin / debug endpoints."""
    return sorted(set(_DISPATCH.keys()) | set(_DISPATCH_STATEFUL.keys()))


def stateful_condition_types() -> list[str]:
    """Return the list of stateful condition types — those that require
    a `ConditionStateRow` allocated via `ctx.state_cache`."""
    return sorted(_DISPATCH_STATEFUL.keys())
