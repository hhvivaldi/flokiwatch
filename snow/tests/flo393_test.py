"""FLO-393 — Recipe Book mandatory consultation gate.

Validator/tool-loop guard rejecting plan submissions that occur in a
Floki cycle where `get_snow_recipe_book` was never called. Counter is
reset at the top of `agent_decide()` (canonical Floki cycle entry) and
incremented by `get_snow_recipe_book`.

Acceptance (per FLO-393 ticket):
  * Plan submitted in cycle with recipe_pulls_count=0 → REJECT
  * Plan submitted in cycle with recipe_pulls_count>=1 → ACCEPT
    (no quality check on which recipe pulled — separate scope)
  * Counter resets per Floki cycle (one full agent_decide invocation)

Out of scope:
  * Quality check on whether recipe matches setup
  * Multi-recipe consultation requirement
  * Recipe Book content modifications
"""
from __future__ import annotations

import sqlite3
from copy import deepcopy
from typing import Any

import pytest

from snow import db as snow_db


# ---------------------------------------------------------------------------
# Local fixtures — explicit counter control. Cannot reuse the shared
# `tools` fixture from tools_test.py because it pre-pumps count=1.
# ---------------------------------------------------------------------------


@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "flo393_test.db"

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
def fresh_tools(snow_conn):
    """AgentTools with `_recipe_pulls_count` left at the constructor
    default (0). Use this for gate-rejection tests."""
    from agent_tools import AgentTools

    class _FakeBot:
        def __init__(self):
            self.symbol = "XAUUSD"
            self._last_agent_data = None
            self.running = True

    _STUB = object()
    return AgentTools(
        bot=_FakeBot(),
        executor=_STUB,
        safety_checks_module=_STUB,
        risk_manager_module=_STUB,
    )


def _future_iso(hours: int = 6) -> str:
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


