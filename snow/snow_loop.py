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
from tz_utils import utc_iso
from snow import db as snow_db
from snow.evaluators import EvalContext, PerPlanTracker, evaluate_condition
from snow.live_data import LiveData
from snow.priority import FireEvent, resolve as priority_resolve
from snow.schema import (
    ContingencyFires,
    ContingencyState,
    Direction,
    Plan,
    PlanStatus,
)
from snow.semantic_cache import SemanticCache
from snow.state import PerConditionStateCache

log = logging.getLogger(__name__)


CYCLE_INTERVAL_SECONDS: float = 5.0
SHUTDOWN_POLL_SECONDS: float = 1.0
TIMING_LOG_INTERVAL_TICKS: int = 60         # 5 minutes at 5s cadence
TIMING_WARN_THRESHOLD_MS: float = 200.0
ORPHAN_SWEEP_INTERVAL_TICKS: int = 60
# FLO-359 Phase 8b commit 3 — flush the per-condition state cache to
# `snow_plans.state_cache_json` every N ticks. 60 ticks at 5 s = 5 min,
# matching the orphan-sweep cadence. Trade-off: more frequent = more DB
# writes; less frequent = larger restart-recovery loss window. Stale
# threshold (15 min) is the upper bound on this cadence.
STATE_FLUSH_INTERVAL_TICKS: int = 60


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
        actions: Optional[Any] = None,
        state_cache: Optional[PerConditionStateCache] = None,
    ) -> None:
        self._bot = bot
        self._symbol = symbol or getattr(config, "SYMBOL", "XAUUSD")

        dry = bool(getattr(config, "SNOW_DRY_RUN", True) if dry_run is None else dry_run)
        self._dry_run = dry

        self._semantic = semantic_cache or SemanticCache(
            lambda: getattr(self._bot, "_last_agent_data", None)
        )
        self._live_data = live_data or LiveData(self._symbol, self._semantic)
        self._tracker = tracker or PerPlanTracker()

        # Phase 5b: action dispatcher. In DRY RUN we never call it, but
        # constructing it is harmless (lazy executor import inside __init__).
        # Tests inject a fake; Phase 5a's priority module stays pure.
        if actions is None and not dry:
            from snow.actions import SnowActions
            actions = SnowActions()
        self._actions = actions

        # FLO-359 Phase 8b commit 3 — per-condition state cache.
        # Production reuses the module-level singleton; tests inject
        # fresh instances for isolation. Rehydrate happens at
        # run_forever() start so a fresh `SnowLoop()` for tests does
        # not touch DB until tests opt into it.
        if state_cache is None:
            from snow.state import state_cache as _global_state_cache
            state_cache = _global_state_cache
        self._state_cache = state_cache

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
        # FLO-359 Phase 8b commit 3 — restore per-condition state from
        # disk before evaluating any condition. Stale rows (last_seen_at
        # older than STALE_STATE_THRESHOLD_MINUTES) drop here; first
        # tick after rehydrate may see one false-negative on stateful
        # conditions whose state was dropped (RFC §5.2).
        try:
            n_loaded = self._state_cache.rehydrate_from_db()
            log.info(
                "snow.state.rehydrate loaded=%d cache_size=%d",
                n_loaded, len(self._state_cache),
            )
        except Exception:
            log.exception("snow.state.rehydrate_failed")
        try:
            while self._is_running():
                try:
                    self._tick()
                except Exception:
                    log.exception("snow.loop.tick_uncaught tick=%d", self._tick_count)
                self._interruptible_sleep(CYCLE_INTERVAL_SECONDS)
        finally:
            # Best-effort final flush so a clean shutdown persists the
            # latest state. Crashes still rely on the periodic flush
            # cadence — final flush is a nicety, not a guarantee.
            try:
                n_flushed = self._state_cache.flush_to_db()
                if n_flushed:
                    log.info("snow.state.final_flush plans=%d", n_flushed)
            except Exception:
                log.exception("snow.state.final_flush_failed")
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

        fires: list[FireEvent] = []
        for row in active_rows:
            self._evaluate_plan_safe(row, fires)

        # Phase 5b: priority-resolve + dispatch (LIVE) or record-with-priority (DRY RUN).
        if fires:
            ordered = priority_resolve(fires)
            if self._dry_run:
                self._record_dry_run_fires(ordered)
            else:
                self._dispatch_fires(ordered)

        self._detect_terminal_transitions(active_rows)

        if self._tick_count % ORPHAN_SWEEP_INTERVAL_TICKS == 0:
            self._orphan_sweep(active_rows)

        if self._tick_count % STATE_FLUSH_INTERVAL_TICKS == 0:
            try:
                n = self._state_cache.flush_to_db()
                if n:
                    log.debug(
                        "snow.state.flush plans=%d tick=%d",
                        n, self._tick_count,
                    )
            except Exception:
                log.exception("snow.state.flush_failed tick=%d", self._tick_count)

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
    def _evaluate_plan_safe(
        self, row: dict[str, Any], fires: list[FireEvent],
    ) -> None:
        plan_id = row.get("id") or "<unknown>"
        try:
            self._evaluate_plan(row, fires)
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

    def _evaluate_plan(
        self, row: dict[str, Any], fires: list[FireEvent],
    ) -> None:
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
            self._evaluate_entry(plan, fires)
        elif status_value == PlanStatus.ACTIVE.value:
            self._evaluate_management_and_exit(plan, current_ticket, fires)
        # TRIGGERED and CLOSING are transient states owned by the action
        # module; skip them here so the loop never races the retry loop.

    # ------------------------------------------------------------------
    # Entry-conditions path
    # ------------------------------------------------------------------
    def _evaluate_entry(self, plan: Plan, fires: list[FireEvent]) -> None:
        ctx = EvalContext(
            live_data=self._live_data,
            semantic_cache=self._semantic,
            tracker=self._tracker,
            plan=plan,
            ticket=None,  # pre-entry → no ticket
            state_cache=self._state_cache,
        )
        all_true, snapshot = self._evaluate_conditions(
            plan.entry.conditions, ctx, plan.id, "_entry",
        )
        if not all_true:
            return
        # Build a FirePayload. actions.py re-hydrates the plan to read
        # entry volume / SL / TP, so payload.action can be a bare
        # ActionExecuteMarket sentinel.
        from snow.actions import FirePayload
        from snow.schema import ActionExecuteMarket
        payload = FirePayload(
            action=ActionExecuteMarket(),
            kind="entry",
            plan_direction=plan.entry.direction,
            ticket=None,
            guards=None,
            entry_price=None,
        )
        fires.append(FireEvent(
            plan_id=plan.id,
            created_at=plan.created_at,
            contingency_name="_entry",
            action_type="execute_market",
            override=5,  # entry has no override; use default
            plan_list_order=-1,  # entry sorts first within a plan
            payload=payload,
            fired_at=utc_iso(),
        ))

    # ------------------------------------------------------------------
    # Management + exit path
    # ------------------------------------------------------------------
    def _evaluate_management_and_exit(
        self, plan: Plan, ticket: Optional[int], fires: list[FireEvent],
    ) -> None:
        ctx = EvalContext(
            live_data=self._live_data,
            semantic_cache=self._semantic,
            tracker=self._tracker,
            plan=plan,
            ticket=ticket,
            state_cache=self._state_cache,
        )
        # plan_list_order convention (see priority.py):
        #   entry = -1, management = 0..N-1, exit = 1000..1000+M-1
        for idx, contingency in enumerate(plan.management):
            self._evaluate_one_contingency(
                plan, contingency, ctx, fires,
                kind="management", plan_list_order=idx, ticket=ticket,
            )
        for idx, contingency in enumerate(plan.exit):
            self._evaluate_one_contingency(
                plan, contingency, ctx, fires,
                kind="exit", plan_list_order=1000 + idx, ticket=ticket,
            )

    def _evaluate_one_contingency(
        self,
        plan: Plan,
        contingency: Any,
        ctx: EvalContext,
        fires: list[FireEvent],
        *,
        kind: str,
        plan_list_order: int,
        ticket: Optional[int],
    ) -> None:
        state_value = _state_value(contingency.state)
        if state_value != ContingencyState.ARMED.value:
            return
        # FLO-373: enforce `fires: once` by checking the audit log.
        # Production code never transitions in-memory state out of ARMED,
        # so without this query a once-contingency would re-fire every
        # tick whose conditions stayed all-true (PLAN-20260426-002 hit
        # this 88×). The log is canonical and survives bot restart, so
        # recovery → reload → loop also respects prior fires.
        fires_value = _fires_value(contingency.fires)
        if fires_value == ContingencyFires.ONCE.value:
            if snow_db.has_contingency_fired_successfully(
                plan.id, contingency.name,
            ):
                return
        all_true, snapshot = self._evaluate_conditions(
            contingency.conditions, ctx, plan.id, contingency.name,
        )
        if not all_true:
            return
        from snow.actions import FirePayload
        # entry_price placeholder — Phase 4 tracker seed path uses live mid
        # or plan.entry; actions.py falls back to executor.get_positions()
        # open_price if this is None.
        entry_price = getattr(plan, "entered_at", None)  # not a price; placeholder None
        payload = FirePayload(
            action=contingency.action,
            kind=kind,
            plan_direction=plan.entry.direction,
            ticket=ticket,
            guards=contingency.guards,
            entry_price=None,  # actions.py resolves from live position
        )
        fires.append(FireEvent(
            plan_id=plan.id,
            created_at=plan.created_at,
            contingency_name=contingency.name,
            action_type=contingency.action.type,
            override=int(contingency.priority),
            plan_list_order=plan_list_order,
            payload=payload,
            fired_at=utc_iso(),
        ))

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
                result = bool(evaluate_condition(
                    cond, ctx,
                    plan_id=plan_id,
                    contingency_name=label,
                    condition_index=idx,
                ))
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
            # FLO-359 Phase 8b commit 3 — symmetrical state-cache cleanup.
            # Terminal plans never re-enter `_LIVE_PLAN_STATUSES`, so any
            # state cached for them is dead memory.
            try:
                forgot = self._state_cache.forget_plan(plan_id)
                if forgot:
                    log.info(
                        "snow.state.forget plan_id=%s rows=%d reason=terminal",
                        plan_id, forgot,
                    )
            except Exception:
                log.exception(
                    "snow.state.forget_failed plan_id=%s", plan_id,
                )
            del self._last_known_status[plan_id]

    # ------------------------------------------------------------------
    # Phase 5b: fire-queue handling
    # ------------------------------------------------------------------
    def _record_dry_run_fires(self, ordered: list[FireEvent]) -> None:
        """DRY RUN mode — record each fire to snow_evaluations with its
        effective_priority in the snapshot. Does NOT touch the executor.
        Priority order is reflected in insertion order (snow_evaluations.id
        is monotonic)."""
        for fire in ordered:
            kind = fire.payload.kind if hasattr(fire.payload, "kind") else "unknown"
            snapshot: dict[str, Any] = {
                "effective_priority": fire.effective_priority,
                "plan_list_order": fire.plan_list_order,
                "action_type": fire.action_type,
                "override": fire.override,
            }
            try:
                snow_db.record_evaluation(
                    plan_id=fire.plan_id,
                    contingency_name=fire.contingency_name,
                    event=f"{kind}_would_fire",
                    conditions_snapshot=snapshot,
                )
            except Exception:
                log.exception(
                    "snow.loop.dry_run_record_failed plan_id=%s contingency=%s",
                    fire.plan_id, fire.contingency_name,
                )
            log.info(
                "snow.%s.would_fire plan_id=%s name=%s priority=%d dry_run=True",
                kind, fire.plan_id, fire.contingency_name, fire.effective_priority,
            )

    def _dispatch_fires(self, ordered: list[FireEvent]) -> None:
        """LIVE mode — hand each fire to the action dispatcher in priority
        order. The dispatcher handles guards, retry, and state transitions.
        Per-fire errors MUST NOT kill the loop."""
        if self._actions is None:
            log.error("snow.loop.dispatch_without_actions — LIVE mode but no actions instance")
            return
        for fire in ordered:
            try:
                self._actions.execute_action(fire)
            except Exception:
                log.exception(
                    "snow.loop.dispatch_failed plan_id=%s contingency=%s",
                    fire.plan_id, fire.contingency_name,
                )

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


def _fires_value(fires: Any) -> str:
    return fires.value if isinstance(fires, ContingencyFires) else str(fires)


def _direction_label(direction: Any) -> str:
    return direction.value if isinstance(direction, Direction) else str(direction)


def run_forever(bot: Any, symbol: Optional[str] = None) -> None:
    """Module-level entry point for `threading.Thread(target=...)`.

    Mirrors the calling convention documented in RFC §5.1:
        threading.Thread(target=snow_loop.run_forever, args=(self,), ...)
    """
    loop = SnowLoop(bot, symbol=symbol)
    loop.run_forever()
