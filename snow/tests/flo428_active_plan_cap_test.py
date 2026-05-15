"""FLO-428 — active-plan cap validator tests.

Three cases:
  - 0 live plans → allow
  - 1 live plan → allow
  - 2 live plans → reject with active_plan_cap: prefix
  - DB error → allow (fail-open) + DEGRADED warn

The check reads `snow.db.get_active_plans()` so we monkeypatch that
function — testing the validator logic, not the DB layer.
"""

from __future__ import annotations

import pytest

import snow.validator as validator_mod
from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    """Patch snow.db.get_active_plans (which the validator imports
    inside _check_active_plan_cap) so each test controls live-count
    without touching the real DB."""
    holder = {"plans": []}

    def _fake_get():
        return list(holder["plans"])

    # Patch where the validator imports from — snow.db
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", _fake_get)
    return holder


class TestActivePlanCap:

    def test_zero_live_allowed(self, valid_plan_dict, patch_active_plans):
        patch_active_plans["plans"] = []
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not any(e.startswith("active_plan_cap:") for e in errors), errors

    def test_one_live_allowed(self, valid_plan_dict, patch_active_plans):
        patch_active_plans["plans"] = [
            {"id": "PLAN-20260101-001", "status": "pending"},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not any(e.startswith("active_plan_cap:") for e in errors), errors

    def test_two_live_blocked(self, valid_plan_dict, patch_active_plans):
        patch_active_plans["plans"] = [
            {"id": "PLAN-20260101-001", "status": "pending"},
            {"id": "PLAN-20260101-002", "status": "active"},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok is False
        assert any(e.startswith("active_plan_cap:") for e in errors), errors
        msg = next(e for e in errors if e.startswith("active_plan_cap:"))
        assert "PLAN-20260101-001" in msg and "PLAN-20260101-002" in msg
        assert "FLO-428" in msg

    def test_three_live_blocked(self, valid_plan_dict, patch_active_plans):
        patch_active_plans["plans"] = [
            {"id": "PLAN-20260101-001", "status": "pending"},
            {"id": "PLAN-20260101-002", "status": "active"},
            {"id": "PLAN-20260101-003", "status": "triggered"},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        assert ok is False
        assert any(e.startswith("active_plan_cap:") for e in errors), errors

    def test_self_excluded_from_count(self, valid_plan_dict, patch_active_plans):
        """If the row being validated is already in the DB (re-validation
        path), it should not count against the cap."""
        self_id = valid_plan_dict["id"]
        patch_active_plans["plans"] = [
            {"id": self_id, "status": "pending"},   # this is the plan being validated
            {"id": "PLAN-20260101-001", "status": "pending"},
        ]
        ok, _, errors = validate_plan(valid_plan_dict)
        # 1 OTHER live → under cap → allow
        assert not any(e.startswith("active_plan_cap:") for e in errors), errors

    def test_db_error_fail_open(self, valid_plan_dict, monkeypatch, caplog):
        """DB hiccup → allow + ACTIVE_PLAN_CAP_DEGRADED log line."""
        import logging
        import snow.db as snow_db_mod

        def _raises():
            raise sqlite_error()

        def sqlite_error():
            raise RuntimeError("simulated DB failure")

        monkeypatch.setattr(snow_db_mod, "get_active_plans", _raises)
        caplog.set_level(logging.WARNING)
        ok, _, errors = validate_plan(valid_plan_dict)
        assert not any(e.startswith("active_plan_cap:") for e in errors), errors
        assert any("ACTIVE_PLAN_CAP_DEGRADED" in r.message for r in caplog.records), (
            f"expected DEGRADED log, got: {[r.message for r in caplog.records]}"
        )
