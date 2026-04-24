"""Validator tests — RFC §12.2 "Unit tests (validator) ~10 tests" (rules half).

Exercises snow.validator.validate_plan business rules (cross-field and
cross-contingency checks that Pydantic can't express). Coverage map:

  * Happy path end-to-end
  * Schema-version gating
  * Timestamp ordering (expires_at after created_at)
  * SL/TP side-correctness per direction
  * XAUUSD price-envelope sanity (catches 47.30 vs 4730 typo)
  * Contingency-name uniqueness within a plan
  * Action-placement (execute_market forbidden outside entry)
  * time_between zero-width rejection + cross-midnight allowed
  * Malformed input shape (non-dict / deeply-broken)
  * Return-shape contract (ok, plan_or_none, errors)
"""

from __future__ import annotations

import pytest

from snow.validator import validate_plan


# =============================================================================
# Happy path
# =============================================================================

class TestHappyPath:

    def test_canonical_plan_validates(self, valid_plan_dict):
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is True
        assert plan is not None
        assert plan.id == "PLAN-20260424-001"
        assert errors == []

    def test_buy_direction_passes(self, valid_plan_dict):
        valid_plan_dict["entry"]["direction"] = "BUY"
        valid_plan_dict["entry"]["initial_sl"] = 4700.0
        valid_plan_dict["entry"]["initial_tp"] = 4740.0
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors


# =============================================================================
# Schema-version gating
# =============================================================================

class TestSchemaVersion:

    def test_newer_version_rejected(self, valid_plan_dict):
        valid_plan_dict["schema_version"] = 999
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is False
        assert any("schema_version=999" in e for e in errors)
        # Plan object returned so caller can show it to Floki
        assert plan is not None

    def test_same_version_accepted(self, valid_plan_dict):
        valid_plan_dict["schema_version"] = 1
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors


# =============================================================================
# Timestamp ordering
# =============================================================================

class TestTimestampOrdering:

    def test_expires_before_created_rejected(self, valid_plan_dict):
        valid_plan_dict["expires_at"] = "2026-04-20T08:00:00Z"
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("expires_at" in e and "after" in e for e in errors)

    def test_expires_equal_created_rejected(self, valid_plan_dict):
        valid_plan_dict["expires_at"] = valid_plan_dict["created_at"]
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("strictly after" in e for e in errors)

    def test_expires_none_accepted(self, valid_plan_dict):
        valid_plan_dict["expires_at"] = None
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_created_at_unparseable_rejected(self, valid_plan_dict):
        # Z-suffixed but otherwise broken
        valid_plan_dict["created_at"] = "notatimestampZ"
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok


# =============================================================================
# SL/TP side-correctness
# =============================================================================

class TestSLTPSideCorrectness:

    def test_sell_sl_below_tp_rejected(self, valid_plan_dict):
        # SELL must have SL > TP
        valid_plan_dict["entry"]["initial_sl"] = 4700.0
        valid_plan_dict["entry"]["initial_tp"] = 4720.0
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("SELL entry" in e for e in errors)

    def test_sell_sl_equal_tp_rejected(self, valid_plan_dict):
        valid_plan_dict["entry"]["initial_sl"] = 4730.0
        valid_plan_dict["entry"]["initial_tp"] = 4730.0
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok

    def test_buy_sl_above_tp_rejected(self, valid_plan_dict):
        # BUY must have SL < TP
        valid_plan_dict["entry"]["direction"] = "BUY"
        valid_plan_dict["entry"]["initial_sl"] = 4750.0
        valid_plan_dict["entry"]["initial_tp"] = 4730.0
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("BUY entry" in e for e in errors)

    def test_buy_correct_sides_accepted(self, valid_plan_dict):
        valid_plan_dict["entry"]["direction"] = "BUY"
        valid_plan_dict["entry"]["initial_sl"] = 4700.0
        valid_plan_dict["entry"]["initial_tp"] = 4740.0
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors


# =============================================================================
# XAUUSD price-envelope sanity
# =============================================================================

