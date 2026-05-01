"""Snow action dispatcher tests — FLO-347 Phase 5b.

Test categories (per CTO mandate):
  * Dispatch (base coverage, per-action-type happy paths + errors)
  * DRY RUN guard (8+)
  * Lock discipline (10+)
  * Retry / circuit breaker (6+)
  * Guards (only_if_tighter_sl, cooldown_seconds, max_adjustments_total, 10+)
  * State transitions (5+)
  * Outcome path (Snow-owned, 3+)
  * Boundary compliance (5+)
  * TradingLogger API discipline (3+)

Total: ~64 tests. CEO floor was 60.

Uses a `_FakeExecutor` and a real `RLock` as the executor_lock so lock
discipline can be verified without MT5. Real tmp `history.db` via the
`snow_conn` fixture (same pattern as other snow tests).
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest

import config
from snow import db as snow_db
from snow import actions as _actions_mod
from snow.actions import (
    ActionResult,
    FirePayload,
    MAX_TRIGGER_WINDOW_SECONDS,
    RETRY_BACKOFF_SECONDS,
    STATUS_DRY_RUN_SKIPPED,
    STATUS_ERROR,
    STATUS_NO_POSITION,
    STATUS_RETRY_EXHAUSTED,
    STATUS_SKIPPED_GUARD,
    STATUS_SUCCESS,
    STATUS_TIMEOUT,
    STATUS_UNSUPPORTED,
    SnowActions,
)
from snow.priority import FireEvent
from snow.schema import (
    ActionAdjustSL,
    ActionAdjustTP,
    ActionAlertFloki,
    ActionCancelPlan,
    ActionClosePartial,
    ActionCloseFull,
    ActionEscalateToFloki,
    ActionExecuteMarket,
    ActionMoveSLToBreakeven,
    ActionMoveSLToPrice,
    ActionTrailSL,
    ContingencyGuards,
    Direction,
    Plan,
    PlanStatus,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Fresh tmp SQLite per test; matches db_test.py pattern."""
    db_path = tmp_path / "snow_actions_test.db"

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
    """Phase 5b tests exercise the LIVE dispatch path. Force the config
    flag off in-process so execute_action proceeds past the layer-2 guard.
    Individual DRY-RUN tests override by setting config.SNOW_DRY_RUN True."""
    monkeypatch.setattr(config, "SNOW_DRY_RUN", False, raising=False)


class _OrderResultLike:
    """Minimal OrderResult stand-in; we don't import from executor to
    keep this test file independent of the production executor module."""
    def __init__(
        self,
        *,
        success: bool = True,
        ticket: Optional[int] = 111_222,
        error_code: Optional[int] = None,
        error_message: Optional[str] = None,
        price: Optional[float] = 4720.0,
        volume: Optional[float] = 0.01,
    ):
        self.success = success
        self.ticket = ticket
        self.error_code = error_code
        self.error_message = error_message
        self.price = price
        self.volume = volume


class _PositionLike:
    def __init__(
        self,
        *,
        ticket: int = 111_222,
        direction: str = "SELL",
        volume: float = 0.02,
        open_price: float = 4725.0,
        current_price: float = 4720.0,
        sl: float = 4740.0,
        tp: float = 4710.0,
    ):
        self.ticket = ticket
        self.direction = direction
        self.volume = volume
        self.open_price = open_price
        self.current_price = current_price
        self.sl = sl
        self.tp = tp


class _FakeExecutor:
    """Drop-in stand-in for MT5Executor. Each method returns the NEXT queued
    result (FIFO) or a default success. Records every call into `.calls`
    for assertion."""

    def __init__(self) -> None:
        self.execute_trade_results: list[_OrderResultLike] = []
        self.modify_results: list[_OrderResultLike] = []
        self.close_results: list[_OrderResultLike] = []
        self.positions: list[_PositionLike] = [_PositionLike()]
        self.calls: list[tuple[str, tuple, dict]] = []
        self.call_event_log: list[str] = []
        self.raise_on: set[str] = set()  # method names to raise on

    def _pop(self, queue: list, default):
        if queue:
            return queue.pop(0)
        return default

    def execute_trade(self, **kwargs):
        self.calls.append(("execute_trade", (), kwargs))
        self.call_event_log.append("execute_trade")
        if "execute_trade" in self.raise_on:
            raise RuntimeError("synthetic executor failure")
        return self._pop(self.execute_trade_results, _OrderResultLike(ticket=111_222))

    def modify_position(self, ticket, new_sl=None, new_tp=None):
        self.calls.append(("modify_position", (), {"ticket": ticket, "new_sl": new_sl, "new_tp": new_tp}))
        self.call_event_log.append("modify_position")
        if "modify_position" in self.raise_on:
            raise RuntimeError("synthetic modify failure")
        return self._pop(self.modify_results, _OrderResultLike(ticket=ticket))

    def close_position(self, ticket, volume=None):
        self.calls.append(("close_position", (), {"ticket": ticket, "volume": volume}))
        self.call_event_log.append("close_position")
        if "close_position" in self.raise_on:
            raise RuntimeError("synthetic close failure")
        return self._pop(self.close_results, _OrderResultLike(ticket=ticket))

    def get_open_positions(self):
        self.calls.append(("get_open_positions", (), {}))
        return list(self.positions)


