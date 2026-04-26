"""Indicator-was evaluator tests — FLO-359 Phase 8b commit 4.

Sliding-window primitive: did `indicator op threshold` hold in any of
the last `within_bars` closed bars on `tf`? Updated on bar boundaries
via `prev_bar_close_at` dedupe; cold-start has empty history → False
until the first bar boundary is observed.

Coverage:
  * Schema: within_bars 1..20 cap (Pydantic), 21+ rejected
  * Cold-start: tick 1 returns False, prev_bar_close_at seeded
  * Bar dedup: multiple ticks within one bar → one append
  * Bar roll: ctx.now advanced past tf boundary → append + cap
  * Window slide: 6 bars on within_bars=4 → 4 most-recent retained
  * within_bars=1 corner — at most 1 element in history
  * within_bars=20 ceiling — no overflow on heavy churn
  * op semantics: above / below; equality does NOT satisfy either
  * Missing data preserves state (no append on None)
  * Dispatch routing: lazy state allocation + per-row dirty flag
"""
from __future__ import annotations

import datetime as _dt

import pytest

from snow.evaluators import EvalContext, PerPlanTracker, evaluate_condition
from snow.evaluators.indicator import evaluate_indicator_was, _bar_open_iso
from snow.schema import IndicatorWas
from snow.state import PerConditionStateCache


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def cache() -> PerConditionStateCache:
    return PerConditionStateCache()


def _ctx(
    *,
    fake_live, fake_semantic, sample_plan, cache,
    rsi_value=None, now=None,
) -> EvalContext:
    return EvalContext(
        live_data=fake_live(rsi_by_tf={"H1": rsi_value} if rsi_value is not None else {}),
        semantic_cache=fake_semantic(),
        tracker=PerPlanTracker(),
        plan=sample_plan,
        ticket=None,
        now=now,
        state_cache=cache,
    )


def _row(cache, plan_id="PLAN-20260424-001",
         contingency_name="_entry", condition_index=0):
    return cache.get_or_create(
        plan_id, contingency_name, condition_index, "indicator_was"
    )


# =============================================================================
# Schema bounds
# =============================================================================

class TestIndicatorWasSchema:

    def test_within_bars_1_accepted(self):
        c = IndicatorWas(
            indicator="rsi", tf="H1", op="below",
            threshold=30.0, within_bars=1,
        )
        assert c.within_bars == 1

    def test_within_bars_20_accepted(self):
        c = IndicatorWas(
            indicator="rsi", tf="H1", op="below",
            threshold=30.0, within_bars=20,
        )
        assert c.within_bars == 20

    def test_within_bars_21_rejected(self):
        with pytest.raises(Exception):
            IndicatorWas(
                indicator="rsi", tf="H1", op="below",
                threshold=30.0, within_bars=21,
            )

    def test_within_bars_0_rejected(self):
        with pytest.raises(Exception):
            IndicatorWas(
                indicator="rsi", tf="H1", op="below",
                threshold=30.0, within_bars=0,
            )


# =============================================================================
# Direct evaluator unit tests (state passed in explicitly)
# =============================================================================

