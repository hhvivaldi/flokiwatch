"""Indicator evaluators: rsi, macd_histogram, ema_relation, atr.

All delegate to LiveData for the current value; LiveData in turn
delegates H1+ timeframes to SemanticCache per RFC §6.1. Missing
data (None from LiveData) → False (fail-safe, RFC §6.5).

`ComparisonOp` is `Literal["above", "below"]`; we treat "above" as
strict > and "below" as strict <. Equality never fires either side.
"""
from __future__ import annotations

from typing import Optional

from snow.evaluators.context import EvalContext, PIP_SIZE
from snow.schema import ATR, EMARelation, MACDHistogram, RSI


def _apply_op(value: float, op: str, threshold: float) -> bool:
    if op == "above":
        return value > threshold
    if op == "below":
        return value < threshold
    return False


def evaluate_rsi(cond: RSI, ctx: EvalContext) -> bool:
    val: Optional[float] = ctx.live_data.rsi(tf=cond.tf, period=14)
    if val is None:
        return False
    return _apply_op(val, cond.op, cond.threshold)


def evaluate_macd_histogram(cond: MACDHistogram, ctx: EvalContext) -> bool:
    val: Optional[float] = ctx.live_data.macd_histogram(tf=cond.tf)
    if val is None:
        return False
    return _apply_op(val, cond.op, cond.threshold)


def evaluate_ema_relation(cond: EMARelation, ctx: EvalContext) -> bool:
    """Four kinds of EMA relation per schema.EMARelationKind:
      price_above  — current price > EMA(tf, period)
      price_below  — current price < EMA(tf, period)
      aligned_bull — EMA9 > EMA21 > EMA50 > EMA200  (all on `tf`)
      aligned_bear — EMA9 < EMA21 < EMA50 < EMA200
    Any missing EMA value → False."""
    if cond.relation in ("price_above", "price_below"):
        price = ctx.live_data.price("mid")
        ema = ctx.live_data.ema(tf=cond.tf, period=cond.period)
        if price is None or ema is None:
            return False
        return (price > ema) if cond.relation == "price_above" else (price < ema)

    # Alignment checks — need all four periods
    ema9   = ctx.live_data.ema(tf=cond.tf, period=9)
    ema21  = ctx.live_data.ema(tf=cond.tf, period=21)
    ema50  = ctx.live_data.ema(tf=cond.tf, period=50)
    ema200 = ctx.live_data.ema(tf=cond.tf, period=200)
    if None in (ema9, ema21, ema50, ema200):
        return False
    if cond.relation == "aligned_bull":
        return ema9 > ema21 > ema50 > ema200   # type: ignore[operator]
    if cond.relation == "aligned_bear":
        return ema9 < ema21 < ema50 < ema200   # type: ignore[operator]
    return False


def evaluate_atr(cond: ATR, ctx: EvalContext) -> bool:
    """ATR comparison per RFC §2.5 #6:
        threshold_price = cond.multiplier × cond.baseline_pips × PIP_SIZE
        result          = atr_value {op} threshold_price

    Example — `multiplier=1.5, baseline_pips=100`, so threshold = 15.0
    price units (= 150 pips at 0.1 PIP_SIZE). If M1 ATR is 13.0,
    `op="above"` evaluates False (13.0 not > 15.0).
    """
    val: Optional[float] = ctx.live_data.atr(tf=cond.tf, period=14)
    if val is None:
        return False
    threshold_price = cond.multiplier * cond.baseline_pips * PIP_SIZE
    return _apply_op(val, cond.op, threshold_price)
