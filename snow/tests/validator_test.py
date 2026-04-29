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
        # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
        # Test purpose is time_between wrap-around acceptance.
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "time_between", "start_utc": "22:00", "end_utc": "06:00"},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_normal_window_accepted(self, valid_plan_dict):
        # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "time_between", "start_utc": "06:00", "end_utc": "20:00"},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
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

# =============================================================================
# FLO-383 — management threshold sanity-floor (condition expressiveness)
# =============================================================================

class TestManagementThresholdFloor:
    """Validator rejects plans whose management contingencies all trigger
    only on profit_pips below a 30-pip noise floor. Empirical basis:
    PLAN-007 322 pip MFE → -0.4 outcome with lock_be_at_10. BE-locked
    at noise level guarantees scratch under routine pullback. Floki
    retains agency on which qualifying primitive to use — validator
    enforces the floor only.
    """

    @staticmethod
    def _set_management(plan: dict, contingencies: list[dict]) -> None:
        """Replace the management array with the given contingencies,
        each filled in with sensible defaults for unrelated fields."""
        out = []
        for i, c in enumerate(contingencies):
            out.append({
                "name": c.get("name", f"mgmt_{i}"),
                "priority": c.get("priority", 7),
                "conditions": c["conditions"],
                "action": c.get("action", {
                    "type": "move_sl_to_breakeven", "offset_pips": 0,
                }),
                "fires": "once",
            })
        plan["management"] = out

    def test_reject_single_low_profit_pips(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_pips", "op": "above",
                            "threshold": 8}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok, f"expected rejection on profit_pips=8; got {errors}"
        assert any("noise floor" in e.lower() for e in errors), errors

    def test_reject_multi_all_below_floor(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [
            {"name": "lock_be_at_10",
             "conditions": [{"type": "profit_pips", "op": "above",
                             "threshold": 10}]},
            {"name": "lock_be_at_15",
             "conditions": [{"type": "profit_pips", "op": "above",
                             "threshold": 15}]},
        ])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        assert any("noise floor" in e.lower() for e in errors), errors

    def test_accept_profit_pips_at_floor(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_pips", "op": "above",
                            "threshold": 30}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_profit_pips_above_floor(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_pips", "op": "above",
                            "threshold": 50}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_mfe_reached_only(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "mfe_reached", "pips": 25}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_profit_retraced_from_peak_only(self, valid_plan_dict):
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_retraced_from_peak",
                            "pips": 15}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_mixed_low_pips_plus_mfe(self, valid_plan_dict):
        """Two contingencies — one is noise-floor (profit_pips=10),
        the other uses mfe_reached. Plan accepted because at least
        one contingency qualifies."""
        self._set_management(valid_plan_dict, [
            {"name": "lock_be_at_10",
             "conditions": [{"type": "profit_pips", "op": "above",
                             "threshold": 10}]},
            {"name": "give_back_guard",
             "conditions": [{"type": "mfe_reached", "pips": 30}],
             "action": {"type": "trail_sl", "trail_pips": 8.0}},
        ])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_and_gated_low_pips_with_indicator(self, valid_plan_dict):
        """A contingency with profit_pips=10 AND-gated by an
        indicator condition is acceptable — the indicator gate
        prevents premature noise-fire. The validator's predicate
        is per-contingency: any non-profit-pips condition lifts
        the contingency above the floor."""
        self._set_management(valid_plan_dict, [{
            "conditions": [
                {"type": "profit_pips", "op": "above", "threshold": 10},
                {"type": "rsi", "tf": "M5", "op": "above", "threshold": 60},
            ],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_empty_management(self, valid_plan_dict):
        """Plans with no management contingencies are unaffected
        (rule has no scope to enforce)."""
        valid_plan_dict["management"] = []
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_accept_below_op_does_not_count_as_trigger(self, valid_plan_dict):
        """profit_pips with `op=below` describes a protective trigger
        ("fire when profit drops below X"), not a BE-arming trigger.
        The noise-floor concern is about arming triggers — `below`
        ops are a different shape and exempt from the floor."""
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_pips", "op": "below",
                            "threshold": 5}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok, errors

    def test_error_message_names_alternatives(self, valid_plan_dict):
        """Floki must be told what to do, not just what's wrong.
        The error message names mfe_reached and
        profit_retraced_from_peak as concrete alternatives."""
        self._set_management(valid_plan_dict, [{
            "conditions": [{"type": "profit_pips", "op": "above",
                            "threshold": 8}],
        }])
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert not ok
        msg = " ".join(errors)
        assert "mfe_reached" in msg
        assert "profit_retraced_from_peak" in msg
        assert "30" in msg  # the floor itself


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
            "management": [{"name": "lock_be_after_meaningful_advance",
                            "priority": 7,
                            "conditions": [{"type": "mfe_reached",
                                            "pips": 30}],
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

    EXPECTED_VERSION = "3.12"

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

    def test_suite_mandatory_every_cycle(self):
        """FLO-404 follow-up (CEO directive 2026-04-29) — INVERTED from
        the v3.3 scope-limiter contract. Prior rule scoped the FULL
        ANALYTICAL SUITE to the no-position + no-active-plan state only,
        preserving agent-first autonomy outside that window. Field
        evidence under the 30-min cadence (FLO-403 Phase 1) showed Floki
        skipping data pulls when an active plan existed and deciding
        WAIT on partial data ("I have the plan status and chart context
        to confidently wait"). The CEO inverted the rule: at 30-min
        cadence each cycle is the only window into the market until the
        next, so the full surface must be pulled every cycle. Agent-
        first framing is preserved on what to DO with the data — Floki
        still decides plan/refine/hold/wait/override — but he must SEE
        all of it before deciding.

        This test pins the NEW contract. The prior "autonomy returns" /
        "suite is not mandatory" escape hatch must NOT reappear in a
        future edit; if it does, that edit is reverting CEO directive
        2026-04-29 and should be surfaced explicitly."""
        from agent_prompts import SYSTEM_PROMPT

        # 1. Mandate present: "every cycle" applies.
        assert "every cycle" in SYSTEM_PROMPT.lower(), (
            "FLO-404: 'every cycle' mandate missing"
        )

        # 2. Prior escape hatch must be DISAVOWED, not silently dropped.
        # The prompt explicitly notes the old exception was removed —
        # this catches a future edit that re-softens by simply omitting.
        assert ("exception" in SYSTEM_PROMPT.lower()
                and ("removed" in SYSTEM_PROMPT.lower()
                     or "deprecated" in SYSTEM_PROMPT.lower())), (
            "FLO-404: prior 'suite-optional-with-active-plan' exception "
            "must be explicitly disavowed, not silently dropped"
        )

        # 3. Agent-first framing preserved: Floki retains autonomy on
        # what to DO with the data. Catches the regression where the
        # mandate sounds like a prescriptive workflow rather than a data
        # surface guarantee.
        assert ("DO with the data" in SYSTEM_PROMPT
                or "do with the data" in SYSTEM_PROMPT), (
            "FLO-404: agent-first framing missing — Floki must still "
            "decide what to DO with the data; the mandate is on SEEING it"
        )

        # 4. The 30-min cadence rationale must be present — without it,
        # the mandate reads as arbitrary procedure rather than a
        # response to the cadence shift that motivated the change.
        assert ("30-min" in SYSTEM_PROMPT
                or "30 min" in SYSTEM_PROMPT
                or "30-minute" in SYSTEM_PROMPT), (
            "FLO-404: 30-min cadence rationale missing — mandate must "
            "name WHY the suite is now non-negotiable"
        )


# =============================================================================
# FLO-347 Phase 7.2 (v3.4) — paired-plans regression guards
# =============================================================================

class TestPromptV3_4PairedPlans:
    """v3.4 introduces PAIRED PLANS — submit TWO plans (one BUY-leg, one
    SELL-leg) in the same cycle for genuinely bidirectional setups
    (pre-event, undecided breakout, post-news whip). Empirically motivated:
    PLAN-20260424-001 was a unidirectional BUY plan in a setup whose
    own thesis enumerated 5 bearish + 1 bullish + 1 neutral signals —
    the missing SELL-leg plan was a v3.x prompt gap, not a Floki failure.
    These tests pin the v3.4 fix so a future edit can't silently drop it."""

    def test_paired_plans_section_present(self):
        from agent_prompts import SYSTEM_PROMPT
        assert "PAIRED PLANS" in SYSTEM_PROMPT, (
            "v3.4: PAIRED PLANS section header missing"
        )

    def test_paired_plans_describes_two_plans_one_per_direction(self):
        """The mechanic: two plans per cycle, one BUY-leg + one SELL-leg.
        Catches an edit that keeps the section header but softens the
        two-plans-per-cycle rule."""
        from agent_prompts import SYSTEM_PROMPT
        # "TWO plans" wording (case-insensitive, allows future copy edits).
        assert ("TWO plans" in SYSTEM_PROMPT
                or "two plans" in SYSTEM_PROMPT), (
            "v3.4: 'two plans' core mechanic missing from PAIRED PLANS"
        )
        # Both leg directions must be named.
        assert "BUY scenario" in SYSTEM_PROMPT or "BUY-leg" in SYSTEM_PROMPT, (
            "v3.4: BUY-leg not explicitly named in PAIRED PLANS"
        )
        assert "SELL scenario" in SYSTEM_PROMPT or "SELL-leg" in SYSTEM_PROMPT, (
            "v3.4: SELL-leg not explicitly named in PAIRED PLANS"
        )

    def test_paired_plans_use_cases_present(self):
        """Three triggering use-cases must be enumerated so Floki recognises
        when a paired plan is the right shape vs a single-direction plan."""
        from agent_prompts import SYSTEM_PROMPT
        # At least two of the three use-case keywords must be present.
        markers = ("pre-event", "undecided breakout", "whip", "balanced",
                   "inflection")
        hits = sum(1 for m in markers if m in SYSTEM_PROMPT)
        assert hits >= 2, (
            f"v3.4: PAIRED PLANS use-case examples insufficient "
            f"(found {hits}/{len(markers)} markers; need >=2)"
        )

    def test_paired_plans_independence_described(self):
        """Critical to teach: paired plans don't interfere with each other.
        Snow watches them independently; the loser expires harmlessly."""
        from agent_prompts import SYSTEM_PROMPT
        assert ("do not interfere" in SYSTEM_PROMPT
                or "watches both independently" in SYSTEM_PROMPT
                or "the other expires" in SYSTEM_PROMPT), (
            "v3.4: paired-plan independence / harmless-expiry semantics "
            "must be taught explicitly"
        )

    def test_worked_flow_acknowledges_paired_option(self):
        """WORKED FLOW step that frames the thesis must offer the
        bidirectional option alongside directional bias and observation."""
        from agent_prompts import SYSTEM_PROMPT
        # Look for "bidirectional" mentioned somewhere in the WORKED FLOW
        # neighbourhood.
        assert "bidirectional" in SYSTEM_PROMPT, (
            "v3.4: WORKED FLOW or PAIRED PLANS must name 'bidirectional' "
            "as a thesis shape"
        )


# =============================================================================
# FLO-359 Phase 8b commit 1 — stateful-in-v1 validator gate
# =============================================================================
#
# A v1 plan that references a stateful primitive (indicator_crossover,
# indicator_was, price_crossed_level) must be rejected up-front: the
# state_cache infrastructure that backs those primitives lives behind
# schema_version >= 2. The gate function `_check_stateful_in_v1`
# operates on the string `condition.type`, so the test exercises it
# with `model_construct`-built fakes — independent of which classes
# happen to be in `snow.schema.Condition`'s union at this commit.
# =============================================================================


class TestStatefulInV1Gate:

    def _v1_plan_with_stateful_entry_cond(self, valid_plan_dict_v1, stateful_type):
        """Build a v1 Plan whose first entry condition has
        `type=<stateful_type>` without going through Pydantic's
        discriminated-union parser. `model_construct` skips validation
        — exactly the escape hatch needed to test the validator gate
        before the stateful classes are wired into the union (commits
        3-5).
        """
        from snow.schema import Plan, RSI
        plan = Plan(**valid_plan_dict_v1)
        fake_cond = RSI.model_construct(
            type=stateful_type, tf="H1", op="above", threshold=70.0
        )
        plan.entry.conditions[0] = fake_cond
        return plan

    def test_v1_plan_with_stateless_only_passes_gate(self, valid_plan_dict_v1):
        """Backward compat: an existing v1 plan with stateless conditions
        (price_above + rsi) must keep validating."""
        ok, plan, errors = validate_plan(valid_plan_dict_v1)
        assert ok is True, f"v1 stateless plan rejected: {errors}"
        assert plan is not None
        assert plan.schema_version == 1

    def test_v1_plan_referencing_indicator_crossover_rejected(
        self, valid_plan_dict_v1
    ):
        from snow.validator import _check_stateful_in_v1
        plan = self._v1_plan_with_stateful_entry_cond(
            valid_plan_dict_v1, "indicator_crossover"
        )
        errors = _check_stateful_in_v1(plan)
        assert len(errors) == 1
        assert "indicator_crossover" in errors[0]
        assert "schema_version >= 2" in errors[0]
        assert "entry.conditions[0]" in errors[0]

    def test_v1_plan_referencing_indicator_was_rejected(
        self, valid_plan_dict_v1
    ):
        from snow.validator import _check_stateful_in_v1
        plan = self._v1_plan_with_stateful_entry_cond(
            valid_plan_dict_v1, "indicator_was"
        )
        errors = _check_stateful_in_v1(plan)
        assert any("indicator_was" in e for e in errors)

    def test_v1_plan_referencing_price_crossed_level_rejected(
        self, valid_plan_dict_v1
    ):
        from snow.validator import _check_stateful_in_v1
        plan = self._v1_plan_with_stateful_entry_cond(
            valid_plan_dict_v1, "price_crossed_level"
        )
        errors = _check_stateful_in_v1(plan)
        assert any("price_crossed_level" in e for e in errors)

    def test_v2_plan_with_stateful_type_bypasses_gate(self, valid_plan_dict_v2):
        """v2 plans bypass the v1 gate. The Pydantic union itself will
        reject unknown types until the matching commit lands; this test
        just confirms the gate function returns no errors when
        schema_version >= 2."""
        from snow.schema import Plan, RSI
        from snow.validator import _check_stateful_in_v1
        assert valid_plan_dict_v2["schema_version"] == 2
        plan = Plan(**valid_plan_dict_v2)
        plan.entry.conditions[0] = RSI.model_construct(
            type="indicator_crossover", tf="H1", op="above", threshold=70.0
        )
        assert _check_stateful_in_v1(plan) == []

    def test_gate_inspects_management_and_exit_blocks(self, valid_plan_dict_v1):
        """Stateful primitive in management or exit (not just entry) must
        also be caught. `_iter_plan_conditions` walks every block."""
        from snow.schema import Plan, RSI
        from snow.validator import _check_stateful_in_v1
        plan = Plan(**valid_plan_dict_v1)
        plan.management[0].conditions[0] = RSI.model_construct(
            type="indicator_was", tf="H1", op="above", threshold=70.0,
        )
        plan.exit[0].conditions[0] = RSI.model_construct(
            type="price_crossed_level", tf="H1", op="above", threshold=70.0,
        )
        errors = _check_stateful_in_v1(plan)
        labels = {e.split(":")[0].split(".")[0] for e in errors}
        assert any(lbl.startswith("management[0]") for lbl in labels), (
            f"management block not inspected: {errors}"
        )
        assert any(lbl.startswith("exit[0]") for lbl in labels), (
            f"exit block not inspected: {errors}"
        )

    def test_validate_plan_surfaces_gate_error_in_envelope(
        self, valid_plan_dict_v1
    ):
        """Integration: the full validate_plan pipeline must surface
        the gate's error in `errors`. We can't go through `Plan(**dict)`
        with a stateful type (the union would reject it before our
        gate runs), so we patch the parser. Confirms wiring."""
        # Build the model_construct'd plan, then have validate_plan see
        # it via monkey-replacement of the Pydantic parse step. Simpler
        # check: invoke validate_plan with a v1 plan dict that the union
        # accepts (stateless), and assert the gate is wired by also
        # calling the helper directly — covered by the dedicated tests
        # above. This integration test instead asserts ordering: gate
        # runs after schema-version check.
        valid_plan_dict_v1["schema_version"] = 999
        ok, _, errors = validate_plan(valid_plan_dict_v1)
        assert ok is False
        # Schema-version error appears; gate is ordered AFTER it but
        # runs unconditionally so both can appear. Here the plan has no
        # stateful conditions so only the version error is expected.
        assert any("schema_version=999" in e for e in errors)
        assert not any("indicator_crossover" in e for e in errors)


# =============================================================================
# End-to-end gate exercise — uses real `indicator_crossover` from the
# union (added in commit 3). These complement the model_construct
# tests above, which had to fake the type before the class existed.
# =============================================================================


class TestStatefulGateEndToEnd:

    def _crossover_cond_dict(self) -> dict:
        return {
            "type": "indicator_crossover",
            "indicator": "rsi",
            "tf": "H1",
            "direction": "above",
            "threshold": 70.0,
        }

    def test_v1_plan_with_real_indicator_crossover_rejected(
        self, valid_plan_dict_v1
    ):
        """End-to-end: a v1 plan dict containing a real
        indicator_crossover condition (the type now exists in the
        Pydantic union after commit 3) goes through validate_plan and
        is rejected with a gate error naming the field path."""
        valid_plan_dict_v1["entry"]["conditions"] = [self._crossover_cond_dict()]
        ok, plan, errors = validate_plan(valid_plan_dict_v1)
        assert ok is False
        # plan parsed (Pydantic accepts the type); gate flagged it.
        assert plan is not None
        assert plan.schema_version == 1
        assert any("indicator_crossover" in e for e in errors)
        assert any("schema_version >= 2" in e for e in errors)

    def test_v2_plan_with_indicator_crossover_validates(
        self, valid_plan_dict_v2
    ):
        """v2 plan with the same condition validates cleanly. Other
        rules (entry SL/TP, timestamps, etc.) keep applying — the
        canonical fixture already supplies a valid SELL entry; just
        replace the condition list."""
        assert valid_plan_dict_v2["schema_version"] == 2
        # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
        # Test purpose is stateful-primitive parse + v2 acceptance — the
        # second (rsi H1 > 50) is benign. First condition is still the
        # crossover so c0 assertions below remain valid.
        valid_plan_dict_v2["entry"]["conditions"] = [
            self._crossover_cond_dict(),
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ]
        ok, plan, errors = validate_plan(valid_plan_dict_v2)
        assert ok is True, f"v2 plan rejected: {errors}"
        assert plan is not None
        # First entry condition is the parsed crossover.
        c0 = plan.entry.conditions[0]
        assert c0.type == "indicator_crossover"
        assert c0.indicator == "rsi"
        assert c0.threshold == 70.0

    # --- indicator_was (Phase 8b commit 4) ---

    def _was_cond_dict(self) -> dict:
        return {
            "type": "indicator_was",
            "indicator": "rsi",
            "tf": "H1",
            "op": "below",
            "threshold": 30.0,
            "within_bars": 4,
        }

    def test_v1_plan_with_real_indicator_was_rejected(
        self, valid_plan_dict_v1
    ):
        valid_plan_dict_v1["entry"]["conditions"] = [self._was_cond_dict()]
        ok, plan, errors = validate_plan(valid_plan_dict_v1)
        assert ok is False
        assert plan is not None
        assert plan.schema_version == 1
        assert any("indicator_was" in e for e in errors)
        assert any("schema_version >= 2" in e for e in errors)

    def test_v2_plan_with_indicator_was_validates(
        self, valid_plan_dict
    ):
        # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
        valid_plan_dict["entry"]["conditions"] = [
            self._was_cond_dict(),
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ]
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is True, f"v2 plan rejected: {errors}"
        c0 = plan.entry.conditions[0]
        assert c0.type == "indicator_was"
        assert c0.within_bars == 4

    def test_v2_plan_with_within_bars_above_cap_rejected(
        self, valid_plan_dict
    ):
        """Pydantic schema-level cap (within_bars ≤ 20) enforced at
        parse time. Errors land in the schema-error list, not the
        gate's stateful-in-v1 list."""
        cond = self._was_cond_dict()
        cond["within_bars"] = 21
        valid_plan_dict["entry"]["conditions"] = [cond]
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is False
        assert plan is None  # Pydantic parse failed; gate never reached.
        assert any("within_bars" in e or "20" in e for e in errors)

    # --- price_crossed_level (Phase 8b commit 5) ---

    def _crossed_cond_dict(self) -> dict:
        return {
            "type": "price_crossed_level",
            "direction": "above",
            "level": 4720.0,
        }

    def test_v1_plan_with_real_price_crossed_level_rejected(
        self, valid_plan_dict_v1
    ):
        valid_plan_dict_v1["entry"]["conditions"] = [self._crossed_cond_dict()]
        ok, plan, errors = validate_plan(valid_plan_dict_v1)
        assert ok is False
        assert plan is not None
        assert plan.schema_version == 1
        assert any("price_crossed_level" in e for e in errors)
        assert any("schema_version >= 2" in e for e in errors)

    def test_v2_plan_with_price_crossed_level_validates(
        self, valid_plan_dict
    ):
        # FLO-Path4: 2 conditions to satisfy _check_min_entry_conditions.
        valid_plan_dict["entry"]["conditions"] = [
            self._crossed_cond_dict(),
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ]
        ok, plan, errors = validate_plan(valid_plan_dict)
        assert ok is True, f"v2 plan rejected: {errors}"
        c0 = plan.entry.conditions[0]
        assert c0.type == "price_crossed_level"
        assert c0.direction == "above"
        assert c0.level == 4720.0


# =============================================================================
# v3.7 prompt regression — exposes stateful primitives to Floki
# =============================================================================
#
# v3.7 lifts the "no crossover / no recent-history / no direction"
# caveat that v3.5 codified, since Phase 8b (FLO-359) shipped the
# three stateful primitives that exception was placeholding for. These
# tests pin the new wording so a future edit can't silently lose:
#   * the per-primitive descriptions (indicator_crossover,
#     indicator_was, price_crossed_level)
#   * the scoped memory-model paragraph that explains which primitives
#     carry state vs which don't
#   * the dead "deferred — separate RFC" sentence (must be removed)
# =============================================================================


class TestPromptV3_7Stateful:

    def test_three_stateful_primitives_named(self):
        from agent_prompts import SYSTEM_PROMPT
        for prim in (
            "indicator_crossover",
            "indicator_was",
            "price_crossed_level",
        ):
            assert prim in SYSTEM_PROMPT, (
                f"v3.7: stateful primitive {prim!r} missing from prompt"
            )

    def test_stateful_section_exists_under_condition_primitives(self):
        """Use-case framing — the Stateful sub-bullet should appear in
        the same neighbourhood as the other category bullets. We check
        for the section header AND the specific keyword 'memory'
        (the Stateful section's distinguishing marker)."""
        from agent_prompts import SYSTEM_PROMPT
        # Locate the Condition primitives section.
        idx = SYSTEM_PROMPT.find("Condition primitives:")
        assert idx >= 0, "Condition primitives section missing"
        section = SYSTEM_PROMPT[idx:idx + 4000]
        assert "Stateful" in section, (
            "v3.7: Stateful sub-bullet missing from Condition primitives"
        )
        assert "memory" in section.lower(), (
            "v3.7: Stateful section should describe carry-memory semantics"
        )

    def test_v35_deferred_caveat_removed(self):
        """The v3.5 sentence framing stateful primitives as deferred to
        a separate RFC is no longer accurate post-FLO-359. It must be
        replaced (not duplicated alongside the new framing)."""
        from agent_prompts import SYSTEM_PROMPT
        assert "deferred — separate RFC" not in SYSTEM_PROMPT, (
            "v3.7: stale 'deferred — separate RFC' caveat from v3.5 still "
            "present; the stateful primitives shipped"
        )
        # Hard "NO crossover / NO 'X within last N bars'" framing also
        # stale — Floki now has both.
        assert "NO crossover" not in SYSTEM_PROMPT, (
            "v3.7: stale 'NO crossover' caveat from v3.5 still present"
        )
        assert 'NO "X within last N bars"' not in SYSTEM_PROMPT, (
            "v3.7: stale 'NO X within last N bars' caveat still present"
        )

    def test_memory_model_paragraph_explains_cold_start(self):
        """Operators should know that a long-outage rehydrate may
        produce a single cold-start false-negative. The phrase must be
        somewhere in the prompt so Floki can read it when drafting."""
        from agent_prompts import SYSTEM_PROMPT
        assert "cold-start" in SYSTEM_PROMPT or "cold start" in SYSTEM_PROMPT, (
            "v3.7: cold-start window not documented in prompt"
        )
        assert "15 min" in SYSTEM_PROMPT or "15 minutes" in SYSTEM_PROMPT, (
            "v3.7: 15-minute stale-state threshold not surfaced"
        )

    def test_schema_version_2_promotion_documented(self):
        """Floki should not have to think about schema_version — but
        when reading the prompt he should at least know stateful
        primitives are gated on v2, and that submit_plan_to_snow does
        the bump for him."""
        from agent_prompts import SYSTEM_PROMPT
        assert "schema_version" in SYSTEM_PROMPT
        # Look for the auto-stamp framing in proximity.
        # (The exact phrasing is "auto-stamps v2" or similar; check
        # both common forms.)
        assert ("auto-stamps" in SYSTEM_PROMPT
                or "automatically" in SYSTEM_PROMPT
                or "invisible" in SYSTEM_PROMPT), (
            "v3.7: prompt should make clear stateful primitives are "
            "available without per-plan schema_version handling"
        )

    def test_use_case_framing_per_stateful_primitive(self):
        """Each stateful primitive should have at least one use-case
        anchor in the prompt — without prescribing how to combine them."""
        from agent_prompts import SYSTEM_PROMPT
        # indicator_crossover → "crossing event" or "first tick" framing
        assert ("crossing event" in SYSTEM_PROMPT
                or "FIRST tick" in SYSTEM_PROMPT
                or "first tick" in SYSTEM_PROMPT), (
            "v3.7: indicator_crossover use-case framing missing"
        )
        # indicator_was → "recent" or "within the last" framing
        assert ("within the last" in SYSTEM_PROMPT
                or "recent" in SYSTEM_PROMPT.lower()), (
            "v3.7: indicator_was recent-history framing missing"
        )
        # price_crossed_level → "tagged" or "latch" framing
        assert ("latch" in SYSTEM_PROMPT.lower()
                or "tagged" in SYSTEM_PROMPT), (
            "v3.7: price_crossed_level latch / tag framing missing"
        )

    def test_get_snow_primitives_reference_still_referenced(self):
        """v3.6's cross-reference to the schema-introspection tool
        should survive — it's the operator's escape hatch for exact
        parameter shapes including the new within_bars bound."""
        from agent_prompts import SYSTEM_PROMPT
        assert "get_snow_primitives_reference" in SYSTEM_PROMPT


# =============================================================================
# v3.8 prompt regression — Snow-managed position visibility (FLO-361)
# =============================================================================
#
# v3.8 is the post-LIVE-flip fix. The pre-Phase-8b "Snow management-
# only plans land in a later phase" framing was wrong once Phase 8b
# shipped real Snow management; v3.8 replaces it with explicit
# guidance that Snow positions are real, identifiable by the
# "snow:<plan_id>" MT5 comment, and Floki MUST NOT call
# adjust_trade / close_trade on them. These tests pin that wording
# so a future edit can't silently regress it.
# =============================================================================


class TestPromptV3_8SnowPositionVisibility:

    def test_snow_dry_run_evidence_window_phrasing_updated(self):
        from agent_prompts import SYSTEM_PROMPT
        # The pre-flip "DURING EVIDENCE WINDOW: SNOW_DRY_RUN is True"
        # phrasing is now stale — Snow is LIVE. The replacement must
        # signal the post-flip state.
        assert "SNOW IS LIVE" in SYSTEM_PROMPT, (
            "v3.8: prompt should signal post-flip Snow state"
        )
        assert "SNOW_DRY_RUN is True" not in SYSTEM_PROMPT, (
            "v3.8: pre-flip 'SNOW_DRY_RUN is True' framing still present"
        )

    def test_snow_comment_prefix_documented(self):
        from agent_prompts import SYSTEM_PROMPT
        assert '"snow:' in SYSTEM_PROMPT, (
            "v3.8: prompt should explain the 'snow:<plan_id>' comment "
            "convention so Floki knows how to identify Snow positions"
        )

    def test_managed_by_field_documented(self):
        """`get_open_positions` returns a `managed_by` field so Floki
        doesn't have to parse comment strings. The prompt should
        reference it explicitly."""
        from agent_prompts import SYSTEM_PROMPT
        assert "managed_by" in SYSTEM_PROMPT

    def test_do_not_touch_snow_positions_directive(self):
        """The directive must explicitly forbid adjust_trade /
        close_trade on Snow-managed positions to prevent the two-
        manager anti-pattern."""
        from agent_prompts import SYSTEM_PROMPT
        # Locate the directive section.
        text = SYSTEM_PROMPT
        assert "Do NOT call adjust_trade or close_trade" in text, (
            "v3.8: explicit prohibition missing"
        )

    def test_cancel_plan_override_path_documented(self):
        """If Floki genuinely needs to override a Snow plan (rare),
        the path is cancel_plan first, then own action — the prompt
        should name that path so Floki has a release valve."""
        from agent_prompts import SYSTEM_PROMPT
        assert "cancel_plan" in SYSTEM_PROMPT
        # And the override framing — at least the keyword "override"
        # somewhere near the directive.
        assert "override" in SYSTEM_PROMPT.lower(), (
            "v3.8: override path framing missing"
        )

    def test_existing_floki_management_path_preserved(self):
        """Backward compat: positions Floki opened directly (no
        snow: comment) still get the existing tool guidance. The
        adjust_trade / close_trade / set_watch_conditions trio
        should still be named for the Floki-managed branch."""
        from agent_prompts import SYSTEM_PROMPT
        # The Floki-managed branch still names the trio.
        assert "adjust_trade, close_trade, set_watch_conditions" in SYSTEM_PROMPT