class TestIndicatorWasDirect:

    COND = IndicatorWas(
        indicator="rsi", tf="H1", op="below",
        threshold=30.0, within_bars=4,
    )

    def test_cold_start_returns_false_and_seeds_bar_id(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        now = _dt.datetime(2026, 4, 26, 14, 32, 0, tzinfo=_dt.timezone.utc)
        ctx = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache,
            rsi_value=25.0, now=now,
        )
        row = _row(cache)
        assert evaluate_indicator_was(self.COND, ctx, row) is False
        assert row.bar_history == []
        # H1 floor of 14:32:00 = 14:00:00.
        assert row.prev_bar_close_at == "2026-04-26T14:00:00Z"
        assert row.bar_history_max_n == 4

    def test_dedup_within_same_bar(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Five ticks within the same H1 bar → no appends after the
        cold-start seed (history stays empty)."""
        row = _row(cache)
        for minute in (32, 35, 41, 47, 59):
            now = _dt.datetime(
                2026, 4, 26, 14, minute, 0, tzinfo=_dt.timezone.utc,
            )
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache,
                rsi_value=25.0, now=now,
            )
            evaluate_indicator_was(self.COND, ctx, row)
        assert row.prev_bar_close_at == "2026-04-26T14:00:00Z"
        assert row.bar_history == []

    def test_bar_roll_appends_one_value(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        # Tick 1: 14:32 — cold-start seed.
        ctx1 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=40.0,
            now=_dt.datetime(2026, 4, 26, 14, 32, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(self.COND, ctx1, row)
        # Tick 2: 15:01 — H1 boundary rolled. RSI=25 (below threshold).
        ctx2 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=25.0,
            now=_dt.datetime(2026, 4, 26, 15, 1, 0, tzinfo=_dt.timezone.utc),
        )
        result = evaluate_indicator_was(self.COND, ctx2, row)
        assert row.bar_history == [25.0]
        assert row.prev_bar_close_at == "2026-04-26T15:00:00Z"
        assert result is True  # 25 < 30 — fires

    def test_window_slides_at_capacity(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Six bar rolls on within_bars=4 → only the 4 most recent
        values retained (oldest popped)."""
        row = _row(cache)
        # Cold start
        ctx = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 26, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(self.COND, ctx, row)

        # 6 successive H1 bars with distinct RSI values.
        rsi_per_bar = [22.0, 35.0, 28.0, 45.0, 50.0, 55.0]
        for h, rsi in zip(range(1, 7), rsi_per_bar):
            now = _dt.datetime(
                2026, 4, 26, h, 5, 0, tzinfo=_dt.timezone.utc,
            )
            ctx_h = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache,
                rsi_value=rsi, now=now,
            )
            evaluate_indicator_was(self.COND, ctx_h, row)

        # Window of 4 → last 4 values retained: 28, 45, 50, 55.
        assert row.bar_history == [28.0, 45.0, 50.0, 55.0]
        # 28.0 is below 30 → fires.
        assert any(v < 30.0 for v in row.bar_history)

    def test_above_op_fires_when_any_value_satisfies(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorWas(
            indicator="rsi", tf="H1", op="above",
            threshold=70.0, within_bars=3,
        )
        row = _row(cache)
        # Cold-start seed.
        ctx0 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 26, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(cond, ctx0, row)
        # Three bars: 65, 75, 60. 75 > 70 → fires.
        for h, rsi in zip((1, 2, 3), (65.0, 75.0, 60.0)):
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache,
                rsi_value=rsi,
                now=_dt.datetime(2026, 4, 26, h, 5, 0, tzinfo=_dt.timezone.utc),
            )
            evaluate_indicator_was(cond, ctx, row)
        # Now the most recent reading (60) is below threshold but the
        # window still contains 75 → fires.
        ctx_check = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache,
            rsi_value=60.0,
            now=_dt.datetime(2026, 4, 26, 3, 30, 0, tzinfo=_dt.timezone.utc),
        )
        assert evaluate_indicator_was(cond, ctx_check, row) is True

    def test_equality_does_not_satisfy_either_op(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond_above = IndicatorWas(
            indicator="rsi", tf="H1", op="above",
            threshold=70.0, within_bars=2,
        )
        row = _row(cache)
        # Seed
        ctx0 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 26, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(cond_above, ctx0, row)
        # Bar with RSI exactly at threshold.
        ctx_eq = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=70.0,
            now=_dt.datetime(2026, 4, 26, 1, 5, 0, tzinfo=_dt.timezone.utc),
        )
        assert evaluate_indicator_was(cond_above, ctx_eq, row) is False
        assert row.bar_history == [70.0]

    def test_missing_data_no_append_and_state_preserved(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        # Seed first
        ctx0 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 26, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(self.COND, ctx0, row)
        snap = (list(row.bar_history), row.prev_bar_close_at)

        # Now: bar would roll, but RSI returns None — no append, no
        # bar_id update.
        ctx_missing = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=None,
            now=_dt.datetime(2026, 4, 26, 1, 5, 0, tzinfo=_dt.timezone.utc),
        )
        assert evaluate_indicator_was(self.COND, ctx_missing, row) is False
        assert (list(row.bar_history), row.prev_bar_close_at) == snap

    def test_within_bars_1_corner(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorWas(
            indicator="rsi", tf="H1", op="below",
            threshold=30.0, within_bars=1,
        )
        row = _row(cache)
        # Seed
        ctx0 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 26, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(cond, ctx0, row)
        # Three bar rolls; only the most recent value retained.
        for h, rsi in zip((1, 2, 3), (22.0, 35.0, 50.0)):
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache,
                rsi_value=rsi,
                now=_dt.datetime(2026, 4, 26, h, 5, 0, tzinfo=_dt.timezone.utc),
            )
            evaluate_indicator_was(cond, ctx, row)
        assert row.bar_history == [50.0]  # only most recent

    def test_within_bars_20_max_no_overflow(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorWas(
            indicator="rsi", tf="H1", op="below",
            threshold=30.0, within_bars=20,
        )
        row = _row(cache)
        # Seed at hour 0
        ctx0 = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, rsi_value=50.0,
            now=_dt.datetime(2026, 4, 25, 0, 30, 0, tzinfo=_dt.timezone.utc),
        )
        evaluate_indicator_was(cond, ctx0, row)
        # 25 successive bars on a multi-day span (capped at 20).
        for n in range(25):
            now = _dt.datetime(
                2026, 4, 25, 0, 0, 0, tzinfo=_dt.timezone.utc,
            ) + _dt.timedelta(hours=n + 1, minutes=5)
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache,
                rsi_value=float(n), now=now,
            )
            evaluate_indicator_was(cond, ctx, row)
        assert len(row.bar_history) == 20


# =============================================================================
# Routing through evaluate_condition
# =============================================================================

class TestIndicatorWasViaDispatch:

    def test_dispatch_lazy_allocates_and_marks_dirty(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = IndicatorWas(
            indicator="rsi", tf="H1", op="below",
            threshold=30.0, within_bars=4,
        )
        ctx = EvalContext(
            live_data=fake_live(rsi_by_tf={"H1": 25.0}),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan,
            ticket=None,
            now=_dt.datetime(2026, 4, 26, 14, 32, 0, tzinfo=_dt.timezone.utc),
            state_cache=cache,
        )
        assert len(cache) == 0
        evaluate_condition(
            cond, ctx,
            plan_id="PLAN-20260424-001",
            contingency_name="_entry",
            condition_index=0,
        )
        assert len(cache) == 1
        assert cache.is_dirty("PLAN-20260424-001")
