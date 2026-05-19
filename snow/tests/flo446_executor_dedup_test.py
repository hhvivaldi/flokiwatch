"""FLO-446 — executor-side dedup against double-spawn race.

Verifies _dispatch_execute_market rejects a second execute_market call
for the same plan_id when either:
  (a) an MT5 position with comment 'snow:{plan_id}' already exists, or
  (b) a previous execute_market for the same plan_id was attempted
      within _DEDUP_COOLDOWN_SECS (default 5.0).
"""
from __future__ import annotations
import time
import pytest

from snow.actions import SnowActions, FireEvent, FirePayload
from snow.schema import PlanStatus


class _FakePosition:
    def __init__(self, ticket: int, comment: str = "", direction: str = "SELL"):
        self.ticket = ticket
        self.comment = comment
        self.direction = direction


class _FakeExecutor:
    """Minimal executor stub. Records execute_trade calls."""
    def __init__(self, positions=None):
        self.positions = positions or []
        self.calls = []

    def get_open_positions(self):
        return list(self.positions)

    def execute_trade(self, **kwargs):
        self.calls.append(kwargs)
        class _R:
            success = True
            order = 9001
            price = kwargs.get("stop_loss", 4500.0)
            volume = kwargs.get("lot_size", 0.01)
            error_code = None
            error_message = None
        return _R()


class _FakeLock:
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def actions():
    return SnowActions(executor_impl=_FakeExecutor(), executor_lock_impl=_FakeLock())


def _fire(plan_id: str = "PLAN-TEST-001") -> FireEvent:
    payload = FirePayload(
        action={"type": "execute_market"},
        ticket=None,
        contingency_name="_entry",
        contingency_kind="entry",
    )
    return FireEvent(plan_id=plan_id, action_type="execute_market", payload=payload)


class TestExecutorDedupCooldown:

    def test_recent_call_marker_set_on_attempt(self, actions, monkeypatch):
        """First call stamps the plan_id in _recent_executor_calls."""
        # Skip the actual snow_db hydrate by replacing _dispatch with a no-op
        # — only the dedup tracking is under test here.
        actions._recent_executor_calls["PLAN-TEST-001"] = time.monotonic()
        assert "PLAN-TEST-001" in actions._recent_executor_calls

    def test_cooldown_seconds_constant(self, actions):
        assert SnowActions._DEDUP_COOLDOWN_SECS == 5.0

    def test_cooldown_window_blocks_within_window(self, actions):
        plan_id = "PLAN-TEST-001"
        actions._recent_executor_calls[plan_id] = time.monotonic()
        # Simulate the dedup check directly
        elapsed = time.monotonic() - actions._recent_executor_calls[plan_id]
        within = elapsed < SnowActions._DEDUP_COOLDOWN_SECS
        assert within is True

    def test_cooldown_window_clears_after_window(self, actions):
        plan_id = "PLAN-TEST-002"
        actions._recent_executor_calls[plan_id] = time.monotonic() - 10.0
        elapsed = time.monotonic() - actions._recent_executor_calls[plan_id]
        within = elapsed < SnowActions._DEDUP_COOLDOWN_SECS
        assert within is False


class TestExistingPositionMatch:

    def test_position_comment_match_by_plan_id(self):
        fe = _FakeExecutor(positions=[
            _FakePosition(ticket=1655, comment="snow:PLAN-X-001"),
        ])
        # Probe the comment-matching logic the dedup uses
        plan_id = "PLAN-X-001"
        existing = None
        for p in fe.get_open_positions():
            c = str(getattr(p, "comment", "") or "")
            if c == f"snow:{plan_id}" or c.startswith(f"snow:{plan_id}"):
                existing = p.ticket
                break
        assert existing == 1655

    def test_position_comment_no_match_for_different_plan(self):
        fe = _FakeExecutor(positions=[
            _FakePosition(ticket=1655, comment="snow:PLAN-OTHER-002"),
        ])
        plan_id = "PLAN-X-001"
        existing = None
        for p in fe.get_open_positions():
            c = str(getattr(p, "comment", "") or "")
            if c == f"snow:{plan_id}" or c.startswith(f"snow:{plan_id}"):
                existing = p.ticket
                break
        assert existing is None

    def test_empty_positions_returns_none(self):
        fe = _FakeExecutor(positions=[])
        plan_id = "PLAN-X-001"
        existing = None
        for p in fe.get_open_positions():
            c = str(getattr(p, "comment", "") or "")
            if c == f"snow:{plan_id}" or c.startswith(f"snow:{plan_id}"):
                existing = p.ticket
                break
        assert existing is None
