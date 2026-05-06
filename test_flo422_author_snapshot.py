"""FLO-422 Step 3 — tests for the passive author-time snapshot helper.

Tests exercise `_maybe_persist_author_regime_snapshot` and its helpers
(`_flo422_fetch_m5_candles`, `_flo422_fetch_analyses`,
`_flo422_persist_snapshot`) with mocked I/O — no real MT5, no
data/history.db, no agent_tools instantiation.

Run: python test_flo422_author_snapshot.py
Exits non-zero on failure.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock


# ----------------------------------------------------------------------
# Test infrastructure
# ----------------------------------------------------------------------

def fail(label: str, msg: str) -> None:
    print(f"FAIL [{label}]: {msg}")
    sys.exit(1)


def passed(label: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"PASS [{label}]{suffix}")


def _make_plan(setup_type: str, direction: str = "BUY", entry_price: float = 4636.0,
               plan_id: str = "PLAN-20260507-TEST"):
    """Construct a minimal plan-shaped object that the helper can read.
    Uses SimpleNamespace to mimic the Pydantic Plan attribute access."""
    plan = types.SimpleNamespace()
    plan.id = plan_id
    plan.analysis = types.SimpleNamespace(setup_type=setup_type)
    plan.entry = types.SimpleNamespace(direction=direction, entry_price=entry_price)
    return plan


def _fake_m5_candles(n: int = 30):
    """Return a list of n M5 candle dicts with controlled compressed shape."""
    out = []
    px = 4600.0
    for i in range(n):
        o = px + (i % 3) * 0.1
        c = o + 0.3
        h = max(o, c) + 0.4
        l = min(o, c) - 0.3
        out.append({"open": o, "high": h, "low": l, "close": c})
        px = c
    return out


def _fake_analyses(n: int = 12, base_price: float = 4600.0):
    """Return a list of n analyses dicts spanning ~4 hours, with controlled
    BB-width / ATR / EMA values."""
    out = []
    for i in range(n):
        out.append({
            "timestamp": f"2026-05-07T{i:02d}:00:00Z",
            "current_price": base_price + i * 0.5,
            "atr_14": 10.0 + i * 0.1,
            "rsi_14": 55.0,
            "ema_50": base_price - 5.0,
            "bb_upper": base_price + 50.0 + i * 0.2,
            "bb_middle": base_price,
            "bb_lower": base_price - 50.0 - i * 0.2,
            "adx_14": 22.0,
        })
    return out


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_1_qualifying_setup_invokes_persist():
    """Happy path — breakout_range BUY plan with valid inputs writes a snapshot."""
    import agent_tools
    plan = _make_plan("breakout_range", direction="BUY", entry_price=4636.0)
    candles = _fake_m5_candles()
    analyses = _fake_analyses()
    persist_calls = []

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=candles), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=analyses), \
         patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
        agent_tools._maybe_persist_author_regime_snapshot(plan)

    if len(persist_calls) != 1:
        fail("test1.persist_called_once", f"got {len(persist_calls)} calls")
    pid, snap = persist_calls[0]
    if pid != "PLAN-20260507-TEST":
        fail("test1.plan_id", f"got {pid}")
    if snap["stage"] != "author":
        fail("test1.stage_author", f"got {snap['stage']!r}")
    if snap["direction"] != "BUY":
        fail("test1.direction_preserved", f"got {snap['direction']!r}")
    if snap["setup_type"] != "breakout_range":
        fail("test1.setup_preserved", f"got {snap['setup_type']!r}")
    passed("test1.qualifying_setup_persists_snapshot")


def test_2_non_qualifying_setup_skips():
    """mean_reversion_extreme is NOT in the lifecycle set — must skip."""
    import agent_tools
    plan = _make_plan("mean_reversion_extreme", direction="SELL")
    persist_calls = []

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
        agent_tools._maybe_persist_author_regime_snapshot(plan)

    if persist_calls:
        fail("test2.no_persist_for_non_qualifying", f"got {len(persist_calls)} unexpected calls")
    passed("test2.non_qualifying_setup_skipped")


def test_3_all_four_qualifying_setups_covered():
    """breakout_range, continuation_momentum, pullback_trend, structural_bounce — all snapshot."""
    import agent_tools
    qualifying = ["breakout_range", "continuation_momentum", "pullback_trend", "structural_bounce"]
    for setup in qualifying:
        plan = _make_plan(setup, plan_id=f"PLAN-TEST-{setup}")
        persist_calls = []
        with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
             patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
             patch.object(agent_tools, "_flo422_persist_snapshot",
                          side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
            agent_tools._maybe_persist_author_regime_snapshot(plan)
        if not persist_calls:
            fail(f"test3.{setup}", "expected snapshot persisted")
    passed("test3.all_four_qualifying_setups_covered")


def test_4_mt5_failure_is_fail_soft():
    """If MT5 returns no candles, snapshot still computes (with warnings)
    and persist still happens — submission MUST NOT crash."""
    import agent_tools
    plan = _make_plan("breakout_range", direction="BUY", entry_price=4636.0)
    persist_calls = []

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=[]), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
        # Must not raise
        agent_tools._maybe_persist_author_regime_snapshot(plan)

    if not persist_calls:
        fail("test4.persist_still_called", "expected persist with warning fields")
    snap = persist_calls[0][1]
    if "insufficient_m5_history" not in snap.get("computation_warnings", []):
        fail("test4.warning_recorded", f"got warnings={snap.get('computation_warnings')}")
    passed("test4.mt5_failure_fail_soft_with_warning")


def test_5_db_persist_failure_is_fail_soft():
    """If the persist UPDATE itself raises, the helper must swallow it
    (logged warning) and the submission caller is unaffected."""
    import agent_tools
    plan = _make_plan("breakout_range", direction="BUY", entry_price=4636.0)

    def explode(_pid, _snap):
        raise sqlite3.OperationalError("disk full simulated")

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot", side_effect=explode):
        try:
            agent_tools._maybe_persist_author_regime_snapshot(plan)
        except Exception as e:
            fail("test5.no_exception_propagates", f"helper raised {type(e).__name__}: {e}")
    passed("test5.db_persist_failure_fail_soft")


def test_6_compute_helper_failure_is_fail_soft():
    """If breakout_regime.compute_regime_snapshot raises (programmer error),
    the wrapping try/except in the helper must catch it."""
    import agent_tools
    plan = _make_plan("breakout_range", direction="BUY", entry_price=4636.0)
    persist_calls = []

    def bad_compute(*args, **kwargs):
        raise RuntimeError("simulated bug in compute path")

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))), \
         patch("breakout_regime.compute_regime_snapshot", side_effect=bad_compute):
        try:
            agent_tools._maybe_persist_author_regime_snapshot(plan)
        except Exception as e:
            fail("test6.no_exception_propagates", f"helper raised {type(e).__name__}: {e}")
    if persist_calls:
        fail("test6.no_persist_when_compute_failed", "should not persist on compute failure")
    passed("test6.compute_failure_fail_soft")


def test_7_malformed_plan_returns_silently():
    """A plan missing .analysis or .entry must not crash. Submission validation
    would have caught these before this helper runs, but defensive."""
    import agent_tools

    p1 = types.SimpleNamespace(id="PLAN-MALFORMED-1")  # no .analysis at all
    p2 = types.SimpleNamespace(id="PLAN-MALFORMED-2",
                               analysis=types.SimpleNamespace())  # no .setup_type
    persist_calls = []
    with patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
        for p in (p1, p2):
            try:
                agent_tools._maybe_persist_author_regime_snapshot(p)
            except Exception as e:
                fail(f"test7.{p.id}_no_raise", f"raised {type(e).__name__}: {e}")
    if persist_calls:
        fail("test7.no_persist_for_malformed", f"got {len(persist_calls)} unexpected persist calls")
    passed("test7.malformed_plans_silent_no_op")


def test_8_invalid_direction_skips():
    """Unknown direction (neither BUY nor SELL) skips."""
    import agent_tools
    plan = _make_plan("breakout_range", direction="HOLD", entry_price=4636.0)
    persist_calls = []
    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot",
                      side_effect=lambda pid, snap: persist_calls.append((pid, snap))):
        agent_tools._maybe_persist_author_regime_snapshot(plan)
    if persist_calls:
        fail("test8.no_persist_for_invalid_direction", "should skip")
    passed("test8.invalid_direction_skipped")


def test_9_persist_emits_valid_json():
    """The JSON written to the DB column must be parseable JSON."""
    import agent_tools
    plan = _make_plan("pullback_trend", direction="SELL", entry_price=4700.0)
    captured = {}
    real_persist = agent_tools._flo422_persist_snapshot

    def capture_persist(pid, snap):
        # Don't actually touch the DB. Just capture the JSON.
        captured["json"] = json.dumps(snap, default=str)
        captured["pid"] = pid

    with patch.object(agent_tools, "_flo422_fetch_m5_candles", return_value=_fake_m5_candles()), \
         patch.object(agent_tools, "_flo422_fetch_analyses", return_value=_fake_analyses()), \
         patch.object(agent_tools, "_flo422_persist_snapshot", side_effect=capture_persist):
        agent_tools._maybe_persist_author_regime_snapshot(plan)

    if "json" not in captured:
        fail("test9.persist_called", "persist was not called")
    parsed = json.loads(captured["json"])
    expected_keys = {"stage", "ts", "current_price", "direction", "setup_type",
                     "breakout_level", "impulse_total_60m", "bb_width_4h_pct",
                     "computation_warnings"}
    missing = expected_keys - set(parsed.keys())
    if missing:
        fail("test9.schema_complete", f"missing keys: {missing}")
    passed("test9.json_round_trips_valid_schema")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-422 Step 3 — author snapshot helper test suite")
    print("=" * 60)
    test_1_qualifying_setup_invokes_persist()
    test_2_non_qualifying_setup_skips()
    test_3_all_four_qualifying_setups_covered()
    test_4_mt5_failure_is_fail_soft()
    test_5_db_persist_failure_is_fail_soft()
    test_6_compute_helper_failure_is_fail_soft()
    test_7_malformed_plan_returns_silently()
    test_8_invalid_direction_skips()
    test_9_persist_emits_valid_json()
    print("=" * 60)
    print("ALL TESTS PASSED")
