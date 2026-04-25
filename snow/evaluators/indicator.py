"""Indicator evaluators.

Stateless: rsi, macd_histogram, ema_relation, atr, bollinger_position,
stochastic, indicator_divergence — delegate to LiveData for the
current value; LiveData in turn delegates H1+ timeframes to
SemanticCache per RFC §6.1. Missing data (None from LiveData) →
False (fail-safe, RFC §6.5).

Stateful (FLO-359 Phase 8b commit 3): indicator_crossover — reads
prev_value / prev_above_threshold from a `ConditionStateRow`,
detects the crossing, then writes back the new prev for the next
tick. Cold-start (no prev) seeds + reports no crossing on the first
tick (RFC §3.1 false-negative window).

`ComparisonOp` is `Literal["above", "below"]`; we treat "above" as
strict > and "below" as strict <. Equality never fires either side.
The crossover evaluator additionally treats `curr == threshold` as
ambiguous and preserves the last definite state per RFC §3.1.
"""
from __future__ import annotations

from typing import Optional

from logger import log
from tz_utils import utc_iso

from snow.evaluators.context import EvalContext, PIP_SIZE
from snow.schema import (
    ATR,
    BollingerPosition,
    EMARelation,
    IndicatorCrossover,
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


# =============================================================================
# Stateful: indicator_crossover (FLO-359 Phase 8b commit 3)
# =============================================================================

def _read_indicator(name: str, tf: str, ctx: EvalContext) -> Optional[float]:
    """Resolve a CrossoverIndicator literal to a current scalar value.

    Returns None on missing data — the crossover evaluator preserves
    state across a missing tick rather than treating the gap as a
    crossing event."""
    if name == "rsi":
        return ctx.live_data.rsi(tf=tf, period=14)
    if name == "macd_histogram":
        return ctx.live_data.macd_histogram(tf=tf)
    if name == "stochastic":
        return ctx.live_data.stochastic(tf=tf)
    return None


def evaluate_indicator_crossover(
    cond: IndicatorCrossover,
    ctx: EvalContext,
    state,  # ConditionStateRow — non-typed import to avoid module cycle
) -> bool:
    """Fire on the FIRST tick after `cond.indicator` crosses
    `cond.threshold` in `cond.direction`.

    Detection rule (RFC §3.1):
      curr_above = curr_value > threshold        # strict
      curr_below = curr_value < threshold        # strict
      direction == "above": fires iff (not prev_above) AND curr_above
      direction == "below": fires iff prev_above AND curr_below

    Equality (`curr == threshold`): ambiguous; the state row's
    `prev_above_threshold` is NOT updated, so the next definite
    reading still has the old prev as its reference. This avoids
    spurious fires when a value parks exactly on the threshold.

    Cold-start (`state.prev_above_threshold is None`): seed
    state.prev_value / state.prev_above_threshold from the current
    reading and return False. The documented one-tick false-negative
    window after a fresh allocation OR a stale-rehydrate drop.

    Missing live data (`curr_value is None`): return False AND do
    NOT mutate state. Preserves prev for the next tick when data
    is back.
    """
    curr = _read_indicator(cond.indicator, cond.tf, ctx)
    if curr is None:
        return False

    curr_above = curr > cond.threshold
    curr_below = curr < cond.threshold
    is_definite = curr_above or curr_below  # False iff curr == threshold

    # Cold-start path — seed and report no crossing.
    if state.prev_above_threshold is None:
        if is_definite:
            state.prev_value = float(curr)
            state.prev_above_threshold = bool(curr_above)
            state.last_seen_at = utc_iso()
        return False

    prev_above = bool(state.prev_above_threshold)
    fired = False
    if cond.direction == "above":
        fired = (not prev_above) and curr_above
    elif cond.direction == "below":
        fired = prev_above and curr_below

    # Update prev only on a definite reading. Equality preserves the
    # last definite state.
    if is_definite:
        state.prev_value = float(curr)
        state.prev_above_threshold = bool(curr_above)
    state.last_seen_at = utc_iso()

    return fired
