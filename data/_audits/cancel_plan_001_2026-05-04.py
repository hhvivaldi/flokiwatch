"""One-shot operator cancel of PLAN-20260503-001 (FLO-419 entry-condition
ban, CEO 2026-05-04). PLAN-001 used `price_at_sr_zone tolerance_pips=8`
in entry — would have fired at any drifted "nearest support" Brain
surfaced later, regardless of the multi-confluence thesis at 4605-4612.

Uses the canonical cancel path (snow.db.mark_plan_terminal +
snow.db.record_trigger) for full audit trail, mirroring
agent_tools.cancel_plan."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from snow import db as _snow_db

PLAN_ID = "PLAN-20260503-001"
REASON = (
    "operator_cancel (FLO-419, CEO 2026-05-04): entry uses dynamic "
    "price_at_sr_zone with no fixed price. Validator now rejects this "
    "primitive in entry.conditions; pre-existing plan must be cancelled "
    "so Floki authors a replacement next cycle with a fixed price level."
)


def main():
    row = _snow_db.get_plan(PLAN_ID)
    if row is None:
        print(f"FAIL: {PLAN_ID} not found")
        return 1
    status = row.get("status")
    print(f"BEFORE: {PLAN_ID} status={status}")
    if status != "pending":
        print(f"FAIL: {PLAN_ID} is {status}; cancel only valid for pending")
        return 1

    _snow_db.mark_plan_terminal(PLAN_ID, "cancelled")
    _snow_db.record_trigger(
        plan_id=PLAN_ID,
        contingency_name="_user_cancel",
        contingency_kind="entry",
        action_type="cancel_plan",
        execution_status="success",
        action_params={"reason": REASON},
    )

    after = _snow_db.get_plan(PLAN_ID)
    print(f"AFTER : {PLAN_ID} status={after.get('status')} closed_at={after.get('closed_at')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
