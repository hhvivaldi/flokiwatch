"""Floki Snow-tool tests — FLO-347 Phase 6.

Exercises the 4 new methods on `AgentTools`:
  * submit_plan_to_snow
  * cancel_plan
  * get_plan_status
  * list_active_plans

And the supporting `snow.db.generate_plan_id()` helper.

Tests go through the tool surface directly — no Floki LLM involvement.
`AgentTools` is instantiated with a minimal bot stand-in; each test
gets a fresh tmp `history.db` via the `snow_conn` fixture so the
`snow_plans` table is empty at the start.

Test categories (~45 tests):
  * TestGeneratePlanId         — daily reset + increment + collision retry
  * TestSubmitPlan             — success, validation failure, field override
  * TestCancelPlan             — state restrictions + audit
  * TestGetPlanStatus          — summary shape + missing plan
  * TestListActivePlans        — ticket filter + terminal exclusion
  * TestToolErrorContract      — no method raises on any input
  * TestBoundaryCompliance     — agent_tools imports stay clean
"""
from __future__ import annotations

import re
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

import pytest

from snow import db as snow_db


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "snow_tools_test.db"

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
    """Build an AgentTools instance with a minimal fake bot + stub deps.
    Only the snow-tool methods are exercised in this test file; the
    injected `executor` / `safety_checks_module` / `risk_manager_module`
    are opaque sentinels since Phase 6 tools never touch them."""
    from agent_tools import AgentTools

    class _FakeBot:
        def __init__(self):
            self.symbol = "XAUUSD"
            self._last_agent_data = None
            self.running = True

    _STUB = object()  # phase 6 tools never dereference these
    return AgentTools(
        bot=_FakeBot(),
        executor=_STUB,
        safety_checks_module=_STUB,
        risk_manager_module=_STUB,
    )


def _future_iso(hours: int = 6) -> str:
    """UTC ISO timestamp `hours` into the future — used as expires_at so
    the tool-injected `created_at = utc_iso()` stays strictly before
    expires_at regardless of wall-clock time at test run."""
    import datetime as _dt
    t = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=hours)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


