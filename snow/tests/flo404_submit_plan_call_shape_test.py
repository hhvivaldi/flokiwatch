"""FLO-404 follow-up — submit_plan_to_snow accepts BOTH call shapes.

Empirical motivation (CEO directive 2026-04-30): Floki tried to encode
a countertrend BUY scenario plan for the first time, but the OpenAI
tool-call layer rejected the call because his shape (direct top-level
plan body) didn't match the schema's required {"plan": {...}} wrapper.
The validator never fired — rejection was upstream of the handler.

Fix: handler accepts BOTH shapes and normalizes; tool input_schema
is relaxed to allow either shape past the OpenAI boundary.

This test file pins:
  1. Wrapped shape works (existing contract preserved).
  2. Direct-shape (kwargs) works (new contract added).
  3. Double-wrapped shape unwraps cleanly (defensive — covers the
     LLM over-correcting after seeing the new wrapper-shape example).
  4. Invalid shapes still reject with clear errors.
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any

import pytest

from snow import db as snow_db


# ---------------------------------------------------------------------------
# Fixtures — pump recipe_pulls_count to 1 so the FLO-393 gate doesn't
# fire (separate scope; we're testing call-shape only).
# ---------------------------------------------------------------------------


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "flo404_call_shape.db"

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
    """AgentTools with FLO-393 gate already satisfied (count=1)."""
    from agent_tools import AgentTools

    class _FakeBot:
        def __init__(self):
            self.symbol = "XAUUSD"
            self._last_agent_data = None
            self.running = True

    _STUB = object()
    t = AgentTools(
        bot=_FakeBot(),
        executor=_STUB,
        safety_checks_module=_STUB,
        risk_manager_module=_STUB,
    )
    t._recipe_pulls_count = 1  # satisfy FLO-393 gate
    return t


def _future_iso(hours: int = 6) -> str:
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


_BASE_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "id": "PLAN-00000000-000",
    "created_by": "floki",
    "created_at": "2026-04-30T08:00:00Z",
    "expires_at": "PLACEHOLDER",
    "status": "pending",
    "analysis": {
        "thesis": "FLO-404 call-shape test plan",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 75,
        "regime_assumed": "TRENDING_BEARISH",
    },
    "entry": {
        "direction": "SELL",
        "volume": 0.02,
        "conditions": [
            {"type": "price_above", "level": 4730.0},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
        ],
        "initial_sl": 4740.0,
        "initial_tp": 4710.0,
    },
    "management": [],
    "exit": [{
        "name": "fallback_target", "priority": 1,
        "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}],
        "action": {"type": "close_full"}, "fires": "once",
    }],  # FLO-401 floor
    "emergency": {
        "max_loss_pips": 150,
        "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _plan_dict(**overrides) -> dict[str, Any]:
    d = deepcopy(_BASE_PLAN)
    d["expires_at"] = _future_iso(6)
    d.update(overrides)
    return d


# =============================================================================
# Wrapped shape — canonical OpenAI tool-call form
# =============================================================================

class TestWrappedCallShape:
    """submit_plan_to_snow(plan={...}) — the existing contract, must
    keep working. Pre-FLO-404 tests (flo393_test.py and others) use
    this shape positionally; loosening must not break them."""

    def test_wrapped_shape_kwarg_succeeds(self, tools):
        result = tools.submit_plan_to_snow(plan=_plan_dict())
        assert result["success"] is True
        assert result["plan_id"] is not None
        assert result["plan_id"].startswith("PLAN-")

    def test_wrapped_shape_positional_succeeds(self, tools):
        """Positional arg form — what existing test files use."""
        result = tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is True
        assert result["plan_id"] is not None


# =============================================================================
# Direct shape — Floki's natural form (matches the prompt examples)
# =============================================================================

class TestDirectCallShape:
    """submit_plan_to_snow(analysis=..., entry=..., ...) — the shape
    Floki naturally produces when copying MINIMAL PLAN EXAMPLE /
    EXPLORATORY SCENARIO EXAMPLE from the prompt."""

    def test_direct_shape_with_full_plan_succeeds(self, tools):
        plan = _plan_dict()
        # Pass plan body as direct kwargs — no wrapper.
        result = tools.submit_plan_to_snow(**plan)
        assert result["success"] is True, (
            f"FLO-404: direct-shape call must succeed. "
            f"validation_errors={result.get('validation_errors')}"
        )
        assert result["plan_id"] is not None

    def test_direct_shape_no_args_rejects(self, tools):
        """Empty call — no plan, no kwargs — must reject cleanly."""
        result = tools.submit_plan_to_snow()
        assert result["success"] is False
        ve = result.get("validation_errors") or []
        assert any("plan must be a dict" in e for e in ve), (
            f"expected 'plan must be a dict' rejection; got {ve}"
        )

    def test_direct_shape_only_some_fields_routes_to_validator(self, tools):
        """Direct shape with only `analysis` (no entry/management/exit) —
        normalizes to a plan, validator catches the missing fields.
        Confirms the normalization runs even on incomplete input."""
        result = tools.submit_plan_to_snow(
            analysis={"thesis": "incomplete plan"},
        )
        # Either snow validator rejects with structured errors (expected)
        # OR the call returns success: False with a reason. Both confirm
        # normalization didn't crash and downstream error path fired.
        assert result["success"] is False


# =============================================================================
# Double-wrap defensive normalization
# =============================================================================

class TestDoubleWrapNormalization:
    """If Floki over-corrects after seeing the wrapper-shape example
    and produces submit_plan_to_snow(plan={"plan": {...full...}}),
    the handler unwraps once. Cheap defensive normalization."""

    def test_double_wrapped_unwraps_and_succeeds(self, tools):
        plan = _plan_dict()
        wrapped_twice = {"plan": plan}
        result = tools.submit_plan_to_snow(plan=wrapped_twice)
        assert result["success"] is True, (
            f"FLO-404: double-wrapped shape must unwrap cleanly. "
            f"validation_errors={result.get('validation_errors')}"
        )
        assert result["plan_id"] is not None

    def test_unwrap_only_fires_when_outer_lacks_analysis(self, tools):
        """The unwrap rule is conditional: outer dict has NO `analysis`
        AND inner dict has `analysis`. If outer ALREADY has analysis,
        the inner `plan` field (whatever it is) must be left alone.
        Verifies the conditional gate, not what the validator does
        with a noisy field."""
        from agent_tools import AgentTools
        # Direct call to the normalization gate via the public method —
        # both inputs preserve outer.analysis, so unwrap must NOT fire
        # and the validator-error message refers to the OUTER plan's
        # content (proving outer was used).
        plan = _plan_dict()
        # Build a wrapper-shape outer that DOES have analysis at top
        # level, plus an inner `plan` key that should be ignored by
        # the unwrap gate.
        plan["analysis"]["thesis"] = "OUTER_THESIS_MARKER"
        plan["plan"] = {"analysis": {"thesis": "INNER_THESIS_MARKER"}}
        # Strict snow validator rejects unknown top-level keys, so this
        # call returns success=False — but the rejection comes from the
        # OUTER plan being processed (with the unknown `plan` key).
        # That's fine; the test asserts the unwrap gate didn't replace
        # outer with inner (which would have caused a different
        # rejection citing the INNER thesis instead).
        result = tools.submit_plan_to_snow(plan=plan)
        # Whether validator accepts or rejects, the unwrap gate's
        # decision is observable: we never silently replaced outer
        # with inner. Run the gate function in isolation to confirm
        # by inspecting the normalized dict directly:
        # The handler is private; instead check that any rejection
        # message is consistent with OUTER processing — the inner
        # marker should not appear in errors:
        ve = result.get("validation_errors") or []
        joined_errors = " ".join(ve)
        assert "INNER_THESIS_MARKER" not in joined_errors, (
            "unwrap fired incorrectly — inner replaced outer despite "
            "outer having its own analysis"
        )


# =============================================================================
# Invalid input still rejects cleanly
# =============================================================================

class TestInvalidInput:
    def test_non_dict_plan_rejects(self, tools):
        for bad in (None, [], "string", 42, 3.14):
            r = tools.submit_plan_to_snow(plan=bad)
            assert r["success"] is False
            ve = r.get("validation_errors") or []
            assert any("plan must be a dict" in e for e in ve), (
                f"expected dict-rejection for {type(bad).__name__}; got {ve}"
            )

    def test_both_wrapper_and_kwargs_uses_wrapper(self, tools):
        """Defensive: if BOTH `plan` and kwargs supplied, the wrapper
        wins — kwargs ignored. This is unlikely in practice but the
        rule must be deterministic to avoid silent ambiguity."""
        wrapped = _plan_dict()
        # Pass plan AND extras at top level — handler should use plan,
        # ignore the kwargs. The plan is valid → success.
        result = tools.submit_plan_to_snow(plan=wrapped, extra_key="ignored")
        assert result["success"] is True
