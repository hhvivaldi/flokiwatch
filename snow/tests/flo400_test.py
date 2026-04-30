"""FLO-400 — pre-validation JSON-string decoder for Gemini-stringified
nested fields. Contract-lock tests.

Background: Gemini's first-attempt failure mode on FLO-389 brain
comparison was JSON-stringifying four nested-object paths
(analysis.context_tags, entry.conditions[*], management[*], exit[*]).
First attempt validation_failed errors=4; retry self-corrected from the
error text but at the cost of one wasted cycle (~95s, 264k input tokens).

Fix: snow/validator.py adds `_decode_known_string_paths` as Step 0 of
`validate_plan`, decoding strings that parse as JSON to dicts at exactly
those four paths. Targeted (not recursive) so legit string fields
elsewhere (e.g. `analysis.thesis`) are never silently parsed.

Provider-agnostic: Qwen and Kimi don't emit strings at these paths, so
the decoder is a no-op for non-Gemini cycles. If Gemini ever stops
emitting strings, also a no-op — graceful obsolescence.

Tests cover:
  1. Helper purity: _try_decode_to_dict and _decode_known_string_paths
     are pure on their inputs.
  2. Roundtrip: the actual failed Gemini body from PLAN-005's cycle
     (verbatim) now validates.
  3. Provider-agnostic: a Qwen-shaped (already-dict) plan is unchanged.
  4. Targeted scope: a stringified value at an UNKNOWN path is left
     alone (not recursively decoded).
  5. Hostile inputs: malformed JSON / strings parsing to non-dicts
     pass through unchanged so Pydantic surfaces the original error.
"""
from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from snow.validator import (
    _decode_known_string_paths,
    _try_decode_to_dict,
    validate_plan,
)


# =============================================================================
# Pure helpers
# =============================================================================


class TestTryDecodeToDict:
    def test_passes_dict_unchanged(self):
        v = {"trend": "strong"}
        assert _try_decode_to_dict(v) is v

    def test_passes_non_string_non_dict_unchanged(self):
        assert _try_decode_to_dict(42) == 42
        assert _try_decode_to_dict([1, 2]) == [1, 2]
        assert _try_decode_to_dict(None) is None

    def test_decodes_json_string_to_dict(self):
        out = _try_decode_to_dict('{"trend": "strong"}')
        assert out == {"trend": "strong"}

    def test_returns_string_unchanged_on_invalid_json(self):
        v = "not actually json {"
        assert _try_decode_to_dict(v) == v

    def test_returns_string_unchanged_when_json_decodes_to_non_dict(self):
        # JSON arrays, scalars, booleans should NOT be promoted —
        # only dicts are valid replacements at the targeted paths.
        assert _try_decode_to_dict('[1, 2, 3]') == '[1, 2, 3]'
        assert _try_decode_to_dict('"hello"') == '"hello"'
        assert _try_decode_to_dict('42') == '42'
        assert _try_decode_to_dict('true') == 'true'
        assert _try_decode_to_dict('null') == 'null'


# =============================================================================
# Path-targeted decoder
# =============================================================================


