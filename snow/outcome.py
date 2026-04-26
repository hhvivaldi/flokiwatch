"""Snow outcome backfill — FLO-353.

Sister blocker to FLO-354. After a position closes — whether via an
explicit Snow `close_full` / `close_partial` action, an external SL/TP
hit, a manual MT5 intervention, or a recovery-path CLOSED transition
— the `snow_plans` row's `outcome_pips` and `outcome_usd` columns
must be populated so dashboards and analytics can show per-plan P&L.

This module ships the engine; both `snow/actions.py`'s close path
and `snow/recovery.py`'s ACTIVE→CLOSED / CLOSING→CLOSED transitions
call `backfill_outcome(plan_id, ticket)` after the position has been
confirmed gone.

Best-effort, NEVER raises
-------------------------
The trade has already closed by the time we run. The outcome columns
are observability — missing them is a degraded UX, not a correctness
problem. So `backfill_outcome` always returns a `BackfillResult` and
records an audit row even on failure. Callers that misuse this and
treat a missing outcome as a fatal condition will not get the chance:
exceptions are caught + logged + swallowed inside.

Asymmetric to FLO-354's recovery path, which is fail-loud (data
integrity > continuing wrong). Here the trade is already closed; we
just want to know about it.

Pip / USD arithmetic
--------------------
- `outcome_usd = sum(deal.profit for deal in close_deals)`. MT5
  reports profit per deal in account currency, already direction- and
  volume-adjusted.
- `outcome_pips = ((vw_close_price - open_price) * sign) / PIP_SIZE`
  where:
    * `vw_close_price = sum(d.price * d.volume) / sum(d.volume)`
      across all close deals (volume-weighted average — partial closes
      collapse to a single representative number)
    * `open_price = first IN-entry deal's price`
    * `sign = +1` if direction == BUY, `-1` if SELL
    * `PIP_SIZE` from `snow.evaluators.context.PIP_SIZE` (0.1 for XAUUSD)

For pure-full-close trades this reduces to the obvious
`(close_price - open_price) * sign / PIP_SIZE`.

Reuses `snow.recovery.fetch_deal_history` for the retry+backoff
helper. Same lookback (14 days), same retry policy (3 attempts,
0.5/1/2 s).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from logger import log
from tz_utils import utc_iso

from snow import db as snow_db
from snow.evaluators.context import PIP_SIZE
from snow.recovery import fetch_deal_history


# MT5 deal-entry constants (avoid importing the live module so the
# module is testable without a connected MT5; tests construct fake
# deals with explicit entry-int values).
_DEAL_ENTRY_IN: int = 0
_DEAL_ENTRY_OUT: int = 1
_DEAL_ENTRY_INOUT: int = 2

# MT5 deal-type constants for direction inference (BUY position has an
# IN deal of type=0; SELL position IN deal type=1).
_DEAL_TYPE_BUY: int = 0
_DEAL_TYPE_SELL: int = 1


@dataclass
class BackfillResult:
    plan_id: str
    success: bool
    outcome_pips: Optional[float] = None
    outcome_usd: Optional[float] = None
    deal_count: int = 0
    reason: Optional[str] = None


def backfill_outcome(
    plan_id: str,
    ticket: int,
    *,
    mt5_proxy=None,
) -> BackfillResult:
    """Best-effort backfill of `snow_plans.outcome_pips` /
    `snow_plans.outcome_usd` for `plan_id` from MT5 deal history.

    Always returns a `BackfillResult` — does NOT raise. Records an
    audit row in `snow_evaluations` whether the backfill succeeded or
    failed. Failure modes (deal history unavailable, no deals, parse
    error) leave outcome columns NULL and stamp an audit row with
    `event="outcome_backfill_failed"` and a `reason`.
    """
    try:
        return _backfill_outcome_inner(plan_id, ticket, mt5_proxy=mt5_proxy)
    except Exception as e:
        # Even unhandled exceptions must not propagate — observability
        # data is not worth crashing a close path or a recovery sweep.
        import traceback as _tb
        log.error(
            f"snow.outcome.backfill_unhandled plan_id={plan_id} "
            f"ticket={ticket} {type(e).__name__}: {e}\n{_tb.format_exc()}"
        )
        try:
            snow_db.record_evaluation(
                plan_id=plan_id,
                contingency_name="_outcome",
                event="outcome_backfill_failed",
                conditions_snapshot={
                    "ticket": int(ticket) if ticket is not None else None,
                    "reason": "unhandled_exception",
                    "error_type": type(e).__name__,
                    "error_message": str(e)[:200],
                    "reconciled_at": utc_iso(),
                },
            )
        except Exception:
            pass  # don't double-fail
        return BackfillResult(
            plan_id=plan_id, success=False,
            reason=f"unhandled: {type(e).__name__}: {e}",
        )


def _backfill_outcome_inner(
    plan_id: str, ticket: int, *, mt5_proxy=None,
) -> BackfillResult:
    deals = fetch_deal_history(ticket, mt5_proxy=mt5_proxy)
    if deals is None:
        # All retries returned None — transient MT5 error.
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_outcome",
            event="outcome_backfill_failed",
            conditions_snapshot={
                "ticket": int(ticket),
                "reason": "deal_history_unavailable",
                "reconciled_at": utc_iso(),
            },
        )
        log.warning(
            f"snow.outcome.deal_history_unavailable plan_id={plan_id} "
            f"ticket={ticket}"
        )
        return BackfillResult(
            plan_id=plan_id, success=False, deal_count=0,
            reason="deal_history_unavailable",
        )

    if not deals:
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_outcome",
            event="outcome_backfill_failed",
            conditions_snapshot={
                "ticket": int(ticket),
                "reason": "no_deals_for_ticket",
                "reconciled_at": utc_iso(),
            },
        )
        log.warning(
            f"snow.outcome.no_deals plan_id={plan_id} ticket={ticket}"
        )
        return BackfillResult(
            plan_id=plan_id, success=False, deal_count=0,
            reason="no_deals_for_ticket",
        )

    in_deals = [d for d in deals if int(getattr(d, "entry", -1)) == _DEAL_ENTRY_IN]
    close_deals = [
        d for d in deals
        if int(getattr(d, "entry", -1)) in (_DEAL_ENTRY_OUT, _DEAL_ENTRY_INOUT)
    ]

    if not in_deals or not close_deals:
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_outcome",
            event="outcome_backfill_failed",
            conditions_snapshot={
                "ticket": int(ticket),
                "reason": "shape_unexpected",
                "in_count": len(in_deals),
                "close_count": len(close_deals),
                "deal_count": len(deals),
                "reconciled_at": utc_iso(),
            },
        )
        log.warning(
            f"snow.outcome.shape_unexpected plan_id={plan_id} "
            f"ticket={ticket} in={len(in_deals)} close={len(close_deals)}"
        )
        return BackfillResult(
            plan_id=plan_id, success=False,
            deal_count=len(deals), reason="shape_unexpected",
        )

    open_price = float(in_deals[0].price)
    in_type = int(getattr(in_deals[0], "type", -1))
    sign = 1 if in_type == _DEAL_TYPE_BUY else (-1 if in_type == _DEAL_TYPE_SELL else 0)
    if sign == 0:
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_outcome",
            event="outcome_backfill_failed",
            conditions_snapshot={
                "ticket": int(ticket),
                "reason": "unknown_direction",
                "in_deal_type": in_type,
                "reconciled_at": utc_iso(),
            },
        )
        return BackfillResult(
            plan_id=plan_id, success=False,
            deal_count=len(deals), reason="unknown_direction",
        )

    total_close_volume = sum(float(getattr(d, "volume", 0.0)) for d in close_deals)
    if total_close_volume <= 0:
        snow_db.record_evaluation(
            plan_id=plan_id,
            contingency_name="_outcome",
            event="outcome_backfill_failed",
            conditions_snapshot={
                "ticket": int(ticket),
                "reason": "zero_close_volume",
                "reconciled_at": utc_iso(),
            },
        )
        return BackfillResult(
            plan_id=plan_id, success=False,
            deal_count=len(deals), reason="zero_close_volume",
        )

    vw_close = sum(
        float(getattr(d, "price", 0.0)) * float(getattr(d, "volume", 0.0))
        for d in close_deals
    ) / total_close_volume
    outcome_pips = (vw_close - open_price) * sign / PIP_SIZE
    outcome_usd = sum(float(getattr(d, "profit", 0.0)) for d in close_deals)

    snow_db.update_plan_outcome_columns_only(
        plan_id,
        outcome_pips=float(outcome_pips),
        outcome_usd=float(outcome_usd),
    )
    snow_db.record_evaluation(
        plan_id=plan_id,
        contingency_name="_outcome",
        event="outcome_backfilled",
        conditions_snapshot={
            "ticket": int(ticket),
            "open_price": open_price,
            "vw_close_price": vw_close,
            "outcome_pips": float(outcome_pips),
            "outcome_usd": float(outcome_usd),
            "close_deal_count": len(close_deals),
            "total_close_volume": total_close_volume,
            "direction": "BUY" if sign == 1 else "SELL",
            "reconciled_at": utc_iso(),
        },
    )
    log.info(
        f"snow.outcome.backfilled plan_id={plan_id} ticket={ticket} "
        f"pips={outcome_pips:.2f} usd={outcome_usd:.2f} "
        f"deals={len(close_deals)}"
    )
    return BackfillResult(
        plan_id=plan_id,
        success=True,
        outcome_pips=float(outcome_pips),
        outcome_usd=float(outcome_usd),
        deal_count=len(deals),
        reason=None,
    )
