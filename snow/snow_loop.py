"""Snow main loop — FLO-347 Phase 4 (DRY RUN).

Wires `LiveData` + `SemanticCache` + `PerPlanTracker` + `evaluate_condition`
into a 5-second tick loop. The loop runs as a daemon thread launched from
`main.py` (Phase 4.5) and, per tick, rehydrates every non-terminal plan from
the `snow_plans` table and evaluates its conditions.

DRY-RUN-only in this phase:
  * Flipping `config.SNOW_DRY_RUN` to False raises `NotImplementedError`
    — Phase 5 (`snow/actions.py`) is the prerequisite for live fires.
  * No executor / agent_tools / ai_agent imports. Structural boundary is
    enforced by `snow/tests/snow_loop_test.py::TestBoundaryCompliance`.
  * When conditions evaluate to `all-true`, the loop writes a
    `*_would_fire` row into `snow_evaluations` and logs a line — the
    plan's status, `trade_ticket`, and outcome columns are NEVER mutated.

Error-isolation contract (RFC §5.4 + CTO Phase 4 directive):
  1. Per-primitive: evaluators already fail-safe to False on exception (Phase 3b).
  2. Per-plan: `_evaluate_plan_safe` swallows + logs + records an
     `evaluation_error` audit row. The loop continues to the next plan.
  3. Per-tick: `_tick` outer try/except so one corrupt plan cannot kill
     the cycle.
  4. Per-thread: `run_forever` outer try/except — the daemon thread NEVER
     dies while `bot.running`.

Tracker lifecycle:
  * Seeded lazily on first observation of `status == ACTIVE`. Entry-price
    source in Phase 4 is a PLACEHOLDER (see `_pick_entry_price_placeholder`).
    Phase 5 MUST replace this with the broker fill price captured at the
    `execute_trade` call site.
  * Forgotten immediately when a previously-seen plan_id drops out of the
    active set (terminal transition). An orphan sweep every 60 ticks
    provides belt-and-suspenders cleanup.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

import config
from snow import db as snow_db
from snow.evaluators import EvalContext, PerPlanTracker, evaluate_condition
from snow.live_data import LiveData
from snow.schema import (
    ContingencyState,
    Direction,
    Plan,
    PlanStatus,
)
from snow.semantic_cache import SemanticCache

log = logging.getLogger(__name__)


CYCLE_INTERVAL_SECONDS: float = 5.0
SHUTDOWN_POLL_SECONDS: float = 1.0
TIMING_LOG_INTERVAL_TICKS: int = 60         # 5 minutes at 5s cadence
TIMING_WARN_THRESHOLD_MS: float = 200.0
ORPHAN_SWEEP_INTERVAL_TICKS: int = 60


class SnowLoop:
    """5-second cadence evaluation loop over all active Snow plans.

    Public API:
      loop = SnowLoop(bot, symbol="XAUUSD")
      loop.run_forever()         # blocks until bot.running is False

    Test surface (optional kwargs):
      live_data / semantic_cache / tracker — inject fakes
      dry_run                              — override config flag
    """

    def __init__(
        self,
        bot: Any,
        *,
        symbol: Optional[str] = None,
        live_data: Optional[Any] = None,
        semantic_cache: Optional[Any] = None,
        tracker: Optional[PerPlanTracker] = None,
        dry_run: Optional[bool] = None,
    ) -> None:
        self._bot = bot
        self._symbol = symbol or getattr(config, "SYMBOL", "XAUUSD")

        dry = bool(getattr(config, "SNOW_DRY_RUN", True) if dry_run is None else dry_run)
        if not dry:
            raise NotImplementedError(
                "Phase 4 supports DRY RUN only. "
                "Set SNOW_DRY_RUN=True until Phase 5 ships snow/actions.py."
            )
        self._dry_run = dry

        self._semantic = semantic_cache or SemanticCache(
            lambda: getattr(self._bot, "_last_agent_data", None)
        )
        self._live_data = live_data or LiveData(self._symbol, self._semantic)
        self._tracker = tracker or PerPlanTracker()

        self._tick_count: int = 0
        # plan_id → status seen on the previous tick; used for terminal
        # transition detection (plan drops out of the active set).
        self._last_known_status: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        """Daemon-thread main loop. Returns when `bot.running` is False."""
        log.info(
            "snow.loop.start symbol=%s dry_run=%s cycle_s=%.1f",
            self._symbol, self._dry_run, CYCLE_INTERVAL_SECONDS,
        )
        try:
            while self._is_running():
                try:
                    self._tick()
                except Exception:
                    log.exception("snow.loop.tick_uncaught tick=%d", self._tick_count)
                self._interruptible_sleep(CYCLE_INTERVAL_SECONDS)
        finally:
            log.info("snow.loop.stop ticks=%d", self._tick_count)

    def _is_running(self) -> bool:
        return bool(getattr(self._bot, "running", False))

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in SHUTDOWN_POLL_SECONDS chunks so stop latency ≤ 1 s."""
        deadline = time.monotonic() + float(seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._is_running():
                return
            time.sleep(min(SHUTDOWN_POLL_SECONDS, remaining))

    # ------------------------------------------------------------------
    # Per-tick orchestration
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        t0 = time.monotonic()
        self._tick_count += 1

        # One refresh per tick; all plans share the same snapshot.
        try:
            self._live_data.refresh()
        except Exception:
            log.exception("snow.loop.live_data_refresh_failed")
            # Keep going — evaluators return False on missing data (fail-safe).

        try:
            active_rows = snow_db.get_active_plans()
        except Exception:
            log.exception("snow.loop.get_active_plans_failed")
            active_rows = []

        for row in active_rows:
            self._evaluate_plan_safe(row)

        self._detect_terminal_transitions(active_rows)

        if self._tick_count % ORPHAN_SWEEP_INTERVAL_TICKS == 0:
            self._orphan_sweep(active_rows)

        duration_ms = (time.monotonic() - t0) * 1000.0
        if self._tick_count % TIMING_LOG_INTERVAL_TICKS == 0:
            log.info(
                "snow.cycle_timing duration_ms=%.1f plans=%d tick=%d",
                duration_ms, len(active_rows), self._tick_count,
            )
        if duration_ms > TIMING_WARN_THRESHOLD_MS:
            log.warning(
                "snow.cycle_slow duration_ms=%.1f plans=%d threshold_ms=%.0f",
                duration_ms, len(active_rows), TIMING_WARN_THRESHOLD_MS,
            )

    # ------------------------------------------------------------------
    # Per-plan evaluation (error-isolated)
    # ------------------------------------------------------------------
    def _evaluate_plan_safe(self, row: dict[str, Any]) -> None:
        plan_id = row.get("id") or "<unknown>"
        try:
            self._evaluate_plan(row)
        except Exception as exc:
            log.exception("snow.plan.evaluation_error plan_id=%s", plan_id)
            try:
                snow_db.record_evaluation(
                    plan_id=plan_id,
                    contingency_name="_loop",
                    event="evaluation_error",
                    conditions_snapshot={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:200],
                    },
                )
            except Exception:
                log.exception(
                    "snow.plan.evaluation_error_audit_failed plan_id=%s", plan_id,
                )
        finally:
            try:
                snow_db.update_plan_last_evaluated(plan_id)
            except Exception:
                log.exception(
                    "snow.plan.last_evaluated_update_failed plan_id=%s", plan_id,
                )

    def _evaluate_plan(self, row: dict[str, Any]) -> None:
        plan_id = row["id"]
        plan = snow_db.get_plan_as_model(plan_id)
        if plan is None:
            log.warning("snow.plan.hydrate_failed plan_id=%s", plan_id)
            return

        # Canonical mutable fields live on the DB row, NOT the rehydrated
        # Pydantic model. `plan_json` is frozen at submit time (status
        # always "pending"); column updates via update_plan_status /
        # update_plan_trade_ticket are the source of truth.
        status_value = str(row.get("status") or "")
        current_ticket = row.get("trade_ticket")
        self._last_known_status[plan_id] = status_value

        # Tracker lifecycle — seed on first observation of ACTIVE.
        self._maybe_seed_tracker(plan, status_value)

        # Per-tick MFE/MAE/peak refresh for position-state evaluators.
        if self._tracker.has(plan_id):
            current = self._safe_mid_price()
            if current is not None:
                self._tracker.update_price(plan_id, current)

        if status_value == PlanStatus.PENDING.value:
            self._evaluate_entry(plan)
        elif status_value == PlanStatus.ACTIVE.value:
            self._evaluate_management_and_exit(plan, current_ticket)
        # TRIGGERED and CLOSING are transient states owned by Phase 5's
        # action module; skip them here so DRY RUN never races broker calls.

    # ------------------------------------------------------------------
    # Entry-conditions path
    # ------------------------------------------------------------------
    def _evaluate_entry(self, plan: Plan) -> None:
        ctx = EvalContext(
            live_data=self._live_data,
            semantic_cache=self._semantic,
            tracker=self._tracker,
            plan=plan,
            ticket=None,  # pre-entry → no ticket
        )
        fires, snapshot = self._evaluate_conditions(plan.entry.conditions, ctx, plan.id, "_entry")
        if fires:
            _record_safely(
                snow_db.record_evaluation,
                plan_id=plan.id,
                contingency_name="_entry",
                event="entry_would_fire",
                conditions_snapshot=snapshot,
            )
            log.info(
                "snow.entry.would_fire plan_id=%s direction=%s dry_run=True",
                plan.id, _direction_label(plan.entry.direction),
            )

    # ------------------------------------------------------------------
    # Management + exit path
    # ------------------------------------------------------------------
    def _evaluate_management_and_exit(
        self, plan: Plan, ticket: Optional[int]
    ) -> None:
        ctx = EvalContext(
            live_data=self._live_data,
            semantic_cache=self._semantic,
            tracker=self._tracker,
            plan=plan,
            ticket=ticket,
        )
        for contingency in plan.management:
            self._evaluate_one_contingency(plan, contingency, ctx, kind="management")
        for contingency in plan.exit:
            self._evaluate_one_contingency(plan, contingency, ctx, kind="exit")

    def _evaluate_one_contingency(
        self,
        plan: Plan,
        contingency: Any,
        ctx: EvalContext,
        *,
        kind: str,
    ) -> None:
        state_value = _state_value(contingency.state)
        if state_value != ContingencyState.ARMED.value:
            return
        fires, snapshot = self._evaluate_conditions(
            contingency.conditions, ctx, plan.id, contingency.name,
        )
        if fires:
            _record_safely(
                snow_db.record_evaluation,
                plan_id=plan.id,
                contingency_name=contingency.name,
                event=f"{kind}_would_fire",
                conditions_snapshot=snapshot,
            )
            log.info(
                "snow.%s.would_fire plan_id=%s name=%s dry_run=True",
                kind, plan.id, contingency.name,
            )

    def _evaluate_conditions(
        self,
        conditions: list,
        ctx: EvalContext,
        plan_id: str,
        label: str,
    ) -> tuple[bool, dict[str, Any]]:
        """AND-fold over conditions; short-circuit on first False.

        Returns (all_true, snapshot_dict). Snapshot records each condition's
        class name + bool result, suitable for JSON storage in
        snow_evaluations.conditions_snapshot.
        """
        snapshot: dict[str, Any] = {}
        all_true = True
        for idx, cond in enumerate(conditions):
            key = f"c{idx}_{type(cond).__name__}"
            try:
                result = bool(evaluate_condition(cond, ctx))
            except Exception:
                log.exception(
                    "snow.condition.eval_error plan_id=%s label=%s idx=%d",
                    plan_id, label, idx,
                )
                result = False
            snapshot[key] = result
            if not result:
                all_true = False
                break
        return all_true, snapshot

    # ------------------------------------------------------------------
    # Tracker lifecycle
    # ------------------------------------------------------------------
    def _maybe_seed_tracker(self, plan: Plan, status_value: str) -> None:
        if status_value != PlanStatus.ACTIVE.value:
            return
        if self._tracker.has(plan.id):
            return
        entry_price = self._pick_entry_price_placeholder(plan)
        if entry_price is None:
            log.warning(
                "snow.tracker.seed_skipped plan_id=%s reason=no_entry_price",
                plan.id,
            )
            return
        direction = plan.entry.direction
        # TODO(FLO-347 Phase 5): replace this placeholder with the actual
        # broker fill price captured at execute_trade time. Market entries
        # currently use live mid-price, which diverges from the real fill
        # in fast markets. Phase 5 moves the seed call to the post-fill
        # code path (snow/actions.py) so entry_price is always authoritative.
        self._tracker.seed(plan.id, entry_price, direction)
        log.info(
            "snow.tracker.seed plan_id=%s entry_price=%.5f direction=%s placeholder=True",
            plan.id, entry_price, _direction_label(direction),
        )

    def _pick_entry_price_placeholder(self, plan: Plan) -> Optional[float]:
        """Best-effort entry-price source for the tracker seed placeholder.

        EntryBlock is market-only in the v1 schema (no `level` field), so
        we fall back to the current live mid. Returns None when the tick
        stream is unavailable — caller will log + skip the seed and retry
        next tick.
        """
        return self._safe_mid_price()

    def _safe_mid_price(self) -> Optional[float]:
        try:
            return self._live_data.price("mid")
        except Exception:
            log.exception("snow.live_data.price_call_failed")
            return None

    def _detect_terminal_transitions(self, active_rows: list[dict[str, Any]]) -> None:
        """Forget tracker state for any plan_id we saw last tick but not this.

        Terminal transitions (CLOSED / CANCELLED / EXPIRED / FAILED) remove
        the plan from `get_active_plans()`. Comparing against the previous
        tick's known-status set catches these without touching DB again.
        """
        current_ids = {r.get("id") for r in active_rows}
        for plan_id in list(self._last_known_status.keys()):
            if plan_id in current_ids:
                continue
            if self._tracker.has(plan_id):
                self._tracker.forget(plan_id)
                log.info(
                    "snow.tracker.forget plan_id=%s reason=terminal", plan_id,
                )
            del self._last_known_status[plan_id]

    def _orphan_sweep(self, active_rows: list[dict[str, Any]]) -> None:
        """Defensive cleanup — drop tracker state for any plan not in the
        active set. Catches whatever the primary `_detect_terminal_transitions`
        path missed (e.g., the very first tick after a restart)."""
        current_ids = {r.get("id") for r in active_rows}
        # Snapshot via the tracker's public API surface — avoid touching
        # private attributes. We probe `has()` per plan_id we've ever seen
        # plus plans currently active; anything `has()` True but absent
        # from current_ids is orphaned.
        candidate_ids = set(self._last_known_status.keys())
        for plan_id in candidate_ids:
            if plan_id in current_ids:
                continue
            if self._tracker.has(plan_id):
                self._tracker.forget(plan_id)
                log.info(
                    "snow.tracker.forget plan_id=%s reason=orphan_sweep",
                    plan_id,
                )


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------
def _status_value(status: Any) -> str:
    return status.value if isinstance(status, PlanStatus) else str(status)


def _state_value(state: Any) -> str:
    return state.value if isinstance(state, ContingencyState) else str(state)


def _direction_label(direction: Any) -> str:
    return direction.value if isinstance(direction, Direction) else str(direction)


def _record_safely(fn, **kwargs) -> None:
    """record_evaluation wrapper — loop survives audit-log failures."""
    try:
        fn(**kwargs)
    except Exception:
        log.exception(
            "snow.loop.record_evaluation_failed plan_id=%s event=%s",
            kwargs.get("plan_id"), kwargs.get("event"),
        )


def run_forever(bot: Any, symbol: Optional[str] = None) -> None:
    """Module-level entry point for `threading.Thread(target=...)`.

    Mirrors the calling convention documented in RFC §5.1:
        threading.Thread(target=snow_loop.run_forever, args=(self,), ...)
    """
    loop = SnowLoop(bot, symbol=symbol)
    loop.run_forever()