class TestDecodeKnownStringPaths:
    def test_decodes_analysis_context_tags(self):
        d = {"analysis": {"context_tags": '{"trend": "strong"}'}}
        out = _decode_known_string_paths(d)
        assert out["analysis"]["context_tags"] == {"trend": "strong"}

    def test_decodes_entry_conditions_items(self):
        d = {"entry": {"conditions": [
            '{"type": "price_above", "level": 4589}',
            '{"type": "rsi", "tf": "H1", "op": "above", "threshold": 50}',
        ]}}
        out = _decode_known_string_paths(d)
        assert out["entry"]["conditions"][0] == {"type": "price_above", "level": 4589}
        assert out["entry"]["conditions"][1]["type"] == "rsi"

    def test_decodes_management_items(self):
        d = {"management": [
            '{"name": "lock_be", "priority": 7, "fires": "once"}'
        ]}
        out = _decode_known_string_paths(d)
        assert out["management"][0] == {"name": "lock_be", "priority": 7, "fires": "once"}

    def test_decodes_exit_items(self):
        d = {"exit": [
            '{"name": "target_hit", "priority": 5, "fires": "once"}'
        ]}
        out = _decode_known_string_paths(d)
        assert out["exit"][0] == {"name": "target_hit", "priority": 5, "fires": "once"}

    def test_does_not_mutate_input(self):
        """Caller state must be preserved — pure function contract."""
        d = {
            "analysis": {"context_tags": '{"trend": "strong"}'},
            "management": ['{"name": "x"}'],
        }
        snapshot = copy.deepcopy(d)
        _ = _decode_known_string_paths(d)
        assert d == snapshot

    def test_passthrough_when_already_dict(self):
        """Provider-agnostic: Qwen plans are dicts already; decoder is
        a no-op (returns equivalent dict)."""
        d = {
            "analysis": {"context_tags": {"trend": "strong"}},
            "entry": {"conditions": [{"type": "price_above", "level": 4589}]},
            "management": [{"name": "lock_be"}],
            "exit": [{"name": "target_hit"}],
        }
        out = _decode_known_string_paths(d)
        assert out["analysis"]["context_tags"] == {"trend": "strong"}
        assert out["entry"]["conditions"][0]["level"] == 4589
        assert out["management"][0]["name"] == "lock_be"
        assert out["exit"][0]["name"] == "target_hit"

    def test_does_not_decode_at_unknown_paths(self):
        """Targeted-not-recursive contract: a stringified value at a
        path NOT in the known list (e.g. analysis.thesis) must be left
        alone. This protects against silently parsing legitimate
        string fields that happen to be JSON-shaped."""
        d = {
            "analysis": {
                "thesis": '{"this": "is a string thesis"}',  # MUST stay string
                "confidence_reason": '{"key": "val"}',
            },
            "entry": {"reason_for_direct_action": '{"foo": "bar"}'},
        }
        out = _decode_known_string_paths(d)
        assert isinstance(out["analysis"]["thesis"], str)
        assert isinstance(out["analysis"]["confidence_reason"], str)
        assert isinstance(out["entry"]["reason_for_direct_action"], str)

    def test_handles_missing_paths_gracefully(self):
        """Plan dicts with some paths missing (partial input) shouldn't
        raise. The decoder visits each path defensively."""
        out = _decode_known_string_paths({})
        assert out == {}
        out = _decode_known_string_paths({"id": "PLAN-X"})
        assert out == {"id": "PLAN-X"}

    def test_handles_non_dict_input(self):
        """Defensive: caller passes a non-dict; pass through."""
        assert _decode_known_string_paths(None) is None  # type: ignore[arg-type]
        assert _decode_known_string_paths("not a dict") == "not a dict"  # type: ignore[arg-type]

    def test_malformed_json_strings_pass_through(self):
        """If a string at a known path doesn't parse as JSON, leave it
        — Pydantic will surface its native validation error on the
        original value, which is more informative than 'JSON decode
        failed'."""
        d = {"analysis": {"context_tags": "not actually json {"}}
        out = _decode_known_string_paths(d)
        assert out["analysis"]["context_tags"] == "not actually json {"

    def test_json_strings_decoding_to_non_dict_pass_through(self):
        """A string that parses as JSON to a list or scalar is NOT
        promoted — those wouldn't satisfy the schema either."""
        d = {"management": ["[1, 2]", '"a"', "42"]}
        out = _decode_known_string_paths(d)
        assert out["management"] == ["[1, 2]", '"a"', "42"]


# =============================================================================
# E2E roundtrip — actual failed Gemini body from PLAN-005's cycle
# =============================================================================


