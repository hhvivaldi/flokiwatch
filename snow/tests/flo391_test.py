"""FLO-391 — validator semantic coherence: management primitive reachability.

Empirical basis: PLAN-011 (live 2026-04-28) shipped with TP=26 pips and
a `trail_sl` contingency triggered by `mfe_reached pips=200`. Trigger
unreachable. Effectively no management. CEO had to defend SL manually
to prevent stupid loss.

Acceptance test cases (per FLO-391 ticket):
  * `mfe_reached pips=200` with TP=26 pips → REJECT
  * `profit_pips threshold=40` with TP=30 pips → REJECT
  * `mfe_reached pips=15`  with TP=50 pips → ACCEPT
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from snow.validator import (
    validate_plan,
    _check_management_reachability,
    _plan_max_profit_pips,
)
from snow.schema import Plan


# Minimal valid plan dict; FLO-391-relevant fields are SL/TP and the
# management contingency. Entry has 2 conditions per FLO-Path4 floor.
def _plan(initial_sl: float, initial_tp: float, mgmt_conditions: list,
          mgmt_action: dict | None = None) -> dict[str, Any]:
    if mgmt_action is None:
        mgmt_action = {"type": "trail_sl", "trail_pips": 5.0}
    return {
        "schema_version": 1,
        "id": "PLAN-20260428-391",
        "created_by": "floki",
        "created_at": "2026-04-28T10:00:00Z",
        "expires_at": "2026-04-28T18:00:00Z",
        "analysis": {
            "thesis": "FLO-391 reachability gate test plan",
            "key_levels": [4500.0, initial_sl, initial_tp],
            "confidence": 60,
            "regime_assumed": "TRENDING_BULLISH",
        },
        "entry": {
            "direction": "BUY",
            "volume": 0.02,
            "conditions": [
                {"type": "price_above", "level": 4500.0},
                {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            ],
            "initial_sl": initial_sl,
            "initial_tp": initial_tp,
        },
        "management": [
            {
                "name": "trail_sl",
                "priority": 7,
                "conditions": mgmt_conditions,
                "action": mgmt_action,
                "fires": "once",
            }
        ],
        "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
        "emergency": {
            "max_loss_pips": 150,
            "max_duration_minutes": 480,
            "on_broker_error": "alert_floki",
        },
    }


# =============================================================================
# Acceptance test cases (FLO-391 ticket spec)
# =============================================================================


class TestFLO391Acceptance:
    def test_mfe_reached_200_with_tp_26_pips_rejected(self):
        """PLAN-011 reproduction: mfe_reached 200 against 26-pip range."""
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4502.6,  # 2.6 price units = 26 pips
            mgmt_conditions=[{"type": "mfe_reached", "pips": 200.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert not ok, "expected rejection on mfe_reached=200 / TP=26 pips"
        msg = " ".join(errors).lower()
        assert "unreachable" in msg
        assert "200" in " ".join(errors)
        assert "26" in " ".join(errors)

    def test_profit_pips_40_with_tp_30_pips_rejected(self):
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4503.0,  # 30 pips
            mgmt_conditions=[
                {"type": "profit_pips", "op": "above", "threshold": 40.0}
            ],
            mgmt_action={"type": "move_sl_to_breakeven", "offset_pips": 0.0},
        )
        ok, _, errors = validate_plan(plan)
        assert not ok, "expected rejection on profit_pips=40 / TP=30 pips"
        msg = " ".join(errors).lower()
        assert "unreachable" in msg

    def test_mfe_reached_15_with_tp_50_pips_accepted(self):
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4505.0,  # 50 pips
            mgmt_conditions=[{"type": "mfe_reached", "pips": 15.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert ok, errors


# =============================================================================
# Boundary + edge cases
# =============================================================================


class TestFLO391Boundaries:
    def test_threshold_equal_to_max_pips_accepted(self):
        """Boundary: threshold == max_pips is ACCEPT (not strictly greater)."""
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4505.0,  # 50 pips
            mgmt_conditions=[{"type": "mfe_reached", "pips": 50.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert ok, errors

    def test_threshold_one_pip_over_rejected(self):
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4505.0,  # 50 pips
            mgmt_conditions=[{"type": "mfe_reached", "pips": 51.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert not ok

    def test_profit_pips_below_op_not_gated(self):
        """`profit_pips op=below` is a protective drop-trigger; reachability
        gate does not apply (threshold is a lower bound, not upper)."""
        # Plan would normally need a non-noise-floor companion to pass
        # _check_management_threshold_floor; pair below trigger with an
        # mfe_reached gate to satisfy the noise-floor rule.
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4503.0,  # 30 pips
            mgmt_conditions=[
                {"type": "profit_pips", "op": "below", "threshold": 999.0},
                {"type": "mfe_reached", "pips": 10.0},
            ],
        )
        ok, _, errors = validate_plan(plan)
        assert ok, errors

    def test_profit_retraced_from_peak_not_gated(self):
        """Retracement triggers gauge give-back from peak, not absolute
        profit. Gate intentionally skips them."""
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4503.0,  # 30 pips
            mgmt_conditions=[
                {"type": "profit_retraced_from_peak", "pips": 999.0}
            ],
        )
        ok, _, errors = validate_plan(plan)
        # profit_retraced_from_peak is peak-relative; FLO-383 noise-floor
        # treats it as qualifying. FLO-391 reachability skips it.
        assert ok, errors

    def test_sell_direction_reachability(self):
        """SELL plan: SL above entry, TP below. Same |TP-SL| computation."""
        plan = _plan(
            initial_sl=4505.0,
            initial_tp=4500.0,
            mgmt_conditions=[{"type": "mfe_reached", "pips": 60.0}],
        )
        plan["entry"]["direction"] = "SELL"
        ok, _, errors = validate_plan(plan)
        # |4505-4500| / 0.1 = 50 pips. mfe_reached=60 > 50 → REJECT.
        assert not ok
        msg = " ".join(errors).lower()
        assert "unreachable" in msg

    def test_realistic_rr_plan_with_unreachable_trigger_rejected(self):
        """Non-degenerate plan: SL 50 pips below entry, TP 25 pips above
        → |TP-SL|=75 pips. mfe_reached=200 > 75 → REJECT.

        Demonstrates the gate works on plans with conventional R:R, not
        only on the degenerate SL=entry fixtures used in acceptance.
        """
        plan = _plan(
            initial_sl=4495.0,
            initial_tp=4502.5,  # |2.5+5|/0.1 = 75 pips total range
            mgmt_conditions=[{"type": "mfe_reached", "pips": 200.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert not ok
        msg = " ".join(errors).lower()
        assert "unreachable" in msg

    def test_multi_condition_management_each_checked(self):
        """Each condition in a contingency is checked independently."""
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4505.0,  # 50 pips
            mgmt_conditions=[
                {"type": "rsi", "tf": "H1", "op": "above", "threshold": 70},
                {"type": "mfe_reached", "pips": 999.0},  # unreachable
            ],
        )
        ok, _, errors = validate_plan(plan)
        assert not ok
        assert any("mfe_reached" in e for e in errors)

    def test_empty_management_unaffected(self):
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4501.0,  # 10 pips — tiny but no management
            mgmt_conditions=[{"type": "mfe_reached", "pips": 5.0}],
        )
        plan["management"] = []
        ok, _, errors = validate_plan(plan)
        assert ok, errors

    def test_max_profit_pips_helper_buy(self):
        plan = Plan(**_plan(
            initial_sl=4500.0,
            initial_tp=4505.0,
            mgmt_conditions=[{"type": "mfe_reached", "pips": 5.0}],
        ))
        assert _plan_max_profit_pips(plan) == pytest.approx(50.0)

    def test_max_profit_pips_helper_sell(self):
        d = _plan(
            initial_sl=4505.0,
            initial_tp=4500.0,
            mgmt_conditions=[{"type": "mfe_reached", "pips": 5.0}],
        )
        d["entry"]["direction"] = "SELL"
        plan = Plan(**d)
        assert _plan_max_profit_pips(plan) == pytest.approx(50.0)

    def test_error_message_names_remediation(self):
        plan = _plan(
            initial_sl=4500.0,
            initial_tp=4502.6,
            mgmt_conditions=[{"type": "mfe_reached", "pips": 200.0}],
        )
        ok, _, errors = validate_plan(plan)
        assert not ok
        msg = " ".join(errors).lower()
        # Floki must be told both remediation paths
        assert "lower" in msg
        assert ("widen" in msg) or ("tp" in msg)
