"""FLO-422 Phase A1 — regression tests for tool-removal.

Verifies that:
  * `get_snow_tags_reference` and `get_snow_primitives_reference` are gone
    from AgentTools.
  * Both names are gone from the ai_agent registered tool sets and tool
    schema list.
  * The validator's missing-tagging error inlines the closed setup_type
    and context_tags vocabulary (no longer references the removed tool).
  * Other static reference content (set lists, schema enums) is preserved.

Run: python test_flo422_a1_tool_removal.py
Exits non-zero on failure.
"""
from __future__ import annotations

import sys
from copy import deepcopy


def fail(label: str, msg: str) -> None:
    print(f"FAIL [{label}]: {msg}")
    sys.exit(1)


def passed(label: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"PASS [{label}]{suffix}")


def test_1_agent_tools_methods_removed():
    from agent_tools import AgentTools
    if hasattr(AgentTools, "get_snow_tags_reference"):
        fail("test1.tags_reference_removed", "method still present on AgentTools")
    if hasattr(AgentTools, "get_snow_primitives_reference"):
        fail("test1.primitives_reference_removed", "method still present on AgentTools")
    passed("test1.both_methods_removed_from_AgentTools")


def test_2_ai_agent_registered_sets_clean():
    import ai_agent
    parallel = ai_agent._PARALLEL_SAFE_TOOLS
    singleton = ai_agent._SINGLETON_TOOLS
    if "get_snow_tags_reference" in parallel:
        fail("test2.parallel_set", "tags_reference still in _PARALLEL_SAFE_TOOLS")
    if "get_snow_tags_reference" in singleton:
        fail("test2.singleton_set", "tags_reference still in _SINGLETON_TOOLS")
    if "get_snow_primitives_reference" in parallel:
        fail("test2.parallel_set_primitives", "primitives_reference still in _PARALLEL_SAFE_TOOLS")
    if "get_snow_primitives_reference" in singleton:
        fail("test2.singleton_set_primitives", "primitives_reference still in _SINGLETON_TOOLS")
    # recipe_book is intentionally retained
    if "get_snow_recipe_book" not in parallel and "get_snow_recipe_book" not in singleton:
        fail("test2.recipe_book_preserved", "recipe_book unexpectedly removed (Phase A1 should leave it)")
    passed("test2.tool_classification_sets_clean")


def test_3_ai_agent_tool_schema_clean():
    """Inspect the tool-schema dispatcher to confirm the two removed tools
    are absent and recipe_book is still present. We render the schema list
    via a lightweight construction, then scan the names."""
    import ai_agent
    # _tool_schemas() returns the OpenAI-shape list. Look it up.
    # The agent class is AIAgent; the list is built by _openai_tools().
    # We just need to read the source to find tool name strings.
    src = open("ai_agent.py", encoding="utf-8").read()
    # Look for "name": "get_snow_tags_reference" / "get_snow_primitives_reference"
    if '"name": "get_snow_tags_reference"' in src:
        fail("test3.tags_schema_present", "tag-reference schema still in ai_agent.py")
    if '"name": "get_snow_primitives_reference"' in src:
        fail("test3.primitives_schema_present", "primitives-reference schema still in ai_agent.py")
    if '"name": "get_snow_recipe_book"' not in src:
        fail("test3.recipe_book_schema_missing", "recipe_book schema unexpectedly removed")
    passed("test3.tool_schemas_only_recipe_book_remains")


def test_4_validator_error_inlines_vocabulary():
    """The missing-tagging validation error MUST NOT reference the removed
    tool name and MUST inline the closed vocabulary."""
    from snow.schema import Plan
    # Use a fixture-style valid plan and strip the tagging fields
    valid = {
        "schema_version": 3,
        "id": "PLAN-20260507-001",
        "created_by": "floki",
        "created_at": "2026-05-07T00:00:00Z",
        "expires_at": None,
        "status": "pending",
        "analysis": {
            "thesis": "test thesis at least 20 chars long for validation",
            "key_levels": [4600.0, 4620.0, 4640.0],
            "confidence": 75,
            "regime_assumed": "TRENDING_BULLISH",
            # No setup_type / context_tags / confidence_reason → triggers the v3 validator
        },
        "entry": {
            "direction": "BUY", "volume": 0.01,
            "conditions": [{"type": "price_above", "level": 4600.0}],
            "initial_sl": 4585.0, "initial_tp": 4630.0, "entry_price": 4601.0,
        },
        "management": [],
        "exit": [{"name": "inv", "priority": 1,
                  "conditions": [{"type": "price_below", "level": 4585.0}],
                  "action": {"type": "close_full"}, "fires": "once"}],
        "emergency": {"max_loss_pips": 150.0, "max_duration_minutes": 240},
    }
    try:
        Plan(**valid)
        fail("test4.expected_error", "plan without tagging passed validation")
    except Exception as e:
        msg = str(e)
        # Old behavior: references the removed tool. Must NOT appear.
        if "get_snow_tags_reference" in msg:
            fail("test4.tool_reference_persists", "validator still mentions get_snow_tags_reference")
        # New behavior: closed vocabulary inlined.
        for token in ("setup_type", "breakout_range", "pullback_trend",
                      "trend_strong", "HTF_aligned", "session_overlap",
                      "confidence_reason"):
            if token not in msg:
                fail(f"test4.vocab_{token}_inlined", f"missing {token!r} in error message")
    passed("test4.validator_error_inlines_full_vocabulary")


