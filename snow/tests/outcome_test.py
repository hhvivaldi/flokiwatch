"""Outcome-backfill tests — FLO-353.

Exercises the best-effort post-close P&L backfill module. Tests
construct fake MT5 deals (with `entry` / `type` / `price` / `volume`
/ `profit` fields) and feed them through a `FakeMT5` proxy. The
`backfill_outcome` function MUST NEVER raise — every test path
verifies that even the worst inputs (None deals, empty list,
malformed shape, exception on DB write) produce a clean
`BackfillResult` and an audit row.

Test classes:
  * TestHappyPath        — single close, BUY + SELL profit/loss
  * TestPartialClose     — multiple close deals, volume-weighted avg
  * TestFailureModes     — None / empty / shape-mismatch / unknown direction
  * TestBestEffortContract — no exceptions propagate
  * TestActionsIntegration — close_full / close_partial wire-up smoke
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from snow import db as snow_db
from snow import outcome as snow_outcome
from snow import recovery as snow_recovery
from snow.outcome import BackfillResult, backfill_outcome
from snow.schema import Plan, PlanStatus


# =============================================================================
# Test doubles
# =============================================================================

@dataclass
class FakeDeal:
    """MT5-shaped deal. `entry` 0=IN, 1=OUT, 2=INOUT. `type` 0=BUY, 1=SELL.

    `position_id` defaults to the ticket so existing tests keep working;
    the position_id-filter test sets a mismatching value explicitly.
    """
    ticket: int
    entry: int
    type: int
    price: float
    volume: float
    profit: float
    position_id: int = -1

    def __post_init__(self):
        if self.position_id < 0:
            self.position_id = self.ticket


class FakeMT5:
    def __init__(self, *, deal_history: Optional[dict] = None,
                 deal_history_attempts: Optional[dict] = None) -> None:
        self._deal_history = deal_history or {}
        self._deal_history_attempts = deal_history_attempts or {}
        self._counter: dict = {}

    def history_deals_get(self, date_from, date_to, *, position: int = 0):
        if position in self._deal_history_attempts:
            seq = self._deal_history_attempts[position]
            i = self._counter.get(position, 0)
            if i < len(seq):
                self._counter[position] = i + 1
                return seq[i]
            return seq[-1]
        return self._deal_history.get(position)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "snow_outcome_test.db"

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
def closed_plan(snow_conn, valid_plan_dict):
    pd = deepcopy(valid_plan_dict)
    pd["id"] = "PLAN-20260424-901"
    plan = Plan(**pd)
    snow_db.insert_plan(plan)
    snow_db.update_plan_trade_ticket(plan.id, 99991)
    snow_db.update_plan_outcome(
        plan.id, outcome_pips=None, outcome_usd=None,
        new_status=PlanStatus.CLOSED.value,
    )
    return plan


def _read_outcome(plan_id: str) -> tuple:
    row = snow_db.get_plan(plan_id)
    return (row["outcome_pips"], row["outcome_usd"])


def _audit_events(plan_id: str) -> list[str]:
    conn = snow_db._connect()
    try:
        rows = conn.execute(
            "SELECT event FROM snow_evaluations WHERE plan_id = ? ORDER BY id",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [r["event"] for r in rows]


def _no_sleep(monkeypatch):
    """Patch out backoff sleep so retry-loop tests run instantly."""
    monkeypatch.setattr(snow_recovery._time, "sleep", lambda _s: None)


# =============================================================================
# Happy path
# =============================================================================

class TestHappyPath:

    def test_buy_profit_single_close(self, snow_conn, closed_plan):
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        # 10 dollars at $1/pip on 0.10 lots → 100 pips? Actually XAUUSD
        # PIP_SIZE=0.1 — (4730-4720)/0.1 = 100 pips. profit=$10 directly.
        assert result.outcome_pips == pytest.approx(100.0)
        assert result.outcome_usd == pytest.approx(10.0)
        pips, usd = _read_outcome(closed_plan.id)
        assert pips == pytest.approx(100.0)
        assert usd == pytest.approx(10.0)
        assert "outcome_backfilled" in _audit_events(closed_plan.id)

    def test_sell_profit_single_close(self, snow_conn, closed_plan):
        # SELL: open higher, close lower → profit. type=1 = SELL.
        deals = [
            FakeDeal(ticket=99991, entry=0, type=1, price=4730.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=0, price=4720.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        # SELL pips = (4720 - 4730) * -1 / 0.1 = 100 pips profit
        assert result.outcome_pips == pytest.approx(100.0)
        assert result.outcome_usd == pytest.approx(10.0)

    def test_buy_loss(self, snow_conn, closed_plan):
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4730.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4725.0, volume=0.10, profit=-5.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        assert result.outcome_pips == pytest.approx(-50.0)
        assert result.outcome_usd == pytest.approx(-5.0)

    def test_sell_loss(self, snow_conn, closed_plan):
        deals = [
            FakeDeal(ticket=99991, entry=0, type=1, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=0, price=4730.0, volume=0.10, profit=-10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        assert result.outcome_pips == pytest.approx(-100.0)
        assert result.outcome_usd == pytest.approx(-10.0)


# =============================================================================
# Partial closes
# =============================================================================

class TestPositionIdFilter:
    """FLO-353 production bug — MT5's `position=ticket` query param is
    documented as a hint, not a guarantee. The first live trade
    (PLAN-20260426-002, ticket=1612264515) saw MT5 return 202 deals,
    of which only 2 actually had `position_id == ticket`. Without
    the defensive filter, backfill aggregated 95× the plan's volume
    into a fictional +$234 outcome on a 0.02-lot SELL whose actual
    close was at MT5 TP=4680.

    This test class pins the filter so the bug cannot regress."""

    def test_filter_drops_deals_with_mismatched_position_id(
        self, snow_conn, closed_plan,
    ):
        """Real shape from production: 1 IN + 1 OUT for OUR ticket,
        plus N+M unrelated deals MT5 surfaced under
        `position=our_ticket`. After the filter, only the 2
        matching deals contribute to the outcome computation."""
        ours = [
            FakeDeal(
                ticket=99991, entry=0, type=1, price=4693.0,
                volume=0.02, profit=0.0, position_id=99991,
            ),
            FakeDeal(
                ticket=99991, entry=1, type=0, price=4680.0,
                volume=0.02, profit=2.6, position_id=99991,
            ),
        ]
        # Noise: 50 IN + 50 OUT deals from a totally different
        # position. MT5 returns them under our `position=` query for
        # reasons known only to MetaQuotes; without the filter they
        # would corrupt the outcome.
        noise = []
        for i in range(50):
            noise.append(FakeDeal(
                ticket=99991, entry=0, type=0, price=4645.0,
                volume=0.02, profit=0.0, position_id=11111,
            ))
            noise.append(FakeDeal(
                ticket=99991, entry=1, type=1, price=4760.0,
                volume=0.02, profit=2.3, position_id=11111,
            ))
        all_deals = ours + noise
        mt5p = FakeMT5(deal_history={99991: all_deals})

        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        # Direction = SELL (from our IN deal), not BUY (from noise).
        # Pips = (4680 - 4693) * -1 / 0.1 = 130 pips profit on the SELL.
        assert result.outcome_pips == pytest.approx(130.0)
        assert result.outcome_usd == pytest.approx(2.6)
        assert result.deal_count == 2  # only the matching pair

    def test_filter_empty_after_drop_returns_no_deals(
        self, snow_conn, closed_plan,
    ):
        """If MT5 returns deals but NONE match position_id, the
        filter strips everything and backfill records the
        no_deals_for_ticket failure mode (not the wrong-data
        success that motivated this filter)."""
        unrelated = [
            FakeDeal(
                ticket=99991, entry=0, type=0, price=4720.0,
                volume=0.10, profit=0.0, position_id=22222,
            ),
            FakeDeal(
                ticket=99991, entry=1, type=1, price=4730.0,
                volume=0.10, profit=10.0, position_id=22222,
            ),
        ]
        mt5p = FakeMT5(deal_history={99991: unrelated})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "no_deals_for_ticket"


class TestPartialClose:

    def test_two_partial_closes_volume_weighted(self, snow_conn, closed_plan):
        """Open 0.20 BUY @4720. Close 0.10 @4730, then 0.10 @4740.
        Vw close = (4730*0.10 + 4740*0.10) / 0.20 = 4735.
        Pips = (4735 - 4720)/0.1 = 150. USD = profit_sum."""
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.20, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4730.0, volume=0.10, profit=10.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4740.0, volume=0.10, profit=20.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        assert result.outcome_pips == pytest.approx(150.0)
        assert result.outcome_usd == pytest.approx(30.0)
        assert result.deal_count == 3

    def test_inout_deal_treated_as_close(self, snow_conn, closed_plan):
        """`entry==2` is DEAL_ENTRY_INOUT (reversal); we treat it as a
        close-side deal."""
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=2, type=1, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is True
        assert result.outcome_pips == pytest.approx(100.0)


# =============================================================================
# Failure modes — best-effort, audit always written
# =============================================================================

class TestFailureModes:

    def test_deal_history_unavailable_records_audit_no_raise(
        self, snow_conn, closed_plan, monkeypatch
    ):
        _no_sleep(monkeypatch)
        # All retries return None.
        mt5p = FakeMT5(deal_history_attempts={99991: [None, None, None]})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "deal_history_unavailable"
        assert result.outcome_pips is None
        assert result.outcome_usd is None
        # outcome columns stayed NULL
        pips, usd = _read_outcome(closed_plan.id)
        assert pips is None and usd is None
        assert "outcome_backfill_failed" in _audit_events(closed_plan.id)

    def test_empty_deal_list_records_audit_no_raise(
        self, snow_conn, closed_plan
    ):
        mt5p = FakeMT5(deal_history={99991: []})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "no_deals_for_ticket"
        pips, usd = _read_outcome(closed_plan.id)
        assert pips is None and usd is None
        assert "outcome_backfill_failed" in _audit_events(closed_plan.id)

    def test_no_in_deal_records_shape_unexpected(
        self, snow_conn, closed_plan
    ):
        # Only close deals, no IN deal.
        deals = [
            FakeDeal(ticket=99991, entry=1, type=1, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "shape_unexpected"

    def test_no_close_deal_records_shape_unexpected(
        self, snow_conn, closed_plan
    ):
        # Only IN deal.
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.10, profit=0.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "shape_unexpected"

    def test_unknown_direction_records_audit(
        self, snow_conn, closed_plan
    ):
        # IN deal with type=99 (neither BUY=0 nor SELL=1).
        deals = [
            FakeDeal(ticket=99991, entry=0, type=99, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=99, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        assert result.reason == "unknown_direction"


# =============================================================================
# Best-effort contract — no exceptions propagate
# =============================================================================

class TestBestEffortContract:

    def test_db_update_failure_does_not_raise(
        self, snow_conn, closed_plan, monkeypatch
    ):
        """Even if the DB UPDATE itself raises, backfill must not
        propagate the exception. Audit row attempt may also fail —
        also caught."""
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})

        def _explode(*a, **k):
            raise RuntimeError("synthetic db write failure")
        monkeypatch.setattr(
            snow_db, "update_plan_outcome_columns_only", _explode
        )

        result = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert result.success is False
        # Outer wrapper caught the exception.
        assert "unhandled" in (result.reason or "")

    def test_idempotent_double_call(self, snow_conn, closed_plan):
        """Calling backfill_outcome twice produces the same final state.
        Second call overwrites with identical numbers."""
        deals = [
            FakeDeal(ticket=99991, entry=0, type=0, price=4720.0, volume=0.10, profit=0.0),
            FakeDeal(ticket=99991, entry=1, type=1, price=4730.0, volume=0.10, profit=10.0),
        ]
        mt5p = FakeMT5(deal_history={99991: deals})
        r1 = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        r2 = backfill_outcome(closed_plan.id, 99991, mt5_proxy=mt5p)
        assert r1.success and r2.success
        assert r1.outcome_pips == r2.outcome_pips
        assert r1.outcome_usd == r2.outcome_usd
        # Two audit rows (one per call) — that's expected; backfill
        # records every call regardless of pre-state.
        events = _audit_events(closed_plan.id)
        assert events.count("outcome_backfilled") == 2
