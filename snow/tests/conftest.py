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
    "schema_version": 3,
    "id": "PLAN-20260424-001",
    "created_by": "floki",
    "created_at": "2026-04-24T08:00:00Z",
    "expires_at": "2026-04-24T12:00:00Z",
    "status": "pending",
    "analysis": {
        "thesis": "Gold at H1 resistance; DXY strong; expect rejection",
        "key_levels": [4735.0, 4720.0, 4707.0],
        "confidence": 75,
        "regime_assumed": "TRENDING_BEARISH",
        # FLO-366 tagging — required from schema_version >= 3.
        "setup_type": "pullback_trend",
        "context_tags": {
            "trend": "trend_strong",
            "volatility": "high_vol",
            "htf": "HTF_aligned",
            "news_session": ["session_overlap"],
        },
        "confidence_reason": "H4/H1 EMA stack aligned bearish; rejection wick at 4735; DXY +0.4% intraday.",
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
        # FLO-419 hybrid architecture: management is Snow's safety-net
        # only — at most one move_sl_to_breakeven contingency at
        # mfe_reached >= 100 pips. Tactical SL belongs to Qwen TM.
        {
            "name": "safety_net_be",
            "priority": 7,
            "conditions": [{"type": "mfe_reached", "pips": 100.0}],
            "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0},
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
    """Return a deep-copied canonical valid plan dict (current schema_version).

    Tests may freely mutate the returned dict; each invocation yields a
    fresh copy so test isolation holds.
    """
    return deepcopy(_BASE_PLAN)


@pytest.fixture
def valid_plan_dict_v1() -> dict[str, Any]:
    """Canonical valid plan pinned to schema_version=1.

    Use for backward-compat regression tests (FLO-359 Phase 8b,
    FLO-366). Stateless primitives only — `_check_stateful_in_v1`
    rejects v1 plans referencing stateful types. Tagging fields
    (FLO-366) are stripped because v1/v2 plans never carry them.
    """
    out = deepcopy(_BASE_PLAN)
    out["schema_version"] = 1
    out["analysis"].pop("setup_type", None)
    out["analysis"].pop("context_tags", None)
    out["analysis"].pop("confidence_reason", None)
    return out


@pytest.fixture
def valid_plan_dict_v2() -> dict[str, Any]:
    """Canonical valid plan pinned to schema_version=2.

    Pre-FLO-366 baseline. Tagging fields stripped because v2 plans
    don't carry them; the version-conditional validator on Plan only
    enforces tagging from v3 onward.
    """
    out = deepcopy(_BASE_PLAN)
    out["schema_version"] = 2
    out["analysis"].pop("setup_type", None)
    out["analysis"].pop("context_tags", None)
    out["analysis"].pop("confidence_reason", None)
    return out


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


# -----------------------------------------------------------------------------
# Evaluator test helpers (Phase 3b)
# -----------------------------------------------------------------------------

class FakeLiveData:
    """Minimal LiveData stand-in for evaluator tests.

    Each indicator accessor returns the value configured via the
    matching `set_*` or via constructor kwargs. Missing configuration
    → returns None (matches real LiveData's failure mode).
    """

    def __init__(
        self,
        *,
        price_mid: Any = None,
        price_bid: Any = None,
        price_ask: Any = None,
        rsi_by_tf: dict = None,
        macd_hist_by_tf: dict = None,
        ema_by_key: dict = None,       # key: (tf, period) → float
        atr_by_tf: dict = None,
        # Phase 7.3 (FLO-355) — Cat A indicator stubs
        bollinger_by_tf: dict = None,
        stochastic_by_tf: dict = None,
        pivot_points_dict: Any = None,
        macd_div_by_tf: dict = None,
    ):
        self._price_mid = price_mid
        self._price_bid = price_bid
        self._price_ask = price_ask
        self._rsi = rsi_by_tf or {}
        self._macd_hist = macd_hist_by_tf or {}
        self._ema = ema_by_key or {}
        self._atr = atr_by_tf or {}
        self._bollinger = bollinger_by_tf or {}
        self._stochastic = stochastic_by_tf or {}
        self._pivot_points = pivot_points_dict
        self._macd_div = macd_div_by_tf or {}

    def price(self, side: str = "mid"):
        if side == "mid":
            return self._price_mid
        if side == "bid":
            return self._price_bid
        if side == "ask":
            return self._price_ask
        return None

    def rsi(self, tf: str = "M1", period: int = 14):
        return self._rsi.get(tf)

    def macd_histogram(self, tf: str = "M1"):
        return self._macd_hist.get(tf)

    def ema(self, tf: str = "M1", period: int = 9):
        return self._ema.get((tf, period))

    def atr(self, tf: str = "M1", period: int = 14):
        return self._atr.get(tf)

    # -- Phase 7.3 Cat A accessors -----------------------------------------
    def bollinger(self, tf: str = "H1"):
        return self._bollinger.get(tf)

    def stochastic(self, tf: str = "H1"):
        return self._stochastic.get(tf)

    def pivot_points(self):
        return self._pivot_points

    def macd_divergence(self, tf: str = "H1"):
        return self._macd_div.get(tf)


class FakeSemanticCache:
    """Minimal SemanticCache stand-in for evaluator tests."""

    def __init__(self, data: Any = None):
        self._data = data

    def get(self, *path: str) -> Any:
        node: Any = self._data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node


@pytest.fixture
def fake_live():
    """Factory returning a FakeLiveData configured via kwargs."""
    def _make(**kwargs) -> FakeLiveData:
        return FakeLiveData(**kwargs)
    return _make


@pytest.fixture
def fake_semantic():
    """Factory returning a FakeSemanticCache seeded with a dict."""
    def _make(data: Any = None) -> FakeSemanticCache:
        return FakeSemanticCache(data=data)
    return _make


@pytest.fixture
def tracker():
    """Fresh PerPlanTracker per test — prevents state leakage."""
    from snow.evaluators.tracker import PerPlanTracker
    return PerPlanTracker()


@pytest.fixture
def sample_plan(valid_plan_dict):
    """Parsed Plan model from the canonical fixture."""
    from snow.schema import Plan
    return Plan(**valid_plan_dict)


@pytest.fixture
def eval_ctx(sample_plan, tracker):
    """Factory producing an EvalContext with configurable live_data /
    semantic_cache / ticket / now. Defaults point at empty fakes."""
    from snow.evaluators.context import EvalContext
    def _make(
        *,
        live_data=None,
        semantic_cache=None,
        plan=None,
        ticket=None,
        now=None,
    ):
        return EvalContext(
            live_data=live_data if live_data is not None else FakeLiveData(),
            semantic_cache=semantic_cache if semantic_cache is not None
                          else FakeSemanticCache(),
            tracker=tracker,
            plan=plan or sample_plan,
            ticket=ticket,
            now=now,
        )
    return _make


# =============================================================================
# FLO-347 post-investigation — redirect TradingLogger file handler to tmp
#
# Problem this fixture solves:
#   `logger.TradingLogger` (imported as `from logger import log` everywhere
#   in the project) attaches a FileHandler to `logs/trading_bot_YYYY-MM-DD.log`
#   unconditionally on first instantiation. Pytest runs that touch AgentTools
#   (e.g. tools_test.py) call the real _log_tool — those log lines land in
#   the same daily log file the running production bot writes to.
#
#   An operator greping `logs/trading_bot_YYYY-MM-DD.log` for e.g. a tool-call
#   audit trail cannot visually distinguish test-generated entries from real
#   Floki tool calls. This caused a false-positive P0 during the FLO-347
#   Phase 6.5 evidence window.
#
# Fix:
#   Session-scoped autouse fixture that swaps the TradingLogger's FileHandler
#   for one pointing at a pytest-owned tmp path for the whole session, then
#   restores the original handlers on teardown. All existing tests keep their
#   logging behaviour (log.info etc. still work) — only the file destination
#   changes.
# =============================================================================

@pytest.fixture(scope="session", autouse=True)
def _redirect_tradinglogger_to_tmp(tmp_path_factory):
    """Keep `logs/trading_bot_*.log` free of pytest-generated noise."""
    import logging as _logging

    # Force the TradingLogger module to initialise (attaches handlers on first
    # instantiation). Safe if already imported elsewhere — idempotent handler
    # guard inside TradingLogger.__init__.
    import logger as _project_logger
    _ = _project_logger.log

    trading_logger = _logging.getLogger("TradingBot")
    tmp_dir = tmp_path_factory.mktemp("tradinglog")
    test_log_path = tmp_dir / "test.log"

    # Remove any FileHandler pointing at the production daily log.
    removed_handlers: list[_logging.Handler] = []
    for h in list(trading_logger.handlers):
        if isinstance(h, _logging.FileHandler):
            trading_logger.removeHandler(h)
            removed_handlers.append(h)
            # Flush + close so no buffered bytes leak to the production file.
            try:
                h.flush()
                h.close()
            except Exception:
                pass

    # Install tmp-path handler with the same formatter as production.
    test_handler = _logging.FileHandler(str(test_log_path), encoding="utf-8")
    test_handler.setLevel(_logging.DEBUG)
    test_handler.setFormatter(_logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    trading_logger.addHandler(test_handler)

    yield test_log_path

    # Teardown: remove tmp handler and reinstate any production handlers
    # we removed. Strictly optional (pytest session is ending), but keeps
    # post-run state clean if tests are re-entered in the same process.
    trading_logger.removeHandler(test_handler)
    try:
        test_handler.flush()
        test_handler.close()
    except Exception:
        pass
    for h in removed_handlers:
        trading_logger.addHandler(h)
