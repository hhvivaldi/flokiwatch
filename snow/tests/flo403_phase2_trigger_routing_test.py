"""FLO-403 Phase 2 Step 3+4+5 — trigger routing partition + Floki
decommission + caller-aware Snow guard (Q10.1 Option A).

These tests run as source-inspection contracts on main.py / agent_prompts.py /
ai_agent.py — same pattern as Phase 1's flo403_phase1_test.py. They lock the
literal partition + the literal decommission + the caller_role parameter
shape so a future refactor that moves the gate without preserving its
semantics fails fast.

Plus integration-style tests on the caller-aware guard at the agent_tools
layer: TM bypasses the guard, Floki remains blocked.
"""
from __future__ import annotations

import inspect
import os
from unittest.mock import MagicMock

import pytest


# =============================================================================
# Helpers
# =============================================================================


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
)))


def _read_file(rel_path: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


class _FakePosition:
    def __init__(self, *, ticket: int, comment: str = "",
                 sl: float = 0.0, tp: float = 0.0, type_: int = 0):
        self.ticket = ticket
        self.comment = comment
        self.sl = sl
        self.tp = tp
        self.type = type_


# =============================================================================
# Step 3 — main.py trigger routing partition
# =============================================================================


class TestTriggerPartition:
    """The allowed-set partition lives in main.py — read source and
    assert the literal sets match the design directive."""

    def _allowed_block(self) -> str:
        src = _read_file("main.py")
        idx = src.find("floki_allowed = {")
        assert idx >= 0, "FLO-403 Phase 2: floki_allowed set missing in main.py"
        # Read until the dispatch line — the partition spans floki_allowed +
        # tm_allowed + the gate, ~10 lines.
        end = src.find("if tt not in floki_allowed:", idx)
        assert end >= 0, "FLO-403 Phase 2: floki_allowed gate line missing"
        return src[idx:end + 100]

    # --- Floki-allowed contents ---

    def test_floki_allowed_contains_scheduled(self):
        block = self._allowed_block()
        assert '"SCHEDULED"' in block

    def test_floki_allowed_contains_simba_exit_executed(self):
        block = self._allowed_block()
        assert '"SIMBA_EXIT_EXECUTED"' in block

    def test_floki_allowed_contains_plan_terminal(self):
        """Phase 2 NEW trigger — Floki notification on plan close."""
        block = self._allowed_block()
        assert '"PLAN_TERMINAL"' in block

    # --- TM-allowed contents ---

    def test_tm_allowed_contains_simba_wake(self):
        block = self._allowed_block()
        assert '"SIMBA_WAKE"' in block

    def test_tm_allowed_contains_simba_watch(self):
        block = self._allowed_block()
        assert '"SIMBA_WATCH"' in block

    def test_tm_allowed_contains_pending_fill(self):
        """Moved from Floki under Phase 2 per CEO directive."""
        block = self._allowed_block()
        assert '"PENDING_FILL"' in block

    def test_tm_allowed_contains_tm_check(self):
        block = self._allowed_block()
        assert '"TM_CHECK"' in block

    def test_tm_allowed_contains_tm_heartbeat(self):
        block = self._allowed_block()
        assert '"TM_HEARTBEAT"' in block

    # --- Echo CRITICAL still absent (FLO-90 regression guard) ---

    def test_echo_critical_still_absent(self):
        block = self._allowed_block()
        assert '"ECHO_CRITICAL"' not in block, (
            "FLO-90 regression — ECHO_CRITICAL re-introduced into "
            "trigger routing. Echo is pull-only via tool."
        )


class TestDispatcherShape:
    """The dispatcher method must exist + import the singleton getter."""

    def test_dispatch_to_trade_manager_exists(self):
        src = _read_file("main.py")
        assert "def _dispatch_to_trade_manager" in src

    def test_dispatcher_calls_get_trade_manager(self):
        src = _read_file("main.py")
        idx = src.find("def _dispatch_to_trade_manager")
        end = src.find("def ", idx + 10)
        body = src[idx:end] if end > 0 else src[idx:]
        assert "get_trade_manager" in body

    def test_dispatcher_handles_uninitialized(self):
        """If TM init failed, dispatcher must return tm_not_initialized
        (not crash). The trigger source — Simba in another thread —
        sees the dict and discards. Defensive contract."""
        src = _read_file("main.py")
        idx = src.find("def _dispatch_to_trade_manager")
        end = src.find("def ", idx + 10)
        body = src[idx:end] if end > 0 else src[idx:]
        assert '"tm_not_initialized"' in body


# =============================================================================
# Step 4 — Floki decommission (agent_prompts.py + ai_agent.py)
# =============================================================================


class TestAgentPromptsDecommission:
    def _decisions_block(self) -> str:
        src = _read_file("agent_prompts.py")
        idx = src.find("<decisions>")
        end = src.find("</decisions>", idx)
        assert idx >= 0 and end >= 0
        return src[idx:end + len("</decisions>")]

    def test_decisions_block_lists_only_four(self):
        """Floki's decision schema must be exactly OPEN_BUY, OPEN_SELL,
        WAIT, REJECT — no HOLD/ADJUST/CLOSE. The first sentence of the
        block locks the contract."""
        block = self._decisions_block()
        # Find the line that enumerates the decisions
        first_line = block.split("\n")[1] if len(block.split("\n")) > 1 else block
        # Must list the four
        assert "OPEN_BUY" in first_line
        assert "OPEN_SELL" in first_line
        assert "WAIT" in first_line
        assert "REJECT" in first_line
        # Must NOT list the three retired
        assert "HOLD_TRADE" not in first_line, (
            "FLO-403 Phase 2: HOLD_TRADE still listed in <decisions> — "
            "Trade Manager owns this now"
        )
        assert "ADJUST_TRADE" not in first_line
        assert "CLOSE_TRADE" not in first_line

    def test_block_documents_trade_manager(self):
        """The block must explain the Phase 2 architecture so a future
        prompt-engineering pass can't accidentally re-add the retired
        decisions."""
        block = self._decisions_block()
        assert "Trade Manager" in block
        assert "FLO-403" in block

    def test_cancel_plan_escape_valve_documented(self):
        """cancel_plan stays as Floki's escape valve — must be visible
        in the prompt so Floki uses it instead of trying to call
        close_trade (which is now off his roster)."""
        block = self._decisions_block()
        assert "cancel_plan" in block


class TestAiAgentToolRosterDecommission:
    def test_close_trade_removed_from_tool_definitions(self):
        """The decommission MUST drop close_trade / adjust_trade from
        the OpenAI tool-definition list (which is what Floki's API
        request actually carries — controls what the LLM can dispatch).

        NOT the same as `_SINGLETON_TOOLS` — that set classifies tools
        for concurrent-dispatch handling (defense-in-depth). The
        SINGLETON entries are dormant if the tool-definition list
        doesn't include the tool, since Floki can't call what isn't
        in his roster. The roster is the gate; SINGLETON is the
        race-condition guard if the roster ever puts it back.

        Discriminator: the OpenAI tool def signature
        (`"description": "Close a trade by ticket"`) is unique to the
        tool definition block — that's the correct check."""
        src = _read_file("ai_agent.py")
        assert '"description": "Close a trade by ticket"' not in src, (
            "FLO-403 Phase 2 Step 4: close_trade tool definition still "
            "in ai_agent.py — Floki's API request still carries it as "
            "a callable tool"
        )
        assert '"description": "Adjust SL/TP of an open trade.' not in src, (
            "FLO-403 Phase 2 Step 4: adjust_trade tool definition still "
            "in ai_agent.py"
        )

    def test_close_trade_definition_removed(self):
        """The OpenAI-tool definition block (~lines 1137-1144) must be
        gone. We grep for the unique signature 'Close a trade by ticket'."""
        src = _read_file("ai_agent.py")
        assert '"description": "Close a trade by ticket"' not in src, (
            "FLO-403 Phase 2 Step 4: close_trade tool definition still "
            "present in ai_agent.py — Floki sees it in his tool roster"
        )

    def test_adjust_trade_definition_removed(self):
        src = _read_file("ai_agent.py")
        assert '"description": "Adjust SL/TP of an open trade.' not in src, (
            "FLO-403 Phase 2 Step 4: adjust_trade tool definition still "
            "present in ai_agent.py"
        )

    def test_execute_trade_preserved(self):
        """OPEN_BUY / OPEN_SELL still need execute_trade — preserved."""
        src = _read_file("ai_agent.py")
        assert '"execute_trade",' in src

    def test_cancel_plan_preserved(self):
        """Floki's escape valve."""
        src = _read_file("ai_agent.py")
        assert '"cancel_plan",' in src


# =============================================================================
# Step 5 — Caller-aware Snow ownership guard (Q10.1 Option A)
# =============================================================================


def _make_tools_with_position(*, ticket: int, comment: str):
    """Build an AgentTools with a mocked executor that returns one
    position with the given comment. Stub _log_tool / _safe_float etc."""
    from agent_tools import AgentTools
    fake_exec = MagicMock()
    fake_exec.get_open_positions.return_value = [
        _FakePosition(ticket=ticket, comment=comment, sl=4500.0, tp=4400.0),
    ]
    fake_exec.close_position.return_value = MagicMock(
        success=True, price=4500.0, error_message=None,
    )
    fake_exec.modify_position.return_value = MagicMock(
        success=True, error_message=None,
    )
    tools = AgentTools.__new__(AgentTools)
    tools._executor = fake_exec
    tools._log_tool = lambda *a, **kw: None
    tools._log_fail = lambda *a, **kw: None
    tools._safe_float = staticmethod(
        lambda v: float(v) if v is not None else None
    )
    return tools, fake_exec


class TestCallerAwareGuard_CloseTrade:
    def test_floki_default_blocks_snow_position(self):
        """Default caller_role='floki' (no arg passed) — Phase 1 guard
        still fires on snow:* positions. This is the regression check
        that Phase 2 didn't accidentally weaken Phase 1."""
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.close_trade(999)  # NO caller_role arg
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        fake_exec.close_position.assert_not_called()

    def test_floki_explicit_role_blocks_snow_position(self):
        """Explicit caller_role='floki' — same outcome."""
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.close_trade(999, caller_role="floki")
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        fake_exec.close_position.assert_not_called()

    def test_trade_manager_role_bypasses_guard(self):
        """Q10.1 Option A core test: caller_role='trade_manager'
        bypasses the Snow guard — TM IS the authorized executor."""
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.close_trade(999, caller_role="trade_manager")
        assert result["success"] is True
        fake_exec.close_position.assert_called_once_with(999)

    def test_trade_manager_role_floki_position_still_works(self):
        """TM can close non-Snow positions too (pre-existing tickets,
        recovered orphans, etc.) — guard not in the picture either way."""
        tools, fake_exec = _make_tools_with_position(
            ticket=777, comment="floki",
        )
        result = tools.close_trade(777, caller_role="trade_manager")
        assert result["success"] is True
        fake_exec.close_position.assert_called_once_with(777)

    def test_unknown_role_treated_as_floki(self):
        """Defensive: any caller_role other than 'trade_manager' → treat
        as Floki (block on snow:*). Catches typos / future role
        introduction without explicit allowlist update."""
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.close_trade(999, caller_role="unknown_role")
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        fake_exec.close_position.assert_not_called()


class TestCallerAwareGuard_AdjustTrade:
    def test_floki_default_blocks_snow_position(self):
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.adjust_trade(999, new_sl=4490.0, new_tp=4410.0)
        assert result["success"] is False
        assert result["reason"] == "snow_owned"
        fake_exec.modify_position.assert_not_called()

    def test_trade_manager_role_bypasses_guard(self):
        tools, fake_exec = _make_tools_with_position(
            ticket=999, comment="snow:PLAN-X",
        )
        result = tools.adjust_trade(
            999, new_sl=4490.0, new_tp=4410.0,
            caller_role="trade_manager",
        )
        assert result["success"] is True
        fake_exec.modify_position.assert_called_once()


class TestCallerAwareGuard_SourceContract:
    """The caller_role parameter must appear in both signatures.
    Source-inspection lock so a refactor that strips the param fails fast."""

    def test_close_trade_signature_has_caller_role(self):
        from agent_tools import AgentTools
        sig = inspect.signature(AgentTools.close_trade)
        assert "caller_role" in sig.parameters
        assert sig.parameters["caller_role"].default == "floki"

    def test_adjust_trade_signature_has_caller_role(self):
        from agent_tools import AgentTools
        sig = inspect.signature(AgentTools.adjust_trade)
        assert "caller_role" in sig.parameters
        assert sig.parameters["caller_role"].default == "floki"


class TestTradeManagerToolsPassesRole:
    """TradeManagerTools.close_trade / adjust_trade must pass
    caller_role='trade_manager' to the underlying AgentTools call,
    otherwise the bypass is wired wrong."""

    def test_close_trade_passes_trade_manager_role(self):
        from trade_manager_tools import TradeManagerTools
        floki = MagicMock()
        floki.close_trade.return_value = {"success": True}
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        tools.close_trade(999, reason="thesis broken")
        floki.close_trade.assert_called_once_with(
            999, caller_role="trade_manager",
        )

    def test_adjust_trade_passes_trade_manager_role(self):
        from trade_manager_tools import TradeManagerTools
        floki = MagicMock()
        floki.adjust_trade.return_value = {"success": True}
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        tools.adjust_trade(999, new_sl=4490.0, new_tp=4530.0, reason="trail")
        floki.adjust_trade.assert_called_once_with(
            999, new_sl=4490.0, new_tp=4530.0, caller_role="trade_manager",
        )


# =============================================================================
# Singleton init contract
# =============================================================================


class TestTradeManagerSingleton:
    def test_get_trade_manager_returns_none_before_init(self, monkeypatch):
        """Cold singleton — no init call yet. Returns None so the
        trigger router falls through to tm_not_initialized."""
        import trade_manager
        monkeypatch.setattr(trade_manager, "_tm_instance", None)
        assert trade_manager.get_trade_manager() is None

    def test_initialize_returns_true_on_success(self, monkeypatch):
        import trade_manager
        monkeypatch.setattr(trade_manager, "_tm_instance", None)
        ok = trade_manager.initialize_trade_manager(executor=MagicMock())
        assert ok is True
        assert trade_manager.get_trade_manager() is not None
        # Reset for test isolation
        monkeypatch.setattr(trade_manager, "_tm_instance", None)

    def test_initialize_returns_false_on_exception(self, monkeypatch):
        """If TradeManager constructor raises (e.g. config import fails),
        initialize returns False without crashing the bot startup."""
        import trade_manager
        monkeypatch.setattr(trade_manager, "_tm_instance", None)

        def _broken(*a, **kw):
            raise RuntimeError("forced failure")

        monkeypatch.setattr(trade_manager, "TradeManager", _broken)
        ok = trade_manager.initialize_trade_manager(executor=MagicMock())
        assert ok is False
        assert trade_manager.get_trade_manager() is None
