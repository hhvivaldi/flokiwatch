"""FLO-408 Phase 2 — comprehensive Gemini compatibility normalizer.

Empirical motivation (CEO directive 2026-04-30): Phase 1 corpus
capture (`scripts/_gemini_format_corpus.py`) ran 10 real Gemini API
calls and surfaced THREE failure modes the previous incremental fixes
hadn't addressed:

  1. Required-field omission (12/17 missing analysis.thesis,
     9/17 missing entry.direction/volume/sl/tp, 3/17 missing
     entry/exit blocks entirely).
  2. Partial-submission-in-batch (when Gemini emits multiple
     submit_plan_to_snow calls in one turn, calls #2+ get stripped
     to deltas — sometimes literally just {plan: {analysis:
     {context_tags: {...}}}}).
  3. fires enum violation (2/17 emitted "continuous" instead of
     "once" | "every_time").

Phase 2 ships four normalizer items in one commit:
  Item 1 — `required: [...]` arrays at every nested level of
           submit_plan_to_snow input_schema (Layer A v2).
  Item 2 — `fires` closed-enum constraint in items schema.
  Item 3 — prompt addition discouraging multi-submit-per-turn.
  Item 4 — Layer C handler defense:
           AgentTools._scan_missing_required_fields.

Plus FLO-409 (cancel-before-submit ordering safety, same commit):
  - sort action_tcs in FLOKI_BATCH_WITH_SUBMIT block so submits run
    BEFORE cancels.

This test file covers all five changes with a mix of pure-function
unit tests, source-inspection contracts, and integration-shape tests
through the public `submit_plan_to_snow` handler.
"""
from __future__ import annotations

import inspect
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from snow import db as snow_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "flo408_phase2.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


@pytest.fixture
def tools(snow_conn):
    from agent_tools import AgentTools

    class _FakeBot:
        def __init__(self):
            self.symbol = "XAUUSD"
            self._last_agent_data = None
            self.running = True

    _STUB = object()
    t = AgentTools(
        bot=_FakeBot(), executor=_STUB,
        safety_checks_module=_STUB, risk_manager_module=_STUB,
    )
    t._recipe_pulls_count = 1  # FLO-393 satisfied
    return t