def _failed_gemini_body():
    """Verbatim reproduction of the failed first-attempt plan dict from
    the FLO-389 PLAN-005 cycle (data/_audits/flo395 tool_trace[11].input).

    Four paths were JSON-stringified:
      analysis.context_tags
      entry.conditions[0]
      entry.conditions[1]
      management[0]

    Plus an exit block added here (mandatory under FLO-401) — also as a
    string, to confirm the decoder handles all four leak points
    simultaneously."""
    return {
        "schema_version": 3,
        "id": "PLAN-20260429-100",
        "created_at": "2026-04-29T07:35:47Z",
        "expires_at": "2026-04-29T12:00:00Z",
        "analysis": {
            "thesis": "H1 pullback to 4589 resistance to join the strong downtrend",
            "context_tags": (
                '{"trend": "trend_strong", "volatility": "high_vol", '
                '"htf": "HTF_aligned", "news_session": []}'
            ),
            "regime_assumed": "TRENDING_BEARISH",
            "confidence": 80,
            "confidence_reason": "Full bearish EMA alignment across H4/H1/M15.",
            "key_levels": [4603, 4589, 4576, 4543],
            "setup_type": "pullback_trend",
        },
        "management": [
            '{"name": "lock_be_at_100_pips", "priority": 7, '
            '"conditions": [{"type": "mfe_reached", "pips": 100}], '
            '"action": {"type": "move_sl_to_breakeven", "offset_pips": 0}, '
            '"fires": "once"}'
        ],
        "entry": {
            "conditions": [
                '{"type": "price_above", "level": 4589.0}',
                # FLO-404 follow-up (CEO directive 2026-04-30): the
                # verbatim production body had `period: 21` with
                # aligned_bear; under the new cross-field rule this
                # is rejected. Updated to omit period (canonical
                # regime-gate shape). Preserves FLO-400's JSON-string
                # decoder test surface — the decoder still has to
                # unwrap this string into a dict.
                '{"type": "ema_relation", "tf": "H1", '
                '"relation": "aligned_bear"}',
            ],
            "initial_sl": 4605,
            "initial_tp": 4559,
            "direction": "SELL",
            "entry_price": 4589,
            "volume": 0.02,
        },
        "exit": [
            '{"name": "rsi_break", "priority": 5, '
            '"conditions": [{"type": "rsi", "tf": "H1", "op": "above", '
            '"threshold": 50}], '
            '"action": {"type": "close_full"}, "fires": "once"}'
        ],
        "emergency": {
            "max_loss_pips": 200,
            "max_duration_minutes": 480,
            "on_broker_error": "alert_floki",
        },
    }


class TestE2ERoundtrip:
    def test_failed_gemini_body_now_validates(self):
        """The four-string-leak body that produced validation_failed
        errors=4 in production now validates without retry."""
        ok, plan, errors = validate_plan(_failed_gemini_body())
        assert ok is True, f"unexpected errors: {errors}"
        assert plan is not None
        # Verify the decoded fields landed correctly.
        assert plan.analysis.context_tags.trend == "trend_strong"
        assert plan.entry.conditions[0].type == "price_above"
        assert plan.entry.conditions[1].type == "ema_relation"
        assert plan.management[0].name == "lock_be_at_100_pips"
        assert plan.exit[0].name == "rsi_break"

    def test_validation_still_rejects_genuine_schema_errors(self):
        """The decoder must NOT mask real validation errors. Submit a
        body where an entry condition has `type=invalid_primitive` —
        validation should still fail with the descriptive Pydantic
        error, not be silently massaged into something else."""
        body = _failed_gemini_body()
        body["entry"]["conditions"] = [
            '{"type": "invalid_primitive", "level": 4589.0}'
        ]
        ok, plan, errors = validate_plan(body)
        assert ok is False
        assert plan is None
        assert any("entry" in e.lower() and "conditions" in e.lower() for e in errors)

    def test_qwen_shaped_plan_unaffected(self):
        """Provider-agnostic guarantee: a fully-dict-shaped plan
        (Qwen baseline pattern) round-trips identically with or without
        the decoder. Use the conftest fixture."""
        from snow.tests.conftest import _BASE_PLAN
        body = copy.deepcopy(_BASE_PLAN)
        ok, plan, errors = validate_plan(body)
        assert ok is True, f"unexpected errors: {errors}"
        assert plan.analysis.context_tags.trend is not None
        assert len(plan.entry.conditions) >= 1


class TestNonRecursiveScope:
    """The decoder is targeted — it does NOT walk arbitrary nested
    string fields. This locks the boundary against a future
    'while-we're-at-it' refactor that adds recursion."""

    def test_does_not_decode_string_inside_inner_action(self):
        """A management contingency's nested action is reached via
        the contingency dict; if the contingency itself was a string,
        the decoder unwraps it. But once it's a dict, nested string
        fields inside (e.g. action) are NOT further decoded — that's
        Pydantic's job."""
        # Contingency-as-string with nested action-as-string would
        # require recursive decoding to fix; we explicitly DON'T do
        # that. This test documents the boundary.
        d = {"management": [{
            "name": "x",
            "priority": 5,
            "fires": "once",
            "conditions": [{"type": "mfe_reached", "pips": 50}],
            # action as a string — decoder leaves alone, Pydantic surfaces.
            "action": '{"type": "move_sl_to_breakeven", "offset_pips": 0}',
        }]}
        out = _decode_known_string_paths(d)
        # action remains string (boundary lock).
        assert isinstance(out["management"][0]["action"], str)
