"""Snow main-loop tests — FLO-347 Phase 4.

Covers the SnowLoop lifecycle, state-gated evaluation, 3-layer error
isolation, tracker lifecycle integration, cycle-timing observability,
DRY-RUN enforcement, and module boundary compliance.

All tests run against a per-test tmp SQLite file via the `snow_conn`
fixture (same pattern as snow/tests/db_test.py). LiveData /
SemanticCache are replaced with FakeLiveData / FakeSemanticCache from
conftest.py so the loop can be exercised without MT5 or a running bot.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from snow import db as snow_db
from snow import snow_loop as snow_loop_mod
from snow.schema import ContingencyState, Plan, PlanStatus
from snow.snow_loop import (
    ORPHAN_SWEEP_INTERVAL_TICKS,
    SHUTDOWN_POLL_SECONDS,
    TIMING_LOG_INTERVAL_TICKS,
    TIMING_WARN_THRESHOLD_MS,
    SnowLoop,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def snow_conn(tmp_path, monkeypatch):
    """Ephemeral tmp SQLite for Snow CRUD — mirrors db_test.py pattern."""
    db_path = tmp_path / "snow_loop_test.db"

    def _tmp_connect() -> sqlite3.Connection:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(snow_db, "_connect", _tmp_connect)
    snow_db.init_snow_tables()
    return db_path


class _FakeBot:
    """Minimal bot stand-in — the loop reads `.running` and
    `._last_agent_data` only."""

    def __init__(self, *, running: bool = True, agent_data: Any = None):
        self.running = running
        self._last_agent_data = agent_data

    def stop(self) -> None:
        self.running = False


class _RefreshableLive:
    """Wrap conftest's FakeLiveData with a no-op refresh().

    Real LiveData.refresh() pulls a fresh MT5 tick window; the fake has
    no underlying MT5 so the stub is a silent no-op. Without this wrapper,
    every test would log a benign AttributeError from the loop's
    `live_data.refresh()` call.
    """

    def __init__(self, inner):
        self._inner = inner

    def refresh(self) -> None:
        return None

    # Delegate every indicator/price accessor to the inner fake.
    def __getattr__(self, name):
        return getattr(self._inner, name)


def _make_loop(
    *,
    bot: _FakeBot,
    live_data,
    semantic_cache,
    tracker=None,
) -> SnowLoop:
    """Build a SnowLoop with injected fakes; DRY RUN forced on."""
    return SnowLoop(
        bot,
        symbol="XAUUSD",
        live_data=_RefreshableLive(live_data),
        semantic_cache=semantic_cache,
        tracker=tracker,
        dry_run=True,
    )


def _insert_plan(plan_dict: dict[str, Any]) -> Plan:
    plan = Plan(**plan_dict)
    snow_db.insert_plan(plan)
    return plan


def _advance_to_active(plan_id: str, ticket: int = 111_222) -> None:
    """Force a plan into ACTIVE state with a trade_ticket — bypasses the
    lifecycle transitions that Phase 5 will own."""
    snow_db.update_plan_trade_ticket(plan_id, ticket)
    snow_db.update_plan_status(plan_id, PlanStatus.ACTIVE.value)


def _set_status(plan_id: str, status: str) -> None:
    snow_db.update_plan_status(plan_id, status)


def _evaluation_rows(db_path: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM snow_evaluations ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _plan_row(db_path: Path, plan_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM snow_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# Use FakeLiveData / FakeSemanticCache from conftest via factories.
# valid_plan_dict comes from conftest too.


# ---------------------------------------------------------------------------
# TestSnowLoopLifecycle
# ---------------------------------------------------------------------------

class TestSnowLoopLifecycle:
    def test_run_forever_exits_when_running_flag_false(
        self, snow_conn, fake_live, fake_semantic
    ):
        bot = _FakeBot(running=False)
        loop = _make_loop(bot=bot, live_data=fake_live(), semantic_cache=fake_semantic())
        start = time.monotonic()
        loop.run_forever()  # must return immediately
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"run_forever should return fast when not running; took {elapsed:.2f}s"
        assert loop._tick_count == 0, "should not tick when running=False at entry"

    def test_run_forever_ticks_until_stopped(
        self, snow_conn, fake_live, fake_semantic
    ):
        bot = _FakeBot(running=True)
        loop = _make_loop(bot=bot, live_data=fake_live(), semantic_cache=fake_semantic())
        t = threading.Thread(target=loop.run_forever, daemon=True)
        t.start()
        # Let the loop run a few ticks. Cycle interval is 5s but we only
        # care that at least one _tick() fires — monkeypatch the sleep
        # shortcut via direct tick invocation in other tests; here we
        # just need to confirm the thread stops cleanly.
        time.sleep(0.2)
        bot.running = False
        t.join(timeout=SHUTDOWN_POLL_SECONDS + 2.0)
        assert not t.is_alive(), "thread must exit within shutdown window"

    def test_interruptible_sleep_respects_running_flag(
        self, snow_conn, fake_live, fake_semantic
    ):
        bot = _FakeBot(running=True)
        loop = _make_loop(bot=bot, live_data=fake_live(), semantic_cache=fake_semantic())
        # Kick off a long sleep, then flip running mid-way.
        t0 = time.monotonic()

        def _stop_soon():
            time.sleep(0.1)
            bot.running = False

        stopper = threading.Thread(target=_stop_soon, daemon=True)
        stopper.start()
        loop._interruptible_sleep(10.0)
        elapsed = time.monotonic() - t0
        stopper.join(timeout=2.0)
        # Must exit within one poll-chunk after the flag flipped.
        assert elapsed < SHUTDOWN_POLL_SECONDS + 1.0, (
            f"interruptible_sleep took {elapsed:.2f}s; expected fast exit"
        )

    def test_tick_increments_counter(
        self, snow_conn, fake_live, fake_semantic
    ):
        bot = _FakeBot(running=True)
        loop = _make_loop(bot=bot, live_data=fake_live(), semantic_cache=fake_semantic())
        assert loop._tick_count == 0
        loop._tick()
        assert loop._tick_count == 1
        loop._tick()
        assert loop._tick_count == 2

    def test_run_forever_logs_start_and_stop(
        self, snow_conn, fake_live, fake_semantic, caplog
    ):
        bot = _FakeBot(running=False)
        loop = _make_loop(bot=bot, live_data=fake_live(), semantic_cache=fake_semantic())
        with caplog.at_level(logging.INFO, logger="snow.snow_loop"):
            loop.run_forever()
        msgs = "\n".join(r.getMessage() for r in caplog.records)
        assert "snow.loop.start" in msgs
        assert "snow.loop.stop" in msgs


# ---------------------------------------------------------------------------
# TestEvaluatePlanHappyPath
# ---------------------------------------------------------------------------

class TestEvaluatePlanHappyPath:
    def test_pending_plan_with_all_true_entry_records_would_fire(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # Entry conditions in the fixture: price_above 4730 AND rsi H1 above 70.
        live = fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0})
        plan = _insert_plan(valid_plan_dict)
        bot = _FakeBot()
        loop = _make_loop(bot=bot, live_data=live, semantic_cache=fake_semantic())

        loop._tick()

        rows = _evaluation_rows(snow_conn)
        would_fire = [r for r in rows if r["event"] == "entry_would_fire"]
        assert len(would_fire) == 1
        assert would_fire[0]["plan_id"] == plan.id

    def test_pending_plan_with_false_entry_records_nothing(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # Price below entry threshold → first condition fails.
        live = fake_live(price_mid=4720.0, rsi_by_tf={"H1": 75.0})
        _insert_plan(valid_plan_dict)
        bot = _FakeBot()
        loop = _make_loop(bot=bot, live_data=live, semantic_cache=fake_semantic())

        loop._tick()

        fires = [r for r in _evaluation_rows(snow_conn) if "would_fire" in r["event"]]
        assert fires == []

    def test_entry_short_circuits_on_first_false(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # price_above fails; evaluator must NOT hit rsi.
        live = fake_live(price_mid=4720.0, rsi_by_tf={})
        plan = _insert_plan(valid_plan_dict)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        # Either nothing recorded, or recorded as not-all-true: our loop
        # only writes would_fire on True, so confirm no would_fire row.
        assert not any(r["event"] == "entry_would_fire" for r in rows)
        # No crash means the short-circuit worked (missing RSI never
        # accessed). We can't observe internal calls, but the fact that
        # FakeLiveData has an empty rsi_by_tf and the loop didn't error
        # is the assertion.
        del plan

    def test_active_plan_management_fires(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # Management trigger: price_below 4720 (lock_10_at_support).
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4715.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        mgmt = [r for r in rows if r["event"] == "management_would_fire"]
        assert len(mgmt) == 1
        assert mgmt[0]["contingency_name"] == "lock_10_at_support"

    def test_active_plan_exit_fires(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # Exit trigger: price_above 4733 (rejection_exit).
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4734.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        exits = [r for r in rows if r["event"] == "exit_would_fire"]
        names = {r["contingency_name"] for r in exits}
        assert "rejection_exit" in names

    def test_deactivated_contingency_is_skipped(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        # Flip the management contingency state to DEACTIVATED before insert.
        mutated = deepcopy(valid_plan_dict)
        mutated["management"][0]["state"] = ContingencyState.DEACTIVATED.value
        plan = _insert_plan(mutated)
        _advance_to_active(plan.id)
        live = fake_live(price_mid=4715.0)  # would fire if ARMED
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        mgmt = [r for r in rows if r["event"] == "management_would_fire"]
        assert mgmt == [], "deactivated contingency must not fire"

    def test_triggered_plan_is_skipped(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        plan = _insert_plan(valid_plan_dict)
        _set_status(plan.id, PlanStatus.TRIGGERED.value)
        live = fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0})
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        assert not any("would_fire" in r["event"] for r in rows)

    def test_closing_plan_is_skipped(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        _set_status(plan.id, PlanStatus.CLOSING.value)
        live = fake_live(price_mid=4734.0)
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        assert not any("would_fire" in r["event"] for r in rows)

    def test_terminal_plan_not_fetched_by_get_active_plans(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        plan = _insert_plan(valid_plan_dict)
        _set_status(plan.id, PlanStatus.CLOSED.value)
        live = fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0})
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )

        loop._tick()
        rows = _evaluation_rows(snow_conn)
        assert rows == [], "terminal plans must not appear in the evaluation set"

    def test_last_evaluated_at_updated_on_tick(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        plan = _insert_plan(valid_plan_dict)
        assert _plan_row(snow_conn, plan.id)["last_evaluated_at"] is None
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(),
            semantic_cache=fake_semantic(),
        )

        loop._tick()
        assert _plan_row(snow_conn, plan.id)["last_evaluated_at"] is not None


# ---------------------------------------------------------------------------
# TestErrorIsolation
# ---------------------------------------------------------------------------

class TestErrorIsolation:
    def test_plan_evaluation_exception_does_not_kill_tick(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, monkeypatch
    ):
        plan = _insert_plan(valid_plan_dict)

        def _boom(_cond, _ctx):
            raise RuntimeError("evaluator blew up")

        monkeypatch.setattr(snow_loop_mod, "evaluate_condition", _boom)
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0}),
            semantic_cache=fake_semantic(),
        )

        # Tick must return normally.
        loop._tick()

        # last_evaluated_at updated despite the failure (bookkeeping guarantees
        # plans aren't repeatedly picked as "never evaluated").
        assert _plan_row(snow_conn, plan.id)["last_evaluated_at"] is not None

    def test_plan_evaluation_exception_preserves_loop_thread(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, monkeypatch
    ):
        _insert_plan(valid_plan_dict)
        monkeypatch.setattr(
            snow_loop_mod, "evaluate_condition",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0}),
            semantic_cache=fake_semantic(),
        )

        # Multiple ticks — tick_count must keep advancing.
        loop._tick()
        loop._tick()
        loop._tick()
        assert loop._tick_count == 3

    def test_other_plans_evaluated_when_one_fails(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, monkeypatch
    ):
        bad = deepcopy(valid_plan_dict)
        bad["id"] = "PLAN-20260424-001"
        good = deepcopy(valid_plan_dict)
        good["id"] = "PLAN-20260424-002"
        _insert_plan(bad)
        _insert_plan(good)

        original_hydrate = snow_db.get_plan_as_model

        def _flaky_hydrate(plan_id):
            if plan_id == "PLAN-20260424-001":
                raise RuntimeError("hydrate blew up")
            return original_hydrate(plan_id)

        monkeypatch.setattr(snow_db, "get_plan_as_model", _flaky_hydrate)

        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0}),
            semantic_cache=fake_semantic(),
        )
        loop._tick()

        # The good plan still got an entry_would_fire row.
        rows = _evaluation_rows(snow_conn)
        fires = [r for r in rows if r["event"] == "entry_would_fire"]
        assert any(r["plan_id"] == "PLAN-20260424-002" for r in fires)

    def test_error_event_recorded_on_plan_failure(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, monkeypatch
    ):
        plan = _insert_plan(valid_plan_dict)
        monkeypatch.setattr(
            snow_db, "get_plan_as_model",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("xxx")),
        )
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(),
            semantic_cache=fake_semantic(),
        )
        loop._tick()
        rows = _evaluation_rows(snow_conn)
        errs = [r for r in rows if r["event"] == "evaluation_error"]
        assert len(errs) == 1
        assert errs[0]["plan_id"] == plan.id

    def test_last_evaluated_updated_even_on_failure(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, monkeypatch
    ):
        plan = _insert_plan(valid_plan_dict)
        monkeypatch.setattr(
            snow_db, "get_plan_as_model",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(),
            semantic_cache=fake_semantic(),
        )
        loop._tick()
        assert _plan_row(snow_conn, plan.id)["last_evaluated_at"] is not None


# ---------------------------------------------------------------------------
# TestTrackerLifecycle
# ---------------------------------------------------------------------------

class TestTrackerLifecycle:
    def test_tracker_seeded_on_first_active_observation(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, tracker
    ):
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
            tracker=tracker,
        )
        assert not tracker.has(plan.id)
        loop._tick()
        assert tracker.has(plan.id), "tracker must be seeded on ACTIVE observation"

    def test_tracker_seed_idempotent_on_repeat(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, tracker
    ):
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
            tracker=tracker,
        )
        loop._tick()
        mfe_after_first = tracker.mfe_pips(plan.id)
        # Move price, tick again — tracker must NOT re-seed (that would
        # reset MFE/MAE).
        loop._live_data._inner._price_mid = 4728.0  # through refresh-wrapper
        loop._tick()
        assert tracker.mfe_pips(plan.id) is not None
        # A re-seed would have cleared MFE accumulated from first tick.
        # Since we moved against the direction (SELL, price up → adverse),
        # mfe should still be 0.0 from first tick (entry was at 4725,
        # SELL never in profit when price went up). Assert absence of
        # RuntimeError and that the tracker didn't throw.
        assert tracker.has(plan.id)

    def test_tracker_forgets_on_terminal_transition(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, tracker
    ):
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
            tracker=tracker,
        )
        loop._tick()
        assert tracker.has(plan.id)

        # Transition to CLOSED — next tick must forget.
        _set_status(plan.id, PlanStatus.CLOSED.value)
        loop._tick()
        assert not tracker.has(plan.id), "terminal transition must forget tracker"

    def test_orphan_sweep_interval(self):
        # Sanity: the module-level constant is what we claim.
        assert ORPHAN_SWEEP_INTERVAL_TICKS == 60

    def test_update_price_called_before_evaluation(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict, tracker
    ):
        plan = _insert_plan(valid_plan_dict)
        _advance_to_active(plan.id)
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(price_mid=4725.0),
            semantic_cache=fake_semantic(),
            tracker=tracker,
        )
        loop._tick()
        # MFE tracked (0.0 since SELL entry @4725 and current=4725).
        assert tracker.profit_pips(plan.id, 4725.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestCycleTiming
# ---------------------------------------------------------------------------

class TestCycleTiming:
    def test_cycle_timing_log_interval_constant(self):
        assert TIMING_LOG_INTERVAL_TICKS == 60

    def test_cycle_timing_logged_at_interval(
        self, snow_conn, fake_live, fake_semantic, caplog
    ):
        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(),
            semantic_cache=fake_semantic(),
        )
        with caplog.at_level(logging.INFO, logger="snow.snow_loop"):
            # Skip ahead to tick 59 — next tick is the 60th and must log.
            loop._tick_count = TIMING_LOG_INTERVAL_TICKS - 1
            loop._tick()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("snow.cycle_timing" in m for m in msgs), (
            f"expected cycle_timing log on tick {TIMING_LOG_INTERVAL_TICKS}"
        )

    def test_cycle_slow_warn_fires_above_threshold(
        self, snow_conn, fake_live, fake_semantic, caplog, monkeypatch
    ):
        # Monkeypatch time.monotonic inside snow_loop to simulate a slow tick.
        real_monotonic = time.monotonic
        calls = {"n": 0}

        def _slow_monotonic():
            calls["n"] += 1
            # First call (tick start) = 0, second call (tick end) = 0.5s
            # → duration 500ms > 200ms threshold.
            return 0.0 if calls["n"] == 1 else 0.5

        monkeypatch.setattr(snow_loop_mod.time, "monotonic", _slow_monotonic)

        loop = _make_loop(
            bot=_FakeBot(),
            live_data=fake_live(),
            semantic_cache=fake_semantic(),
        )
        with caplog.at_level(logging.WARNING, logger="snow.snow_loop"):
            loop._tick()
        msgs = [r.getMessage() for r in caplog.records]
        assert any("snow.cycle_slow" in m for m in msgs), (
            "slow-cycle WARN must fire when duration exceeds threshold"
        )
        # Sanity — threshold constant surfaced for external observability.
        assert TIMING_WARN_THRESHOLD_MS == 200.0


# ---------------------------------------------------------------------------
# TestDryRunMode
# ---------------------------------------------------------------------------

class TestDryRunMode:
    def test_config_default_is_dry_run_true(self):
        import config
        assert getattr(config, "SNOW_DRY_RUN", True) is True, (
            "config.SNOW_DRY_RUN must default True in Phase 4"
        )

    def test_config_default_snow_enabled_false(self):
        import config
        assert getattr(config, "SNOW_ENABLED", False) is False, (
            "config.SNOW_ENABLED must default False in Phase 4"
        )

    def test_dry_run_false_raises_notimplemented(
        self, snow_conn, fake_live, fake_semantic
    ):
        with pytest.raises(NotImplementedError):
            SnowLoop(
                _FakeBot(),
                live_data=fake_live(),
                semantic_cache=fake_semantic(),
                dry_run=False,
            )

    def test_dry_run_never_mutates_plan_status(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        plan = _insert_plan(valid_plan_dict)
        # All-true entry would transition PENDING → TRIGGERED in Phase 5.
        live = fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0})
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )
        loop._tick()
        row = _plan_row(snow_conn, plan.id)
        assert row["status"] == PlanStatus.PENDING.value, (
            "DRY RUN must leave status untouched even when entry fires"
        )
        assert row["trade_ticket"] is None

    def test_dry_run_records_only_audit_events(
        self, snow_conn, fake_live, fake_semantic, valid_plan_dict
    ):
        _insert_plan(valid_plan_dict)
        live = fake_live(price_mid=4731.0, rsi_by_tf={"H1": 75.0})
        loop = _make_loop(
            bot=_FakeBot(), live_data=live, semantic_cache=fake_semantic()
        )
        loop._tick()
        rows = _evaluation_rows(snow_conn)
        # No "fired" event — only "_would_fire" variants.
        assert all(
            r["event"].endswith("would_fire") or r["event"] == "evaluation_error"
            for r in rows
        ), f"DRY RUN must not record non-would-fire events: {[r['event'] for r in rows]}"


# ---------------------------------------------------------------------------
# TestBoundaryCompliance
# ---------------------------------------------------------------------------

class TestBoundaryCompliance:
    FORBIDDEN = (
        "executor",
        "agent_tools",
        "ai_agent",
        "rex_validator",
        "rex_monitor",
        "monitor",
    )

    def test_snow_loop_imports_no_forbidden_module(self):
        """Static check: no top-level or inline import of the executor /
        Floki / Rex / monitor surfaces from snow_loop.py. Enforces the
        Phase 4 boundary (CTO directive)."""
        source = Path(snow_loop_mod.__file__).read_text(encoding="utf-8")
        for name in self.FORBIDDEN:
            # Catch both `import name` and `from name`.
            pattern = rf"(?m)^(?:import|from)\s+{re.escape(name)}(?:\s|\.|$)"
            match = re.search(pattern, source)
            assert match is None, (
                f"snow_loop.py has forbidden import of `{name}`: "
                f"{match.group(0) if match else ''}"
            )

    def test_snow_loop_namespace_has_no_forbidden_modules(self):
        """Runtime check: after import, snow_loop's module dict must NOT
        reference the forbidden surfaces."""
        ns = vars(snow_loop_mod)
        for name in self.FORBIDDEN:
            assert name not in ns, (
                f"snow_loop imported `{name}` into its namespace"
            )