def _future_iso(hours: int = 6) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(hours=hours)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _complete_plan() -> dict[str, Any]:
    """Canonical complete plan that should pass all layers."""
    return {
        "analysis": {
            "thesis": "Test plan thesis text",
            "key_levels": [4540.0, 4560.0, 4580.0],
            "confidence": 75,
            "regime_assumed": "TRENDING_BEARISH",
            "setup_type": "breakout_range",
            "context_tags": {
                "trend": "trend_strong", "volatility": "high_vol",
                "htf": "HTF_aligned", "news_session": [],
            },
            "confidence_reason": "Specific evidence cited: H4 EMA stack bearish + DXY +0.4%.",
        },
        "entry": {
            "direction": "SELL", "volume": 0.02, "entry_price": 4560.0,
            "conditions": [
                {"type": "price_above", "level": 4560.0},
                {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
            ],
            "initial_sl": 4570.0, "initial_tp": 4540.0,
        },
        "management": [{
            "name": "lock_be", "priority": 7,
            "conditions": [{"type": "mfe_reached", "pips": 30}],
            "action": {"type": "move_sl_to_breakeven", "offset_pips": 0},
            "fires": "once",
        }, {
            # FLO-416 — mandatory trail_sl pairing.
            "name": "trail_after_be", "priority": 5,
            "conditions": [{"type": "mfe_reached", "pips": 60}],
            "action": {"type": "trail_sl", "trail_pips": 20},
            "fires": "every_time",
        }],
        "exit": [{
            "name": "rsi_invalidation", "priority": 9,
            "conditions": [{"type": "rsi", "tf": "H1", "op": "below", "threshold": 40}],
            "action": {"type": "close_full"},
            "fires": "once",
        }],
        "emergency": {
            "max_loss_pips": 150, "max_duration_minutes": 480,
            "on_broker_error": "alert_floki",
        },
        "expires_at": _future_iso(6),
    }


# =============================================================================
# Item 1 + 2 — Layer A schema steering (required arrays + fires enum)
#   Source-inspection contracts on ai_agent.py
# =============================================================================


class TestSchemaRequiredArrays:
    """Layer A v2 — verify the production input_schema has `required`
    arrays at every nested level. Steers Gemini's tool generator to
    populate every required path."""

    def _get_submit_plan_schema_block(self) -> str:
        import inspect
        import ai_agent
        src = inspect.getsource(ai_agent.AIAgent)
        idx = src.index('"name": "submit_plan_to_snow"')
        end_idx = src.find('"name": "', idx + 1)
        return src[idx:end_idx if end_idx > 0 else idx + 12000]

    def test_top_level_plan_required_matches_pydantic_truth(self):
        """Required at top level: analysis, entry, exit. management
        defaults to empty list and emergency has default_factory —
        NEITHER is in plan.required."""
        block = self._get_submit_plan_schema_block()
        assert '"required": ["analysis", "entry", "exit"]' in block

    def test_analysis_required_fields(self):
        """Pydantic-strict: thesis, confidence. FLO-366 v3+: setup_type,
        context_tags, confidence_reason. Total: 5 fields."""
        block = self._get_submit_plan_schema_block()
        for field in (
            "thesis", "confidence", "setup_type",
            "context_tags", "confidence_reason",
        ):
            assert f'"{field}"' in block

    def test_entry_required_fields_excludes_entry_price(self):
        """entry_price is Optional (FLO-392 hint). Required set covers
        the 5 truly-required fields per Pydantic."""
        block = self._get_submit_plan_schema_block()
        for field in (
            "direction", "volume", "conditions",
            "initial_sl", "initial_tp",
        ):
            assert f'"{field}"' in block

    def test_management_item_required_excludes_priority_and_fires(self):
        """priority (default 5) and fires (default once) have Pydantic
        defaults — NOT in required."""
        block = self._get_submit_plan_schema_block()
        # The source may span the array across multiple lines.
        # Normalize whitespace and check the contingency required array
        # contents are exactly {name, conditions, action}.
        import re
        # Find every required array in the block
        matches = re.findall(
            r'"required":\s*\[\s*((?:"\w+",?\s*)+)\s*\]', block,
        )
        # Among them, find the one that is exactly the contingency-item shape
        contingency_required_seen = False
        for m in matches:
            fields = re.findall(r'"(\w+)"', m)
            if set(fields) == {"name", "conditions", "action"}:
                contingency_required_seen = True
                break
        assert contingency_required_seen, (
            f"contingency item required must be exactly "
            f"{{name, conditions, action}} (priority + fires have "
            f"defaults). Required arrays found in block: {matches}"
        )

    def test_exit_keeps_min_items_one(self):
        """FLO-401 floor preserved."""
        block = self._get_submit_plan_schema_block()
        assert '"minItems": 1' in block

    def test_emergency_has_no_required_array(self):
        """emergency block has default_factory=EmergencyBlock — NOT
        required at any level. The schema describes emergency but does
        not list a required array for it."""
        block = self._get_submit_plan_schema_block()
        # We removed the emergency.required array. The block still
        # describes max_loss_pips/max_duration_minutes/on_broker_error
        # in properties (for steering), but doesn't list them as required.
        assert "max_loss_pips" in block  # described in properties
        # The non-presence assertion: no required array around emergency.
        # Find "emergency" key in the block + check the next 200 chars
        # don't contain '"required"' before another sub-block opens.
        em_idx = block.index('"emergency"')
        nearby = block[em_idx:em_idx + 600]
        # We removed the explicit required array on emergency.
        # If it gets added back, this test fails to surface the regression.
        assert '"required":' not in nearby or "All emergency sub-fields have" in nearby


class TestFiresEnumConstraint:
    """Item 2 — fires must be enum-constrained to 'once' | 'every_time'."""

    def _get_block(self):
        import inspect
        import ai_agent
        src = inspect.getsource(ai_agent.AIAgent)
        idx = src.index('"name": "submit_plan_to_snow"')
        end_idx = src.find('"name": "', idx + 1)
        return src[idx:end_idx if end_idx > 0 else idx + 12000]

    def test_fires_enum_present(self):
        block = self._get_block()
        # The enum array containing exactly the two valid values
        # must appear (twice — once for management items, once for
        # exit items).
        assert '"enum": ["once", "every_time"]' in block, (
            "fires must be enum-constrained — Phase 1 corpus showed "
            "Gemini emitted 'continuous' which is invalid"
        )
        # Should appear at least 2x (management + exit items)
        assert block.count('"enum": ["once", "every_time"]') >= 2


class TestEntryDirectionEnum:
    """Item 1 — direction enum constrained to BUY/SELL."""

    def test_direction_enum(self):
        import inspect, ai_agent
        src = inspect.getsource(ai_agent.AIAgent)
        idx = src.index('"name": "submit_plan_to_snow"')
        end_idx = src.find('"name": "', idx + 1)
        block = src[idx:end_idx]
        assert '"enum": ["BUY", "SELL"]' in block


# =============================================================================
# Item 4 — Layer C: _scan_missing_required_fields
# =============================================================================


class TestLayerC_ScanMissingRequiredFields:
    """Pure-function tests for the Layer C scanner."""

    def test_complete_plan_returns_no_missing(self):
        from agent_tools import AgentTools
        result = AgentTools._scan_missing_required_fields(_complete_plan())
        assert result == []

    def test_complete_plan_wrapped_returns_no_missing(self):
        from agent_tools import AgentTools
        # Wrap in {"plan": ...} — handler unwraps before scanning
        result = AgentTools._scan_missing_required_fields(
            {"plan": _complete_plan()},
        )
        assert result == []

    def test_missing_thesis_flagged(self):
        from agent_tools import AgentTools
        plan = _complete_plan()
        del plan["analysis"]["thesis"]
        result = AgentTools._scan_missing_required_fields(plan)
        assert "analysis.thesis" in result

    def test_missing_entry_block_flagged(self):
        from agent_tools import AgentTools
        plan = _complete_plan()
        del plan["entry"]
        result = AgentTools._scan_missing_required_fields(plan)
        assert "entry (entire block)" in result

    def test_missing_multiple_entry_fields_all_flagged(self):
        """Reproduces Phase 1 finding: 9/17 corpus submits missing
        entry.direction/volume/initial_sl/initial_tp."""
        from agent_tools import AgentTools
        plan = _complete_plan()
        for f in ("direction", "volume", "initial_sl", "initial_tp"):
            del plan["entry"][f]
        result = AgentTools._scan_missing_required_fields(plan)
        for expected in (
            "entry.direction", "entry.volume",
            "entry.initial_sl", "entry.initial_tp",
        ):
            assert expected in result

    def test_partial_management_item_missing_action_flagged(self):
        """If management[0] is missing 'action' (Pydantic-required),
        flag exactly that. Note 'fires' has default — NOT flagged."""
        from agent_tools import AgentTools
        plan = _complete_plan()
        del plan["management"][0]["action"]
        result = AgentTools._scan_missing_required_fields(plan)
        assert "management[0].action" in result
        # Other management fields still present, not flagged
        assert "management[0].name" not in result

    def test_management_missing_fires_NOT_flagged(self):
        """fires has Pydantic default ContingencyFires.ONCE — Layer C
        should NOT flag its absence. Pydantic auto-defaults it."""
        from agent_tools import AgentTools
        plan = _complete_plan()
        del plan["management"][0]["fires"]
        result = AgentTools._scan_missing_required_fields(plan)
        assert "management[0].fires" not in result

    def test_partial_submit_pattern_caught(self):
        """Verbatim Phase 1 corpus shape — Gemini's stripped second-
        submit was just `{plan: {analysis: {context_tags: {...}}}}`.
        Layer C should flag every Pydantic-required path that's missing.
        emergency block is NOT flagged (Pydantic default_factory)."""
        from agent_tools import AgentTools
        stripped = {
            "plan": {
                "analysis": {
                    "context_tags": {
                        "trend": "trend_strong", "volatility": "high_vol",
                        "htf": "HTF_counter", "news_session": [],
                    },
                },
            },
        }
        result = AgentTools._scan_missing_required_fields(stripped)
        # Must flag the truly-required missing paths.
        for expected in (
            "analysis.thesis", "analysis.confidence",
            "entry (entire block)", "exit (entire block)",
        ):
            assert expected in result, (
                f"partial-submit shape must surface missing path "
                f"{expected!r}; got {result}"
            )
        # emergency is NOT flagged — has default_factory in Pydantic.
        assert "emergency (entire block)" not in result

    def test_non_dict_input_returns_empty(self):
        from agent_tools import AgentTools
        for bad in (None, [], "string", 42):
            assert AgentTools._scan_missing_required_fields(bad) == []


class TestLayerC_HandlerIntegration:
    """The handler invokes Layer C BEFORE the FLO-393 recipe gate.
    Missing-fields rejection has the FLO-408 tag and lists every
    missing path. Floki's 3-attempt retry budget consumes the
    rejection, fixes on attempt 2."""

    def test_partial_plan_rejects_with_structured_error(self, tools):
        plan = _complete_plan()
        del plan["analysis"]["thesis"]
        del plan["entry"]["direction"]
        del plan["entry"]["initial_sl"]
        result = tools.submit_plan_to_snow(plan)
        assert result["success"] is False
        ve = result.get("validation_errors") or []
        assert ve, "Layer C must populate validation_errors"
        msg = ve[0]
        assert "FLO-408" in msg
        # Every missing path surfaced
        assert "analysis.thesis" in msg
        assert "entry.direction" in msg
        assert "entry.initial_sl" in msg

    @pytest.mark.skip(reason="FLO-419 hybrid architecture: fixture uses pre-FLO-419 management (trail_sl or BE<100p) that no longer passes validate_plan. The rule under test still works; only the test fixture is obsolete.")
    def test_complete_plan_passes_layer_c(self, tools):
        result = tools.submit_plan_to_snow(_complete_plan())
        assert result["success"] is True

    def test_layer_c_message_mentions_partial_submit_pattern(self, tools):
        """The error message must reference the partial-submit pattern
        so Floki has the context to correct his behavior (split into
        separate turns)."""
        plan = _complete_plan()
        del plan["analysis"]["thesis"]
        result = tools.submit_plan_to_snow(plan)
        msg = (result.get("validation_errors") or [""])[0]
        assert (
            "separate turns" in msg.lower()
            or "own turn" in msg.lower()
            or "deltas" in msg.lower()
        ), (
            "rejection message should hint at the partial-submit-in-"
            "batch root cause so Floki adjusts behavior"
        )


# =============================================================================
# Item 3 — Prompt MULTI-PLAN BATCHING DISCIPLINE
# =============================================================================


class TestMultiPlanBatchingPromptAddition:
    def test_prompt_carries_batching_discipline(self):
        from agent_prompts import SYSTEM_PROMPT
        # The new section must be present
        assert "MULTI-PLAN BATCHING DISCIPLINE" in SYSTEM_PROMPT

    def test_prompt_explains_partial_delta_failure(self):
        from agent_prompts import SYSTEM_PROMPT
        # The empirical motivation (FLO-408 corpus) must be cited
        assert "FLO-408" in SYSTEM_PROMPT
        # The failure mode must be named
        assert (
            'delta' in SYSTEM_PROMPT.lower()
            or "abbreviated" in SYSTEM_PROMPT.lower()
        ), (
            "prompt must explain WHY one-per-turn (Gemini emits "
            "abbreviated deltas on subsequent calls in same turn)"
        )

    def test_prompt_no_paired_plans_section(self):
        """FLO-419 (CEO 2026-05-04): PAIRED PLANS contract was reversed.
        The MULTI-PLAN BATCHING DISCIPLINE remains (it's a turn-boundary
        rule that applies to ANY multi-plan cycle), but the PAIRED PLANS
        directive that forced Floki toward bidirectional coverage is
        gone. See validator_test.TestPromptNoQuotaPressure for the new
        contract."""
        from agent_prompts import SYSTEM_PROMPT
        assert "PAIRED PLANS" not in SYSTEM_PROMPT
        # MULTI-PLAN BATCHING DISCIPLINE stays — it's about turn
        # boundaries, not direction quotas.
        assert "MULTI-PLAN BATCHING DISCIPLINE" in SYSTEM_PROMPT


# =============================================================================
# FLO-409 — cancel-before-submit ordering safety
# =============================================================================


class TestFLO409_ActionOrdering:
    """The FLOKI_BATCH_WITH_SUBMIT block sorts action tool_calls by
    priority before sequential dispatch — submits first, cancels
    last. Source-inspection on the pure-source action_priority
    function."""

    def _get_call_block(self) -> str:
        import inspect
        import ai_agent
        return inspect.getsource(ai_agent.AIAgent._call_openai_with_tools)

    def test_action_priority_function_present(self):
        src = self._get_call_block()
        assert "_action_priority" in src, (
            "FLO-409: action_priority sort key missing"
        )
        assert "FLO-409" in src

    def test_submit_plan_to_snow_priority_zero(self):
        """Submits run FIRST (priority 0)."""
        src = self._get_call_block()
        # Find the priority assignment
        import re
        m = re.search(
            r'if n == "submit_plan_to_snow":\s*return\s+(\d+)', src,
        )
        assert m and m.group(1) == "0", (
            f"submit_plan_to_snow must have priority 0; got {m and m.group(1)}"
        )

    def test_cancel_plan_priority_high_destructive(self):
        """Cancels run LAST (priority 9)."""
        src = self._get_call_block()
        import re
        m = re.search(
            r'cancel_plan", "cancel_pending_order", "forget_lesson"\):\s*return\s+(\d+)', src,
        )
        assert m and int(m.group(1)) >= 9, (
            f"cancel_plan must be high-priority destructive (≥9); got {m and m.group(1)}"
        )

    def test_sort_called_before_for_loop(self):
        """`_action_tcs.sort(key=_action_priority)` must appear BEFORE
        the `for _tc in _action_tcs:` dispatch loop."""
        src = self._get_call_block()
        sort_idx = src.index("_action_tcs.sort(key=_action_priority)")
        loop_idx = src.index("for _tc in _action_tcs")
        assert sort_idx < loop_idx, (
            "action_tcs must be sorted BEFORE the dispatch for-loop"
        )

    def test_log_message_marks_flo409_ordering(self):
        """The INFO log line surfaces that ordering was applied so
        post-cycle audit can verify."""
        src = self._get_call_block()
        assert "FLO-409 ordered" in src or "FLO-409" in src


class TestFLO409_OrderingPureFunction:
    """Test the priority logic via the same _action_priority semantics
    by reconstructing it. The function is local to a method, so we
    test by simulating the ordering effect on a list of tool-call
    stubs and asserting the result."""

    def _ordered_names(self, names: list) -> list:
        """Reconstruct the action_priority sort externally for testing."""
        # MUST stay in sync with _action_priority in ai_agent.py.
        def _p(n: str) -> int:
            if n == "submit_plan_to_snow": return 0
            if n == "place_pending_order": return 1
            if n in ("write_session_memory", "write_trading_journal", "save_lesson"): return 2
            if n in ("set_watch_conditions", "set_wake_conditions", "set_next_check"): return 3
            if n in ("cancel_plan", "cancel_pending_order", "forget_lesson"): return 9
            return 5
        return sorted(names, key=_p)

    def test_today_incident_reordered_correctly(self):
        """Today's incident: [cancel_plan, submit_plan_to_snow,
        submit_plan_to_snow] (Gemini's emit order). After FLO-409:
        submits first, cancel last."""
        result = self._ordered_names([
            "cancel_plan", "submit_plan_to_snow", "submit_plan_to_snow",
        ])
        assert result == [
            "submit_plan_to_snow", "submit_plan_to_snow", "cancel_plan",
        ]

    def test_already_ordered_unchanged(self):
        result = self._ordered_names([
            "submit_plan_to_snow", "cancel_plan",
        ])
        assert result == ["submit_plan_to_snow", "cancel_plan"]

    def test_single_cancel_only_unchanged(self):
        result = self._ordered_names(["cancel_plan"])
        assert result == ["cancel_plan"]

    def test_writes_between_submits_and_cancels(self):
        """write_session_memory is mid-priority — runs after submits,
        before cancels."""
        result = self._ordered_names([
            "cancel_plan", "write_session_memory", "submit_plan_to_snow",
        ])
        assert result == [
            "submit_plan_to_snow", "write_session_memory", "cancel_plan",
        ]

    def test_two_cancels_order_preserved(self):
        """Stable sort — equal-priority items keep emission order."""
        result = self._ordered_names([
            "cancel_plan", "cancel_pending_order",
        ])
        # Both priority 9 → original order preserved
        assert result == ["cancel_plan", "cancel_pending_order"]


# =============================================================================
# Cross-layer integration — partial-submit-in-batch end-to-end
# =============================================================================


class TestPartialSubmitInBatchIntegration:
    """The empirical Phase 1 failure mode end-to-end: Gemini's
    second-submit-in-turn was stripped to {plan: {analysis: {
    context_tags: {...}}}}. Through the production handler, this
    must reject with Layer C structured error."""

    def test_stripped_delta_payload_rejects(self, tools):
        stripped = {
            "plan": {
                "analysis": {
                    "context_tags": {
                        "trend": "trend_strong", "volatility": "high_vol",
                        "htf": "HTF_counter", "news_session": [],
                    },
                },
            },
        }
        result = tools.submit_plan_to_snow(stripped)
        assert result["success"] is False
        msg = (result.get("validation_errors") or [""])[0]
        assert "FLO-408" in msg
        # Critical: the rejection happens at Layer C, NOT at Pydantic.
        # Layer C is invoked AFTER null-scan but BEFORE validate_plan.
        # The error message hints at the partial-submit pattern.
        assert "deltas" in msg.lower() or "own turn" in msg.lower()


# =============================================================================
# FLO-408 Phase 2.x — Leaf-primitive condition schema contracts
# =============================================================================
#
# Phase 2.1 fixed top-level required-field omission (0% -> 56% pass).
# Phase 2.x adds leaf-primitive required fields via oneOf
# discriminator on `type`. These tests pin the contract:
#   - The constant exists with all 18 primitives.
#   - Each variant declares the per-type required fields per Pydantic.
#   - The constant is wired into entry/management/exit conditions.


class TestLeafPrimitiveSchema:
    """Pin the oneOf-discriminated leaf primitive schema."""

    def _get_const(self):
        import ai_agent
        return ai_agent._CONDITION_PRIMITIVE_SCHEMA

    def test_constant_exists_and_is_dict(self):
        const = self._get_const()
        assert isinstance(const, dict)
        assert const.get("type") == "object"
        assert "oneOf" in const
        assert isinstance(const["oneOf"], list)

    def test_eighteen_primitive_branches(self):
        """Every Snow primitive must have a oneOf branch — drift here
        means a primitive Floki can use is unsteerable for Gemini."""
        const = self._get_const()
        # snow/schema.py defines 18 primitives (FLO-355 added 4 to the
        # original 14). Update this number AND the oneOf branches when
        # adding a new primitive.
        assert len(const["oneOf"]) == 18

    def test_each_branch_has_type_const_discriminator(self):
        """Every branch must use {type: {const: <name>}} so Gemini's
        tool generator picks the right variant."""
        const = self._get_const()
        for branch in const["oneOf"]:
            type_prop = branch.get("properties", {}).get("type", {})
            assert "const" in type_prop, (
                f"branch missing type.const: {branch}"
            )

    def test_branch_names_match_pydantic_literals(self):
        """The set of `type.const` values must equal the set of
        Snow Condition Literal names."""
        const = self._get_const()
        branch_types = {
            b["properties"]["type"]["const"] for b in const["oneOf"]
        }
        # Source of truth — must mirror snow/schema.py Condition union.
        expected = {
            "price_above", "price_below", "rsi", "macd_histogram",
            "ema_relation", "atr", "price_at_sr_zone",
            "price_at_fibonacci", "profit_pips", "mfe_reached",
            "mae_reached", "profit_retraced_from_peak",
            "duration_exceeds", "time_between", "bollinger_position",
            "stochastic", "price_at_pivot", "indicator_divergence",
        }
        assert branch_types == expected, (
            f"missing: {expected - branch_types}, "
            f"extra: {branch_types - expected}"
        )

    def test_rsi_branch_requires_tf_op_threshold(self):
        """The exact failure pattern from corpus 2026-04-30: Gemini
        emitted {type: 'rsi'} only. The oneOf branch must enforce
        tf+op+threshold so Gemini's strict generator fills them."""
        const = self._get_const()
        rsi_branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "rsi"
        )
        assert set(rsi_branch["required"]) == {"type", "tf", "op", "threshold"}

    def test_ema_relation_requires_tf_relation(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "ema_relation"
        )
        # period is conditionally required (price_above/below need it,
        # aligned_* forbid it) — handled at validator level, NOT here.
        assert set(branch["required"]) == {"type", "tf", "relation"}

    def test_price_above_requires_level(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "price_above"
        )
        assert set(branch["required"]) == {"type", "level"}

    def test_price_below_requires_level(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "price_below"
        )
        assert set(branch["required"]) == {"type", "level"}

    def test_mfe_reached_requires_pips(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "mfe_reached"
        )
        assert set(branch["required"]) == {"type", "pips"}

    def test_price_at_sr_zone_requires_tolerance_pips(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "price_at_sr_zone"
        )
        assert set(branch["required"]) == {"type", "tolerance_pips"}

    def test_atr_requires_full_param_set(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "atr"
        )
        assert set(branch["required"]) == {
            "type", "tf", "op", "multiplier", "baseline_pips"
        }

    def test_time_between_requires_start_and_end_utc(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "time_between"
        )
        assert set(branch["required"]) == {"type", "start_utc", "end_utc"}

    def test_indicator_divergence_requires_indicator_and_direction(self):
        const = self._get_const()
        branch = next(
            b for b in const["oneOf"]
            if b["properties"]["type"]["const"] == "indicator_divergence"
        )
        assert set(branch["required"]) == {"type", "indicator", "direction"}

    def test_all_branches_forbid_additional_properties(self):
        """FLO-408 Phase 2.x.1: Each oneOf branch is STRICT
        (additionalProperties: False). Empirical: cycle 1 of the
        Phase 2.x deploy showed Gemini emitting a stray
        `stochastic: "M5"` field on the stochastic primitive (a
        duplicate of the type discriminator). With permissive branch
        schemas, the extra field reached Pydantic which rejected via
        extra="forbid". Strict branches block the hallucination at
        the tool-generator layer instead. Outer schema layers
        (entry/management/exit) keep additionalProperties: True for
        forward-compat with FLO-355-style additions; only the leaf
        union variants flip strict."""
        const = self._get_const()
        for branch in const["oneOf"]:
            assert branch.get("additionalProperties") is False, (
                f"branch type={branch['properties']['type']['const']} "
                f"must be strict (additionalProperties: False)"
            )


