"""Snow action dispatcher — FLO-347 Phase 5b.

Consumes `snow.priority.FireEvent` objects produced by the Snow loop and
executes the corresponding MT5 action under `executor_lock`. Handles:
  * per-contingency guards (`only_if_tighter_sl`, `cooldown_seconds`,
    `max_adjustments_total`)
  * 3x retry with 2s -> 4s backoff (RFC §7.4)
  * MAX_TRIGGER_WINDOW_SECONDS=30 circuit breaker against broker hangs
  * atomic plan-state transition + audit row via
    `snow.db.record_trigger_and_transition`

Boundary (expanded relative to Phase 4):
  allowed: snow.db, snow.schema, snow.priority, executor (executor +
           executor_lock + OrderResult), logger (project log), config,
           tz_utils
  forbidden: agent_tools, ai_agent, rex_validator, rex_monitor, monitor,
             mt5_safe (direct), mt5 (direct)

DRY-RUN layer 2 defense: every `execute_action` entry point checks
`config.SNOW_DRY_RUN` and returns `dry_run_skipped` before touching
the executor, even if the loop forgot to filter. This is defense in
depth — the loop already skips actions dispatch in DRY RUN.

Out of scope for 5b:
  * cancel_plan action (validator rejects; Floki tool in Phase 6 calls
    snow.db.update_plan_status directly)
  * outcome_pips / outcome_usd backfill (deferred; snow_plans.outcome_*
    left None on close; follow-up commit adds the backfill pass that
    reads deal history)
"""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass
from typing import Any, Optional

import config
from logger import log
from tz_utils import utc_iso

from snow import db as snow_db
from snow import execution_quality as _eq
from snow.priority import FireEvent
from snow.schema import (
    ActionAdjustSL,
    ActionAdjustTP,
    ActionAlertFloki,
    ActionClosePartial,
    ActionCloseFull,
    ActionEscalateToFloki,
    ActionExecuteMarket,
    ActionMoveSLToBreakeven,
    ActionMoveSLToPrice,
    ActionTrailSL,
    ContingencyGuards,
    Direction,
    PlanStatus,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_TRIGGER_WINDOW_SECONDS: float = 30.0
RETRY_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 4.0)  # between attempts 1-2 and 2-3

# Execution-status strings written to snow_triggers.execution_status.
STATUS_SUCCESS:          str = "success"
STATUS_DRY_RUN_SKIPPED:  str = "dry_run_skipped"
STATUS_SKIPPED_GUARD:    str = "skipped_guard"
STATUS_RETRY_EXHAUSTED:  str = "retry_exhausted"
STATUS_TIMEOUT:          str = "timeout"
STATUS_UNSUPPORTED:      str = "unsupported_action"
STATUS_NO_POSITION:      str = "no_position"
STATUS_ERROR:            str = "error"


# ---------------------------------------------------------------------------
# Payload dataclass (opaque to priority.py)
# ---------------------------------------------------------------------------

@dataclass
class FirePayload:
    """Carried inside `FireEvent.payload`. The Snow loop builds one per
    evaluated-true contingency (or entry block); actions.py consumes it.

    Fields:
      action        — Pydantic Action model (e.g. ActionCloseFull)
      kind          — 'entry', 'management', or 'exit'
      plan_direction — Direction of the plan's entry (BUY/SELL). Needed
                       for tighter-SL comparisons and BE offset sign.
      ticket        — broker ticket; None for entry fires (no position yet)
      guards        — Contingency.guards or None (entries have no guards)
      entry_price   — plan entry price (best-known at eval time); None
                      for plans not yet filled. Used for BE offset.
    """
    action:          Any
    kind:            str
    plan_direction:  Direction
    ticket:          Optional[int] = None
    guards:          Optional[ContingencyGuards] = None
    entry_price:     Optional[float] = None


# ---------------------------------------------------------------------------
# ActionResult
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    """Return value from `SnowActions.execute_action`. Not a ContractError —
    the loop only needs to know success/skip for logging; detailed audit
    lives in snow_triggers."""
    status:    str
    plan_id:   str
    action_type: str
    reason:    Optional[str] = None
    ticket:    Optional[int] = None
    attempts:  int = 0
    elapsed_ms: int = 0


# ---------------------------------------------------------------------------
# SnowActions
# ---------------------------------------------------------------------------