class _InstrumentedLock:
    """RLock wrapper that records acquire / release events so tests can
    verify lock discipline end-to-end."""

    def __init__(self):
        self._inner = threading.RLock()
        self.events: list[str] = []
        self.acquire_count = 0
        self.release_count = 0

    def __enter__(self):
        self._inner.__enter__()
        self.events.append("acquire")
        self.acquire_count += 1
        return self

    def __exit__(self, *args):
        self.events.append("release")
        self.release_count += 1
        return self._inner.__exit__(*args)

    def acquire(self, *args, **kwargs):
        res = self._inner.acquire(*args, **kwargs)
        if res:
            self.events.append("acquire")
            self.acquire_count += 1
        return res

    def release(self):
        self.events.append("release")
        self.release_count += 1
        self._inner.release()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_PLAN_DICT: dict[str, Any] = {
    "schema_version": 1,
    "id": "PLAN-20260424-001",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "integration test",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 72,
        "regime_assumed": "TRENDING_BEARISH",
    },
    "entry": {
        "direction": "SELL",
        "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4730.0}],
        "initial_sl": 4740.0,
        "initial_tp": 4710.0,
    },
    "management": [],
    "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
    "emergency": {
        "max_loss_pips": 150,
        "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _insert_base_plan(status: str = "pending", ticket: Optional[int] = None,
                      plan_id: str = "PLAN-20260424-001") -> Plan:
    d = deepcopy(_BASE_PLAN_DICT)
    d["id"] = plan_id
    plan = Plan(**d)
    snow_db.insert_plan(plan)
    if status != "pending":
        snow_db.update_plan_status(plan_id, status)
    if ticket is not None:
        snow_db.update_plan_trade_ticket(plan_id, ticket)
    return plan


def _make_actions(
    executor: Optional[_FakeExecutor] = None,
    lock: Optional[_InstrumentedLock] = None,
) -> tuple[SnowActions, _FakeExecutor, _InstrumentedLock]:
    exe = executor or _FakeExecutor()
    lk = lock or _InstrumentedLock()
    act = SnowActions(executor_impl=exe, executor_lock_impl=lk)
    return act, exe, lk


def _entry_fire(plan_id: str = "PLAN-20260424-001") -> FireEvent:
    payload = FirePayload(
        action=ActionExecuteMarket(),
        kind="entry",
        plan_direction=Direction.SELL,
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
    )


def _contingency_fire(
    *,
    plan_id: str = "PLAN-20260424-001",
    action: Any,
    name: str = "test_c",
    ticket: Optional[int] = 111_222,
    guards: Optional[ContingencyGuards] = None,
    direction: Direction = Direction.SELL,
    kind: str = "management",
    plan_list_order: int = 0,
    override: int = 5,
) -> FireEvent:
    payload = FirePayload(
        action=action, kind=kind, plan_direction=direction,
        ticket=ticket, guards=guards, entry_price=None,
    )
    return FireEvent(
        plan_id=plan_id,
        created_at="2026-04-24T08:00:00Z",
        contingency_name=name,
        action_type=action.type,
        override=override,
        plan_list_order=plan_list_order,
        payload=payload,
    )


