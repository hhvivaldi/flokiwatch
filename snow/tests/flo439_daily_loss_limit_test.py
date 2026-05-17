"""FLO-439 — daily loss limit validator tests."""

from __future__ import annotations

import pytest

from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


class TestDailyLossLimit:

    def test_pnl_above_limit_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        # -1% loss → allow
        acct = {"balance": 10_000.0, "today_pnl_usd": -100.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_positive_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        acct = {"balance": 10_000.0, "today_pnl_usd": 250.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_at_exactly_limit_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        # -2% exactly → reject (gate fires at <= -2%)
        acct = {"balance": 10_000.0, "today_pnl_usd": -200.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert ok is False
        assert any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_well_below_limit_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        acct = {"balance": 10_000.0, "today_pnl_usd": -350.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert ok is False
        msg = next(e for e in errors if e.startswith("daily_loss_limit:"))
        assert "FLO-439" in msg
        assert "-3.50%" in msg or "-3.5" in msg

    def test_no_account_fails_open(
        self, valid_plan_dict, patch_active_plans, caplog
    ):
        import logging
        caplog.set_level(logging.WARNING)
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=None,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors
        assert any(
            "DAILY_LOSS_LIMIT_DEGRADED" in r.message for r in caplog.records
        )

    def test_zero_balance_fails_open(
        self, valid_plan_dict, patch_active_plans, caplog
    ):
        import logging
        caplog.set_level(logging.WARNING)
        acct = {"balance": 0.0, "today_pnl_usd": -500.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors
        assert any(
            "DAILY_LOSS_LIMIT_DEGRADED" in r.message
            and "zero_balance" in r.message
            for r in caplog.records
        )

    def test_unparseable_values_fail_open(
        self, valid_plan_dict, patch_active_plans, caplog
    ):
        import logging
        caplog.set_level(logging.WARNING)
        acct = {"balance": "not-a-number", "today_pnl_usd": -1000.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors
