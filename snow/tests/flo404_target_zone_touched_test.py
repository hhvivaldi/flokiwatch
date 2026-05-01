"""FLO-404 staleness signal — `target_zone_touched` field on
list_active_plans output.

Empirical motivation (CEO directive 2026-04-30): PLAN-20260429-012
sat as `pending` for hours after price had already reached its target
zone (4565-4572 thesis band; max-high 4574.14) and momentum reversed.
Floki had no surface telling him the thesis already played out, so he
kept the plan alive and missed the cancel-and-replace opportunity.

The fix: AgentTools._plan_target_zone_touched computes a per-plan
boolean by comparing the plan's directional target (max(key_levels)
for BUY, min(key_levels) for SELL) against MT5 high/low since the
plan's created_at. Surfaced in list_active_plans output.

This file pins:
  1. BUY plan: target reached → True
  2. BUY plan: target not reached → False
  3. SELL plan: target reached → True (mirror direction)
  4. Edge cases that should return None (no opinion)
  5. End-to-end: list_active_plans output carries the field.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from snow import db as snow_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "flo404_target_zone.db"

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
def tools(snow_conn):
    from agent_tools import AgentTools

    class _FakeBot:
        def __init__(self):
            self.symbol = "XAUUSD"
            self._last_agent_data = None
            self.running = True

    _STUB = object()
    t = AgentTools(
        bot=_FakeBot(), executor=_STUB,
        safety_checks_module=_STUB, risk_manager_module=_STUB,
    )
    t._recipe_pulls_count = 1  # FLO-393 gate satisfied
    return t


def _future_iso(hours: int = 6) -> str:
    t = datetime.now(timezone.utc) + timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_iso(minutes: int = 30) -> str:
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mk_plan(direction: str = "BUY", key_levels=None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "PLAN-00000000-000",
        "created_by": "floki",
        "created_at": "2026-04-30T00:00:00Z",
        "expires_at": "PLACEHOLDER",
        "status": "pending",
        "analysis": {
            "thesis": "FLO-404 staleness test plan",
            "key_levels": key_levels or [4543.0, 4553.0, 4565.0, 4572.0],
            "confidence": 75,
            "regime_assumed": "TRENDING_BEARISH",
        },
        "entry": {
            "direction": direction, "volume": 0.02,
            "conditions": [
                {"type": "price_above", "level": 4553.0},
                {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            ],
            "initial_sl": 4540.0, "initial_tp": 4570.0,
        },
        "management": [],
        "exit": [{
            "name": "fallback", "priority": 1,
            "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}],
            "action": {"type": "close_full"}, "fires": "once",
        }],
        "emergency": {
            "max_loss_pips": 150, "max_duration_minutes": 480,
            "on_broker_error": "alert_floki",
        },
    }


def _mock_rates(highs: list[float], lows: list[float], minutes_ago_start: int = 60):
    """Build a numpy-recarray-ish list of dict-like rates with
    `time` (broker epoch) and `high` / `low`. MT5 candle.time is
    broker-local; the helper subtracts _mt5_server_offset() to filter.
    For tests we stub _mt5_server_offset to 0, so candle.time IS UTC."""
    now = int(datetime.now(timezone.utc).timestamp())
    n = len(highs)
    rates = []
    for i in range(n):
        t = now - (minutes_ago_start - i) * 60
        rates.append({"time": t, "high": highs[i], "low": lows[i]})
    return rates


# ---------------------------------------------------------------------------
# Pure-function tests with mocked MT5
# ---------------------------------------------------------------------------


class TestTargetZoneTouchedHelper:
    """Test the static helper directly with mocked mt5.copy_rates_from_pos."""

    def _patch_mt5(self, rates):
        """Patch chain: mt5.copy_rates_from_pos returns `rates`,
        _mt5_server_offset returns 0 so candle.time IS UTC."""
        return [
            patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates),
            patch("executor._mt5_server_offset", return_value=0),
        ]

    def test_buy_plan_target_reached_returns_true(self):
        """BUY plan, key_levels max=4572, MT5 high reaches 4574.14
        since creation → True (PLAN-012 case)."""
        from agent_tools import AgentTools
        plan = _mk_plan(direction="BUY", key_levels=[4543.0, 4553.0, 4572.0])
        # Highs span 4555 to 4574.14 — max exceeds target 4572
        highs = [4555.0, 4560.0, 4565.0, 4570.0, 4574.14, 4571.0, 4567.0]
        lows = [4548.0] * len(highs)
        rates = _mock_rates(highs, lows, minutes_ago_start=20)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(minutes=30),
            )
        assert result is True, (
            "BUY target 4572 reached by max(highs)=4574.14 since create"
        )

    def test_buy_plan_target_not_reached_returns_false(self):
        """BUY plan, key_levels max=4600, MT5 high only reaches 4574 → False."""
        from agent_tools import AgentTools
        plan = _mk_plan(direction="BUY", key_levels=[4543.0, 4600.0])
        highs = [4555.0, 4570.0, 4574.0, 4571.0, 4567.0]
        lows = [4548.0] * len(highs)
        rates = _mock_rates(highs, lows, minutes_ago_start=20)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(minutes=30),
            )
        assert result is False, (
            "BUY target 4600 NOT reached by max(highs)=4574"
        )

    def test_sell_plan_target_reached_returns_true(self):
        """SELL plan, key_levels min=4500, MT5 low reaches 4498 → True."""
        from agent_tools import AgentTools
        plan = _mk_plan(direction="SELL", key_levels=[4500.0, 4520.0, 4543.0])
        highs = [4540.0] * 5
        lows = [4530.0, 4520.0, 4510.0, 4498.0, 4505.0]
        rates = _mock_rates(highs, lows, minutes_ago_start=20)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(minutes=30),
            )
        assert result is True, (
            "SELL target 4500 reached by min(lows)=4498"
        )

    def test_sell_plan_target_not_reached_returns_false(self):
        """SELL plan, key_levels min=4400, MT5 low only reaches 4498 → False."""
        from agent_tools import AgentTools
        plan = _mk_plan(direction="SELL", key_levels=[4400.0, 4500.0])
        highs = [4540.0] * 5
        lows = [4530.0, 4520.0, 4510.0, 4498.0, 4505.0]
        rates = _mock_rates(highs, lows, minutes_ago_start=20)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(minutes=30),
            )
        assert result is False

    def test_filter_excludes_pre_creation_candles(self):
        """Candles BEFORE plan.created_at must be filtered out. If the
        only high above target is in a pre-creation candle, target was
        NOT touched since creation → False."""
        from agent_tools import AgentTools
        plan = _mk_plan(direction="BUY", key_levels=[4572.0])
        # First candle is 60min ago (= 30min BEFORE plan creation 30min ago).
        # Its high spikes above target. Subsequent candles stay below.
        highs = [4580.0, 4555.0, 4560.0, 4565.0]  # spike at index 0
        lows = [4550.0] * 4
        rates = _mock_rates(highs, lows, minutes_ago_start=60)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(minutes=30),  # plan created 30min ago
            )
        assert result is False, (
            "spike above target was in a pre-creation candle and must "
            "be filtered out"
        )


class TestNoOpinionEdgeCases:
    """Cases that should return None (no opinion) rather than False."""

    def test_no_plan_dict_returns_none(self):
        from agent_tools import AgentTools
        assert AgentTools._plan_target_zone_touched(None, "2026-04-30T00:00:00Z") is None
        assert AgentTools._plan_target_zone_touched({}, "2026-04-30T00:00:00Z") is None

    def test_no_created_at_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        assert AgentTools._plan_target_zone_touched(plan, None) is None
        assert AgentTools._plan_target_zone_touched(plan, "") is None

    def test_no_direction_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        plan["entry"]["direction"] = "INVALID"
        assert AgentTools._plan_target_zone_touched(plan, _past_iso(30)) is None

    def test_no_key_levels_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        plan["analysis"]["key_levels"] = []
        assert AgentTools._plan_target_zone_touched(plan, _past_iso(30)) is None

    def test_unparseable_created_at_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        assert AgentTools._plan_target_zone_touched(plan, "garbage-not-iso") is None

    def test_too_old_plan_returns_none(self):
        """Plans older than 24h skip the check (cost-bound)."""
        from agent_tools import AgentTools
        plan = _mk_plan()
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert AgentTools._plan_target_zone_touched(plan, old) is None

    def test_future_dated_plan_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        assert AgentTools._plan_target_zone_touched(plan, future) is None

    def test_mt5_returns_empty_returns_none(self):
        from agent_tools import AgentTools
        plan = _mk_plan()
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=[]):
            result = AgentTools._plan_target_zone_touched(
                plan, _past_iso(30),
            )
        assert result is None


# ---------------------------------------------------------------------------
# End-to-end — list_active_plans surfaces the field
# ---------------------------------------------------------------------------


class TestListActivePlansSurfacesTargetTouched:
    """Confirm the field appears in list_active_plans output dict
    alongside the existing FLO-404 duplicate-avoidance fields."""

    def test_field_present_in_output(self, tools):
        # Submit a plan; doesn't matter what target_zone_touched
        # resolves to (likely None in test env without MT5) — just
        # that the field is part of the contract.
        plan = _mk_plan()
        plan["expires_at"] = _future_iso(6)
        # Need to use the canonical _plan_dict construction; reuse
        # by calling submit_plan_to_snow directly.
        result = tools.submit_plan_to_snow(plan=plan)
        assert result["success"] is True

        listed = tools.list_active_plans()
        assert listed["success"] is True
        assert listed["count"] == 1
        p = listed["plans"][0]
        assert "target_zone_touched" in p, (
            "FLO-404: list_active_plans must surface target_zone_touched "
            "field per CEO directive 2026-04-30"
        )
        # Without MT5 mocked at this layer, value is likely None.
        # The contract test is on the FIELD's presence, not its truth.
        assert p["target_zone_touched"] in (True, False, None)

    def test_field_resolves_to_true_with_mocked_mt5_high(self, tools):
        """Patch MT5 to return a high above the plan's max(key_levels)
        — list_active_plans must surface target_zone_touched: True.

        Note: submit_plan_to_snow stamps created_at to "now", which
        would filter out all mocked candles. We backdate the row's
        created_at to 30min ago so the candle filter has a window
        to evaluate."""
        plan = _mk_plan(direction="BUY", key_levels=[4543.0, 4572.0])
        plan["expires_at"] = _future_iso(6)
        result = tools.submit_plan_to_snow(plan=plan)
        assert result["success"] is True
        plan_id = result["plan_id"]

        # Backdate created_at so the mocked candles fall AFTER it.
        backdated = _past_iso(minutes=30)
        conn = snow_db._connect()
        try:
            conn.execute(
                "UPDATE snow_plans SET created_at = ? WHERE id = ?",
                (backdated, plan_id),
            )
            conn.commit()
        finally:
            conn.close()

        # Candles at 5-15 min ago, all well after backdated creation.
        highs = [4565.0, 4575.0, 4570.0]
        lows = [4555.0] * 3
        rates = _mock_rates(highs, lows, minutes_ago_start=15)
        with patch("mt5_safe.mt5.copy_rates_from_pos", return_value=rates), \
             patch("executor._mt5_server_offset", return_value=0):
            listed = tools.list_active_plans()

        p = listed["plans"][0]
        assert p["target_zone_touched"] is True, (
            f"BUY target 4572 reached by mocked MT5 high 4575; "
            f"got {p['target_zone_touched']}"
        )
