"""Dispatch tests — evaluate_condition() routing + fail-safe paths.

Covers:
  * All 14 registered condition types are in the dispatch table
  * Unknown condition type → False (and logs WARN)
  * Condition without a .type attribute → False
  * Evaluator that raises → False (and logs WARN)
  * Registry is not mutable from outside
"""
from __future__ import annotations

import logging

import pytest

from snow.evaluators import dispatch as dispatch_mod
from snow.evaluators.dispatch import (
    evaluate_condition,
    registered_condition_types,
)
from snow.schema import PriceAbove


# =============================================================================
# Registry completeness
# =============================================================================

class TestRegistry:

    def test_all_primitives_registered(self):
        """14 v1 + 4 Phase 7.3 Cat A + 3 Phase 8b stateful = 21."""
        expected = {
            # v1 (RFC §2.5)
            "price_above", "price_below",
            "rsi", "macd_histogram", "ema_relation", "atr",
            "price_at_sr_zone", "price_at_fibonacci",
            "profit_pips", "mfe_reached", "mae_reached",
            "profit_retraced_from_peak",
            "duration_exceeds", "time_between",
            # Phase 7.3 (FLO-355) — Cat A indicator additions
            "bollinger_position", "stochastic",
            "price_at_pivot", "indicator_divergence",
            # Phase 8b (FLO-359) — stateful additions
            "indicator_crossover", "indicator_was", "price_crossed_level",
        }
        assert set(registered_condition_types()) == expected
        assert len(expected) == 21

    def test_registered_types_returns_copy(self):
        """Mutation of the return value must not affect the live table."""
        listing = registered_condition_types()
        listing.append("injected_type")
        assert "injected_type" not in registered_condition_types()


# =============================================================================
# Happy-path routing (sanity — deeper coverage in primitives_test.py)
# =============================================================================

class TestRouting:

    def test_price_above_routes(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4740.0))
        assert evaluate_condition(PriceAbove(level=4730.0), ctx) is True


# =============================================================================
# Fail-safe paths
# =============================================================================

class _FakeUnknown:
    """Mimics a Pydantic condition with an unknown discriminator."""
    type = "unknown_condition"


class _FakeNoType:
    """Object missing the .type attribute entirely."""
    pass


class TestFailSafe:

    def test_unknown_type_returns_false(self, eval_ctx, fake_live, caplog):
        ctx = eval_ctx(live_data=fake_live())
        with caplog.at_level(logging.WARNING, logger="snow.evaluators.dispatch"):
            result = evaluate_condition(_FakeUnknown(), ctx)
        assert result is False
        # Log evidence: "unknown condition type" warning was emitted
        assert any("unknown condition type" in rec.getMessage()
                   for rec in caplog.records)

    def test_no_type_attribute_returns_false(
        self, eval_ctx, fake_live, caplog
    ):
        ctx = eval_ctx(live_data=fake_live())
        with caplog.at_level(logging.WARNING, logger="snow.evaluators.dispatch"):
            result = evaluate_condition(_FakeNoType(), ctx)
        assert result is False
        assert caplog.records  # at least one warning

    def test_evaluator_raising_returns_false(
        self, eval_ctx, fake_live, caplog, monkeypatch
    ):
        """If a primitive raises (which it shouldn't — always return bool),
        dispatch must catch, log WARN, and return False."""
        def _boom(cond, ctx):
            raise RuntimeError("simulated primitive bug")

        # Patch the live dispatch table
        original = dispatch_mod._DISPATCH["price_above"]
        monkeypatch.setitem(dispatch_mod._DISPATCH, "price_above", _boom)
        try:
            ctx = eval_ctx(live_data=fake_live(price_mid=4740.0))
            with caplog.at_level(logging.WARNING, logger="snow.evaluators.dispatch"):
                result = evaluate_condition(PriceAbove(level=4730.0), ctx)
            assert result is False
            assert any("raised" in rec.getMessage() for rec in caplog.records)
        finally:
            dispatch_mod._DISPATCH["price_above"] = original
