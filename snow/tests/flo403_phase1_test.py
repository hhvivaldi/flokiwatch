"""FLO-403 Phase 1 — atomic test suite for the three-component ship:

  1. Snow ownership guard on close_trade / adjust_trade (agent_tools.py).
  2. set_next_check 30-min default floor / 10-min only when no plan AND
     no position (agent_tools.py:43).
  3. Simba trigger cleanup — SIMBA_WAKE / SIMBA_WATCH dropped from
     main.py:339 allowed-set; SCHEDULED / PENDING_FILL /
     SIMBA_EXIT_EXECUTED preserved.

The Phase 1 changes carry no model swap. Floki stays on Qwen / Gemini
(per LLM_PROVIDER); the three components are wholly mechanical guards
on existing tool surfaces. Phase 2 (Trade Manager Agent) inherits this
floor.

Test discipline mirrors FLO-401's contract-lock pattern (snow/tests/
flo401_test.py): each component gets a class with positive AND negative
cases. Source-inspection tests lock the gate at the literal line in
main.py / agent_tools.py so a future refactor that moves the check
without preserving its semantics fails fast.
"""
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest


# =============================================================================
# Helpers
# =============================================================================


class _FakePosition:
    """Minimal duck-type for executor.get_open_positions() entries.
    We only need .ticket and .comment for the ownership guard."""
    def __init__(self, ticket: int, comment: str = "", sl: float = 0.0,
                 tp: float = 0.0, type_: int = 0):
        self.ticket = ticket
        self.comment = comment
        self.sl = sl
        self.tp = tp
        self.type = type_


def _make_tools_with_positions(*positions):
    """Build an AgentTools instance with a mocked executor whose
    get_open_positions returns the given positions, and stubs every
    other method we need to keep close_trade / adjust_trade non-crashy."""
    from agent_tools import AgentTools
    fake_exec = MagicMock()
    fake_exec.get_open_positions.return_value = list(positions)
    # close_position / modify_position should NOT be called when guard
    # blocks; failing the test loudly if they are.
    fake_exec.close_position.side_effect = AssertionError(
        "close_position must not be called on a Snow-owned ticket"
    )
    fake_exec.modify_position.side_effect = AssertionError(
        "modify_position must not be called on a Snow-owned ticket"
    )
    tools = AgentTools.__new__(AgentTools)
    tools._executor = fake_exec
    # Minimum surface for the logging path in close_trade / adjust_trade.
    tools._log_tool = lambda *a, **kw: None
    tools._log_fail = lambda *a, **kw: None
    tools._safe_float = staticmethod(
        lambda v: float(v) if v is not None else None
    )
    return tools, fake_exec


# =============================================================================
# Component 1 — Snow ownership guard
# =============================================================================


class TestSnowOwnershipGuard_CloseTrade:
    """close_trade must refuse Snow-owned positions and route Floki to
    cancel_plan. The position is Snow-owned IFF its MT5 comment starts
    with `snow:` (FLO-361 marker, set by snow/actions.py:224)."""

    def test_blocks_snow_owned_position(self):
        tools, fake_exec = _make_tools_with_positions(
            _FakePosition(ticket=999, comment="snow:PLAN-20260429-008"),
        )
        result = tools.close_trade(999)
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        assert "cancel_plan" in result["hint"]
        # close_position must not have been invoked (mock would have
        # raised AssertionError if it was).
        fake_exec.close_position.assert_not_called()

    def test_allows_floki_owned_position(self):
        """Position with non-snow comment passes through to executor."""
        tools, fake_exec = _make_tools_with_positions(
            _FakePosition(ticket=777, comment="floki_manual"),
        )
        # Replace the AssertionError side_effect with a successful result.
        fake_exec.close_position.side_effect = None
        fake_exec.close_position.return_value = MagicMock(
            success=True, price=4500.0, error_message=None,
        )
        result = tools.close_trade(777)
        assert result["success"] is True
        fake_exec.close_position.assert_called_once_with(777)

    def test_allows_unrelated_snow_ticket_with_floki_target(self):
        """Two open positions, one Snow-owned, one Floki-owned. Closing
        the Floki ticket must succeed even though a Snow ticket exists
        in the inventory — the guard checks the SPECIFIC ticket."""
        tools, fake_exec = _make_tools_with_positions(
            _FakePosition(ticket=111, comment="snow:PLAN-X"),
            _FakePosition(ticket=222, comment=""),  # Floki-owned
        )
        fake_exec.close_position.side_effect = None
        fake_exec.close_position.return_value = MagicMock(
            success=True, price=4500.0, error_message=None,
        )
        result = tools.close_trade(222)
        assert result["success"] is True
        fake_exec.close_position.assert_called_once_with(222)

    def test_failure_safe_when_position_lookup_raises(self):
        """If get_open_positions raises (MT5 disconnect, executor down),
        the guard MUST default to NOT-snow-owned (false negative over
        false positive — see FLO-403 §E rationale). The dominant safety
        net is the existing close_position path itself."""
        tools, fake_exec = _make_tools_with_positions()
        fake_exec.get_open_positions.side_effect = RuntimeError("MT5 down")
        fake_exec.close_position.side_effect = None
        fake_exec.close_position.return_value = MagicMock(
            success=True, price=0.0, error_message=None,
        )
        result = tools.close_trade(999)
        # Did NOT block — close_position was called.
        fake_exec.close_position.assert_called_once_with(999)
        assert result["success"] is True


