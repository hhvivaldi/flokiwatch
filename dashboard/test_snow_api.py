"""FLO-377 — Dashboard Snow API endpoints.

Smoke + boundary tests for ``/api/snow/plans`` and
``/api/snow/plan/{id}``. Uses httpx ASGI transport (matches the
TestClient signature drift in the locally-installed starlette).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy

import httpx
import pytest


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Per-test tmp Snow DB. Same shape as snow/tests/snow_conn fixture."""
    from snow import db as snow_db
    db_path = tmp_path / "dashboard_snow_api_test.db"

    def _tmp_connect():
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


@pytest.fixture
def app_client():
    """Async httpx client wrapping the dashboard FastAPI app."""
    from dashboard.server import app
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


_BASE_PLAN: dict = {
    "schema_version": 3,
    "id": "PLAN-20260424-001",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "Gold at H1 resistance — expecting rejection.",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 72,
        "regime_assumed": "TRENDING_BEARISH",
        "setup_type": "pullback_trend",
        "context_tags": {
            "trend": "trend_strong",
            "volatility": "high_vol",
            "htf": "HTF_aligned",
            "news_session": ["session_overlap"],
        },
        "confidence_reason": (
            "H4/H1 EMA stack aligned bearish; rejection wick at 4735."
        ),
    },
    "entry": {
        "direction": "SELL", "volume": 0.02,
        "conditions": [{"type": "price_above", "level": 4730.0}],
        "initial_sl": 4740.0, "initial_tp": 4710.0,
    },
    "management": [{
        "name": "lock_be", "priority": 7,
        "conditions": [{"type": "profit_pips", "op": "above", "threshold": 5.0}],
        "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
        "fires": "once",
    }],
    "exit": [{
        "name": "rejection", "priority": 9,
        "conditions": [{"type": "price_above", "level": 4733.0}],
        "action": {"type": "close_full"}, "fires": "once",
    }],
    "emergency": {
        "max_loss_pips": 150, "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


def _insert_plan(plan_id: str, **overrides):
    from snow import db as snow_db
    from snow.schema import Plan
    d = deepcopy(_BASE_PLAN)
    d["id"] = plan_id
    for k, v in overrides.items():
        d[k] = v
    snow_db.insert_plan(Plan(**d))


def _make_terminal(plan_id: str, status: str, outcome_pips=None, outcome_usd=None):
    from snow import db as snow_db
    snow_db.update_plan_status(plan_id, status)
    if outcome_pips is not None:
        snow_db.update_plan_outcome(
            plan_id, outcome_pips=outcome_pips, outcome_usd=outcome_usd,
            new_status=status,
        )


def _run(coro):
    """Each test gets a fresh event loop. Avoids the deprecated
    ``get_event_loop()`` (Python 3.12+) AND the cross-test client
    closure issue (a yield-fixture would also work but requires
    pytest-asyncio configuration which this repo doesn't have)."""
    return asyncio.run(coro)


# ── /api/snow/plans ───────────────────────────────────────────────────

class TestSnowPlansList:
    def test_active_filter_default(self, snow_conn, app_client):
        _insert_plan("PLAN-20260424-100", status="pending")
        _insert_plan("PLAN-20260424-101", status="active")
        _insert_plan("PLAN-20260424-102", status="closed")

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        assert body["success"] is True
        ids = {p["id"] for p in body["plans"]}
        assert "PLAN-20260424-100" in ids
        assert "PLAN-20260424-101" in ids
        assert "PLAN-20260424-102" not in ids  # closed → excluded

    def test_terminal_filter(self, snow_conn, app_client):
        _insert_plan("PLAN-20260424-200", status="pending")
        _insert_plan("PLAN-20260424-201", status="closed")
        _insert_plan("PLAN-20260424-202", status="cancelled")

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans?status=terminal")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        ids = {p["id"] for p in body["plans"]}
        assert ids == {"PLAN-20260424-201", "PLAN-20260424-202"}

    def test_explicit_single_status(self, snow_conn, app_client):
        """Status names that are NOT group aliases (active/terminal/all)
        match exactly. 'active' is a group alias by design (UX: "show
        me active plans" = pending+triggered+active+closing). To query
        the literal status 'active' alone, the caller would need to
        intersect with the trade_ticket filter or add a future
        ``?status_exact=active`` parameter — out of scope for v1."""
        _insert_plan("PLAN-20260424-300", status="pending")
        _insert_plan("PLAN-20260424-301", status="closed",
                     outcome_pips=2.6, outcome_usd=0.52)

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans?status=closed")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        ids = {p["id"] for p in body["plans"]}
        assert ids == {"PLAN-20260424-301"}

    def test_invalid_filter_returns_400(self, snow_conn, app_client):
        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans?status=bogus")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 400
        assert body["success"] is False
        assert "bogus" in body["error"]

    def test_limit_clamped(self, snow_conn, app_client):
        for i in range(5):
            _insert_plan(f"PLAN-20260424-4{i:02d}", status="pending")

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans?limit=2")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        assert body["count"] == 2

    def test_offset_pagination(self, snow_conn, app_client):
        # Insert 4 distinct plans.
        for i in range(4):
            _insert_plan(f"PLAN-20260424-5{i:02d}", status="pending")

        async def go():
            async with app_client as c:
                r1 = await c.get("/api/snow/plans?limit=2&offset=0")
                r2 = await c.get("/api/snow/plans?limit=2&offset=2")
                return r1.json(), r2.json()
        page1, page2 = _run(go())
        ids1 = [p["id"] for p in page1["plans"]]
        ids2 = [p["id"] for p in page2["plans"]]
        assert len(ids1) == 2 and len(ids2) == 2
        assert set(ids1).isdisjoint(set(ids2))

    def test_summary_carries_v3_tagging(self, snow_conn, app_client):
        _insert_plan("PLAN-20260424-600", status="pending")

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plans")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        p = body["plans"][0]
        assert p["setup_type"] == "pullback_trend"
        assert p["context_tags"]["trend"] == "trend_strong"
        assert p["confidence"] == 72


# ── /api/snow/plan/{plan_id} ──────────────────────────────────────────

class TestSnowPlanDetail:
    def test_happy_path(self, snow_conn, app_client):
        _insert_plan("PLAN-20260424-700", status="active")
        from snow import db as snow_db
        # Seed a trigger row + an evaluation row so we exercise both
        # audit-log paths.
        snow_db.record_trigger(
            plan_id="PLAN-20260424-700",
            contingency_name="lock_be", contingency_kind="management",
            action_type="move_sl_to_breakeven",
            execution_status="success",
            action_params={"ticket": 999, "new_sl": 4720.0},
        )
        snow_db.record_evaluation(
            plan_id="PLAN-20260424-700", contingency_name="lock_be",
            event="management_would_fire",
            conditions_snapshot={"price": 4715.0},
        )

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plan/PLAN-20260424-700")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        assert body["success"] is True
        assert body["summary"]["id"] == "PLAN-20260424-700"
        assert body["plan"]["entry"]["direction"] == "SELL"
        assert len(body["triggers"]) == 1
        assert body["triggers"][0]["execution_status"] == "success"
        assert len(body["evaluations"]) == 1
        # Evaluations conditions_snapshot should be parsed back to a dict.
        assert isinstance(body["evaluations"][0]["conditions_snapshot"], dict)

    def test_404_on_missing_plan(self, snow_conn, app_client):
        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plan/PLAN-29991231-999")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 404
        assert body["success"] is False
        assert "not found" in body["error"]

    def test_400_on_malformed_id(self, snow_conn, app_client):
        async def go():
            async with app_client as c:
                results = []
                for bad in ("banana", "PLAN-2026-001", "2026-04-24-001"):
                    r = await c.get(f"/api/snow/plan/{bad}")
                    results.append((bad, r.status_code, r.json().get("error", "")))
                return results
        rs = _run(go())
        for bad, status, err in rs:
            assert status == 400, f"{bad!r} got {status}"
            assert "invalid plan_id format" in err

    def test_execution_quality_included_when_present(self, snow_conn, app_client):
        _insert_plan("PLAN-20260424-800", status="active")
        from snow import db as snow_db
        # Seed trigger then EQ row tied to its id.
        tid = snow_db.record_trigger(
            plan_id="PLAN-20260424-800",
            contingency_name="_entry", contingency_kind="entry",
            action_type="execute_market", execution_status="success",
            action_params={"direction": "SELL", "volume": 0.02},
        )
        snow_db.insert_execution_quality(
            trigger_id=tid, plan_id="PLAN-20260424-800",
            action_type="execute_market",
            fired_at="2026-04-24T08:00:00.000Z",
            executed_at="2026-04-24T08:00:00.045Z",
            latency_ms=45,
            plan_volume=0.02, plan_price=4720.0,
            actual_volume=0.02, actual_price=4720.4,
            slippage_pips=4.0,
            bid_at_fire=4720.0, ask_at_fire=4720.4, mid_at_fire=4720.2,
            status="success", ticket=999, attempts=1, error_message=None,
        )

        async def go():
            async with app_client as c:
                r = await c.get("/api/snow/plan/PLAN-20260424-800")
                return r.status_code, r.json()
        status, body = _run(go())
        assert status == 200
        assert len(body["execution_quality"]) == 1
        eq = body["execution_quality"][0]
        assert eq["plan_volume"] == 0.02
        assert eq["actual_price"] == 4720.4
        assert eq["slippage_pips"] == 4.0
