"""One-shot: re-run outcome backfill for PLAN-20260426-002 after the
position_id filter fix (commit 493c0b3).

Usage:
    python scripts/rerun_backfill_FLO-353.py

Reads MT5 deal history via the now-fixed `snow.recovery.fetch_deal_history`
+ `snow.outcome.backfill_outcome`, recomputes outcome_pips and
outcome_usd for the named plan, and writes the corrected values to
snow_plans (with a fresh audit row).

This script is intentionally NOT generic — it targets the known bad
plan. A future ad-hoc backfill of multiple plans would adapt this
template.
"""
from __future__ import annotations

import json
import os
import sys

# Allow running from any cwd by putting repo root on sys.path —
# same pattern as scripts/snow_status.py.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import MetaTrader5 as mt5

from snow import db as snow_db
from snow.outcome import backfill_outcome


PLAN_ID = "PLAN-20260426-002"
TICKET = 1612264515


def main() -> int:
    print(f"=== rerun backfill {PLAN_ID} (ticket {TICKET}) ===")

    # 1. Pre-state.
    pre = snow_db.get_plan(PLAN_ID)
    if pre is None:
        print(f"FATAL: plan {PLAN_ID} not found in snow_plans")
        return 1
    print(f"PRE: status={pre['status']} "
          f"outcome_pips={pre['outcome_pips']} "
          f"outcome_usd={pre['outcome_usd']}")

    # 2. Initialise MT5 in this process (the running bot has its
    # own connection in another process; concurrent Python clients
    # to the same terminal are supported).
    if not mt5.initialize():
        print(f"FATAL: mt5.initialize failed: {mt5.last_error()}")
        return 2
    try:
        # 3. Call the now-fixed backfill. The fix is in the shared
        # `fetch_deal_history` helper that backfill_outcome uses.
        result = backfill_outcome(PLAN_ID, TICKET)
        print("\nBACKFILL RESULT:")
        print(f"  success     = {result.success}")
        print(f"  reason      = {result.reason}")
        print(f"  outcome_pips= {result.outcome_pips}")
        print(f"  outcome_usd = {result.outcome_usd}")
        print(f"  deal_count  = {result.deal_count}")
    finally:
        mt5.shutdown()

    # 4. Post-state.
    post = snow_db.get_plan(PLAN_ID)
    print(f"\nPOST: status={post['status']} "
          f"outcome_pips={post['outcome_pips']} "
          f"outcome_usd={post['outcome_usd']}")

    # 5. Audit trail — show the most recent _outcome event.
    conn = snow_db._connect()
    try:
        rows = conn.execute(
            "SELECT id, event, conditions_snapshot FROM snow_evaluations "
            "WHERE plan_id = ? AND contingency_name = '_outcome' "
            "ORDER BY id DESC LIMIT 3",
            (PLAN_ID,),
        ).fetchall()
    finally:
        conn.close()
    print("\nRECENT _outcome AUDIT ROWS (newest first):")
    for r in rows:
        snapshot = r["conditions_snapshot"] or "{}"
        try:
            parsed = json.loads(snapshot)
            print(f"  [id={r['id']}] event={r['event']}")
            for k, v in parsed.items():
                print(f"      {k}: {v}")
        except Exception:
            print(f"  [id={r['id']}] event={r['event']} snapshot={snapshot}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
