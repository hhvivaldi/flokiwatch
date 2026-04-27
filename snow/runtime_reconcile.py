"""FLO-379 — Runtime in-loop reconciliation for Snow plans.

Catches ACTIVE plans whose broker positions closed outside Snow's
dispatch path (broker SL fire, broker TP fire, manual terminal close)
while the bot is running. `snow.recovery.reconcile_on_startup` only
runs at boot and cannot detect mid-run closes; this module fills the
gap.

Pre-FLO-379 incident — PLAN-20260427-004
----------------------------------------
SELL plan with BE-locked SL fired broker-side at 13:11:35 UTC. Snow
plan record stayed `status=active`, `closed_at=NULL` for 2+ hours
because no in-loop pass reconciled the broker close back into Snow's
state. Floki, who reads `list_active_plans` at cycle start and is
prompt-instructed not to submit duplicates while plans are watching,
was silently throttled — zero new plan submissions across 16 cycles
post-close. Dashboard showed stale state. Manual stop-gap unblocked
him; this module ensures the gap doesn't reopen.

Differences from `reconcile_on_startup`
---------------------------------------
- **Scope**: only the ACTIVE-without-position bucket. Other buckets
  (PENDING expiry, TRIGGERED crash, CLOSING leftover) are
  startup-only conditions — if they ever appear mid-loop, the next
  restart's reconcile catches them. Keeping the runtime pass narrow
  reduces the blast radius of any bug it carries.
- **Fail-soft**: NEVER raises. All failures (DB read error, MT5
  disconnect, per-plan exception) log a WARNING and return whatever
  summary we built. The Snow loop is a long-running service; a
  transient MT5 hiccup must not crash it.
- **Broker close time**: `closed_at` is stamped from the OUT deal's
  timestamp in MT5 history, not from `utc_iso()` at detection. CTO
  directive on FLO-379 acceptance — audit accuracy beats convention.
- **Idempotent**: `mark_plan_terminal`'s `COALESCE(closed_at, ?)`
  preserves the first transition's `closed_at`. Safe to call
  concurrently with startup recovery; safe to call repeatedly. The
  audit row uses `event="runtime_active_to_closed"` so future
  operators can distinguish runtime closes from startup closes.

Boundary
--------
Imports `snow.db`, `snow.recovery.fetch_deal_history`,
`snow.outcome.backfill_outcome`, `mt5_safe.mt5`, and `config`.
Does NOT import `executor.py`, `snow.actions`, or `snow.snow_loop` —
the loop calls into here, not the reverse.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional

import config
from logger import log
from mt5_safe import mt5 as _default_mt5
from tz_utils import utc_iso

from snow import db as snow_db
from snow.recovery import fetch_deal_history
from snow.schema import PlanStatus


@dataclass
class RuntimeReconcileSummary:
    """Counts emitted by `reconcile_runtime`. Mirrors the shape of
    the startup summary's fields that apply at runtime — only the
    active-without-position bucket is reachable mid-loop."""
    plans_checked: int = 0
    active_to_closed: int = 0
    active_left_for_retry: int = 0

    def as_log_kvs(self) -> str:
        return (
            f"plans_checked={self.plans_checked} "
            f"active_to_closed={self.active_to_closed} "
            f"active_left_for_retry={self.active_left_for_retry}"
        )


def reconcile_runtime(
    *,
    mt5_proxy=None,
    symbol: Optional[str] = None,
    magic: Optional[int] = None,
) -> RuntimeReconcileSummary:
    """Run one in-loop reconciliation pass.

    Args:
      mt5_proxy: Defaults to `mt5_safe.mt5`. Tests inject a fake.
      symbol: Defaults to `config.SYMBOL`.
      magic: Defaults to `config.MAGIC_NUMBER`.

    Returns:
      `RuntimeReconcileSummary`. Always returns; never raises.
    """
    summary = RuntimeReconcileSummary()
    proxy = mt5_proxy if mt5_proxy is not None else _default_mt5
    sym = symbol or getattr(config, "SYMBOL", "XAUUSD")
    mg = magic if magic is not None else int(
        getattr(config, "MAGIC_NUMBER", 0)
    )

    try:
        active_rows = snow_db.list_plans_by_status(
            (PlanStatus.ACTIVE.value,), limit=10_000,
        )
    except Exception as e:
        log.warning(
            f"snow.runtime_reconcile.db_read_failed "
            f"{type(e).__name__}: {e}"
        )
        return summary

    # Candidates: ACTIVE plans with a ticket assigned and no
    # closed_at yet. ACTIVE-without-ticket is a startup-only
    # data-loss condition; we skip it here so a transient runtime
    # data-read race can't mass-FAIL plans.
    candidates = [
        r for r in active_rows
        if r.get("trade_ticket") is not None
        and r.get("closed_at") is None
    ]
    summary.plans_checked = len(candidates)
    if not candidates:
        return summary

    try:
        raw_positions = proxy.positions_get(symbol=sym)
    except Exception as e:
        log.warning(
            f"snow.runtime_reconcile.mt5_query_failed "
            f"{type(e).__name__}: {e}"
        )
        return summary
    if raw_positions is None:
        # MT5 disconnect. NEVER mass-FAIL on this — leave for next
        # pass. Same defensive contract reconcile_on_startup honors
        # for its batch positions query (where it does abort, but
        # that's startup; runtime stays soft).
        log.warning(
            "snow.runtime_reconcile.mt5_disconnected — skipping pass"
        )
        return summary

    by_ticket = {p.ticket: p for p in raw_positions if p.magic == mg}

    for row in candidates:
        plan_id = row.get("id") or "<unknown>"
        ticket = row.get("trade_ticket")
        try:
            ticket_int = int(ticket)
        except (TypeError, ValueError):
            continue
        if ticket_int in by_ticket:
            continue  # Position still open — nothing to do.
        try:
            _runtime_reconcile_one(
                plan_id, ticket_int, summary, mt5_proxy=proxy,
            )
        except Exception as e:
            import traceback as _tb
            log.error(
                f"snow.runtime_reconcile.plan_failed plan_id={plan_id} "
                f"{type(e).__name__}: {e}\n{_tb.format_exc()}"
            )

    return summary


def _runtime_reconcile_one(
    plan_id: str,
    ticket: int,
    summary: RuntimeReconcileSummary,
    *,
    mt5_proxy,
) -> None:
    """Reconcile a single ACTIVE plan whose position is known absent.
    Pulls deal history, extracts the broker close time, stamps
    `status=CLOSED` + `closed_at=broker_time`, then attempts outcome
    backfill (best-effort)."""
    # FLO-379: max_attempts=1 keeps the Snow tick non-blocking. At
    # runtime the next reconcile pass is 60s away, so a transient None
    # response is fine to retry next cycle rather than blocking up to
    # 3.5s here. Startup recovery keeps the full 3-attempt policy.
    deals = fetch_deal_history(ticket, mt5_proxy=mt5_proxy, max_attempts=1)
    if deals is None:
        # Transient MT5 error — single attempt failed.
        summary.active_left_for_retry += 1
        log.info(
            f"snow.runtime_reconcile.left_for_retry plan_id={plan_id} "
            f"ticket={ticket} reason=deal_history_unavailable"
        )
        return
    if not deals:
        # No deals for this ticket. Could be MT5 deal-write lag or
        # genuine vanish. Conservative: leave for the next pass.
        # Startup recovery will FAIL it as `position_vanished` if
        # the condition persists across a restart.
        summary.active_left_for_retry += 1
        log.warning(
            f"snow.runtime_reconcile.left_for_retry plan_id={plan_id} "
            f"ticket={ticket} reason=no_deals_yet"
        )
        return

    close_time_iso = _broker_close_time_from_deals(deals)
    snow_db.mark_plan_terminal(
        plan_id, PlanStatus.CLOSED.value, closed_at=close_time_iso,
    )
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_runtime_reconcile",
        event="runtime_active_to_closed",
        conditions_snapshot={
            "ticket": int(ticket),
            "deal_count": len(deals),
            "broker_close_time_utc": close_time_iso,
            "reconciled_at": utc_iso(),
        },
    )
    summary.active_to_closed += 1
    log.info(
        f"snow.runtime_reconcile.active_to_closed plan_id={plan_id} "
        f"ticket={ticket} broker_close={close_time_iso} "
        f"deals={len(deals)}"
    )

    try:
        from snow.outcome import backfill_outcome
        backfill_outcome(plan_id, int(ticket), mt5_proxy=mt5_proxy)
    except Exception as e:
        log.warning(
            f"snow.runtime_reconcile.outcome_backfill_failed "
            f"plan_id={plan_id} ticket={ticket} "
            f"{type(e).__name__}: {e}"
        )


def _broker_close_time_from_deals(deals: list) -> Optional[str]:
    """Extract broker-side close time as ISO-8601 UTC Z string.

    Looks at DEAL_ENTRY_OUT deals (`entry == 1`) and picks the latest
    by `time`. Returns None if no OUT deal or if the timestamp is
    unparseable — caller's `mark_plan_terminal` then falls back to
    `utc_iso()` now via the helper's optional-arg semantics.
    """
    out_deals = [d for d in deals if int(getattr(d, "entry", -1)) == 1]
    if not out_deals:
        return None
    latest = max(
        out_deals,
        key=lambda d: int(getattr(d, "time", 0) or 0),
    )
    try:
        ts = _dt.datetime.fromtimestamp(
            int(getattr(latest, "time", 0)),
            tz=_dt.timezone.utc,
        )
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
