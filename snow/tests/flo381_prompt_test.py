"""FLO-381 — v3.10 prompt management-primitive selection guidance.

Pinning tests for the v3.10 prompt update. Triggered by N=4 entered
Snow plans where 3 scratched at BE-locked SL on post-management
reversal and 1 ran to TP. The data implicates BE-only management as
converting any reversal into a scratch — so the lever is primitive
selection (trail_sl / close_partial / profit_retraced_from_peak),
not BE threshold tuning.

These tests guard that future prompt edits don't:
  * Silently drop the MANAGEMENT PRIMITIVE SELECTION section
  * Drift the named primitive vocabulary
  * Drop the setup_type → management cross-reference
  * Break baseline plan validation (additive-only contract)
"""
from __future__ import annotations

from copy import deepcopy

from agent_prompts import SYSTEM_PROMPT, get_prompt_version


class TestV310PromptVersion:
    def test_version_returns_3_10(self):
        assert get_prompt_version() == "3.10"


class TestV310ManagementSection:
    def test_section_header_present(self):
        assert "MANAGEMENT PRIMITIVE SELECTION" in SYSTEM_PROMPT

    def test_named_action_primitives(self):
        """All four management actions named with selection guidance —
        not just listed in the Action types one-liner."""
        for primitive in (
            "move_sl_to_breakeven",
            "trail_sl",
            "close_partial",
            "move_sl_to_price",
        ):
            # Each must appear at least twice: once in the Action types
            # listing and once in the SELECTION section's per-primitive
            # framing.
            assert SYSTEM_PROMPT.count(primitive) >= 2, (
                f"v3.10: {primitive!r} should appear in both the Action "
                f"types listing and the SELECTION section"
            )

    def test_position_state_primitives_for_give_back_patterns(self):
        """profit_retraced_from_peak and mfe_reached unlock management
        shapes that BE-only can't express. The SELECTION section names
        them explicitly."""
        for primitive in (
            "profit_retraced_from_peak",
            "mfe_reached",
            "mae_reached",
            "profit_pips",
        ):
            assert primitive in SYSTEM_PROMPT, (
                f"v3.10: position-state primitive {primitive!r} missing"
            )

    def test_setup_type_cross_reference(self):
        """The load-bearing claim of v3.10 is that management shape
        should align with the setup_type tag. Every setup_type Floki
        can submit must appear in the cross-reference."""
        # 10 setup_type values from FLO-366 closed enum
        setup_types = [
            "breakout_range",
            "pullback_trend",
            "mean_reversion_extreme",
            "liquidity_sweep",
            "continuation_momentum",
            "news_reaction",
            "divergence_play",
            "paired_hedge",
            "structural_bounce",
            "session_open_break",
        ]
        for tag in setup_types:
            assert tag in SYSTEM_PROMPT, (
                f"v3.10: setup_type {tag!r} not cross-referenced"
            )

    def test_worked_flow_step4_inclusive_framing(self):
        """v3.9 step 4 said "(BE lock, optional trail)" — bias toward
        BE as default. v3.10 reframes to "(one or more contingencies
        — see MANAGEMENT PRIMITIVE SELECTION)"."""
        assert "one or more contingencies" in SYSTEM_PROMPT
        # Pre-v3.10 framing must be gone (this is the only non-additive
        # tweak; reframing the WORKED FLOW step rather than removing
        # any logic).
        assert "BE lock, optional trail" not in SYSTEM_PROMPT

    def test_no_prescriptive_must_use_language(self):
        """feedback_no_prescriptive_rules: never tell Floki what he
        MUST do at the management level. Frame as scenarios + natural
        fits, not directives. Guard against future edits that escalate
        to MUST USE / SHOULD USE on a specific primitive."""
        # The phrase "must use trail_sl" or "should use trail_sl" would
        # violate the contract. Same for the other primitives.
        forbidden = [
            "must use trail_sl",
            "must use close_partial",
            "should use trail_sl",
            "should use close_partial",
            "MUST USE trail",
            "SHOULD USE trail",
        ]
        lower = SYSTEM_PROMPT.lower()
        for phrase in forbidden:
            assert phrase.lower() not in lower, (
                f"v3.10 prescriptive language regression: {phrase!r}"
            )


class TestV310AdditiveContract:
    """v3.10 is additive only: no v3.9 logic removed. These tests guard
    that the agent capabilities Floki had at v3.9 still surface."""

    def test_v39_setup_tagging_preserved(self):
        assert "SETUP TAGGING" in SYSTEM_PROMPT
        assert "context_tags" in SYSTEM_PROMPT
        assert "confidence_reason" in SYSTEM_PROMPT

    def test_v38_managed_by_visibility_preserved(self):
        assert "managed_by" in SYSTEM_PROMPT
        assert "snow:" in SYSTEM_PROMPT

    def test_v37_stateful_primitives_preserved(self):
        for prim in ("indicator_crossover", "indicator_was", "price_crossed_level"):
            assert prim in SYSTEM_PROMPT

    def test_v34_paired_plans_preserved(self):
        assert "PAIRED PLANS" in SYSTEM_PROMPT

    def test_v32_cycle_start_check_preserved(self):
        assert "CYCLE-START CHECK" in SYSTEM_PROMPT
        assert "list_active_plans" in SYSTEM_PROMPT


class TestV310NoValidationRetryStorms:
    """v3.10 is prompt-only; schema unchanged. Existing valid plan
    fixtures must continue to pass without validation retries."""

    def test_baseline_v3_plan_still_validates(self):
        from snow.validator import validate_plan
        from snow.tests.conftest import _BASE_PLAN
        plan = deepcopy(_BASE_PLAN)
        ok, parsed, errors = validate_plan(plan)
        assert ok, f"v3.10 prompt update broke baseline plan: {errors}"
        assert not errors
