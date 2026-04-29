"""FLO-404 follow-up — post-decision SLOT ACCOUNTING validator tests.

Pure-function tests for `floki_slot_accounting.check_reasoning` plus
write/consume cycle, render_reminder shape, and idempotency. The
validator is a feedback loop only — it never blocks decisions, so
these tests focus on:

  1. Correct slot-detection across ceiling cases (0..4 active plans).
  2. Persistence round-trip: write → consume → reminder block.
  3. Consume-deletes-state semantics so the reminder is one-shot per
     missing-accounting cycle.
  4. Defensive failure: malformed/missing inputs return safe defaults
     rather than raising (validator must never break the cycle).
"""
from __future__ import annotations

import json
import os

import pytest

import floki_slot_accounting as fsa


# =============================================================================
# Test fixtures — isolate the state file per test
# =============================================================================

@pytest.fixture(autouse=True)
def _isolate_state_path(tmp_path, monkeypatch):
    """Redirect the module-level _STATE_PATH to a tmp file so tests
    don't touch the real data/floki_slot_accounting_warning.json."""
    p = tmp_path / "warning.json"
    monkeypatch.setattr(fsa, "_STATE_PATH", str(p))
    yield


# =============================================================================
# check_reasoning — core slot detection
# =============================================================================

class TestCheckReasoning:
    def test_full_ceiling_no_check_required(self):
        """4 active plans → accounting not required → empty list."""
        assert fsa.check_reasoning("anything", 4) == []
        assert fsa.check_reasoning("", 4) == []

    def test_zero_active_all_three_slots_must_justify(self):
        """0 active plans → slots 1, 2, 3, 4 are all empty.
        Wait — when active=0, expected slots are 1..4, all four."""
        # NOTE: when active_plan_count=0, no plans submitted yet means
        # 4 slots are open and Floki must justify each. The prompt's
        # canonical example uses active=1 → slots 2,3,4. With active=0
        # the expected range is 1..4.
        missing = fsa.check_reasoning("", 0)
        assert missing == [1, 2, 3, 4]

    def test_one_active_three_slots_must_justify(self):
        """1 active plan → slots 2, 3, 4 must be justified."""
        # Empty reasoning → all three missing
        missing = fsa.check_reasoning("Plans active: 1/4. The market is bearish.", 1)
        assert missing == [2, 3, 4]

    def test_one_active_complete_accounting_passes(self):
        """All three slot lines present → no missing slots."""
        reasoning = (
            "Plans active: 1/4. "
            "Slot 2 empty: no countertrend BUY because M15 lacks reversal structure. "
            "Slot 3 empty: a second SELL needs higher resistance, none in range. "
            "Slot 4 empty: divergence-play setups require Echo signals not present."
        )
        assert fsa.check_reasoning(reasoning, 1) == []

    def test_one_active_partial_accounting_returns_gaps(self):
        """Only Slot 2 justified → 3 and 4 are missing."""
        reasoning = (
            "Plans active: 1/4. "
            "Slot 2 empty: no countertrend BUY because M15 lacks reversal structure."
        )
        missing = fsa.check_reasoning(reasoning, 1)
        assert missing == [3, 4]

    def test_two_active_slots_3_and_4_must_justify(self):
        """2 active plans → only slots 3 and 4 need justification."""
        reasoning = (
            "Plans active: 2/4. "
            "Slot 3 empty: scenario considered, no structural confluence. "
            "Slot 4 empty: divergence absent this cycle."
        )
        assert fsa.check_reasoning(reasoning, 2) == []

    def test_two_active_one_slot_missing(self):
        reasoning = (
            "Plans active: 2/4. "
            "Slot 3 empty: scenario considered, no structural confluence."
        )
        assert fsa.check_reasoning(reasoning, 2) == [4]

    def test_three_active_only_slot_4(self):
        """3 active plans → only slot 4."""
        assert fsa.check_reasoning("Slot 4 empty: ...", 3) == []
        assert fsa.check_reasoning("nothing useful", 3) == [4]

    def test_match_is_literal_substring_not_regex(self):
        """The validator does exact substring match, not loose regex.
        Variant phrasings ("Slot 2: empty", "slot 2 empty") don't count."""
        reasoning_loose = "slot 2 empty: lowercase. Slot 3 empty:: extra colon. Slot 4: missing colon."
        # "Slot 2 empty:" lowercase doesn't match (case-sensitive)
        # "Slot 3 empty:" — the ":: extra colon" still contains "Slot 3 empty:" as substring → matches
        # "Slot 4 empty:" missing entirely → 4 is in missing
        missing = fsa.check_reasoning(reasoning_loose, 1)
        assert 2 in missing  # lowercase didn't match
        assert 3 not in missing  # substring matched (we permit downstream extra colons)
        assert 4 in missing

    def test_active_count_non_int_returns_empty(self):
        """Defensive: non-int active_plan_count returns empty (no
        validation triggered) rather than raising."""
        assert fsa.check_reasoning("anything", "not_a_number") == []  # type: ignore[arg-type]
        assert fsa.check_reasoning("anything", None) == []  # type: ignore[arg-type]

    def test_none_reasoning_treated_as_empty(self):
        missing = fsa.check_reasoning(None, 1)  # type: ignore[arg-type]
        assert missing == [2, 3, 4]


