"""Snow startup-reconciliation tests — FLO-354.

Exercises every reconciliation bucket + the fail-loud contract +
the retry-with-backoff helper. Tests use a shared `FakeMT5` proxy
(positions + deal history mocks under our control), an in-memory
SQLite via the standard snow_db fixture, and an injected fake
tracker so we can assert seed calls without running the real
PerPlanTracker.

Test classes:
  * TestFetchDealHistory  — retry/backoff for the FLO-353-shared helper
  * TestPendingExpiry     — PENDING bucket
  * TestTriggered         — TRIGGERED bucket (3 sub-cases)
  * TestClosing           — CLOSING bucket (2 sub-cases)
  * TestActive            — ACTIVE bucket (5 sub-cases)
  * TestStartupContract   — fail-loud + per-plan error isolation
  * TestIdempotency       — re-run is a no-op
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

from snow import db as snow_db
from snow import recovery as snow_recovery
from snow.recovery import (
    ReconcileSummary,
    RecoveryAborted,
    fetch_deal_history,
    reconcile_on_startup,
)
from snow.schema import Direction, Plan, PlanStatus


# =============================================================================
# Test doubles
# =============================================================================

@dataclass
class FakePosition:
    ticket: int
    symbol: str = "XAUUSD"
    magic: int = 123456
    open_price: float = 4720.0


@dataclass
class FakeDeal:
    ticket: int
    profit: float = 0.0
    # FLO-353 follow-up — `position_id` matches the ticket we asked
    # for; `fetch_deal_history` filters out any deal whose
    # `position_id` does NOT match (defensive against MT5's
    # documented unreliability of the `position=` query param).
    position_id: int = -1

    def __post_init__(self):
        if self.position_id < 0:
            self.position_id = self.ticket


class FakeMT5:
    """Minimal mt5 proxy for recovery tests. Every method stubbed."""

    def __init__(
        self,
        *,
        positions: Any = (),
        positions_raises: Optional[BaseException] = None,
        deal_history: Optional[dict[int, list]] = None,
        # Per-ticket override of attempt sequence: list of return values
        # for sequential history_deals_get calls. e.g. [None, None, [deal]]
        # exercises retry+backoff.
        deal_history_attempts: Optional[dict[int, list]] = None,
    ) -> None:
        self._positions = positions  # tuple/list/None
        self._positions_raises = positions_raises
        self._deal_history = deal_history or {}
        self._deal_history_attempts = deal_history_attempts or {}
        self._attempt_counter: dict[int, int] = {}

    # --- positions ---

    def positions_get(self, *, symbol: str = "XAUUSD"):
        if self._positions_raises is not None:
            raise self._positions_raises
        return self._positions

    # --- history ---

    def history_deals_get(self, date_from, date_to, *, position: int = 0):
        if position in self._deal_history_attempts:
            seq = self._deal_history_attempts[position]
            i = self._attempt_counter.get(position, 0)
            if i < len(seq):
                self._attempt_counter[position] = i + 1
                return seq[i]
            return seq[-1]
        return self._deal_history.get(position)


class FakeTracker:
    """PerPlanTracker stand-in. Records every seed() call."""

    def __init__(self) -> None:
        self.seeds: list[tuple[str, float, Direction]] = []
        self._has_set: set[str] = set()

    def has(self, plan_id: str) -> bool:
        return plan_id in self._has_set

    def seed(self, plan_id: str, entry_price: float, direction: Direction) -> None:
        self.seeds.append((plan_id, float(entry_price), direction))
        self._has_set.add(plan_id)

    def forget(self, plan_id: str) -> None:
        self._has_set.discard(plan_id)


# =============================================================================
# Shared fixtures
# =============================================================================

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "snow_recovery_test.db"

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
def tracker() -> FakeTracker:
    return FakeTracker()


def _insert_plan(
    valid_plan_dict, *, plan_id: str, status: str,
    expires_at: Optional[str] = ...,  # sentinel: keep base value
    trade_ticket: Optional[int] = None,
) -> Plan:
    pd = deepcopy(valid_plan_dict)
    pd["id"] = plan_id
    pd["status"] = status
    if expires_at is not ...:
        pd["expires_at"] = expires_at
    plan = Plan(**pd)
    snow_db.insert_plan(plan)
    if trade_ticket is not None:
        snow_db.update_plan_trade_ticket(plan_id, trade_ticket)
    if status != PlanStatus.PENDING.value:
        # Plan was inserted as PENDING by default (validator sets it);
        # transition to the requested target.
        snow_db.update_plan_status(plan_id, status)
    return plan


def _read_status(plan_id: str) -> str:
    return snow_db.get_plan(plan_id)["status"]


def _read_evaluations(plan_id: str) -> list[dict]:
    conn = snow_db._connect()
    try:
        rows = conn.execute(
            "SELECT * FROM snow_evaluations WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# =============================================================================
# Deal-history retry helper
# =============================================================================

class TestFetchDealHistory:

    def test_returns_list_on_first_success(self):
        deals = [FakeDeal(ticket=111, position_id=111)]
        mt5p = FakeMT5(deal_history={111: deals})
        out = fetch_deal_history(111, mt5_proxy=mt5p)
        assert out == deals

    def test_empty_list_returns_immediately_no_retry(self):
        """Empty list is definitive — caller must not retry."""
        mt5p = FakeMT5(deal_history={111: []})
        out = fetch_deal_history(111, mt5_proxy=mt5p)
        assert out == []

    def test_none_then_success_within_retry_budget(self, monkeypatch):
        """First two attempts return None; third returns a non-empty
        list. Sleep is monkeypatched out so the test runs fast."""
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)
        deals = [FakeDeal(ticket=222, position_id=222)]
        mt5p = FakeMT5(
            deal_history_attempts={222: [None, None, deals]},
        )
        out = fetch_deal_history(222, mt5_proxy=mt5p)
        assert out == deals
        # Three calls observed.
        assert mt5p._attempt_counter[222] == 3

    def test_all_retries_none_returns_none(self, monkeypatch):
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)
        mt5p = FakeMT5(
            deal_history_attempts={333: [None, None, None]},
        )
        assert fetch_deal_history(333, mt5_proxy=mt5p) is None


# =============================================================================
# PENDING bucket
# =============================================================================

class TestPendingExpiry:

    def test_expires_at_past_marks_expired(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-201",
            status=PlanStatus.PENDING.value,
            expires_at="2026-04-20T00:00:00Z",  # past
        )
        # Need a UTC-aware "now" past the expiry.
        now = _dt.datetime(2026, 4, 26, 0, 0, 0, tzinfo=_dt.timezone.utc)
        summary = reconcile_on_startup(
            tracker=tracker,
            mt5_proxy=FakeMT5(positions=()),
            now=now,
        )
        assert _read_status("PLAN-20260424-201") == PlanStatus.EXPIRED.value
        evals = _read_evaluations("PLAN-20260424-201")
        assert any(e["event"] == "recovery_expired" for e in evals)
        assert summary.pending_expired == 1

    def test_expires_at_future_unchanged(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-202",
            status=PlanStatus.PENDING.value,
            expires_at="2099-01-01T00:00:00Z",
        )
        now = _dt.datetime(2026, 4, 26, 0, 0, 0, tzinfo=_dt.timezone.utc)
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()), now=now,
        )
        assert _read_status("PLAN-20260424-202") == PlanStatus.PENDING.value
        assert summary.pending_expired == 0

    def test_expires_at_null_unchanged(
        self, snow_conn, valid_plan_dict, tracker
    ):
        """Plan with expires_at NULL stays PENDING. No auto-expiry
        without an explicit expiration set by Floki."""
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-203",
            status=PlanStatus.PENDING.value,
            expires_at=None,
        )
        now = _dt.datetime(2026, 4, 26, 0, 0, 0, tzinfo=_dt.timezone.utc)
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()), now=now,
        )
        assert _read_status("PLAN-20260424-203") == PlanStatus.PENDING.value
        assert summary.pending_expired == 0


# =============================================================================
# TRIGGERED bucket
# =============================================================================

class TestTriggered:

    def test_triggered_with_matching_position_to_active_and_seeded(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-301",
            status=PlanStatus.TRIGGERED.value,
            trade_ticket=11111,
        )
        pos = FakePosition(ticket=11111, open_price=4731.5, magic=123456)
        summary = reconcile_on_startup(
            tracker=tracker,
            mt5_proxy=FakeMT5(positions=(pos,)),
            symbol="XAUUSD",
            magic=123456,
        )
        assert _read_status("PLAN-20260424-301") == PlanStatus.ACTIVE.value
        assert summary.triggered_to_active == 1
        assert summary.tracker_reseeds == 1
        # Tracker seed used MT5 open_price, not a column.
        seeded = tracker.seeds[0]
        assert seeded[0] == "PLAN-20260424-301"
        assert seeded[1] == 4731.5
        evals = _read_evaluations("PLAN-20260424-301")
        assert any(e["event"] == "recovery_triggered_to_active" for e in evals)

    def test_triggered_without_position_marks_failed(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-302",
            status=PlanStatus.TRIGGERED.value,
            trade_ticket=22222,
        )
        summary = reconcile_on_startup(
            tracker=tracker,
            mt5_proxy=FakeMT5(positions=()),
            symbol="XAUUSD", magic=123456,
        )
        assert _read_status("PLAN-20260424-302") == PlanStatus.FAILED.value
        assert summary.triggered_failed == 1
        assert tracker.seeds == []
        evals = _read_evaluations("PLAN-20260424-302")
        evt = next(e for e in evals if e["event"] == "recovery_failed")
        assert "crash_during_trigger" in (evt["conditions_snapshot"] or "")

    def test_triggered_without_ticket_marks_failed(
        self, snow_conn, valid_plan_dict, tracker
    ):
        """A TRIGGERED plan that never had a ticket assigned can only
        have come from a crash before insert_plan persisted the ticket
        column. Mark FAILED."""
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-303",
            status=PlanStatus.TRIGGERED.value,
            trade_ticket=None,
        )
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()),
        )
        assert _read_status("PLAN-20260424-303") == PlanStatus.FAILED.value
        assert summary.triggered_failed == 1


# =============================================================================
# CLOSING bucket
# =============================================================================

class TestClosing:

    def test_closing_with_position_reverts_to_active(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-401",
            status=PlanStatus.CLOSING.value,
            trade_ticket=44441,
        )
        pos = FakePosition(ticket=44441, open_price=4730.0)
        summary = reconcile_on_startup(
            tracker=tracker,
            mt5_proxy=FakeMT5(positions=(pos,)),
            magic=123456,
        )
        assert _read_status("PLAN-20260424-401") == PlanStatus.ACTIVE.value
        assert summary.closing_to_active == 1

    def test_closing_without_position_to_closed(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-402",
            status=PlanStatus.CLOSING.value,
            trade_ticket=44442,
        )
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()),
            magic=123456,
        )
        assert _read_status("PLAN-20260424-402") == PlanStatus.CLOSED.value
        assert summary.closing_to_closed == 1
        evals = _read_evaluations("PLAN-20260424-402")
        # recovery_closing_to_closed event written; outcome backfill
        # also attempted (best-effort — fakes here lack deal shape, so
        # backfill records an `outcome_backfill_failed` audit row but
        # does not raise; CLOSED transition is independent of backfill
        # outcome).
        events = [e["event"] for e in evals]
        assert "recovery_closing_to_closed" in events


# =============================================================================
# ACTIVE bucket
# =============================================================================

class TestActive:

    def test_active_with_position_reseeds_tracker(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-501",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=55551,
        )
        pos = FakePosition(ticket=55551, open_price=4729.25)
        summary = reconcile_on_startup(
            tracker=tracker,
            mt5_proxy=FakeMT5(positions=(pos,)),
            magic=123456,
        )
        # No status change.
        assert _read_status("PLAN-20260424-501") == PlanStatus.ACTIVE.value
        # But tracker reseed happened.
        assert summary.tracker_reseeds == 1
        assert tracker.seeds[0][1] == 4729.25

    def test_active_position_vanished_with_deals_to_closed(
        self, snow_conn, valid_plan_dict, tracker, monkeypatch
    ):
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-502",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=55552,
        )
        deals = [FakeDeal(ticket=55552, profit=12.34)]
        mt5p = FakeMT5(positions=(), deal_history={55552: deals})
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=mt5p, magic=123456,
        )
        assert _read_status("PLAN-20260424-502") == PlanStatus.CLOSED.value
        assert summary.active_to_closed == 1

    def test_active_position_vanished_no_deals_to_failed(
        self, snow_conn, valid_plan_dict, tracker, monkeypatch
    ):
        """Definitive empty list → FAILED with `position_vanished`."""
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-503",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=55553,
        )
        mt5p = FakeMT5(positions=(), deal_history={55553: []})
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=mt5p, magic=123456,
        )
        assert _read_status("PLAN-20260424-503") == PlanStatus.FAILED.value
        assert summary.active_failed == 1
        evals = _read_evaluations("PLAN-20260424-503")
        evt = next(e for e in evals if e["event"] == "recovery_failed")
        assert "position_vanished" in (evt["conditions_snapshot"] or "")

    def test_active_position_vanished_transient_mt5_error_left_for_retry(
        self, snow_conn, valid_plan_dict, tracker, monkeypatch
    ):
        """All retries return None — leave plan ACTIVE for the next
        startup, do NOT mark FAILED. Critical correctness guarantee:
        a transient MT5 error must never cause a permanent FAILED."""
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-504",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=55554,
        )
        mt5p = FakeMT5(
            positions=(),
            deal_history_attempts={55554: [None, None, None]},
        )
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=mt5p, magic=123456,
        )
        # Status unchanged.
        assert _read_status("PLAN-20260424-504") == PlanStatus.ACTIVE.value
        assert summary.active_left_for_retry == 1
        assert summary.active_failed == 0

    def test_active_without_ticket_to_failed(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-505",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=None,
        )
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()),
        )
        assert _read_status("PLAN-20260424-505") == PlanStatus.FAILED.value
        assert summary.active_failed == 1


# =============================================================================
# Fail-loud contract
# =============================================================================

class TestStartupContract:

    def test_empty_live_set_returns_clean_summary(
        self, snow_conn, tracker
    ):
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=()),
        )
        assert summary == ReconcileSummary()
        assert summary.total_transitions() == 0

    def test_mt5_positions_get_returns_none_aborts(
        self, snow_conn, valid_plan_dict, tracker
    ):
        """MT5 disconnect during the batch positions query MUST abort
        — misreading 'MT5 down' as 'all positions vanished' would
        mass-FAIL plans."""
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-601",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=66661,
        )
        with pytest.raises(RecoveryAborted, match="MT5 disconnected"):
            reconcile_on_startup(
                tracker=tracker, mt5_proxy=FakeMT5(positions=None),
            )
        # Plan untouched.
        assert _read_status("PLAN-20260424-601") == PlanStatus.ACTIVE.value

    def test_mt5_positions_get_raises_aborts(
        self, snow_conn, valid_plan_dict, tracker
    ):
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-602",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=66662,
        )
        with pytest.raises(RecoveryAborted):
            reconcile_on_startup(
                tracker=tracker,
                mt5_proxy=FakeMT5(positions_raises=RuntimeError("boom")),
            )

    def test_db_read_failure_aborts(
        self, snow_conn, tracker, monkeypatch
    ):
        def _explode(*a, **k):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(snow_db, "list_plans_by_status", _explode)
        with pytest.raises(RecoveryAborted, match="list_plans_by_status"):
            reconcile_on_startup(
                tracker=tracker, mt5_proxy=FakeMT5(positions=()),
            )

    def test_per_plan_exception_isolated_from_batch(
        self, snow_conn, valid_plan_dict, tracker, monkeypatch
    ):
        """One bad plan must not kill the rest of the batch. Patch
        update_plan_status to raise on a specific plan_id; assert
        the OTHER plan still reconciles."""
        # Two TRIGGERED plans; the first will fail, the second succeeds.
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-701",
            status=PlanStatus.TRIGGERED.value,
            trade_ticket=77771,
        )
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-702",
            status=PlanStatus.TRIGGERED.value,
            trade_ticket=77772,
        )

        original = snow_db.update_plan_status

        def _picky(plan_id, new_status):
            if plan_id == "PLAN-20260424-701":
                raise RuntimeError("synthetic transient error")
            return original(plan_id, new_status)

        monkeypatch.setattr(snow_db, "update_plan_status", _picky)
        # Second plan's MT5 position exists, so it's expected to
        # transition to ACTIVE.
        pos = FakePosition(ticket=77772, open_price=4720.0)
        summary = reconcile_on_startup(
            tracker=tracker, mt5_proxy=FakeMT5(positions=(pos,)),
            magic=123456,
        )
        # First plan stuck in TRIGGERED; second succeeded.
        assert _read_status("PLAN-20260424-701") == PlanStatus.TRIGGERED.value
        assert _read_status("PLAN-20260424-702") == PlanStatus.ACTIVE.value
        assert summary.triggered_to_active == 1


# =============================================================================
# Idempotency
# =============================================================================

class TestIdempotency:

    def test_re_run_is_no_op_after_terminal_transitions(
        self, snow_conn, valid_plan_dict, tracker, monkeypatch
    ):
        """Run reconcile twice: every transitioned plan is now in a
        terminal status, so the second pass sees no live plans for
        them."""
        monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)

        # PENDING expired
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-801",
            status=PlanStatus.PENDING.value,
            expires_at="2026-04-20T00:00:00Z",
        )
        # ACTIVE → CLOSED (deal exists)
        _insert_plan(
            valid_plan_dict,
            plan_id="PLAN-20260424-802",
            status=PlanStatus.ACTIVE.value,
            trade_ticket=88882,
        )
        deals = [FakeDeal(ticket=88882)]
        mt5p = FakeMT5(positions=(), deal_history={88882: deals})
        now = _dt.datetime(2026, 4, 26, 0, 0, 0, tzinfo=_dt.timezone.utc)

        s1 = reconcile_on_startup(
            tracker=tracker, mt5_proxy=mt5p, now=now, magic=123456,
        )
        assert s1.pending_expired == 1
        assert s1.active_to_closed == 1

        # Second run sees both plans in terminal statuses now — they
        # won't be in the live-statuses query result.
        # Reset the FakeMT5 attempt counter just in case.
        mt5p._attempt_counter.clear()
        s2 = reconcile_on_startup(
            tracker=tracker, mt5_proxy=mt5p, now=now, magic=123456,
        )
        assert s2 == ReconcileSummary()
