"""Position-state evaluator tests — 4 stateful primitives.

Each test seeds the tracker (ticket attached), updates the price,
then evaluates the condition. Also exercises the fail-safe paths:
  * ticket=None (plan not yet entered) → False
  * live price unavailable → False
  * plan not seeded in tracker → False (equivalent to ticket=None in
    practice, but separately covered).
"""
from __future__ import annotations

import pytest

from snow.evaluators.dispatch import evaluate_condition
from snow.schema import (
    Direction,
    MAEReached,
    MFEReached,
    ProfitPips,
    ProfitRetracedFromPeak,
)


# =============================================================================
# Shared seeding helper
# =============================================================================

def _seed_buy_at_4700(tracker, plan_id: str):
    tracker.seed(plan_id, 4700.0, Direction.BUY)


def _seed_sell_at_4700(tracker, plan_id: str):
    tracker.seed(plan_id, 4700.0, Direction.SELL)


# =============================================================================
# profit_pips — SIGNED, both directions
# =============================================================================

class TestProfitPipsEval:

    def test_buy_winning_above_threshold(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4715.0),
            ticket=111, plan=sample_plan,
        )
        # +150 pips — passes op=above, threshold=100
        assert evaluate_condition(
            ProfitPips(op="above", threshold=100.0), ctx
        ) is True

    def test_buy_winning_under_threshold(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4705.0),
            ticket=111, plan=sample_plan,
        )
        # +50 pips — fails above, threshold=100
        assert evaluate_condition(
            ProfitPips(op="above", threshold=100.0), ctx
        ) is False

    def test_sell_losing_triggers_below_negative_threshold(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        """SELL at 4700, price rose to 4706 → losing 60 pips (signed -60).
        op=below, threshold=-50 → -60 < -50 → True."""
        _seed_sell_at_4700(tracker, sample_plan.id)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4706.0),
            ticket=222, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitPips(op="below", threshold=-50.0), ctx
        ) is True

    def test_ticket_none_returns_false(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4715.0),
            ticket=None, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitPips(op="above", threshold=100.0), ctx
        ) is False

    def test_no_live_price_returns_false(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=None),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitPips(op="above", threshold=100.0), ctx
        ) is False

    def test_not_seeded_returns_false(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        # Tracker empty → tracker.profit_pips returns None → False
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4715.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitPips(op="above", threshold=100.0), ctx
        ) is False


# =============================================================================
# mfe_reached
# =============================================================================

class TestMFEReached:

    def test_reached(self, eval_ctx, fake_live, sample_plan, tracker):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4720.0)  # MFE = 200
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4715.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(MFEReached(pips=150.0), ctx) is True

    def test_not_reached(self, eval_ctx, fake_live, sample_plan, tracker):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4705.0)  # MFE = 50
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4705.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(MFEReached(pips=150.0), ctx) is False

    def test_ticket_none_false(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4720.0)
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4715.0),
            ticket=None, plan=sample_plan,
        )
        assert evaluate_condition(MFEReached(pips=150.0), ctx) is False


# =============================================================================
# mae_reached
# =============================================================================

class TestMAEReached:

    def test_reached(self, eval_ctx, fake_live, sample_plan, tracker):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4685.0)  # MAE = 150
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4690.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(MAEReached(pips=100.0), ctx) is True

    def test_not_reached(self, eval_ctx, fake_live, sample_plan, tracker):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4695.0)  # MAE = 50
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4695.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(MAEReached(pips=100.0), ctx) is False


# =============================================================================
# profit_retraced_from_peak — advisor #3 edge case
# =============================================================================

class TestProfitRetracedFromPeak:

    def test_retrace_triggers(self, eval_ctx, fake_live, sample_plan, tracker):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4720.0)  # peak = 200
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4708.0),   # now +80 → retrace 120
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitRetracedFromPeak(pips=100.0), ctx
        ) is True

    def test_retrace_does_not_trigger(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4720.0)  # peak = 200
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4716.0),   # now +160 → retrace 40
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitRetracedFromPeak(pips=100.0), ctx
        ) is False

    def test_never_in_profit_is_false(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        """Peak was never positive → retrace = 0 → any positive threshold False."""
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4685.0)  # only losing
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4685.0),
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitRetracedFromPeak(pips=50.0), ctx
        ) is False

    def test_new_high_floors_at_zero(
        self, eval_ctx, fake_live, sample_plan, tracker
    ):
        _seed_buy_at_4700(tracker, sample_plan.id)
        tracker.update_price(sample_plan.id, 4710.0)  # peak = 100
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4720.0),   # new high
            ticket=111, plan=sample_plan,
        )
        assert evaluate_condition(
            ProfitRetracedFromPeak(pips=1.0), ctx
        ) is False
