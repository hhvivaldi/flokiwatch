"""FLO-403 Phase 2 Steps 1+2 — Trade Manager daemon + tools + prompts.

Contract-lock test suite for the three new modules:
  - trade_manager_prompts.py (SYSTEM_PROMPT + build_user_prompt + parser)
  - trade_manager_tools.py (TradeManagerTools class — 6 methods)
  - trade_manager.py (TradeManager daemon — run_cycle)

Phase 2 Steps 1+2 are PURE ADDITIONS: no main.py routing yet, no
Floki decommission yet, no caller-aware guard yet (all Step 5).
TRADE_MANAGER_ENABLED defaults to False — daemon runs in shadow,
never executes. These tests validate the daemon's shape, prompt
discipline, tool surface scope, and the shadow/production dispatch
gating.

Test discipline mirrors FLO-401 / FLO-403 Phase 1 patterns: positive
AND negative cases, source-inspection contracts, defensive failure
modes (lookup raises → conservative default, never crash the cycle).
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# =============================================================================
# Shared fakes
# =============================================================================


class _FakePosition:
    """MT5 position duck-type."""
    def __init__(self, *, ticket: int, comment: str = "",
                 direction: str = "BUY", entry: float = 4500.0,
                 sl: float = 4480.0, tp: float = 4520.0,
                 profit: float = 5.0, type_: int = 0,
                 open_time: float | None = None):
        self.ticket = ticket
        self.comment = comment
        self.direction = direction
        self.open_price = entry
        self.sl = sl
        self.tp = tp
        self.profit = profit
        self.type = type_
        self.volume = 0.05
        self.open_time = open_time


def _make_floki_tools_stub(positions: list | None = None,
                           indicators_per_tf: dict | None = None,
                           regime: str = "TRENDING_BEARISH",
                           echo_alerts: list | None = None):
    """Build a MagicMock that behaves like AgentTools for the surface
    TradeManagerTools touches."""
    stub = MagicMock()
    stub.get_open_positions.return_value = {
        "positions": [
            {
                "ticket": p.ticket, "direction": p.direction,
                "entry": p.open_price, "sl": p.sl, "tp": p.tp,
                "current_pnl": p.profit, "phase": "OPEN",
                "comment": p.comment,
                "managed_by": (
                    "snow" if p.comment.startswith("snow:") else "floki"
                ),
            }
            for p in (positions or [])
        ],
        "count": len(positions or []),
    }
    if indicators_per_tf is None:
        indicators_per_tf = {
            "M5": {
                "rsi": 55.0,
                "stochastic": {"k": 60.0, "d": 58.0},
                "atr": 1.2,
                "recent_closes": [4500.1, 4500.3, 4500.5,
                                  4500.7, 4500.9, 4501.1,
                                  4501.3, 4501.5, 4501.7, 4501.9],
            },
            "M15": {
                "rsi": 52.0,
                "stochastic": {"k": 55.0, "d": 53.0},
                "atr": 2.4,
                "macd": {"histogram": 0.05},
            },
        }
    def _ind(timeframe: str = ""):
        tf = (timeframe or "").upper()
        return indicators_per_tf.get(tf, {})
    stub.get_indicators.side_effect = _ind
    stub.get_market_regime.return_value = {"regime": regime}
    stub.get_echo_alerts.return_value = {"alerts": echo_alerts or []}
    stub.close_trade.return_value = {"success": True}
    stub.adjust_trade.return_value = {"success": True}
    return stub


# =============================================================================
# 1. Decision JSON parser
# =============================================================================


class TestDecisionParser:
    def test_accepts_no_op(self):
        from trade_manager_prompts import parse_decision_json
        out = parse_decision_json('{"decision": "NO_OP", "reason": "fine"}')
        assert out == {"decision": "NO_OP", "reason": "fine"}

    def test_accepts_hold(self):
        from trade_manager_prompts import parse_decision_json
        out = parse_decision_json('{"decision": "HOLD_TRADE", "reason": "stays"}')
        assert out["decision"] == "HOLD_TRADE"

    def test_accepts_close(self):
        from trade_manager_prompts import parse_decision_json
        out = parse_decision_json(
            '{"decision": "CLOSE_TRADE", "reason": "thesis broken"}'
        )
        assert out["decision"] == "CLOSE_TRADE"

    def test_accepts_adjust_with_geometry(self):
        from trade_manager_prompts import parse_decision_json
        out = parse_decision_json(
            '{"decision": "ADJUST_TRADE", "reason": "BE", '
            '"new_sl": 4490.0, "new_tp": 4530.0}'
        )
        assert out["decision"] == "ADJUST_TRADE"
        assert out["new_sl"] == 4490.0
        assert out["new_tp"] == 4530.0

    def test_rejects_adjust_without_geometry(self):
        from trade_manager_prompts import parse_decision_json
        assert parse_decision_json(
            '{"decision": "ADJUST_TRADE", "reason": "x"}'
        ) is None
        assert parse_decision_json(
            '{"decision": "ADJUST_TRADE", "new_sl": 1.0}'
        ) is None
        assert parse_decision_json(
            '{"decision": "ADJUST_TRADE", "new_sl": "bad", "new_tp": 1.0}'
        ) is None

    def test_rejects_unknown_decision(self):
        from trade_manager_prompts import parse_decision_json
        assert parse_decision_json('{"decision": "REJECT"}') is None
        assert parse_decision_json('{"decision": "OPEN_BUY"}') is None
        assert parse_decision_json('{"decision": ""}') is None

    def test_rejects_invalid_json(self):
        from trade_manager_prompts import parse_decision_json
        assert parse_decision_json("not json") is None
        assert parse_decision_json("") is None
        assert parse_decision_json("{") is None
        assert parse_decision_json(None) is None  # type: ignore[arg-type]

    def test_strips_markdown_fence(self):
        """Defensive: Qwen sometimes ignores the no-fence instruction."""
        from trade_manager_prompts import parse_decision_json
        wrapped = '```json\n{"decision": "NO_OP", "reason": "fine"}\n```'
        out = parse_decision_json(wrapped)
        assert out == {"decision": "NO_OP", "reason": "fine"}

    def test_strips_bare_fence(self):
        from trade_manager_prompts import parse_decision_json
        wrapped = '```\n{"decision": "HOLD_TRADE"}\n```'
        out = parse_decision_json(wrapped)
        assert out["decision"] == "HOLD_TRADE"

    def test_reason_truncated(self):
        """200-char cap on reason — defense against runaway LLM prose."""
        from trade_manager_prompts import parse_decision_json
        long_reason = "x" * 500
        out = parse_decision_json(
            json.dumps({"decision": "NO_OP", "reason": long_reason})
        )
        assert len(out["reason"]) == 200


# =============================================================================
# 2. System prompt + user prompt builder
# =============================================================================


class TestSystemPrompt:
    def test_under_500_token_budget(self):
        """Token estimate (chars/4) must clear the 500-token directive."""
        from trade_manager_prompts import SYSTEM_PROMPT
        # rough char/token ratio; design budget is ≤500 tokens
        assert len(SYSTEM_PROMPT) < 500 * 5  # comfortable headroom

    def test_lists_all_four_decisions(self):
        from trade_manager_prompts import SYSTEM_PROMPT
        for d in ("HOLD_TRADE", "ADJUST_TRADE", "CLOSE_TRADE", "NO_OP"):
            assert d in SYSTEM_PROMPT

    def test_default_bias_no_op(self):
        from trade_manager_prompts import SYSTEM_PROMPT
        # The directive principle must be explicit so the LLM doesn't
        # over-fire CLOSE_TRADE on every wobble.
        assert "DEFAULT BIAS" in SYSTEM_PROMPT
        assert "NO_OP" in SYSTEM_PROMPT

    def test_dont_preempt_snow(self):
        from trade_manager_prompts import SYSTEM_PROMPT
        assert "DON'T preempt Snow" in SYSTEM_PROMPT

    def test_dont_author_plans(self):
        from trade_manager_prompts import SYSTEM_PROMPT
        assert "DON'T author" in SYSTEM_PROMPT


class TestUserPromptBuilder:
    def _ctx(self):
        return {
            "position": {
                "ticket": 999, "direction": "BUY", "volume": 0.05,
                "entry": 4500.0, "sl": 4480.0, "tp": 4520.0,
                "age_minutes": 12, "managed_by": "snow",
                "current_pnl": 8.5,
                "mfe_pips": 18.0, "mae_pips": 5.0,
            },
            "plan": {
                "plan_id": "PLAN-20260429-009",
                "thesis": "H1 pullback to 4500 resistance to join bear trend",
                "contingencies_remaining": ["lock_be", "rsi_invalidation"],
            },
            "market": {
                "current_price": 4501.5,
                "regime_changed": False,
                "indicators": {
                    "M5": {"RSI": 55.0, "Stoch_K": 60.0, "Stoch_D": 58.0,
                           "ATR": 1.2,
                           "recent_closes": [4500.1, 4500.5, 4501.0]},
                    "M15": {"RSI": 52.0, "Stoch_K": 55.0, "Stoch_D": 53.0,
                            "MACD_histogram": 0.05, "ATR": 2.4},
                },
                "echo_critical_since_open": [],
            },
            "trigger": {"type": "TM_HEARTBEAT", "data": {}},
        }

    def test_includes_position_block(self):
        from trade_manager_prompts import build_user_prompt
        out = build_user_prompt(self._ctx())
        assert "<position>" in out and "</position>" in out
        assert "ticket: 999" in out
        assert "direction: BUY" in out

    def test_includes_plan_thesis(self):
        from trade_manager_prompts import build_user_prompt
        out = build_user_prompt(self._ctx())
        assert "PLAN-20260429-009" in out
        assert "H1 pullback to 4500 resistance" in out

    def test_includes_management_indicators(self):
        from trade_manager_prompts import build_user_prompt
        out = build_user_prompt(self._ctx())
        for f in ("M5_RSI", "M5_Stoch_K", "M5_Stoch_D",
                  "M15_RSI", "M15_Stoch_K", "M15_MACD_histogram"):
            assert f in out

    def test_includes_regime_changed_bool(self):
        from trade_manager_prompts import build_user_prompt
        out = build_user_prompt(self._ctx())
        assert "regime_changed: False" in out

    def test_excludes_charts_and_higher_tf(self):
        """Lean by directive: no chart references, no D1/H4/H1 fields."""
        from trade_manager_prompts import build_user_prompt
        out = build_user_prompt(self._ctx())
        assert "chart" not in out.lower()
        assert "screenshot" not in out.lower()
        # Higher-TF indicator fields should not appear.
        assert "D1_RSI" not in out
        assert "H4_RSI" not in out
        assert "H1_RSI" not in out

    def test_handles_missing_fields_gracefully(self):
        """Defensive: an indicator fetch raised → field is None →
        prompt renders 'n/a' rather than crashing."""
        from trade_manager_prompts import build_user_prompt
        ctx = self._ctx()
        ctx["market"]["indicators"]["M5"]["RSI"] = None
        ctx["plan"]["thesis"] = None
        out = build_user_prompt(ctx)
        assert "M5_RSI: n/a" in out
        assert "thesis: n/a" in out


# =============================================================================
# 3. TradeManagerTools — wrappers + tool-surface contract
# =============================================================================


class TestTradeManagerTools_OpenPositions:
    def test_passthrough(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub(
            positions=[_FakePosition(ticket=1, comment="snow:PLAN-X")],
        )
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_open_positions()
        assert out["count"] == 1
        assert out["positions"][0]["managed_by"] == "snow"


class TestTradeManagerTools_PositionState:
    def test_managed_by_snow_detection(self):
        from trade_manager_tools import TradeManagerTools
        exec_mock = MagicMock()
        exec_mock.get_open_positions.return_value = [
            _FakePosition(ticket=999, comment="snow:PLAN-X"),
        ]
        floki = _make_floki_tools_stub(positions=[])
        tools = TradeManagerTools(executor=exec_mock, agent_tools=floki)
        out = tools.get_position_state(999)
        assert out["ticket"] == 999
        assert out["managed_by"] == "snow"

    def test_managed_by_floki_detection(self):
        from trade_manager_tools import TradeManagerTools
        exec_mock = MagicMock()
        exec_mock.get_open_positions.return_value = [
            _FakePosition(ticket=999, comment=""),
        ]
        floki = _make_floki_tools_stub(positions=[])
        tools = TradeManagerTools(executor=exec_mock, agent_tools=floki)
        out = tools.get_position_state(999)
        assert out["managed_by"] == "floki"

    def test_missing_position_returns_defaults(self):
        from trade_manager_tools import TradeManagerTools
        exec_mock = MagicMock()
        exec_mock.get_open_positions.return_value = []
        floki = _make_floki_tools_stub(positions=[])
        tools = TradeManagerTools(executor=exec_mock, agent_tools=floki)
        out = tools.get_position_state(999)
        # No crash; returns the stub shape with None / [] defaults.
        assert out["ticket"] == 999
        assert out["plan_id"] is None
        assert out["contingencies_remaining"] == []

    def test_executor_raises_returns_defaults(self):
        from trade_manager_tools import TradeManagerTools
        exec_mock = MagicMock()
        exec_mock.get_open_positions.side_effect = RuntimeError("MT5 down")
        floki = _make_floki_tools_stub(positions=[])
        tools = TradeManagerTools(executor=exec_mock, agent_tools=floki)
        out = tools.get_position_state(999)
        assert out["managed_by"] == "floki"  # default — conservative


class TestTradeManagerTools_ManagementIndicators:
    def test_only_m5_m15_keys(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub()
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_management_indicators()
        assert set(out.keys()) == {"M5", "M15"}

    def test_drops_higher_tf(self):
        """Wrapper must NOT call get_indicators with D1/H4/H1 — those
        fields aren't in the directive's ALLOWED list."""
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub()
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        _ = tools.get_management_indicators()
        called_tfs = {
            call.kwargs.get("timeframe") or (call.args[0] if call.args else "")
            for call in floki.get_indicators.call_args_list
        }
        assert "D1" not in called_tfs
        assert "H4" not in called_tfs
        assert "H1" not in called_tfs

    def test_m5_includes_required_fields(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub()
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_management_indicators()
        m5 = out["M5"]
        assert "RSI" in m5
        assert "Stoch_K" in m5 and "Stoch_D" in m5
        assert "ATR" in m5
        assert "recent_closes" in m5

    def test_m15_includes_macd_histogram(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub()
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_management_indicators()
        m15 = out["M15"]
        assert "MACD_histogram" in m15
        assert m15["MACD_histogram"] == 0.05

    def test_recent_closes_capped_at_10(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub(
            indicators_per_tf={
                "M5": {"recent_closes": list(range(100))},
                "M15": {},
            },
        )
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_management_indicators()
        assert len(out["M5"]["recent_closes"]) == 10
        # Last 10 = oldest-first slice tail
        assert out["M5"]["recent_closes"] == [
            float(x) for x in range(90, 100)
        ]


class TestTradeManagerTools_RegimeStability:
    def _isolated_cache_path(self, tmp_path, monkeypatch):
        """Redirect the regime cache to a tmp file so tests don't
        cross-contaminate."""
        from trade_manager_tools import _REGIME_CACHE_PATH  # noqa
        path = str(tmp_path / "tm_regime.json")
        monkeypatch.setattr(
            "trade_manager_tools._REGIME_CACHE_PATH", path,
        )
        return path

    def test_first_sighting_stamps_at_open(self, tmp_path, monkeypatch):
        from trade_manager_tools import TradeManagerTools
        self._isolated_cache_path(tmp_path, monkeypatch)
        floki = _make_floki_tools_stub(regime="TRENDING_BEARISH")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_regime_stability_flag(999)
        assert out["regime_changed"] is False
        assert out["current"] == "TRENDING_BEARISH"
        assert out["at_open"] == "TRENDING_BEARISH"

    def test_second_call_same_regime(self, tmp_path, monkeypatch):
        from trade_manager_tools import TradeManagerTools
        self._isolated_cache_path(tmp_path, monkeypatch)
        floki = _make_floki_tools_stub(regime="TRENDING_BEARISH")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        _ = tools.get_regime_stability_flag(999)  # stamp
        out = tools.get_regime_stability_flag(999)
        assert out["regime_changed"] is False

    def test_regime_flip_detected(self, tmp_path, monkeypatch):
        from trade_manager_tools import TradeManagerTools
        path = self._isolated_cache_path(tmp_path, monkeypatch)
        floki = _make_floki_tools_stub(regime="TRENDING_BEARISH")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        _ = tools.get_regime_stability_flag(999)
        # Flip the regime upstream
        floki.get_market_regime.return_value = {"regime": "RANGING"}
        out = tools.get_regime_stability_flag(999)
        assert out["regime_changed"] is True
        assert out["at_open"] == "TRENDING_BEARISH"
        assert out["current"] == "RANGING"

    def test_regime_lookup_failure_returns_safe_default(
        self, tmp_path, monkeypatch
    ):
        """Conservative on-error: regime_changed=False (no phantom flip)."""
        from trade_manager_tools import TradeManagerTools
        self._isolated_cache_path(tmp_path, monkeypatch)
        floki = _make_floki_tools_stub()
        floki.get_market_regime.side_effect = RuntimeError("brain down")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_regime_stability_flag(999)
        assert out["regime_changed"] is False

    def test_clear_cache_removes_ticket(self, tmp_path, monkeypatch):
        from trade_manager_tools import TradeManagerTools
        self._isolated_cache_path(tmp_path, monkeypatch)
        floki = _make_floki_tools_stub(regime="TRENDING_BEARISH")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        _ = tools.get_regime_stability_flag(999)
        tools.clear_regime_cache_for_ticket(999)
        floki.get_market_regime.return_value = {"regime": "RANGING"}
        out = tools.get_regime_stability_flag(999)
        # After clear, second call stamps RANGING fresh, not flagged.
        assert out["regime_changed"] is False
        assert out["at_open"] == "RANGING"


class TestTradeManagerTools_EchoCritical:
    def test_filters_to_critical(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub(echo_alerts=[
            {"severity": "CRITICAL", "headline": "Fed shock", "time": "2026-04-29T10:00:00Z"},
            {"severity": "WARNING", "headline": "Routine", "time": "2026-04-29T10:01:00Z"},
            {"severity": "INFO", "headline": "Daily", "time": "2026-04-29T10:02:00Z"},
        ])
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_echo_critical()
        assert len(out) == 1
        assert out[0]["severity"] == "CRITICAL"
        assert out[0]["headline"] == "Fed shock"

    def test_filters_since_iso(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub(echo_alerts=[
            {"severity": "CRITICAL", "headline": "old",
             "time": "2026-04-29T08:00:00Z"},
            {"severity": "CRITICAL", "headline": "new",
             "time": "2026-04-29T11:00:00Z"},
        ])
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        out = tools.get_echo_critical(since_iso="2026-04-29T10:00:00Z")
        assert len(out) == 1
        assert out[0]["headline"] == "new"

    def test_no_alerts_returns_empty(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub(echo_alerts=[])
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        assert tools.get_echo_critical() == []

    def test_lookup_failure_returns_empty(self):
        from trade_manager_tools import TradeManagerTools
        floki = _make_floki_tools_stub()
        floki.get_echo_alerts.side_effect = RuntimeError("echo down")
        tools = TradeManagerTools(executor=MagicMock(), agent_tools=floki)
        assert tools.get_echo_critical() == []


class TestTradeManagerTools_NeverList:
    """Lock the data-surface NEVER list at the tool-class level. The
    LLM cannot call what isn't a method on this class — tests assert
    the absence directly."""

    def test_no_chart_screenshots(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_chart_screenshots")

    def test_no_sr_zones(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_sr_zones")

    def test_no_fibonacci(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_fibonacci_levels")

    def test_no_pivot_points(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_pivot_points")

    def test_no_recipe_book(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_snow_recipe_book")

    def test_no_luna(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_luna_brief")

    def test_no_rex_monitor(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "get_rex_monitor")

    def test_no_submit_plan_to_snow(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "submit_plan_to_snow")

    def test_no_place_pending_order(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "place_pending_order")

    def test_no_execute_trade_open(self):
        from trade_manager_tools import TradeManagerTools
        assert not hasattr(TradeManagerTools, "execute_trade")


# =============================================================================
# 4. TradeManager daemon — run_cycle paths
# =============================================================================


def _make_tm(*, shadow: bool, positions: list | None = None,
             llm_response: str = '{"decision": "NO_OP", "reason": "fine"}'):
    """Build a TradeManager with mocked LLM and stubbed tools."""
    from trade_manager import TradeManager
    exec_mock = MagicMock()
    exec_mock.get_open_positions.return_value = []  # for state lookup
    floki = _make_floki_tools_stub(positions=positions or [])
    tm = TradeManager(
        executor=exec_mock,
        model="qwen3.6-plus",
        api_base="https://example/v1",
        api_key="test-key",
        shadow_mode=shadow,
        agent_tools=floki,
    )
    # Inject mocked LLM call to avoid network.
    tm._call_llm = MagicMock(return_value=llm_response)
    return tm, exec_mock, floki


class TestTradeManager_NoOpenPosition:
    def test_returns_noop_short_circuit(self):
        tm, _, floki = _make_tm(shadow=True, positions=[])
        result = tm.run_cycle("TM_HEARTBEAT", {})
        assert result["success"] is True
        assert result["decision"] == "NO_OP"
        assert result["reason"] == "no_open_position"
        # LLM never called when no position.
        tm._call_llm.assert_not_called()


class TestTradeManager_HappyPath:
    def test_runs_full_cycle(self):
        tm, _, _ = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=999, comment="snow:PLAN-X")],
            llm_response='{"decision": "HOLD_TRADE", "reason": "thesis intact"}',
        )
        result = tm.run_cycle("TM_HEARTBEAT", {})
        assert result["success"] is True
        assert result["decision"] == "HOLD_TRADE"
        tm._call_llm.assert_called_once()


class TestTradeManager_ConcurrencyLock:
    def test_second_call_blocked_during_cycle(self):
        """Per-instance RLock; a SECOND THREAD calling run_cycle while
        the cycle is in flight returns tm_cycle_in_progress. We use a
        separate thread because RLock is re-entrant on the same thread
        (intentional — a re-entrant call from the dispatcher chain
        would otherwise deadlock if main.py ever invokes TM from inside
        a TM-spawned context)."""
        import threading
        from trade_manager import TradeManager
        tm, _, _ = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=1)],
        )

        # Hold the lock in a background thread; the test thread tries
        # run_cycle and must observe the busy reason.
        held = threading.Event()
        release = threading.Event()

        def _hold():
            tm._cycle_lock.acquire()
            try:
                held.set()
                release.wait(timeout=2.0)
            finally:
                tm._cycle_lock.release()

        holder = threading.Thread(target=_hold)
        holder.start()
        try:
            assert held.wait(timeout=1.0) is True
            result = tm.run_cycle("TM_HEARTBEAT", {})
            assert result["success"] is False
            assert result["reason"] == "tm_cycle_in_progress"
            assert result["decision"] == "NO_OP"
            assert result["executed"] is False
        finally:
            release.set()
            holder.join(timeout=2.0)


class TestTradeManager_LLMFailure:
    def test_llm_raises_returns_noop(self):
        tm, _, _ = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=1)],
        )
        tm._call_llm.side_effect = RuntimeError("provider 503")
        result = tm.run_cycle("TM_HEARTBEAT", {})
        assert result["success"] is False
        assert result["decision"] == "NO_OP"
        assert "llm_failed" in result["reason"]
        assert result["executed"] is False


class TestTradeManager_ParseFailure:
    def test_unparseable_response_returns_noop(self):
        tm, _, _ = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=1)],
            llm_response="this is not JSON at all",
        )
        result = tm.run_cycle("TM_HEARTBEAT", {})
        assert result["success"] is False
        assert result["decision"] == "NO_OP"
        assert result["reason"] == "parse_failed"
        assert result["executed"] is False


# =============================================================================
# 5. Shadow vs production dispatch
# =============================================================================


class TestShadowMode:
    def test_close_decision_does_not_execute_in_shadow(self):
        """TRADE_MANAGER_ENABLED=False (shadow): decision logged but
        close_trade NEVER called on the executor or floki tools."""
        tm, _, floki = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=999, comment="snow:PLAN-X")],
            llm_response=(
                '{"decision": "CLOSE_TRADE", "reason": "thesis broken"}'
            ),
        )
        result = tm.run_cycle("TM_CHECK", {"ticket": 999})
        assert result["decision"] == "CLOSE_TRADE"
        assert result["executed"] is False
        floki.close_trade.assert_not_called()

    def test_adjust_decision_does_not_execute_in_shadow(self):
        tm, _, floki = _make_tm(
            shadow=True,
            positions=[_FakePosition(ticket=999, comment="floki")],
            llm_response=(
                '{"decision": "ADJUST_TRADE", "reason": "BE", '
                '"new_sl": 4490.0, "new_tp": 4530.0}'
            ),
        )
        result = tm.run_cycle("TM_CHECK", {"ticket": 999})
        assert result["decision"] == "ADJUST_TRADE"
        assert result["executed"] is False
        floki.adjust_trade.assert_not_called()


class TestProductionMode:
    """When TRADE_MANAGER_ENABLED=True, execute paths actually fire.
    Note: in Steps 1+2 the underlying close_trade/adjust_trade are
    gated by Phase 1 Snow ownership guard for snow:* positions
    (that's the WHOLE reason Step 5 introduces caller_role). Tests
    here use Floki-managed tickets so the guard doesn't interfere."""

    def test_close_decision_executes_on_floki_ticket(self):
        tm, _, floki = _make_tm(
            shadow=False,  # production
            positions=[_FakePosition(ticket=999, comment="floki")],
            llm_response=(
                '{"decision": "CLOSE_TRADE", "reason": "thesis broken"}'
            ),
        )
        result = tm.run_cycle("TM_CHECK", {"ticket": 999})
        assert result["decision"] == "CLOSE_TRADE"
        assert result["executed"] is True
        # Phase 2 Step 5: TM passes caller_role="trade_manager" to
        # bypass the Phase 1 ownership guard.
        floki.close_trade.assert_called_once_with(
            999, caller_role="trade_manager",
        )

    def test_adjust_decision_executes_on_floki_ticket(self):
        tm, _, floki = _make_tm(
            shadow=False,
            positions=[_FakePosition(ticket=999, comment="floki")],
            llm_response=(
                '{"decision": "ADJUST_TRADE", "reason": "tighten", '
                '"new_sl": 4490.0, "new_tp": 4530.0}'
            ),
        )
        result = tm.run_cycle("TM_CHECK", {"ticket": 999})
        assert result["decision"] == "ADJUST_TRADE"
        assert result["executed"] is True
        floki.adjust_trade.assert_called_once_with(
            999, new_sl=4490.0, new_tp=4530.0,
            caller_role="trade_manager",
        )

    def test_no_op_decision_does_not_execute(self):
        tm, _, floki = _make_tm(
            shadow=False,
            positions=[_FakePosition(ticket=999)],
            llm_response='{"decision": "NO_OP", "reason": "fine"}',
        )
        result = tm.run_cycle("TM_CHECK", {})
        assert result["executed"] is False
        floki.close_trade.assert_not_called()
        floki.adjust_trade.assert_not_called()

    def test_hold_decision_does_not_execute(self):
        tm, _, floki = _make_tm(
            shadow=False,
            positions=[_FakePosition(ticket=999)],
            llm_response='{"decision": "HOLD_TRADE", "reason": "wait"}',
        )
        result = tm.run_cycle("TM_CHECK", {})
        assert result["executed"] is False
        floki.close_trade.assert_not_called()


# =============================================================================
# 6. Config defaults
# =============================================================================


class TestConfigDefaults:
    def test_trade_manager_enabled_default_false(self):
        """Code-level default for TRADE_MANAGER_ENABLED must be False.

        Source-inspection contract: config.py uses load_dotenv(override=True),
        so env-isolation via monkeypatch.delenv is not reliable (dotenv
        re-reads .env on import-reload). We instead pin the literal
        default in the os.environ.get call. Operator can flip via .env
        without changing the code default."""
        import inspect, re
        import config as _cfg
        src = inspect.getsource(_cfg)
        m = re.search(
            r'TRADE_MANAGER_ENABLED\s*=\s*os\.environ\.get\(\s*'
            r'"TRADE_MANAGER_ENABLED"\s*,\s*"(\w+)"',
            src,
        )
        assert m is not None, "TRADE_MANAGER_ENABLED os.environ.get not found"
        assert m.group(1).lower() == "false", (
            f"code default must be 'false', got '{m.group(1)}'"
        )

    def test_trade_manager_model_default_qwen(self, monkeypatch):
        for k in ("TRADE_MANAGER_MODEL",):
            monkeypatch.delenv(k, raising=False)
        import importlib
        import config as _cfg
        _cfg = importlib.reload(_cfg)
        assert "qwen" in _cfg.TRADE_MANAGER_MODEL.lower()

    def test_heartbeat_default_60s(self, monkeypatch):
        for k in ("TRADE_MANAGER_HEARTBEAT_SECONDS",):
            monkeypatch.delenv(k, raising=False)
        import importlib
        import config as _cfg
        _cfg = importlib.reload(_cfg)
        assert _cfg.TRADE_MANAGER_HEARTBEAT_SECONDS == 60
