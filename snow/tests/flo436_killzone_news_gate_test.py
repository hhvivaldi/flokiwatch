"""FLO-436 — killzone session gate + news blackout gate tests."""

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


def _at(plan: dict[str, Any], iso_hour_utc: str) -> dict[str, Any]:
    """Return a copy of `plan` with created_at set to the given UTC time."""
    out = deepcopy(plan)
    out["created_at"] = iso_hour_utc
    # bump expires to keep it after created_at
    out["expires_at"] = "2026-05-18T23:00:00Z"
    return out


def _with_setup(plan: dict[str, Any], setup_type: str) -> dict[str, Any]:
    out = deepcopy(plan)
    out["analysis"]["setup_type"] = setup_type
    return out


class TestKillzoneGate:

    def test_london_open_breakout_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T08:00:00Z"),
            "breakout_range",
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("killzone_gate:") for e in errors), errors

    def test_london_open_pullback_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T08:00:00Z"),
            "pullback_trend",
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any(
            e.startswith("killzone_gate:") and "LONDON_OPEN" in e for e in errors
        ), errors

    def test_ny_overlap_all_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        for setup in (
            "pullback_trend",
            "breakout_range",
            "mean_reversion_extreme",
            "divergence_play",
        ):
            plan = _with_setup(
                _at(valid_plan_dict, "2026-05-19T14:00:00Z"),
                setup,
            )
            ok, _, errors = validate_plan(plan)
            assert not any(e.startswith("killzone_gate:") for e in errors), (
                f"{setup} should be allowed in NY_OVERLAP: {errors}"
            )

    def test_ny_pm_pullback_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T17:00:00Z"),
            "pullback_trend",
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("killzone_gate:") for e in errors), errors

    def test_ny_pm_breakout_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T17:00:00Z"),
            "breakout_range",
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any(
            e.startswith("killzone_gate:") and "NY_PM" in e for e in errors
        ), errors

    def test_asian_breakout_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T02:00:00Z"),
            "breakout_range",
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any(
            e.startswith("killzone_gate:") and "ASIAN" in e for e in errors
        ), errors

    def test_asian_structural_bounce_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        plan = _with_setup(
            _at(valid_plan_dict, "2026-05-19T03:00:00Z"),
            "structural_bounce",
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("killzone_gate:") for e in errors), errors


class TestNewsBlackoutGate:

    def test_nfp_within_blackout_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "US Nonfarm Payrolls", "importance": "HIGH",
             "minutes_until": 12.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert ok is False
        assert any(
            e.startswith("news_blackout:") and "Nonfarm" in e for e in errors
        ), errors

    def test_cpi_just_passed_still_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "Consumer Price Index (CPI)", "importance": "HIGH",
             "minutes_until": -15.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert ok is False
        gb = next(e for e in errors if e.startswith("news_blackout:"))
        assert "CPI" in gb or "Consumer Price" in gb

    def test_low_impact_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "US PMI", "importance": "MEDIUM",
             "minutes_until": 5.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert not any(e.startswith("news_blackout:") for e in errors), errors

    def test_high_impact_non_tier1_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "Building Permits", "importance": "HIGH",
             "minutes_until": 10.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert not any(e.startswith("news_blackout:") for e in errors), errors

    def test_far_outside_window_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "FOMC Statement", "importance": "HIGH",
             "minutes_until": 120.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert not any(e.startswith("news_blackout:") for e in errors), errors

    def test_no_calendar_fails_open(
        self, valid_plan_dict, patch_active_plans, caplog
    ):
        import logging
        caplog.set_level(logging.WARNING)
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=None,
        )
        assert not any(e.startswith("news_blackout:") for e in errors), errors
        assert any(
            "NEWS_BLACKOUT_DEGRADED" in r.message for r in caplog.records
        ), [r.message for r in caplog.records]

    def test_multiple_events_first_match_rejects(
        self, valid_plan_dict, patch_active_plans
    ):
        cal = [
            {"name": "Random Speech", "importance": "MEDIUM",
             "minutes_until": 5.0},
            {"name": "FOMC Rate Decision", "importance": "HIGH",
             "minutes_until": 20.0},
            {"name": "GDP Final", "importance": "HIGH",
             "minutes_until": -25.0},
        ]
        ok, _, errors = validate_plan(
            valid_plan_dict, author_calendar=cal,
        )
        assert ok is False
        assert any(e.startswith("news_blackout:") for e in errors), errors