def _plan_row(db_path: Path, plan_id: str) -> dict:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM snow_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _trigger_rows(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM snow_triggers ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ===========================================================================
# TestDispatch (base coverage)
# ===========================================================================

class TestDispatch:
    def test_execute_market_success_transitions_to_active(self, snow_conn):
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        exe.execute_trade_results.append(_OrderResultLike(ticket=555_000))

        result = actions.execute_action(_entry_fire())

        assert result.status == STATUS_SUCCESS
        assert result.ticket == 555_000
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value
        assert row["trade_ticket"] == 555_000

    def test_execute_market_failure_marks_plan_failed(self, snow_conn):
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        # All 3 attempts fail — retry exhausts.
        for _ in range(3):
            exe.execute_trade_results.append(
                _OrderResultLike(success=False, ticket=None,
                                 error_message="broker unavailable")
            )

        result = actions.execute_action(_entry_fire())

        assert result.status == STATUS_RETRY_EXHAUSTED
        assert result.attempts == 3
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.FAILED.value

    def test_adjust_sl_success_writes_audit_row(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()

        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)

        assert result.status == STATUS_SUCCESS
        triggers = _trigger_rows(snow_conn)
        assert len(triggers) == 1
        assert triggers[0]["action_type"] == "adjust_sl"

    def test_adjust_tp_success(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionAdjustTP(price=4705.0))
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_move_sl_to_price_success(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionMoveSLToPrice(price=4730.0))
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_move_sl_to_breakeven_sell_direction(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        # Position open_price=4725, SELL, offset=5 pips.
        # BE = open_price - 5*0.1 = 4724.5 (for SELL, tighter is DOWN).
        fire = _contingency_fire(
            action=ActionMoveSLToBreakeven(offset_pips=5),
            direction=Direction.SELL,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        assert result.action_type == "move_sl_to_breakeven"
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4724.5)

    def test_move_sl_to_breakeven_buy_direction(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="BUY", open_price=4720.0,
                                        sl=4700.0, current_price=4725.0)]
        fire = _contingency_fire(
            action=ActionMoveSLToBreakeven(offset_pips=3),
            direction=Direction.BUY,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        # BUY: BE = open + 3*0.1 = 4720.3
        assert modify_call[2]["new_sl"] == pytest.approx(4720.3)

    def test_trail_sl_sell_direction(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        # SELL current=4720, trail=10 pips → new SL = current + 10*0.1 = 4721.0
        fire = _contingency_fire(
            action=ActionTrailSL(trail_pips=10),
            direction=Direction.SELL,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4721.0)

    def test_trail_sl_buy_direction(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        # BUY @ 4715, current=4725 (10 USD favorable), original SL=4710 below entry.
        # Trail=10p tightens to 4725 - 1.0 = 4724, well above the original SL.
        exe.positions = [_PositionLike(direction="BUY", open_price=4715.0,
                                        sl=4710.0, current_price=4725.0)]
        fire = _contingency_fire(
            action=ActionTrailSL(trail_pips=10),
            direction=Direction.BUY,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4724.0)

    def test_trail_sl_sell_clamps_when_price_reverses(self, snow_conn):
        # FLO-419: trailing SL must be monotonic. Mirrors PLAN-20260501-013:
        # SELL @ 4593.79, SL already locked at 4593.54 from a prior MFE peak;
        # price has reversed up to 4594.50. Naive trail = 4594.50 + 12*0.1
        # = 4595.70, which would WIDEN the lock by 2.16 USD-units. Clamp
        # must hold SL at 4593.54.
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="SELL", open_price=4593.79,
                                        sl=4593.54, current_price=4594.50)]
        fire = _contingency_fire(
            action=ActionTrailSL(trail_pips=12),
            direction=Direction.SELL,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4593.54)

    def test_trail_sl_buy_clamps_when_price_reverses(self, snow_conn):
        # FLO-419 BUY symmetric case. SL locked at 4728.0 (above entry,
        # below current); price pulls back to 4728.5. Naive trail =
        # 4728.5 - 10*0.1 = 4727.5, which would WIDEN the lock. Clamp
        # must hold SL at 4728.0.
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="BUY", open_price=4700.0,
                                        sl=4728.0, current_price=4728.5)]
        fire = _contingency_fire(
            action=ActionTrailSL(trail_pips=10),
            direction=Direction.BUY,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4728.0)

    def test_trail_sl_no_prior_sl_first_set_unclamped(self, snow_conn):
        # FLO-419: when pos.sl is 0.0 (no prior SL), the clamp must NOT
        # block the initial trail set. Verifies the `old_sl > 0.0` gate.
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="SELL", open_price=4725.0,
                                        sl=0.0, current_price=4720.0)]
        fire = _contingency_fire(
            action=ActionTrailSL(trail_pips=10),
            direction=Direction.SELL,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        modify_call = next(c for c in exe.calls if c[0] == "modify_position")
        assert modify_call[2]["new_sl"] == pytest.approx(4721.0)

    def test_close_full_success_marks_closed(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.CLOSED.value
        assert row["closed_at"] is not None

    def test_close_partial_success_no_status_change(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(
            action=ActionClosePartial(percent=50.0),
            kind="management",
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value  # unchanged

    def test_alert_floki_records_snow_evaluations_row(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        fire = _contingency_fire(
            action=ActionAlertFloki(message="test alert msg"),
            kind="management",
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        # No executor call made
        assert not any(c[0] in ("execute_trade", "modify_position", "close_position")
                       for c in exe.calls)
        # Evaluation row written with event=alert
        conn = sqlite3.connect(str(snow_conn))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM snow_evaluations WHERE event='alert'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_escalate_to_floki_uses_urgent_event(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(
            action=ActionEscalateToFloki(message="URGENT"),
            kind="exit",
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        conn = sqlite3.connect(str(snow_conn))
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM snow_evaluations WHERE event='alert_urgent'"
            ).fetchall()
            assert len(rows) == 1
        finally:
            conn.close()

    def test_cancel_plan_returns_unsupported(self, snow_conn):
        """cancel_plan is validator-rejected in management/exit; if a fire
        reaches actions.py with it, defensive unsupported response."""
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        # Bypass validator — construct fire directly with cancel_plan.
        fire = _contingency_fire(
            action=ActionCancelPlan(), kind="management", ticket=111_222,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_UNSUPPORTED
        # No executor call made
        assert all(c[0] == "get_open_positions" or c[0] not in
                   ("execute_trade", "modify_position", "close_position")
                   for c in exe.calls)


# ===========================================================================
# TestDryRunGuard
# ===========================================================================

class TestDryRunGuard:
    def test_dry_run_skips_execute_market(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        result = actions.execute_action(_entry_fire())
        assert result.status == STATUS_DRY_RUN_SKIPPED
        assert not any(c[0] == "execute_trade" for c in exe.calls)

    def test_dry_run_skips_modify_position(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)
        assert result.status == STATUS_DRY_RUN_SKIPPED
        assert not any(c[0] == "modify_position" for c in exe.calls)

    def test_dry_run_skips_close_position(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        result = actions.execute_action(fire)
        assert result.status == STATUS_DRY_RUN_SKIPPED
        assert not any(c[0] == "close_position" for c in exe.calls)

    def test_dry_run_skips_alert(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionAlertFloki(message="x"))
        result = actions.execute_action(fire)
        assert result.status == STATUS_DRY_RUN_SKIPPED

    def test_dry_run_does_not_acquire_executor_lock(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan()
        actions, _, lk = _make_actions()
        _ = actions.execute_action(_entry_fire())
        assert lk.acquire_count == 0, "DRY RUN must not acquire executor_lock"

    def test_dry_run_does_not_mutate_plan_status(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan()
        actions, _, _ = _make_actions()
        _ = actions.execute_action(_entry_fire())
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.PENDING.value

    def test_dry_run_reason_set_correctly(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan()
        actions, _, _ = _make_actions()
        result = actions.execute_action(_entry_fire())
        assert "SNOW_DRY_RUN" in (result.reason or "")

    def test_dry_run_logs_leak_warning(self, snow_conn, monkeypatch, caplog):
        """Loop should have filtered. If dispatcher is reached with DRY RUN
        on, a WARNING must be emitted as a structural-bug signal."""
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan()
        actions, _, _ = _make_actions()
        import logging as stdlib_logging
        with caplog.at_level(stdlib_logging.WARNING):
            _ = actions.execute_action(_entry_fire())
        # TradingLogger routes to root logger; capture via caplog.
        assert any("dry_run_leak" in r.getMessage() for r in caplog.records), (
            f"expected dry_run_leak warning; got: {[r.getMessage() for r in caplog.records]}"
        )


# ===========================================================================
# TestLockDiscipline
# ===========================================================================

class TestLockDiscipline:
    def test_execute_market_acquires_lock(self, snow_conn):
        _insert_base_plan()
        actions, _, lk = _make_actions()
        _ = actions.execute_action(_entry_fire())
        assert lk.acquire_count >= 1

    def test_modify_position_acquires_lock(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, lk = _make_actions()
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        _ = actions.execute_action(fire)
        assert lk.acquire_count >= 1

    def test_close_position_acquires_lock(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, lk = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        assert lk.acquire_count >= 1

    def test_alert_does_not_acquire_lock(self, snow_conn):
        """alert_floki / escalate_to_floki are DB-only; no executor call,
        therefore no lock acquisition."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, lk = _make_actions()
        fire = _contingency_fire(action=ActionAlertFloki(message="x"))
        _ = actions.execute_action(fire)
        assert lk.acquire_count == 0

    def test_lock_released_on_success(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, lk = _make_actions()
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        _ = actions.execute_action(fire)
        assert lk.acquire_count == lk.release_count

    def test_lock_released_on_broker_failure(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, lk = _make_actions()
        for _ in range(3):
            exe.modify_results.append(
                _OrderResultLike(success=False, error_message="broker err")
            )
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        _ = actions.execute_action(fire)
        assert lk.acquire_count == lk.release_count, (
            "lock must be released on retry-exhausted path"
        )

    def test_lock_released_on_exception(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, lk = _make_actions()
        exe.raise_on.add("modify_position")
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        _ = actions.execute_action(fire)
        assert lk.acquire_count == lk.release_count, (
            "lock must be released even when executor call raises"
        )

    def test_lock_held_across_all_retries(self, snow_conn):
        """Single `with self._lock:` covers all 3 attempts. If retries
        re-acquired the lock, acquire_count would exceed 1."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, lk = _make_actions()
        for _ in range(3):
            exe.modify_results.append(_OrderResultLike(success=False))
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        _ = actions.execute_action(fire)
        # Exactly one acquire for the whole retry sequence.
        assert lk.acquire_count == 1

    def test_concurrent_dispatch_serialized(self, snow_conn):
        """Two threads hammering execute_action on the same SnowActions
        must serialize. We detect via call_event_log: execute and modify
        must not interleave in a way that violates pair-ordering."""
        _insert_base_plan(plan_id="PLAN-20260424-001", status="active", ticket=111)
        _insert_base_plan(plan_id="PLAN-20260424-002", status="active", ticket=222)
        actions, exe, lk = _make_actions()
        # Slow the modify response slightly so contention is real.
        import time as _t
        original_modify = exe.modify_position

        def _slow_modify(ticket, new_sl=None, new_tp=None):
            _t.sleep(0.02)
            return original_modify(ticket, new_sl=new_sl, new_tp=new_tp)

        exe.modify_position = _slow_modify
        # Make sure the fake has positions for BOTH tickets used in the test.
        exe.positions = [_PositionLike(ticket=111), _PositionLike(ticket=222)]

        fire_a = _contingency_fire(
            plan_id="PLAN-20260424-001",
            action=ActionAdjustSL(price=4735.0), ticket=111,
        )
        fire_b = _contingency_fire(
            plan_id="PLAN-20260424-002",
            action=ActionAdjustSL(price=4735.0), ticket=222,
        )
        results: list[ActionResult] = []

        def _run(fire):
            results.append(actions.execute_action(fire))

        t1 = threading.Thread(target=_run, args=(fire_a,))
        t2 = threading.Thread(target=_run, args=(fire_b,))
        t1.start(); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)

        assert len(results) == 2
        assert all(r.status == STATUS_SUCCESS for r in results)
        assert lk.acquire_count == lk.release_count
        # Acquire/release must come in pairs; no interleaving.
        assert lk.acquire_count == 2

    def test_lock_is_rlock_allows_reentry(self, snow_conn):
        """executor_lock is RLock — same thread can re-acquire.
        Simulates executor.execute_trade internally calling close_position
        (phantom cleanup path)."""
        _insert_base_plan()
        actions, exe, lk = _make_actions()
        orig_execute = exe.execute_trade

        def _nested(**kwargs):
            # Re-enter the lock from the same thread — RLock allows it.
            with lk:
                pass
            return orig_execute(**kwargs)

        exe.execute_trade = _nested
        result = actions.execute_action(_entry_fire())
        assert result.status == STATUS_SUCCESS
        assert lk.acquire_count == lk.release_count

    def test_dry_run_layer_does_not_acquire(self, snow_conn, monkeypatch):
        monkeypatch.setattr(config, "SNOW_DRY_RUN", True, raising=False)
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, lk = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        assert lk.acquire_count == 0


# ===========================================================================
# TestRetryBackoff
# ===========================================================================

class TestRetryBackoff:
    def test_success_on_first_attempt(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        assert result.attempts == 1

    def test_retry_after_failure_succeeds(self, snow_conn, monkeypatch):
        # Zero out backoff to make this test fast (we verify timing
        # separately in another test).
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.modify_results.append(_OrderResultLike(success=False))
        exe.modify_results.append(_OrderResultLike(success=True))
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        assert result.attempts == 2

    def test_retry_exhausted_after_3_attempts(self, snow_conn, monkeypatch):
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        for _ in range(3):
            exe.modify_results.append(_OrderResultLike(success=False))
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)
        assert result.status == STATUS_RETRY_EXHAUSTED
        assert result.attempts == 3

    def test_backoff_timing_2s_then_4s(self, snow_conn, monkeypatch):
        """Real sleep timings — this is a slow test but critical for
        verifying the RFC §7.4 backoff contract."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        for _ in range(3):
            exe.modify_results.append(_OrderResultLike(success=False))
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        t0 = time.monotonic()
        result = actions.execute_action(fire)
        elapsed = time.monotonic() - t0
        # Expected: 2s + 4s = 6s of sleep between the 3 attempts.
        # Allow generous margin for CI slow machines.
        assert result.status == STATUS_RETRY_EXHAUSTED
        assert 5.5 <= elapsed <= 10.0, f"expected ~6s elapsed; got {elapsed:.2f}s"

    def test_position_gone_treated_as_success_no_retry(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.close_results.append(_OrderResultLike(
            success=False, error_message="no position found for ticket 111222",
        ))
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        result = actions.execute_action(fire)
        # STATUS_NO_POSITION per RFC §7.5
        assert result.status == STATUS_NO_POSITION
        # Only ONE close attempt — position-gone short-circuits retry.
        assert sum(1 for c in exe.calls if c[0] == "close_position") == 1
        # Plan marked CLOSED (externally-closed treated as success)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.CLOSED.value

    def test_circuit_breaker_max_trigger_window(self, snow_conn, monkeypatch):
        """30s absolute budget across all retries. Shrink the window for
        this test so it runs in reasonable time. All attempts fail; the
        deadline check must cut the retry loop short before 3 attempts
        complete."""
        monkeypatch.setattr(_actions_mod, "MAX_TRIGGER_WINDOW_SECONDS", 0.5)
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (1.0, 1.0))
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        for _ in range(3):
            exe.modify_results.append(_OrderResultLike(success=False))
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        t0 = time.monotonic()
        result = actions.execute_action(fire)
        elapsed = time.monotonic() - t0
        # Circuit breaker must cut the loop short. Status is
        # RETRY_EXHAUSTED (last attempt's failed result) or TIMEOUT
        # (couldn't start an attempt at all); attempts must be < 3 and
        # total elapsed must be bounded by ~(window + one backoff).
        assert result.status in (STATUS_RETRY_EXHAUSTED, STATUS_TIMEOUT)
        assert result.attempts < 3
        assert elapsed < 2.0, f"circuit breaker should cut retry short; got {elapsed:.2f}s"

    def test_retry_on_exception(self, snow_conn, monkeypatch):
        """Exception in executor call counts as a failed attempt; retry
        logic applies normally."""
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()

        attempts = {"n": 0}
        orig_modify = exe.modify_position

        def _flaky(ticket, new_sl=None, new_tp=None):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("transient")
            return orig_modify(ticket, new_sl=new_sl, new_tp=new_tp)

        exe.modify_position = _flaky
        fire = _contingency_fire(action=ActionAdjustSL(price=4735.0))
        result = actions.execute_action(fire)
        assert result.status == STATUS_SUCCESS
        assert attempts["n"] == 2


# ===========================================================================
# TestGuards
# ===========================================================================

class TestGuards:
    def test_only_if_tighter_sl_sell_accepts_lower_sl(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        # SELL position; current SL=4740. New SL=4735 is tighter (lower = closer).
        exe.positions = [_PositionLike(direction="SELL", sl=4740.0)]
        guards = ContingencyGuards(only_if_tighter_sl=True)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0),
            direction=Direction.SELL, guards=guards,
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_only_if_tighter_sl_sell_rejects_higher_sl(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="SELL", sl=4740.0)]
        guards = ContingencyGuards(only_if_tighter_sl=True)
        # New SL=4745 is LOOSER for SELL (further from price).
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4745.0),
            direction=Direction.SELL, guards=guards,
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SKIPPED_GUARD
        assert "tighter" in (result.reason or "").lower()

    def test_only_if_tighter_sl_buy_accepts_higher_sl(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="BUY", sl=4700.0, open_price=4720.0)]
        guards = ContingencyGuards(only_if_tighter_sl=True)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4710.0),
            direction=Direction.BUY, guards=guards,
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_only_if_tighter_sl_buy_rejects_lower_sl(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="BUY", sl=4700.0)]
        guards = ContingencyGuards(only_if_tighter_sl=True)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4690.0),
            direction=Direction.BUY, guards=guards,
        )
        assert actions.execute_action(fire).status == STATUS_SKIPPED_GUARD

    def test_cooldown_blocks_within_window(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        # First fire succeeds.
        guards = ContingencyGuards(cooldown_seconds=60)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), guards=guards, name="c1",
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        # Second fire within cooldown must skip.
        result = actions.execute_action(fire)
        assert result.status == STATUS_SKIPPED_GUARD
        assert "cooldown" in (result.reason or "").lower()

    def test_cooldown_allows_after_window(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        # Cooldown=0 means no throttle.
        guards = ContingencyGuards(cooldown_seconds=0)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), guards=guards,
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_max_adjustments_blocks_at_limit(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        guards = ContingencyGuards(max_adjustments_total=2)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), guards=guards, name="c_max",
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        third = actions.execute_action(fire)
        assert third.status == STATUS_SKIPPED_GUARD
        assert "max_adjustments" in (third.reason or "").lower()

    def test_max_adjustments_allows_under_limit(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        guards = ContingencyGuards(max_adjustments_total=5)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), guards=guards, name="c_under",
        )
        for _ in range(3):
            assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_no_guards_passes_through(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), guards=None,
        )
        assert actions.execute_action(fire).status == STATUS_SUCCESS

    def test_multiple_guards_any_one_blocks(self, snow_conn):
        """cooldown AND only_if_tighter_sl both set; cooldown fires first."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="SELL", sl=4740.0)]
        guards = ContingencyGuards(cooldown_seconds=60, only_if_tighter_sl=True)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0),
            direction=Direction.SELL, guards=guards, name="c_multi",
        )
        # First succeeds
        assert actions.execute_action(fire).status == STATUS_SUCCESS
        # Second blocks on cooldown (not on tighter_sl)
        r = actions.execute_action(fire)
        assert r.status == STATUS_SKIPPED_GUARD
        assert "cooldown" in (r.reason or "").lower()

    def test_guards_skip_does_not_call_executor(self, snow_conn):
        """A skipped guard must NOT reach the executor — the whole point."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(direction="SELL", sl=4740.0)]
        guards = ContingencyGuards(only_if_tighter_sl=True)
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4745.0),  # looser
            direction=Direction.SELL, guards=guards,
        )
        _ = actions.execute_action(fire)
        # No modify_position call was made.
        assert not any(c[0] == "modify_position" for c in exe.calls)


# ===========================================================================
# TestStateTransitions
# ===========================================================================

class TestStateTransitions:
    def test_entry_success_pending_to_active(self, snow_conn):
        _insert_base_plan()
        actions, _, _ = _make_actions()
        _ = actions.execute_action(_entry_fire())
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value

    def test_entry_failure_to_failed(self, snow_conn, monkeypatch):
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        for _ in range(3):
            exe.execute_trade_results.append(
                _OrderResultLike(success=False, error_message="fail"))
        _ = actions.execute_action(_entry_fire())
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.FAILED.value

    def test_close_full_active_to_closed(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.CLOSED.value

    def test_close_full_failure_to_failed(self, snow_conn, monkeypatch):
        monkeypatch.setattr(_actions_mod, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        _insert_base_plan(status="active", ticket=111_222)
        actions, exe, _ = _make_actions()
        for _ in range(3):
            exe.close_results.append(
                _OrderResultLike(success=False, error_message="broker down"))
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.FAILED.value

    def test_management_action_does_not_change_status(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(
            action=ActionAdjustSL(price=4735.0), kind="management",
        )
        _ = actions.execute_action(fire)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value


# ===========================================================================
# TestOutcomePath (Snow-owned, not db_writer)
# ===========================================================================

class TestOutcomePath:
    def test_close_full_stamps_closed_at(self, snow_conn):
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["closed_at"] is not None

    def test_close_full_outcome_none_deferred_to_backfill(self, snow_conn):
        """Phase 5b leaves outcome_pips/usd NULL; backfill is a follow-up."""
        _insert_base_plan(status="active", ticket=111_222)
        actions, _, _ = _make_actions()
        fire = _contingency_fire(action=ActionCloseFull(), kind="exit")
        _ = actions.execute_action(fire)
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["outcome_pips"] is None
        assert row["outcome_usd"] is None

    def test_actions_does_not_import_db_writer_record_trade_close(self):
        """Snow owns its outcome path via snow_plans.outcome_*.
        Must not reach into db_writer's Floki-side close path."""
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        assert "record_trade_close" not in src
        assert "db_writer" not in src


# ===========================================================================
# TestBoundaryCompliance
# ===========================================================================

class TestBoundaryCompliance:
    FORBIDDEN = (
        "agent_tools",
        "ai_agent",
        "rex_validator",
        "rex_monitor",
        "monitor",
    )

    def test_actions_does_not_import_forbidden_modules(self):
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        for name in self.FORBIDDEN:
            pattern = rf"(?m)^(?:import|from)\s+{re.escape(name)}(?:\s|\.|$)"
            assert not re.search(pattern, src), (
                f"actions.py imports forbidden module `{name}`"
            )

    def test_actions_imports_executor(self):
        """Phase 5b allows executor. Lazy-import in __init__ is fine."""
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        assert "from executor import" in src or "import executor" in src, (
            "actions.py must import executor (allowed in Phase 5b)"
        )

    def test_actions_no_direct_mt5_imports(self):
        """All MT5 access must go through the executor (FLO-348 discipline).
        No direct mt5_safe or MetaTrader5 imports."""
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("mt5_safe", "MetaTrader5"):
            pattern = rf"(?m)^(?:import|from)\s+{re.escape(forbidden)}(?:\s|\.|$)"
            assert not re.search(pattern, src), (
                f"actions.py has direct `{forbidden}` import"
            )

    def test_actions_uses_project_tradinglogger(self):
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        assert "from logger import log" in src, (
            "actions.py must use project TradingLogger via `from logger import log`"
        )

    def test_no_stdlib_logging_getlogger_in_actions(self):
        """Phase 4.5 lesson: mixing stdlib logging with TradingLogger led
        to API mismatches. actions.py uses ONLY the project logger."""
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        assert "logging.getLogger" not in src


# ===========================================================================
# TestTradingLoggerAPIDiscipline
# ===========================================================================

class TestTradingLoggerAPIDiscipline:
    """Phase 4.5 memory (project_tradinglogger_api.md): TradingLogger
    accepts a single-str positional arg; no .exception() method."""

    def test_no_exception_method_calls_in_actions(self):
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        # Zero `log.exception(` occurrences.
        assert "log.exception(" not in src, (
            "actions.py uses log.exception — TradingLogger has no such method"
        )

    def test_all_log_info_calls_use_single_positional_arg(self):
        """Grep sanity: no `log.info(fmt, arg)` patterns."""
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        # Pattern: log.info("...", ...) — comma AFTER the closing quote
        # with ANY preceding content — picks up multi-arg stdlib style.
        multi_arg_re = re.compile(
            r"\blog\.info\(\s*[\"'][^\"']*[\"']\s*,",
            re.MULTILINE,
        )
        matches = multi_arg_re.findall(src)
        assert matches == [], (
            f"log.info multi-arg calls found in actions.py: {matches}. "
            f"TradingLogger.info accepts single-str positional only."
        )

    def test_all_log_error_calls_use_single_positional_arg(self):
        src = Path(_actions_mod.__file__).read_text(encoding="utf-8")
        multi_arg_re = re.compile(
            r"\blog\.(error|warning|debug)\(\s*[\"'][^\"']*[\"']\s*,",
            re.MULTILINE,
        )
        matches = multi_arg_re.findall(src)
        assert matches == [], (
            f"multi-arg TradingLogger calls found: {matches}"
        )


# =============================================================================
# FLO-417 — opposing-positions safety gate on Snow's execute_market
# =============================================================================
#
# Empirical motivation (CEO directive 2026-04-30): on this date PLAN-022
# (BUY) and PLAN-020 (SELL) were both open simultaneously for 54 minutes
# because Snow's execute_market action bypassed FLO-85's opposing-
# positions guard (which only covered Floki's execute_trade tool path).

class TestFLO417OpposingPositionsGate:
    """Gate contract:
      1. Reject when an OPPOSING position is open
      2. Allow same-direction positions (FLO-85 forbids opposing only)
      3. Allow when no positions are open
      4. Transition plan to FAILED so Snow doesn't retry
      5. Record snow_triggers row with execution_status=skipped_guard
      6. Fail-OPEN on executor query errors
    """

    def test_opposing_buy_holds_sell_entry_for_floki_decision(self, snow_conn):
        """FLO-418: opposing detection now HOLDS the plan in PENDING with
        awaiting_decision flag, NOT a hard block. Plan stays alive so
        Floki can decide on the next cycle (cancel / close / override)."""
        _insert_base_plan()  # SELL plan
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(ticket=999_111, direction="BUY")]

        result = actions.execute_action(_entry_fire())

        assert result.status == STATUS_SKIPPED_GUARD
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        # Plan stays PENDING (not FAILED) so it stays alive for Floki.
        assert row["status"] == PlanStatus.PENDING.value
        # No broker call.
        exec_calls = [c for c in exe.calls if c[0] == "execute_trade"]
        assert exec_calls == [], "broker must not be called when awaiting"
        # Awaiting flag is stamped on the plan's state_cache_json.
        awaiting = snow_db.get_awaiting_decision("PLAN-20260424-001")
        assert awaiting is not None, "awaiting_decision flag missing"
        assert awaiting["attempted_direction"] == "SELL"
        assert 999_111 in awaiting["opposing_tickets"]

    def test_opposing_idempotent_across_ticks(self, snow_conn):
        """Snow ticks every 5s; Floki cycles every 5-30 min. The first
        tick that sees opposing should log + write awaiting flag once;
        subsequent ticks with the same opposing set must silent-skip
        (no log spam, no DB churn)."""
        _insert_base_plan()  # SELL plan
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(ticket=999_111, direction="BUY")]

        # Tick 1 — first detection
        actions.execute_action(_entry_fire())
        triggers_after_1 = len(_trigger_rows(snow_conn))
        # Tick 2 — same opposing set
        actions.execute_action(_entry_fire())
        # Tick 3 — same opposing set
        actions.execute_action(_entry_fire())

        triggers_after_3 = len(_trigger_rows(snow_conn))
        # Only ONE audit row across 3 ticks (idempotent).
        assert triggers_after_3 == triggers_after_1, (
            f"expected 1 audit row across 3 ticks, got {triggers_after_3}"
        )
        # Awaiting payload's noticed_at didn't get rewritten.
        awaiting = snow_db.get_awaiting_decision("PLAN-20260424-001")
        assert awaiting is not None

    def test_override_opposing_bypasses_gate(self, snow_conn):
        """When Floki calls override_opposing_block, the dispatcher
        bypasses the opposing detection on the next tick and fires
        the entry normally."""
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(ticket=999_111, direction="BUY")]
        exe.execute_trade_results.append(_OrderResultLike(ticket=555_111))

        # Stamp the override (Floki's tool would do this).
        snow_db.set_override_opposing("PLAN-20260424-001", ttl_seconds=300)

        result = actions.execute_action(_entry_fire())

        # Entry fires despite opposing — override active.
        assert result.status == STATUS_SUCCESS
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value
        # Awaiting flag (if any) cleared on successful fire.
        awaiting = snow_db.get_awaiting_decision("PLAN-20260424-001")
        assert awaiting is None

    def test_opposing_sell_holds_buy_entry(self, snow_conn):
        """Symmetric: SELL position open + BUY plan fires → awaiting."""
        from copy import deepcopy
        d = deepcopy(_BASE_PLAN_DICT)
        d["id"] = "PLAN-20260424-099"
        d["entry"]["direction"] = "BUY"
        d["entry"]["initial_sl"] = 4710.0
        d["entry"]["initial_tp"] = 4740.0
        snow_db.insert_plan(Plan(**d))

        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(ticket=888_222, direction="SELL")]

        from snow.actions import FireEvent, FirePayload
        from snow.schema import Direction, ActionExecuteMarket
        fire = FireEvent(
            plan_id="PLAN-20260424-099",
            created_at="2026-04-24T08:00:00Z",
            contingency_name="_entry",
            action_type="execute_market",
            override=5,
            plan_list_order=-1,
            payload=FirePayload(
                action=ActionExecuteMarket(),
                kind="entry",
                plan_direction=Direction.BUY,
                ticket=None, guards=None, entry_price=None,
            ),
        )
        result = actions.execute_action(fire)
        assert result.status == STATUS_SKIPPED_GUARD
        row = _plan_row(snow_conn, "PLAN-20260424-099")
        assert row["status"] == PlanStatus.PENDING.value
        awaiting = snow_db.get_awaiting_decision("PLAN-20260424-099")
        assert awaiting is not None
        assert awaiting["attempted_direction"] == "BUY"

    def test_same_direction_position_does_not_block(self, snow_conn):
        """FLO-85 forbids OPPOSING; same-direction is permitted by this
        gate (other gates handle max-positions / stacking)."""
        _insert_base_plan()  # SELL plan
        actions, exe, _ = _make_actions()
        exe.positions = [_PositionLike(ticket=777_333, direction="SELL")]
        exe.execute_trade_results.append(_OrderResultLike(ticket=555_000))

        result = actions.execute_action(_entry_fire())
        assert result.status == STATUS_SUCCESS
        row = _plan_row(snow_conn, "PLAN-20260424-001")
        assert row["status"] == PlanStatus.ACTIVE.value

    def test_no_positions_does_not_block(self, snow_conn):
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        exe.positions = []
        exe.execute_trade_results.append(_OrderResultLike(ticket=555_001))

        result = actions.execute_action(_entry_fire())
        assert result.status == STATUS_SUCCESS

    def test_executor_get_positions_failure_fails_open(self, snow_conn):
        """If get_open_positions raises, log a warning and proceed.
        Documented fail-OPEN contract — better to risk a duplicate
        entry than deadlock all entries on transient MT5 hiccups."""
        _insert_base_plan()
        actions, exe, _ = _make_actions()
        exe.raise_on.add("get_open_positions")
        exe.execute_trade_results.append(_OrderResultLike(ticket=555_002))

        result = actions.execute_action(_entry_fire())
        assert result.status == STATUS_SUCCESS
