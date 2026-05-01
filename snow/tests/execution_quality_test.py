"""snow_execution_quality (FLO-365) — DDL, helpers, and entry wire-up.

Covers:
  * insert_execution_quality happy path + best-effort swallow on bad FK
  * execution_quality.compute_slippage_pips signed convention
  * execution_quality.latency_ms parses fired_at/executed_at correctly
  * Entry dispatch records a row with non-null actual_price + slippage
  * Regression for the production bug shape: plan_volume=0.02 vs
    actual_volume=1.89 — a row is written and the discrepancy is visible.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any, Optional

import pytest

import config
from snow import db as snow_db
from snow import execution_quality as eq
from snow.actions import (
    FirePayload,
    SnowActions,
    STATUS_SUCCESS,
)
from snow.priority import FireEvent
from snow.schema import (
    ActionAdjustSL,
    ActionAdjustTP,
    ActionCloseFull,
    ActionClosePartial,
    ActionExecuteMarket,
    ActionMoveSLToBreakeven,
    ContingencyGuards,
    Direction,
    Plan,
)


# ---------------------------------------------------------------------------
# Fixtures (slim — we lift just what we need from actions_test.py shape)
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "snow_eq_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


@pytest.fixture(autouse=True)
def force_dry_run_off(monkeypatch):
    monkeypatch.setattr(config, "SNOW_DRY_RUN", False, raising=False)


@pytest.fixture(autouse=True)
def stub_tick(monkeypatch):
    """Default tick capture used by entry-dispatch tests. Individual tests
    override by re-patching `eq.capture_tick`."""
    def _fake(_symbol: str) -> eq.TickSnapshot:
        return eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2)
    monkeypatch.setattr(eq, "capture_tick", _fake)


class _OrderResultLike:
    def __init__(self, *, success=True, ticket=111_222,
                 error_code=None, error_message=None,
                 price=4720.0, volume=0.02):
        self.success = success
        self.ticket = ticket
        self.error_code = error_code
        self.error_message = error_message
        self.price = price
        self.volume = volume


class _FakeExecutor:
    def __init__(self, result: Optional[_OrderResultLike] = None):
        self._next = result or _OrderResultLike()
        self.calls: list[dict] = []

    def execute_trade(self, **kwargs):
        self.calls.append(kwargs)
        return self._next

    def modify_position(self, ticket, new_sl=None, new_tp=None):
        return self._next

    def close_position(self, ticket, volume=None):
        return self._next

    def get_open_positions(self):
        return []


import threading

_BASE_PLAN_DICT: dict[str, Any] = {
    "schema_version": 1,
    "id": "PLAN-20260424-100",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "FLO-365 test",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 75,
        "regime_assumed": "TRENDING_BEARISH",
    },
    "entry": {
        "direction": "BUY",
        "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4715.0}],
        "initial_sl": 4710.0,
        "initial_tp": 4730.0,
    },
    "management": [{"name": "be", "priority": 7, "conditions": [{"type": "mfe_reached", "pips": 100.0}], "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0}, "fires": "once"}],
    "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
    "emergency": {
        "max_loss_pips": 150,
        "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _insert_plan(plan_id: str, direction: str = "BUY",
                 volume: float = 0.02) -> Plan:
    d = deepcopy(_BASE_PLAN_DICT)
    d["id"] = plan_id
    d["entry"]["direction"] = direction
    d["entry"]["volume"] = volume
    plan = Plan(**d)
    snow_db.insert_plan(plan)
    return plan


def _entry_fire(plan_id: str, direction: str = "BUY",
                fired_at: str = "2026-04-24T08:00:00.500Z") -> FireEvent:
    payload = FirePayload(
        action=ActionExecuteMarket(),
        kind="entry",
        plan_direction=Direction.BUY if direction == "BUY" else Direction.SELL,
        ticket=None,
        guards=None,
        entry_price=None,
    )
    return FireEvent(
        plan_id=plan_id,
        created_at="2026-04-24T08:00:00Z",
        contingency_name="_entry",
        action_type="execute_market",
        override=5,
        plan_list_order=-1,
        payload=payload,
        fired_at=fired_at,
    )


def _make_actions(executor: _FakeExecutor) -> SnowActions:
    return SnowActions(
        executor_impl=executor,
        executor_lock_impl=threading.RLock(),
    )


# ---------------------------------------------------------------------------
# Pure helpers (no DB)
# ---------------------------------------------------------------------------

class TestSlippageMath:
    def test_buy_filled_above_ask_is_positive_slippage(self):
        # XAU/USD pip = 0.1. Filled 0.4 above ask → +4 pips unfavourable.
        assert eq.compute_slippage_pips("BUY", 4720.0, 4720.4) == 4.0

    def test_buy_filled_below_ask_is_negative_slippage(self):
        # Got better price than expected → -2 pips (favourable).
        assert eq.compute_slippage_pips("BUY", 4720.0, 4719.8) == -2.0

    def test_sell_filled_below_bid_is_positive_slippage(self):
        assert eq.compute_slippage_pips("SELL", 4720.0, 4719.6) == 4.0

    def test_sell_filled_above_bid_is_negative_slippage(self):
        assert eq.compute_slippage_pips("SELL", 4720.0, 4720.2) == -2.0

    def test_returns_none_on_missing_ref(self):
        assert eq.compute_slippage_pips("BUY", None, 4720.0) is None

    def test_returns_none_on_missing_actual(self):
        assert eq.compute_slippage_pips("BUY", 4720.0, None) is None

    def test_returns_none_on_unknown_direction(self):
        assert eq.compute_slippage_pips("FLAT", 4720.0, 4720.4) is None


class TestLatencyMs:
    def test_basic_diff(self):
        assert eq.latency_ms(
            "2026-04-24T08:00:00.000Z",
            "2026-04-24T08:00:00.500Z",
        ) == 500

    def test_sub_ms_rounds(self):
        assert eq.latency_ms(
            "2026-04-24T08:00:00.000Z",
            "2026-04-24T08:00:00.001Z",
        ) == 1

    def test_none_fired_at_returns_none(self):
        assert eq.latency_ms(None, "2026-04-24T08:00:00.500Z") is None

    def test_unparseable_returns_none(self):
        assert eq.latency_ms("not-a-date", "also-not") is None


class TestEntryReferencePrice:
    def test_buy_uses_ask(self):
        tick = eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2)
        assert eq.entry_reference_price("BUY", tick) == 4720.4

    def test_sell_uses_bid(self):
        tick = eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2)
        assert eq.entry_reference_price("SELL", tick) == 4720.0

    def test_unknown_returns_none(self):
        tick = eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2)
        assert eq.entry_reference_price("FLAT", tick) is None


# ---------------------------------------------------------------------------
# DB layer — insert_execution_quality
# ---------------------------------------------------------------------------

class TestInsertExecutionQuality:
    def test_happy_path_persists_row_and_links_to_trigger(self, snow_conn):
        _insert_plan("PLAN-20260424-101")
        # Need a real snow_triggers row to FK against (we share its id).
        trigger_id = snow_db.record_trigger_and_transition(
            "PLAN-20260424-101",
            contingency_name="_entry",
            contingency_kind="entry",
            action_type="execute_market",
            execution_status="success",
            new_plan_status="active",
            action_params={"direction": "BUY", "volume": 0.02},
            execution_result={"price": 4720.4, "ticket": 999},
            cycle_duration_ms=42,
            trade_ticket=999,
        )

        snow_db.insert_execution_quality(
            trigger_id=trigger_id,
            plan_id="PLAN-20260424-101",
            action_type="execute_market",
            fired_at="2026-04-24T08:00:00.000Z",
            executed_at="2026-04-24T08:00:00.045Z",
            latency_ms=45,
            plan_volume=0.02,
            plan_price=4720.4,
            actual_volume=0.02,
            actual_price=4720.5,
            slippage_pips=1.0,
            bid_at_fire=4720.0,
            ask_at_fire=4720.4,
            mid_at_fire=4720.2,
            status="success",
            ticket=999,
            attempts=1,
            error_message=None,
        )

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            "SELECT * FROM snow_execution_quality WHERE id = ?",
            (trigger_id,),
        ))
        conn.close()
        assert len(rows) == 1
        r = rows[0]
        assert r["plan_id"] == "PLAN-20260424-101"
        assert r["action_type"] == "execute_market"
        assert r["latency_ms"] == 45
        assert r["plan_volume"] == 0.02
        assert r["actual_price"] == 4720.5
        assert r["slippage_pips"] == 1.0
        assert r["status"] == "success"
        assert r["ticket"] == 999

    def test_swallows_fk_violation_silently(self, snow_conn, caplog):
        # Ref a non-existent trigger_id — FK violation is logged, not raised.
        snow_db.insert_execution_quality(
            trigger_id=999_999,
            plan_id="PLAN-20260424-101",  # FK to plans, also missing
            action_type="execute_market",
            fired_at=None, executed_at="2026-04-24T08:00:00Z",
            latency_ms=None, plan_volume=None, plan_price=None,
            actual_volume=None, actual_price=None, slippage_pips=None,
            bid_at_fire=None, ask_at_fire=None, mid_at_fire=None,
            status="success", ticket=None, attempts=None, error_message=None,
        )
        # If we got here without raising, swallow contract holds.

    def test_modify_path_nullable_columns(self, snow_conn):
        _insert_plan("PLAN-20260424-102")
        trigger_id = snow_db.record_trigger_and_transition(
            "PLAN-20260424-102",
            contingency_name="lock_be",
            contingency_kind="management",
            action_type="adjust_sl",
            execution_status="success",
            new_plan_status="active",
        )
        snow_db.insert_execution_quality(
            trigger_id=trigger_id,
            plan_id="PLAN-20260424-102",
            action_type="adjust_sl",
            fired_at="2026-04-24T08:01:00Z",
            executed_at="2026-04-24T08:01:00.030Z",
            latency_ms=30,
            plan_volume=None, plan_price=4715.0,
            actual_volume=None, actual_price=None, slippage_pips=None,
            bid_at_fire=4720.0, ask_at_fire=4720.4, mid_at_fire=4720.2,
            status="success", ticket=999, attempts=1, error_message=None,
        )

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM snow_execution_quality WHERE id = ?",
            (trigger_id,),
        ).fetchone()
        conn.close()
        assert r["actual_price"] is None
        assert r["slippage_pips"] is None
        assert r["bid_at_fire"] == 4720.0


# ---------------------------------------------------------------------------
# Wire-up — _dispatch_execute_market records a row
# ---------------------------------------------------------------------------

class TestEntryDispatchRecording:
    def test_records_row_with_slippage_and_latency(self, snow_conn, monkeypatch):
        _insert_plan("PLAN-20260424-200", direction="BUY", volume=0.02)
        # Tick: ask=4720.4 (BUY ref). Fill at 4720.5 → +1.0 pip slippage.
        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2),
        )
        result = _OrderResultLike(success=True, ticket=999,
                                  price=4720.5, volume=0.02)
        actions = _make_actions(_FakeExecutor(result))
        fire = _entry_fire("PLAN-20260424-200", direction="BUY")

        actions.execute_action(fire)

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            "SELECT * FROM snow_execution_quality "
            "WHERE plan_id = ? ORDER BY id DESC",
            ("PLAN-20260424-200",),
        ))
        conn.close()
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] == STATUS_SUCCESS
        assert r["action_type"] == "execute_market"
        assert r["plan_volume"] == 0.02
        assert r["plan_price"] == 4720.4
        assert r["actual_volume"] == 0.02
        assert r["actual_price"] == 4720.5
        assert r["slippage_pips"] == 1.0
        assert r["bid_at_fire"] == 4720.0
        assert r["ask_at_fire"] == 4720.4
        assert r["mid_at_fire"] == 4720.2
        assert r["ticket"] == 999
        assert r["fired_at"] == "2026-04-24T08:00:00.500Z"
        # latency from fired_at -> executed_at is non-null and >= 0.
        assert r["latency_ms"] is not None
        assert r["latency_ms"] >= 0

    def test_volume_mismatch_visible_in_row(self, snow_conn, monkeypatch):
        """Regression for FLO-353 production bug shape: plan_volume=0.02
        but executor reports 1.89 (broker/aggregation glitch). The row
        must persist BOTH numbers so the discrepancy is queryable."""
        _insert_plan("PLAN-20260424-201", direction="BUY", volume=0.02)
        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2),
        )
        result = _OrderResultLike(
            success=True, ticket=999, price=4720.4, volume=1.89,
        )
        actions = _make_actions(_FakeExecutor(result))
        fire = _entry_fire("PLAN-20260424-201", direction="BUY")

        actions.execute_action(fire)

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT plan_volume, actual_volume "
            "FROM snow_execution_quality WHERE plan_id = ?",
            ("PLAN-20260424-201",),
        ).fetchone()
        conn.close()
        assert r["plan_volume"] == 0.02
        assert r["actual_volume"] == 1.89
        # The whole point of the regression: |actual - plan| is huge.
        assert abs(r["actual_volume"] - r["plan_volume"]) > 1.0

    def test_modify_path_records_null_actual_price(self, snow_conn, monkeypatch):
        """adjust_sl: plan_price = SL target; actual_price/slippage NULL
        because no fill happens."""
        plan = _insert_plan("PLAN-20260424-203", direction="BUY", volume=0.02)
        snow_db.update_plan_status("PLAN-20260424-203", "active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-203", 999)

        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2),
        )
        actions = _make_actions(_FakeExecutor(_OrderResultLike(ticket=999)))
        payload = FirePayload(
            action=ActionAdjustSL(price=4715.0),
            kind="management",
            plan_direction=Direction.BUY,
            ticket=999,
            guards=None,
            entry_price=None,
        )
        fire = FireEvent(
            plan_id="PLAN-20260424-203",
            created_at="2026-04-24T08:00:00Z",
            contingency_name="lock_be",
            action_type="adjust_sl",
            override=5,
            plan_list_order=0,
            payload=payload,
            fired_at="2026-04-24T08:01:00.000Z",
        )

        actions.execute_action(fire)

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM snow_execution_quality WHERE plan_id = ?",
            ("PLAN-20260424-203",),
        ).fetchone()
        conn.close()
        assert r is not None
        assert r["action_type"] == "adjust_sl"
        assert r["plan_price"] == 4715.0
        assert r["actual_price"] is None
        assert r["slippage_pips"] is None
        assert r["bid_at_fire"] == 4720.0
        assert r["status"] == STATUS_SUCCESS

    def test_close_full_failure_preserves_error_message(self, snow_conn, monkeypatch):
        """Regression for advisor-flagged bug: failure-path used to drop
        result, losing error_message. Now preserved across close paths."""
        _insert_plan("PLAN-20260424-205", direction="BUY", volume=0.02)
        snow_db.update_plan_status("PLAN-20260424-205", "active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-205", 999)
        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4730.0, ask=4730.4, mid=4730.2),
        )
        # Failed close result with a message.
        exe = _FakeExecutor(_OrderResultLike(
            success=False, ticket=None, price=None, volume=None,
            error_message="broker rejected close",
        ))
        actions = _make_actions(exe)
        payload = FirePayload(
            action=ActionCloseFull(),
            kind="exit",
            plan_direction=Direction.BUY,
            ticket=999, guards=None, entry_price=None,
        )
        fire = FireEvent(
            plan_id="PLAN-20260424-205",
            created_at="2026-04-24T08:00:00Z",
            contingency_name="hit_sl", action_type="close_full",
            override=5, plan_list_order=1000, payload=payload,
            fired_at="2026-04-24T08:02:00.000Z",
        )
        actions.execute_action(fire)
        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM snow_execution_quality WHERE plan_id = ?",
            ("PLAN-20260424-205",),
        ).fetchone()
        conn.close()
        assert r is not None
        assert r["status"] != STATUS_SUCCESS
        assert r["error_message"] == "broker rejected close"

    def test_close_full_path_records_fill_price(self, snow_conn, monkeypatch):
        plan = _insert_plan("PLAN-20260424-204", direction="BUY", volume=0.02)
        snow_db.update_plan_status("PLAN-20260424-204", "active")
        snow_db.update_plan_trade_ticket("PLAN-20260424-204", 999)

        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4730.0, ask=4730.4, mid=4730.2),
        )
        # Stub out the outcome backfill so close_full doesn't try to
        # touch MT5 deal history during the unit test.
        from snow import outcome as snow_outcome
        monkeypatch.setattr(
            snow_outcome, "backfill_outcome",
            lambda *a, **k: type("R", (), {"success": True, "reason": "noop"})(),
        )

        exe = _FakeExecutor(_OrderResultLike(
            success=True, ticket=999, price=4730.5, volume=0.02,
        ))
        actions = _make_actions(exe)
        payload = FirePayload(
            action=ActionCloseFull(),
            kind="exit",
            plan_direction=Direction.BUY,
            ticket=999,
            guards=None,
            entry_price=None,
        )
        fire = FireEvent(
            plan_id="PLAN-20260424-204",
            created_at="2026-04-24T08:00:00Z",
            contingency_name="hit_tp",
            action_type="close_full",
            override=5,
            plan_list_order=1000,
            payload=payload,
            fired_at="2026-04-24T08:02:00.000Z",
        )

        actions.execute_action(fire)

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM snow_execution_quality WHERE plan_id = ?",
            ("PLAN-20260424-204",),
        ).fetchone()
        conn.close()
        assert r is not None
        assert r["action_type"] == "close_full"
        assert r["plan_price"] is None
        assert r["actual_price"] == 4730.5
        assert r["actual_volume"] == 0.02
        # Close: no expected price → slippage NULL by design.
        assert r["slippage_pips"] is None
        assert r["status"] == STATUS_SUCCESS

    def test_failure_path_still_records_row(self, snow_conn, monkeypatch):
        """retry_exhausted / failure must also produce a quality row so
        the dashboard can show failed dispatches alongside successes."""
        _insert_plan("PLAN-20260424-202", direction="BUY", volume=0.02)
        monkeypatch.setattr(
            eq, "capture_tick",
            lambda _s: eq.TickSnapshot(bid=4720.0, ask=4720.4, mid=4720.2),
        )
        result = _OrderResultLike(
            success=False, ticket=None, price=None, volume=None,
            error_message="broker rejected",
        )
        actions = _make_actions(_FakeExecutor(result))
        fire = _entry_fire("PLAN-20260424-202", direction="BUY")

        actions.execute_action(fire)

        conn = sqlite3.connect(str(snow_conn))
        conn.row_factory = sqlite3.Row
        rows = list(conn.execute(
            "SELECT * FROM snow_execution_quality WHERE plan_id = ?",
            ("PLAN-20260424-202",),
        ))
        conn.close()
        assert len(rows) == 1
        r = rows[0]
        assert r["status"] != STATUS_SUCCESS
        assert r["actual_price"] is None
        assert r["slippage_pips"] is None
        assert r["error_message"] == "broker rejected"


# ---------------------------------------------------------------------------
# Aggregate query
# ---------------------------------------------------------------------------

class TestAggregateSummary:
    def test_groups_by_action_with_basic_stats(self, snow_conn):
        # Seed 3 entry rows + 2 adjust rows directly via the helper.
        # Use "now-1h" for executed_at so the test stays valid for any
        # window_days >= 1 (no calendar drift).
        from datetime import datetime, timedelta, timezone
        from tz_utils import utc_iso
        recent_iso = utc_iso(datetime.now(timezone.utc) - timedelta(hours=1))

        _insert_plan("PLAN-20260424-300", direction="BUY", volume=0.02)
        for slip, lat, status in [
            (0.0, 50, "success"),
            (2.0, 80, "success"),
            (5.0, 200, "retry_exhausted"),
        ]:
            tid = snow_db.record_trigger(
                plan_id="PLAN-20260424-300",
                contingency_name="_entry",
                contingency_kind="entry",
                action_type="execute_market",
                execution_status=status,
            )
            snow_db.insert_execution_quality(
                trigger_id=tid, plan_id="PLAN-20260424-300",
                action_type="execute_market",
                fired_at=None, executed_at=recent_iso,
                latency_ms=lat,
                plan_volume=0.02, plan_price=4720.4,
                actual_volume=0.02, actual_price=4720.4 + slip * 0.1,
                slippage_pips=slip,
                bid_at_fire=4720.0, ask_at_fire=4720.4, mid_at_fire=4720.2,
                status=status, ticket=999, attempts=1, error_message=None,
            )
        for lat in [10, 30]:
            tid = snow_db.record_trigger(
                plan_id="PLAN-20260424-300",
                contingency_name="lock_be",
                contingency_kind="management",
                action_type="adjust_sl",
                execution_status="success",
            )
            snow_db.insert_execution_quality(
                trigger_id=tid, plan_id="PLAN-20260424-300",
                action_type="adjust_sl",
                fired_at=None, executed_at=recent_iso,
                latency_ms=lat,
                plan_volume=None, plan_price=4715.0,
                actual_volume=None, actual_price=None, slippage_pips=None,
                bid_at_fire=4720.0, ask_at_fire=4720.4, mid_at_fire=4720.2,
                status="success", ticket=999, attempts=1, error_message=None,
            )

        out = eq.aggregate_summary(window_days=30)
        assert "execute_market" in out
        assert "adjust_sl" in out
        em = out["execute_market"]
        assert em["count"] == 3
        assert em["success_count"] == 2
        assert em["max_slippage_pips"] == 5.0
        # avg = (0 + 2 + 5) / 3 ≈ 2.33
        assert em["avg_slippage_pips"] == 2.33
        adj = out["adjust_sl"]
        assert adj["count"] == 2
        # No slippage data on adjusts.
        assert adj["avg_slippage_pips"] is None
        assert adj["max_slippage_pips"] is None
        # p95 of two latencies → max bucket.
        assert adj["p95_latency_ms"] == 30

    def test_window_filters_old_rows(self, snow_conn):
        _insert_plan("PLAN-20260424-301", direction="BUY", volume=0.02)
        # One recent (will pass), one ancient (should be filtered).
        from tz_utils import utc_iso
        from datetime import datetime, timezone, timedelta
        recent = utc_iso(datetime.now(timezone.utc) - timedelta(hours=1))
        ancient = "2024-01-01T00:00:00.000Z"
        for ts in (recent, ancient):
            tid = snow_db.record_trigger(
                plan_id="PLAN-20260424-301",
                contingency_name="_entry",
                contingency_kind="entry",
                action_type="execute_market",
                execution_status="success",
            )
            snow_db.insert_execution_quality(
                trigger_id=tid, plan_id="PLAN-20260424-301",
                action_type="execute_market",
                fired_at=None, executed_at=ts,
                latency_ms=10, plan_volume=0.02, plan_price=4720.0,
                actual_volume=0.02, actual_price=4720.0, slippage_pips=0.0,
                bid_at_fire=None, ask_at_fire=None, mid_at_fire=None,
                status="success", ticket=999, attempts=1, error_message=None,
            )
        out = eq.aggregate_summary(window_days=7)
        assert out["execute_market"]["count"] == 1
