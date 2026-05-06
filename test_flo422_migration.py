"""FLO-422 Step 2 — migration tests for snow/db.py _ADDITIVE_COLUMNS.

Verifies the three new TEXT columns (author_regime_snapshot_json,
trigger_regime_snapshot_json, regime_drift_json) are added correctly,
the migration is idempotent across runs, partial state is completed,
non-duplicate-column errors propagate, and pre-existing rows get NULL
for the new columns.

In-memory SQLite only — does NOT touch data/history.db.

Run: python test_flo422_migration.py
Exits non-zero on failure.
"""
from __future__ import annotations

import sqlite3
import sys

from snow.db import _apply_additive_migrations


class _FakeConn:
    """Minimal stand-in for sqlite3.Connection — only needs .execute().
    Used to inject errors that real Connection rejects (its execute attr
    is read-only and cannot be monkey-patched)."""
    def __init__(self, raise_on_execute):
        self._raise = raise_on_execute

    def execute(self, *_args, **_kwargs):
        raise self._raise


FLO422_COLS = (
    "author_regime_snapshot_json",
    "trigger_regime_snapshot_json",
    "regime_drift_json",
)


def _fresh_conn() -> sqlite3.Connection:
    """Create an in-memory snow_plans table with a minimal schema mirroring
    snow/db.py's CREATE statement. Only includes pre-FLO-422 columns —
    state_cache_json is added via the existing migration path so we test
    the full chain."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE snow_plans (
            id                TEXT PRIMARY KEY,
            schema_version    INTEGER NOT NULL,
            created_by        TEXT NOT NULL,
            created_at        TEXT NOT NULL,
            expires_at        TEXT,
            status            TEXT NOT NULL,
            plan_json         TEXT NOT NULL,
            trade_ticket      INTEGER,
            entered_at        TEXT,
            closed_at         TEXT,
            outcome_pips      REAL,
            outcome_usd       REAL,
            last_evaluated_at TEXT
        )
        """
    )
    return conn


def _column_set(conn: sqlite3.Connection) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(snow_plans)").fetchall()}


def fail(label: str, msg: str) -> None:
    print(f"FAIL [{label}]: {msg}")
    sys.exit(1)


def passed(label: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"PASS [{label}]{suffix}")


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_1_columns_added_on_fresh_db():
    conn = _fresh_conn()
    _apply_additive_migrations(conn)
    cols = _column_set(conn)
    for c in FLO422_COLS:
        if c not in cols:
            fail("test1.column_present", f"{c} missing after migration; got {cols}")
    # state_cache_json (FLO-359 precedent) must also be present
    if "state_cache_json" not in cols:
        fail("test1.state_cache_added", "state_cache_json missing — FLO-359 precedent regressed")
    passed("test1.fresh_db_all_columns_added", f"cols={sorted(cols)}")


def test_2_idempotent_on_rerun():
    conn = _fresh_conn()
    _apply_additive_migrations(conn)
    cols_first = _column_set(conn)
    # Re-run; must not raise, must not add duplicates
    _apply_additive_migrations(conn)
    _apply_additive_migrations(conn)
    cols_third = _column_set(conn)
    if cols_first != cols_third:
        fail("test2.column_set_stable", f"first={cols_first} third={cols_third}")
    passed("test2.idempotent_across_3_runs")


def test_3_partial_state_completed():
    """If one column was somehow added out-of-band, the migration should
    complete the remaining two without raising."""
    conn = _fresh_conn()
    conn.execute("ALTER TABLE snow_plans ADD COLUMN author_regime_snapshot_json TEXT")
    cols_before = _column_set(conn)
    if "author_regime_snapshot_json" not in cols_before:
        fail("test3.precondition", "manual ALTER did not add column")
    _apply_additive_migrations(conn)
    cols_after = _column_set(conn)
    for c in FLO422_COLS:
        if c not in cols_after:
            fail("test3.completion", f"{c} missing after migration completed partial state")
    passed("test3.partial_state_completed")


