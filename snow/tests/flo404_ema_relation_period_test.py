"""FLO-404 follow-up — ema_relation period/relation cross-field rule.

Empirical motivation (CEO directive 2026-04-30): PLAN-20260429-012
used `relation: aligned_bull` with `period: 21` thinking it meant
"price above EMA21" — but the evaluator reads all 4 EMAs (9/21/50/200)
in strict alignment regardless of period. Plan never fired during the
30+ minutes when price was above EMA21 with bullish momentum because
EMA50 had not yet crossed EMA200.

Schema fix: `period: Optional[Literal[9,21,50,200]] = None`
Validator fix: new function _check_ema_relation_period_consistency:
  - price_above / price_below: period REQUIRED (single-EMA flip)
  - aligned_bull / aligned_bear: period FORBIDDEN (regime gate)

This test file pins both directions of the new contract with
educational error messages that point Floki at the correct primitive.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from snow.schema import EMARelation
from snow.validator import (
    _check_ema_relation_period_consistency,
    validate_plan,
)


def _future_iso(hours: int = 6) -> str:
    t = datetime.now(timezone.utc) + timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan_with_entry_conditions(conditions: list[dict]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": "PLAN-00000000-000",
        "created_by": "floki",
        "created_at": "2026-04-30T00:00:00Z",
        "expires_at": _future_iso(6),
        "status": "pending",
        "analysis": {
            "thesis": "FLO-404 cross-field rule test",
            "key_levels": [4543.0, 4553.0, 4570.0],
            "confidence": 75,
            "regime_assumed": "TRENDING_BEARISH",
        },
        "entry": {
            "direction": "BUY", "volume": 0.02,
            "conditions": conditions,
            "initial_sl": 4543.0, "initial_tp": 4570.0,
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


# =============================================================================
# Schema accepts both shapes — period is now Optional
# =============================================================================


class TestSchemaAcceptsOptionalPeriod:
    def test_aligned_bull_without_period_is_valid_schema(self):
        c = EMARelation(tf="H1", relation="aligned_bull")
        assert c.period is None
        assert c.relation == "aligned_bull"

    def test_aligned_bear_without_period_is_valid_schema(self):
        c = EMARelation(tf="H4", relation="aligned_bear")
        assert c.period is None

    def test_price_above_with_period_is_valid_schema(self):
        c = EMARelation(tf="M5", period=21, relation="price_above")
        assert c.period == 21

    def test_price_below_with_period_is_valid_schema(self):
        c = EMARelation(tf="H1", period=50, relation="price_below")
        assert c.period == 50


# =============================================================================
# Validator rejects: price_above/price_below WITHOUT period
# =============================================================================


class TestPriceAboveBelowRequiresPeriod:
    @pytest.mark.parametrize("relation", ["price_above", "price_below"])
    def test_price_relation_without_period_rejects(self, relation):
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "M5", "relation": relation},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        ok, _, errors = validate_plan(plan_dict)
        assert ok is False
        assert any(
            "REQUIRES the `period` field" in e and relation in e
            for e in errors
        ), (
            f"expected 'period required' rejection for {relation}; "
            f"got: {errors}"
        )

    @pytest.mark.parametrize("relation,period", [
        ("price_above", 9), ("price_above", 21), ("price_above", 50),
        ("price_above", 200), ("price_below", 21), ("price_below", 50),
    ])
    def test_price_relation_with_period_accepts(self, relation, period):
        """price_above / price_below WITH period passes the cross-field
        rule — the rest of validation may still surface other errors,
        but not this one."""
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "M5",
             "period": period, "relation": relation},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        # Run only the cross-field check directly.
        from snow.validator import _check_ema_relation_period_consistency
        from snow.schema import Plan
        plan = Plan.model_validate(plan_dict)
        errors = _check_ema_relation_period_consistency(plan)
        assert errors == [], (
            f"{relation}+period={period} must pass the cross-field check; "
            f"got: {errors}"
        )


# =============================================================================
# Validator rejects: aligned_bull/aligned_bear WITH period
# =============================================================================


class TestAlignedRejectsPeriod:
    @pytest.mark.parametrize("relation", ["aligned_bull", "aligned_bear"])
    @pytest.mark.parametrize("period", [9, 21, 50, 200])
    def test_aligned_with_period_rejects(self, relation, period):
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "H1",
             "period": period, "relation": relation},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        ok, _, errors = validate_plan(plan_dict)
        assert ok is False
        assert any(
            "must NOT carry a `period` field" in e and relation in e
            for e in errors
        ), (
            f"expected period-forbidden rejection for {relation}+period={period}; "
            f"got: {errors}"
        )

    @pytest.mark.parametrize("relation", ["aligned_bull", "aligned_bear"])
    def test_aligned_without_period_accepts(self, relation):
        """aligned_bull / aligned_bear WITHOUT period passes the
        cross-field rule (the canonical regime-gate shape)."""
        from snow.validator import _check_ema_relation_period_consistency
        from snow.schema import Plan
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "H1", "relation": relation},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        plan = Plan.model_validate(plan_dict)
        errors = _check_ema_relation_period_consistency(plan)
        assert errors == [], (
            f"{relation} without period is the canonical shape; "
            f"got: {errors}"
        )


# =============================================================================
# Educational message content — Floki must be able to self-correct
# =============================================================================


class TestErrorMessageActionability:
    def test_aligned_with_period_message_points_at_price_above(self):
        """The rejection must explicitly suggest `relation: price_above`
        as the alternative when Floki meant a single-EMA flip — that's
        the today's-bug recovery path."""
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "M5",
             "period": 21, "relation": "aligned_bull"},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        ok, _, errors = validate_plan(plan_dict)
        assert ok is False
        # Find the cross-field rejection
        relevant = [e for e in errors if "ema_relation" in e and "aligned_bull" in e]
        assert relevant, f"expected ema_relation/aligned_bull rejection; got {errors}"
        msg = relevant[0]
        # Three required teaching points:
        assert "all four EMAs" in msg or "9, 21, 50, 200" in msg
        assert "regime gate" in msg.lower() or "regime" in msg.lower()
        assert "price_above" in msg, (
            "the rejection MUST point at price_above as the single-EMA "
            "alternative — that's the FLO-404 recovery path"
        )

    def test_price_above_without_period_message_lists_valid_periods(self):
        plan_dict = _plan_with_entry_conditions([
            {"type": "ema_relation", "tf": "M5", "relation": "price_above"},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ])
        ok, _, errors = validate_plan(plan_dict)
        assert ok is False
        relevant = [e for e in errors if "REQUIRES" in e and "price_above" in e]
        assert relevant
        msg = relevant[0]
        # Floki must see the valid period values to self-correct.
        assert "9, 21, 50, 200" in msg or "9," in msg
        assert "single-EMA" in msg.lower() or "ema(tf, period)" in msg.lower()


# =============================================================================
# PLAN-20260429-012 regression — the empirical case that motivated this
# =============================================================================


class TestPLAN012Regression:
    """Pre-FLO-404 the schema accepted `aligned_bull + period: 21` and
    the evaluator silently ignored period. PLAN-012 sat unfired for
    hours. This test pins the rejection of the exact misuse pattern."""

    def test_plan_012_misuse_pattern_rejects(self):
        plan_dict = _plan_with_entry_conditions([
            {"type": "price_above", "level": 4553.0},
            {"type": "macd_histogram", "tf": "M5", "op": "above",
             "threshold": 0.0},
            # The exact PLAN-012 misuse: aligned_bull with period=21
            {"type": "ema_relation", "tf": "M5",
             "period": 21, "relation": "aligned_bull"},
        ])
        ok, _, errors = validate_plan(plan_dict)
        assert ok is False
        assert any(
            "aligned_bull" in e and "must NOT carry a `period` field" in e
            for e in errors
        ), f"PLAN-012 regression: misuse pattern not rejected. errors={errors}"
