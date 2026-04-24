"""snow_status.py — quick observability for the FLO-347 evidence window.

Prints a human-readable snapshot of Snow's DB state so the CEO can
audit what plans exist, what fires happened, and what actions were
attempted during the DRY RUN observation window.

Usage:
    python scripts/snow_status.py
    python scripts/snow_status.py --plan-id PLAN-20260424-001
    python scripts/snow_status.py --limit 50
    python scripts/snow_status.py --terminal        # include closed/cancelled/expired/failed

Read-only. Does not touch the executor, Floki, or any running process —
a separate process running this script cannot interfere with the bot.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

# Allow running this script directly (`python scripts/snow_status.py`)
# from any working directory by putting the repo root on sys.path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Use the project's canonical DB path via snow.db (same _connect wrapper
# the loop uses — single source of truth for where history.db lives).
try:
    from snow import db as snow_db
    from snow.schema import PlanStatus
except Exception as e:  # pragma: no cover — diagnostic bail-out
    print(f"ERROR: cannot import snow package: {e}", file=sys.stderr)
    sys.exit(1)


_TERMINAL = {
    PlanStatus.CLOSED.value,
    PlanStatus.CANCELLED.value,
    PlanStatus.EXPIRED.value,
    PlanStatus.FAILED.value,
}


def _print_section(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _pretty_json(blob: Optional[str]) -> str:
    if not blob:
        return "-"
    try:
        return json.dumps(json.loads(blob), indent=2, sort_keys=True)
    except Exception:
        return str(blob)


def _print_plan_row(row: dict[str, Any]) -> None:
    print(
        f"  {row.get('id'):<26} {row.get('status'):<10} "
        f"ticket={row.get('trade_ticket') or '-':<8} "
        f"created={row.get('created_at') or '-':<22} "
        f"last_eval={row.get('last_evaluated_at') or '-'}"
    )
    if row.get("entered_at"):
        print(f"      entered_at={row['entered_at']}")
    if row.get("closed_at"):
        print(f"      closed_at={row['closed_at']}  "
              f"pips={row.get('outcome_pips')}  "
              f"usd={row.get('outcome_usd')}")


def _print_evaluation_row(row: dict[str, Any]) -> None:
    snap = _pretty_json(row.get("conditions_snapshot"))
    print(
        f"  [{row.get('id')}] {row.get('recorded_at') or '-'} "
        f"{row.get('plan_id')} / {row.get('contingency_name')} "
        f"-> {row.get('event')}"
    )
    if snap != "-":
        for line in snap.splitlines():
            print(f"      {line}")


def _print_trigger_row(row: dict[str, Any]) -> None:
    print(
        f"  [{row.get('id')}] {row.get('fired_at') or '-'} "
        f"{row.get('plan_id')} / {row.get('contingency_name')} "
        f"[{row.get('contingency_kind')}] "
        f"{row.get('action_type')} -> {row.get('execution_status')}"
    )
    params = row.get("action_params")
    if params:
        print(f"      params: {_pretty_json(params)}")
    result = row.get("execution_result")
    if result:
        print(f"      result: {_pretty_json(result)}")


def _list_plans(include_terminal: bool, plan_id: Optional[str]):
    if plan_id:
        row = snow_db.get_plan(plan_id)
        return [row] if row else []
    all_statuses = [s.value for s in PlanStatus]
    if not include_terminal:
        all_statuses = [s for s in all_statuses if s not in _TERMINAL]
    return snow_db.list_plans_by_status(all_statuses, limit=10_000)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-id", help="Narrow to a single plan_id")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max evaluations / triggers to print (default: 20)")
    parser.add_argument("--terminal", action="store_true",
                        help="Include terminal plans (closed/cancelled/expired/failed)")
    args = parser.parse_args(argv)

    try:
        snow_db.init_snow_tables()  # idempotent; safe to run standalone
    except Exception as e:
        print(f"ERROR: init_snow_tables failed: {e}", file=sys.stderr)
        return 2

    plans = _list_plans(args.terminal, args.plan_id)
    _print_section(f"PLANS ({len(plans)})")
    if not plans:
        print("  (none)")
    else:
        for p in plans:
            if p:
                _print_plan_row(p)

    # Recent evaluations (all plans, sorted newest first by id DESC).
    _print_section(f"RECENT EVALUATIONS (last {args.limit})")
    # snow.db doesn't expose a list_evaluations helper — reach in via the
    # same connection wrapper for parity with the rest of the CLI.
    conn = snow_db._connect()
    try:
        where = ""
        params: tuple = ()
        if args.plan_id:
            where = "WHERE plan_id = ?"
            params = (args.plan_id,)
        rows = conn.execute(
            f"SELECT * FROM snow_evaluations {where} "
            f"ORDER BY id DESC LIMIT ?",
            params + (args.limit,),
        ).fetchall()
        if not rows:
            print("  (none)")
        else:
            for r in rows:
                _print_evaluation_row(dict(r))
    finally:
        conn.close()

    # Recent triggers.
    _print_section(f"RECENT TRIGGERS (last {args.limit})")
    triggers = snow_db.list_triggers(plan_id=args.plan_id, limit=args.limit)
    if not triggers:
        print("  (none)")
    else:
        for t in triggers:
            _print_trigger_row(t)

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
