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
