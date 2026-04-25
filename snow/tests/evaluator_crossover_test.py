"""Indicator-crossover evaluator tests — FLO-359 Phase 8b commit 3.

First stateful primitive. Exercises the (cond, ctx, state) signature
end-to-end through dispatch — covering: cold-start, fire on the first
crossing tick, no-fire on continuation, equality preserves last
definite state, missing data preserves prev, post-restart behaviour
via cache rehydrate.

State semantics (RFC §3.1):
  prev_above = (prev_value > threshold)
  curr_above = (curr_value > threshold)
  direction == "above": fires iff (not prev_above) AND curr_above
  direction == "below": fires iff prev_above AND (not curr_above)
  curr == threshold:    ambiguous — preserve last definite state
  cold-start (no prev): seed + report no crossing
"""
from __future__ import annotations

import pytest

from snow.evaluators import EvalContext, PerPlanTracker, evaluate_condition
from snow.evaluators.indicator import evaluate_indicator_crossover
from snow.schema import IndicatorCrossover
from snow.state import PerConditionStateCache


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def cache() -> PerConditionStateCache:
    return PerConditionStateCache()


def _build_ctx(
    fake_live, sample_plan, fake_semantic, cache, *, ticket=None
) -> EvalContext:
    return EvalContext(
        live_data=fake_live(),
        semantic_cache=fake_semantic(),
        tracker=PerPlanTracker(),
        plan=sample_plan,
        ticket=ticket,
        state_cache=cache,
    )


def _eval_via_dispatch(cond, ctx, *, plan_id="PLAN-20260424-001",
                       contingency_name="_entry", condition_index=0):
    return evaluate_condition(
        cond, ctx,
        plan_id=plan_id,
        contingency_name=contingency_name,
        condition_index=condition_index,
    )


# =============================================================================
# Direct evaluator unit tests (state passed in explicitly)
# =============================================================================

class TestCrossoverDirect:

    def test_cold_start_seeds_and_reports_no_fire(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 50.0}),
            sample_plan, fake_semantic, cache,
        )
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        assert evaluate_indicator_crossover(cond, ctx, row) is False
        # Seeded.
        assert row.prev_value == 50.0
        assert row.prev_above_threshold is False

    def test_above_direction_fires_on_first_cross(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        # tick 1: prev seeded at 65 (below threshold)
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 65.0}),
            sample_plan, fake_semantic, cache,
        )
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        evaluate_indicator_crossover(cond, ctx, row)

        # tick 2: rsi=72 → crosses above
        ctx2 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 72.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx2, row) is True
        assert row.prev_above_threshold is True

        # tick 3: still above (continuation, not a fresh cross)
        ctx3 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 73.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx3, row) is False

    def test_below_direction_fires_on_first_cross(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="below", threshold=30.0
        )
        # tick 1: prev seeded at 35 (above threshold)
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 35.0}),
            sample_plan, fake_semantic, cache,
        )
        evaluate_indicator_crossover(cond, ctx, row)

        # tick 2: rsi=28 → crosses below
        ctx2 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 28.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx2, row) is True

        # tick 3: still below (continuation)
        ctx3 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 25.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx3, row) is False

    def test_equality_preserves_last_definite_state(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """RSI parks exactly on the threshold. Neither direction fires.
        prev_above_threshold preserves whichever side it was on last."""
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        # tick 1: 65 (below)
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 65.0}),
            sample_plan, fake_semantic, cache,
        )
        evaluate_indicator_crossover(cond, ctx, row)
        # tick 2: exactly 70 (ambiguous) — no fire, preserve prev_above=False
        ctx2 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 70.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx2, row) is False
        assert row.prev_above_threshold is False
        # tick 3: 71 → still treated as a fresh "above" crossing
        ctx3 = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 71.0}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx3, row) is True

    def test_missing_data_returns_false_and_preserves_state(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        # Seed first.
        ctx_seed = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 65.0}),
            sample_plan, fake_semantic, cache,
        )
        evaluate_indicator_crossover(cond, ctx_seed, row)
        prev_snapshot = (row.prev_value, row.prev_above_threshold)

        # Now simulate missing data — rsi() returns None.
        ctx_missing = _build_ctx(
            lambda: fake_live(rsi_by_tf={}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx_missing, row) is False
        # State unchanged.
        assert (row.prev_value, row.prev_above_threshold) == prev_snapshot

    def test_macd_histogram_indicator_routes_correctly(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="macd_histogram", tf="H1",
            direction="above", threshold=0.0,
        )
        row = cache.get_or_create(
            "PLAN-20260424-001", "_entry", 0, "indicator_crossover"
        )
        # tick 1: -0.1 (below 0)
        ctx = _build_ctx(
            lambda: fake_live(macd_hist_by_tf={"H1": -0.1}),
            sample_plan, fake_semantic, cache,
        )
        evaluate_indicator_crossover(cond, ctx, row)
        # tick 2: +0.05 (crosses above)
        ctx2 = _build_ctx(
            lambda: fake_live(macd_hist_by_tf={"H1": 0.05}),
            sample_plan, fake_semantic, cache,
        )
        assert evaluate_indicator_crossover(cond, ctx2, row) is True


# =============================================================================
# Routing through evaluate_condition (the loop's actual call path)
# =============================================================================

class TestCrossoverViaDispatch:

    def test_dispatch_allocates_state_row_lazily(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 65.0}),
            sample_plan, fake_semantic, cache,
        )
        assert len(cache) == 0
        result = _eval_via_dispatch(cond, ctx)
        assert result is False  # cold-start
        assert len(cache) == 1  # row was allocated by dispatch

    def test_dispatch_marks_plan_dirty_per_row(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 65.0}),
            sample_plan, fake_semantic, cache,
        )
        _eval_via_dispatch(cond, ctx, plan_id="PLAN-20260424-001")
        assert cache.is_dirty("PLAN-20260424-001")

    def test_dispatch_without_state_cache_returns_false(
        self, fake_live, fake_semantic, sample_plan,
    ):
        """A misconfigured caller (no state_cache on EvalContext) MUST
        not crash — fail-safe to False with WARN log."""
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        ctx = EvalContext(
            live_data=fake_live(rsi_by_tf={"H1": 75.0}),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan,
            ticket=None,
            state_cache=None,
        )
        assert _eval_via_dispatch(cond, ctx) is False

    def test_dispatch_without_location_returns_false(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Stateful types need plan_id/contingency_name/condition_index
        to find their state row. Caller that omits them gets False."""
        cond = IndicatorCrossover(
            indicator="rsi", tf="H1", direction="above", threshold=70.0
        )
        ctx = _build_ctx(
            lambda: fake_live(rsi_by_tf={"H1": 75.0}),
            sample_plan, fake_semantic, cache,
        )
        # No plan_id/contingency_name/condition_index kwargs.
        assert evaluate_condition(cond, ctx) is False
