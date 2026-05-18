"""FLO-439 — daily loss limit validator tests.

Updated 2026-05-18: threshold is fixed -$200 (was -2% of balance). The
percentage rule blocked plan creation after a single normal SL hit on
the small ~$2200 account; fixed-dollar threshold only fires when the
day's realized losses cross the catastrophe line.
"""

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
        # -$100 loss → allow (above -$200 threshold)
        acct = {"balance": 2200.0, "today_pnl_usd": -100.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_positive_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        acct = {"balance": 2200.0, "today_pnl_usd": 250.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_just_under_limit_allowed(
        self, valid_plan_dict, patch_active_plans
    ):
        """-$199.99 → allow (above -$200 threshold by 1 cent)."""
        acct = {"balance": 2200.0, "today_pnl_usd": -199.99}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_at_exactly_limit_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        # -$200 exactly → reject (gate fires at <= -$200)
        acct = {"balance": 2200.0, "today_pnl_usd": -200.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert ok is False
        assert any(e.startswith("daily_loss_limit:") for e in errors), errors

    def test_pnl_well_below_limit_rejected(
        self, valid_plan_dict, patch_active_plans
    ):
        acct = {"balance": 2200.0, "today_pnl_usd": -350.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert ok is False
        msg = next(e for e in errors if e.startswith("daily_loss_limit:"))
        assert "FLO-439" in msg
        assert "-350.00" in msg
        assert "$200" in msg or "200.00" in msg

    def test_small_loss_at_small_balance_no_longer_blocks(
        self, valid_plan_dict, patch_active_plans
    ):
        """Regression: at the production ~$2200 balance, a single -$58
        SL hit (the PLAN-20260518-001 actual loss) used to trigger the
        2%-of-balance rule (-$44 threshold). Under the new fixed -$200
        rule, the same loss is well within budget and plan creation
        stays open."""
        acct = {"balance": 2200.0, "today_pnl_usd": -58.48}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert not any(e.startswith("daily_loss_limit:") for e in errors), errors

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

    def test_zero_balance_no_longer_blocks(
        self, valid_plan_dict, patch_active_plans
    ):
        """Under the prior percentage rule, balance=0 forced fail-open
        (couldn't compute percentage). Under the fixed-dollar rule, the
        balance is purely informational — the gate evaluates against
        pnl directly. balance=0 with pnl=-$300 still rejects."""
        acct = {"balance": 0.0, "today_pnl_usd": -300.0}
        ok, _, errors = validate_plan(
            valid_plan_dict, author_account=acct,
        )
        assert ok is False
        assert any(e.startswith("daily_loss_limit:") for e in errors), errors

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