def test_4_non_duplicate_errors_propagate():
    """A non-duplicate-column OperationalError must re-raise, not be silently
    swallowed. Use a fake conn since sqlite3.Connection.execute is read-only."""
    fake = _FakeConn(sqlite3.OperationalError("near \"SYNTAX\": syntax error"))
    try:
        _apply_additive_migrations(fake)
    except sqlite3.OperationalError as e:
        if "syntax" in str(e).lower():
            passed("test4.non_duplicate_propagates", str(e))
            return
        fail("test4.wrong_exception", f"unexpected: {e}")
    fail("test4.no_exception", "non-duplicate error was swallowed")


def test_5_pre_existing_rows_get_null():
    """Rows that exist before the migration must show NULL for the new columns."""
    conn = _fresh_conn()
    conn.execute(
        "INSERT INTO snow_plans (id, schema_version, created_by, created_at, status, plan_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("PLAN-TEST-001", 3, "test", "2026-05-06T00:00:00Z", "pending", "{}"),
    )
    _apply_additive_migrations(conn)
    row = conn.execute(
        "SELECT author_regime_snapshot_json, trigger_regime_snapshot_json, regime_drift_json "
        "FROM snow_plans WHERE id = ?",
        ("PLAN-TEST-001",),
    ).fetchone()
    if row != (None, None, None):
        fail("test5.pre_existing_null", f"expected (None,None,None), got {row}")
    passed("test5.pre_existing_rows_nullable")


def test_6_columns_are_text_and_nullable():
    """Verify the column type is TEXT and that NULL inserts succeed."""
    conn = _fresh_conn()
    _apply_additive_migrations(conn)
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(snow_plans)").fetchall()}
    for c in FLO422_COLS:
        col_info = info.get(c)
        if col_info is None:
            fail("test6.column_present", f"{c} missing")
        # PRAGMA table_info returns: cid, name, type, notnull, dflt_value, pk
        col_type = col_info[2].upper()
        notnull = col_info[3]
        if col_type != "TEXT":
            fail("test6.type", f"{c} type {col_type!r} is not TEXT")
        if notnull:
            fail("test6.nullable", f"{c} is NOT NULL — should be nullable")
    # Insert a row with explicit NULLs in new columns
    conn.execute(
        "INSERT INTO snow_plans (id, schema_version, created_by, created_at, status, plan_json, "
        "author_regime_snapshot_json, trigger_regime_snapshot_json, regime_drift_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("PLAN-TEST-002", 3, "test", "2026-05-06T00:00:00Z", "pending", "{}", None, None, None),
    )
    passed("test6.text_and_nullable")


def test_7_round_trip_json():
    """Sanity: insert a JSON blob, read it back unchanged."""
    conn = _fresh_conn()
    _apply_additive_migrations(conn)
    payload = '{"stage":"author","ts":"2026-05-06T13:11:27Z","impulse_total_60m":4}'
    conn.execute(
        "INSERT INTO snow_plans (id, schema_version, created_by, created_at, status, plan_json, author_regime_snapshot_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("PLAN-TEST-003", 3, "test", "2026-05-06T00:00:00Z", "pending", "{}", payload),
    )
    out = conn.execute(
        "SELECT author_regime_snapshot_json FROM snow_plans WHERE id = ?",
        ("PLAN-TEST-003",),
    ).fetchone()[0]
    if out != payload:
        fail("test7.json_round_trip", f"expected {payload!r}, got {out!r}")
    passed("test7.json_round_trip")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-422 Step 2 — migration test suite")
    print("=" * 60)
    test_1_columns_added_on_fresh_db()
    test_2_idempotent_on_rerun()
    test_3_partial_state_completed()
    test_4_non_duplicate_errors_propagate()
    test_5_pre_existing_rows_get_null()
    test_6_columns_are_text_and_nullable()
    test_7_round_trip_json()
    print("=" * 60)
    print("ALL TESTS PASSED")
