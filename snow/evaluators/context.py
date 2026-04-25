"""EvalContext — the bundle every evaluator primitive receives.

Kept deliberately flat so test fixtures can construct one without a
live bot. All fields are immutable-ish references; primitives must
treat ctx as read-only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only import avoids cycles
    from snow.live_data import LiveData
    from snow.semantic_cache import SemanticCache
    from snow.evaluators.tracker import PerPlanTracker
    from snow.schema import Plan
    from snow.state import PerConditionStateCache


# XAUUSD pip size. Matches capture.py:19 and is the one magic number
# the evaluator layer needs — any other pip arithmetic comes via this
# constant so a future symbol change touches one spot.
PIP_SIZE: float = 0.1


@dataclass
class EvalContext:
    """Bundle of state handed to every evaluator invocation.

    Fields:
      live_data       — fresh per-tick MT5 data (LiveData.refresh() has
                        already run by the time we dispatch).
      semantic_cache  — Floki-cycle cached snapshot (indicators H1+,
                        SR zones, fibonacci, regime).
      tracker         — per-plan state container (MFE/MAE/peak
                        tracker). Only the 4 position-state primitives
                        touch it.
      plan            — the Plan being evaluated.
      ticket          — broker ticket if plan has entered, else None.
                        None = plan in PENDING state; position-state
                        primitives MUST short-circuit to False.
      now             — optional injected UTC datetime for deterministic
                        time-window tests. None = read wall clock.
    """
    live_data: "LiveData"
    semantic_cache: "SemanticCache"
    tracker: "PerPlanTracker"
    plan: "Plan"
    ticket: Optional[int] = None
    now: Optional[datetime] = None
    # FLO-359 Phase 8b commit 3 — stateful primitives need per-condition
    # memory across ticks. Stateless evaluators ignore this field;
    # stateful evaluators short-circuit to False (with WARN) if it is
    # None, so plan eval stays fail-safe even when a caller forgot to
    # plumb the cache through.
    state_cache: Optional["PerConditionStateCache"] = None