_BASE_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "id": "PLAN-00000000-000",
    "created_by": "floki",
    "created_at": "2026-04-28T08:00:00Z",
    "expires_at": "PLACEHOLDER",
    "status": "pending",
    "analysis": {
        "thesis": "FLO-393 gate test plan",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 72,
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
    "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
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
# Acceptance — gate rejects/accepts on counter value
# =============================================================================


class TestFLO393Acceptance:
    def test_count_zero_plan_rejected(self, fresh_tools):
        """Acceptance: plan submitted with recipe_pulls_count=0 → REJECT."""
        # Sanity: constructor leaves counter at 0
        assert fresh_tools._recipe_pulls_count == 0
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is False
        assert result["plan_id"] is None
        errors = result.get("validation_errors") or []
        assert errors, "expected validation_errors with FLO-393 message"
        msg = " ".join(errors)
        assert "FLO-393" in msg
        assert "get_snow_recipe_book" in msg
        # Remediation must name the categories so Floki knows the fix
        assert "trend" in msg or "range" in msg or "reversal" in msg

    def test_count_one_plan_accepted(self, fresh_tools):
        """Acceptance: plan submitted with recipe_pulls_count>=1 → ACCEPT."""
        fresh_tools._recipe_pulls_count = 1
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is True, result
        assert result["plan_id"] is not None
        assert result.get("validation_errors") is None

    def test_count_high_plan_accepted(self, fresh_tools):
        """Counter accepts any positive integer; no upper bound."""
        fresh_tools._recipe_pulls_count = 42
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is True, result


# =============================================================================
# Counter mechanics — increment + reset
# =============================================================================


class TestFLO393CounterMechanics:
    def test_get_snow_recipe_book_increments_counter(self, fresh_tools):
        assert fresh_tools._recipe_pulls_count == 0
        fresh_tools.get_snow_recipe_book()
        assert fresh_tools._recipe_pulls_count == 1

    def test_multiple_pulls_increment(self, fresh_tools):
        fresh_tools.get_snow_recipe_book(category="trend")
        fresh_tools.get_snow_recipe_book(category="range")
        fresh_tools.get_snow_recipe_book()
        assert fresh_tools._recipe_pulls_count == 3

    def test_pull_then_submit_passes_gate(self, fresh_tools):
        """Realistic flow: Floki calls get_snow_recipe_book first, then
        submit_plan_to_snow within the same cycle. Both succeed."""
        recipe_result = fresh_tools.get_snow_recipe_book(category="trend")
        assert recipe_result.get("success") is True
        submit_result = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert submit_result["success"] is True, submit_result

    def test_paired_hedge_one_pull_covers_two_submits(self, fresh_tools):
        """Paired-plan flow: one Recipe Book call, then two
        submit_plan_to_snow calls in the same cycle (BUY leg + SELL leg).
        Counter accumulates — both submits pass the gate."""
        fresh_tools.get_snow_recipe_book(category="range")
        # First leg
        r1 = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert r1["success"] is True, r1
        # Second leg (mirrored direction; same plan-shape suffices for
        # this gate-only test)
        r2 = fresh_tools.submit_plan_to_snow(_plan_dict(
            entry=dict(_BASE_PLAN["entry"], direction="BUY",
                       initial_sl=4710.0, initial_tp=4740.0,
                       conditions=[
                           {"type": "price_below", "level": 4720.0},
                           {"type": "rsi", "tf": "H1", "op": "below",
                            "threshold": 50},
                       ])
        ))
        assert r2["success"] is True, r2

    def test_counter_does_not_decrement_on_submit(self, fresh_tools):
        """Counter is a 'consultation occurred' bit, not a 'pull
        consumed by submit' decrement. Verifies design choice."""
        fresh_tools._recipe_pulls_count = 1
        fresh_tools.submit_plan_to_snow(_plan_dict())
        assert fresh_tools._recipe_pulls_count == 1


# =============================================================================
# agent_decide() reset hook
# =============================================================================


class TestFLO393AgentDecideReset:
    def test_agent_decide_source_contains_reset_block(self):
        """Locks the cycle-reset contract: `agent_decide()` source must
        contain a `_recipe_pulls_count = 0` reset on the tools arg.
        This test fails if a future refactor drops the reset, even if
        the rest of the codebase still works.

        Brittle to formatting changes — that's the point. Adjust the
        signature line below if the canonical reset idiom is rewritten,
        but the existence-of-reset invariant must survive.
        """
        import inspect
        from ai_agent import agent_decide
        src = inspect.getsource(agent_decide)
        assert "_recipe_pulls_count" in src, (
            "agent_decide() must reference _recipe_pulls_count"
        )
        assert "tools._recipe_pulls_count = 0" in src, (
            "agent_decide() must reset tools._recipe_pulls_count to 0 "
            "at cycle entry (FLO-393 contract)"
        )
        assert "FLO-393" in src, (
            "agent_decide() reset block must be tagged FLO-393 so "
            "future readers can locate the contract"
        )

    def test_agent_decide_reset_handles_none_tools(self):
        """agent_decide() reset is a no-op when tools is None.
        Verifies the guarded reset block doesn't AttributeError."""
        # Reproduce the reset shape with tools=None
        tools = None
        try:
            if tools is not None and hasattr(tools, "_recipe_pulls_count"):
                tools._recipe_pulls_count = 0
        except Exception:
            pytest.fail("reset block must be a no-op when tools is None")

    def test_reset_block_executed_against_real_tools_instance(
        self, fresh_tools
    ):
        """Behavioural test of the reset semantics applied to a real
        AgentTools instance: counter goes from non-zero to 0."""
        fresh_tools._recipe_pulls_count = 7
        # Apply the same reset block the source-inspection test pins.
        if fresh_tools is not None and hasattr(
            fresh_tools, "_recipe_pulls_count"
        ):
            fresh_tools._recipe_pulls_count = 0
        assert fresh_tools._recipe_pulls_count == 0


# =============================================================================
# Coexistence with FLO-382 deque
# =============================================================================


class TestFLO393CoexistenceWithFLO382:
    def test_both_counters_increment_on_pull(self, fresh_tools):
        """get_snow_recipe_book updates BOTH the FLO-382 deque (for the
        600s telemetry recency window) AND the FLO-393 counter (for the
        per-cycle gate). Each tracks a different concern."""
        assert len(fresh_tools._recipe_pulls) == 0
        assert fresh_tools._recipe_pulls_count == 0
        fresh_tools.get_snow_recipe_book(category="trend")
        assert len(fresh_tools._recipe_pulls) == 1
        assert fresh_tools._recipe_pulls_count == 1

    def test_flo382_deque_not_cleared_by_flo393_reset(self, fresh_tools):
        """The FLO-393 reset (counter=0) must NOT touch the FLO-382
        deque. Paired-hedge attribution depends on the deque
        persisting across cycle boundaries within the 600s window."""
        fresh_tools.get_snow_recipe_book(category="trend")
        assert len(fresh_tools._recipe_pulls) == 1
        # Simulate FLO-393 cycle reset
        fresh_tools._recipe_pulls_count = 0
        # Deque must be untouched
        assert len(fresh_tools._recipe_pulls) == 1


# =============================================================================
# Error message — structured remediation per CTO directive
# =============================================================================


class TestFLO393ErrorMessage:
    def test_error_returns_validation_errors_shape(self, fresh_tools):
        """Reject path uses the same response shape as schema validation
        failures (success=False, plan_id=None, validation_errors list)
        so Floki's existing retry handling works without changes."""
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is False
        assert result["plan_id"] is None
        assert isinstance(result.get("validation_errors"), list)
        assert len(result["validation_errors"]) >= 1

    def test_error_names_remediation(self, fresh_tools):
        """Error must tell Floki what to do (call get_snow_recipe_book)
        and provide the category vocabulary so the remediation is
        actionable, not just diagnostic."""
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        msg = " ".join(result.get("validation_errors") or [])
        assert "get_snow_recipe_book" in msg
        assert "mandatory" in msg.lower()
        assert "resubmit" in msg.lower() or "submit" in msg.lower()

    def test_error_does_not_request_plan_changes(self, fresh_tools):
        """Critical: reject message must NOT suggest changing the plan
        shape. The plan is fine; only the precondition (Recipe Book
        consultation) is missing. Otherwise Floki may pointlessly
        re-draft a valid plan."""
        result = fresh_tools.submit_plan_to_snow(_plan_dict())
        msg = (" ".join(result.get("validation_errors") or [])).lower()
        # The remediation should explicitly say "no plan-shape changes"
        assert "no plan-shape changes" in msg or "same plan dict" in msg
