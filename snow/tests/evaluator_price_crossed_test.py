"""price_crossed_level evaluator tests — FLO-359 Phase 8b commit 5.

One-shot latch primitive: fires when mid-price crosses `level` in
`direction`, then returns True for the rest of the plan's lifetime
(no mid-plan reset per CEO Q3). Restart-safe: the `latched` field
on `ConditionStateRow` round-trips through state_cache_json.

Coverage:
  * Cold-start seeds prev, returns False
  * Strict cross (above): prev<level, curr>level → fires
  * Inclusive cross (above): curr lands exactly on level → fires
  * Strict and inclusive crosses for "below" direction
  * Latch persists across subsequent ticks even when price reverts
  * Latch returns True even on missing data (latch check runs before
    price read)
  * Missing data on cold-start preserves state (no mutation)
  * Dispatch routing: lazy allocation + per-row dirty flag
  * Restart simulation: flush → fresh cache → rehydrate → latch
    preserved
"""
from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

import pytest

from snow import db as snow_db
from snow.evaluators import EvalContext, PerPlanTracker, evaluate_condition
from snow.evaluators.price import evaluate_price_crossed_level
from snow.schema import Plan, PriceCrossedLevel
from snow.state import PerConditionStateCache


# =============================================================================
# Helpers
# =============================================================================

@pytest.fixture
def cache() -> PerConditionStateCache:
    return PerConditionStateCache()


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "price_crossed_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


def _ctx(*, fake_live, fake_semantic, sample_plan, cache, price=None):
    return EvalContext(
        live_data=fake_live(price_mid=price),
        semantic_cache=fake_semantic(),
        tracker=PerPlanTracker(),
        plan=sample_plan,
        ticket=None,
        state_cache=cache,
    )


def _row(cache, plan_id="PLAN-20260424-001"):
    return cache.get_or_create(
        plan_id, "_entry", 0, "price_crossed_level"
    )


# =============================================================================
# Direct evaluator
# =============================================================================

