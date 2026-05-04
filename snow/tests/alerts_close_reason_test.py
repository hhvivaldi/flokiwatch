"""FLO-419 (CEO 2026-05-04) — alerts._lookup_close_reason coverage.

Tests for the helper that maps a closed broker ticket to the Snow
contingency name that fired the close, falling back to the MT5
deal reason string when no Snow trigger matched.

Empirical motivation: PLAN-009 alert showed "Expert Advisor" (raw MT5
DEAL_REASON_EXPERT) — useless for forensics. The actual closer was
either a Snow exit's close_full action OR the broker SL hit; the
contingency name is the operationally meaningful attribution.
"""
from __future__ import annotations

import sqlite3
import pytest

from snow import db as snow_db


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Fresh tmp SQLite per test, isolated from production. Patches
    BOTH snow.db._connect AND alerts._lookup_close_reason's own
    sqlite3.connect target so the helper queries the test DB."""
    db_path = tmp_path / "alerts_close_reason_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()

    # alerts._lookup_close_reason resolves DB path from
    # config.HISTORY_DB_PATH and opens its own sqlite3 connection.
    # Redirect that path to the tmp DB so the helper queries the
    # same isolated tables this fixture seeds.
    import config as _config
    monkeypatch.setattr(_config, "HISTORY_DB_PATH", str(db_path),
                        raising=False)
    return db_path


def _seed_plan(plan_id: str, trade_ticket: int):
    """Insert a minimal snow_plans row for the helper's JOIN."""
    conn = snow_db._connect()
    try:
        conn.execute(
            "INSERT INTO snow_plans "
            "(id, schema_version, created_by, created_at, status, "
            " plan_json, trade_ticket) "
            "VALUES (?, 3, 'floki', '2026-05-04T12:00:00Z', 'closed', "
            "        '{}', ?)",
            (plan_id, trade_ticket),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_trigger(plan_id: str, contingency_name: str,
                  action_type: str = "close_full",
                  execution_status: str = "success",
                  contingency_kind: str = "exit"):
    conn = snow_db._connect()
    try:
        conn.execute(
            "INSERT INTO snow_triggers "
            "(plan_id, contingency_name, contingency_kind, fired_at, "
            " action_type, execution_status) "
            "VALUES (?, ?, ?, '2026-05-04T12:30:00Z', ?, ?)",
            (plan_id, contingency_name, contingency_kind,
             action_type, execution_status),
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Tests
# =============================================================================


class TestCloseReasonLookup:

    def test_close_reason_uses_snow_contingency_when_present(
        self, snow_conn,
    ):
        """A close_full trigger from a named Snow exit contingency
        produces 'Snow: <contingency_name>', overriding the MT5
        fallback string."""
        _seed_plan("PLAN-20260504-XX1", trade_ticket=9000001)
        _seed_trigger("PLAN-20260504-XX1",
                      contingency_name="give_back_protection")

        from alerts import _lookup_close_reason
        result = _lookup_close_reason(9000001, fallback="Expert Advisor")
        assert result == "Snow: give_back_protection"

    def test_close_reason_falls_back_when_no_snow_trigger(
        self, snow_conn,
    ):
        """Plan exists with the matching ticket but NO close_full /
        close_partial trigger fired. Helper returns the fallback
        unchanged (broker SL/TP, manual close, etc.)."""
        _seed_plan("PLAN-20260504-XX2", trade_ticket=9000002)
        # Only an entry trigger, no close trigger — common shape for
        # broker-SL closes (Snow's _entry fires on entry; broker hits
        # SL before any exit contingency arms).
        _seed_trigger("PLAN-20260504-XX2",
                      contingency_name="_entry",
                      action_type="execute_market",
                      contingency_kind="entry")

        from alerts import _lookup_close_reason
        result = _lookup_close_reason(9000002, fallback="Stop Loss")
        assert result == "Stop Loss"

    def test_close_reason_ignores_user_cancel_contingency(
        self, snow_conn,
    ):
        """`_user_cancel` is the operator-driven cancel path
        (agent_tools.cancel_plan / data/_audits operator scripts).
        Cancels are NOT closes — they cancel pending plans before
        entry. The helper must NOT dress these up as Snow closes."""
        _seed_plan("PLAN-20260504-XX3", trade_ticket=9000003)
        _seed_trigger("PLAN-20260504-XX3",
                      contingency_name="_user_cancel",
                      action_type="cancel_plan")

        from alerts import _lookup_close_reason
        result = _lookup_close_reason(9000003,
                                       fallback="Expert Advisor")
        # Falls back — _user_cancel is excluded from the SQL.
        assert result == "Expert Advisor"
