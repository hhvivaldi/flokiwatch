"""FLO-395 Phase 1 — tool utilization gap interventions.

Three orthogonal changes verified here:
  * B1 — 8 worked YAML entry-condition examples in agent_prompts.py;
    each must round-trip through validate_plan().
  * C3 — `_format_indicators` annotates each indicator block with a
    `primitive_shape` field showing the YAML template Floki can paste.
  * E2 — `emit_recipe_pulled` extended with `entry_distinct_primitive_types`
    + `entry_distinct_families` + `entry_families` for vocabulary
    diversity tracking. Success metric per Phase 1 stop rule:
    7-day diversity moves from 0.84 → 2.0+.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
from copy import deepcopy
from typing import Any

import pytest

from snow.validator import validate_plan
from snow.schema import Plan
from snow.instrumentation import (
    emit_recipe_pulled,
    _entry_vocabulary_diversity,
    _PRIMITIVE_FAMILY,
)


# =============================================================================
# B1 — Prompt examples must round-trip through validate_plan()
# =============================================================================


def _future_iso(hours: int = 6) -> str:
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _wrap_plan(conditions: list, schema_version: int = 1) -> dict:
    return {
        "schema_version": schema_version,
        "id": "PLAN-20260428-995",
        "created_by": "floki",
        "created_at": "2026-04-28T08:00:00Z",
        "expires_at": _future_iso(6),
        "analysis": {
            "thesis": "FLO-395 prompt-example round-trip test",
            "key_levels": [4500.0, 4510.0, 4520.0],
            "confidence": 60,
            "regime_assumed": "TRENDING_BULLISH",
        },
        "entry": {
            "direction": "BUY",
            "volume": 0.02,
            "conditions": conditions,
            "initial_sl": 4500.0,
            "initial_tp": 4520.0,
            "entry_price": 4510.0,
        },
        "management": [{
            "name": "be",
            "priority": 7,
            "conditions": [{"type": "mfe_reached", "pips": 20.0}],
            "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
            "fires": "once",
        }],
        "exit": [],
        "emergency": {
            "max_loss_pips": 150,
            "max_duration_minutes": 480,
            "on_broker_error": "alert_floki",
        },
    }


# Eight examples replicated from agent_prompts.py ENTRY-CONDITION
# VOCABULARY EXAMPLES section. Any drift between prompt and test
# triggers test failure — locks the prompt examples as a contract.
PROMPT_EXAMPLES = [
    ("bb_squeeze_breakout", 1, [
        {"type": "bollinger_position", "tf": "H1", "relation": "above_upper"},
        {"type": "macd_histogram", "tf": "H1", "op": "above", "threshold": 0.0},
        {"type": "price_at_sr_zone", "zone_type": "any", "tolerance_pips": 5.0},
    ]),
    ("trend_pullback_ma_confluence", 1, [
        {"type": "ema_relation", "tf": "H1", "period": 50, "relation": "aligned_bull"},
        {"type": "price_at_fibonacci", "level": 0.618, "tolerance_pips": 8.0},
        {"type": "stochastic", "tf": "H1", "op": "below", "threshold": 30.0},
    ]),
    ("macd_momentum_continuation", 1, [
        {"type": "macd_histogram", "tf": "H1", "op": "above", "threshold": 0.05},
        {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
        {"type": "price_above", "level": 4720.0},
    ]),
    ("divergence_play_reversal", 1, [
        {"type": "indicator_divergence", "indicator": "macd", "direction": "bearish"},
        {"type": "price_at_sr_zone", "zone_type": "resistance", "tolerance_pips": 5.0},
        {"type": "rsi", "tf": "H1", "op": "above", "threshold": 70},
    ]),
    ("pivot_level_rejection", 1, [
        {"type": "price_at_pivot", "pivot_set": "classic", "level": "R1", "tolerance_pips": 5.0},
        {"type": "stochastic", "tf": "M15", "op": "above", "threshold": 80.0},
        {"type": "rsi", "tf": "M15", "op": "above", "threshold": 70},
    ]),
    ("stateful_crossover_entry", 2, [
        {"type": "indicator_crossover", "indicator": "macd_histogram", "tf": "H1", "direction": "above", "threshold": 0.0},
        {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
    ]),
    ("failed_breakdown_reclaim", 2, [
        {"type": "price_crossed_level", "level": 4707.0, "direction": "below"},
        {"type": "price_above", "level": 4710.0},
        {"type": "indicator_was", "indicator": "rsi", "tf": "H1", "op": "below", "threshold": 30, "within_bars": 4},
    ]),
    ("mtf_trend_alignment_entry", 1, [
        {"type": "ema_relation", "tf": "H4", "period": 50, "relation": "aligned_bull"},
        {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
        {"type": "price_at_sr_zone", "zone_type": "support", "tolerance_pips": 5.0},
    ]),
]


class TestB1PromptExamples:
    """Each of the 8 worked entry-condition examples must validate.
    A drift between agent_prompts.py and the schema would ship a
    broken canonical example to Floki."""

    @pytest.mark.parametrize("name,sv,conds", PROMPT_EXAMPLES,
                             ids=[e[0] for e in PROMPT_EXAMPLES])
    def test_prompt_example_roundtrips(self, name, sv, conds):
        plan = _wrap_plan(conds, schema_version=sv)
        ok, parsed, errors = validate_plan(plan)
        assert ok, f"prompt example {name!r} failed validation: {errors}"

    def test_prompt_examples_present_in_agent_prompts(self):
        """Anchor: agent_prompts.py must contain the FLO-395 vocabulary
        examples block. Locks the prompt-content contract — if a future
        edit drops the examples, this test fails."""
        with open("agent_prompts.py", "r", encoding="utf-8") as f:
            src = f.read()
        assert "ENTRY-CONDITION VOCABULARY EXAMPLES (FLO-395)" in src
        # Each example must be present by recipe id
        for tag in (
            "BOLLINGER SQUEEZE BREAKOUT",
            "TREND-PULLBACK to MA CONFLUENCE",
            "MACD MOMENTUM CONTINUATION",
            "DIVERGENCE-PLAY REVERSAL",
            "PIVOT-LEVEL REJECTION",
            "STATEFUL CROSSOVER ENTRY",
            "FAILED-BREAKDOWN RECLAIM",
            "MTF TREND-ALIGNMENT ENTRY",
        ):
            assert tag in src, f"prompt missing example tag: {tag}"

    def test_prompt_yaml_content_matches_test_parametrize(self):
        """Tighter anchor (per advisor): each example's distinguishing
        YAML token must appear in the prompt source. Catches the silent
        drift class where a prompt edit changes example shape but tags
        stay intact and the parametrized round-trip test still passes
        (because it has its own hardcoded list).

        Brittle to formatting — that's the point. If the prompt YAML
        is reflowed, update both the test and the prompt together."""
        with open("agent_prompts.py", "r", encoding="utf-8") as f:
            src = f.read()
        # One distinguishing token per example, tied to the
        # PROMPT_EXAMPLES list above. Each must be unique enough that
        # accidental matches in unrelated prompt text are unlikely.
        anchors = {
            "bb_squeeze_breakout": '"relation": "above_upper"',
            "trend_pullback_ma_confluence": '"level": 0.618, "tolerance_pips": 8.0',
            "macd_momentum_continuation": '"threshold": 0.05',
            "divergence_play_reversal": '"indicator": "macd", "direction": "bearish"',
            "pivot_level_rejection": '"pivot_set": "classic", "level": "R1"',
            "stateful_crossover_entry": '"indicator": "macd_histogram", "tf": "H1", "direction": "above", "threshold": 0.0',
            "failed_breakdown_reclaim": '"level": 4707.0, "direction": "below"',
            "mtf_trend_alignment_entry": '"tf": "H4", "period": 50, "relation": "aligned_bull"',
        }
        for name, anchor in anchors.items():
            assert anchor in src, (
                f"prompt YAML for {name!r} drifted from test contract; "
                f"expected token not found: {anchor!r}"
            )


# =============================================================================
# C3 — _format_indicators primitive_shape annotations
# =============================================================================


class TestC3IndicatorPrimitiveShape:
    """`_format_indicators` must add `primitive_shape` to each
    indicator block so Floki can copy-paste the primitive YAML
    template instead of mentally translating fact → primitive."""

    def _format(self):
        from agent_data_builder import _format_indicators
        tech = {
            "rsi": {"value": 58.2, "level": "neutral"},
            "macd": {"histogram": 0.05, "signal": "neutral", "trend": "neutral"},
            "ema": {"ema9": 4500.0, "ema21": 4495.0, "ema50": 4490.0,
                    "ema200": 4480.0, "above_ema20": True,
                    "above_ema50": True, "above_ema200": True},
            "bollinger": {"upper": 4520.0, "middle": 4510.0,
                          "lower": 4500.0, "position": 0.6, "squeeze": False},
        }
        momentum = {
            "atr": {"atr_value": 12.5, "atr_trend": "rising"},
            "adx": {"adx_value": 28.0, "plus_di": 25.0,
                    "minus_di": 15.0, "adx_classification": "strong"},
            "volume": {"volume_ratio": 1.2, "volume_classification": "normal"},
        }
        return _format_indicators(tech, momentum)

    def test_rsi_carries_primitive_shape(self):
        out = self._format()
        assert "primitive_shape" in out["rsi"]
        assert '"type": "rsi"' in out["rsi"]["primitive_shape"]
        # Existing fields preserved
        assert out["rsi"]["value"] == 58.2

    def test_macd_carries_primitive_shape(self):
        out = self._format()
        assert "primitive_shape" in out["macd"]
        assert '"type": "macd_histogram"' in out["macd"]["primitive_shape"]

    def test_ema_carries_primitive_shape(self):
        out = self._format()
        assert "primitive_shape" in out["emas"]
        assert '"type": "ema_relation"' in out["emas"]["primitive_shape"]
        # Existing fields preserved
        assert out["emas"]["ema21"] == 4495.0

    def test_bollinger_carries_primitive_shape(self):
        out = self._format()
        assert "primitive_shape" in out["bollinger"]
        assert '"type": "bollinger_position"' in out["bollinger"]["primitive_shape"]

    def test_atr_carries_primitive_shape(self):
        out = self._format()
        assert "primitive_shape" in out["atr"]
        assert '"type": "atr"' in out["atr"]["primitive_shape"]

    def test_indicators_without_snow_primitive_have_no_shape(self):
        """ADX and volume have NO Snow primitive — they should NOT
        carry a primitive_shape. Otherwise Floki may try to encode them
        and fail validation."""
        out = self._format()
        assert "primitive_shape" not in out["adx"]
        assert "primitive_shape" not in out["volume"]


# =============================================================================
# E2 — Vocabulary diversity helper + emit extension
# =============================================================================


class TestE2VocabularyDiversity:
    def test_helper_zero_for_no_plan(self):
        assert _entry_vocabulary_diversity(None) == (0, 0, [])

    def test_helper_zero_for_empty_conditions(self):
        plan = Plan(**_wrap_plan([
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            {"type": "price_above", "level": 4500.0},
        ]))
        n_types, n_fams, fams = _entry_vocabulary_diversity(plan)
        assert n_types == 2
        assert n_fams == 2  # rsi=oscillator, price_above=structural
        assert "oscillator" in fams
        assert "structural" in fams

    def test_helper_three_distinct_families(self):
        plan = Plan(**_wrap_plan([
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
            {"type": "price_at_sr_zone", "zone_type": "any", "tolerance_pips": 5.0},
        ]))
        n_types, n_fams, fams = _entry_vocabulary_diversity(plan)
        assert n_types == 3
        assert n_fams == 3
        assert set(fams) == {"oscillator", "trend", "structural"}

    def test_helper_collapses_same_family(self):
        """Multiple conditions in the same family count as ONE family."""
        plan = Plan(**_wrap_plan([
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            {"type": "macd_histogram", "tf": "H1", "op": "above", "threshold": 0.0},
        ]))
        n_types, n_fams, fams = _entry_vocabulary_diversity(plan)
        assert n_types == 2  # 2 distinct primitives
        assert n_fams == 1   # both oscillator family
        assert fams == ["oscillator"]

    def test_helper_handles_dict_form(self):
        """Helper must accept either parsed Plan model OR plan dict."""
        d = _wrap_plan([
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
        ])
        n_types, n_fams, fams = _entry_vocabulary_diversity(d)
        assert n_types == 2
        assert n_fams == 2

    def test_taxonomy_covers_all_entry_primitives(self):
        """Every condition primitive that can plausibly appear in
        entry.conditions should be in the taxonomy. Position-state
        primitives are excluded by design (they don't appear in entry).
        """
        # Sanity: known-entry primitives must all be classified
        for prim in [
            "rsi", "macd_histogram", "stochastic",
            "ema_relation",
            "price_above", "price_below", "price_at_sr_zone",
            "price_at_fibonacci", "price_at_pivot", "price_crossed_level",
            "atr", "bollinger_position",
            "indicator_divergence", "indicator_crossover", "indicator_was",
        ]:
            assert prim in _PRIMITIVE_FAMILY, (
                f"primitive {prim!r} not classified in _PRIMITIVE_FAMILY"
            )

    def test_emit_includes_diversity_fields(self, caplog):
        """emit_recipe_pulled log line must include the three FLO-395
        E2 fields: entry_distinct_primitive_types, entry_distinct_families,
        entry_families."""
        plan = Plan(**_wrap_plan([
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            {"type": "ema_relation", "tf": "H1", "period": 21, "relation": "aligned_bull"},
            {"type": "price_at_sr_zone", "zone_type": "any", "tolerance_pips": 5.0},
        ]))
        with caplog.at_level(logging.INFO, logger="snow.instrumentation"):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-001",
                recipe_pulls=[{"ts": "2026-04-28T08:00:00Z", "category": "trend"}],
                final_setup_type="pullback_trend",
                plan=plan,
            )
        # Look for our diversity line specifically
        diag_lines = [r.message for r in caplog.records
                      if "snow.plan.recipe_pulled" in r.message]
        assert diag_lines, "no snow.plan.recipe_pulled emission captured"
        msg = diag_lines[-1]
        assert "entry_distinct_primitive_types=3" in msg
        assert "entry_distinct_families=3" in msg
        assert "oscillator" in msg
        assert "trend" in msg
        assert "structural" in msg

    def test_emit_zero_diversity_when_plan_none(self, caplog):
        """Backwards compat: emit must still work when plan kwarg is
        omitted, and diversity fields must emit as 0 / []."""
        with caplog.at_level(logging.INFO, logger="snow.instrumentation"):
            emit_recipe_pulled(
                plan_id="PLAN-20260428-002",
                recipe_pulls=[],
                final_setup_type=None,
            )
        diag_lines = [r.message for r in caplog.records
                      if "snow.plan.recipe_pulled" in r.message]
        assert diag_lines
        msg = diag_lines[-1]
        assert "entry_distinct_primitive_types=0" in msg
        assert "entry_distinct_families=0" in msg
        assert "entry_families=[]" in msg
