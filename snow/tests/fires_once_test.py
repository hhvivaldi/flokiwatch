"""FLO-373: `fires: once` enforcement across loop ticks.

Pre-fix behaviour: snow_loop._evaluate_one_contingency only checked
`state == ARMED`. Nothing in production ever transitioned state out of
ARMED, so every tick that found conditions still all-true generated a
fresh FireEvent. Production evidence: PLAN-20260426-002's
`lock_be_at_5_pips` produced 88 successive snow_triggers rows after a
single legitimate fire.

These tests exercise both the DRY_RUN evaluation path (writes
`*_would_fire` rows to snow_evaluations) and the LIVE dispatch path
(writes execution_status='success' rows to snow_triggers). The
post-fix behaviour: across two consecutive ticks with conditions
constant-true, a `fires: once` contingency must produce exactly ONE
fire event.

Coverage requested in FLO-373 acceptance: adjust_sl / adjust_tp /
move_sl_to_breakeven / close_full / close_partial.
"""
from __future__ import annotations

import sqlite3
import threading
from copy import deepcopy
from typing import Any

import pytest

from snow import db as snow_db
from snow.schema import Plan, PlanStatus
from snow.snow_loop import SnowLoop


# ---------------------------------------------------------------------------
# Fixtures (reuse conftest's snow_conn / fake_live / fake_semantic /
# valid_plan_dict; the loop helpers below mirror snow_loop_test.py).
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Ephemeral tmp SQLite — same pattern as snow_loop_test.py."""
    db_path = tmp_path / "fires_once_test.db"

    def _tmp_connect():
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


class _FakeBot:
    def __init__(self, *, running=True):
        self.running = running


class _RefreshableLive:
    def __init__(self, inner):
        self._inner = inner

    def refresh(self):
        return None

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_loop(*, bot, live_data, semantic_cache, dry_run=True):
    return SnowLoop(
        bot,
        symbol="XAUUSD",
        live_data=_RefreshableLive(live_data),
        semantic_cache=semantic_cache,
        tracker=None,
        dry_run=dry_run,
        state_cache=None,
    )


def _insert_plan(plan_dict: dict[str, Any]) -> Plan:
    plan = Plan(**plan_dict)
    snow_db.insert_plan(plan)
    return plan


def _advance_to_active(plan_id: str, ticket: int = 111_222) -> None:
    snow_db.update_plan_trade_ticket(plan_id, ticket)
    snow_db.update_plan_status(plan_id, PlanStatus.ACTIVE.value)


def _eval_rows_for(db_path, contingency_name: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM snow_evaluations "
            "WHERE contingency_name = ? "
            "  AND event LIKE '%would_fire' "
            "ORDER BY id ASC",
            (contingency_name,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _trigger_rows_for(db_path, contingency_name: str) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM snow_triggers "
            "WHERE contingency_name = ? "
            "ORDER BY id ASC",
            (contingency_name,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helper: build a management/exit contingency with the requested action.
# ---------------------------------------------------------------------------

def _management_with_action(action_dict: dict) -> dict:
    """A management contingency that fires when price drops below
    4720, with the requested action. fires: once."""
    return {
        "name": "test_lock_once",
        "priority": 7,
        "conditions": [{"type": "price_below", "level": 4720.0}],
        "action": action_dict,
        "fires": "once",
    }


_ACTION_CASES = [
    pytest.param({"type": "adjust_sl", "price": 4727.0}, id="adjust_sl"),
    pytest.param({"type": "adjust_tp", "price": 4715.0}, id="adjust_tp"),
    pytest.param(
        {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
        id="move_sl_to_breakeven",
    ),
    pytest.param({"type": "close_full"}, id="close_full"),
    pytest.param({"type": "close_partial", "percent": 50.0}, id="close_partial"),
]


# ---------------------------------------------------------------------------
# DRY_RUN path — fires:once must dedup across ticks
# ---------------------------------------------------------------------------

class TestFiresOnceDryRun:
    @pytest.mark.parametrize("action_dict", _ACTION_CASES)
    def test_management_fires_at_most_once_across_ticks(
        self, action_dict, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """Two ticks, conditions still all-true on the second. Fix
        contract: exactly one would_fire row, not two.

        Pre-FLO-373: this assert finds 2 rows (the bug).
        Post-fix: 1 row.
        """
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = [_management_with_action(action_dict)]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4715.0)  # below 4720 threshold
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic(),
        )

        loop._tick()
        loop._tick()

        rows = _eval_rows_for(snow_conn, "test_lock_once")
        assert len(rows) == 1, (
            f"fires:once contingency fired {len(rows)} times across 2 ticks "
            f"(action={action_dict['type']}); expected 1. "
            f"This is the FLO-373 bug — pre-fix the loop never read the "
            f"audit log to dedup."
        )

    def test_exit_block_fires_once_too(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """The exit list shares the same evaluation path as management.
        Same fires:once contract."""
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = []
        mutated["exit"] = [{
            "name": "test_exit_once",
            "priority": 9,
            "conditions": [{"type": "price_above", "level": 4733.0}],
            "action": {"type": "close_full"},
            "fires": "once",
        }]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4734.0)  # above 4733
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic(),
        )

        loop._tick()
        loop._tick()
        loop._tick()  # third tick for good measure

        rows = _eval_rows_for(snow_conn, "test_exit_once")
        assert len(rows) == 1, (
            f"exit fires:once contingency fired {len(rows)} times across "
            f"3 ticks; expected 1."
        )

    def test_fires_every_time_still_fires_each_tick(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """Negative regression: the fix must NOT break fires:every_time —
        those should still fire on every tick where conditions are
        all-true."""
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = [{
            "name": "test_every_tick",
            "priority": 7,
            "conditions": [{"type": "price_below", "level": 4720.0}],
            "action": {"type": "adjust_sl", "price": 4727.0},
            "fires": "every_time",
        }]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4715.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic(),
        )

        loop._tick()
        loop._tick()
        loop._tick()

        rows = _eval_rows_for(snow_conn, "test_every_tick")
        assert len(rows) == 3, (
            f"fires:every_time contingency fired {len(rows)} times across "
            f"3 ticks; expected 3 (the fix must not regress non-once)."
        )

    def test_fires_once_with_failing_conditions_can_fire_later(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """fires:once does NOT mean 'evaluated once' — it means 'fires at
        most once'. If conditions are false on tick 1 and true on tick 2,
        the single fire happens on tick 2."""
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = [_management_with_action(
            {"type": "adjust_sl", "price": 4727.0}
        )]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)

        # Tick 1: price ABOVE 4720 — conditions false, no fire.
        loop_a = _make_loop(
            bot=_FakeBot(), live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
        )
        loop_a._tick()
        assert _eval_rows_for(snow_conn, "test_lock_once") == []

        # Tick 2: price BELOW 4720 — conditions true, ONE fire.
        loop_b = _make_loop(
            bot=_FakeBot(), live_data=fake_live(price_mid=4715.0),
            semantic_cache=fake_semantic(),
        )
        loop_b._tick()
        rows = _eval_rows_for(snow_conn, "test_lock_once")
        assert len(rows) == 1

        # Tick 3: still below — fire is suppressed (already fired once).
        loop_b._tick()
        rows = _eval_rows_for(snow_conn, "test_lock_once")
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# LIVE path — exercise via direct snow_triggers seeding (avoids needing
# the executor wiring; the loop's check just reads the audit log).
# ---------------------------------------------------------------------------

class TestFiresOnceLivePath:
    def test_existing_success_trigger_blocks_re_fire(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """Simulate the post-restart / mid-loop scenario: a contingency
        already has a successful snow_triggers row from a prior fire.
        On the next tick (DRY_RUN for test simplicity), the loop must
        treat that as already-fired and NOT generate a duplicate event.

        This also covers the recovery-path use case: bot restart hydrates
        plans, but snow_triggers carries the fire history. Fires:once
        must respect history, not just in-memory state."""
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = [_management_with_action(
            {"type": "move_sl_to_breakeven", "offset_pips": 0.0}
        )]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)

        # Seed a successful trigger row as if the contingency had fired
        # in a previous bot run.
        snow_db.record_trigger_and_transition(
            plan.id,
            contingency_name="test_lock_once",
            contingency_kind="management",
            action_type="move_sl_to_breakeven",
            execution_status="success",
            new_plan_status=PlanStatus.ACTIVE.value,
            action_params={"ticket": 111_222, "new_sl": 4720.0},
            execution_result={"success": True},
        )

        live = fake_live(price_mid=4715.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic(),
        )
        loop._tick()

        # No would_fire rows in evaluations — the contingency is treated
        # as already-fired and suppressed.
        rows = _eval_rows_for(snow_conn, "test_lock_once")
        assert rows == [], (
            f"fires:once with existing SUCCESS trigger row should be "
            f"suppressed; got {len(rows)} new fire(s)."
        )

    def test_failed_trigger_does_not_block_re_fire(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict,
    ):
        """A failed dispatch (retry_exhausted / timeout) is NOT a fire.
        The contingency must remain eligible to re-fire next tick."""
        mutated = deepcopy(valid_plan_dict)
        mutated["management"] = [_management_with_action(
            {"type": "adjust_sl", "price": 4727.0}
        )]
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)

        # Seed a FAILED trigger — should NOT block re-evaluation.
        snow_db.record_trigger_and_transition(
            plan.id,
            contingency_name="test_lock_once",
            contingency_kind="management",
            action_type="adjust_sl",
            execution_status="retry_exhausted",
            new_plan_status=PlanStatus.ACTIVE.value,
            action_params={"ticket": 111_222, "new_sl": 4727.0},
            execution_result={"success": False, "error_message": "timeout"},
        )

        live = fake_live(price_mid=4715.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic(),
        )
        loop._tick()

        # ONE new fire on the next tick — failed prior attempt does not
        # disable the contingency.
        rows = _eval_rows_for(snow_conn, "test_lock_once")
        assert len(rows) == 1