def test_5_orphan_modules_deleted():
    """The two reference modules (`snow.tags_reference`, `snow.reference`)
    should fail to import — they are deleted in this commit."""
    import importlib
    for mod_name in ("snow.tags_reference", "snow.reference"):
        try:
            importlib.import_module(mod_name)
            fail(f"test5.{mod_name}_still_present", f"{mod_name} should be deleted")
        except ImportError:
            pass
    passed("test5.orphan_modules_deleted")


def test_6_prompt_no_longer_calls_removed_tools():
    """The system prompt (agent_prompts.get_system_prompt) must not contain
    instructions to call the removed tools."""
    from agent_prompts import get_system_prompt
    prompt = get_system_prompt()
    forbidden = [
        "Call get_snow_tags_reference",
        "Call get_snow_primitives_reference",
        "call get_snow_primitives_reference",
        "call get_snow_tags_reference",
    ]
    for phrase in forbidden:
        if phrase in prompt:
            fail("test6.prompt_calls_removed_tool",
                 f"prompt still says {phrase!r}")
    passed("test6.prompt_does_not_call_removed_tools")


def test_7_setup_type_vocabulary_in_prompt():
    """All 10 setup_type values must remain in the prompt — this is now the
    canonical vocabulary location."""
    from agent_prompts import get_system_prompt
    prompt = get_system_prompt()
    required = [
        "breakout_range", "pullback_trend", "mean_reversion_extreme",
        "liquidity_sweep", "continuation_momentum", "news_reaction",
        "divergence_play", "paired_hedge", "structural_bounce",
        "session_open_break",
    ]
    missing = [v for v in required if v not in prompt]
    if missing:
        fail("test7.prompt_setup_vocab", f"missing in prompt: {missing}")
    passed("test7.all_10_setup_types_in_prompt")


def test_8_context_tags_vocabulary_in_prompt():
    from agent_prompts import get_system_prompt
    prompt = get_system_prompt()
    required = [
        "trend_strong", "trend_weak", "range_tight", "range_wide",
        "high_vol", "low_vol",
        "HTF_aligned", "HTF_counter", "HTF_neutral",
        "near_news", "post_news", "session_overlap", "session_thin",
    ]
    missing = [v for v in required if v not in prompt]
    if missing:
        fail("test8.prompt_context_tags_vocab", f"missing in prompt: {missing}")
    passed("test8.all_context_tag_values_in_prompt")


def test_9_primitive_vocabulary_in_prompt():
    from agent_prompts import get_system_prompt
    prompt = get_system_prompt()
    required_primitives = [
        "price_above", "price_below",
        "rsi", "macd_histogram", "ema_relation", "atr", "stochastic",
        "bollinger_position", "indicator_divergence",
        "price_at_sr_zone", "price_at_fibonacci", "price_at_pivot",
        "profit_pips", "mfe_reached", "mae_reached", "profit_retraced_from_peak",
        "duration_exceeds",
        "indicator_crossover", "indicator_was", "price_crossed_level",
    ]
    missing = [p for p in required_primitives if p not in prompt]
    if missing:
        fail("test9.prompt_primitive_vocab", f"missing in prompt: {missing}")
    passed("test9.all_primitive_types_in_prompt")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-422 Phase A1 — tool-removal regression tests")
    print("=" * 60)
    test_1_agent_tools_methods_removed()
    test_2_ai_agent_registered_sets_clean()
    test_3_ai_agent_tool_schema_clean()
    test_4_validator_error_inlines_vocabulary()
    test_5_orphan_modules_deleted()
    test_6_prompt_no_longer_calls_removed_tools()
    test_7_setup_type_vocabulary_in_prompt()
    test_8_context_tags_vocabulary_in_prompt()
    test_9_primitive_vocabulary_in_prompt()
    print("=" * 60)
    print("ALL A1 REGRESSION TESTS PASSED")
