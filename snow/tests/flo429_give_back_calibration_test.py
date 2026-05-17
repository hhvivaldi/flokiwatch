"""FLO-429 — give_back_exit calibration validator tests.

Two rules under test:
  (a) Reject any profit_retraced_from_peak exit in TRENDING regimes.
  (b) Otherwise, require pips >= 3.0 × M5_ATR(14)_pips.

The MT5 fetch is monkeypatched via _fetch_m5_atr_pips so tests don't
require a live terminal.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import snow.validator as validator_mod
from snow.validator import validate_plan


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _plan_with_give_back(valid_plan_dict: dict[str, Any], pips: float) -> dict[str, Any]:
    """Replace the second exit ('time_stop') with a give_back contingency."""
    out = deepcopy(valid_plan_dict)
    out["exit"] = [
        out["exit"][0],
        {
            "name": "give_back_exit",
            "priority": 8,
            "conditions": [{"type": "profit_retraced_from_peak", "pips": pips}],
            "action": {"type": "close_full"},
            "fires": "once",
        },
    ]
    return out


@pytest.fixture
def patch_atr(monkeypatch):
    """Patch _fetch_m5_atr_pips so tests control the ATR value."""
    holder = {"atr": 30.0}

    def _fake_fetch():
        return holder["atr"]

    monkeypatch.setattr(validator_mod, "_fetch_m5_atr_pips", _fake_fetch)
    return holder


@pytest.fixture
def patch_active_plans(monkeypatch):
    """Disable FLO-428 active-plan cap (no live plans)."""
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


# -----------------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------------

class TestGiveBackCalibration:

    def test_no_give_back_allowed(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """Plans with no profit_retraced_from_peak exits should pass."""
        # Base fixture's exits use price_above + duration_exceeds — no give-back.
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_trending_bullish_rejects_give_back(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        plan = _plan_with_give_back(valid_plan_dict, pips=200.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "TRENDING_BULLISH", "confidence": "high", "adx": 35.0},
        )
        assert ok is False
        gb_errs = [e for e in errors if e.startswith("give_back_calibration:")]
        assert gb_errs, errors
        assert "TRENDING_BULLISH" in gb_errs[0]
        assert "give_back_exit" in gb_errs[0]
        assert "FLO-429" in gb_errs[0]

    def test_trending_bearish_rejects_give_back(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        # Use a generous pips value so it's only the regime ban firing.
        plan = _plan_with_give_back(valid_plan_dict, pips=500.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "TRENDING_BEARISH", "confidence": "moderate", "adx": 42.0},
        )
        assert ok is False
        assert any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_ranging_allows_wide_give_back(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """In RANGING regime: pips=200, ATR=30 → min=90 → allow."""
        patch_atr["atr"] = 30.0
        plan = _plan_with_give_back(valid_plan_dict, pips=200.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "RANGING", "confidence": "moderate", "adx": 18.0},
        )
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_ranging_rejects_tight_give_back(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """RANGING regime: pips=50, ATR=30 → min=90 → reject."""
        patch_atr["atr"] = 30.0
        plan = _plan_with_give_back(valid_plan_dict, pips=50.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "RANGING", "confidence": "moderate", "adx": 18.0},
        )
        assert ok is False
        gb_errs = [e for e in errors if e.startswith("give_back_calibration:")]
        assert gb_errs, errors
        assert "too tight" in gb_errs[0] or "3.0" in gb_errs[0]

    def test_transitional_uses_atr_floor(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """TRANSITIONAL with PLAN-009's exact values: pips=80, ATR=70.86
        → min=213 → should REJECT. This is the production failure case."""
        patch_atr["atr"] = 70.86
        plan = _plan_with_give_back(valid_plan_dict, pips=80.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "TRANSITIONAL", "confidence": "moderate", "adx": 46.87},
        )
        assert ok is False
        gb_errs = [e for e in errors if e.startswith("give_back_calibration:")]
        assert gb_errs, errors

    def test_mt5_unavailable_fails_open(
        self, valid_plan_dict, monkeypatch, patch_active_plans, caplog
    ):
        """MT5 failure → allow + GIVE_BACK_CAL_DEGRADED log."""
        import logging
        monkeypatch.setattr(validator_mod, "_fetch_m5_atr_pips", lambda: None)
        caplog.set_level(logging.WARNING)
        plan = _plan_with_give_back(valid_plan_dict, pips=80.0)
        ok, _, errors = validate_plan(
            plan,
            author_regime={"regime": "RANGING", "confidence": "moderate", "adx": 18.0},
        )
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_no_author_regime_fails_open(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """No regime snapshot → ATR floor still applies if ATR is fetchable."""
        patch_atr["atr"] = 30.0
        plan = _plan_with_give_back(valid_plan_dict, pips=200.0)
        ok, _, errors = validate_plan(plan, author_regime=None)
        # ATR rule still fires (200 > 90), so should allow.
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_multiple_give_backs_one_offender(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """Two give-back exits, one tight, one wide → reject."""
        patch_atr["atr"] = 30.0
        out = deepcopy(valid_plan_dict)
        out["exit"] = [
            out["exit"][0],
            {
                "name": "give_back_wide",
                "priority": 7,
                "conditions": [{"type": "profit_retraced_from_peak", "pips": 200.0}],
                "action": {"type": "close_full"},
                "fires": "once",
            },
            {
                "name": "give_back_tight",
                "priority": 8,
                "conditions": [{"type": "profit_retraced_from_peak", "pips": 40.0}],
                "action": {"type": "close_full"},
                "fires": "once",
            },
        ]
        ok, _, errors = validate_plan(
            out,
            author_regime={"regime": "RANGING", "confidence": "moderate", "adx": 18.0},
        )
        assert ok is False
        gb_errs = [e for e in errors if e.startswith("give_back_calibration:")]
        assert gb_errs, errors
        assert "give_back_tight" in gb_errs[0]
