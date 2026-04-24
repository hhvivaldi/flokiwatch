"""DB-layer tests — RFC §12.2 "Unit tests (DB) ~10 tests" (expanded).

Verifies:
  * DDL creates all 3 tables + 4 indexes; idempotent on repeat
  * Plan insert / get round-trip preserves JSON + column denormalisation
  * Plan round-trips through `model_validate_json`
  * Status transitions + trade ticket attach + outcome stamp
  * UTC timestamp invariant (Rule 22) on all module-generated times
  * I7 partial-UNIQUE matrix — 4 cases as per advisor review
  * Foreign-key enforcement (PRAGMA foreign_keys = ON)
  * schema_version column and plan_json always agree on insert
  * Atomic compound op rolls back on mid-transaction failure
  * Trigger + evaluation append/list
  * EXPLAIN QUERY PLAN confirms partial index used for hot-path query (CTO item 2)
  * Concurrent inserts under WAL succeed (CTO optional addition)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from copy import deepcopy
from typing import Any

import pytest

import db_writer
from snow import db as snow_db
from snow.schema import Plan, PlanStatus


# =============================================================================
# Fixture: on-disk tmp DB, monkeypatched for the whole test
# =============================================================================

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Point `snow.db._connect` at an ephemeral tmp-path .db file.

    We do NOT use ":memory:" because Snow (matching db_writer) opens a
    fresh connection per call — a new :memory: connection is a new
    (empty) DB, so CRUD tests would fail mysteriously with phantom empty
    reads. tmp_path gives per-test isolation with normal on-disk
    semantics.
    """
    db_path = tmp_path / "snow_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


@pytest.fixture
def sample_plan(valid_plan_dict) -> Plan:
    """Parse the canonical fixture dict into a Plan model."""
    return Plan(**valid_plan_dict)


def _count_rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# =============================================================================
# DDL / init
# =============================================================================

class TestInitSnowTables:

    def test_tables_created(self, snow_conn):
        conn = sqlite3.connect(str(snow_conn))
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert {"snow_plans", "snow_triggers", "snow_evaluations"} <= names

    def test_indexes_created(self, snow_conn):
        conn = sqlite3.connect(str(snow_conn))
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        conn.close()
        assert {
            "idx_snow_plans_status",
            "idx_snow_plans_expires",
            "idx_snow_plans_live_ticket",
            "idx_snow_triggers_plan_fired",
            "idx_snow_evaluations_plan_time",
        } <= names

    def test_idempotent(self, snow_conn):
        # Calling init again must not raise — IF NOT EXISTS everywhere.
        snow_db.init_snow_tables()
        snow_db.init_snow_tables()


# =============================================================================
# Plan insert / get round-trip
# =============================================================================