class TestSnowOwnershipGuard_AdjustTrade:
    """adjust_trade must refuse Snow-owned positions: SL/TP changes for
    Snow-owned trades must come from the plan's contingencies, not
    Floki's free-hand adjustment."""

    def test_blocks_snow_owned_position(self):
        tools, fake_exec = _make_tools_with_positions(
            _FakePosition(
                ticket=999, comment="snow:PLAN-20260429-008",
                sl=4500.0, tp=4400.0, type_=0,
            ),
        )
        result = tools.adjust_trade(999, new_sl=4490.0, new_tp=4405.0)
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        assert "cancel_plan" in result["hint"]
        fake_exec.modify_position.assert_not_called()

    def test_allows_floki_owned_position(self):
        tools, fake_exec = _make_tools_with_positions(
            _FakePosition(
                ticket=777, comment="floki_pivot_setup",
                sl=4500.0, tp=4400.0, type_=0,
            ),
        )
        fake_exec.modify_position.side_effect = None
        fake_exec.modify_position.return_value = MagicMock(
            success=True, error_message=None,
        )
        # adjust_trade has more downstream logic (db_writer record)
        # which we don't need to exercise; allow it to silently no-op.
        result = tools.adjust_trade(777, new_sl=4495.0, new_tp=4405.0)
        assert result["success"] is True
        fake_exec.modify_position.assert_called_once()


class TestSnowOwnershipGuard_SourceContract:
    """Source-inspection lock: the literal `snow:` prefix check + the
    `snow_owned` reason must be present in close_trade and adjust_trade.
    A future refactor that moves the guard without preserving its
    semantics fails fast here."""

    def test_close_trade_carries_guard(self):
        from agent_tools import AgentTools
        src = inspect.getsource(AgentTools.close_trade)
        assert 'startswith("snow:")' in src
        assert '"snow_owned"' in src
        assert "cancel_plan" in src

    def test_adjust_trade_carries_guard(self):
        from agent_tools import AgentTools
        src = inspect.getsource(AgentTools.adjust_trade)
        assert 'startswith("snow:")' in src
        assert '"snow_owned"' in src
        assert "cancel_plan" in src


# =============================================================================
# Component 2 — set_next_check 30-min floor
# =============================================================================