# =============================================================================
# write_warning + consume_warning — persistence round-trip
# =============================================================================

class TestPersistenceRoundTrip:
    def test_write_then_consume_returns_payload(self):
        fsa.write_warning(1, [2, 3, 4])
        w = fsa.consume_warning()
        assert w is not None
        assert w["active_plan_count"] == 1
        assert w["missing_slots"] == [2, 3, 4]
        assert "ts" in w and w["ts"].endswith("Z")

    def test_consume_deletes_state_file(self):
        fsa.write_warning(1, [2, 3, 4])
        assert os.path.exists(fsa._STATE_PATH)
        fsa.consume_warning()
        assert not os.path.exists(fsa._STATE_PATH), (
            "consume_warning must delete the state file so the "
            "reminder is one-shot, not on every subsequent cycle"
        )

    def test_consume_when_no_state_returns_none(self):
        assert fsa.consume_warning() is None

    def test_double_consume_returns_none_second_time(self):
        fsa.write_warning(2, [3, 4])
        first = fsa.consume_warning()
        second = fsa.consume_warning()
        assert first is not None
        assert second is None

    def test_write_overwrites_prior_warning(self):
        """Idempotency — last writer wins."""
        fsa.write_warning(1, [2, 3, 4])
        fsa.write_warning(2, [3, 4])
        w = fsa.consume_warning()
        assert w["active_plan_count"] == 2
        assert w["missing_slots"] == [3, 4]

    def test_write_failure_silent(self, monkeypatch):
        """Persistence is feedback-loop, not critical path — write
        failures must not raise."""
        def _broken_replace(*a, **kw):
            raise OSError("disk full")
        monkeypatch.setattr(os, "replace", _broken_replace)
        # Must not raise.
        fsa.write_warning(1, [2, 3, 4])

    def test_consume_handles_corrupt_state_file(self):
        """Garbage JSON → consume returns None, doesn't crash."""
        os.makedirs(os.path.dirname(fsa._STATE_PATH) or ".", exist_ok=True)
        with open(fsa._STATE_PATH, "w") as f:
            f.write("{not valid json")
        assert fsa.consume_warning() is None


# =============================================================================
# render_reminder — block content + consume semantics
# =============================================================================

class TestRenderReminder:
    def test_no_warning_returns_empty_string(self):
        assert fsa.render_reminder() == ""

    def test_with_warning_contains_required_anchors(self):
        fsa.write_warning(1, [2, 3, 4])
        block = fsa.render_reminder()
        # Frame
        assert block.startswith("<reminder>")
        assert block.endswith("</reminder>")
        # Active count surfaced
        assert "1/4" in block
        # All missing slots listed by number
        assert "2, 3, 4" in block or ("2" in block and "3" in block and "4" in block)
        # Each slot has a literal "Slot N empty:" template line
        for n in (2, 3, 4):
            assert f"Slot {n} empty:" in block, (
                f"reminder block must include literal 'Slot {n} empty:' "
                f"template for the prompted format"
            )
        # The mandatory header
        assert "Plans active: N/4." in block

    def test_render_reminder_consumes_state(self):
        """Calling render_reminder once clears the warning — second
        call returns empty even though we just wrote one."""
        fsa.write_warning(1, [2, 3, 4])
        first = fsa.render_reminder()
        assert first  # non-empty
        second = fsa.render_reminder()
        assert second == "", (
            "render_reminder must consume state so the reminder is "
            "one-shot per missing-accounting cycle"
        )

    def test_partial_missing_renders_only_those_slots(self):
        """Active=2, missing=[4] → reminder mentions slot 4 only."""
        fsa.write_warning(2, [4])
        block = fsa.render_reminder()
        assert "Slot 4 empty:" in block
        # Slot 2 / 3 templates NOT included
        assert "Slot 2 empty:" not in block
        assert "Slot 3 empty:" not in block

    def test_empty_missing_list_renders_empty(self):
        """Edge: warning written with empty missing_slots → no reminder."""
        fsa.write_warning(1, [])
        assert fsa.render_reminder() == ""
