"""Snow startup reconciliation — FLO-354.

Run on bot startup BEFORE the SnowLoop thread spawns. Reconciles
`snow_plans` state against MT5 reality + the wall clock so that a
crash, a manual MT5 intervention, or an SL/TP hit during downtime
doesn't leave Snow with inconsistent state.

Five buckets, processed in order
--------------------------------

1. **PENDING + expires_at past → EXPIRED.**  (Visible-bug fix from
   the CEO ask; not in the original FLO-354 ticket text but adopted
   here as the cheapest expression of "auto-expiry stale pending
   plans".)
2. **TRIGGERED.**  Check MT5: position exists → ACTIVE + tracker
   seed; no position → FAILED with reason `crash_during_trigger`.
3. **CLOSING.**  Check MT5: position exists → back to ACTIVE (the
   normal close action will retry next tick); no position → CLOSED.
4. **ACTIVE without matching MT5 position.**  Query deal history
   with retry+backoff. Non-empty deal list → CLOSED (closed
   externally / SL or TP hit during downtime). Definitive empty
   list → FAILED with reason `position_vanished`. Transient
   MT5 error (None responses for all retries) → leave ACTIVE,
   re-check next startup.
5. **ACTIVE remaining (position confirmed).**  Reseed
   PerPlanTracker with `position.price_open` (broker-authoritative — MT5
   TradePosition uses `price_open`, NOT `open_price`; the latter is the
   monitor.py wrapper's field. FLO-455 follow-up fix), not from a
   snow_plans column — there is no `entry_price` column
   yet (FLO-353 doesn't add one either; that ticket scopes only
   outcome_pips/outcome_usd).

Outcome backfill (snow_plans.outcome_pips/outcome_usd) is intentionally
NOT touched here — that work lives in FLO-353 (`snow/outcome.py`).
Both this module's CLOSED transitions and `snow/actions.py`'s close
path will eventually call `outcome.backfill_outcome(plan_id, ticket)`
once that ticket lands. Until then, recovery's CLOSED transitions
leave `outcome_pips`/`outcome_usd` NULL with an audit row noting the
deferral.

Fail-loud
---------

- Any unhandled exception during reconciliation (DB read failure,
  MT5 API exception, etc.) propagates as `RecoveryAborted` so
  `main.py` refuses to spawn the loop. Operator handles it; a fresh
  restart with MT5 connected reconciles correctly.
- Specifically, `mt5.positions_get(symbol=...)` returning `None`
  is treated as MT5 disconnect → abort. Misreading "MT5 down" as
  "all positions vanished" would mass-FAIL plans, which is
  unrecoverable.

Boundary
--------

This module imports `snow.db`, `mt5_safe.mt5`, and
`snow.evaluators.tracker.PerPlanTracker`. It does NOT import
`executor.py` or any Floki-side surface. Tests inject fakes for the
MT5 proxy and tracker via keyword arguments.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import config
from logger import log
from mt5_safe import mt5 as _default_mt5
from tz_utils import utc_iso

from snow import db as snow_db
from snow.schema import PlanStatus


# Plan statuses considered "live" for reconciliation. Order matters:
# PENDING is processed first (cheap clock check) so we don't waste an
# MT5 call on a plan that should be EXPIRED anyway.
_LIVE_STATUSES: tuple[str, ...] = (
    PlanStatus.PENDING.value,
    PlanStatus.TRIGGERED.value,
    PlanStatus.CLOSING.value,
    PlanStatus.ACTIVE.value,
)

# Deal-history retry policy. Distinguishes transient (None response)
# from definitive (empty list) — the former retries, the latter does
# not. Reused by FLO-353's outcome backfill via `fetch_deal_history`.
_DEAL_HISTORY_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0)
_DEAL_HISTORY_LOOKBACK_DAYS: int = 14


# =============================================================================
# Public API
# =============================================================================

class RecoveryAborted(RuntimeError):
    """Reconciliation cannot proceed safely. main.py MUST refuse to
    spawn SnowLoop on this exception. Examples: MT5 disconnect during
    the batch positions query; a DB read that raises."""


@dataclass
class ReconcileSummary:
    pending_expired: int = 0
    triggered_to_active: int = 0
    triggered_failed: int = 0
    closing_to_active: int = 0
    closing_to_closed: int = 0
    active_to_closed: int = 0
    active_failed: int = 0
    active_left_for_retry: int = 0
    tracker_reseeds: int = 0

    def total_transitions(self) -> int:
        return (
            self.pending_expired
            + self.triggered_to_active
            + self.triggered_failed
            + self.closing_to_active
            + self.closing_to_closed
            + self.active_to_closed
            + self.active_failed
        )

    def as_log_kvs(self) -> str:
        return (
            f"pending_expired={self.pending_expired} "
            f"triggered_active={self.triggered_to_active} "
            f"triggered_failed={self.triggered_failed} "
            f"closing_active={self.closing_to_active} "
            f"closing_closed={self.closing_to_closed} "
            f"active_closed={self.active_to_closed} "
            f"active_failed={self.active_failed} "
            f"active_left_for_retry={self.active_left_for_retry} "
            f"tracker_reseeds={self.tracker_reseeds}"
        )


def reconcile_on_startup(
    *,
    tracker=None,
    mt5_proxy=None,
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
    now: Optional[_dt.datetime] = None,
) -> ReconcileSummary:
    """Reconcile every live plan. Single batched MT5 query for
    positions; per-plan deal-history queries only when needed.

    Args:
      tracker: PerPlanTracker singleton or test fake. None disables
        tracker reseed (useful for unit tests that don't care).
      mt5_proxy: Defaults to `mt5_safe.mt5`. Tests inject a fake.
      symbol: Defaults to `config.SYMBOL`.
      magic: Defaults to `config.MAGIC_NUMBER`. Used to filter the
        batched positions list to bot-owned positions.
      now: Wall clock for `expires_at` checks. Defaults to UTC now.

    Returns:
      ReconcileSummary with per-bucket counts.

    Raises:
      RecoveryAborted: MT5 disconnect on the batch positions query,
        or DB read failure. main.py treats this as fatal.
    """
    proxy = mt5_proxy if mt5_proxy is not None else _default_mt5
    sym = symbol or getattr(config, "SYMBOL", "XAUUSD")
    mg = magic if magic is not None else int(getattr(config, "MAGIC_NUMBER", 0))
    now = now or _dt.datetime.now(_dt.timezone.utc)

    # 1. Read live plans up-front; abort if DB read fails.
    try:
        live_plans = snow_db.list_plans_by_status(
            _LIVE_STATUSES, limit=10_000
        )
    except Exception as e:
        raise RecoveryAborted(
            f"snow.recovery: list_plans_by_status raised {type(e).__name__}: {e}"
        ) from e

    log.info(
        f"snow.recovery.start live_plans={len(live_plans)} "
        f"symbol={sym} magic={mg}"
    )
    if not live_plans:
        return ReconcileSummary()

    # 2. Batch MT5 positions query — single call for all plans. None
    # response → MT5 disconnect → abort.
    try:
        raw_positions = proxy.positions_get(symbol=sym)
    except Exception as e:
        raise RecoveryAborted(
            f"snow.recovery: mt5.positions_get raised {type(e).__name__}: {e}"
        ) from e
    if raw_positions is None:
        raise RecoveryAborted(
            "snow.recovery: mt5.positions_get returned None — "
            "MT5 disconnected. Refusing to reconcile (would misread "
            "as 'all positions vanished')."
        )
    by_ticket: dict[int, Any] = {
        p.ticket: p for p in raw_positions if p.magic == mg
    }
    log.info(
        f"snow.recovery.mt5_positions count={len(by_ticket)} "
        f"raw={len(raw_positions)} (filtered by magic={mg})"
    )

    # 3. Per-plan reconciliation. Per-plan errors are caught + logged
    # so one bad plan doesn't kill the batch; the surrounding fail-loud
    # contract is for infrastructure failures (DB / MT5), not for one
    # plan's broken state.
    summary = ReconcileSummary()
    for row in live_plans:
        plan_id = row.get("id") or "<unknown>"
        try:
            _reconcile_plan(
                row, by_ticket, summary,
                tracker=tracker, mt5_proxy=proxy, now=now,
            )
        except Exception as e:
            import traceback as _tb
            log.error(
                f"snow.recovery.plan_failed plan_id={plan_id} "
                f"{type(e).__name__}: {e}\n{_tb.format_exc()}"
            )

    log.info(f"snow.recovery.summary {summary.as_log_kvs()}")
    return summary


def fetch_deal_history(
    ticket: int,
    *,
    mt5_proxy=None,
    lookback_days: int = _DEAL_HISTORY_LOOKBACK_DAYS,
    max_attempts: Optional[int] = None,
) -> Optional[list]:
    """Query MT5 deal history for a position ticket with retry+backoff
    AND a defensive `position_id == ticket` re-filter.

    Returns:
      `list` (possibly empty): MT5 returned a definitive answer.
        Empty list = no deals exist FOR THIS TICKET (after filter).
        Non-empty = the ticket has trades in history.
      `None`: All retries returned None (transient MT5 error).
        Caller should NOT mark as FAILED; leave the plan for the
        next startup pass.

    Defensive position_id filter (FLO-373/observation):
      MT5's `position=ticket` parameter is documented as a hint, not
      a guarantee. In practice it can return deals where
      `deal.position_id != ticket` — the same workaround
      `executor._search_deal_by_position_param` already applies. This
      function therefore re-filters the returned list to only deals
      whose `position_id` matches `ticket`. A non-matching deal is
      dropped silently; if the entire list filters away to empty,
      that's the genuine "no deals for this ticket" answer.

    Reused by FLO-353's outcome backfill — same retry pattern, same
    filter.
    """
    proxy = mt5_proxy if mt5_proxy is not None else _default_mt5
    _date_from_utc = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=lookback_days)
    _date_to_utc = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1)
    # FLO-380: MT5 stores deal.time as broker-wall-clock-as-unix
    # (broker_local treated as UTC), and history_deals_get interprets
    # naive datetime args as box-local time. Tz-aware UTC datetimes
    # therefore mis-classify the query window — recent deals drop out
    # entirely. Canary PLAN-20260427-005 (entry/close 14:27/14:29 UTC)
    # returned 0 matches with tz-aware lookups, 2 matches once
    # converted to broker-naive. mfe_backfill._utc_to_broker_naive
    # and regime_detector._broker_offset_s established the canonical
    # pattern; recovery.fetch_deal_history was the outlier still
    # passing tz-aware datetimes.
    date_from = _utc_to_broker_naive(_date_from_utc, proxy)
    date_to = _utc_to_broker_naive(_date_to_utc, proxy)
    ticket_int = int(ticket)

    # FLO-379: optional retry budget cap. Runtime callers pass
    # `max_attempts=1` to avoid blocking the Snow tick for up to 3.5s
    # on a transient MT5 None response — at runtime the next
    # reconcile pass is 60s away, so retrying immediately buys
    # nothing. Startup recovery keeps the full 3-attempt policy
    # because boot is one-shot and tolerates the wait.
    backoffs = (
        _DEAL_HISTORY_RETRY_BACKOFFS[:max_attempts]
        if max_attempts is not None
        else _DEAL_HISTORY_RETRY_BACKOFFS
    )
    for attempt, backoff in enumerate(backoffs, start=1):
        try:
            deals = proxy.history_deals_get(
                date_from, date_to, position=ticket_int
            )
        except Exception as e:
            log.warning(
                f"snow.recovery.deal_history_error ticket={ticket_int} "
                f"attempt={attempt} error={type(e).__name__}: {e}"
            )
            deals = None
        if deals is None:
            if attempt < len(backoffs):
                _time.sleep(backoff)
                continue
            log.warning(
                f"snow.recovery.deal_history_exhausted ticket={ticket_int} "
                f"attempts={attempt}"
            )
            return None
        # Defensive position_id filter — drop any deal MT5 surfaced
        # whose position_id doesn't match the ticket we asked for.
        raw = list(deals)
        filtered = [
            d for d in raw
            if int(getattr(d, "position_id", -1)) == ticket_int
        ]
        if len(filtered) != len(raw):
            _filt_logfn = log.info if filtered else log.warning
            _filt_logfn(
                f"snow.recovery.deal_history_filtered "
                f"ticket={ticket_int} raw={len(raw)} kept={len(filtered)} "
                f"dropped={len(raw) - len(filtered)} "
                f"reason=position_id_mismatch"
            )
        # FLO-379 fallback retained as defense-in-depth. Originally
        # added on the wrong root cause — I assumed `position=ticket`
        # was the bug. FLO-380 revealed the real culprit was
        # tz-aware datetimes (now converted upstream via
        # _utc_to_broker_naive). With broker-naive datetimes
        # `position=ticket` works correctly: production verification
        # shows kept=2 dropped=191 on PLAN-005 (was kept=0 dropped=193
        # pre-FLO-380). The fallback is not dead code — dropped > 0
        # in production indicates MT5 position filter is still
        # imperfect (returns deals for unrelated tickets in the same
        # symbol), so re-querying without the hint is a useful
        # last-resort safety net.
        if not filtered and len(raw) > 0:
            fallback = _fetch_deal_history_no_hint(
                proxy, date_from, date_to, ticket_int,
            )
            if fallback:
                log.info(
                    f"snow.recovery.deal_history_fallback_hit "
                    f"ticket={ticket_int} found={len(fallback)}"
                )
                return fallback
        return filtered
    return None


def _utc_to_broker_naive(
    dt_utc: _dt.datetime, mt5_proxy,
) -> _dt.datetime:
    """FLO-380: convert tz-aware UTC datetime to broker-naive for
    MT5 history_deals_get / copy_rates_range.

    MT5's server-side `deal.time` is stored as
    `mktime(broker_local_struct)` — broker wall-clock seconds, NOT
    real UTC seconds. The Python API in turn converts a naive
    datetime arg by treating it as box-local time. So to select a
    moment at real UTC `T`, the naive datetime we pass must render
    via `fromtimestamp` as the broker-stored unix for that moment
    (= utc_unix + broker_offset_s).

    Falls back to a naive UTC datetime if the MT5 tick is
    unavailable. That fallback misclassifies the window by the
    broker offset (~3h) but is still better than passing tz-aware,
    which causes MT5 to drop recent deals entirely. Production
    paths always have a live tick; the fallback only fires in unit
    tests with a fake proxy that omits `symbol_info_tick`.

    Mirrors `mfe_backfill._utc_to_broker_naive` and
    `regime_detector._broker_offset_s` — established codebase
    pattern. Local copy keeps `snow.recovery` self-contained
    (no cross-module dependency to a non-snow file).
    """
    try:
        tick = mt5_proxy.symbol_info_tick("XAUUSD")
        tick_time = int(getattr(tick, "time", 0)) if tick else 0
        if tick_time:
            offset_s = tick_time - int(_time.time())
            broker_unix = int(dt_utc.timestamp()) + offset_s
            return _dt.datetime.fromtimestamp(broker_unix)
    except Exception as e:
        log.warning(
            f"snow.recovery.broker_offset_unavailable "
            f"{type(e).__name__}: {e} — falling back to naive UTC"
        )
    return dt_utc.replace(tzinfo=None)


def _fetch_deal_history_no_hint(
    proxy, date_from, date_to, ticket_int: int,
) -> list:
    """FLO-379 fallback path. Drops the `position=ticket` hint and
    filters manually. Returns [] on any MT5 error — caller treats
    that as the same "no match" signal."""
    try:
        deals = proxy.history_deals_get(date_from, date_to)
    except Exception as e:
        log.warning(
            f"snow.recovery.deal_history_fallback_error "
            f"ticket={ticket_int} error={type(e).__name__}: {e}"
        )
        return []
    if not deals:
        return []
    return [
        d for d in deals
        if int(getattr(d, "position_id", -1)) == ticket_int
    ]


# =============================================================================
# Per-plan handlers
# =============================================================================

def _reconcile_plan(
    row: dict, by_ticket: dict, summary: ReconcileSummary,
    *, tracker, mt5_proxy, now: _dt.datetime,
) -> None:
    status = str(row.get("status") or "")
    plan_id = row["id"]
    if status == PlanStatus.PENDING.value:
        _reconcile_pending(plan_id, row, summary, now)
    elif status == PlanStatus.TRIGGERED.value:
        _reconcile_triggered(plan_id, row, by_ticket, summary, tracker)
    elif status == PlanStatus.CLOSING.value:
        _reconcile_closing(plan_id, row, by_ticket, summary, mt5_proxy)
    elif status == PlanStatus.ACTIVE.value:
        _reconcile_active(
            plan_id, row, by_ticket, summary, tracker, mt5_proxy,
        )


def _reconcile_pending(
    plan_id: str, row: dict, summary: ReconcileSummary, now: _dt.datetime,
) -> None:
    expires_raw = row.get("expires_at")
    if not expires_raw:
        return  # No expiry set; leave alone (operator territory).
    expires = _parse_iso_z(str(expires_raw))
    if expires is None:
        log.warning(
            f"snow.recovery.pending_unparseable_expires_at "
            f"plan_id={plan_id} value={expires_raw!r}"
        )
        return
    if expires > now:
        return  # Not yet expired.
    snow_db.mark_plan_terminal(plan_id, PlanStatus.EXPIRED.value)
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_recovery",
        event="recovery_expired",
        conditions_snapshot={
            "reason": "pending_expired",
            "expires_at": expires_raw,
            "reconciled_at": utc_iso(),
        },
    )
    summary.pending_expired += 1
    log.info(
        f"snow.recovery.pending_expired plan_id={plan_id} "
        f"expires_at={expires_raw}"
    )


def _reconcile_triggered(
    plan_id: str, row: dict, by_ticket: dict,
    summary: ReconcileSummary, tracker,
) -> None:
    ticket = row.get("trade_ticket")
    if ticket is not None and int(ticket) in by_ticket:
        pos = by_ticket[int(ticket)]
        snow_db.update_plan_status(plan_id, PlanStatus.ACTIVE.value)
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_recovery",
            event="recovery_triggered_to_active",
            conditions_snapshot={
                "ticket": int(ticket),
                "open_price": float(pos.price_open),
                "reconciled_at": utc_iso(),
            },
        )
        summary.triggered_to_active += 1
        if tracker is not None:
            _seed_tracker(tracker, plan_id, row, pos, summary)
        log.info(
            f"snow.recovery.triggered_to_active plan_id={plan_id} "
            f"ticket={ticket} open_price={pos.price_open}"
        )
        return

    # No matching position — crash during trigger.
    snow_db.mark_plan_terminal(plan_id, PlanStatus.FAILED.value)
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_recovery",
        event="recovery_failed",
        conditions_snapshot={
            "reason": "crash_during_trigger",
            "ticket_in_db": ticket,
            "reconciled_at": utc_iso(),
        },
    )
    summary.triggered_failed += 1
    log.warning(
        f"snow.recovery.triggered_failed plan_id={plan_id} "
        f"reason=crash_during_trigger ticket_in_db={ticket}"
    )


def _reconcile_closing(
    plan_id: str, row: dict, by_ticket: dict,
    summary: ReconcileSummary, mt5_proxy,
) -> None:
    ticket = row.get("trade_ticket")
    if ticket is not None and int(ticket) in by_ticket:
        # Position still open — close action was in flight; revert to
        # ACTIVE so the normal exit path retries next tick.
        snow_db.update_plan_status(plan_id, PlanStatus.ACTIVE.value)
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_recovery",
            event="recovery_closing_to_active",
            conditions_snapshot={
                "ticket": int(ticket),
                "reconciled_at": utc_iso(),
            },
        )
        summary.closing_to_active += 1
        log.info(
            f"snow.recovery.closing_to_active plan_id={plan_id} "
            f"ticket={ticket}"
        )
        return

    # Position absent — close completed during downtime.
    snow_db.mark_plan_terminal(plan_id, PlanStatus.CLOSED.value)
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_recovery",
        event="recovery_closing_to_closed",
        conditions_snapshot={
            "ticket": ticket,
            "reconciled_at": utc_iso(),
        },
    )
    summary.closing_to_closed += 1
    log.info(
        f"snow.recovery.closing_to_closed plan_id={plan_id} "
        f"ticket={ticket}"
    )
    # FLO-353 — backfill outcome columns from MT5 deal history.
    # Best-effort; never raises.
    if ticket is not None:
        from snow.outcome import backfill_outcome
        backfill_outcome(plan_id, int(ticket), mt5_proxy=mt5_proxy)


def _reconcile_active(
    plan_id: str, row: dict, by_ticket: dict,
    summary: ReconcileSummary, tracker, mt5_proxy,
) -> None:
    ticket = row.get("trade_ticket")
    if ticket is not None and int(ticket) in by_ticket:
        # Position still open — bucket 5: tracker reseed only.
        if tracker is not None:
            pos = by_ticket[int(ticket)]
            _seed_tracker(tracker, plan_id, row, pos, summary)
        return

    # Position absent — closed externally OR data loss. Query deal
    # history; retry transient MT5 errors before deciding.
    if ticket is None:
        # No ticket assigned to an ACTIVE plan — true data loss.
        snow_db.mark_plan_terminal(plan_id, PlanStatus.FAILED.value)
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_recovery",
            event="recovery_failed",
            conditions_snapshot={
                "reason": "active_without_ticket",
                "reconciled_at": utc_iso(),
            },
        )
        summary.active_failed += 1
        log.warning(
            f"snow.recovery.active_failed plan_id={plan_id} "
            f"reason=active_without_ticket"
        )
        return

    deals = fetch_deal_history(int(ticket), mt5_proxy=mt5_proxy)
    if deals is None:
        # Transient MT5 error — leave for next startup; do NOT FAIL.
        summary.active_left_for_retry += 1
        log.warning(
            f"snow.recovery.active_left_for_retry plan_id={plan_id} "
            f"ticket={ticket} reason=deal_history_unknown"
        )
        return
    if deals:
        # Closed externally / SL or TP hit during downtime.
        snow_db.mark_plan_terminal(plan_id, PlanStatus.CLOSED.value)
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_recovery",
            event="recovery_active_to_closed",
            conditions_snapshot={
                "ticket": int(ticket),
                "deal_count": len(deals),
                "reconciled_at": utc_iso(),
            },
        )
        summary.active_to_closed += 1
        log.info(
            f"snow.recovery.active_to_closed plan_id={plan_id} "
            f"ticket={ticket} deals={len(deals)}"
        )
        # FLO-353 — backfill outcome columns. Reuses the deals we just
        # fetched conceptually, but `backfill_outcome` re-queries to
        # apply its own retry logic + handle direction inference. The
        # extra MT5 call is the price of a clean module boundary; it's
        # rare (only on recovery-CLOSED) and capped at 3 retries.
        from snow.outcome import backfill_outcome
        backfill_outcome(plan_id, int(ticket), mt5_proxy=mt5_proxy)
        return
    # Definitive empty list — position never existed (or older than
    # lookback window). Mark FAILED so the operator notices.
    snow_db.mark_plan_terminal(plan_id, PlanStatus.FAILED.value)
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_recovery",
        event="recovery_failed",
        conditions_snapshot={
            "reason": "position_vanished",
            "ticket": int(ticket),
            "lookback_days": _DEAL_HISTORY_LOOKBACK_DAYS,
            "reconciled_at": utc_iso(),
        },
    )
    summary.active_failed += 1
    log.warning(
        f"snow.recovery.active_failed plan_id={plan_id} "
        f"reason=position_vanished ticket={ticket}"
    )


# =============================================================================
# Helpers
# =============================================================================

def _seed_tracker(
    tracker, plan_id: str, row: dict, pos, summary: ReconcileSummary,
) -> None:
    """Seed/reseed PerPlanTracker for a plan whose MT5 position is
    confirmed open. Uses MT5's broker-authoritative `pos.price_open`,
    not a snow_plans column (no `entry_price` column exists yet)."""
    direction = _extract_direction(row)
    if direction is None:
        log.warning(
            f"snow.recovery.tracker_seed_skipped plan_id={plan_id} "
            f"reason=no_direction"
        )
        return
    try:
        tracker.seed(plan_id, float(pos.price_open), direction)
    except Exception as e:
        log.warning(
            f"snow.recovery.tracker_seed_failed plan_id={plan_id} "
            f"error={type(e).__name__}: {e}"
        )
        return
    summary.tracker_reseeds += 1


def _extract_direction(row: dict):
    """Pull the entry direction from the plan row's frozen plan_json
    blob. Returns either a `Direction` enum value or None if the JSON
    isn't shaped as expected."""
    import json
    from snow.schema import Direction
    plan_json = row.get("plan_json")
    if not plan_json:
        return None
    try:
        parsed = json.loads(plan_json)
    except (TypeError, ValueError):
        return None
    entry = parsed.get("entry")
    if not isinstance(entry, dict):
        return None
    direction_str = entry.get("direction")
    if not direction_str:
        return None
    try:
        return Direction(str(direction_str))
    except ValueError:
        return None


def _parse_iso_z(ts: str) -> Optional[_dt.datetime]:
    if not ts or not ts.endswith("Z"):
        return None
    try:
        return _dt.datetime.fromisoformat(ts[:-1]).replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None