_BASE_PLAN: dict[str, Any] = {
    # id / created_by / created_at are ALWAYS tool-injected — values
    # here are dummies that the tool will overwrite.
    "schema_version": 1,
    "id": "PLAN-00000000-000",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    # expires_at is replaced at plan-build time (see _plan_dict) so
    # it's always strictly AFTER tool-injected created_at = utc_iso().
    "expires_at": "PLACEHOLDER",
    "status": "pending",
    "analysis": {
        "thesis": "integration test",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 72,
        "regime_assumed": "TRENDING_BEARISH",
    },
    "entry": {
        "direction": "SELL",
        "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4730.0}],
        "initial_sl": 4740.0,
        "initial_tp": 4710.0,
    },
    "management": [],
    "exit": [],
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


# ===========================================================================
# TestGeneratePlanId
# ===========================================================================

class TestGeneratePlanId:
    def test_first_id_of_day_is_001(self, snow_conn):
        pid = snow_db.generate_plan_id()
        assert re.match(r"^PLAN-\d{8}-001$", pid), pid

    def test_increments_when_plans_exist(self, snow_conn, tools):
        r1 = tools.submit_plan_to_snow(_plan_dict())
        r2 = tools.submit_plan_to_snow(_plan_dict())
        r3 = tools.submit_plan_to_snow(_plan_dict())
        assert r1["success"] and r2["success"] and r3["success"]
        ids = [r1["plan_id"], r2["plan_id"], r3["plan_id"]]
        numbers = [int(i.rsplit("-", 1)[-1]) for i in ids]
        assert numbers == sorted(numbers)
        assert numbers[-1] == numbers[0] + 2  # strictly monotonic

    def test_generate_plan_id_explicit_date(self, snow_conn):
        pid = snow_db.generate_plan_id(date="20200101")
        assert pid == "PLAN-20200101-001"

    def test_generate_plan_id_day_format_no_dashes(self):
        """`date` argument is YYYYMMDD (no dashes); bare string used
        verbatim in the generated id."""
        pid = snow_db.generate_plan_id(date="20260424")
        assert "PLAN-20260424-" in pid
        assert "PLAN-2026-04-24-" not in pid


# ===========================================================================
# TestSubmitPlan
# ===========================================================================

class TestSubmitPlan:
    def test_valid_plan_returns_plan_id_and_inserts(self, snow_conn, tools):
        result = tools.submit_plan_to_snow(_plan_dict())
        assert result["success"] is True
        assert result["validation_errors"] is None
        assert re.match(r"^PLAN-\d{8}-\d{3}$", result["plan_id"])
        row = snow_db.get_plan(result["plan_id"])
        assert row is not None
        assert row["status"] == "pending"

    def test_floki_supplied_id_is_overwritten(self, snow_conn, tools):
        """Tool must ALWAYS overwrite id; Floki cannot pick the plan_id."""
        rogue = _plan_dict(id="PLAN-20990101-999")
        result = tools.submit_plan_to_snow(rogue)
        assert result["success"] is True
        assert result["plan_id"] != "PLAN-20990101-999"
        # Rogue id must not appear in DB.
        assert snow_db.get_plan("PLAN-20990101-999") is None

    def test_floki_supplied_created_by_is_overwritten(self, snow_conn, tools):
        rogue = _plan_dict(created_by="somebody_else")
        result = tools.submit_plan_to_snow(rogue)
        # Pydantic Literal["floki"] would reject anything else — but our
        # tool overwrites BEFORE validation, so even if Floki sends
        # something wrong, it becomes "floki" and validates.
        assert result["success"] is True
        row = snow_db.get_plan(result["plan_id"])
        assert row["created_by"] == "floki"

    def test_floki_supplied_created_at_is_overwritten(self, snow_conn, tools):
        rogue = _plan_dict(created_at="2000-01-01T00:00:00Z")
        result = tools.submit_plan_to_snow(rogue)
        assert result["success"] is True
        row = snow_db.get_plan(result["plan_id"])
        assert row["created_at"] != "2000-01-01T00:00:00Z"

    def test_invalid_plan_returns_validation_errors(self, snow_conn, tools):
        # Violate the direction literal — Pydantic will reject.
        bad = _plan_dict()
        bad["entry"]["direction"] = "SIDEWAYS"
        result = tools.submit_plan_to_snow(bad)
        assert result["success"] is False
        assert result["plan_id"] is None
        assert isinstance(result["validation_errors"], list)
        assert len(result["validation_errors"]) >= 1

    def test_invalid_plan_does_not_insert(self, snow_conn, tools):
        bad = _plan_dict()
        bad["entry"]["direction"] = "SIDEWAYS"
        tools.submit_plan_to_snow(bad)
        # No rows inserted.
        rows = snow_db.list_plans_by_status(
            ("pending", "triggered", "active", "closing",
             "closed", "cancelled", "expired", "failed"),
            limit=100,
        )
        assert rows == []

    def test_non_dict_input_returns_error(self, snow_conn, tools):
        for bad in (None, [], "string", 42, object()):
            result = tools.submit_plan_to_snow(bad)
            assert result["success"] is False

    def test_empty_dict_returns_validation_errors(self, snow_conn, tools):
        result = tools.submit_plan_to_snow({})
        assert result["success"] is False
        assert len(result["validation_errors"]) >= 1

    def test_business_rule_violation_surfaced(self, snow_conn, tools):
        """SL on wrong side for SELL (SL < TP) → business-rule rejection."""
        bad = _plan_dict()
        bad["entry"]["initial_sl"] = 4700.0  # below TP 4710 → invalid for SELL
        result = tools.submit_plan_to_snow(bad)
        assert result["success"] is False
        assert any("sl" in e.lower() or "tp" in e.lower()
                   for e in result["validation_errors"])

    def test_cancel_plan_action_in_management_rejected(self, snow_conn, tools):
        """Phase 5b validator rule: cancel_plan is not a contingency action."""
        bad = _plan_dict()
        bad["management"] = [{
            "name": "rogue_cancel",
            "priority": 5,
            "conditions": [{"type": "price_below", "level": 4720.0}],
            "action": {"type": "cancel_plan"},
            "fires": "once",
        }]
        result = tools.submit_plan_to_snow(bad)
        assert result["success"] is False
        assert any("cancel_plan" in e for e in result["validation_errors"])

    def test_plan_id_format_is_daily_counter(self, snow_conn, tools):
        """Submit several; verify same-day NNN increment."""
        ids = []
        for _ in range(3):
            r = tools.submit_plan_to_snow(_plan_dict())
            assert r["success"]
            ids.append(r["plan_id"])
        # All share the same YYYYMMDD; NNN is 001, 002, 003.
        prefixes = {"-".join(i.split("-")[:2]) for i in ids}
        assert len(prefixes) == 1
        suffixes = [int(i.rsplit("-", 1)[-1]) for i in ids]
        assert suffixes == [1, 2, 3]

    def test_submit_never_raises_on_garbage_input(self, snow_conn, tools):
        """Tool convention: return error dict, never raise."""
        for bad in (None, "xxx", 42, [1, 2, 3], {"malformed": True}):
            result = tools.submit_plan_to_snow(bad)  # must not raise
            assert isinstance(result, dict)
            assert "success" in result


# ===========================================================================
# TestCancelPlan
# ===========================================================================

class TestCancelPlan:
    def test_pending_plan_cancels_successfully(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        c = tools.cancel_plan(pid, reason="changed my mind")
        assert c["success"] is True
        assert c["new_status"] == "cancelled"
        row = snow_db.get_plan(pid)
        assert row["status"] == "cancelled"

    def test_cancel_writes_audit_trigger_row(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        tools.cancel_plan(pid, reason="test audit trail")
        triggers = snow_db.list_triggers(plan_id=pid)
        audit = [t for t in triggers if t["contingency_name"] == "_user_cancel"]
        assert len(audit) == 1
        assert audit[0]["action_type"] == "cancel_plan"
        assert audit[0]["execution_status"] == "success"

    def test_cancel_active_plan_rejected_with_pedagogical_error(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        snow_db.update_plan_status(pid, "active")
        c = tools.cancel_plan(pid, reason="oops")
        assert c["success"] is False
        assert "close_trade" in c["reason"].lower()
        # Plan still ACTIVE, not cancelled.
        assert snow_db.get_plan(pid)["status"] == "active"

    def test_cancel_triggered_plan_rejected(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        snow_db.update_plan_status(pid, "triggered")
        c = tools.cancel_plan(pid, reason="x")
        assert c["success"] is False
        assert "triggered" in c["reason"]

    def test_cancel_already_closed_plan_rejected(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        snow_db.update_plan_status(pid, "closed")
        c = tools.cancel_plan(pid, reason="x")
        assert c["success"] is False
        assert "nothing to cancel" in c["reason"].lower()

    def test_cancel_missing_plan_returns_error(self, snow_conn, tools):
        c = tools.cancel_plan("PLAN-20990101-999", reason="x")
        assert c["success"] is False
        assert "not found" in c["reason"].lower()

    def test_cancel_empty_reason_rejected(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        for bad_reason in ("", "   ", "\t\n"):
            c = tools.cancel_plan(pid, reason=bad_reason)
            assert c["success"] is False
            assert "reason" in c["reason"].lower()

    def test_cancel_empty_plan_id_rejected(self, snow_conn, tools):
        c = tools.cancel_plan("", reason="x")
        assert c["success"] is False
        assert "plan_id" in c["reason"].lower()

    def test_cancel_never_raises_on_garbage(self, snow_conn, tools):
        for bad in (None, 42, [], {"dict": 1}):
            result = tools.cancel_plan(bad, reason="x")  # type: ignore[arg-type]
            assert isinstance(result, dict)
            assert "success" in result


# ===========================================================================
# TestGetPlanStatus
# ===========================================================================

class TestGetPlanStatus:
    def test_known_plan_returns_summary(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        pid = r["plan_id"]
        s = tools.get_plan_status(pid)
        assert s["success"] is True
        assert s["plan_id"] == pid
        assert s["status"] == "pending"
        # Expected summary fields present.
        for field in ("created_at", "expires_at", "trade_ticket",
                      "entered_at", "closed_at", "outcome_pips",
                      "outcome_usd", "last_evaluated_at"):
            assert field in s, f"missing summary field: {field}"

    def test_unknown_plan_returns_failure(self, snow_conn, tools):
        s = tools.get_plan_status("PLAN-20990101-999")
        assert s["success"] is False
        assert "not found" in s["reason"].lower()

    def test_get_status_does_not_return_plan_json(self, snow_conn, tools):
        """Summary must NOT leak the full plan_json — context-hygiene for Floki."""
        r = tools.submit_plan_to_snow(_plan_dict())
        s = tools.get_plan_status(r["plan_id"])
        assert "plan_json" not in s
        assert "analysis" not in s
        assert "entry" not in s

    def test_terminal_plan_status_visible(self, snow_conn, tools):
        r = tools.submit_plan_to_snow(_plan_dict())
        snow_db.update_plan_status(r["plan_id"], "closed")
        s = tools.get_plan_status(r["plan_id"])
        assert s["success"] is True
        assert s["status"] == "closed"

    def test_get_status_empty_id_rejected(self, snow_conn, tools):
        s = tools.get_plan_status("")
        assert s["success"] is False

    def test_get_status_never_raises(self, snow_conn, tools):
        for bad in (None, 42, [], {"k": 1}):
            result = tools.get_plan_status(bad)  # type: ignore[arg-type]
            assert isinstance(result, dict)
            assert "success" in result


# ===========================================================================
# TestListActivePlans
# ===========================================================================

class TestListActivePlans:
    def test_empty_db_returns_empty_list(self, snow_conn, tools):
        r = tools.list_active_plans()
        assert r["success"] is True
        assert r["count"] == 0
        assert r["plans"] == []

    def test_lists_multiple_active_plans(self, snow_conn, tools):
        pids = []
        for _ in range(3):
            res = tools.submit_plan_to_snow(_plan_dict())
            pids.append(res["plan_id"])
        r = tools.list_active_plans()
        assert r["success"] is True
        assert r["count"] == 3
        listed = {p["plan_id"] for p in r["plans"]}
        assert listed == set(pids)

    def test_terminal_plans_excluded(self, snow_conn, tools):
        res_a = tools.submit_plan_to_snow(_plan_dict())
        res_b = tools.submit_plan_to_snow(_plan_dict())
        # Transition b to CLOSED.
        snow_db.update_plan_status(res_b["plan_id"], "closed")
        r = tools.list_active_plans()
        listed = {p["plan_id"] for p in r["plans"]}
        assert res_a["plan_id"] in listed
        assert res_b["plan_id"] not in listed

    def test_ticket_filter_narrows_results(self, snow_conn, tools):
        a = tools.submit_plan_to_snow(_plan_dict())
        b = tools.submit_plan_to_snow(_plan_dict())
        snow_db.update_plan_trade_ticket(a["plan_id"], 111_111)
        snow_db.update_plan_trade_ticket(b["plan_id"], 222_222)
        r = tools.list_active_plans(ticket=111_111)
        assert r["success"] is True
        assert r["count"] == 1
        assert r["plans"][0]["plan_id"] == a["plan_id"]

    def test_ticket_filter_no_match_returns_empty(self, snow_conn, tools):
        tools.submit_plan_to_snow(_plan_dict())
        r = tools.list_active_plans(ticket=999_999)
        assert r["success"] is True
        assert r["count"] == 0

    def test_ticket_filter_non_int_rejected(self, snow_conn, tools):
        r = tools.list_active_plans(ticket="not_an_int")  # type: ignore[arg-type]
        assert r["success"] is False
        assert "int" in r["reason"].lower()

    def test_summary_shape_is_limited(self, snow_conn, tools):
        """Listed plans return summaries only (no plan_json leakage)."""
        tools.submit_plan_to_snow(_plan_dict())
        r = tools.list_active_plans()
        p = r["plans"][0]
        assert "plan_json" not in p
        assert set(p.keys()) == {
            "plan_id", "status", "trade_ticket", "created_at", "last_evaluated_at",
        }


# ===========================================================================
# TestToolErrorContract
# ===========================================================================

class TestToolErrorContract:
    """No tool method may raise; all return dicts with 'success' key.
    Catches regressions in the try/except discipline."""

    def test_submit_plan_always_returns_dict(self, snow_conn, tools):
        for bad in (None, [], {"oops": True}, "xxx", 0):
            result = tools.submit_plan_to_snow(bad)
            assert isinstance(result, dict) and "success" in result

    def test_cancel_plan_always_returns_dict(self, snow_conn, tools):
        for plan_id in (None, "", "PLAN-XXX-999"):
            result = tools.cancel_plan(plan_id, reason="x")  # type: ignore[arg-type]
            assert isinstance(result, dict) and "success" in result

    def test_get_plan_status_always_returns_dict(self, snow_conn, tools):
        for pid in (None, "", "PLAN-XXX-999"):
            result = tools.get_plan_status(pid)  # type: ignore[arg-type]
            assert isinstance(result, dict) and "success" in result

    def test_list_active_plans_always_returns_dict(self, snow_conn, tools):
        for ticket in (None, 0, -1, 111, "abc", [], {}):
            result = tools.list_active_plans(ticket=ticket)  # type: ignore[arg-type]
            assert isinstance(result, dict) and "success" in result


# ===========================================================================
# TestBoundaryCompliance
# ===========================================================================

class TestBoundaryCompliance:
    """agent_tools.py may import snow.db and snow.validator (Phase 6
    grants). It must NOT import snow.actions, snow.snow_loop,
    snow.priority, or snow.evaluators — those are loop-internal surfaces."""

    FORBIDDEN = (
        "snow.actions",
        "snow.snow_loop",
        "snow.priority",
        "snow.evaluators",
    )

    def test_agent_tools_does_not_import_forbidden_snow_internals(self):
        import agent_tools
        src = Path(agent_tools.__file__).read_text(encoding="utf-8")
        for name in self.FORBIDDEN:
            # Allow substring matches inside the PROSE-only observation docstring
            # (e.g., a comment mentioning snow.priority), but forbid ACTUAL
            # import statements (top-level OR indented — both are bypasses).
            pattern = rf"(?m)^\s*(?:import|from)\s+{re.escape(name)}(?:\s|\.|$)"
            assert not re.search(pattern, src), (
                f"agent_tools.py imports forbidden Snow internal `{name}`"
            )

    def test_agent_tools_can_use_snow_db_and_validator(self):
        import agent_tools
        src = Path(agent_tools.__file__).read_text(encoding="utf-8")
        assert "from snow import db" in src or "snow.db" in src
        assert "snow.validator" in src or "from snow.validator" in src

    def test_snow_db_does_not_import_agent_tools(self):
        """Reverse direction: snow.db must stay Floki-agnostic."""
        src = Path(snow_db.__file__).read_text(encoding="utf-8")
        assert "import agent_tools" not in src
        assert "from agent_tools" not in src

    def test_snow_validator_does_not_import_agent_tools(self):
        from snow import validator as snow_validator
        src = Path(snow_validator.__file__).read_text(encoding="utf-8")
        assert "import agent_tools" not in src
        assert "from agent_tools" not in src


# ===========================================================================
# TestSchemaRegistration
# ===========================================================================

class TestSchemaRegistration:
    """The 4 tool schemas must be registered in ai_agent.py's _tool_schemas
    list so Floki can actually see them via OpenAI function-calling."""

    def test_all_four_tools_registered(self):
        import ai_agent
        src = Path(ai_agent.__file__).read_text(encoding="utf-8")
        for tool_name in (
            "submit_plan_to_snow",
            "cancel_plan",
            "get_plan_status",
            "list_active_plans",
        ):
            # Quick check: the tool name appears inside a "name": ... entry
            # in the schemas list.
            pattern = rf'"name"\s*:\s*"{re.escape(tool_name)}"'
            assert re.search(pattern, src), (
                f"tool `{tool_name}` not found in ai_agent.py tool schemas"
            )
