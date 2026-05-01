"""Phase 7.3 (FLO-355) — Cat A primitive evaluator tests.

Covers the 4 new condition primitives + extended Fibonacci levels +
optional fib tolerance:
  * bollinger_position (5 relations)
  * stochastic
  * price_at_pivot (Classic + Fibonacci sets, all levels)
  * indicator_divergence (MACD bull / bear)
  * extended FibLevel literal + tolerance_pips override

All evaluators follow the v1 fail-safe contract: missing data → False
(NEVER raise). Brain publishes the underlying values via
`_last_agent_data["indicators"]` / `["pivot_points"]`; SemanticCache
reads them; LiveData accessors expose them; evaluators consume them.
The contract is read-only; no state caching.
"""
from __future__ import annotations

import pytest

from snow.evaluators.context import EvalContext
from snow.evaluators.dispatch import evaluate_condition, _DISPATCH
from snow.schema import (
    BollingerPosition,
    IndicatorDivergence,
    PriceAtFibonacci,
    PriceAtPivot,
    Stochastic,
)


# ---------------------------------------------------------------------------
# Helpers — build a minimal EvalContext for primitive tests.
# ---------------------------------------------------------------------------

def _ctx(eval_ctx, **kwargs):
    """Wrap the conftest factory so each test passes only what it needs."""
    return eval_ctx(**kwargs)


# ===========================================================================
# TestDispatchRegistry
# ===========================================================================

class TestDispatchRegistry:
    def test_all_four_new_types_registered(self):
        for t in ("bollinger_position", "stochastic",
                  "price_at_pivot", "indicator_divergence"):
            assert t in _DISPATCH, f"missing dispatch entry for {t!r}"

    def test_total_dispatch_count_is_18(self):
        """14 v1 primitives + 4 Phase 7.3 additions."""
        assert len(_DISPATCH) == 18


# ===========================================================================
# TestBollingerPosition
# ===========================================================================

