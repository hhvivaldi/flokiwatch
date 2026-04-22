"""
FLO-334 Phase 2 — Rule 20 test suite (5 tests).

Run from repo root:
  PYTHONIOENCODING=utf-8 python scripts/_investigations/flo334_phase2_tests.py

Uses a tempfile snapshot directory + tempfile history.db so production data
is never touched. Standalone asserts per CLAUDE.md convention — no pytest.
"""
from __future__ import annotations

import importlib
import io
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"C:/Users/Hermano/OneDrive/Desktop/XAUUSD")
os.chdir(REPO)
sys.path.insert(0, str(REPO))


def _setup_tempdir(snapshots, trades):
    """Build a temp snapshot dir + mini history.db with the given fixtures."""
    d = tempfile.mkdtemp(prefix="flo334_test_")
    snap_dir = Path(d) / "trade_conditions"
    snap_dir.mkdir()
    for s in snapshots:
        with (snap_dir / f"{s['ticket']}.json").open("w", encoding="utf-8") as f:
            json.dump(s, f)

    db_path = Path(d) / "history.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE trades (ticket INTEGER PRIMARY KEY, profit REAL)"
    )
    for t in trades:
        conn.execute("INSERT INTO trades (ticket, profit) VALUES (?, ?)",
                     (t["ticket"], t.get("profit")))
    conn.commit()
    conn.close()
    return str(snap_dir), str(db_path)


def _reload_lessons_module(snap_dir: str, db_path: str, boundary: str = "pre_FLO-327"):
    """Reload trade_lessons against the temp dir + db + boundary."""
    # Pre-configure config values
    import config as _cfg
    _cfg.LESSONS_ERA_BOUNDARY = boundary
    _cfg.LESSONS_WINDOW_DAYS = 30
    _cfg.HISTORY_DB_PATH = db_path

    # Reimport trade_lessons so module-level CONDITIONS_DIR / DATA_DIR reflect the temp dir
    if "trade_lessons" in sys.modules:
        del sys.modules["trade_lessons"]
    import trade_lessons as tl
    # Redirect module constants
    tl.CONDITIONS_DIR = snap_dir
    tl.DATA_DIR = os.path.dirname(db_path)
    tl._LESSONS_EMPTY_WARNED = False  # reset module-level WARN flag
    return tl


