"""Position-state evaluators: profit_pips, mfe_reached, mae_reached,
profit_retraced_from_peak.

All four short-circuit to False when:
  * ctx.ticket is None (plan has not entered yet), OR
  * tracker has no state for ctx.plan.id (pre-seed or already forgotten).

Sign conventions (advisor decisions):
  profit_pips              — SIGNED. Positive = winning, negative = losing.
  mfe_pips                 — positive magnitude (always ≥ 0).
  mae_pips                 — positive magnitude representing drawdown (≥ 0).
  profit_retraced_from_peak — positive magnitude; 0 when peak was never > 0.
"""
from __future__ import annotations

from snow.evaluators.context import EvalContext
from snow.schema import (
    MAEReached,
    MFEReached,
    ProfitPips,
    ProfitRetracedFromPeak,
)


def _apply_op(value: float, op: str, threshold: float) -> bool:
    if op == "above":
        return value > threshold
    if op == "below":
        return value < threshold
    return False


def evaluate_profit_pips(cond: ProfitPips, ctx: EvalContext) -> bool:
    """Signed profit in pips, compared per cond.op against cond.threshold.

    Example — BUY trade +15 pips: passes `op="above", threshold=10`.
    Example — SELL trade -8 pips: passes `op="below", threshold=-5`.
    """
    if ctx.ticket is None:
        return False
    current_price = ctx.live_data.price("mid")
    if current_price is None:
        return False
    profit = ctx.tracker.profit_pips(ctx.plan.id, current_price)
    if profit is None:
        return False
    return _apply_op(profit, cond.op, cond.threshold)


def evaluate_mfe_reached(cond: MFEReached, ctx: EvalContext) -> bool:
    """True iff MFE so far ≥ cond.pips. MFE is unsigned magnitude —
    a trade that never went into profit has MFE = 0, so any positive
    threshold evaluates False until the trade actually ticks favourably."""
    if ctx.ticket is None:
        return False
    mfe = ctx.tracker.mfe_pips(ctx.plan.id)
    if mfe is None:
        return False
    return mfe >= cond.pips


def evaluate_mae_reached(cond: MAEReached, ctx: EvalContext) -> bool:
    """True iff MAE so far ≥ cond.pips. MAE is the POSITIVE drawdown
    magnitude: a BUY trade entered at 4700 that dipped to 4690 has
    MAE = 100 pips (not -100)."""
    if ctx.ticket is None:
        return False
    mae = ctx.tracker.mae_pips(ctx.plan.id)
    if mae is None:
        return False
    return mae >= cond.pips


def evaluate_profit_retraced_from_peak(
    cond: ProfitRetracedFromPeak, ctx: EvalContext
) -> bool:
    """True iff pips retraced from peak ≥ cond.pips.

    Edge case (advisor item #3): trades that never reached profit have
    peak_profit_pips = 0. The tracker returns 0.0 for retracement in
    that case — condition is False for any positive threshold.
    Stateless semantics: "retrace N from the BEST so far" requires the
    best to be better than flat first."""
    if ctx.ticket is None:
        return False
    current_price = ctx.live_data.price("mid")
    if current_price is None:
        return False
    retrace = ctx.tracker.retrace_from_peak(ctx.plan.id, current_price)
    if retrace is None:
        return False
    return retrace >= cond.pips
