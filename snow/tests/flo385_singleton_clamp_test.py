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
        "write_trading_journal",
        "save_lesson",
        "forget_lesson",
        "set_next_check",
        # FLO-419 (CEO 2026-05-04): set_wake_conditions and
        # set_watch_conditions removed from Floki's roster (Simba
        # deprecated). They no longer appear in _SINGLETON_TOOLS.
        "execute_trade",
        "close_trade",
        "adjust_trade",
        "place_pending_order",
        "cancel_pending_order",
    )

    # FLO-385 follow-up (CEO directive 2026-04-29) — INVERTED from the
    # original v1 contract. KNOWN_PROTOCOL_FRAGILE used to list
    # `get_chart_screenshots` as a singleton because it appends a
    # post-response user-message (chart images). The FLO-262
    # `_deferred_user_msgs` path (in ai_agent.py:1938-1981) already
    # protects the protocol invariant by appending the user-message
    # AFTER the full tool-response sequence — making the singleton
    # classification belt-and-suspenders. Under the new mandatory-suite
    # rule + GPT-5.4's 16-18-tool parallel batches, the redundancy
    # forced ~95s/cycle and 15+ iterations. Reclassified parallel-safe;
    # the deferral path is the single load-bearing protection.
    # If a future edit re-adds chart_screenshots here, that edit is
    # reverting CEO directive 2026-04-29 and must surface explicitly.
    KNOWN_PROTOCOL_FRAGILE: tuple = ()

    # FLO-434 (2026-05-17): debate_with_rex removed from the cycle.
    # No expensive-subagent tools currently — empty tuple parameterises
    # to zero test instances.
    KNOWN_EXPENSIVE_SUBAGENT: tuple = ()

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
        # FLO-422 Phase A1: get_snow_primitives_reference removed.
        "read_session_memory",
        "get_trade_lessons",
        # FLO-385 follow-up (CEO directive 2026-04-29) additions —
        # explicitly classify the read-only tools that previously fell
        # through the fail-safe-singleton path. Each must stay
        # parallel-safe so the mandatory-suite batch keeps its
        # one-round-trip dispatch.
        "get_chart_screenshots",  # protocol invariant covered by
                                  # _deferred_user_msgs (FLO-262)
        "get_echo_alerts",        # pure read of echo_aggregate.json
        "get_trade_history",      # SQLite read, idempotent
        "search_reflexions",      # vector-DB read, idempotent
        "search_memory",          # vector-DB read, idempotent
        "get_trade_journal",      # JSON read, idempotent
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

    def test_chart_screenshots_in_parallel_batch_passes_through(self):
        """FLO-385 follow-up (CEO directive 2026-04-29) — INVERTED from
        the original v1 contract. get_chart_screenshots WAS singleton
        because of its post-response user-message inject (chart images),
        and the original test asserted the clamp would fire when it
        appeared in a parallel batch. The FLO-262 `_deferred_user_msgs`
        path already protects the protocol invariant (chart-image
        user-message appended AFTER the full tool-response sequence),
        making the singleton classification redundant. Reclassifying
        chart_screenshots parallel-safe is the dominant fix for the
        FLO-404 mandatory-suite latency (~95s → ~25s expected).

        New contract: a batch of all-parallel-safe tools INCLUDING
        chart_screenshots passes through with zero clamping."""
        batch = [
            _tc("get_indicators", "tc1"),
            _tc("get_chart_screenshots", "tc2"),
            _tc("get_sr_zones", "tc3"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 3, (
            "FLO-385 v2: chart_screenshots is parallel-safe; batch must "
            "pass through without clamping. If this fails, someone "
            "re-added chart_screenshots to _SINGLETON_TOOLS — that "
            "reverts CEO directive 2026-04-29."
        )
        assert dropped == []
        assert [tc.function.name for tc in kept] == [
            "get_indicators", "get_chart_screenshots", "get_sr_zones",
        ]

    def test_full_mandatory_suite_batch_passes_through(self):
        """FLO-385 follow-up — locks the canonical case the fix targets.
        Under the new mandatory-suite rule (FLO-404), Floki batches
        ~10-18 read-only tools per turn. Before the v2 reclassification
        these batches triggered the clamp because chart_screenshots OR
        get_echo_alerts (uncategorised → fail-safe singleton) was
        present, forcing 15+ sequential iterations and ~95s latency.

        New contract: the full mandatory suite — including
        chart_screenshots and get_echo_alerts — dispatches in a single
        round-trip with zero drops."""
        batch = [
            _tc(name, f"tc{i}")
            for i, name in enumerate([
                "list_active_plans",
                "get_open_positions",
                "get_chart_screenshots",
                "get_market_regime",
                "get_sr_zones",
                "get_indicators",
                "get_fibonacci_levels",
                "get_chart_patterns",
                "get_tick_pressure",
                "get_market_context",
                "get_luna_brief",
                "get_echo_alerts",
                "get_rex_monitor",
                "get_snow_recipe_book",
                # FLO-422 Phase A1: get_snow_tags_reference removed.
            ])
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 14, (
            "FLO-385 v2: the full mandatory suite must dispatch in one "
            "round-trip. Latency target ~25s/cycle vs ~95s under v1."
        )
        assert dropped == []

    def test_singleton_in_mandatory_suite_still_clamps(self):
        """Sanity check the clamp still fires when an actual write
        sneaks into the mandatory-suite batch (e.g. Floki tries to
        submit a plan in the same turn as the suite reads). This
        protects the original FLO-385 race-condition guarantee."""
        batch = [
            _tc("list_active_plans", "tc1"),
            _tc("get_open_positions", "tc2"),
            _tc("submit_plan_to_snow", "tc3"),  # write — clamps batch
            _tc("get_indicators", "tc4"),
        ]
        kept, dropped = _apply_singleton_clamp(batch)
        assert len(kept) == 1
        assert kept[0].function.name == "list_active_plans"
        assert len(dropped) == 3