def _capture_warnings(tl):
    """Attach a capture handler to the module logger so we can assert WARN emission.

    trade_lessons.py does `from logger import log` where `log` is a TradingLogger
    wrapper. The wrapper exposes `log.logger` as the underlying stdlib Logger.
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.WARNING)
    from logger import log as module_log
    inner = getattr(module_log, "logger", module_log)
    inner.addHandler(handler)
    return buf, handler, inner


def _release_handler(inner, handler):
    inner.removeHandler(handler)


def _fixture_snapshot(ticket, system_version, days_ago=0, direction="BUY",
                      rsi=45.0, volume=1000, session="NY"):
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    ot = now - timedelta(days=days_ago)
    return {
        "ticket": ticket,
        "direction": direction,
        "open_time": ot.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00"),
        "system_version": system_version,
        "conditions_at_open": {
            "rsi_h1": rsi,
            "volume_h1": volume,
            "session": session,
        },
    }


def test_1_empty_snapshot_dir_returns_empty_no_warn():
    snap_dir, db_path = _setup_tempdir([], [])
    tl = _reload_lessons_module(snap_dir, db_path)
    buf, handler, module_log = _capture_warnings(tl)
    try:
        lessons = tl.get_relevant_lessons(min_occurrences=3, limit=10)
        assert lessons == [], f"expected [], got {lessons!r}"
        warn_out = buf.getvalue()
        assert "LESSONS_ERA_FILTER_DEGRADED" not in warn_out, \
            f"WARN unexpectedly fired: {warn_out!r}"
        print(f"  OK  empty dir → [] with no WARN")
    finally:
        _release_handler(module_log, handler)


def test_2_all_pre_327_returns_empty_warn_fires():
    """All pre_FLO-327 snapshots → filter excludes all → WARN should fire."""
    snaps = [_fixture_snapshot(1001 + i, "pre_FLO-327") for i in range(4)]
    trades = [{"ticket": s["ticket"], "profit": -1.0 if i % 2 else 1.0}
              for i, s in enumerate(snaps)]
    snap_dir, db_path = _setup_tempdir(snaps, trades)
    tl = _reload_lessons_module(snap_dir, db_path)
    buf, handler, module_log = _capture_warnings(tl)
    try:
        lessons = tl.get_relevant_lessons(min_occurrences=3, limit=10)
        assert lessons == [], f"expected [], got {lessons!r}"
        warn_out = buf.getvalue()
        assert "LESSONS_ERA_FILTER_DEGRADED" in warn_out, \
            f"expected WARN, got: {warn_out!r}"
        print(f"  OK  all pre-327 → [] + WARN fires")
    finally:
        _release_handler(module_log, handler)


def test_3_mixed_returns_post_327_only_no_warn():
    """Pre-327 + post-327 snapshots, min_occurrences=2 → lessons from post-327 only."""
    snaps = [
        _fixture_snapshot(2001, "pre_FLO-327", direction="BUY"),
        _fixture_snapshot(2002, "pre_FLO-327", direction="BUY"),
        _fixture_snapshot(2003, "abc1234", direction="BUY"),  # post-327
        _fixture_snapshot(2004, "def5678", direction="BUY"),  # post-327
    ]
    trades = [
        {"ticket": 2001, "profit": -5.0},
        {"ticket": 2002, "profit": -5.0},
        {"ticket": 2003, "profit": -3.0},
        {"ticket": 2004, "profit": -2.0},
    ]
    snap_dir, db_path = _setup_tempdir(snaps, trades)
    tl = _reload_lessons_module(snap_dir, db_path)
    buf, handler, module_log = _capture_warnings(tl)
    try:
        lessons = tl.get_relevant_lessons(min_occurrences=2, limit=10)
        # Should return ONE lesson from the 2 post-327 trades
        assert len(lessons) == 1, f"expected 1 lesson, got {len(lessons)}: {lessons!r}"
        # Verify pnl_sum from post-327 ONLY (-3 + -2 = -5) not pre-327 (-5 + -5 = -10)
        assert lessons[0]["wins"] + lessons[0]["losses"] == 2, \
            f"expected 2 trades aggregated, got {lessons[0]}"
        avg = lessons[0]["avg_pnl"]
        assert abs(avg - (-2.5)) < 0.01, f"expected avg -2.5, got {avg}"
        warn_out = buf.getvalue()
        assert "LESSONS_ERA_FILTER_DEGRADED" not in warn_out, \
            f"WARN should NOT fire when processed > 0: {warn_out!r}"
        print(f"  OK  mixed → {len(lessons)} post-327 lessons, no WARN")
    finally:
        _release_handler(module_log, handler)


def test_4_post_327_under_threshold_returns_empty_no_warn():
    """post-327 snapshots but < min_occurrences → [] but WARN does NOT fire (processed > 0)."""
    snaps = [_fixture_snapshot(3001 + i, "abc1234", direction="SELL") for i in range(2)]
    trades = [{"ticket": s["ticket"], "profit": -1.0} for s in snaps]
    snap_dir, db_path = _setup_tempdir(snaps, trades)
    tl = _reload_lessons_module(snap_dir, db_path)
    buf, handler, module_log = _capture_warnings(tl)
    try:
        lessons = tl.get_relevant_lessons(min_occurrences=3, limit=10)
        assert lessons == [], f"expected [] (< min_occurrences), got {lessons!r}"
        warn_out = buf.getvalue()
        assert "LESSONS_ERA_FILTER_DEGRADED" not in warn_out, \
            f"WARN should NOT fire when processed > 0: {warn_out!r}"
        print(f"  OK  post-327 under threshold → [] no WARN")
    finally:
        _release_handler(module_log, handler)


def test_5_warn_fires_once_per_process():
    """Two consecutive calls with all-pre-327 → WARN fires on first, NOT second."""
    snaps = [_fixture_snapshot(4001 + i, "pre_FLO-327") for i in range(3)]
    trades = [{"ticket": s["ticket"], "profit": -1.0} for s in snaps]
    snap_dir, db_path = _setup_tempdir(snaps, trades)
    tl = _reload_lessons_module(snap_dir, db_path)
    buf, handler, module_log = _capture_warnings(tl)
    try:
        tl.get_relevant_lessons(min_occurrences=3, limit=10)
        first_warn_count = buf.getvalue().count("LESSONS_ERA_FILTER_DEGRADED")
        tl.get_relevant_lessons(min_occurrences=3, limit=10)
        second_warn_count = buf.getvalue().count("LESSONS_ERA_FILTER_DEGRADED")
        assert first_warn_count == 1, f"expected 1 WARN on first call, got {first_warn_count}"
        assert second_warn_count == 1, f"expected WARN not re-fired, got {second_warn_count}"
        print(f"  OK  WARN fires once ({first_warn_count}→{second_warn_count} across calls)")
    finally:
        _release_handler(module_log, handler)


def main() -> int:
    tests = [
        ("1. empty dir → [] no WARN",            test_1_empty_snapshot_dir_returns_empty_no_warn),
        ("2. all pre-327 → [] + WARN fires",     test_2_all_pre_327_returns_empty_warn_fires),
        ("3. mixed → post-327 only, no WARN",    test_3_mixed_returns_post_327_only_no_warn),
        ("4. post-327 under threshold → [] no WARN", test_4_post_327_under_threshold_returns_empty_no_warn),
        ("5. WARN fires once per process",       test_5_warn_fires_once_per_process),
    ]
    failed = 0
    for name, fn in tests:
        print(f"TEST {name}")
        try:
            fn()
        except AssertionError as e:
            print(f"  FAIL  {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR  {type(e).__name__}: {e}")
            import traceback; traceback.print_exc()
            failed += 1
    print()
    print(f"Results: {len(tests) - failed}/{len(tests)} passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