class TestBollingerPosition:
    def test_above_upper_when_position_above_1(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0,
                   "position": 1.05, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="above_upper")
        assert evaluate_condition(cond, ctx) is True

    def test_above_upper_false_when_position_at_1(self, fake_live, fake_semantic, eval_ctx):
        # position == 1.0 is the band itself, not "above"
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0,
                   "position": 1.0, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="above_upper")
        assert evaluate_condition(cond, ctx) is False

    def test_below_lower_when_position_negative(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0,
                   "position": -0.1, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="below_lower")
        assert evaluate_condition(cond, ctx) is True

    def test_above_middle_at_high_position(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0,
                   "position": 0.8, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        assert evaluate_condition(
            BollingerPosition(tf="H1", relation="above_middle"), ctx
        ) is True
        assert evaluate_condition(
            BollingerPosition(tf="H1", relation="below_middle"), ctx
        ) is False

    def test_below_middle_at_low_position(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0,
                   "position": 0.2, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        assert evaluate_condition(
            BollingerPosition(tf="H1", relation="below_middle"), ctx
        ) is True

    def test_in_squeeze_true(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4725.0, "middle": 4720.0, "lower": 4715.0,
                   "position": 0.5, "squeeze": True}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="in_squeeze")
        assert evaluate_condition(cond, ctx) is True

    def test_in_squeeze_false_when_squeeze_field_missing(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730.0, "middle": 4720.0, "lower": 4710.0, "position": 0.5}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="in_squeeze")
        assert evaluate_condition(cond, ctx) is False

    def test_missing_data_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live()  # no bollinger
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="H1", relation="above_upper")
        assert evaluate_condition(cond, ctx) is False

    def test_non_h1_tf_returns_false_today(self, fake_live, fake_semantic, eval_ctx):
        """Brain only computes BB on H1; M1/M5/M15/H4/D1 calls return None
        from LiveData → evaluator False."""
        live = fake_live(bollinger_by_tf={
            "H1": {"upper": 4730, "middle": 4720, "lower": 4710,
                   "position": 1.5, "squeeze": False}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = BollingerPosition(tf="M5", relation="above_upper")
        assert evaluate_condition(cond, ctx) is False


# ===========================================================================
# TestStochastic
# ===========================================================================

class TestStochastic:
    def test_above_threshold(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(stochastic_by_tf={"H1": 78.0})
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = Stochastic(tf="H1", op="above", threshold=70)
        assert evaluate_condition(cond, ctx) is True

    def test_below_threshold(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(stochastic_by_tf={"H1": 18.0})
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = Stochastic(tf="H1", op="below", threshold=20)
        assert evaluate_condition(cond, ctx) is True

    def test_strict_inequality_at_threshold(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(stochastic_by_tf={"H1": 70.0})
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        # 70 > 70 is False
        cond = Stochastic(tf="H1", op="above", threshold=70)
        assert evaluate_condition(cond, ctx) is False

    def test_missing_data_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live()
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = Stochastic(tf="H1", op="above", threshold=50)
        assert evaluate_condition(cond, ctx) is False


# ===========================================================================
# TestPriceAtPivot
# ===========================================================================

_PIVOTS = {
    "classic":   {"PP": 4720.0, "R1": 4730.0, "R2": 4740.0, "R3": 4750.0,
                  "S1": 4710.0, "S2": 4700.0, "S3": 4690.0},
    "fibonacci": {"PP": 4720.0, "R1": 4727.5, "R2": 4732.5, "R3": 4740.0,
                  "S1": 4712.5, "S2": 4707.5, "S3": 4700.0},
}


class TestPriceAtPivot:
    def test_at_classic_pp_within_tolerance(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(price_mid=4720.3, pivot_points_dict=_PIVOTS)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = PriceAtPivot(pivot_set="classic", level="PP", tolerance_pips=5)
        assert evaluate_condition(cond, ctx) is True

    def test_at_classic_r1_exact(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(price_mid=4730.0, pivot_points_dict=_PIVOTS)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = PriceAtPivot(pivot_set="classic", level="R1", tolerance_pips=2)
        assert evaluate_condition(cond, ctx) is True

    def test_outside_tolerance_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(price_mid=4725.0, pivot_points_dict=_PIVOTS)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        # PP is 4720, price is 4725 = 50 pips off; tolerance 5 pips → False
        cond = PriceAtPivot(pivot_set="classic", level="PP", tolerance_pips=5)
        assert evaluate_condition(cond, ctx) is False

    def test_fibonacci_set_distinct_from_classic(self, fake_live, fake_semantic, eval_ctx):
        # Classic R1 = 4730, Fib R1 = 4727.5 — 25 pips apart
        live = fake_live(price_mid=4727.5, pivot_points_dict=_PIVOTS)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        # Fib R1 hits at price 4727.5 with tight tolerance
        assert evaluate_condition(
            PriceAtPivot(pivot_set="fibonacci", level="R1", tolerance_pips=2),
            ctx,
        ) is True
        # Classic R1 (4730) does NOT hit at 4727.5 with the same tolerance
        assert evaluate_condition(
            PriceAtPivot(pivot_set="classic", level="R1", tolerance_pips=2),
            ctx,
        ) is False

    def test_missing_pivot_data_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(price_mid=4720.0)  # no pivot_points
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = PriceAtPivot(pivot_set="classic", level="PP", tolerance_pips=5)
        assert evaluate_condition(cond, ctx) is False

    def test_multilayer_daily_wrapper_accepted(self, fake_live, fake_semantic, eval_ctx):
        """Brain may wrap pivots in {'daily': {...}} — LiveData accessor
        unwraps automatically."""
        wrapped = {"daily": _PIVOTS, "weekly": {"classic": {"PP": 9999}}}
        live = fake_live(price_mid=4720.0, pivot_points_dict=wrapped)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = PriceAtPivot(pivot_set="classic", level="PP", tolerance_pips=5)
        assert evaluate_condition(cond, ctx) is True

    def test_all_seven_levels_resolvable(self, fake_live, fake_semantic, eval_ctx):
        """Smoke: every level enum value resolves cleanly when data exists.
        XAUUSD PIP_SIZE=0.1, so tolerance_pips=400 = 40 price units, which
        covers the |4720 - {S3=4690 or R3=4750}| = 30 unit gap with margin."""
        live = fake_live(price_mid=4720.0, pivot_points_dict=_PIVOTS)
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        for level in ("PP", "R1", "R2", "R3", "S1", "S2", "S3"):
            cond = PriceAtPivot(pivot_set="classic", level=level, tolerance_pips=400)
            assert evaluate_condition(cond, ctx) is True, level


# ===========================================================================
# TestIndicatorDivergence
# ===========================================================================

class TestIndicatorDivergence:
    def test_bullish_macd_divergence_detected(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(macd_div_by_tf={
            "H1": {"detected": True, "type": "bullish", "bars_since": 2}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = IndicatorDivergence(indicator="macd", direction="bullish")
        assert evaluate_condition(cond, ctx) is True

    def test_bearish_macd_divergence_detected(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(macd_div_by_tf={
            "H1": {"detected": True, "type": "bearish", "bars_since": 5}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = IndicatorDivergence(indicator="macd", direction="bearish")
        assert evaluate_condition(cond, ctx) is True

    def test_direction_mismatch_returns_false(self, fake_live, fake_semantic, eval_ctx):
        """Detected=True but type=bullish; condition asks for bearish → False."""
        live = fake_live(macd_div_by_tf={
            "H1": {"detected": True, "type": "bullish", "bars_since": 2}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = IndicatorDivergence(indicator="macd", direction="bearish")
        assert evaluate_condition(cond, ctx) is False

    def test_not_detected_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live(macd_div_by_tf={
            "H1": {"detected": False, "type": None}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = IndicatorDivergence(indicator="macd", direction="bullish")
        assert evaluate_condition(cond, ctx) is False

    def test_missing_data_returns_false(self, fake_live, fake_semantic, eval_ctx):
        live = fake_live()
        ctx = eval_ctx(live_data=live, semantic_cache=fake_semantic())
        cond = IndicatorDivergence(indicator="macd", direction="bullish")
        assert evaluate_condition(cond, ctx) is False


# ===========================================================================
# TestExtendedFibonacci
# ===========================================================================

class TestExtendedFibonacci:
    """v1 supported only 0.382 / 0.5 / 0.618 / 0.786. Phase 7.3 extends
    to include 0.236 / 1.0 / 1.272 / 1.618 (extension levels)."""

    @pytest.mark.parametrize("level", [0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618])
    def test_all_levels_accepted_by_pydantic(self, level):
        """Round-trip through Pydantic — schema accepts every extended level."""
        cond = PriceAtFibonacci(level=level)
        assert cond.level == level

    def test_invalid_level_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PriceAtFibonacci(level=0.9)  # not in the literal set

    def test_explicit_tolerance_overrides_default(self, fake_live, fake_semantic, eval_ctx):
        """The 5-pip default is implicit. Phase 7.3 adds an optional
        tolerance_pips field — explicit value must take effect."""
        # Build a semantic cache returning fib level price 4720.5 → price
        # 4715 is 55 pips away.
        live = fake_live(price_mid=4715.0)
        sem = fake_semantic({
            "fibonacci": {"levels": [{"pct": "61.8", "price": 4720.5}]}
        })
        ctx = eval_ctx(live_data=live, semantic_cache=sem)

        # Default 5-pip tolerance: 55 pips off → False
        cond_default = PriceAtFibonacci(level=0.618)
        assert evaluate_condition(cond_default, ctx) is False

        # Wide explicit tolerance: 100 pips → True
        cond_wide = PriceAtFibonacci(level=0.618, tolerance_pips=100)
        assert evaluate_condition(cond_wide, ctx) is True


# ===========================================================================
# Plan-level integration: the new primitives are valid in a Plan
# ===========================================================================

class TestPlanIntegration:
    def test_plan_with_phase73_conditions_validates(self):
        """A Plan that mixes v1 + Phase 7.3 primitives must round-trip
        cleanly through Pydantic."""
        from copy import deepcopy
        from snow.tests.conftest import _BASE_PLAN  # type: ignore
        # _BASE_PLAN may not be importable directly — fall back to a
        # canonical dict if needed.
        try:
            d = deepcopy(_BASE_PLAN)
        except Exception:
            # Inline canonical (mirrors snow/tests/conftest.py _BASE_PLAN)
            d = {
                "schema_version": 1,
                "id": "PLAN-20260424-001",
                "created_by": "floki",
                "created_at": "2026-04-24T08:00:00Z",
                "expires_at": "2026-04-24T12:00:00Z",
                "status": "pending",
                "analysis": {
                    "thesis": "test",
                    "key_levels": [4735.0, 4720.0, 4707.0],
                    "confidence": 75,
                    "regime_assumed": "TRENDING_BEARISH",
                },
                "entry": {
                    "direction": "SELL",
                    "volume": 0.02,
                    "conditions": [{"type": "price_above", "level": 4730.0}],
                    "initial_sl": 4740.0,
                    "initial_tp": 4710.0,
                },
                "management": [],
                "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
                "emergency": {
                    "max_loss_pips": 150, "max_duration_minutes": 480,
                    "on_broker_error": "alert_floki",
                },
            }

        # Replace entry conditions with a 3-way Phase 7.3 confluence:
        # BB above upper AND Stoch > 70 AND MACD bearish divergence.
        d["entry"]["conditions"] = [
            {"type": "bollinger_position", "tf": "H1", "relation": "above_upper"},
            {"type": "stochastic", "tf": "H1", "op": "above", "threshold": 70},
            {"type": "indicator_divergence", "indicator": "macd",
             "direction": "bearish"},
        ]
        # Add a pivot-based exit
        d["exit"] = [{
            "name": "pp_pullback_exit",
            "priority": 9,
            "conditions": [{"type": "price_at_pivot",
                            "pivot_set": "classic", "level": "PP",
                            "tolerance_pips": 3}],
            "action": {"type": "close_full"},
            "fires": "once",
        }]

        from snow.validator import validate_plan
        ok, plan, errors = validate_plan(d)
        assert ok, f"plan with Phase 7.3 conditions failed validation: {errors}"
        assert plan is not None

    def test_extended_fib_level_in_plan(self):
        """Fib 1.0 (extension) was rejected in v1 — must be accepted now."""
        d = {
            "schema_version": 1,
            "id": "PLAN-20260424-001",
            "created_by": "floki",
            "created_at": "2026-04-24T08:00:00Z",
            "expires_at": "2026-04-24T12:00:00Z",
            "status": "pending",
            "analysis": {"thesis": "test", "key_levels": [4720.0],
                         "confidence": 75, "regime_assumed": "RANGING"},
            "entry": {
                "direction": "BUY", "volume": 0.02,
                # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
                # Test purpose is fib-level acceptance, not entry shape — second
                # condition (rsi H1 > 50) is benign and orthogonal.
                "conditions": [
                    {"type": "price_at_fibonacci", "level": 1.0,
                     "tolerance_pips": 8},
                    {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
                ],
                "initial_sl": 4710.0, "initial_tp": 4730.0,
            },
            "management": [], "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
            "emergency": {"max_loss_pips": 150, "max_duration_minutes": 480,
                          "on_broker_error": "alert_floki"},
        }
        from snow.validator import validate_plan
        ok, plan, errors = validate_plan(d)
        assert ok, f"fib level 1.0 rejected: {errors}"
