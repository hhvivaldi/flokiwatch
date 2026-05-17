"""FLO-430 — ADX override (Path B of the regime gate) validator tests.

Path B fires when ALL hold:
  - adx >= 30
  - d1_direction == h4_direction in {"bullish", "bearish"}
  - plan direction opposes the D1+H4 EMA50 stack
Independent of regime label and confidence tier.

Tests cover the PLAN-20260514-009 production failure (ADX 46.87,
counter-trend BUY in a bearish stack that regime_detector labelled
TRANSITIONAL with confidence=moderate).
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import snow.validator as validator_mod
from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


def _buy_plan(valid_plan_dict: dict[str, Any]) -> dict[str, Any]:
    """Flip the base SELL plan to a BUY plan with consistent SL/TP."""
    out = deepcopy(valid_plan_dict)
    out["entry"]["direction"] = "BUY"
    out["entry"]["initial_sl"] = 4710.0   # BUY: SL below entry
    out["entry"]["initial_tp"] = 4740.0   # BUY: TP above entry
    # entry conditions: rsi BUY-side
    out["entry"]["conditions"] = [
        {"type": "price_above", "level": 4720.0},
        {"type": "rsi", "tf": "H1", "op": "below", "threshold": 30},
    ]
    out["exit"][0]["conditions"] = [{"type": "price_below", "level": 4715.0}]
    return out


class TestADXOverride:

    def test_plan009_production_case_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        """PLAN-20260514-009 reproduction: BUY with ADX 46.87, full
        bearish EMA stack, regime=TRANSITIONAL, confidence=moderate.
        FLO-427 missed it; FLO-430 must catch it."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "TRANSITIONAL",
                "confidence": "moderate",
                "adx": 46.87,
                "d1_direction": "bearish",
                "h4_direction": "bearish",
            },
        )
        assert ok is False
        gate_errs = [e for e in errors if e.startswith("regime_gate:")]
        assert gate_errs, errors
        assert "FLO-430" in gate_errs[0]
        assert "ADX 46.9" in gate_errs[0]
        assert "bearish" in gate_errs[0]

    def test_sell_in_bullish_stack_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        """Mirror case: SELL with ADX 32, D1+H4 both bullish."""
        # valid_plan_dict is already a SELL plan
        ok, _, errors = validate_plan(
            valid_plan_dict,
            author_regime={
                "regime": "RANGING",
                "confidence": "moderate",
                "adx": 32.0,
                "d1_direction": "bullish",
                "h4_direction": "bullish",
            },
        )
        assert ok is False
        assert any(
            e.startswith("regime_gate:") and "FLO-430" in e for e in errors
        ), errors

    def test_adx_below_floor_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        """ADX 28 < 30 → no override."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "RANGING",
                "confidence": "moderate",
                "adx": 28.0,
                "d1_direction": "bearish",
                "h4_direction": "bearish",
            },
        )
        assert not any(
            e.startswith("regime_gate:") and "FLO-430" in e for e in errors
        ), errors

    def test_stack_not_aligned_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        """D1 bullish, H4 bearish (or vice versa) → no override."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "RANGING",
                "confidence": "moderate",
                "adx": 40.0,
                "d1_direction": "bullish",
                "h4_direction": "bearish",
            },
        )
        assert not any(
            e.startswith("regime_gate:") and "FLO-430" in e for e in errors
        ), errors

    def test_aligned_with_stack_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        """SELL with bearish stack is aligned, not counter — allow."""
        # valid_plan_dict is SELL
        ok, _, errors = validate_plan(
            valid_plan_dict,
            author_regime={
                "regime": "RANGING",
                "confidence": "moderate",
                "adx": 35.0,
                "d1_direction": "bearish",
                "h4_direction": "bearish",
            },
        )
        assert not any(
            e.startswith("regime_gate:") and "FLO-430" in e for e in errors
        ), errors

    def test_missing_d1_h4_skips_override(
        self, valid_plan_dict, patch_active_plans
    ):
        """If d1/h4 are missing (legacy snapshot), Path B is inactive."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "RANGING",
                "confidence": "moderate",
                "adx": 50.0,
                # no d1_direction / h4_direction
            },
        )
        assert not any(
            e.startswith("regime_gate:") and "FLO-430" in e for e in errors
        ), errors

    def test_fl0427_trending_still_catches_counter_trend(
        self, valid_plan_dict, patch_active_plans
    ):
        """Regression: FLO-427 Path A still fires when regime is
        TRENDING + confidence high + adx>=25."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "TRENDING_BEARISH",
                "confidence": "high",
                "adx": 28.0,
                "d1_direction": "bearish",
                "h4_direction": "bearish",
            },
        )
        assert ok is False
        gate_errs = [e for e in errors if e.startswith("regime_gate:")]
        assert gate_errs, errors
        # FLO-427 fires first (regime=TRENDING_BEARISH, confidence=high)
        assert "FLO-427" in gate_errs[0] or "TRENDING_BEARISH" in gate_errs[0]

    def test_trending_low_confidence_caught_by_adx_override(
        self, valid_plan_dict, patch_active_plans
    ):
        """Belt-and-suspenders: TRENDING regime + confidence=moderate
        slips past FLO-427's confidence floor, but Path B catches it
        because adx>=30 + stack aligned."""
        plan = _buy_plan(valid_plan_dict)
        ok, _, errors = validate_plan(
            plan,
            author_regime={
                "regime": "TRENDING_BEARISH",
                "confidence": "moderate",  # <-- FLO-427 bails
                "adx": 35.0,
                "d1_direction": "bearish",
                "h4_direction": "bearish",
            },
        )
        assert ok is False
        gate_errs = [e for e in errors if e.startswith("regime_gate:")]
        assert gate_errs, errors
        assert "FLO-430" in gate_errs[0]
