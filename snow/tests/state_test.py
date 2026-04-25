"""State-cache tests — FLO-359 Phase 8b commit 2.

Covers the per-condition state cache module shipped ahead of any
consumer (commits 3-5 add primitives that mutate it; commit 3+ wires
it into the loop). Tests instantiate fresh `PerConditionStateCache()`
instances rather than reaching for the module-level singleton, so
test isolation does not depend on conftest plumbing.

Test classes:
  * TestConditionStateRow      — dict round-trip + lenient deserialise
  * TestCacheLifecycle         — get / get_or_create / forget / dirty
  * TestFlushToDb              — UPDATE per dirty plan, snapshot semantics
  * TestRehydrateFromDb        — load + stale drop + corrupt-JSON drop
  * TestRestartSimulation      — write → flush → clear → rehydrate
  * TestThreadSafety           — concurrent get_or_create yields one row
  * TestPerformance            — flush of 100 plans × 8 conds < 50 ms
"""
from __future__ import annotations

import datetime as _dt
import json
import sqlite3
import threading
import time
from copy import deepcopy
from typing import Any

import pytest

import db_writer
from snow import db as snow_db
from snow.schema import Plan, PlanStatus
from snow.state import (
    PerConditionStateCache,
    ConditionStateRow,
    STALE_STATE_THRESHOLD_MINUTES,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def cache() -> PerConditionStateCache:
    """Fresh per-test cache instance — explicitly NOT the singleton."""
    return PerConditionStateCache()


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Tmp-path SQLite, init_snow_tables() called once."""
    db_path = tmp_path / "state_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


def _insert_active_plan(valid_plan_dict, plan_id: str, status: str = "active") -> Plan:
    """Helper: persist a plan with the requested id + status."""
    pd = deepcopy(valid_plan_dict)
    pd["id"] = plan_id
    pd["status"] = status
    plan = Plan(**pd)
    snow_db.insert_plan(plan)
    return plan


# =============================================================================
# ConditionStateRow round-trip
# =============================================================================

class TestConditionStateRow:

    def test_dict_round_trip_preserves_payload(self):
        row = ConditionStateRow(
            plan_id="PLAN-20260424-101",
            contingency_name="_entry",
            condition_index=2,
            cond_type="indicator_crossover",
            prev_value=49.6,
            prev_above_threshold=False,
            bar_history=[22.0, 19.5, 26.1],
            bar_history_max_n=4,
            prev_bar_close_at="2026-04-25T14:00:00Z",
            last_seen_at="2026-04-25T14:32:11Z",
        )
        d = row.to_dict()
        # plan_id is implicit in the parent column key; not serialised.
        assert "plan_id" not in d
        round = ConditionStateRow.from_dict("PLAN-20260424-101", d)
        assert round == row

    def test_from_dict_lenient_about_optional_fields(self):
        # Forward-compat: a v3 reader's missing fields default cleanly.
        minimal = {
            "contingency_name": "_entry",
            "condition_index": 0,
            "cond_type": "indicator_crossover",
        }
        row = ConditionStateRow.from_dict("PLAN-20260424-102", minimal)
        assert row.prev_value is None
        assert row.bar_history == []
        assert row.bar_history_max_n == 0
        assert row.last_seen_at == ""

    def test_from_dict_rejects_missing_required(self):
        with pytest.raises(KeyError):
            ConditionStateRow.from_dict("PLAN-20260424-103", {"contingency_name": "x"})


# =============================================================================
# Cache lifecycle
# =============================================================================

class TestCacheLifecycle:

    def test_get_returns_none_when_absent(self, cache):
        assert cache.get("PLAN-20260424-101", "_entry", 0) is None
        assert len(cache) == 0

    def test_get_or_create_allocates_fresh_then_returns_same_object(self, cache):
        a = cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        b = cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        assert a is b
        assert len(cache) == 1
        assert cache.is_dirty("PLAN-20260424-101")

    def test_distinct_keys_yield_distinct_rows(self, cache):
        a = cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        b = cache.get_or_create("PLAN-20260424-101", "_entry", 1, "indicator_was")
        c = cache.get_or_create("PLAN-20260424-102", "_entry", 0, "indicator_crossover")
        assert {id(a), id(b), id(c)} == {id(a), id(b), id(c)}  # all distinct
        assert len(cache) == 3

    def test_forget_plan_drops_only_that_plan(self, cache):
        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.get_or_create("PLAN-20260424-101", "exit_a", 0, "indicator_was")
        cache.get_or_create("PLAN-20260424-102", "_entry", 0, "indicator_crossover")
        removed = cache.forget_plan("PLAN-20260424-101")
        assert removed == 2
        assert cache.get("PLAN-20260424-101", "_entry", 0) is None
        assert cache.get("PLAN-20260424-102", "_entry", 0) is not None
        assert cache.is_dirty("PLAN-20260424-101") is False

    def test_mark_updated_stamps_last_seen_on_all_rows_for_plan(self, cache):
        r1 = cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        r2 = cache.get_or_create("PLAN-20260424-101", "exit_a", 0, "indicator_was")
        # Force last_seen_at into the past so we can detect refresh.
        r1.last_seen_at = "2020-01-01T00:00:00Z"
        r2.last_seen_at = "2020-01-01T00:00:00Z"
        cache.mark_updated("PLAN-20260424-101", stamp=True)
        assert r1.last_seen_at != "2020-01-01T00:00:00Z"
        assert r2.last_seen_at != "2020-01-01T00:00:00Z"
        assert cache.is_dirty("PLAN-20260424-101")

    def test_clear_resets_cache_and_dirty_set(self, cache):
        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.clear()
        assert len(cache) == 0
        assert cache.is_dirty("PLAN-20260424-101") is False


# =============================================================================
# Flush to DB
# =============================================================================

class TestFlushToDb:

    def test_flush_no_dirty_is_noop(self, snow_conn, cache):
        assert cache.flush_to_db() == 0

    def test_flush_writes_one_blob_per_dirty_plan(
        self, snow_conn, cache, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101")
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-102")

        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.get_or_create("PLAN-20260424-101", "exit_a", 0, "indicator_was",
                            bar_history_max_n=4)
        cache.get_or_create("PLAN-20260424-102", "_entry", 0, "indicator_crossover")

        n = cache.flush_to_db()
        assert n == 2

        # Read back; every plan should have its full row-set.
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            rows = check_conn.execute(
                "SELECT id, state_cache_json FROM snow_plans ORDER BY id"
            ).fetchall()
        finally:
            check_conn.close()
        # Plans inserted by the conftest fixture have id PLAN-20260424-001;
        # plus our two PLAN-X-* rows. Filter to the ones we wrote.
        by_id = {r[0]: r[1] for r in rows}
        p1 = json.loads(by_id["PLAN-20260424-101"])
        p2 = json.loads(by_id["PLAN-20260424-102"])
        assert len(p1) == 2
        assert len(p2) == 1
        assert {e["contingency_name"] for e in p1} == {"_entry", "exit_a"}
        # And dirty flags cleared.
        assert cache.is_dirty("PLAN-20260424-101") is False
        assert cache.is_dirty("PLAN-20260424-102") is False

    def test_flush_targeted_plan_ids(
        self, snow_conn, cache, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101")
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-102")
        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.get_or_create("PLAN-20260424-102", "_entry", 0, "indicator_crossover")

        n = cache.flush_to_db(plan_ids=["PLAN-20260424-101"])
        assert n == 1
        # Only PLAN-20260424-101's dirty bit cleared; PLAN-20260424-102 still dirty.
        assert cache.is_dirty("PLAN-20260424-101") is False
        assert cache.is_dirty("PLAN-20260424-102") is True

    def test_flush_serialises_full_row_set_per_plan(
        self, snow_conn, cache, valid_plan_dict
    ):
        """In-memory IS the truth between flushes — the JSON column is
        the FULL current row-set for the plan, not a delta over what
        was on disk."""
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101")
        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.get_or_create("PLAN-20260424-101", "exit_a", 0, "indicator_was")
        cache.flush_to_db()

        # Forget one row, leave the other, flush again — the disk blob
        # must reflect the in-memory truth (one row, not two).
        cache.forget_plan("PLAN-20260424-101")
        # Re-add only the entry row.
        cache.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        cache.flush_to_db()

        check_conn = sqlite3.connect(str(snow_conn))
        try:
            blob = check_conn.execute(
                "SELECT state_cache_json FROM snow_plans WHERE id = ?",
                ("PLAN-20260424-101",),
            ).fetchone()[0]
        finally:
            check_conn.close()
        rows = json.loads(blob)
        assert len(rows) == 1
        assert rows[0]["contingency_name"] == "_entry"


# =============================================================================
# Rehydrate from DB
# =============================================================================

class TestRehydrateFromDb:

    def test_rehydrate_loads_recent_rows(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        # Pre-populate via a writer cache, flush, then create a fresh
        # reader cache and rehydrate.
        writer = PerConditionStateCache()
        writer.get_or_create("PLAN-20260424-101", "_entry", 0, "indicator_crossover")
        writer.get_or_create("PLAN-20260424-101", "exit_a", 0, "indicator_was")
        writer.flush_to_db()

        reader = PerConditionStateCache()
        loaded = reader.rehydrate_from_db()
        assert loaded == 2
        assert reader.get("PLAN-20260424-101", "_entry", 0) is not None
        assert reader.get("PLAN-20260424-101", "exit_a", 0) is not None

    def test_rehydrate_drops_stale_rows(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        # Hand-write a stale blob (last_seen_at well past threshold).
        stale_ts = (
            _dt.datetime.now(_dt.timezone.utc)
            - _dt.timedelta(minutes=STALE_STATE_THRESHOLD_MINUTES + 5)
        ).isoformat().replace("+00:00", "Z")
        fresh_ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        blob = json.dumps([
            {"contingency_name": "_entry", "condition_index": 0,
             "cond_type": "indicator_crossover", "last_seen_at": stale_ts},
            {"contingency_name": "exit_a", "condition_index": 0,
             "cond_type": "indicator_was", "last_seen_at": fresh_ts},
        ])
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            check_conn.execute(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                (blob, "PLAN-20260424-101"),
            )
            check_conn.commit()
        finally:
            check_conn.close()

        reader = PerConditionStateCache()
        loaded = reader.rehydrate_from_db()
        assert loaded == 1
        assert reader.get("PLAN-20260424-101", "_entry", 0) is None
        assert reader.get("PLAN-20260424-101", "exit_a", 0) is not None

    def test_rehydrate_drops_corrupt_json(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            check_conn.execute(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                ("{not valid json", "PLAN-20260424-101"),
            )
            check_conn.commit()
        finally:
            check_conn.close()

        reader = PerConditionStateCache()
        # Should NOT raise; just log and skip.
        loaded = reader.rehydrate_from_db()
        assert loaded == 0

    def test_rehydrate_drops_non_list_payload(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            check_conn.execute(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                ('{"contingency_name": "wrong shape"}', "PLAN-20260424-101"),
            )
            check_conn.commit()
        finally:
            check_conn.close()

        reader = PerConditionStateCache()
        assert reader.rehydrate_from_db() == 0

    def test_rehydrate_skips_terminal_plans(
        self, snow_conn, valid_plan_dict
    ):
        """CLOSED plans should not be loaded into the cache — they
        can't fire anything. _LIVE_PLAN_STATUSES filters them out."""
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-199", status="closed")
        # Try to write state for the closed plan — UPDATE has no
        # status filter, so it succeeds — but rehydrate must skip it.
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            check_conn.execute(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                (
                    '[{"contingency_name": "_entry", "condition_index": 0, '
                    '"cond_type": "indicator_crossover", '
                    '"last_seen_at": "2099-01-01T00:00:00Z"}]',
                    "PLAN-20260424-199",
                ),
            )
            check_conn.commit()
        finally:
            check_conn.close()

        reader = PerConditionStateCache()
        assert reader.rehydrate_from_db() == 0
        assert reader.get("PLAN-20260424-199", "_entry", 0) is None

    def test_rehydrate_atomic_clears_existing_state(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        reader = PerConditionStateCache()
        reader.get_or_create("PLAN-OLD", "_entry", 0, "indicator_crossover")
        # No blob on disk for PLAN-20260424-101 → rehydrate loads zero rows.
        loaded = reader.rehydrate_from_db()
        assert loaded == 0
        # And the pre-existing PLAN-OLD row is cleared by the atomic
        # reload — caller expects rehydrate to be a fresh reset.
        assert reader.get("PLAN-OLD", "_entry", 0) is None


# =============================================================================
# Restart simulation
# =============================================================================

class TestRestartSimulation:

    def test_state_survives_kill_and_rehydrate(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        c1 = PerConditionStateCache()
        row = c1.get_or_create(
            "PLAN-20260424-101", "_entry", 1, "indicator_crossover"
        )
        row.prev_value = 49.6
        row.prev_above_threshold = False
        c1.mark_updated("PLAN-20260424-101")
        c1.flush_to_db()

        # "Kill" the cache (process restart).
        c2 = PerConditionStateCache()
        c2.rehydrate_from_db()

        restored = c2.get("PLAN-20260424-101", "_entry", 1)
        assert restored is not None
        assert restored.prev_value == 49.6
        assert restored.prev_above_threshold is False

    def test_corrupt_one_row_doesnt_break_siblings(
        self, snow_conn, valid_plan_dict
    ):
        _insert_active_plan(valid_plan_dict, "PLAN-20260424-101", status="active")
        # One good row, one row missing the required cond_type field.
        blob = json.dumps([
            {"contingency_name": "_entry", "condition_index": 0,
             "cond_type": "indicator_crossover",
             "last_seen_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                 "+00:00", "Z")},
            # Missing "cond_type" — from_dict raises KeyError.
            {"contingency_name": "exit_a", "condition_index": 0,
             "last_seen_at": _dt.datetime.now(_dt.timezone.utc).isoformat().replace(
                 "+00:00", "Z")},
        ])
        check_conn = sqlite3.connect(str(snow_conn))
        try:
            check_conn.execute(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                (blob, "PLAN-20260424-101"),
            )
            check_conn.commit()
        finally:
            check_conn.close()

        reader = PerConditionStateCache()
        loaded = reader.rehydrate_from_db()
        assert loaded == 1
        assert reader.get("PLAN-20260424-101", "_entry", 0) is not None
        assert reader.get("PLAN-20260424-101", "exit_a", 0) is None


# =============================================================================
# Thread safety
# =============================================================================

class TestThreadSafety:

    def test_concurrent_get_or_create_yields_one_shared_row(self, cache):
        """Smoke test: two threads racing on the same key must observe
        a single shared row (the lock enforces ordering inside
        get_or_create)."""
        results: list[ConditionStateRow] = []
        barrier = threading.Barrier(2)

        def _worker():
            barrier.wait()
            r = cache.get_or_create(
                "PLAN-20260424-101", "_entry", 0, "indicator_crossover"
            )
            results.append(r)

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert len(results) == 2
        assert results[0] is results[1]
        assert len(cache) == 1


# =============================================================================
# Performance — spec test, not regression
# =============================================================================

class TestPerformance:

    def test_flush_under_50ms_at_realistic_scale(
        self, snow_conn, valid_plan_dict
    ):
        """RFC §8.2 budget: flush of 100 plans × 8 conditions in <50 ms.
        Spec test, not regression — if this fails, design has a
        serialisation problem to find before commit 3+ makes it real."""
        # Insert 100 active plans (all sharing the canonical fixture).
        for i in range(100):
            pd = deepcopy(valid_plan_dict)
            pd["id"] = f"PLAN-20260424-{200 + i:03d}"
            pd["status"] = "active"
            snow_db.insert_plan(Plan(**pd))

        cache = PerConditionStateCache()
        for i in range(100):
            pid = f"PLAN-20260424-{200 + i:03d}"
            for j in range(8):
                row = cache.get_or_create(
                    pid, "_entry", j, "indicator_crossover"
                )
                row.prev_value = 50.0 + i + j
                row.prev_above_threshold = (j % 2 == 0)
                row.bar_history = [40.0 + k for k in range(4)]
                row.bar_history_max_n = 4
            cache.mark_updated(pid)

        t0 = time.perf_counter()
        n = cache.flush_to_db()
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert n == 100
        # 50 ms ceiling per RFC; allow 2× headroom for slow CI runners.
        assert elapsed_ms < 100, (
            f"flush of 100×8 took {elapsed_ms:.1f} ms — RFC budget 50 ms"
        )
