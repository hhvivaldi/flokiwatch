"""FLO-374 — every terminal transition stamps `closed_at`.

Pre-fix: `update_plan_status(plan_id, "closed")` was the recovery
sweep's exit path, but it only updated the status column. The
`closed_at` column was left NULL — the dashboard's "duration" cell
read NULL on reconciled plans, and downstream queries had to fall
back to `last_evaluated_at` heuristics. PLAN-20260426-002 was the
production canary: status=closed, outcome backfilled, closed_at=NULL.

Post-fix: a single helper `mark_plan_terminal` stamps status +
closed_at atomically. Used by recovery (6 transitions), actions.py
(close_full FAILED branch), and the cancel_plan tool. The actions
success path keeps using `update_plan_outcome` which already
stamped closed_at — pinned by tests below to prevent regression.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any

import pytest

from snow import db as snow_db
from snow.schema import Plan, PlanStatus


# ---------------------------------------------------------------------------
# Fixtures (slim — same shape as snow_loop_test.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "closed_at_test.db"

    def _tmp_connect():
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


_BASE_PLAN: dict[str, Any] = {
    "schema_version": 3,
    "id": "PLAN-20260424-001",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "test",
        "key_levels": [4720.0],
        "confidence": 60,
        "regime_assumed": "RANGING",
        "setup_type": "pullback_trend",
        "context_tags": {
            "trend": "trend_strong", "volatility": "high_vol",
            "htf": "HTF_aligned", "news_session": [],
        },
        "confidence_reason": "test reason for closed_at coverage spec",
    },
    "entry": {
        "direction": "SELL", "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4730.0}],
        "initial_sl": 4740.0, "initial_tp": 4710.0,
    },
    "management": [],
    "exit": [],
    "emergency": {
        "max_loss_pips": 150, "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _insert(plan_id: str, status: str = "pending"):
    d = deepcopy(_BASE_PLAN)
    d["id"] = plan_id
    d["status"] = status
    snow_db.insert_plan(Plan(**d))


def _read_row(plan_id: str) -> dict:
    conn = snow_db._connect()
    try:
        r = conn.execute(
            "SELECT * FROM snow_plans WHERE id = ?", (plan_id,),
        ).fetchone()
        return dict(r) if r else {}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# mark_plan_terminal — direct unit tests
# ---------------------------------------------------------------------------

class TestMarkPlanTerminalHelper:
    def test_stamps_closed_at_on_closed_transition(self, snow_conn):
        _insert("PLAN-20260424-100", status="active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-100", 12345)

        snow_db.mark_plan_terminal("PLAN-20260424-100", PlanStatus.CLOSED.value)

        row = _read_row("PLAN-20260424-100")
        assert row["status"] == "closed"
        assert row["closed_at"] is not None
        assert row["closed_at"].endswith("Z"), (
            f"closed_at must be Z-suffixed UTC ISO; got {row['closed_at']!r}"
        )

    def test_stamps_closed_at_on_failed_transition(self, snow_conn):
        _insert("PLAN-20260424-101", status="active")
        snow_db.mark_plan_terminal("PLAN-20260424-101", PlanStatus.FAILED.value)
        row = _read_row("PLAN-20260424-101")
        assert row["status"] == "failed"
        assert row["closed_at"] is not None

    def test_stamps_closed_at_on_expired_transition(self, snow_conn):
        _insert("PLAN-20260424-102", status="pending")
        snow_db.mark_plan_terminal("PLAN-20260424-102", PlanStatus.EXPIRED.value)
        row = _read_row("PLAN-20260424-102")
        assert row["status"] == "expired"
        assert row["closed_at"] is not None

    def test_stamps_closed_at_on_cancelled_transition(self, snow_conn):
        _insert("PLAN-20260424-103", status="pending")
        snow_db.mark_plan_terminal("PLAN-20260424-103", PlanStatus.CANCELLED.value)
        row = _read_row("PLAN-20260424-103")
        assert row["status"] == "cancelled"
        assert row["closed_at"] is not None

    def test_rejects_non_terminal_status(self, snow_conn):
        _insert("PLAN-20260424-104", status="pending")
        with pytest.raises(ValueError) as ei:
            snow_db.mark_plan_terminal("PLAN-20260424-104", PlanStatus.ACTIVE.value)
        assert "terminal" in str(ei.value).lower()

    def test_does_not_clobber_existing_closed_at(self, snow_conn):
        """COALESCE protects against double-stamp on re-entry: if a
        plan was already closed at T1 and a later code path runs
        mark_plan_terminal again at T2, the original T1 wins."""
        _insert("PLAN-20260424-105", status="active")
        snow_db.update_plan_outcome(
            "PLAN-20260424-105", outcome_pips=10.0, outcome_usd=20.0,
            new_status=PlanStatus.CLOSED.value,
        )
        first = _read_row("PLAN-20260424-105")["closed_at"]
        # Force a second terminal call. Should NOT change closed_at.
        snow_db.mark_plan_terminal("PLAN-20260424-105", PlanStatus.CLOSED.value)
        second = _read_row("PLAN-20260424-105")["closed_at"]
        assert first == second, (
            f"closed_at must not be re-stamped (first={first!r} "
            f"second={second!r})"
        )


# ---------------------------------------------------------------------------
# Recovery transitions (the core FLO-374 regression test)
# ---------------------------------------------------------------------------

class FakePosition:
    def __init__(self, ticket, magic=234000, open_price=4720.0):
        self.ticket = ticket
        self.magic = magic
        self.price_open = open_price
        self.price_current = open_price
        self.symbol = "XAUUSD"
        self.profit = 0.0


class FakeMT5:
    """Minimal MT5 stand-in for recovery tests. Returns the configured
    positions list and an empty deals list (recovery's deal-history
    path is exercised separately)."""
    def __init__(self, positions=()):
        self._positions = list(positions)
        self._deals = []

    def initialize(self): return True
    def shutdown(self): pass
    def positions_get(self, *args, **kwargs):
        return tuple(self._positions)
    def history_deals_get(self, *args, **kwargs):
        return tuple(self._deals)
    def last_error(self):
        return (0, "ok")


class FakeMT5WithDeals(FakeMT5):
    """Position closed externally — deal history reports a single
    closing deal so recovery transitions ACTIVE → CLOSED."""
    def __init__(self, positions=(), close_ticket=None, close_volume=0.02,
                 close_price=4715.0):
        super().__init__(positions)
        if close_ticket is not None:
            self._deals = [_make_deal(close_ticket, close_volume, close_price)]


def _make_deal(ticket, volume, price):
    """Stand-in for an MT5 deal record covering the fields recovery
    looks at via fetch_deal_history."""
    class _D:
        pass
    d = _D()
    d.ticket = 99000 + ticket
    d.position_id = ticket
    d.entry = 1            # DEAL_ENTRY_OUT
    d.symbol = "XAUUSD"
    d.volume = volume
    d.price = price
    d.profit = 0.5
    d.time = 1714000000
    d.commission = 0.0
    d.swap = 0.0
    d.magic = 234000
    return d


class TestRecoveryStampsClosedAt:
    def test_active_to_closed_stamps_closed_at(self, snow_conn, monkeypatch):
        """Recovery's ACTIVE → CLOSED bucket — the PLAN-20260426-002
        scenario. Pre-fix: closed_at was NULL. Post-fix: stamped."""
        from snow import recovery as snow_recovery
        # Fake out the time.sleep calls in the deal-history retry loop.
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert("PLAN-20260424-200", status="active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-200", 555_000)

        # Position absent, deal history shows a close deal.
        mt5 = FakeMT5WithDeals(
            positions=(),
            close_ticket=555_000, close_volume=0.02, close_price=4715.0,
        )
        snow_recovery.reconcile_on_startup(
            tracker=None, mt5_proxy=mt5, magic=234000,
        )

        row = _read_row("PLAN-20260424-200")
        assert row["status"] == "closed"
        assert row["closed_at"] is not None
        assert row["closed_at"].endswith("Z")

    def test_pending_to_expired_stamps_closed_at(self, snow_conn, monkeypatch):
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert("PLAN-20260424-201", status="pending")
        # Force expiry into the past.
        conn = snow_db._connect()
        try:
            conn.execute(
                "UPDATE snow_plans SET expires_at = ? WHERE id = ?",
                ("2024-01-01T00:00:00Z", "PLAN-20260424-201"),
            )
            conn.commit()
        finally:
            conn.close()

        snow_recovery.reconcile_on_startup(
            tracker=None, mt5_proxy=FakeMT5(), magic=234000,
        )
        row = _read_row("PLAN-20260424-201")
        assert row["status"] == "expired"
        assert row["closed_at"] is not None

    def test_active_no_ticket_to_failed_stamps_closed_at(
        self, snow_conn, monkeypatch,
    ):
        """active_failed branch — no ticket assigned to an ACTIVE plan."""
        from snow import recovery as snow_recovery
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        _insert("PLAN-20260424-202", status="active")  # NO ticket
        snow_recovery.reconcile_on_startup(
            tracker=None, mt5_proxy=FakeMT5(), magic=234000,
        )
        row = _read_row("PLAN-20260424-202")
        assert row["status"] == "failed"
        assert row["closed_at"] is not None


# ---------------------------------------------------------------------------
# Actions path — ensure FLO-374 didn't regress the existing close_full
# success-path closed_at stamping.
# ---------------------------------------------------------------------------

class TestActionsClosedAtPreserved:
    def test_close_full_success_still_stamps_closed_at(self, snow_conn):
        """Actions success path uses update_plan_outcome which has
        always stamped closed_at. The FLO-374 changes touched the
        FAILED branch only (now uses mark_plan_terminal). Pin that
        the success branch is unchanged."""
        _insert("PLAN-20260424-300", status="active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-300", 999_000)

        snow_db.update_plan_outcome(
            "PLAN-20260424-300",
            outcome_pips=2.5, outcome_usd=12.50,
            new_status=PlanStatus.CLOSED.value,
        )
        row = _read_row("PLAN-20260424-300")
        assert row["status"] == "closed"
        assert row["closed_at"] is not None
        assert row["closed_at"].endswith("Z")
        assert row["outcome_pips"] == 2.5
