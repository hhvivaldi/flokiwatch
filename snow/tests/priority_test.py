"""Snow priority resolver tests — FLO-347 Phase 5a.

Covers:
  * Formula correctness (RFC §8.1 worked examples)
  * Category-gap doubling invariant (RFC §8.1)
  * Inter-category ordering (RFC §8.5 edge cases 1-6)
  * Override clipping for small-base categories
  * Deterministic tie-break chain (RFC §8.3)
  * `resolve()` stability + purity
  * FireEvent dataclass contract

All tests are pure-math. No DB, no executor, no threading.
"""
from __future__ import annotations

import pytest

from snow.priority import (
    FireEvent,
    _BASE_BY_ACTION,
    _DOUBLING_SEQUENCE,
    _verify_doubling_rule,
    action_base,
    effective_priority,
    resolve,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fire(
    *,
    plan_id: str = "PLAN-20260424-001",
    created_at: str = "2026-04-24T08:00:00Z",
    contingency_name: str = "test_c",
    action_type: str = "close_full",
    override: int = 5,
    plan_list_order: int = 0,
    payload=None,
) -> FireEvent:
    return FireEvent(
        plan_id=plan_id,
        created_at=created_at,
        contingency_name=contingency_name,
        action_type=action_type,
        override=override,
        plan_list_order=plan_list_order,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# TestActionBase
# ---------------------------------------------------------------------------

class TestActionBase:
    def test_all_expected_action_types_present(self):
        expected = {
            "close_full", "close_partial", "cancel_plan",
            "adjust_sl", "adjust_tp", "move_sl_to_price", "trail_sl",
            "move_sl_to_breakeven",
            "alert_floki", "escalate_to_floki",
            "execute_market",
        }
        assert set(_BASE_BY_ACTION) == expected, (
            f"base table drift: {set(_BASE_BY_ACTION) ^ expected}"
        )

    def test_bases_match_rfc_8_1(self):
        expected = {
            "close_full": 128,
            "close_partial": 64,
            "cancel_plan": 32,
            "adjust_sl": 16,
            "adjust_tp": 16,
            "move_sl_to_price": 16,
            "trail_sl": 16,
            "move_sl_to_breakeven": 8,
            "alert_floki": 4,
            "escalate_to_floki": 4,
            "execute_market": 1,
        }
        for action_type, base in expected.items():
            assert action_base(action_type) == base, (
                f"{action_type} base drift: {action_base(action_type)} != {base}"
            )

    def test_unknown_action_raises_keyerror(self):
        with pytest.raises(KeyError):
            action_base("bogus_action")


# ---------------------------------------------------------------------------
# TestEffectivePriorityFormula  — RFC §8.1 worked examples
# ---------------------------------------------------------------------------

class TestEffectivePriorityFormula:
    """Each case mirrors the RFC §8.1 worked-examples table."""

    def test_close_full_default_override_5(self):
        # 128 + min(127, 50) = 178
        assert effective_priority("close_full", 5) == 178

    def test_close_full_min_override_1(self):
        # 128 + min(127, 10) = 138
        assert effective_priority("close_full", 1) == 138

    def test_close_full_max_override_10(self):
        # 128 + min(127, 100) = 228
        assert effective_priority("close_full", 10) == 228

    def test_close_partial_max_override_10(self):
        # 64 + min(63, 100) = 127
        assert effective_priority("close_partial", 10) == 127

    def test_cancel_plan_max_override_10(self):
        # 32 + min(31, 100) = 63
        assert effective_priority("cancel_plan", 10) == 63

    def test_adjust_sl_max_override_10(self):
        # 16 + min(15, 100) = 31
        assert effective_priority("adjust_sl", 10) == 31

    def test_move_sl_to_breakeven_default(self):
        # 8 + min(7, 50) = 15
        assert effective_priority("move_sl_to_breakeven", 5) == 15

    def test_alert_floki_default(self):
        # 4 + min(3, 50) = 7
        assert effective_priority("alert_floki", 5) == 7


# ---------------------------------------------------------------------------
# TestCategoryOrderInvariant — RFC §8.5
# ---------------------------------------------------------------------------

class TestCategoryOrderInvariant:
    """Strict category ordering: min of category N >= max of category N+1 + 1.

    RFC §8.5 calls this out: `close_full` with override=1 (min in that
    category) must still exceed `close_partial` with override=10 (max
    in that category). All 5 category boundaries tested.
    """

    def test_close_full_min_exceeds_close_partial_max(self):
        assert (
            effective_priority("close_full", 1)
            > effective_priority("close_partial", 10)
        ), "close_full(1)=138 must beat close_partial(10)=127"

    def test_close_partial_min_exceeds_cancel_plan_max(self):
        assert (
            effective_priority("close_partial", 1)
            > effective_priority("cancel_plan", 10)
        ), "close_partial(1)=74 must beat cancel_plan(10)=63"

    def test_cancel_plan_min_exceeds_adjust_max(self):
        assert (
            effective_priority("cancel_plan", 1)
            > effective_priority("adjust_sl", 10)
        ), "cancel_plan(1)=42 must beat adjust_sl(10)=31"

    def test_adjust_min_exceeds_breakeven_max(self):
        assert (
            effective_priority("adjust_sl", 1)
            > effective_priority("move_sl_to_breakeven", 10)
        ), "adjust_sl(1)=26 must beat move_sl_to_breakeven(10)=15"

    def test_breakeven_min_exceeds_alert_max(self):
        assert (
            effective_priority("move_sl_to_breakeven", 1)
            > effective_priority("alert_floki", 10)
        ), "move_sl_to_breakeven(1)=15 must beat alert_floki(10)=7"

    def test_doubling_rule_holds_across_all_categories(self):
        """Import-time invariant: base_{n+1} >= 2 * base_n.
        Calling the verifier must NOT raise."""
        _verify_doubling_rule()

    def test_doubling_sequence_is_strictly_decreasing_powers_of_two(self):
        for v in _DOUBLING_SEQUENCE:
            # Powers of 2
            assert v > 0 and (v & (v - 1)) == 0, f"{v} is not a power of 2"
        # Strictly decreasing
        assert list(_DOUBLING_SEQUENCE) == sorted(
            _DOUBLING_SEQUENCE, reverse=True
        )


# ---------------------------------------------------------------------------
# TestOverrideClipping
# ---------------------------------------------------------------------------

class TestOverrideClipping:
    """For small-base actions, the override boost saturates early.
    RFC §8.1 acknowledges this: `cancel_plan` has only 4 distinct override
    values (1, 2, 3, 4+) before the clip kicks in. This is by design
    (category ordering trumps override expressiveness)."""

    def test_adjust_sl_override_saturates_at_2(self):
        """adjust_sl base=16, base-1=15. override*10 >= 15 when override >= 2.
        So override=2 and override=10 both yield 16+15=31."""
        assert (
            effective_priority("adjust_sl", 2)
            == effective_priority("adjust_sl", 10)
            == 31
        )

    def test_cancel_plan_override_4_already_saturated(self):
        """cancel_plan base=32, base-1=31. override*10 >= 31 when override >= 4."""
        assert effective_priority("cancel_plan", 4) == 63
        assert effective_priority("cancel_plan", 10) == 63

    def test_close_full_override_is_fully_expressive(self):
        """close_full base=128, base-1=127. Every override 1..10 yields
        a distinct effective priority (10 * 10 = 100 <= 127)."""
        values = {effective_priority("close_full", o) for o in range(1, 11)}
        assert len(values) == 10, (
            f"close_full should have 10 distinct override values, got {len(values)}"
        )


# ---------------------------------------------------------------------------
# TestResolveOrdering
# ---------------------------------------------------------------------------

class TestResolveOrdering:
    def test_resolve_empty_returns_empty(self):
        assert resolve([]) == []

    def test_resolve_single_fire_returns_singleton(self):
        f = _fire()
        assert resolve([f]) == [f]

    def test_resolve_sorts_by_priority_descending(self):
        low = _fire(action_type="alert_floki", override=5, plan_id="PLAN-A")
        mid = _fire(action_type="adjust_sl", override=5, plan_id="PLAN-B")
        high = _fire(action_type="close_full", override=5, plan_id="PLAN-C")
        out = resolve([low, high, mid])
        assert [f.plan_id for f in out] == ["PLAN-C", "PLAN-B", "PLAN-A"]

    def test_resolve_does_not_mutate_input(self):
        inputs = [
            _fire(action_type="alert_floki", plan_id="PLAN-A"),
            _fire(action_type="close_full", plan_id="PLAN-B"),
        ]
        original = list(inputs)
        _ = resolve(inputs)
        assert inputs == original, "resolve() must not mutate its argument"

    def test_resolve_is_deterministic_across_calls(self):
        fires = [
            _fire(action_type="close_full", override=5, plan_id="PLAN-X"),
            _fire(action_type="close_full", override=5, plan_id="PLAN-Y"),
            _fire(action_type="adjust_sl", override=5, plan_id="PLAN-Z"),
        ]
        first = [f.plan_id for f in resolve(fires)]
        second = [f.plan_id for f in resolve(fires)]
        third = [f.plan_id for f in resolve(list(reversed(fires)))]
        assert first == second == third


# ---------------------------------------------------------------------------
# TestTieBreaks — RFC §8.3
# ---------------------------------------------------------------------------

class TestTieBreaks:
    def test_same_plan_earlier_list_position_wins(self):
        """Identical priority + same plan → lower plan_list_order wins."""
        early = _fire(plan_list_order=0, contingency_name="early")
        late = _fire(plan_list_order=5, contingency_name="late")
        out = resolve([late, early])
        assert [f.contingency_name for f in out] == ["early", "late"]

    def test_cross_plan_older_created_at_wins(self):
        """Identical priority + same list_order + different plans → older
        plan.created_at wins."""
        older = _fire(
            plan_id="PLAN-20260423-999",  # same-category id
            created_at="2026-04-23T08:00:00Z",
        )
        newer = _fire(
            plan_id="PLAN-20260424-001",
            created_at="2026-04-24T08:00:00Z",
        )
        out = resolve([newer, older])
        assert out[0].plan_id == older.plan_id

    def test_identical_createdat_lexicographic_planid_wins(self):
        """Identical priority + list_order + created_at → lexicographic
        plan_id fallback (deterministic)."""
        a = _fire(plan_id="PLAN-20260424-001")
        b = _fire(plan_id="PLAN-20260424-002")
        out = resolve([b, a])
        assert [f.plan_id for f in out] == [a.plan_id, b.plan_id]

    def test_triple_tie_chain(self):
        """When the full tie-break chain triggers, each rule must fire in
        the documented order."""
        # Same priority everywhere; list_order differs first.
        first = _fire(
            plan_id="PLAN-20260424-Z",       # worst lex fallback
            created_at="2026-04-25T00:00:00Z",  # newest
            plan_list_order=0,                 # earliest list → wins
        )
        loses_on_list = _fire(
            plan_id="PLAN-20260424-A",
            created_at="2026-04-23T00:00:00Z",
            plan_list_order=1,
        )
        out = resolve([loses_on_list, first])
        assert out[0] is first

    def test_higher_priority_always_beats_tiebreak(self):
        """Priority is the PRIMARY key. No amount of
        lower list_order / older created_at / earlier plan_id can rescue a
        low-priority fire."""
        loser = _fire(
            action_type="alert_floki", override=5,  # eff=7
            plan_id="PLAN-20260101-001",             # earliest plan_id
            created_at="2020-01-01T00:00:00Z",       # ancient
            plan_list_order=-999,                    # earliest list
        )
        winner = _fire(
            action_type="close_full", override=1,   # eff=138
            plan_id="PLAN-20260424-999",             # newest lex
            created_at="2099-12-31T23:59:59Z",       # future
            plan_list_order=9999,                    # latest list
        )
        out = resolve([loser, winner])
        assert out[0] is winner

    def test_category_boundary_never_crossed_by_override(self):
        """close_partial with max override still loses to close_full with
        min override — even if close_full loses every tie-break rule."""
        close_full_weakest = _fire(
            action_type="close_full", override=1,   # eff=138
            plan_id="PLAN-20260424-ZZZ",
            created_at="2099-01-01T00:00:00Z",
            plan_list_order=9999,
        )
        close_partial_strongest = _fire(
            action_type="close_partial", override=10,  # eff=127
            plan_id="PLAN-20200101-001",
            created_at="2020-01-01T00:00:00Z",
            plan_list_order=0,
        )
        out = resolve([close_partial_strongest, close_full_weakest])
        assert out[0] is close_full_weakest


# ---------------------------------------------------------------------------
# TestFireEvent
# ---------------------------------------------------------------------------

class TestFireEvent:
    def test_effective_priority_property_matches_function(self):
        f = _fire(action_type="close_full", override=7)
        assert f.effective_priority == effective_priority("close_full", 7)

    def test_payload_is_opaque_to_priority(self):
        """resolve() must not touch `payload`; two fires that differ ONLY
        in payload must sort identically regardless of payload content."""
        a = _fire(plan_id="PLAN-A", payload={"anything": [1, 2, 3]})
        b = _fire(plan_id="PLAN-A", payload=None)
        # Same sort key → stable order (input order preserved).
        assert resolve([a, b]) == [a, b]
        assert resolve([b, a]) == [b, a]

    def test_fire_event_has_documented_fields(self):
        f = _fire()
        for field in (
            "plan_id", "created_at", "contingency_name",
            "action_type", "override", "plan_list_order", "payload",
        ):
            assert hasattr(f, field), f"FireEvent missing field {field!r}"


# ---------------------------------------------------------------------------
# TestImportTimeInvariant
# ---------------------------------------------------------------------------

class TestImportTimeInvariant:
    def test_verify_doubling_rule_passes_for_current_table(self):
        """Current base table must pass the invariant check."""
        _verify_doubling_rule()  # must not raise

    def test_verify_doubling_rule_catches_violation(self, monkeypatch):
        """If a future edit drops the doubling rule, the verifier raises."""
        # Patch the module's sequence to simulate a broken table: 128, 64,
        # then 40 (which is < 2*32 — wait, 2*32=64, and 40 >= 64 is false).
        # Actually need a pair where high < 2*low. (64, 40) gives high=64,
        # low=40, 2*40=80, 64 < 80 → violation. Good.
        from snow import priority as pri
        broken = (128, 64, 40)
        monkeypatch.setattr(pri, "_DOUBLING_SEQUENCE", broken)
        with pytest.raises(AssertionError, match="doubling rule"):
            pri._verify_doubling_rule()