class TestPriceEnvelope:

    def test_sl_decimal_point_typo(self, valid_plan_dict):
        """47.40 vs 4740.0 — easy off-by-decimal Floki could make."""
        valid_plan_dict["entry"]["initial_sl"] = 47.40
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        # Should flag both the order AND the envelope violation
        assert any("outside XAUUSD sanity envelope" in e for e in errors)

    def test_tp_too_high(self, valid_plan_dict):
        valid_plan_dict["entry"]["initial_sl"] = 4740.0
        valid_plan_dict["entry"]["initial_tp"] = 9_999_999.0
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("outside XAUUSD sanity envelope" in e for e in errors)

    def test_key_level_typo_flagged(self, valid_plan_dict):
        valid_plan_dict["analysis"]["key_levels"] = [47.35, 4720.0]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("key_levels" in e for e in errors)

    def test_action_price_typo_flagged(self, valid_plan_dict):
        valid_plan_dict["management"][0]["action"]["price"] = 47.27
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("management[0].action.price" in e for e in errors)


# =============================================================================
# Contingency-name uniqueness
# =============================================================================

class TestContingencyNameUniqueness:

    def test_duplicate_names_across_management_exit_rejected(self, valid_plan_dict):
        # Duplicate name between management and exit
        valid_plan_dict["management"][0]["name"] = "rejection_exit"   # already in exit[]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("duplicated" in e for e in errors)

    def test_duplicate_within_exit_rejected(self, valid_plan_dict):
        valid_plan_dict["exit"][1]["name"] = valid_plan_dict["exit"][0]["name"]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("duplicated" in e for e in errors)

    def test_unique_names_accepted(self, valid_plan_dict):
        # Rename everything uniquely (they already are in fixture)
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors


# =============================================================================
# Action placement: execute_market forbidden outside entry
# =============================================================================

class TestActionPlacement:

    def test_execute_market_in_management_rejected(self, valid_plan_dict):
        valid_plan_dict["management"][0]["action"] = {"type": "execute_market"}
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("execute_market" in e and "management" in e for e in errors)

    def test_execute_market_in_exit_rejected(self, valid_plan_dict):
        valid_plan_dict["exit"][0]["action"] = {"type": "execute_market"}
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("execute_market" in e and "exit" in e for e in errors)

    def test_cancel_plan_in_management_rejected(self, valid_plan_dict):
        """FLO-347 Phase 5b: cancel_plan is reachable only via Floki's
        Phase 6 tool, NOT as a contingency action. Validator rejects it."""
        valid_plan_dict["management"][0]["action"] = {"type": "cancel_plan"}
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("cancel_plan" in e and "management" in e for e in errors)

    def test_cancel_plan_in_exit_rejected(self, valid_plan_dict):
        valid_plan_dict["exit"][0]["action"] = {"type": "cancel_plan"}
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("cancel_plan" in e and "exit" in e for e in errors)


# =============================================================================
# time_between: zero-width + cross-midnight
# =============================================================================

class TestTimeBetween:

    def test_zero_width_rejected(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "time_between", "start_utc": "12:00", "end_utc": "12:00"}
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("zero-width" in e for e in errors)

    def test_cross_midnight_window_accepted(self, valid_plan_dict):
        """end < start is valid — wrap-around at runtime."""
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "time_between", "start_utc": "22:00", "end_utc": "06:00"}
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_normal_window_accepted(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "time_between", "start_utc": "06:00", "end_utc": "20:00"}
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors


# =============================================================================
# Malformed input shape
# =============================================================================

class TestMalformedInput:

    def test_non_dict_returns_error_not_exception(self):
        ok, plan, errors = validate_plan("not a dict")  # type: ignore[arg-type]
        assert not ok
        assert plan is None
        assert errors  # non-empty

    def test_missing_required_field(self, valid_plan_dict):
        del valid_plan_dict["entry"]
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert plan is None
        assert any("entry" in e for e in errors)

    def test_empty_dict(self):
        ok, plan, errors = validate_plan({})
        assert not ok
        assert plan is None
        assert errors


# =============================================================================
# Return contract
# =============================================================================

