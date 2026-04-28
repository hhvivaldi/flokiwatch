"""FLO-385 — Group-by-dependency tool-call clamp tests.

Pure-function unit tests for `_classify_tool` and `_apply_singleton_clamp`,
plus CI guards on the `_SINGLETON_TOOLS` / `_PARALLEL_SAFE_TOOLS` set
membership so future readers can't silently re-categorise critical tools.

Mitigation 1 — explicit singleton list documented:
  * test_singleton_set_contains_known_state_mutators (CI guard)
  * test_parallel_safe_set_contains_known_read_only_tools (CI guard)
  * test_no_overlap_between_classifications

Mitigation 2 — fail-safe default for uncategorised tools:
  * test_uncategorised_tool_routes_to_singleton
  * test_uncategorised_tool_emits_warning

Mitigation 3 — visible failure on mis-categorisation + behaviour:
  * test_misclassifying_state_mutator_breaks_ci_guard (regression for
    Mitigation 1 — naming a singleton tool here makes the failure mode
    explicit if the set ever drifts)
  * test_uncategorised_tool_routes_to_singleton (Mit-2 / Mit-3b overlap)
  * test_read_only_batch_preserves_full_parallelism (Mit-3c)
  * test_mixed_batch_caps_to_first_call (Mit-3d)
"""
from __future__ import annotations

import logging as stdlib_logging
from types import SimpleNamespace

import pytest

from ai_agent import (
    _SINGLETON_TOOLS,
    _PARALLEL_SAFE_TOOLS,
    _classify_tool,
    _apply_singleton_clamp,
)


def _tc(name: str, tc_id: str = "tc1", args: str = "{}"):
    """Build a minimal tool_call mock matching OpenAI SDK shape:
    .id + .function.name + .function.arguments"""
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=args),
    )


# =============================================================================
# Mitigation 1 — explicit singleton list documented
# =============================================================================

class TestSingletonSetMembership:
    """CI guards. If a future change removes one of these from the
    singleton set, the offending commit fails CI with an explicit name
    rather than producing silent protocol-fragility regressions."""

    KNOWN_STATE_MUTATORS = (
        "submit_plan_to_snow",
        "cancel_plan",
        "write_session_memory",
        "set_next_check",
        "set_wake_conditions",
        "set_watch_conditions",
        "execute_trade",
        "close_trade",
        "adjust_trade",
        "cancel_pending_order",
    )

    KNOWN_PROTOCOL_FRAGILE = (
        "get_chart_screenshots",
    )

    KNOWN_EXPENSIVE_SUBAGENT = (
        "debate_with_rex",
    )

    @pytest.mark.parametrize("tool", KNOWN_STATE_MUTATORS)
    def test_state_mutator_classified_singleton(self, tool):
        assert tool in _SINGLETON_TOOLS, (
            f"FLO-385 regression: {tool!r} is a state-mutating tool "
            f"and MUST be in _SINGLETON_TOOLS. Removing it lets the "
            f"LLM dispatch it concurrently with reads, racing the "
            f"side effect against other tool results."
        )
        assert _classify_tool(tool) == "singleton"

    @pytest.mark.parametrize("tool", KNOWN_PROTOCOL_FRAGILE)
    def test_protocol_fragile_classified_singleton(self, tool):
        assert tool in _SINGLETON_TOOLS, (
            f"FLO-385 regression: {tool!r} appends a user-message "
            f"after its tool response (chart-image inject), breaking "
            f"the contiguous tool-response sequence stricter providers "
            f"require. It MUST be in _SINGLETON_TOOLS."
        )

    @pytest.mark.parametrize("tool", KNOWN_EXPENSIVE_SUBAGENT)
    def test_expensive_subagent_classified_singleton(self, tool):
        assert tool in _SINGLETON_TOOLS, (
            f"FLO-385 regression: {tool!r} invokes an expensive "
            f"sub-agent loop and shouldn't race with concurrent reads."
        )


class TestParallelSafeSetMembership:
    """CI guard for the read-only side of the classification. Read-only
    tools should stay in the parallel-safe set so common Floki polling
    batches (indicators + S/R + fibs in one shot) keep their performance."""

    KNOWN_READ_ONLY = (
        "get_indicators",
        "get_sr_zones",
        "get_market_regime",
        "get_fibonacci_levels",
        "get_pivot_points",
        "get_chart_patterns",
        "get_tick_pressure",
        "get_current_price",
        "get_open_positions",
        "list_active_plans",
        "get_plan_status",
        "rex_divergence_scan",
        "rex_correlation_check",
        "rex_regime_history",
        "rex_session_performance",
        "get_snow_recipe_book",
        "get_snow_primitives_reference",
        "read_session_memory",
        "get_trade_lessons",
    )

    @pytest.mark.parametrize("tool", KNOWN_READ_ONLY)
    def test_read_only_classified_parallel(self, tool):
        assert tool in _PARALLEL_SAFE_TOOLS, (
            f"FLO-385: {tool!r} is a read-only state-polling tool "
            f"and should stay in _PARALLEL_SAFE_TOOLS so Floki's "
            f"polling batches keep parallel performance."
        )
        assert _classify_tool(tool) == "parallel"