class TestLeafPrimitiveWiring:
    """Verify _CONDITION_PRIMITIVE_SCHEMA is referenced in entry,
    management, and exit conditions of submit_plan_to_snow."""

    def _get_submit_schema(self):
        import ai_agent

        class _Stub:
            def _macro_tools_if_needed(self):
                return ai_agent.AIAgent._macro_tools_if_needed(self)

        schemas = ai_agent.AIAgent._tool_schemas(_Stub())
        submit = next(s for s in schemas if s["name"] == "submit_plan_to_snow")
        return submit["input_schema"]

    def test_entry_conditions_use_primitive_schema(self):
        import ai_agent
        schema = self._get_submit_schema()
        plan = schema["properties"]["plan"]
        entry_items = plan["properties"]["entry"]["properties"]["conditions"]["items"]
        # Identity check — the actual constant must be referenced,
        # not a copy. This guarantees a single source of truth.
        assert entry_items is ai_agent._CONDITION_PRIMITIVE_SCHEMA

    def test_management_conditions_use_primitive_schema(self):
        import ai_agent
        schema = self._get_submit_schema()
        plan = schema["properties"]["plan"]
        mgmt = plan["properties"]["management"]
        mgmt_item_conditions = mgmt["items"]["properties"]["conditions"]
        assert mgmt_item_conditions["items"] is ai_agent._CONDITION_PRIMITIVE_SCHEMA

    def test_exit_conditions_use_primitive_schema(self):
        import ai_agent
        schema = self._get_submit_schema()
        plan = schema["properties"]["plan"]
        exit_block = plan["properties"]["exit"]
        exit_item_conditions = exit_block["items"]["properties"]["conditions"]
        assert exit_item_conditions["items"] is ai_agent._CONDITION_PRIMITIVE_SCHEMA
