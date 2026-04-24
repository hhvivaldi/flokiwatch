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
from typing import Any, Callable

from snow.evaluators.context import EvalContext
from snow.evaluators.indicator import (
    evaluate_atr,
    evaluate_ema_relation,
    evaluate_macd_histogram,
    evaluate_rsi,
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
}


def evaluate_condition(cond: Any, ctx: EvalContext) -> bool:
    """Evaluate a single condition against the context. Fail-safe: any
    unknown type or escaping exception returns False (with a WARN log)
    so the Snow loop never crashes on a bad condition."""
    cond_type = getattr(cond, "type", None)
    if not isinstance(cond_type, str):
        _log.warning(
            "snow.evaluators: condition with no .type attribute: %r", cond
        )
        return False
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


# Expose the registry for diagnostics — NOT for mutation. A defensive
# copy is returned to callers.
def registered_condition_types() -> list[str]:
    """Return the list of condition-type strings this dispatch knows
    how to route. Useful for admin / debug endpoints."""
    return sorted(_DISPATCH.keys())
