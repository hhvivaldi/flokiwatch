"""Pytest fixtures shared across snow tests.

Phase 1 scope: only plan-dict factories for schema + validator tests.
MT5 / executor / DB fixtures (FakeMT5, FakeBot, in-memory sqlite) land
in later phases per RFC §12.3.

Run tests from repo root:
    python -m pytest snow/tests/ -v
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest


# -----------------------------------------------------------------------------
# Canonical valid plan (RFC §2.8 example)
# -----------------------------------------------------------------------------

_BASE_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "id": "PLAN-20260424-001",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "Gold at H1 resistance; DXY strong; expect rejection",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 72,
        "regime_assumed": "TRENDING_BEARISH",
    },
    "entry": {
        "direction": "SELL",
        "volume": 0.02,
        "conditions": [
            {"type": "price_above", "level": 4730.0},
            {"type": "rsi", "tf": "H1", "op": "above", "threshold": 70},
        ],
        "initial_sl": 4740.0,
        "initial_tp": 4710.0,
    },
    "management": [
        {
            "name": "lock_10_at_support",
            "priority": 7,
            "conditions": [{"type": "price_below", "level": 4720.0}],
            "action": {"type": "move_sl_to_price", "price": 4727.0},
            "fires": "once",
            "guards": {"only_if_tighter_sl": True, "cooldown_seconds": 60},
        }
    ],
    "exit": [
        {
            "name": "rejection_exit",
            "priority": 9,
            "conditions": [{"type": "price_above", "level": 4733.0}],
            "action": {"type": "close_full"},
            "fires": "once",
        },
        {
            "name": "time_stop",
            "priority": 3,
            "conditions": [
                {"type": "duration_exceeds", "minutes": 240},
                {"type": "profit_pips", "op": "below", "threshold": 10},
            ],
            "action": {"type": "close_full"},
            "fires": "once",
        },
    ],
    "emergency": {
        "max_loss_pips": 150,
        "max_duration_minutes": 480,
        "on_broker_error": "alert_floki",
    },
}


@pytest.fixture
def valid_plan_dict() -> dict[str, Any]:
    """Return a deep-copied canonical valid plan dict.

    Tests may freely mutate the returned dict; each invocation yields a
    fresh copy so test isolation holds.
    """
    return deepcopy(_BASE_PLAN)


@pytest.fixture
def patch_plan(valid_plan_dict):
    """Helper: return a patcher that overlays `overrides` onto the base plan.

    Example:
        def test_foo(patch_plan):
            plan = patch_plan(entry={"direction": "BUY", "volume": 0.01,
                                     "conditions": [...], "initial_sl": 4700,
                                     "initial_tp": 4750})
    """
    def _patch(**overrides) -> dict[str, Any]:
        out = deepcopy(valid_plan_dict)
        for k, v in overrides.items():
            out[k] = v
        return out
    return _patch