class TestReturnContract:

    def test_ok_returns_plan_and_empty_errors(self, valid_plan_dict):
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is True
        assert plan is not None
        assert errors == []

    def test_business_rule_failure_returns_parsed_plan(self, valid_plan_dict):
        """Business-rule failures keep the parsed plan in the tuple so the
        caller can show it to Floki while reporting errors. Pydantic-level
        failures return plan=None."""
        valid_plan_dict["entry"]["initial_sl"] = 4700.0  # SELL SL < TP
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert plan is not None       # parsed OK at schema level
        assert len(errors) >= 1

    def test_schema_failure_returns_plan_none(self, valid_plan_dict):
        valid_plan_dict["entry"]["volume"] = "not a number"
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert plan is None           # couldn't even parse


# =============================================================================
# Multiple errors captured in one pass
# =============================================================================

class TestMultipleErrors:

    def test_multiple_business_rule_errors_all_reported(self, valid_plan_dict):
        # Violate 3 rules simultaneously: SL>=TP, duplicate names, bad envelope
        valid_plan_dict["entry"]["initial_sl"] = 4700.0  # SELL inverted
        valid_plan_dict["management"][0]["name"] = "rejection_exit"   # dup
        valid_plan_dict["analysis"]["key_levels"] = [47.35]           # typo
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert len(errors) >= 3


# =============================================================================
# FLO-347 Phase 6.5 — prompt-example drift guard
# =============================================================================