class TestPlanInsertGet:

    def test_insert_then_get_dict(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        row = snow_db.get_plan(sample_plan.id)
        assert row is not None
        assert row["id"] == sample_plan.id
        assert row["status"] == PlanStatus.PENDING.value
        assert row["schema_version"] == sample_plan.schema_version
        assert row["trade_ticket"] is None
        assert row["entered_at"] is None
        assert row["closed_at"] is None
        assert row["plan_json"]  # non-empty

    def test_get_missing_plan_returns_none(self, snow_conn):
        assert snow_db.get_plan("PLAN-99999999-999") is None

    def test_plan_json_roundtrips_through_pydantic(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        rehydrated = snow_db.get_plan_as_model(sample_plan.id)
        assert rehydrated is not None
        assert rehydrated.id == sample_plan.id
        assert rehydrated.entry.direction == sample_plan.entry.direction
        assert rehydrated.entry.initial_sl == sample_plan.entry.initial_sl
        assert len(rehydrated.management) == len(sample_plan.management)
        assert len(rehydrated.exit) == len(sample_plan.exit)

    def test_schema_version_column_matches_json(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        row = snow_db.get_plan(sample_plan.id)
        embedded = json.loads(row["plan_json"])["schema_version"]
        assert row["schema_version"] == embedded
        assert row["schema_version"] == sample_plan.schema_version

    def test_duplicate_plan_id_rejected(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        with pytest.raises(sqlite3.IntegrityError):
            snow_db.insert_plan(sample_plan)


# =============================================================================
# Status / ticket / outcome updates
# =============================================================================

class TestPlanMutations:

    def test_update_plan_status(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_status(sample_plan.id, PlanStatus.EXPIRED.value)
        assert snow_db.get_plan(sample_plan.id)["status"] == "expired"

    def test_update_plan_trade_ticket_stamps_entered_at(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_trade_ticket(sample_plan.id, 123456789)
        row = snow_db.get_plan(sample_plan.id)
        assert row["trade_ticket"] == 123456789
        assert row["entered_at"] is not None
        assert row["entered_at"].endswith("Z")  # Rule 22 — UTC suffix

    def test_update_plan_outcome_stamps_closed_at(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_outcome(sample_plan.id, 45.0, 225.30)
        row = snow_db.get_plan(sample_plan.id)
        assert row["outcome_pips"] == 45.0
        assert row["outcome_usd"] == 225.30
        assert row["status"] == PlanStatus.CLOSED.value
        assert row["closed_at"] is not None
        assert row["closed_at"].endswith("Z")

    def test_update_plan_last_evaluated_stamps_utc(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_last_evaluated(sample_plan.id)
        row = snow_db.get_plan(sample_plan.id)
        assert row["last_evaluated_at"] is not None
        assert row["last_evaluated_at"].endswith("Z")


# =============================================================================
# Active-plan reload / listing
# =============================================================================

class TestListPlans:

    def _insert_n_plans(self, sample_plan, count: int) -> list[Plan]:
        """Clone the sample plan with unique ids + stagger created_at."""
        plans = []
        for i in range(count):
            data = sample_plan.model_dump()
            # Use a patched timestamp so ORDER BY created_at DESC is meaningful
            data["id"] = f"PLAN-20260424-{i+1:03d}"
            data["created_at"] = f"2026-04-24T08:{i:02d}:00Z"
            p = Plan(**data)
            snow_db.insert_plan(p)
            plans.append(p)
        return plans

    def test_get_active_plans_excludes_terminal(
        self, snow_conn, sample_plan
    ):
        plans = self._insert_n_plans(sample_plan, 3)
        # Close one, expire one, leave one pending
        snow_db.update_plan_outcome(plans[0].id, 10.0, 50.0)
        snow_db.update_plan_status(plans[1].id, PlanStatus.EXPIRED.value)
        active = snow_db.get_active_plans()
        assert len(active) == 1
        assert active[0]["id"] == plans[2].id

    def test_list_plans_by_status_filter(self, snow_conn, sample_plan):
        plans = self._insert_n_plans(sample_plan, 3)
        snow_db.update_plan_status(plans[0].id, PlanStatus.ACTIVE.value)
        snow_db.update_plan_status(plans[1].id, PlanStatus.ACTIVE.value)
        rows = snow_db.list_plans_by_status([PlanStatus.ACTIVE.value])
        assert len(rows) == 2
        assert {r["id"] for r in rows} == {plans[0].id, plans[1].id}

    def test_list_plans_empty_statuses_returns_empty(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        assert snow_db.list_plans_by_status([]) == []


# =============================================================================
# I7 — partial-UNIQUE on trade_ticket (4-case matrix per advisor)
# =============================================================================

class TestI7LiveTicketUnique:
    """
    Partial index on snow_plans(trade_ticket)
        WHERE trade_ticket IS NOT NULL
          AND status IN ('triggered', 'active', 'closing')
    Must enforce: at most ONE live plan per ticket.
    """

    def _fresh_plan(self, sample_plan, plan_id_suffix: str) -> Plan:
        data = sample_plan.model_dump()
        data["id"] = f"PLAN-20260424-{plan_id_suffix}"
        return Plan(**data)

    def test_case1_different_tickets_both_active_ok(
        self, snow_conn, sample_plan
    ):
        p1 = self._fresh_plan(sample_plan, "001")
        p2 = self._fresh_plan(sample_plan, "002")
        snow_db.insert_plan(p1)
        snow_db.insert_plan(p2)
        snow_db.update_plan_trade_ticket(p1.id, 111)
        snow_db.update_plan_trade_ticket(p2.id, 222)
        snow_db.update_plan_status(p1.id, PlanStatus.ACTIVE.value)
        snow_db.update_plan_status(p2.id, PlanStatus.ACTIVE.value)
        # No integrity violation — different tickets
        assert snow_db.get_plan(p1.id)["trade_ticket"] == 111
        assert snow_db.get_plan(p2.id)["trade_ticket"] == 222

    def test_case2_same_ticket_pending_plus_active_ok(
        self, snow_conn, sample_plan
    ):
        # PENDING is excluded from the partial-unique index → this is allowed.
        # Useful during entry transitions where a stale PENDING clone exists.
        p1 = self._fresh_plan(sample_plan, "001")
        p2 = self._fresh_plan(sample_plan, "002")
        snow_db.insert_plan(p1)
        snow_db.insert_plan(p2)
        # p1 stays PENDING; its trade_ticket stays NULL (PENDING has no ticket),
        # but we manually write the ticket via raw SQL to make the case crisp.
        conn = snow_db._connect()
        try:
            conn.execute(
                "UPDATE snow_plans SET trade_ticket = 555 WHERE id = ?",
                (p1.id,),
            )
            conn.commit()
        finally:
            conn.close()
        snow_db.update_plan_trade_ticket(p2.id, 555)
        snow_db.update_plan_status(p2.id, PlanStatus.ACTIVE.value)
        # Both carry ticket 555 but p1.status='pending' is excluded from the
        # partial-UNIQUE, so this must succeed.
        assert snow_db.get_plan(p1.id)["trade_ticket"] == 555
        assert snow_db.get_plan(p2.id)["trade_ticket"] == 555

    def test_case3_same_ticket_two_active_rejected(
        self, snow_conn, sample_plan
    ):
        p1 = self._fresh_plan(sample_plan, "001")
        p2 = self._fresh_plan(sample_plan, "002")
        snow_db.insert_plan(p1)
        snow_db.insert_plan(p2)
        snow_db.update_plan_trade_ticket(p1.id, 777)
        snow_db.update_plan_status(p1.id, PlanStatus.ACTIVE.value)
        snow_db.update_plan_trade_ticket(p2.id, 777)
        with pytest.raises(sqlite3.IntegrityError):
            snow_db.update_plan_status(p2.id, PlanStatus.ACTIVE.value)

    def test_case4_active_to_closed_then_ticket_reused_ok(
        self, snow_conn, sample_plan
    ):
        # After the first plan closes, the partial-UNIQUE no longer covers it
        # (status='closed' excluded) → a new plan can pick up the same ticket.
        p1 = self._fresh_plan(sample_plan, "001")
        p2 = self._fresh_plan(sample_plan, "002")
        snow_db.insert_plan(p1)
        snow_db.insert_plan(p2)
        snow_db.update_plan_trade_ticket(p1.id, 888)
        snow_db.update_plan_status(p1.id, PlanStatus.ACTIVE.value)
        snow_db.update_plan_outcome(p1.id, 0.0, 0.0)  # → CLOSED
        snow_db.update_plan_trade_ticket(p2.id, 888)
        snow_db.update_plan_status(p2.id, PlanStatus.ACTIVE.value)
        assert snow_db.get_plan(p2.id)["status"] == "active"


# =============================================================================
# FK enforcement regression trap
# =============================================================================

class TestForeignKeyEnforcement:
    """If anyone future-edits `_connect()` and drops PRAGMA foreign_keys=ON,
    these fail loudly instead of silently losing referential integrity."""

    def test_trigger_with_unknown_plan_rejected(self, snow_conn):
        with pytest.raises(sqlite3.IntegrityError):
            snow_db.record_trigger(
                plan_id="PLAN-DOES-NOT-EXIST-001",
                contingency_name="x",
                contingency_kind="entry",
                action_type="execute_market",
                execution_status="success",
            )

    def test_evaluation_with_unknown_plan_rejected(self, snow_conn):
        with pytest.raises(sqlite3.IntegrityError):
            snow_db.record_evaluation(
                plan_id="PLAN-DOES-NOT-EXIST-001",
                contingency_name="x",
                event="armed",
            )


# =============================================================================
# Triggers
# =============================================================================

class TestTriggers:

    def test_record_and_list_trigger(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        tid = snow_db.record_trigger(
            plan_id=sample_plan.id,
            contingency_name="lock_10_at_support",
            contingency_kind="management",
            action_type="move_sl_to_price",
            execution_status="success",
            action_params={"price": 4727.0},
            execution_result={"new_sl": 4727.0},
            cycle_duration_ms=42,
        )
        assert isinstance(tid, int) and tid > 0
        rows = snow_db.list_triggers(sample_plan.id)
        assert len(rows) == 1
        row = rows[0]
        assert row["contingency_name"] == "lock_10_at_support"
        assert row["fired_at"].endswith("Z")
        assert json.loads(row["action_params"]) == {"price": 4727.0}
        assert json.loads(row["execution_result"]) == {"new_sl": 4727.0}

    def test_list_triggers_orders_newest_first(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        for i in range(3):
            snow_db.record_trigger(
                plan_id=sample_plan.id,
                contingency_name=f"c{i}",
                contingency_kind="management",
                action_type="move_sl_to_price",
                execution_status="success",
            )
        rows = snow_db.list_triggers(sample_plan.id)
        assert [r["contingency_name"] for r in rows] == ["c2", "c1", "c0"]

    def test_null_json_fields_stored_as_null(
        self, snow_conn, sample_plan
    ):
        snow_db.insert_plan(sample_plan)
        snow_db.record_trigger(
            plan_id=sample_plan.id,
            contingency_name="c",
            contingency_kind="exit",
            action_type="close_full",
            execution_status="success",
        )
        row = snow_db.list_triggers(sample_plan.id)[0]
        assert row["action_params"] is None
        assert row["execution_result"] is None


# =============================================================================
# Evaluations
# =============================================================================

class TestEvaluations:

    def test_record_evaluation(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        eid = snow_db.record_evaluation(
            plan_id=sample_plan.id,
            contingency_name="rejection_exit",
            event="armed",
            conditions_snapshot={"price_above_4733": True},
        )
        assert isinstance(eid, int) and eid > 0
        assert _count_rows(snow_conn, "snow_evaluations") == 1


# =============================================================================
# Atomic compound op (invariant I6)
# =============================================================================

class TestAtomicCompound:

    def test_success_writes_both_rows(self, snow_conn, sample_plan):
        snow_db.insert_plan(sample_plan)
        tid = snow_db.record_trigger_and_transition(
            sample_plan.id,
            contingency_name="entry",
            contingency_kind="entry",
            action_type="execute_market",
            execution_status="success",
            new_plan_status=PlanStatus.ACTIVE.value,
            trade_ticket=9876,
        )
        assert tid > 0
        plan_row = snow_db.get_plan(sample_plan.id)
        assert plan_row["status"] == PlanStatus.ACTIVE.value
        assert plan_row["trade_ticket"] == 9876
        assert plan_row["entered_at"] is not None
        assert _count_rows(snow_conn, "snow_triggers") == 1

    def test_rollback_on_mid_transaction_failure(
        self, snow_conn, sample_plan, monkeypatch
    ):
        """Simulate failure on the UPDATE half — trigger row must not persist.

        Python 3.12 marks `sqlite3.Connection.execute` read-only, so we wrap
        the connection in a forwarding proxy that intercepts `execute()`
        and raises on the UPDATE statement, while still delegating
        `commit`, `rollback`, `close`, etc. to the underlying connection.
        """
        snow_db.insert_plan(sample_plan)

        real_connect = snow_db._connect

        class _FlakyConn:
            def __init__(self, real):
                self._real = real

            def execute(self, sql, params=()):
                if sql.strip().startswith("UPDATE snow_plans"):
                    raise sqlite3.OperationalError("simulated mid-tx failure")
                return self._real.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._real, name)

        def _flaky_connect():
            return _FlakyConn(real_connect())

        monkeypatch.setattr(snow_db, "_connect", _flaky_connect)

        with pytest.raises(sqlite3.OperationalError):
            snow_db.record_trigger_and_transition(
                sample_plan.id,
                contingency_name="entry",
                contingency_kind="entry",
                action_type="execute_market",
                execution_status="success",
                new_plan_status=PlanStatus.ACTIVE.value,
                trade_ticket=1111,
            )

        # Restore real _connect and verify DB state
        monkeypatch.setattr(snow_db, "_connect", real_connect)
        assert _count_rows(snow_conn, "snow_triggers") == 0
        plan_row = snow_db.get_plan(sample_plan.id)
        assert plan_row["status"] == PlanStatus.PENDING.value
        assert plan_row["trade_ticket"] is None

    def test_without_trade_ticket_updates_status_only(
        self, snow_conn, sample_plan
    ):
        # e.g. exit-fired path — no ticket change, just status transition
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_trade_ticket(sample_plan.id, 4242)
        snow_db.update_plan_status(sample_plan.id, PlanStatus.ACTIVE.value)
        snow_db.record_trigger_and_transition(
            sample_plan.id,
            contingency_name="rejection_exit",
            contingency_kind="exit",
            action_type="close_full",
            execution_status="success",
            new_plan_status=PlanStatus.CLOSING.value,
        )
        row = snow_db.get_plan(sample_plan.id)
        assert row["status"] == PlanStatus.CLOSING.value
        # Ticket preserved from the earlier attach — not clobbered
        assert row["trade_ticket"] == 4242


# =============================================================================
# Performance — CTO item 2: verify partial index is actually used
# =============================================================================

class TestIndexUsage:
    """The Snow loop calls `get_active_plans()` every 5 s. SQLite must use
    the `idx_snow_plans_status` index, not a sequential scan, for that query
    to stay cheap as the table grows."""

    def test_active_plans_query_uses_index(self, snow_conn):
        conn = sqlite3.connect(str(snow_conn))
        try:
            plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM snow_plans
                 WHERE status IN ('pending','triggered','active','closing')
                 ORDER BY created_at DESC
                 LIMIT 10000
                """
            ).fetchall()
        finally:
            conn.close()
        plan_text = " | ".join(str(r[-1]) for r in plan)
        # SQLite's planner description should mention the status index OR
        # the partial expires index. "SCAN snow_plans" alone (no USING) is
        # the failure signal — means full sequential scan.
        assert "USING INDEX" in plan_text.upper() or \
               "SEARCH" in plan_text.upper(), (
            f"Expected index usage for active-plans query, got: {plan_text}"
        )

    def test_live_ticket_lookup_uses_unique_index(
        self, snow_conn, sample_plan
    ):
        """Recovery / duplicate-ticket defense queries by trade_ticket on
        live plans. The partial-UNIQUE index idx_snow_plans_live_ticket
        should cover this."""
        snow_db.insert_plan(sample_plan)
        snow_db.update_plan_trade_ticket(sample_plan.id, 999)
        snow_db.update_plan_status(sample_plan.id, PlanStatus.ACTIVE.value)
        conn = sqlite3.connect(str(snow_conn))
        try:
            plan = conn.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM snow_plans
                 WHERE trade_ticket = 999
                   AND status IN ('triggered','active','closing')
                """
            ).fetchall()
        finally:
            conn.close()
        plan_text = " | ".join(str(r[-1]) for r in plan)
        assert "idx_snow_plans_live_ticket" in plan_text or \
               "USING INDEX" in plan_text.upper(), (
            f"Expected live-ticket index usage, got: {plan_text}"
        )


# =============================================================================
# Concurrency — CTO optional: two threads inserting simultaneously
# =============================================================================

class TestConcurrentInserts:
    """Basic WAL validation: two threads inserting different plans against
    the same DB file must both succeed without deadlock or corruption.
    This exercises the `timeout=5` busy-wait path inherited from
    db_writer._get_connection()."""

    def test_two_threads_insert_different_plans(
        self, snow_conn, sample_plan
    ):
        errors: list[BaseException] = []

        def _insert(plan_id: str, created_min: int):
            try:
                data = sample_plan.model_dump()
                data["id"] = plan_id
                data["created_at"] = f"2026-04-24T08:{created_min:02d}:00Z"
                snow_db.insert_plan(Plan(**data))
            except BaseException as e:
                errors.append(e)

        t1 = threading.Thread(
            target=_insert, args=("PLAN-20260424-101", 10)
        )
        t2 = threading.Thread(
            target=_insert, args=("PLAN-20260424-102", 11)
        )
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)
        assert not errors, f"Concurrent insert failures: {errors}"
        assert _count_rows(snow_conn, "snow_plans") == 2
        assert snow_db.get_plan("PLAN-20260424-101") is not None
        assert snow_db.get_plan("PLAN-20260424-102") is not None
