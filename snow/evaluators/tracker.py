"""PerPlanTracker — in-memory per-plan state for position-state evaluators.

Owned by the Snow loop (one instance per process). Seeded on the
plan's PENDING → TRIGGERED transition (entry fired, we now know the
fill price), updated every tick with the current market price, and
forgotten when the plan reaches a terminal status.

State per plan (see `PlanState` dataclass):
    entry_price    — fill price from the broker (positive float).
    direction      — BUY or SELL; sets sign convention for profit/MFE/MAE.
    mfe_pips       — max favourable excursion, positive magnitude.
    mae_pips       — max adverse excursion, positive magnitude (NOT signed).
    peak_profit_pips — largest profit observed to date; floor at 0
                       (never-in-profit trades stay at 0, not negative).
    seeded_at      — monotonic timestamp; used for staleness diagnostics.

Contract for the position-state primitives:

  profit_pips(current_price)      — signed; positive = winning, negative = losing
  mfe_pips()                      — positive magnitude; 0 if never favourable
  mae_pips()                      — positive magnitude (drawdown); 0 if never adverse
  retrace_from_peak(current_price)— positive magnitude; 0 if peak_profit_pips == 0
                                    (never-in-profit = no retracement)

Thread-safety:
  All public methods acquire `self._lock` (an RLock). Re-entry safe
  (one evaluator call may compose two queries). Contention model:
  the Snow loop calls `update_price()` once per tick, then reads
  inside the same tick. Other threads (dashboard API, debugging)
  may read concurrently; locks keep the view consistent.

Lifecycle:
  * `seed(plan_id, entry_price, direction)` — on entry fill. Idempotent:
    calling again overwrites (rarely needed; surfaces as a warning).
  * `update_price(plan_id, current_price)` — tick refresh. Advances
    MFE / MAE / peak_profit in one call so callers cannot desynchronise
    them.
  * `forget(plan_id)` — on terminal status. Idempotent: forgetting an
    unknown id is a silent no-op.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from snow.evaluators.context import PIP_SIZE
from snow.schema import Direction


@dataclass
class PlanState:
    entry_price: float
    direction: Direction
    mfe_pips: float = 0.0
    mae_pips: float = 0.0
    peak_profit_pips: float = 0.0
    seeded_at: float = 0.0


def _profit_pips_for(direction: Direction, entry: float, current: float) -> float:
    """Signed pip P&L for the given direction. Positive = winning."""
    if direction == Direction.BUY:
        return (current - entry) / PIP_SIZE
    # SELL: profit when price falls
    return (entry - current) / PIP_SIZE


class PerPlanTracker:

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plans: dict[str, PlanState] = {}

    # -- Lifecycle -----------------------------------------------------------

    def seed(
        self, plan_id: str, entry_price: float, direction: Direction
    ) -> None:
        """Register a plan on its entry-fire transition. If the plan is
        already seeded, overwrite (callers should not re-seed; treat as
        a signal something is off upstream, but do not crash)."""
        with self._lock:
            self._plans[plan_id] = PlanState(
                entry_price=float(entry_price),
                direction=direction,
                seeded_at=time.monotonic(),
            )

    def forget(self, plan_id: str) -> None:
        """Remove a plan's state. Idempotent — unknown ids are no-ops.
        Called by the Snow loop when a plan reaches a terminal status
        so memory doesn't balloon over process lifetime."""
        with self._lock:
            self._plans.pop(plan_id, None)

    def has(self, plan_id: str) -> bool:
        with self._lock:
            return plan_id in self._plans

    # -- Tick update ---------------------------------------------------------

    def update_price(self, plan_id: str, current_price: float) -> None:
        """Recompute MFE / MAE / peak_profit for `plan_id` at the given
        price. Must be called once per Snow tick per active plan BEFORE
        the evaluators for that plan run — otherwise the primitives see
        stale MFE/MAE.

        No-op for unknown plan_id (pre-seed / post-forget). The stateful
        primitives already check `tracker.has(plan_id)` and return False,
        so silent no-op here matches the fail-safe contract.
        """
        with self._lock:
            state = self._plans.get(plan_id)
            if state is None:
                return
            profit = _profit_pips_for(
                state.direction, state.entry_price, current_price
            )
            if profit > state.peak_profit_pips:
                state.peak_profit_pips = profit
            if profit > state.mfe_pips:
                state.mfe_pips = profit
            if profit < -state.mae_pips:
                # profit is more-negative than current -mae
                state.mae_pips = -profit

    # -- Queries (used by position.py primitives) ----------------------------

    def profit_pips(
        self, plan_id: str, current_price: float
    ) -> Optional[float]:
        """Signed P&L in pips at `current_price`. None if plan not seeded."""
        with self._lock:
            state = self._plans.get(plan_id)
            if state is None:
                return None
            return _profit_pips_for(
                state.direction, state.entry_price, current_price
            )

    def mfe_pips(self, plan_id: str) -> Optional[float]:
        """Max favourable excursion (positive magnitude). None if not seeded.
        0.0 if the trade has never been in profit."""
        with self._lock:
            state = self._plans.get(plan_id)
            return None if state is None else state.mfe_pips

    def mae_pips(self, plan_id: str) -> Optional[float]:
        """Max adverse excursion (positive magnitude representing drawdown).
        None if not seeded. 0.0 if the trade has never been in loss."""
        with self._lock:
            state = self._plans.get(plan_id)
            return None if state is None else state.mae_pips

    def retrace_from_peak(
        self, plan_id: str, current_price: float
    ) -> Optional[float]:
        """Pips retraced from the peak profit so far.

        Edge case (advisor item #3): if `peak_profit_pips == 0` — i.e.
        the trade has never been in profit — retracement is 0.0, not
        the unsigned drawdown. Trades that go directly negative have
        no peak to retrace from.

        None if not seeded.
        """
        with self._lock:
            state = self._plans.get(plan_id)
            if state is None:
                return None
            if state.peak_profit_pips <= 0.0:
                return 0.0
            current_profit = _profit_pips_for(
                state.direction, state.entry_price, current_price
            )
            retrace = state.peak_profit_pips - current_profit
            return max(retrace, 0.0)  # negative would mean new peak