class SnowActions:
    """Action dispatcher. Executor reference is injected for testability.

    Usage from snow_loop:
        actions = SnowActions()
        for fire in ordered_fires:
            actions.execute_action(fire)

    Usage from tests:
        actions = SnowActions(executor_impl=FakeExecutor())
    """

    def __init__(self, executor_impl: Any = None, executor_lock_impl: Any = None):
        # Lazy-import the real executor so tests that inject a fake never
        # trigger the executor module's MT5 connect path.
        if executor_impl is None or executor_lock_impl is None:
            from executor import executor as _exe, executor_lock as _lock
            self._executor = executor_impl if executor_impl is not None else _exe
            self._lock = executor_lock_impl if executor_lock_impl is not None else _lock
        else:
            self._executor = executor_impl
            self._lock = executor_lock_impl

    # ----- Public entry point ---------------------------------------------
    def execute_action(self, fire: FireEvent) -> ActionResult:
        """Dispatch `fire` to the right executor call, under executor_lock,
        with guards, retry, and atomic state update.

        Defence-in-depth against DRY-RUN leakage: if config.SNOW_DRY_RUN is
        True at entry, returns immediately without touching the executor.
        The Snow loop already filters DRY-RUN before calling; this is the
        second layer.
        """
        if bool(getattr(config, "SNOW_DRY_RUN", True)):
            log.warning(
                f"snow.actions.dry_run_leak plan={fire.plan_id} "
                f"action={fire.action_type} — loop should have filtered"
            )
            return ActionResult(
                status=STATUS_DRY_RUN_SKIPPED,
                plan_id=fire.plan_id,
                action_type=fire.action_type,
                reason="SNOW_DRY_RUN=True",
            )

        payload = fire.payload
        if not isinstance(payload, FirePayload):
            return self._record_and_return(
                fire, STATUS_ERROR,
                reason=f"fire.payload not FirePayload (got {type(payload).__name__})",
            )

        action = payload.action

        # Dispatch table — keeps the per-action handler small
        if isinstance(action, ActionExecuteMarket):
            return self._dispatch_execute_market(fire, payload)
        if isinstance(action, (ActionAdjustSL, ActionAdjustTP,
                               ActionMoveSLToPrice, ActionTrailSL)):
            return self._dispatch_modify_direct(fire, payload)
        if isinstance(action, ActionMoveSLToBreakeven):
            return self._dispatch_move_to_breakeven(fire, payload)
        if isinstance(action, ActionCloseFull):
            return self._dispatch_close_full(fire, payload)
        if isinstance(action, ActionClosePartial):
            return self._dispatch_close_partial(fire, payload)
        if isinstance(action, (ActionAlertFloki, ActionEscalateToFloki)):
            return self._dispatch_alert(fire, payload)

        # cancel_plan: validator rejects it in management/exit. If we got
        # here, something bypassed validation.
        return self._record_and_return(
            fire, STATUS_UNSUPPORTED,
            reason=f"action type {type(action).__name__} not supported in Phase 5b",
        )

    # ----- Entry dispatch --------------------------------------------------
    def _dispatch_execute_market(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """PENDING -> TRIGGERED -> ACTIVE on success, TRIGGERED -> FAILED on
        retry exhaust. TRIGGERED is visible in DB only for the executor-call
        + retry window, bounded by MAX_TRIGGER_WINDOW_SECONDS."""
        plan = snow_db.get_plan_as_model(fire.plan_id)
        if plan is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="plan hydrate failed",
            )

        entry = plan.entry
        direction = entry.direction.value  # "BUY" or "SELL"
        comment = f"snow:{fire.plan_id}"

        # FLO-418: opposing-positions soft-decision flow (replaces
        # FLO-417 hard block). When Snow detects an opposing live
        # position, the plan does NOT enter and does NOT fail —
        # instead it stays PENDING with an `awaiting_decision` flag
        # in state_cache_json. agent_data_builder reads the flag and
        # injects a <snow_pending_decisions> block into Floki's next
        # cycle, listing 3 options:
        #   (a) cancel_plan(plan_id, reason)
        #   (b) close_trade(opposing_ticket) — Snow auto-fires next tick
        #   (c) override_opposing_block(plan_id, reason) — bypass gate
        # Empirical (2026-05-01): the FLO-417 hard block on PLAN-010
        # cost ~$2.68 of opportunity. The Floki-decision flow lets the
        # operator choose per-instance.
        # First check: if an active override stamp exists, bypass the
        # opposing detection entirely (option c was already chosen).
        override = snow_db.get_override_opposing(fire.plan_id)
        if override:
            log.info(
                f"snow.actions.opposing_override_active plan_id={fire.plan_id} "
                f"attempted_direction={direction} stamped_at={override.get('stamped_at')} "
                f"expires_at={override.get('expires_at')} — bypassing opposing-positions gate"
            )
            # Fall through to the broker call with no awaiting check.
        else:
            try:
                existing = self._executor.get_open_positions()
            except Exception as e:
                log.warning(
                    f"snow.actions.opposing_positions_check_failed plan_id={fire.plan_id} "
                    f"err={type(e).__name__}: {e} — proceeding with entry "
                    f"(fail-open: better to risk a duplicate-side entry than "
                    f"deadlock the entry path on a transient MT5 hiccup)"
                )
                existing = []

            opposing = [
                p for p in existing
                if str(getattr(p, "direction", "")).upper() not in ("", direction)
            ]
            if opposing:
                opp_tickets = [
                    int(getattr(p, "ticket", 0) or 0) for p in opposing
                ]
                opp_summary = ", ".join(
                    f"#{getattr(p, 'ticket', '?')}({getattr(p, 'direction', '?')})"
                    for p in opposing
                )
                fresh_write = snow_db.set_awaiting_decision(
                    plan_id=fire.plan_id,
                    opposing_tickets=opp_tickets,
                    attempted_direction=direction,
                    reason=(
                        f"FLO-85 opposing: {fire.plan_id} {direction} "
                        f"conditions all-true while {opp_summary} live"
                    ),
                )
                if fresh_write:
                    # First detection: log once + record audit row.
                    log.info(
                        f"snow.actions.opposing_positions_awaiting plan_id={fire.plan_id} "
                        f"attempted_direction={direction} opposing={opp_summary} — "
                        f"FLO-418: plan held PENDING, awaiting Floki decision "
                        f"(cancel / close opposing / override). Plan stays alive; "
                        f"Snow will not retry the entry until Floki resolves."
                    )
                    snow_db.record_trigger(
                        plan_id=fire.plan_id,
                        contingency_name="_entry",
                        contingency_kind="entry",
                        action_type="execute_market",
                        execution_status=STATUS_SKIPPED_GUARD,
                        action_params={
                            "direction": direction,
                            "volume": entry.volume,
                            "sl": entry.initial_sl,
                            "tp": entry.initial_tp,
                            "awaiting_decision": True,
                            "opposing_tickets": opp_tickets,
                        },
                        execution_result={
                            "success": False,
                            "error_code": "awaiting_floki_decision",
                            "error_message": (
                                f"opposing positions live: {opp_summary}; "
                                f"plan held PENDING for Floki decision"
                            ),
                        },
                    )
                # Subsequent ticks: silent skip — same opposing set,
                # plan already awaiting. Floki will see notification
                # in next prompt cycle. No ActionResult logging row.
                return ActionResult(
                    status=STATUS_SKIPPED_GUARD,
                    plan_id=fire.plan_id,
                    action_type="execute_market",
                    reason=f"awaiting Floki decision (opposing: {opp_summary})",
                    ticket=None,
                )

        # Transition PENDING -> TRIGGERED BEFORE broker call (makes the
        # transient visible; loop will skip further evals while TRIGGERED).
        snow_db.update_plan_status(fire.plan_id, PlanStatus.TRIGGERED.value)

        # FLO-365: tick snapshot taken once, immediately before broker call.
        # Reference price = ask for BUY / bid for SELL — what the fill is
        # measured against to compute slippage.
        tick = _eq.capture_tick(getattr(config, "SYMBOL", "XAUUSDm"))
        ref_price = _eq.entry_reference_price(direction, tick)

        t0 = time.monotonic()
        result, attempts = self._call_with_retry(
            lambda: self._executor.execute_trade(
                direction=direction,
                lot_size=entry.volume,
                stop_loss=entry.initial_sl,
                take_profit=entry.initial_tp,
                comment=comment,
            )
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        executed_at = utc_iso()

        if result is None or not result.success:
            trigger_id = snow_db.record_trigger_and_transition(
                fire.plan_id,
                contingency_name="_entry",
                contingency_kind="entry",
                action_type="execute_market",
                execution_status=STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT,
                new_plan_status=PlanStatus.FAILED.value,
                action_params={"direction": direction, "volume": entry.volume,
                               "sl": entry.initial_sl, "tp": entry.initial_tp},
                execution_result=_order_result_to_dict(result),
                cycle_duration_ms=elapsed_ms,
            )
            self._record_execution_quality(
                trigger_id=trigger_id, fire=fire, executed_at=executed_at,
                action_type="execute_market", direction=direction,
                plan_volume=float(entry.volume), plan_price=ref_price,
                tick=tick, result=result,
                status=STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT,
                attempts=attempts,
            )
            return ActionResult(
                status=STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT,
                plan_id=fire.plan_id, action_type="execute_market",
                reason=(result.error_message if result else "trigger-window timeout"),
                attempts=attempts, elapsed_ms=elapsed_ms,
            )

        trigger_id = snow_db.record_trigger_and_transition(
            fire.plan_id,
            contingency_name="_entry",
            contingency_kind="entry",
            action_type="execute_market",
            execution_status=STATUS_SUCCESS,
            new_plan_status=PlanStatus.ACTIVE.value,
            action_params={"direction": direction, "volume": entry.volume,
                           "sl": entry.initial_sl, "tp": entry.initial_tp},
            execution_result=_order_result_to_dict(result),
            cycle_duration_ms=elapsed_ms,
            trade_ticket=result.ticket,
        )
        self._record_execution_quality(
            trigger_id=trigger_id, fire=fire, executed_at=executed_at,
            action_type="execute_market", direction=direction,
            plan_volume=float(entry.volume), plan_price=ref_price,
            tick=tick, result=result,
            status=STATUS_SUCCESS, attempts=attempts,
        )
        # Attach trade_ticket on the plan row (record_trigger_and_transition
        # may or may not — use explicit call to be safe).
        if result.ticket is not None:
            snow_db.update_plan_trade_ticket(fire.plan_id, int(result.ticket))
        # FLO-418: clear any awaiting / override flags on successful
        # entry — they were transient state for the gate decision and
        # have no meaning post-fire.
        try:
            snow_db.clear_awaiting_decision(fire.plan_id)
        except Exception:
            pass
        return ActionResult(
            status=STATUS_SUCCESS, plan_id=fire.plan_id,
            action_type="execute_market", ticket=result.ticket,
            attempts=attempts, elapsed_ms=elapsed_ms,
        )

    # ----- Modify dispatch -------------------------------------------------
    def _dispatch_modify_direct(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """adjust_sl / adjust_tp / move_sl_to_price / trail_sl.
        No plan-state transition on success (plan stays ACTIVE)."""
        action = payload.action
        ticket = payload.ticket
        if ticket is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="modify action with no ticket",
            )

        new_sl: Optional[float] = None
        new_tp: Optional[float] = None
        action_type: str = fire.action_type
        new_sl_target_for_guard: Optional[float] = None

        if isinstance(action, ActionAdjustSL):
            new_sl = float(action.price)
            new_sl_target_for_guard = new_sl
        elif isinstance(action, ActionAdjustTP):
            new_tp = float(action.price)
        elif isinstance(action, ActionMoveSLToPrice):
            new_sl = float(action.price)
            new_sl_target_for_guard = new_sl
        elif isinstance(action, ActionTrailSL):
            # Trail = offset current price by trail_pips in favourable direction.
            # "current price" is read from executor positions to keep the
            # lock discipline tidy (same lock as the modify call).
            new_sl = self._trail_target_sl(ticket, action.trail_pips, payload)
            if new_sl is None:
                return self._record_and_return(
                    fire, STATUS_ERROR, reason="trail_sl: position not found",
                )
            new_sl_target_for_guard = new_sl

        # Guards (run BEFORE any executor call that mutates state)
        skip, reason = self._run_guards(
            fire, payload, new_sl_target=new_sl_target_for_guard,
        )
        if skip:
            return self._record_and_return(
                fire, STATUS_SKIPPED_GUARD, reason=reason,
            )

        # FLO-365: tick snapshot for dashboard context (no slippage on modify).
        tick = _eq.capture_tick(getattr(config, "SYMBOL", "XAUUSDm"))

        t0 = time.monotonic()
        result, attempts = self._call_with_retry(
            lambda: self._executor.modify_position(ticket, new_sl=new_sl, new_tp=new_tp)
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        executed_at = utc_iso()

        status = (STATUS_SUCCESS if (result and result.success)
                  else (STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT))
        trigger_id = snow_db.record_trigger(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            contingency_kind=payload.kind,
            action_type=action_type,
            execution_status=status,
            action_params={"ticket": ticket, "new_sl": new_sl, "new_tp": new_tp},
            execution_result=_order_result_to_dict(result),
            cycle_duration_ms=elapsed_ms,
        )
        # plan_price = the SL/TP target the action carried; modify is not
        # a fill, so actual_price/slippage stay NULL via result=None.
        plan_price = new_sl if new_sl is not None else new_tp
        self._record_execution_quality(
            trigger_id=trigger_id, fire=fire, executed_at=executed_at,
            action_type=action_type,
            direction=payload.plan_direction.value,
            plan_volume=None, plan_price=plan_price,
            tick=tick, result=None,  # modify is not a fill
            status=status, attempts=attempts,
        )
        return ActionResult(
            status=status, plan_id=fire.plan_id, action_type=action_type,
            ticket=ticket, attempts=attempts, elapsed_ms=elapsed_ms,
            reason=(None if status == STATUS_SUCCESS else
                    (result.error_message if result else "trigger-window timeout")),
        )

    def _dispatch_move_to_breakeven(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """SL -> entry_price + offset_pips (direction-aware). Needs
        entry_price; falls back to the live position's open_price if
        payload didn't supply one."""
        action = payload.action
        ticket = payload.ticket
        if ticket is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="breakeven with no ticket",
            )

        entry_price = payload.entry_price
        if entry_price is None:
            # Fallback: read current position open_price from executor.
            pos = self._find_position(ticket)
            if pos is None:
                return self._record_and_return(
                    fire, STATUS_ERROR, reason="breakeven: position not found",
                )
            entry_price = float(pos.open_price)

        # PIP_SIZE from schema context (avoid cross-module coupling)
        pip = 0.1  # XAUUSD convention; matches snow.evaluators.context.PIP_SIZE
        offset = float(action.offset_pips) * pip
        if payload.plan_direction == Direction.BUY:
            new_sl = entry_price + offset
        else:  # SELL
            new_sl = entry_price - offset

        # Reuse the direct-modify path for guards + retry + bookkeeping.
        synth_payload = FirePayload(
            action=ActionAdjustSL(price=new_sl),  # re-wrap so guard logic sees an SL target
            kind=payload.kind,
            plan_direction=payload.plan_direction,
            ticket=ticket,
            guards=payload.guards,
            entry_price=entry_price,
        )
        # Re-frame the fire so the audit row records move_sl_to_breakeven,
        # not adjust_sl.
        return self._dispatch_modify_with_synth(
            fire, synth_payload, announced_action_type="move_sl_to_breakeven",
        )

    def _dispatch_modify_with_synth(
        self, fire: FireEvent, payload: FirePayload, *, announced_action_type: str,
    ) -> ActionResult:
        """Internal: shared tail for breakeven (which builds a synthetic
        adjust_sl) without rewriting the retry+guard machinery."""
        action = payload.action  # ActionAdjustSL synth
        ticket = payload.ticket
        new_sl = float(action.price)  # type: ignore[attr-defined]
        new_tp = None

        skip, reason = self._run_guards(fire, payload, new_sl_target=new_sl)
        if skip:
            return self._record_and_return(
                fire, STATUS_SKIPPED_GUARD, reason=reason,
                announced_action_type=announced_action_type,
            )

        tick = _eq.capture_tick(getattr(config, "SYMBOL", "XAUUSDm"))

        t0 = time.monotonic()
        result, attempts = self._call_with_retry(
            lambda: self._executor.modify_position(ticket, new_sl=new_sl, new_tp=new_tp)
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        executed_at = utc_iso()
        status = (STATUS_SUCCESS if (result and result.success)
                  else (STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT))
        trigger_id = snow_db.record_trigger(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            contingency_kind=payload.kind,
            action_type=announced_action_type,
            execution_status=status,
            action_params={"ticket": ticket, "new_sl": new_sl},
            execution_result=_order_result_to_dict(result),
            cycle_duration_ms=elapsed_ms,
        )
        self._record_execution_quality(
            trigger_id=trigger_id, fire=fire, executed_at=executed_at,
            action_type=announced_action_type,
            direction=payload.plan_direction.value,
            plan_volume=None, plan_price=new_sl,
            tick=tick, result=None,
            status=status, attempts=attempts,
        )
        return ActionResult(
            status=status, plan_id=fire.plan_id,
            action_type=announced_action_type, ticket=ticket,
            attempts=attempts, elapsed_ms=elapsed_ms,
            reason=(None if status == STATUS_SUCCESS else
                    (result.error_message if result else "trigger-window timeout")),
        )

    # ----- Close dispatch --------------------------------------------------
    def _dispatch_close_full(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """ACTIVE -> CLOSING -> CLOSED on success, CLOSING -> FAILED on
        retry exhaust. Outcome_pips/usd left None for Phase 5b; backfilled
        by a follow-up pass that reads deal history."""
        ticket = payload.ticket
        if ticket is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="close_full with no ticket",
            )

        snow_db.update_plan_status(fire.plan_id, PlanStatus.CLOSING.value)

        tick = _eq.capture_tick(getattr(config, "SYMBOL", "XAUUSDm"))

        t0 = time.monotonic()
        result, attempts = self._call_with_retry(
            lambda: self._executor.close_position(ticket)
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        executed_at = utc_iso()

        if result and result.success:
            # FLO-353 — backfill outcome columns inline. update_plan_outcome
            # transitions CLOSING → CLOSED with NULL outcome columns first,
            # then backfill_outcome populates them from MT5 deal history.
            # Best-effort: never raises; on failure leaves outcome_* NULL
            # with an audit row so the close itself isn't blocked.
            snow_db.update_plan_outcome(
                fire.plan_id, outcome_pips=None, outcome_usd=None,
                new_status=PlanStatus.CLOSED.value,
            )
            from snow.outcome import backfill_outcome
            backfill_outcome(fire.plan_id, ticket)
            status = STATUS_SUCCESS
        elif _looks_like_position_gone(result):
            # External close (TP/SL already hit). Per RFC §7.5, treat as
            # success. Same backfill — deal history captures the external
            # close as cleanly as a Snow-initiated one.
            snow_db.update_plan_outcome(
                fire.plan_id, outcome_pips=None, outcome_usd=None,
                new_status=PlanStatus.CLOSED.value,
            )
            from snow.outcome import backfill_outcome
            backfill_outcome(fire.plan_id, ticket)
            status = STATUS_NO_POSITION
        else:
            # FLO-374: terminal transition stamps closed_at.
            snow_db.mark_plan_terminal(fire.plan_id, PlanStatus.FAILED.value)
            status = STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT

        trigger_id = snow_db.record_trigger(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            contingency_kind=payload.kind,
            action_type="close_full",
            execution_status=status,
            action_params={"ticket": ticket},
            execution_result=_order_result_to_dict(result),
            cycle_duration_ms=elapsed_ms,
        )
        # FLO-365: close has no plan-side target price; record fill price
        # via result. Slippage stays NULL (close has no expected price).
        # Pass result through on failure too so error_message survives.
        self._record_execution_quality(
            trigger_id=trigger_id, fire=fire, executed_at=executed_at,
            action_type="close_full",
            direction=payload.plan_direction.value,
            plan_volume=None, plan_price=None,
            tick=tick, result=result,
            status=status, attempts=attempts,
        )
        return ActionResult(
            status=status, plan_id=fire.plan_id, action_type="close_full",
            ticket=ticket, attempts=attempts, elapsed_ms=elapsed_ms,
            reason=(None if status in (STATUS_SUCCESS, STATUS_NO_POSITION)
                    else (result.error_message if result else "trigger-window timeout")),
        )

    def _dispatch_close_partial(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """Partial close. No plan-state transition (plan stays ACTIVE).

        Outcome backfill: NOT called here. Backfill runs on the
        CLOSED transition, which `close_partial` does not perform.
        Two coverage paths for a fully-closed-via-partials position:

          1. The remaining volume is closed via `close_full` later —
             the close_full success branch backfills as normal.
          2. The plan is left in ACTIVE with no MT5 position until the
             next bot restart, at which point `snow.recovery`'s
             ACTIVE → CLOSED bucket transitions the plan AND calls
             `backfill_outcome` itself (FLO-354 + FLO-353 wiring).

        A chain of close_partial calls that drives volume to zero
        without a final close_full will therefore leave outcome
        columns NULL until the next restart, when recovery catches
        it. Acceptable: this is observability, not correctness, and
        recovery's ACTIVE → CLOSED detection is the safety net.
        """
        ticket = payload.ticket
        action = payload.action
        if ticket is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="close_partial with no ticket",
            )
        pos = self._find_position(ticket)
        if pos is None:
            return self._record_and_return(
                fire, STATUS_ERROR, reason="close_partial: position not found",
            )
        volume_to_close = round(pos.volume * (float(action.percent) / 100.0), 2)
        if volume_to_close <= 0:
            return self._record_and_return(
                fire, STATUS_ERROR,
                reason=f"close_partial: computed volume {volume_to_close} <= 0",
            )

        tick = _eq.capture_tick(getattr(config, "SYMBOL", "XAUUSDm"))

        t0 = time.monotonic()
        result, attempts = self._call_with_retry(
            lambda: self._executor.close_position(ticket, volume=volume_to_close)
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        executed_at = utc_iso()
        status = (STATUS_SUCCESS if (result and result.success)
                  else (STATUS_RETRY_EXHAUSTED if result else STATUS_TIMEOUT))
        trigger_id = snow_db.record_trigger(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            contingency_kind=payload.kind,
            action_type="close_partial",
            execution_status=status,
            action_params={"ticket": ticket, "volume": volume_to_close,
                           "percent": action.percent},
            execution_result=_order_result_to_dict(result),
            cycle_duration_ms=elapsed_ms,
        )
        self._record_execution_quality(
            trigger_id=trigger_id, fire=fire, executed_at=executed_at,
            action_type="close_partial",
            direction=payload.plan_direction.value,
            plan_volume=volume_to_close, plan_price=None,
            tick=tick, result=result,
            status=status, attempts=attempts,
        )
        return ActionResult(
            status=status, plan_id=fire.plan_id, action_type="close_partial",
            ticket=ticket, attempts=attempts, elapsed_ms=elapsed_ms,
            reason=(None if status == STATUS_SUCCESS else
                    (result.error_message if result else "trigger-window timeout")),
        )

    # ----- Alert dispatch --------------------------------------------------
    def _dispatch_alert(
        self, fire: FireEvent, payload: FirePayload
    ) -> ActionResult:
        """alert_floki / escalate_to_floki: write a snow_evaluations row
        with event='alert' or 'alert_urgent'. Floki pulls in Phase 6.
        No executor call; no lock needed."""
        action = payload.action
        urgent = isinstance(action, ActionEscalateToFloki)
        event = "alert_urgent" if urgent else "alert"
        message = getattr(action, "message", "")
        snow_db.record_evaluation(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            event=event,
            conditions_snapshot={
                "message": str(message)[:500],
                "kind": payload.kind,
                "urgent": urgent,
                "timestamp": utc_iso(),
            },
        )
        snow_db.record_trigger(
            plan_id=fire.plan_id,
            contingency_name=fire.contingency_name,
            contingency_kind=payload.kind,
            action_type=fire.action_type,
            execution_status=STATUS_SUCCESS,
            action_params={"message": str(message)[:500], "urgent": urgent},
        )
        return ActionResult(
            status=STATUS_SUCCESS, plan_id=fire.plan_id,
            action_type=fire.action_type, attempts=1, elapsed_ms=0,
        )

    # ----- Guards ----------------------------------------------------------
    def _run_guards(
        self, fire: FireEvent, payload: FirePayload,
        *, new_sl_target: Optional[float] = None,
    ) -> tuple[bool, Optional[str]]:
        """Return (skip, reason). skip=True means don't execute.

        Guards evaluated (RFC §7.3):
          * only_if_tighter_sl  — reads live position SL
          * cooldown_seconds    — queries snow_triggers for recent fires
          * max_adjustments_total — queries snow_triggers count for this
                                    (plan_id, contingency_name)
        """
        guards = payload.guards
        if guards is None:
            return False, None

        # 1. cooldown
        cooldown = getattr(guards, "cooldown_seconds", None)
        if cooldown is not None and cooldown > 0:
            if self._within_cooldown(fire, cooldown):
                return True, f"cooldown {cooldown}s not elapsed"

        # 2. max_adjustments_total
        max_adj = getattr(guards, "max_adjustments_total", None)
        if max_adj is not None and max_adj > 0:
            fired = self._count_successful_fires(fire)
            if fired >= max_adj:
                return True, f"max_adjustments_total {max_adj} reached ({fired} fires)"

        # 3. only_if_tighter_sl
        if new_sl_target is not None and getattr(guards, "only_if_tighter_sl", False):
            if not self._new_sl_is_tighter(
                payload.ticket, payload.plan_direction, new_sl_target
            ):
                return True, "only_if_tighter_sl: new SL not tighter than current"

        return False, None

    def _within_cooldown(self, fire: FireEvent, cooldown_seconds: int) -> bool:
        """Check most recent successful trigger for this (plan, contingency).

        list_triggers only filters by plan_id; name filter done in Python.
        """
        triggers = snow_db.list_triggers(plan_id=fire.plan_id, limit=100)
        for row in triggers:  # newest first
            if row.get("contingency_name") != fire.contingency_name:
                continue
            if row.get("execution_status") != STATUS_SUCCESS:
                continue
            ts = row.get("fired_at")
            if not ts:
                return False
            try:
                import datetime as _dt
                parsed = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
                now = _dt.datetime.now(_dt.timezone.utc)
                elapsed = (now - parsed).total_seconds()
                return elapsed < cooldown_seconds
            except Exception:
                log.warning(f"snow.actions.cooldown_parse_failed ts={ts!r}")
                return False
        return False

    def _count_successful_fires(self, fire: FireEvent) -> int:
        triggers = snow_db.list_triggers(plan_id=fire.plan_id, limit=1000)
        return sum(
            1 for t in triggers
            if t.get("contingency_name") == fire.contingency_name
            and t.get("execution_status") == STATUS_SUCCESS
        )

    def _new_sl_is_tighter(
        self, ticket: Optional[int], direction: Direction, new_sl: float,
    ) -> bool:
        """Direction-aware tighter check.
          BUY: SL below entry; tighter SL = LARGER number (moves up toward price)
          SELL: SL above entry; tighter SL = SMALLER number (moves down toward price)
        """
        if ticket is None:
            return True  # no position to compare; defer to broker
        pos = self._find_position(ticket)
        if pos is None:
            return True  # position gone; let executor handle
        old_sl = float(pos.sl or 0.0)
        if old_sl == 0.0:
            return True  # no previous SL → any new SL is "tighter"
        if direction == Direction.BUY:
            return new_sl > old_sl
        return new_sl < old_sl

    # ----- Helpers ---------------------------------------------------------
    def _find_position(self, ticket: int):
        try:
            for pos in self._executor.get_open_positions():
                if int(pos.ticket) == int(ticket):
                    return pos
        except Exception as e:
            log.warning(f"snow.actions.get_positions_failed: {e}")
        return None

    def _trail_target_sl(
        self, ticket: int, trail_pips: float, payload: FirePayload,
    ) -> Optional[float]:
        pos = self._find_position(ticket)
        if pos is None:
            return None
        pip = 0.1  # XAUUSD
        price = float(pos.current_price)
        if payload.plan_direction == Direction.BUY:
            return price - float(trail_pips) * pip
        return price + float(trail_pips) * pip

    def _call_with_retry(
        self, fn,
    ) -> tuple[Any, int]:
        """3x retry with 2s->4s backoff + MAX_TRIGGER_WINDOW_SECONDS
        circuit breaker. All executor calls happen inside the lock so
        a single RLock acquisition covers the full retry sequence.

        Returns (result, attempts). `result=None` means the circuit
        breaker tripped before we could even attempt.
        """
        deadline = time.monotonic() + MAX_TRIGGER_WINDOW_SECONDS
        last_result = None
        attempts = 0
        with self._lock:
            for attempt in (1, 2, 3):
                if time.monotonic() > deadline:
                    log.error(
                        f"snow.actions.trigger_window_timeout attempt={attempt}"
                    )
                    return None, attempts
                attempts = attempt
                try:
                    last_result = fn()
                except Exception as e:
                    log.error(f"snow.actions.executor_call_raised attempt={attempt}: {e}")
                    log.error(traceback.format_exc())
                    last_result = None
                if last_result is not None and getattr(last_result, "success", False):
                    return last_result, attempts
                if _looks_like_position_gone(last_result):
                    return last_result, attempts
                # Backoff (only if another attempt is possible AND still in window)
                if attempt < 3:
                    wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(wait, remaining))
        return last_result, attempts

    # ----- Execution-quality recording (FLO-365) --------------------------
    def _record_execution_quality(
        self,
        *,
        trigger_id: int,
        fire: FireEvent,
        executed_at: str,
        action_type: str,
        direction: str,
        plan_volume: Optional[float],
        plan_price: Optional[float],
        tick: "_eq.TickSnapshot",
        result: Any,
        status: str,
        attempts: int,
    ) -> None:
        """Persist one snow_execution_quality row. Best-effort — any
        failure is swallowed inside snow_db.insert_execution_quality.

        For non-fill paths (modify/adjust), pass result=None and the
        actual_price / slippage_pips columns will land as NULL.
        """
        actual_price: Optional[float] = None
        actual_volume: Optional[float] = None
        error_message: Optional[str] = None
        ticket: Optional[int] = None
        if result is not None:
            actual_price = getattr(result, "price", None)
            actual_volume = getattr(result, "volume", None)
            error_message = getattr(result, "error_message", None)
            ticket = getattr(result, "ticket", None)

        slippage = _eq.compute_slippage_pips(direction, plan_price, actual_price)
        snow_db.insert_execution_quality(
            trigger_id=trigger_id,
            plan_id=fire.plan_id,
            action_type=action_type,
            fired_at=getattr(fire, "fired_at", None),
            executed_at=executed_at,
            latency_ms=_eq.latency_ms(getattr(fire, "fired_at", None), executed_at),
            plan_volume=plan_volume,
            plan_price=plan_price,
            actual_volume=(float(actual_volume) if actual_volume is not None else None),
            actual_price=(float(actual_price) if actual_price is not None else None),
            slippage_pips=slippage,
            bid_at_fire=tick.bid,
            ask_at_fire=tick.ask,
            mid_at_fire=tick.mid,
            status=status,
            ticket=(int(ticket) if ticket is not None else None),
            attempts=attempts,
            error_message=error_message,
        )

    def _record_and_return(
        self, fire: FireEvent, status: str,
        *, reason: Optional[str] = None,
        announced_action_type: Optional[str] = None,
    ) -> ActionResult:
        action_type = announced_action_type or fire.action_type
        payload = fire.payload if isinstance(fire.payload, FirePayload) else None
        kind = payload.kind if payload else "unknown"
        ticket = payload.ticket if payload else None
        try:
            snow_db.record_trigger(
                plan_id=fire.plan_id,
                contingency_name=fire.contingency_name,
                contingency_kind=kind,
                action_type=action_type,
                execution_status=status,
                action_params={"reason": reason} if reason else None,
            )
        except Exception as e:
            log.error(f"snow.actions.record_trigger_failed: {e}")
        return ActionResult(
            status=status, plan_id=fire.plan_id, action_type=action_type,
            reason=reason, ticket=ticket,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _order_result_to_dict(result: Any) -> Optional[dict[str, Any]]:
    if result is None:
        return None
    try:
        return {
            "success": bool(getattr(result, "success", False)),
            "ticket": getattr(result, "ticket", None),
            "error_code": getattr(result, "error_code", None),
            "error_message": getattr(result, "error_message", None),
            "price": getattr(result, "price", None),
            "volume": getattr(result, "volume", None),
        }
    except Exception:
        return {"serialize_error": True}


def _looks_like_position_gone(result: Any) -> bool:
    """Heuristic: treat 'no position found' error responses as externally
    closed (TP/SL already hit). RFC §7.5 contract.

    MT5 codes vary; we match on message substring to be robust across
    broker messages. The caller records STATUS_NO_POSITION separately.
    """
    if result is None:
        return False
    if getattr(result, "success", False):
        return False
    msg = (getattr(result, "error_message", "") or "").lower()
    return (
        "no position" in msg
        or "position not found" in msg
        or "position gone" in msg
    )


__all__ = [
    "FirePayload",
    "ActionResult",
    "SnowActions",
    "MAX_TRIGGER_WINDOW_SECONDS",
    "RETRY_BACKOFF_SECONDS",
    "STATUS_SUCCESS", "STATUS_DRY_RUN_SKIPPED", "STATUS_SKIPPED_GUARD",
    "STATUS_RETRY_EXHAUSTED", "STATUS_TIMEOUT", "STATUS_UNSUPPORTED",
    "STATUS_NO_POSITION", "STATUS_ERROR",
]