class TestPromptExamplePlan:
    """Forcing function: the canonical plan example shown inside Floki's
    SYSTEM_PROMPT (<plans> section, MINIMAL PLAN EXAMPLE) must validate
    cleanly. If the schema drifts, this test fails BEFORE Floki sees the
    broken example at runtime and loops on validation errors.

    When the prompt example is updated, update this dict in lockstep.
    """

    def _prompt_example_plan(self) -> dict:
        # Match the JSON shown in agent_prompts.py SYSTEM_PROMPT <plans>
        # verbatim (field-for-field). expires_at resolved at runtime to
        # a future timestamp so the validator's "expires > created" rule
        # passes regardless of wall clock at test time. The rest of the
        # dict mirrors the prompt byte-for-byte.
        import datetime as _dt
        exp = (_dt.datetime.now(_dt.timezone.utc)
               + _dt.timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')
        created = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        return {
            "schema_version": 1,
            "id": "PLAN-20260424-001",
            "created_by": "floki",
            "created_at": created,
            "expires_at": exp,
            "status": "pending",
            "analysis": {"thesis": "H1 pullback to 4720 support with trend intact",
                         "key_levels": [4735.0, 4720.0, 4707.0],
                         "confidence": 72,
                         "regime_assumed": "TRENDING_BEARISH"},
            "entry":    {"direction": "SELL", "volume": 0.02,
                         "conditions": [{"type": "price_above", "level": 4730.0},
                                        {"type": "rsi", "tf": "H1", "op": "above",
                                         "threshold": 70}],
                         "initial_sl": 4740.0, "initial_tp": 4710.0},
            "management": [{"name": "lock_be_at_10_profit",
                            "priority": 7,
                            "conditions": [{"type": "profit_pips", "op": "above",
                                            "threshold": 10}],
                            "action": {"type": "move_sl_to_breakeven",
                                       "offset_pips": 0},
                            "fires": "once"}],
            "exit": [{"name": "rsi_exit",
                      "priority": 9,
                      "conditions": [{"type": "rsi", "tf": "H1", "op": "below",
                                      "threshold": 40}],
                      "action": {"type": "close_full"},
                      "fires": "once"}],
            "emergency": {"max_loss_pips": 150, "max_duration_minutes": 480,
                          "on_broker_error": "alert_floki"},
        }

    def test_example_in_prompt_validates_clean(self):
        ok, parsed, errors = validate_plan(self._prompt_example_plan())
        assert ok, f"prompt example does not validate: {errors}"
        assert parsed is not None
        assert errors == []

    def test_example_appears_in_system_prompt(self):
        """The example text fragment (MINIMAL PLAN EXAMPLE) must appear
        in SYSTEM_PROMPT. Catches accidental prompt deletion / rename."""
        from agent_prompts import SYSTEM_PROMPT
        assert "MINIMAL PLAN EXAMPLE:" in SYSTEM_PROMPT, (
            "prompt example marker missing — drift from Phase 6.5 state"
        )

    def test_prompt_example_key_fields_match_example(self):
        """Spot-check that the literal JSON fragment in the prompt contains
        the distinctive field strings this test uses. If someone edits the
        prompt to change field names without updating this test, catch it."""
        from agent_prompts import SYSTEM_PROMPT
        for marker in (
            '"initial_sl": 4740.0',
            '"initial_tp": 4710.0',
            '"direction": "SELL"',
            '"regime_assumed": "TRENDING_BEARISH"',
            '"type": "rsi"',
            '"type": "move_sl_to_breakeven"',
            '"type": "close_full"',
        ):
            assert marker in SYSTEM_PROMPT, (
                f"prompt example missing expected marker: {marker}"
            )


# =============================================================================
# FLO-347 Phase 7+ — mandatory-plan pivot regression guards (v3.2 + v3.3)
# =============================================================================

class TestPromptMandatoryPlan:
    """Forcing functions for the Escola 2 mandatory-plan pivot. If future
    edits accidentally soften the mandatory-plan framing back toward the
    v3.1 permissive style, these tests flip red. Version-agnostic class
    name — the mandate carries across v3.2+ revisions. Only the version
    assertion below (`EXPECTED_VERSION`) is version-specific and MUST be
    bumped in lockstep with `agent_prompts.get_prompt_version()`."""

    EXPECTED_VERSION = "3.3"

    def test_prompt_version_matches_expected(self):
        """Guards against forgotten version bump alongside a prompt edit."""
        from agent_prompts import get_prompt_version
        assert get_prompt_version() == self.EXPECTED_VERSION, (
            f"prompt version drift: got {get_prompt_version()!r}, "
            f"expected {self.EXPECTED_VERSION!r}"
        )

    def test_v3_2_mandatory_plan_framing_present(self):
        """The mandatory-plan mandate lives near submit_plan_to_snow. A
        future edit that accidentally softens it back to 'when you want
        a plan' must fail this test.

        Checks:
          * `submit_plan_to_snow` is still referenced in the prompt
          * `list_active_plans` appears as a cycle-start action
          * The 'primary deliverable' (or equivalent) language is present
        """
        from agent_prompts import SYSTEM_PROMPT
        assert "submit_plan_to_snow" in SYSTEM_PROMPT
        # Cycle-start list-active-plans action — codified in v3.2 but not v3.1.
        assert "CYCLE-START" in SYSTEM_PROMPT or "cycle start" in SYSTEM_PROMPT, (
            "v3.2 cycle-start mandate missing"
        )
        # Deliverable / projective framing — the chosen phrasing for
        # 'must submit a plan' without a prescriptive MUST.
        assert ("primary deliverable" in SYSTEM_PROMPT
                or "MUST submit" in SYSTEM_PROMPT), (
            "v3.2 mandatory framing missing — plan submission must be "
            "described as the cycle's deliverable or a MUST"
        )
        # Worked flow marker
        assert "WORKED FLOW" in SYSTEM_PROMPT, (
            "v3.2 WORKED FLOW example missing"
        )
        # Validation retry pedagogy — max 3 attempts
        assert "3 attempts" in SYSTEM_PROMPT or "3 tries" in SYSTEM_PROMPT or "Maximum 3" in SYSTEM_PROMPT, (
            "v3.2 validation retry (max 3) pedagogy missing"
        )

    def test_v3_2_observation_plans_mentioned(self):
        """Observation / conditional-branch plans for ambiguous markets
        are a key v3.2 addition — sharpens market read when no clear
        directional scenario exists."""
        from agent_prompts import SYSTEM_PROMPT
        assert ("AMBIGUOUS" in SYSTEM_PROMPT or "ambiguous" in SYSTEM_PROMPT
                or "observation plan" in SYSTEM_PROMPT.lower()), (
            "v3.2 ambiguous-market / observation-plan guidance missing"
        )


# =============================================================================
# FLO-347 Phase 7.1 (v3.3) — chart-suite + Luna-distinctness regressions
# =============================================================================

class TestPromptV3_3ChartSuite:
    """v3.3 tightens the FULL ANALYTICAL SUITE paragraph after observing
    that Floki's first post-v3.2 plan only requested 4 of 6 chart
    timeframes and skipped get_luna_brief. These guards prevent the
    tightening from being silently softened later."""

    def test_all_six_chart_timeframes_enumerated(self):
        """Prompt must name all 6 TFs (D1, H4, H1, M15, M5, M1) in the
        context of get_chart_screenshots — not just 'multi-TF'."""
        from agent_prompts import SYSTEM_PROMPT
        # Must mention get_chart_screenshots AND each of the 6 TF tokens
        # in a proximity consistent with the requirement (all 6 named).
        assert "get_chart_screenshots" in SYSTEM_PROMPT
        for tf in ("D1", "H4", "H1", "M15", "M5", "M1"):
            assert tf in SYSTEM_PROMPT, (
                f"v3.3 chart-suite mandate missing timeframe {tf!r}"
            )
        # Explicit "ALL 6 timeframes" anchor — catches a soft edit that
        # keeps the TF list but drops the enumeration imperative.
        assert ("ALL 6 timeframes" in SYSTEM_PROMPT
                or "all 6 timeframes" in SYSTEM_PROMPT), (
            "v3.3 chart-suite mandate missing the 'all 6 timeframes' anchor"
        )

    def test_chart_endpoints_explicitly_called_out(self):
        """D1 (week's structural frame) and M1 (live test of level) MUST
        be mentioned as rationale, not left to inference. Catches an edit
        that lists the 6 TFs but doesn't explain why endpoints matter."""
        from agent_prompts import SYSTEM_PROMPT
        assert ("week's structural frame" in SYSTEM_PROMPT
                or "week" in SYSTEM_PROMPT and "structural" in SYSTEM_PROMPT), (
            "v3.3: D1 rationale (week's structural frame) missing"
        )
        assert ("live test" in SYSTEM_PROMPT or "level under" in SYSTEM_PROMPT), (
            "v3.3: M1 rationale (live test of level) missing"
        )

    def test_luna_distinctness_from_market_context_called_out(self):
        """Luna is NOT a price feed. v3.3 adds an explicit note so Floki
        stops substituting get_market_context for get_luna_brief."""
        from agent_prompts import SYSTEM_PROMPT
        assert "get_luna_brief" in SYSTEM_PROMPT
        assert "get_market_context" in SYSTEM_PROMPT
        # The distinction sentence — Luna != market_context.
        assert "NOT duplicated" in SYSTEM_PROMPT or "not duplicated" in SYSTEM_PROMPT, (
            "v3.3: Luna/market_context distinctness language missing"
        )
        # The WHY — Luna returns patterns, not just prices.
        assert ("correlation-break" in SYSTEM_PROMPT
                or "safe-haven" in SYSTEM_PROMPT
                or "risk-flow" in SYSTEM_PROMPT), (
            "v3.3: Luna's distinctive content (pattern analysis) not named"
        )

    def test_scope_limiter_preserves_agent_autonomy(self):
        """The mandatory-suite rule applies ONLY when the mandatory
        workflow is active (no position + no active plan). Outside that
        state, Floki retains normal agent-first autonomy. This scope
        limiter MUST be explicit — without it, the suite enumeration
        becomes a prescriptive always-rule that contradicts the agent-
        first principles captured in the owner's memories."""
        from agent_prompts import SYSTEM_PROMPT
        # Look for the "autonomy returns" / "suite is not mandatory"
        # escape-hatch phrasing.
        assert ("not mandatory" in SYSTEM_PROMPT
                or "autonomy returns" in SYSTEM_PROMPT
                or "your normal autonomy" in SYSTEM_PROMPT), (
            "v3.3: scope-limiter missing — suite tightening must be scoped "
            "to the mandatory-workflow state only"
        )
        # And the triggering condition for the scope — open position
        # OR active plan
        assert "open position" in SYSTEM_PROMPT
        assert "active plan" in SYSTEM_PROMPT
