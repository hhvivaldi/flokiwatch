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
from snow.schema import (
    ATR,
    BollingerPosition,
    EMARelation,
    IndicatorDivergence,
    MACDHistogram,
    RSI,
    Stochastic,
)


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


# =============================================================================
# Phase 7.3 (FLO-355) Cat A indicator evaluators
# =============================================================================

def evaluate_bollinger_position(cond: BollingerPosition, ctx: EvalContext) -> bool:
    """Bollinger position relations.

    `position` is Brain's 0..1 normalised value (0 == lower band, 1 == upper).
    Values >1 mean price has CLOSED above upper band; <0 below lower.
    `above_upper` / `below_lower` use strict inequality against the
    band itself (position > 1 / position < 0). The half-band relations
    use 0.5 as the middle reference.

    Squeeze is Brain's pre-computed bool — True iff bb_width is
    materially compressed (Brain's threshold), False otherwise.
    """
    bb = ctx.live_data.bollinger(tf=cond.tf)
    if not isinstance(bb, dict):
        return False

    if cond.relation == "in_squeeze":
        return bool(bb.get("squeeze") is True)

    pos = bb.get("position")
    if not isinstance(pos, (int, float)):
        return False
    pos = float(pos)
    if cond.relation == "above_upper":
        return pos > 1.0
    if cond.relation == "below_lower":
        return pos < 0.0
    if cond.relation == "above_middle":
        return pos > 0.5
    if cond.relation == "below_middle":
        return pos < 0.5
    return False


def evaluate_stochastic(cond: Stochastic, ctx: EvalContext) -> bool:
    val: Optional[float] = ctx.live_data.stochastic(tf=cond.tf)
    if val is None:
        return False
    return _apply_op(val, cond.op, cond.threshold)


def evaluate_indicator_divergence(
    cond: IndicatorDivergence, ctx: EvalContext
) -> bool:
    """True iff Brain currently detects divergence on `cond.indicator`
    matching `cond.direction`. Reads the boolean state Brain publishes;
    Snow itself does no peak-detection."""
    if cond.indicator == "macd":
        div = ctx.live_data.macd_divergence(tf="H1")
    else:
        return False  # other indicators not yet supported by Brain

    if not isinstance(div, dict):
        return False
    if not div.get("detected"):
        return False
    return str(div.get("type") or "") == str(cond.direction)
