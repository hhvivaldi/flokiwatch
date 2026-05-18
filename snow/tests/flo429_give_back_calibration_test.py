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

    def test_no_author_regime_falls_back_to_plan_claim(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """FLO-440 — no live snapshot AND plan claims a non-trending
        regime → ATR floor still applies (200 >= 90 → allow).

        Earlier behavior (pre-FLO-440) was to no-op the trending-ban
        whenever the live snapshot was missing, regardless of what the
        plan's `analysis.regime_assumed` claimed. PLAN-20260517-001
        showed that produced a contradiction-free pass when the plan
        claimed TRENDING and the snapshot was DEGRADED. Test now
        verifies the ATR path with a RANGING claim; the
        TestGiveBackRegimeFallback class verifies the trending-ban
        path fires correctly under the same DEGRADED-snapshot condition.
        """
        patch_atr["atr"] = 30.0
        plan = _plan_with_give_back(valid_plan_dict, pips=200.0)
        plan["analysis"]["regime_assumed"] = "RANGING"
        ok, _, errors = validate_plan(plan, author_regime=None)
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


class TestGiveBackRegimeFallback:
    """FLO-440 — when the live regime snapshot is DEGRADED, fall back
    to plan.analysis.regime_assumed for Rule (a) trending-ban check.

    Regression: PLAN-20260517-001 claimed regime_assumed=TRENDING_BEARISH
    but the live snapshot was DEGRADED, so the trending-ban rule
    silently no-op'd and a 150p give_back exit slipped through the
    ATR floor."""

    def test_plan009_reproduction_uses_claimed_regime(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """Plan self-claims TRENDING_BEARISH; author_regime is None
        (DEGRADED). Rule (a) must still fire on the claimed regime."""
        out = _plan_with_give_back(valid_plan_dict, pips=150.0)
        out["analysis"]["regime_assumed"] = "TRENDING_BEARISH"
        ok, _, errors = validate_plan(out, author_regime=None)
        assert ok is False, errors
        gb_errs = [e for e in errors if e.startswith("give_back_calibration:")]
        assert gb_errs, errors
        assert "TRENDING_BEARISH" in gb_errs[0]

    def test_claimed_trending_bullish_also_caught(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        out = _plan_with_give_back(valid_plan_dict, pips=200.0)
        out["analysis"]["regime_assumed"] = "TRENDING_BULLISH"
        ok, _, errors = validate_plan(out, author_regime=None)
        assert ok is False
        assert any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_claimed_ranging_falls_through_to_atr_floor(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """Non-trending claimed regime → ATR-floor rule (b) applies."""
        patch_atr["atr"] = 30.0  # min required = 90 pips
        out = _plan_with_give_back(valid_plan_dict, pips=200.0)
        out["analysis"]["regime_assumed"] = "RANGING"
        ok, _, errors = validate_plan(out, author_regime=None)
        # 200 >= 90 → allow
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors

    def test_live_snapshot_still_wins_over_claim(
        self, valid_plan_dict, patch_atr, patch_active_plans
    ):
        """If the live snapshot HAS a regime, it takes precedence over
        the plan's self-claim (live data is the source of truth when
        available)."""
        out = _plan_with_give_back(valid_plan_dict, pips=200.0)
        out["analysis"]["regime_assumed"] = "TRENDING_BEARISH"
        # Live snapshot says RANGING; the live value wins.
        ok, _, errors = validate_plan(
            out,
            author_regime={"regime": "RANGING", "confidence": "moderate", "adx": 18.0},
        )
        # patch_atr default 30 → min_req 90; 200 >= 90 → allow
        assert not any(e.startswith("give_back_calibration:") for e in errors), errors
