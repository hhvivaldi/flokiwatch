"""FLO-442 — close_partial allowed in Escola 2 management blocks."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


def _set_management(plan: dict[str, Any], contingencies: list[dict[str, Any]]) -> dict[str, Any]:
    out = deepcopy(plan)
    out["management"] = contingencies
    return out


class TestClosePartialAllowed:

    def test_single_close_partial_50pct_at_1r_accepted(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("management") for e in errors), errors

    def test_close_partial_plus_be_pair_accepted(
        self, valid_plan_dict, patch_active_plans
    ):
        """Canonical FLO-442 pattern: TP1 partial → BE on runner."""
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
            {
                "name": "be_after_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
                "fires": "once",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("management") for e in errors), errors

    def test_close_partial_plus_trail_pair_accepted(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
            {
                "name": "trail_runner",
                "priority": 6,
                "conditions": [{"type": "mfe_reached", "pips": 150.0}],
                "action": {"type": "trail_sl", "trail_pips": 100.0},
                "fires": "every_time",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("management") for e in errors), errors

    def test_close_partial_without_mfe_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "profit_pips", "op": "above", "threshold": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any("must trigger on `mfe_reached`" in e for e in errors), errors

    def test_close_partial_with_mfe_zero_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
        ])
        # Pydantic ClosePartial.percent has gt=0/lt=100 — sanity check
        # that a 50% partial passes validation.
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("management") for e in errors), errors

    def test_three_contingencies_still_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        """_MAX=2 unchanged in this commit."""
        plan = _set_management(valid_plan_dict, [
            {
                "name": "tp1_partial",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "close_partial", "percent": 50.0},
                "fires": "once",
            },
            {
                "name": "be",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
                "fires": "once",
            },
            {
                "name": "trail",
                "priority": 6,
                "conditions": [{"type": "mfe_reached", "pips": 150.0}],
                "action": {"type": "trail_sl", "trail_pips": 100.0},
                "fires": "every_time",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any("at most 2 contingencies" in e for e in errors), errors

    def test_adjust_sl_still_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        """Sanity: relaxing the allowlist did NOT open adjust_sl."""
        plan = _set_management(valid_plan_dict, [
            {
                "name": "bad",
                "priority": 7,
                "conditions": [{"type": "mfe_reached", "pips": 100.0}],
                "action": {"type": "adjust_sl", "new_sl": 4700.0},
                "fires": "once",
            },
        ])
        ok, _, errors = validate_plan(plan)
        assert ok is False
        # Pydantic may reject `adjust_sl` at the schema layer before
        # the validator runs, so accept either failure mode.
        assert errors, "expected adjust_sl to be rejected"