class TestSetNextCheckFloor:
    """30-min default floor; 10-min only when both stores are POSITIVELY
    confirmed empty. Conservative direction: any lookup failure keeps
    the 30-min floor."""

    def _make_tools(self, monkeypatch, *, no_plan: bool, no_position: bool,
                    plan_raises=False, position_raises=False):
        """Build an AgentTools with stubbed snow.db.list_plans_by_status
        and executor.get_open_positions to drive the four states."""
        from agent_tools import AgentTools
        from snow import db as snow_db

        tools = AgentTools.__new__(AgentTools)
        tools._safe_int = staticmethod(
            lambda v: int(v) if v is not None else None
        )
        tools._log_tool = lambda *a, **kw: None
        tools._log_fail = lambda *a, **kw: None
        tools._next_check_path = lambda: "/tmp/_test_next_check.json"
        tools._write_json_atomic = staticmethod(lambda *a, **kw: True)

        fake_exec = MagicMock()
        if position_raises:
            fake_exec.get_open_positions.side_effect = RuntimeError("MT5 down")
        else:
            fake_exec.get_open_positions.return_value = (
                [] if no_position else [_FakePosition(1)]
            )
        tools._executor = fake_exec

        if plan_raises:
            monkeypatch.setattr(
                snow_db, "list_plans_by_status",
                lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("DB down")),
            )
        else:
            monkeypatch.setattr(
                snow_db, "list_plans_by_status",
                lambda *a, **kw: [] if no_plan else [{"id": "PLAN-X"}],
            )
        return tools

    def test_floor_30_when_position_open(self, monkeypatch):
        """Position exists → floor=60 (FLO-419 raised from 30). Floki
        cannot fast-iterate while a trade is in flight (TM manages it)."""
        tools = self._make_tools(monkeypatch, no_plan=True, no_position=False)
        result = tools.set_next_check(minutes=5)
        assert result["success"] is True
        assert result["requested_minutes"] == 60

    def test_floor_30_when_plan_pending(self, monkeypatch):
        """Pending plan exists → floor=60 (FLO-419). Snow is watching
        for entry; Floki doesn't need to re-author."""
        tools = self._make_tools(monkeypatch, no_plan=False, no_position=True)
        result = tools.set_next_check(minutes=5)
        assert result["requested_minutes"] == 60

    def test_floor_10_when_no_plan_and_no_position(self, monkeypatch):
        """Fresh-authoring window: nothing in flight, allow 10-min for
        faster iteration after market open or plan cancel."""
        tools = self._make_tools(monkeypatch, no_plan=True, no_position=True)
        result = tools.set_next_check(minutes=5)
        assert result["requested_minutes"] == 10

    def test_floor_10_respects_request_above_floor(self, monkeypatch):
        """Floor is a minimum, not a target. 45-min request stands."""
        tools = self._make_tools(monkeypatch, no_plan=True, no_position=True)
        result = tools.set_next_check(minutes=45)
        assert result["requested_minutes"] == 45

    def test_inverted_default_db_failure_keeps_30(self, monkeypatch):
        """CTO-flagged inversion: if the snow.db lookup raises, the
        conservative default is 'plan exists' — floor stays 60 (FLO-419,
        was 30). Failing the other way (defaulting to 'no plan' on
        error) would let a single transient DB error open the 10-min
        path during an active trade window."""
        tools = self._make_tools(
            monkeypatch, no_plan=True, no_position=True, plan_raises=True,
        )
        result = tools.set_next_check(minutes=5)
        assert result["requested_minutes"] == 60

    def test_inverted_default_position_failure_keeps_30(self, monkeypatch):
        """Same protection on the executor side."""
        tools = self._make_tools(
            monkeypatch, no_plan=True, no_position=True, position_raises=True,
        )
        result = tools.set_next_check(minutes=5)
        assert result["requested_minutes"] == 60

    def test_both_failures_keeps_30(self, monkeypatch):
        """Belt and braces: if BOTH lookups fail, both flip to
        conservative — 60-min floor (FLO-419)."""
        tools = self._make_tools(
            monkeypatch, no_plan=True, no_position=True,
            plan_raises=True, position_raises=True,
        )
        result = tools.set_next_check(minutes=5)
        assert result["requested_minutes"] == 60


