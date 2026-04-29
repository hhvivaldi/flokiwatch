"""Schema tests — RFC §12.2 "Unit tests (validator) ~10 tests" (schema half).

Exercises the Pydantic v2 models in snow.schema directly (no business-rule
checks here; those are in validator_test.py). Coverage:

  * Happy path: canonical plan parses round-trip clean
  * Discriminated union: each primitive type accepted
  * Field constraints: out-of-range values rejected
  * Extra fields forbidden (model_config extra="forbid")
  * Plan ID pattern enforcement
  * Nested Contingency + ContingencyGuards round-trip
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from snow.schema import (
    SCHEMA_VERSION,
    ActionCloseFull,
    ATR,
    Contingency,
    ContingencyGuards,
    DurationExceeds,
    EMARelation,
    EntryBlock,
    MACDHistogram,
    MAEReached,
    MFEReached,
    PriceAbove,
    PriceAtFibonacci,
    PriceAtSRZone,
    PriceBelow,
    ProfitPips,
    ProfitRetracedFromPeak,
    Plan,
    RSI,
    TimeBetween,
)


# =============================================================================
# Happy path
# =============================================================================

class TestPlanRoundtrip:

    def test_canonical_plan_parses(self, valid_plan_dict):
        p = Plan(**valid_plan_dict)
        assert p.id == "PLAN-20260424-001"
        assert p.schema_version == SCHEMA_VERSION
        assert p.entry.direction.value == "SELL"
        assert len(p.management) == 1
        assert len(p.exit) == 2

    def test_roundtrip_via_model_dump(self, valid_plan_dict):
        p1 = Plan(**valid_plan_dict)
        dumped = p1.model_dump(mode="json")
        p2 = Plan(**dumped)
        assert p2.id == p1.id
        assert p2.entry.initial_sl == p1.entry.initial_sl
        assert len(p2.exit) == len(p1.exit)

    def test_minimal_plan_omitting_exit_raises(self, valid_plan_dict):
        """FLO-401: exit is mandatory (min_length=1). A plan that omits it
        — or supplies an empty list — must raise ValidationError. This
        inverts the prior `test_minimal_plan_accepts_defaults` contract,
        which was the implicit codification of the regressed default that
        Gemini surfaced (PLAN-20260429-005/006 shipped exit=[] and left
        management-only as the entire downside-protection layer)."""
        minimal_no_exit = {
            "id": "PLAN-20260424-002",
            "created_at": "2026-04-24T08:00:00Z",
            "analysis": valid_plan_dict["analysis"],
            "entry": valid_plan_dict["entry"],
        }
        with pytest.raises(ValidationError):
            Plan(**minimal_no_exit)

        # Empty list also rejected — min_length=1, not just non-None.
        minimal_empty_exit = {**minimal_no_exit, "exit": []}
        with pytest.raises(ValidationError):
            Plan(**minimal_empty_exit)

    def test_minimal_plan_accepts_management_default(self, valid_plan_dict):
        """FLO-401 split: management still defaults to []. emergency still
        materializes its own defaults. Status still defaults to PENDING.
        Only `exit` had its default-empty contract tightened."""
        minimal_with_exit = {
            "id": "PLAN-20260424-003",
            "created_at": "2026-04-24T08:00:00Z",
            "analysis": valid_plan_dict["analysis"],
            "entry": valid_plan_dict["entry"],
            "exit": valid_plan_dict["exit"],
        }
        p = Plan(**minimal_with_exit)
        assert p.management == []
        assert len(p.exit) >= 1
        assert p.emergency.max_loss_pips == 150
        assert p.status.value == "pending"


# =============================================================================
# Plan ID pattern
# =============================================================================

class TestPlanIdPattern:

    @pytest.mark.parametrize("bad_id", [
        "PLAN-2026-001",          # missing full YYYYMMDD
        "PLAN-20260424-1",        # NNN must be 3 digits
        "PLAN-20260424-1000",     # NNN > 3 digits
        "plan-20260424-001",      # lowercase prefix
        "PLAN_20260424_001",      # wrong separator
        "",
    ])
    def test_rejects_malformed_id(self, valid_plan_dict, bad_id):
        valid_plan_dict["id"] = bad_id
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_accepts_valid_id(self, valid_plan_dict):
        valid_plan_dict["id"] = "PLAN-20261231-999"
        p = Plan(**valid_plan_dict)
        assert p.id == "PLAN-20261231-999"


# =============================================================================
# Timestamp Z-suffix enforcement (Rule 22)
# =============================================================================

class TestTimestampZRule:

    def test_created_at_without_z_rejected(self, valid_plan_dict):
        valid_plan_dict["created_at"] = "2026-04-24T08:00:00"
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_expires_at_without_z_rejected(self, valid_plan_dict):
        valid_plan_dict["expires_at"] = "2026-04-24T12:00:00"
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_expires_at_None_allowed(self, valid_plan_dict):
        valid_plan_dict["expires_at"] = None
        p = Plan(**valid_plan_dict)
        assert p.expires_at is None


# =============================================================================
# Entry block constraints
# =============================================================================

class TestEntryBlock:

    @pytest.mark.parametrize("bad_vol", [0.0, -0.01, 2.01, 100])
    def test_volume_out_of_range(self, valid_plan_dict, bad_vol):
        valid_plan_dict["entry"]["volume"] = bad_vol
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_direction_enum_enforced(self, valid_plan_dict):
        valid_plan_dict["entry"]["direction"] = "HOLD"
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_conditions_minimum_one(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = []
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_conditions_max_eight(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_above", "level": 4700 + i} for i in range(9)
        ]
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)


# =============================================================================
# Condition primitives — each type parses
# =============================================================================

class TestConditionPrimitives:
    """One parse-check per primitive — confirms discriminated union routes."""

    def test_price_above(self):
        c = PriceAbove(level=4700.0)
        assert c.type == "price_above"

    def test_price_below(self):
        c = PriceBelow(level=4700.0)
        assert c.type == "price_below"

    def test_rsi_roundtrip(self):
        c = RSI(tf="H1", op="above", threshold=70.0)
        assert c.tf == "H1"

    def test_rsi_invalid_threshold(self):
        with pytest.raises(ValidationError):
            RSI(tf="H1", op="above", threshold=101)

    def test_rsi_invalid_timeframe(self):
        with pytest.raises(ValidationError):
            RSI(tf="M2", op="above", threshold=50)  # type: ignore[arg-type]

    def test_macd_histogram(self):
        c = MACDHistogram(tf="M5", op="below", threshold=0.0)
        assert c.type == "macd_histogram"

    def test_ema_relation_aligned_bull(self):
        c = EMARelation(tf="H1", period=50, relation="aligned_bull")
        assert c.relation == "aligned_bull"

    def test_ema_relation_invalid_period(self):
        with pytest.raises(ValidationError):
            EMARelation(tf="H1", period=100, relation="price_above")  # type: ignore[arg-type]

    def test_atr(self):
        c = ATR(tf="M15", op="above", multiplier=1.5, baseline_pips=20)
        assert c.multiplier == 1.5

    def test_atr_rejects_zero_multiplier(self):
        with pytest.raises(ValidationError):
            ATR(tf="M15", op="above", multiplier=0, baseline_pips=20)

    def test_price_at_sr_zone(self):
        c = PriceAtSRZone(zone_type="support", tolerance_pips=5.0)
        assert c.zone_type == "support"

    def test_price_at_fibonacci(self):
        c = PriceAtFibonacci(level=0.618)
        assert c.level == 0.618

    def test_price_at_fibonacci_rejects_string_level(self):
        # JSON callers pass numeric levels (0.618), not quoted strings.
        # Literal[0.382, 0.5, 0.618, 0.786] must reject "0.618".
        with pytest.raises(ValidationError):
            PriceAtFibonacci(level="0.618")

    def test_price_at_fibonacci_rejects_offlist_numeric(self):
        # 0.5000001 is a valid float but not in the enumerated set.
        with pytest.raises(ValidationError):
            PriceAtFibonacci(level=0.5000001)

    def test_profit_pips(self):
        c = ProfitPips(op="above", threshold=10)
        assert c.threshold == 10

    def test_mfe_reached(self):
        c = MFEReached(pips=50)
        assert c.pips == 50

    def test_mae_reached(self):
        c = MAEReached(pips=50)
        assert c.pips == 50

    def test_profit_retraced_from_peak(self):
        c = ProfitRetracedFromPeak(pips=20)
        assert c.pips == 20

    def test_duration_exceeds(self):
        c = DurationExceeds(minutes=60)
        assert c.minutes == 60

    def test_duration_exceeds_rejects_zero(self):
        with pytest.raises(ValidationError):
            DurationExceeds(minutes=0)

    def test_time_between_valid(self):
        c = TimeBetween(start_utc="06:00", end_utc="18:00")
        assert c.start_utc == "06:00"

    def test_time_between_rejects_bad_format(self):
        with pytest.raises(ValidationError):
            TimeBetween(start_utc="6:00", end_utc="18:00")
        with pytest.raises(ValidationError):
            TimeBetween(start_utc="06:60", end_utc="18:00")
        with pytest.raises(ValidationError):
            TimeBetween(start_utc="24:00", end_utc="18:00")


# =============================================================================
# Stateful primitives explicitly NOT in v1 — §6.6 decision
# =============================================================================

class TestStatefulPrimitivesRejected:

    def test_price_crosses_above_unknown_type(self, valid_plan_dict):
        """Discriminated union must reject the v2-deferred stateful types."""
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_crosses_above", "level": 4700.0}
        ]
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_price_crosses_below_unknown_type(self, valid_plan_dict):
        valid_plan_dict["entry"]["conditions"] = [
            {"type": "price_crosses_below", "level": 4700.0}
        ]
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)


# =============================================================================
# Contingency + guards
# =============================================================================

class TestContingency:

    def test_priority_default_5(self):
        c = Contingency(
            name="c1",
            conditions=[PriceAbove(level=4700)],
            action=ActionCloseFull(),
        )
        assert c.priority == 5

    @pytest.mark.parametrize("bad", [0, -1, 11, 100])
    def test_priority_out_of_range_rejected(self, bad):
        with pytest.raises(ValidationError):
            Contingency(
                name="c1", priority=bad,
                conditions=[PriceAbove(level=4700)],
                action=ActionCloseFull(),
            )

    def test_name_length_limit(self):
        with pytest.raises(ValidationError):
            Contingency(
                name="x" * 41,
                conditions=[PriceAbove(level=4700)],
                action=ActionCloseFull(),
            )

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            Contingency(
                name="",
                conditions=[PriceAbove(level=4700)],
                action=ActionCloseFull(),
            )

    def test_guards_optional(self):
        c = Contingency(
            name="c1",
            conditions=[PriceAbove(level=4700)],
            action=ActionCloseFull(),
        )
        assert c.guards is None

    def test_guards_populated_roundtrip(self):
        g = ContingencyGuards(
            only_if_tighter_sl=True, cooldown_seconds=60,
            min_mfe_pips_required=20, max_adjustments_total=3,
        )
        c = Contingency(
            name="c1",
            conditions=[PriceAbove(level=4700)],
            action=ActionCloseFull(),
            guards=g,
        )
        assert c.guards.only_if_tighter_sl is True
        assert c.guards.cooldown_seconds == 60

    def test_guards_reject_negative_cooldown(self):
        with pytest.raises(ValidationError):
            ContingencyGuards(cooldown_seconds=-1)


# =============================================================================
# Extra fields forbidden (model_config extra="forbid")
# =============================================================================

class TestExtraFieldsForbidden:

    def test_plan_rejects_unknown_field(self, valid_plan_dict):
        valid_plan_dict["__hacky_extra_field__"] = 42
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_entry_rejects_unknown_field(self, valid_plan_dict):
        valid_plan_dict["entry"]["__hacky_extra__"] = "nope"
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_contingency_rejects_unknown_field(self, valid_plan_dict):
        valid_plan_dict["exit"][0]["__hacky__"] = "nope"
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)


# =============================================================================
# Capacity limits
# =============================================================================

class TestCapacityLimits:

    def test_management_max_ten(self, valid_plan_dict):
        valid_plan_dict["management"] = [
            {
                "name": f"m{i}",
                "priority": 5,
                "conditions": [{"type": "price_below", "level": 4700.0 + i}],
                "action": {"type": "move_sl_to_price", "price": 4700.0 + i},
                "fires": "once",
            }
            for i in range(11)
        ]
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)

    def test_exit_max_ten(self, valid_plan_dict):
        valid_plan_dict["exit"] = [
            {
                "name": f"e{i}",
                "priority": 5,
                "conditions": [{"type": "price_above", "level": 4700.0 + i}],
                "action": {"type": "close_full"},
                "fires": "once",
            }
            for i in range(11)
        ]
        with pytest.raises(ValidationError):
            Plan(**valid_plan_dict)
