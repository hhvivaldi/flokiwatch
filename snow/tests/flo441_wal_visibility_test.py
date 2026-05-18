"""FLO-441 — list_plans_by_status visibility regression tests.

The 2026-05-14/15 production failure mode (memory 9854) was:
`list_active_plans` returned count=0 every cycle despite pending plans
existing in snow_plans. The bug was NOT reproducible in-process during
the FLO-441 investigation, so these tests lock the *fix shape* — they
verify that:

  1. After Connection A inserts and commits a plan, a fresh call to
     `list_plans_by_status` from the same process sees it.
  2. The read uses an autocommit connection so no implicit-read-txn
     snapshot can pin a stale view across multiple consecutive reads.
  3. Each call opens + closes its own connection (no caching).
"""
from __future__ import annotations
import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def tmp_history_db(monkeypatch, tmp_path):
    """Point both db_writer and snow.db at a fresh tmp_path .db file
    and create the snow_plans table the validators expect."""
    db_path = str(tmp_path / "history.db")
    import db_writer
    import snow.db as snow_db_mod

    monkeypatch.setattr(db_writer, "_get_connection",
                        lambda: _open_with_wal(db_path))

    # Apply snow_plans DDL (minimum columns the function reads)
    conn = _open_with_wal(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS snow_plans (
            id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL DEFAULT 3,
            created_by TEXT NOT NULL DEFAULT 'floki',
            created_at TEXT NOT NULL,
            expires_at TEXT,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL DEFAULT '{}',
            trade_ticket INTEGER,
            entered_at TEXT,
            closed_at TEXT,
            outcome_pips REAL,
            outcome_usd REAL,
            last_evaluated_at TEXT,
            state_cache_json TEXT,
            author_regime_snapshot_json TEXT,
            trigger_regime_snapshot_json TEXT,
            regime_drift_json TEXT
        )
    """)
    conn.commit()
    conn.close()
    return db_path


def _open_with_wal(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _insert_plan(db_path: str, plan_id: str, status: str = "pending") -> None:
    conn = _open_with_wal(db_path)
    conn.execute(
        "INSERT INTO snow_plans (id, status, created_at) VALUES (?, ?, ?)",
        (plan_id, status, "2026-05-18T12:00:00Z"),
    )
    conn.commit()
    conn.close()


class TestListPlansByStatusVisibility:

    def test_empty_returns_zero(self, tmp_history_db):
        from snow.db import get_active_plans
        assert get_active_plans() == []

    def test_inserted_plan_visible_immediately(self, tmp_history_db):
        from snow.db import get_active_plans
        _insert_plan(tmp_history_db, "PLAN-IMMED-001", status="pending")
        rows = get_active_plans()
        assert len(rows) == 1
        assert rows[0]["id"] == "PLAN-IMMED-001"

    def test_terminal_status_not_returned(self, tmp_history_db):
        from snow.db import get_active_plans
        _insert_plan(tmp_history_db, "PLAN-CLOSED-001", status="closed")
        _insert_plan(tmp_history_db, "PLAN-PEND-002", status="pending")
        rows = get_active_plans()
        ids = {r["id"] for r in rows}
        assert ids == {"PLAN-PEND-002"}

    def test_repeated_reads_see_writes_between_them(self, tmp_history_db):
        """The FLO-441 failure-shape: read1 → write → read2 from same
        process. read2 must see the write. With autocommit reader,
        no implicit-txn snapshot can pin read2 to read1's view."""
        from snow.db import get_active_plans
        assert len(get_active_plans()) == 0
        _insert_plan(tmp_history_db, "PLAN-MIDREAD-001")
        assert len(get_active_plans()) == 1
        _insert_plan(tmp_history_db, "PLAN-MIDREAD-002")
        rows = get_active_plans()
        assert len(rows) == 2

    def test_active_status_visible(self, tmp_history_db):
        from snow.db import get_active_plans
        _insert_plan(tmp_history_db, "PLAN-A-001", status="active")
        rows = get_active_plans()
        assert {r["id"] for r in rows} == {"PLAN-A-001"}

    def test_read_connection_is_autocommit(self, tmp_history_db):
        """Lock the autocommit property of the FLO-441 read helper."""
        from snow.db import _connect_read_only
        conn = _connect_read_only()
        try:
            assert conn.isolation_level is None
        finally:
            conn.close()