class TestNoOverlap:
    def test_singleton_and_parallel_sets_are_disjoint(self):
        overlap = _SINGLETON_TOOLS & _PARALLEL_SAFE_TOOLS
        assert not overlap, (
            f"FLO-385 categorisation conflict: tools listed in both "
            f"sets: {sorted(overlap)}. A tool is either singleton or "
            f"parallel-safe; pick one."
        )


# =============================================================================
# Mitigation 2 — fail-safe default for uncategorised tools
# =============================================================================

class TestUncategorisedDefault:
    def test_uncategorised_tool_routes_to_singleton(self):
        # Use a deliberately fake name to ensure no future addition
        # accidentally categorises it.
        assert _classify_tool("definitely_not_a_real_tool_xyz") == "singleton"

    def test_uncategorised_tool_emits_warning(self, caplog):
        with caplog.at_level(stdlib_logging.WARNING):
            _classify_tool("another_fake_tool_for_warn_test")
        warns = [r.getMessage() for r in caplog.records
                 if "FLO-385" in r.getMessage()]
        assert any(
            "another_fake_tool_for_warn_test" in m and
            "fail-safe" in m.lower()
            for m in warns
        ), (
            f"expected FLO-385 warn about uncategorised tool; got: {warns}"
        )

    def test_uncategorised_tool_in_batch_caps_to_singleton(self):
        """Mit-3 (b)+(d) overlap: uncategorised tool in a parallel batch
        triggers the singleton clamp via fail-safe default."""
        kept, dropped = _apply_singleton_clamp([
            _tc("get_indicators", "tc1"),
            _tc("definitely_not_a_real_tool_zzz", "tc2"),
            _tc("get_sr_zones", "tc3"),
        ])
        assert len(kept) == 1
        assert len(dropped) == 2
        assert kept[0].function.name == "get_indicators"


# =============================================================================
# Mitigation 3 — clamp behaviour on real batch shapes
# =============================================================================

class TestClampBehaviour:
    def test_empty_batch_returns_empty(self):
        kept, dropped = _apply_singleton_clamp([])
        assert kept == []
        assert dropped == []

    def test_single_call_passes_through_regardless_of_class(self):
        # Single singleton call → kept, no drop
        kept, dropped = _apply_singleton_clamp([_tc("submit_plan_to_snow")])
        assert len(kept) == 1
        assert dropped == []
        # Single parallel-safe call → kept, no drop
        kept, dropped = _apply_singleton_clamp([_tc("get_indicators")])
        assert len(kept) == 1
        assert dropped == []

    def test_read_only_batch_preserves_full_parallelism(self):
        """Mit-3 (c). Floki's typical poll batch (5 read-only tools)
        must dispatch in one round-trip — no clamping, no drops."""
        batch = [
            _tc("get_indicators", "tc1"),
            _tc("get_sr_zones", "tc2"),
            _tc("get_market_regime", "tc3"),
            _tc("get_fibonacci_levels", "tc4"),
            _tc("get_tick_pressure", "tc5"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 5
        assert len(dropped) == 0
        assert [tc.function.name for tc in kept] == [
            "get_indicators", "get_sr_zones", "get_market_regime",
            "get_fibonacci_levels", "get_tick_pressure",
        ]

    def test_mixed_batch_caps_to_first_call(self):
        """Mit-3 (d). 3 read-only + 1 singleton → keep first only,
        drop the rest. Floki re-emits dropped calls next turn."""
        batch = [
            _tc("get_indicators", "tc1"),
            _tc("get_sr_zones", "tc2"),
            _tc("get_market_regime", "tc3"),
            _tc("submit_plan_to_snow", "tc4"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 1
        assert len(dropped) == 3
        assert kept[0].function.name == "get_indicators"
        assert [tc.function.name for tc in dropped] == [
            "get_sr_zones", "get_market_regime", "submit_plan_to_snow",
        ]

    def test_singleton_first_in_mixed_batch_keeps_singleton(self):
        """If the singleton happens to be the first call, it is kept
        and the parallel-safe tail is dropped (Floki re-emits)."""
        batch = [
            _tc("submit_plan_to_snow", "tc1"),
            _tc("get_indicators", "tc2"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 1
        assert kept[0].function.name == "submit_plan_to_snow"
        assert len(dropped) == 1

    def test_two_singletons_caps_to_first(self):
        """Two state-mutating tools in one batch — only first dispatched.
        Prevents racing two writes against each other."""
        batch = [
            _tc("submit_plan_to_snow", "tc1"),
            _tc("set_next_check", "tc2"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 1
        assert kept[0].function.name == "submit_plan_to_snow"
        assert dropped[0].function.name == "set_next_check"

    def test_chart_screenshots_in_parallel_batch_caps(self):
        """get_chart_screenshots is singleton-class because of its
        post-response user-message inject (chart images). When it
        appears in a parallel batch, the clamp keeps the first call
        only — preserving the protocol invariant on stricter providers
        as defence-in-depth alongside the chart-inject deferral."""
        batch = [
            _tc("get_indicators", "tc1"),
            _tc("get_chart_screenshots", "tc2"),
            _tc("get_sr_zones", "tc3"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 1
        assert kept[0].function.name == "get_indicators"
        assert any(tc.function.name == "get_chart_screenshots"
                   for tc in dropped)