class TestPriceCrossedLevelDirect:

    COND_ABOVE = PriceCrossedLevel(direction="above", level=4720.0)
    COND_BELOW = PriceCrossedLevel(direction="below", level=4720.0)

    def test_cold_start_seeds_prev_and_returns_false(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        ctx = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4715.0,
        )
        row = _row(cache)
        assert evaluate_price_crossed_level(self.COND_ABOVE, ctx, row) is False
        assert row.prev_value == 4715.0
        assert row.latched is None

    def test_above_strict_cross_fires_and_latches(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        # Seed
        ctx_seed = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4715.0,
        )
        evaluate_price_crossed_level(self.COND_ABOVE, ctx_seed, row)
        # Cross strictly above
        ctx_cross = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4725.0,
        )
        assert evaluate_price_crossed_level(self.COND_ABOVE, ctx_cross, row) is True
        assert row.latched is True

    def test_above_inclusive_cross_fires_on_tag(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Tick lands EXACTLY on the level after coming from below.
        prev<level AND curr>=level → fires (tag-and-bounce semantics)."""
        row = _row(cache)
        ctx_seed = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4719.0,
        )
        evaluate_price_crossed_level(self.COND_ABOVE, ctx_seed, row)
        ctx_tag = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4720.0,
        )
        assert evaluate_price_crossed_level(self.COND_ABOVE, ctx_tag, row) is True
        assert row.latched is True

    def test_below_strict_cross_fires_and_latches(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        ctx_seed = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4725.0,
        )
        evaluate_price_crossed_level(self.COND_BELOW, ctx_seed, row)
        ctx_cross = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4715.0,
        )
        assert evaluate_price_crossed_level(self.COND_BELOW, ctx_cross, row) is True
        assert row.latched is True

    def test_below_inclusive_cross_fires_on_tag(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        ctx_seed = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4721.0,
        )
        evaluate_price_crossed_level(self.COND_BELOW, ctx_seed, row)
        ctx_tag = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4720.0,
        )
        assert evaluate_price_crossed_level(self.COND_BELOW, ctx_tag, row) is True

    def test_latch_persists_when_price_reverts(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Once latched, returns True even when price moves back to the
        original side. Models 'tagged then bounced'."""
        row = _row(cache)
        # Seed below, cross above, then come back below.
        for price in (4715.0, 4725.0, 4710.0, 4700.0):
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache, price=price,
            )
            result = evaluate_price_crossed_level(self.COND_ABOVE, ctx, row)
        # Final tick: price=4700 (well below level), but latched.
        assert row.latched is True
        assert result is True

    def test_latch_returns_true_on_missing_data(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Latch check runs BEFORE the price read — once latched, the
        condition fires even if MT5 disconnects momentarily."""
        row = _row(cache)
        # Get latched first.
        ctx_seed = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4715.0,
        )
        evaluate_price_crossed_level(self.COND_ABOVE, ctx_seed, row)
        ctx_cross = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=4725.0,
        )
        evaluate_price_crossed_level(self.COND_ABOVE, ctx_cross, row)
        assert row.latched is True

        # Now price feed drops. fake_live() with no price_mid kwarg
        # returns None.
        ctx_missing = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=None,
        )
        assert evaluate_price_crossed_level(self.COND_ABOVE, ctx_missing, row) is True

    def test_missing_data_on_cold_start_preserves_state(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        row = _row(cache)
        ctx = _ctx(
            fake_live=fake_live, fake_semantic=fake_semantic,
            sample_plan=sample_plan, cache=cache, price=None,
        )
        assert evaluate_price_crossed_level(self.COND_ABOVE, ctx, row) is False
        assert row.prev_value is None
        assert row.latched is None

    def test_no_cross_no_latch(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        """Price stays on one side of the level. Never fires."""
        row = _row(cache)
        for price in (4710.0, 4712.0, 4715.0, 4718.0, 4719.5):
            ctx = _ctx(
                fake_live=fake_live, fake_semantic=fake_semantic,
                sample_plan=sample_plan, cache=cache, price=price,
            )
            assert evaluate_price_crossed_level(self.COND_ABOVE, ctx, row) is False
        assert row.latched is None


# =============================================================================
# Routing through evaluate_condition
# =============================================================================

class TestPriceCrossedLevelViaDispatch:

    def test_dispatch_lazy_allocates_and_marks_dirty(
        self, fake_live, fake_semantic, sample_plan, cache,
    ):
        cond = PriceCrossedLevel(direction="above", level=4720.0)
        ctx = EvalContext(
            live_data=fake_live(price_mid=4715.0),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan,
            ticket=None,
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


# =============================================================================
# Restart simulation — latch survives flush + rehydrate
# =============================================================================

class TestPriceCrossedLevelRestart:

    def test_latched_row_survives_kill_and_rehydrate(
        self, snow_conn, fake_live, fake_semantic, sample_plan, valid_plan_dict,
    ):
        # Insert a real plan so flush has a row to UPDATE.
        pd = deepcopy(valid_plan_dict)
        pd["id"] = "PLAN-20260424-501"
        pd["status"] = "active"
        snow_db.insert_plan(Plan(**pd))

        cond = PriceCrossedLevel(direction="above", level=4720.0)
        c1 = PerConditionStateCache()

        # Cross + latch using c1.
        row = c1.get_or_create(
            "PLAN-20260424-501", "_entry", 0, "price_crossed_level"
        )
        ctx_seed = EvalContext(
            live_data=fake_live(price_mid=4715.0),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan, ticket=None, state_cache=c1,
        )
        evaluate_price_crossed_level(cond, ctx_seed, row)
        ctx_cross = EvalContext(
            live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan, ticket=None, state_cache=c1,
        )
        evaluate_price_crossed_level(cond, ctx_cross, row)
        assert row.latched is True
        c1.mark_updated("PLAN-20260424-501")
        c1.flush_to_db()

        # "Kill" cache, rehydrate via fresh instance.
        c2 = PerConditionStateCache()
        c2.rehydrate_from_db()

        restored = c2.get("PLAN-20260424-501", "_entry", 0)
        assert restored is not None
        assert restored.latched is True

        # Subsequent eval against c2 should fire immediately even with
        # price reverted below level.
        ctx_post = EvalContext(
            live_data=fake_live(price_mid=4710.0),
            semantic_cache=fake_semantic(),
            tracker=PerPlanTracker(),
            plan=sample_plan, ticket=None, state_cache=c2,
        )
        assert evaluate_price_crossed_level(cond, ctx_post, restored) is True
