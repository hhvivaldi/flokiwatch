"""Snow evaluator package — 14 condition primitives + dispatch.

Public surface kept narrow. The Snow loop (Phase 4) calls:
    from snow.evaluators import evaluate_condition, PerPlanTracker, EvalContext

Internally split by category for review-friendly file sizes:
    price.py          — price_above, price_below
    indicator.py      — rsi, macd_histogram, ema_relation, atr
    structural.py     — price_at_sr_zone, price_at_fibonacci
    position.py       — profit_pips, mfe_reached, mae_reached,
                        profit_retraced_from_peak  (stateful; use PerPlanTracker)
    time_window.py    — duration_exceeds, time_between
    dispatch.py       — type string → evaluator function map + evaluate_condition()
    tracker.py        — PerPlanTracker; in-memory state keyed by plan_id
    context.py        — EvalContext dataclass
"""
from __future__ import annotations

from snow.evaluators.context import EvalContext
from snow.evaluators.dispatch import evaluate_condition
from snow.evaluators.tracker import PerPlanTracker

__all__ = ["EvalContext", "PerPlanTracker", "evaluate_condition"]
