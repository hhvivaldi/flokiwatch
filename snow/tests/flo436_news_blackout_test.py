"""FLO-436 — Tier-1 news blackout gate tests.

(FLO-440, 2026-05-18: the companion killzone-session gate was removed
from this file and from the validator after empirical analysis showed
the killzone allowlists inverted this bot's actual P&L pattern. The
session signal is now an informational prompt block, not a hard gate.
News blackout remains a hard gate — Tier-1 macro releases continue to
have non-discretionary impact on price action.)
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


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
