"""Integration tests for the main.py → Snow wiring (FLO-347 Phase 4.5).

Covers the production code path that the Phase 4 unit tests missed.
Three bugs escaped on the first SNOW_ENABLED=true restart, all in the
~20 lines of main.py wiring:

  1. `log.info(fmt, arg)` — the project's TradingLogger (logger.py)
     accepts a single `message: str`, not stdlib-style (fmt, *args).
  2. `log.exception(...)` — TradingLogger has no `.exception` method.
     The error-path call itself raised AttributeError.
  3. `init_snow_tables()` was never called in production. Unit tests
     used tmp_path fixtures that called it; production `history.db`
     had no snow_plans / snow_triggers / snow_evaluations tables, so
     the first tick's `get_active_plans()` hit
     `sqlite3.OperationalError: no such table: snow_plans`.

These tests exercise the ACTUAL production surfaces:
  * real `logger.log` (TradingLogger instance — not a mock)
  * real `snow.db.init_snow_tables()` against a tmp `history.db`
  * a real daemon thread running `snow_loop.run_forever`

Any future change to the main.py snow wiring — or to TradingLogger's
public API — must keep these tests green.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from pathlib import Path

import pytest

import config


# ---------------------------------------------------------------------------
# Fixture: redirect production-style history.db to a tmp path.
# ---------------------------------------------------------------------------

@pytest.fixture
def real_history_db(tmp_path, monkeypatch) -> Path:
    """Point `config.HISTORY_DB_PATH` at a tmp SQLite file.

    `db_writer._get_connection()` reads `config.HISTORY_DB_PATH` lazily
    per call, so monkeypatching the config module suffices. No need to
    touch any connection cache — `_get_connection` opens fresh each call.
    """
    db_path = tmp_path / "history.db"
    monkeypatch.setattr(config, "HISTORY_DB_PATH", str(db_path), raising=False)
    return db_path


# ---------------------------------------------------------------------------
# Test class — one concern per method.
# ---------------------------------------------------------------------------

class TestSnowSpawnBlock:
    """Integration tests for the exact code path in main.py:1200-1215."""

    def test_init_snow_tables_creates_tables_in_real_db(self, real_history_db):
        """Reproduce Bug 3: spawning the loop without init_snow_tables()
        leaves the production DB without snow_* tables."""
        from snow import db as snow_db

        snow_db.init_snow_tables()

        conn = sqlite3.connect(str(real_history_db))
        try:
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
        for required in ("snow_plans", "snow_triggers", "snow_evaluations"):
            assert required in tables, (
                f"init_snow_tables() did not create `{required}` — "
                f"present: {sorted(tables)}"
            )

    def test_init_snow_tables_is_idempotent(self, real_history_db):
        """Must be safe to call on every bot start (tables already exist
        after the first start). IF NOT EXISTS makes the DDL idempotent;
        verify the contract holds end-to-end."""
        from snow import db as snow_db

        snow_db.init_snow_tables()
        snow_db.init_snow_tables()  # must not raise
        snow_db.init_snow_tables()

    def test_tradinglogger_info_accepts_fstring(self):
        """Reproduce Bug 1: `log.info(fmt, arg)` crashes because
        TradingLogger.info(self, message: str) only takes one positional
        argument. The spawn block now uses f-strings — verify that
        format is accepted."""
        from logger import log

        dry_run = True
        # Exact form used in main.py line 1209 after the fix.
        log.info(f"Snow loop spawned (DRY_RUN={dry_run})")

        # Guard against silent regression — the multi-arg form MUST fail
        # so we know this test stays meaningful.
        with pytest.raises(TypeError):
            log.info("Snow loop spawned (DRY_RUN=%s)", dry_run)  # type: ignore[misc]

    def test_tradinglogger_has_no_exception_method(self):
        """Reproduce Bug 2: the fallback handler used log.exception(),
        which doesn't exist on TradingLogger. Confirm the method is
        still absent so the replacement (log.error + traceback.format_exc)
        stays in place."""
        from logger import log

        assert not hasattr(log, "exception"), (
            "TradingLogger grew an .exception method — if that's "
            "intentional, update main.py spawn block to use it and "
            "remove this test."
        )

    def test_tradinglogger_error_path_matches_main_py_usage(self):
        """Reproduce the FIXED error path: `log.error(f"...: {e}")` plus
        `log.error(traceback.format_exc())`. Both calls must succeed against
        the real TradingLogger."""
        from logger import log

        try:
            raise RuntimeError("synthetic snow spawn failure")
        except Exception as e:
            # Exact form used in main.py line 1213-1214 after the fix.
            log.error(f"snow.loop.spawn_failed: {e}")
            log.error(traceback.format_exc())

    def test_snow_thread_survives_first_tick_against_real_db(
        self, real_history_db
    ):
        """End-to-end: mirror the main.py spawn sequence (init_snow_tables
        → spawn thread → run for a moment → stop). Verifies:
          * init_snow_tables populates the schema so get_active_plans works
          * run_forever survives at least one full tick without crashing
          * shutdown latency is bounded (bot.running = False → exit)
        """
        from snow import db as snow_db
        from snow import snow_loop as _snow_loop

        class _FakeBot:
            def __init__(self) -> None:
                self.running = True
                self._last_agent_data = None

        snow_db.init_snow_tables()

        bot = _FakeBot()
        t = threading.Thread(
            target=_snow_loop.run_forever,
            args=(bot,),
            name="SnowLoop",
            daemon=True,
        )
        t.start()
        # The first _tick() fires immediately on thread start; 0.5 s is
        # plenty for it to hit get_active_plans() (which used to raise
        # "no such table" before the fix).
        time.sleep(0.5)
        bot.running = False
        t.join(timeout=3.0)
        assert not t.is_alive(), (
            "Snow thread did not exit within shutdown window — "
            "likely either blocked or dead"
        )

    def test_snow_thread_queries_tables_without_error(self, real_history_db):
        """Stronger variant — after a short run, verify the loop actually
        wrote something sensible to the DB (last_evaluated_at bookkeeping
        requires at least one row to be visible, so we insert one first)."""
        from snow import db as snow_db
        from snow import snow_loop as _snow_loop
        from snow.schema import Plan

        # Fixture plan lifted from conftest — minimal valid dict.
        plan_dict = {
            "schema_version": 1,
            "id": "PLAN-20260424-999",
            "created_by": "floki",
            "created_at": "2026-04-24T08:00:00Z",
            "expires_at": "2026-04-24T12:00:00Z",
            "status": "pending",
            "analysis": {
                "thesis": "integration test seed",
                "key_levels": [4735.0, 4720.0, 4707.0],
                "confidence": 75,
                "regime_assumed": "TRENDING_BEARISH",
            },
            "entry": {
                "direction": "SELL",
                "volume": 0.02,
                "conditions": [{"type": "price_above", "level": 4730.0}],
                "initial_sl": 4740.0,
                "initial_tp": 4710.0,
            },
            "management": [{"name": "be", "priority": 7, "conditions": [{"type": "mfe_reached", "pips": 100.0}], "action": {"type": "move_sl_to_breakeven", "offset_pips": 0.0}, "fires": "once"}],
            "exit": [{"name": "fallback_target", "priority": 1, "conditions": [{"type": "profit_pips", "op": "above", "threshold": 9999}], "action": {"type": "close_full"}, "fires": "once"}],  # FLO-401 floor
            "emergency": {
                "max_loss_pips": 150,
                "max_duration_minutes": 480,
                "on_broker_error": "alert_floki",
            },
        }

        snow_db.init_snow_tables()
        snow_db.insert_plan(Plan(**plan_dict))

        class _FakeBot:
            def __init__(self) -> None:
                self.running = True
                self._last_agent_data = None

        bot = _FakeBot()
        t = threading.Thread(
            target=_snow_loop.run_forever,
            args=(bot,),
            name="SnowLoop",
            daemon=True,
        )
        t.start()
        time.sleep(0.5)
        bot.running = False
        t.join(timeout=3.0)
        assert not t.is_alive()

        # The loop touched last_evaluated_at for the seeded plan —
        # proves get_active_plans() + update_plan_last_evaluated() both
        # succeeded end-to-end against the real DB.
        row = snow_db.get_plan("PLAN-20260424-999")
        assert row is not None
        assert row["last_evaluated_at"] is not None, (
            "loop should have stamped last_evaluated_at within the tick"
        )