class TestSetNextCheckFloor_SourceContract:
    """Source-inspection lock: the inverted-default fix must be visible
    in the source. `_no_plan = False` (not True) on exception is the
    fingerprint of the conservative direction."""

    def test_source_carries_inverted_default(self):
        from agent_tools import AgentTools
        src = inspect.getsource(AgentTools.set_next_check)
        # Both bools must default to False on exception (= "assume
        # plan/position exists"). If either flips to True, the floor
        # incorrectly drops to 10.
        assert "_no_plan = False" in src, (
            "FLO-403 Phase 1: _no_plan must default to False on lookup "
            "failure (conservative — assume plan exists, keep 30-min floor)"
        )
        assert "_no_position = False" in src, (
            "FLO-403 Phase 1: _no_position must default to False on lookup "
            "failure (conservative — assume position exists, keep 30-min floor)"
        )

    def test_source_carries_floor_constant(self):
        from agent_tools import AgentTools
        src = inspect.getsource(AgentTools.set_next_check)
        # FLO-419 (2026-05-01): default floor raised 30 -> 60. The
        # 10-min fast-iteration path (no plan AND no position) is preserved.
        assert "_floor = 60" in src
        assert "_floor = 10" in src


# =============================================================================
# Component 3 — Simba trigger cleanup at main.py:339
# =============================================================================


class TestSimbaTriggerCleanup_SourceContract:
    """The allowed-trigger set lives in main.py:339. We don't import
    main.py wholesale (it boots a real bot); we read the source and
    assert the literal allowed-set."""

    @staticmethod
    def _read_main():
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))),
            "main.py",
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_simba_wake_removed(self):
        """SIMBA_WAKE must NOT appear in the allowed-trigger literal."""
        src = self._read_main()
        # Locate the allowed-set assignment (handles future formatting).
        # The block we're locking starts with `allowed = {`.
        idx = src.find("allowed = {")
        assert idx >= 0, "allowed-set assignment not found in main.py"
        end = src.find("}", idx)
        block = src[idx:end + 1]
        assert "SIMBA_WAKE" not in block, (
            "FLO-403 Phase 1: SIMBA_WAKE must be removed from main.py "
            "allowed-trigger set. Current block:\n" + block
        )

    def test_simba_watch_removed(self):
        src = self._read_main()
        idx = src.find("allowed = {")
        end = src.find("}", idx)
        block = src[idx:end + 1]
        assert "SIMBA_WATCH" not in block, (
            "FLO-403 Phase 1: SIMBA_WATCH must be removed from main.py "
            "allowed-trigger set. Current block:\n" + block
        )

    def test_scheduled_pending_fill_exit_executed_preserved(self):
        """Phase 1 → Phase 2 evolution: SCHEDULED + SIMBA_EXIT_EXECUTED
        stay on Floki throughout. PENDING_FILL was Floki under Phase 1,
        moved to TM under Phase 2 — assertion now scoped to the
        floki_allowed set (Phase 2 partition). This test was originally
        a Phase 1 contract; the partition rename is covered by the
        Phase 2 trigger-routing test file."""
        src = self._read_main()
        # Phase 2 introduced floki_allowed = {...} naming; Phase 1
        # used allowed = {...}. Match either to keep the test stable
        # across phases.
        for pat in ("floki_allowed = {", "allowed = {"):
            idx = src.find(pat)
            if idx >= 0:
                break
        assert idx >= 0
        end = src.find("}", idx)
        block = src[idx:end + 1]
        assert "SCHEDULED" in block
        assert "SIMBA_EXIT_EXECUTED" in block
        # PENDING_FILL is now in tm_allowed (Phase 2) — no longer in
        # the Floki block. Test it from the phase-aware location.
        # See snow/tests/flo403_phase2_trigger_routing_test.py for the
        # canonical PENDING_FILL → TM contract.

    def test_echo_critical_still_absent(self):
        """FLO-90 precedent: ECHO_CRITICAL was removed 37 days ago.
        It should remain absent under FLO-403 Phase 1 — no regression."""
        src = self._read_main()
        idx = src.find("allowed = {")
        end = src.find("}", idx)
        block = src[idx:end + 1]
        assert "ECHO_CRITICAL" not in block, (
            "FLO-90 regression — ECHO_CRITICAL re-introduced into the "
            "allowed-trigger set"
        )


class TestSimbaTriggerCleanup_FLO403Doc:
    """Lock the FLO-403 Phase 1 ticket reference into the comment block
    so future archaeology has a breadcrumb."""

    def test_flo403_comment_present(self):
        import os
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)
            ))),
            "main.py",
        )
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "FLO-403" in src, (
            "FLO-403 Phase 1: ticket reference must appear in main.py "
            "near the allowed-trigger set"
        )
