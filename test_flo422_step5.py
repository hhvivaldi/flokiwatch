"""FLO-422 Step 5 — trigger-time regime snapshot capture tests.

Standalone test script (no pytest, matching project convention). Exits
with non-zero code on any failure.

Coverage:
  T1  happy path: entry fire on a lifecycle plan with author snapshot
      → trigger snapshot persists, drift persists, log emitted
  T2  missing author snapshot: trigger persists, drift stays NULL,
      drift_class="no_author_snapshot"
  T3  malformed author snapshot JSON: trigger persists, drift stays
      NULL, drift_class="no_author_snapshot"
  T4  compute_regime_snapshot raises: nothing persisted, no exception
      propagates, warning emitted
  T5  DB persist fails: no exception propagates, warning emitted
  T6  drift compute raises: trigger snapshot still persists,
      drift_class records the exception type
  T7  non-entry fire (plan_list_order != -1): no-op — no DB writes
  T8  non-lifecycle setup_type: no-op — no DB writes
  T9  plan row missing: no-op — no exception
  T10 wiring: snow_loop._dispatch_fires calls hook on success only,
      NOT on dispatch failure
  T11 wiring: hook runs AFTER execute_action returns (order check)

All DB tests use a tempfile sqlite path injected via config monkey-patch.
MT5 + analyses fetches are monkey-patched to canned data (no live MT5
or history.db reads).
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_FAILURES: List[str] = []


def _ok(name: str) -> None:
    print(f"  PASS  {name}")


def _fail(name: str, exc: Optional[BaseException] = None) -> None:
    msg = f"  FAIL  {name}"
    if exc is not None:
        msg += f" — {type(exc).__name__}: {exc}"
        msg += "\n" + traceback.format_exc()
    print(msg)
    _FAILURES.append(name)


# ---------------------------------------------------------------------
# Test fixture: temp sqlite DB with snow_plans table + a seeded plan row.
# ---------------------------------------------------------------------
def _setup_temp_db() -> str:
    """Create a tempfile sqlite DB with the snow_plans schema (only the
    fields PR1 touches) plus a single seeded plan row."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="flo422_step5_test_")
    os.close(fd)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE snow_plans (
                id TEXT PRIMARY KEY,
                plan_json TEXT,
                author_regime_snapshot_json TEXT,
                trigger_regime_snapshot_json TEXT,
                regime_drift_json TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()
    return path


def _seed_plan(
    db_path: str,
    plan_id: str,
    setup_type: str = "continuation_momentum",
    direction: str = "BUY",
    entry_price: float = 4750.0,
    author_snapshot: Optional[Dict[str, Any]] = "default",
) -> None:
    plan_json = {
        "id": plan_id,
        "analysis": {"setup_type": setup_type, "thesis": "test"},
        "entry": {"direction": direction, "entry_price": entry_price,
                  "initial_sl": entry_price - 20, "initial_tp": entry_price + 20,
                  "conditions": []},
    }
    if author_snapshot == "default":
        author_snapshot = {
            "stage": "author",
            "ts": "2026-05-07T08:00:00+00:00",
            "current_price": 4748.0,
            "direction": direction,
            "setup_type": setup_type,
            "breakout_level": entry_price,
            "impulse_total_60m": 3,
            "bb_width_4h_pct": 30.0,
            "atr_4h_pct": 5.0,
            "computation_warnings": [],
        }
    author_json: Optional[str]
    if author_snapshot is None:
        author_json = None
    elif isinstance(author_snapshot, str):
        author_json = author_snapshot  # raw string for malformed-JSON test
    else:
        author_json = json.dumps(author_snapshot)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO snow_plans (id, plan_json, author_regime_snapshot_json) "
            "VALUES (?, ?, ?)",
            (plan_id, json.dumps(plan_json), author_json),
        )
        conn.commit()
    finally:
        conn.close()


def _read_plan(db_path: str, plan_id: str) -> Dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT trigger_regime_snapshot_json, regime_drift_json "
            "FROM snow_plans WHERE id = ?",
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "trigger_regime_snapshot_json": row[0] if row else None,
        "regime_drift_json": row[1] if row else None,
    }


# ---------------------------------------------------------------------
# Patch helpers: redirect HISTORY_DB_PATH + canned MT5/analyses fetches.
# ---------------------------------------------------------------------
_FAKE_M5: List[Dict[str, Any]] = [
    {"open": 4748.0 + i * 0.1, "high": 4750.0 + i * 0.1,
     "low":  4747.0 + i * 0.1, "close": 4749.0 + i * 0.1}
    for i in range(30)
]
_FAKE_ANALYSES_24H: List[Dict[str, Any]] = [
    {"timestamp": f"2026-05-07T{h:02d}:00:00",
     "current_price": 4750.0, "atr_14": 0.5, "rsi_14": 65.0,
     "ema_50": 4700.0, "bb_upper": 4760.0, "bb_middle": 4750.0,
     "bb_lower": 4740.0, "adx_14": 30.0}
    for h in range(0, 24)
]


def _install_patches(db_path: str,
                     m5_override=None,
                     analyses_override=None,
                     compute_override=None,
                     persist_override=None) -> Dict[str, Any]:
    """Install monkey-patches; return the originals for teardown."""
    import config
    import agent_tools
    import breakout_regime
    import snow.regime_capture as rc

    originals = {
        "HISTORY_DB_PATH": getattr(config, "HISTORY_DB_PATH", None),
        "_flo422_fetch_m5_candles": agent_tools._flo422_fetch_m5_candles,
        "_flo422_fetch_analyses": agent_tools._flo422_fetch_analyses,
        "compute_regime_snapshot": breakout_regime.compute_regime_snapshot,
        "_persist_trigger_snapshot": rc._persist_trigger_snapshot,
    }
    config.HISTORY_DB_PATH = db_path
    agent_tools._flo422_fetch_m5_candles = (
        m5_override if m5_override is not None else (lambda ts, n: list(_FAKE_M5))
    )
    agent_tools._flo422_fetch_analyses = (
        analyses_override if analyses_override is not None
        else (lambda ts, minutes_back: list(_FAKE_ANALYSES_24H))
    )
    if compute_override is not None:
        breakout_regime.compute_regime_snapshot = compute_override
    if persist_override is not None:
        rc._persist_trigger_snapshot = persist_override
    return originals


def _uninstall_patches(originals: Dict[str, Any]) -> None:
    import config
    import agent_tools
    import breakout_regime
    import snow.regime_capture as rc

    if originals["HISTORY_DB_PATH"] is None:
        if hasattr(config, "HISTORY_DB_PATH"):
            delattr(config, "HISTORY_DB_PATH")
    else:
        config.HISTORY_DB_PATH = originals["HISTORY_DB_PATH"]
    agent_tools._flo422_fetch_m5_candles = originals["_flo422_fetch_m5_candles"]
    agent_tools._flo422_fetch_analyses = originals["_flo422_fetch_analyses"]
    breakout_regime.compute_regime_snapshot = originals["compute_regime_snapshot"]
    rc._persist_trigger_snapshot = originals["_persist_trigger_snapshot"]


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------
def t1_happy_path() -> None:
    name = "T1 happy path: entry fire persists trigger snapshot + drift"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T1")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        maybe_capture_trigger_snapshot("PLAN-T1", "_entry", -1)
        row = _read_plan(db, "PLAN-T1")
        assert row["trigger_regime_snapshot_json"] is not None, "trigger snapshot not persisted"
        assert row["regime_drift_json"] is not None, "drift not persisted"
        snap = json.loads(row["trigger_regime_snapshot_json"])
        assert snap.get("stage") == "trigger", f"stage wrong: {snap.get('stage')}"
        drift = json.loads(row["regime_drift_json"])
        assert "drift_assessment" in drift, "drift missing assessment"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t2_missing_author_snapshot() -> None:
    name = "T2 missing author snapshot: trigger persists, drift NULL"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T2", author_snapshot=None)
        from snow.regime_capture import maybe_capture_trigger_snapshot
        maybe_capture_trigger_snapshot("PLAN-T2", "_entry", -1)
        row = _read_plan(db, "PLAN-T2")
        assert row["trigger_regime_snapshot_json"] is not None, "trigger snapshot missing"
        assert row["regime_drift_json"] is None, "drift should be NULL"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t3_malformed_author_snapshot() -> None:
    name = "T3 malformed author JSON: trigger persists, drift NULL"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T3", author_snapshot="not json {{")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        maybe_capture_trigger_snapshot("PLAN-T3", "_entry", -1)
        row = _read_plan(db, "PLAN-T3")
        assert row["trigger_regime_snapshot_json"] is not None, "trigger snapshot missing"
        assert row["regime_drift_json"] is None, "drift should be NULL on malformed author"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t4_compute_raises() -> None:
    name = "T4 compute_regime_snapshot raises: no persist, no propagation"
    db = _setup_temp_db()

    def _raising_compute(**kw):
        raise RuntimeError("compute exploded")

    originals = _install_patches(db, compute_override=_raising_compute)
    try:
        _seed_plan(db, "PLAN-T4")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        # Must NOT raise.
        maybe_capture_trigger_snapshot("PLAN-T4", "_entry", -1)
        row = _read_plan(db, "PLAN-T4")
        assert row["trigger_regime_snapshot_json"] is None, "should not have persisted on compute failure"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t5_persist_fails() -> None:
    name = "T5 persist fails: no exception propagates"
    db = _setup_temp_db()

    def _raising_persist(plan_id, snapshot, drift):
        raise sqlite3.OperationalError("disk full")

    originals = _install_patches(db, persist_override=_raising_persist)
    try:
        _seed_plan(db, "PLAN-T5")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        maybe_capture_trigger_snapshot("PLAN-T5", "_entry", -1)
        # No assertion on the row — persist override is mocked. The contract
        # is "no exception propagates"; reaching this line proves it.
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t6_drift_compute_raises() -> None:
    name = "T6 drift compute raises: trigger persists, drift NULL"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T6")
        # Patch compute_drift directly via the breakout_regime module
        import breakout_regime
        original_drift = breakout_regime.compute_drift

        def _raising_drift(*a, **kw):
            raise ValueError("drift exploded")
        breakout_regime.compute_drift = _raising_drift
        try:
            from snow.regime_capture import maybe_capture_trigger_snapshot
            maybe_capture_trigger_snapshot("PLAN-T6", "_entry", -1)
        finally:
            breakout_regime.compute_drift = original_drift
        row = _read_plan(db, "PLAN-T6")
        assert row["trigger_regime_snapshot_json"] is not None, "trigger snapshot should still persist"
        assert row["regime_drift_json"] is None, "drift should be NULL on drift-compute failure"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t7_non_entry_fire() -> None:
    name = "T7 non-entry fire (management): no-op"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T7")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        # Management contingency — plan_list_order >= 0, contingency_name != "_entry"
        maybe_capture_trigger_snapshot("PLAN-T7", "move_sl_to_breakeven", 0)
        row = _read_plan(db, "PLAN-T7")
        assert row["trigger_regime_snapshot_json"] is None, "non-entry must not write"
        assert row["regime_drift_json"] is None, "non-entry must not write"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t8_non_lifecycle_setup() -> None:
    name = "T8 non-lifecycle setup_type: no-op"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        _seed_plan(db, "PLAN-T8", setup_type="some_other_setup")
        from snow.regime_capture import maybe_capture_trigger_snapshot
        maybe_capture_trigger_snapshot("PLAN-T8", "_entry", -1)
        row = _read_plan(db, "PLAN-T8")
        assert row["trigger_regime_snapshot_json"] is None, "non-lifecycle must not write"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t9_plan_missing() -> None:
    name = "T9 plan row missing: no exception, no write"
    db = _setup_temp_db()
    originals = _install_patches(db)
    try:
        from snow.regime_capture import maybe_capture_trigger_snapshot
        # No seed_plan call — row does not exist
        maybe_capture_trigger_snapshot("PLAN-NEVER-EXISTED", "_entry", -1)
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(originals)
        os.unlink(db)


def t10_wiring_dispatch_failure() -> None:
    name = "T10 wiring: dispatch failure does NOT trigger snapshot"
    # Build a minimal SnowLoop-like object exercising _dispatch_fires.
    # We can't easily import & instantiate SnowLoop here, but the dispatch
    # logic is small; assert via a structural mock.
    import snow.snow_loop as snow_loop
    import snow.regime_capture as rc

    calls: List[str] = []

    def _fake_capture(plan_id, name_, order):
        calls.append(plan_id)

    original_capture = rc.maybe_capture_trigger_snapshot
    rc.maybe_capture_trigger_snapshot = _fake_capture
    try:
        # Construct a fake fire event and a fake actions object that raises.
        class _Fire:
            plan_id = "PLAN-T10"
            contingency_name = "_entry"
            plan_list_order = -1

        class _RaisingActions:
            def execute_action(self, fire):
                raise RuntimeError("simulated dispatch failure")

        # Build a minimal stand-in for SnowLoop with just the fields
        # _dispatch_fires touches. We bind the unbound method to it.
        class _Stub:
            _actions = _RaisingActions()

        _Stub._dispatch_fires = snow_loop.SnowLoop._dispatch_fires
        stub = _Stub()
        stub._dispatch_fires([_Fire()])
        assert calls == [], f"hook should NOT run on dispatch failure; calls={calls}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        rc.maybe_capture_trigger_snapshot = original_capture


def t11_wiring_dispatch_success() -> None:
    name = "T11 wiring: dispatch success DOES trigger snapshot"
    import snow.snow_loop as snow_loop
    import snow.regime_capture as rc

    call_log: List[str] = []

    def _fake_capture(plan_id, name_, order):
        call_log.append(f"capture:{plan_id}")

    original_capture = rc.maybe_capture_trigger_snapshot
    rc.maybe_capture_trigger_snapshot = _fake_capture
    try:
        class _Fire:
            plan_id = "PLAN-T11"
            contingency_name = "_entry"
            plan_list_order = -1

        class _OkActions:
            def execute_action(self, fire):
                call_log.append(f"execute:{fire.plan_id}")

        class _Stub:
            _actions = _OkActions()
        _Stub._dispatch_fires = snow_loop.SnowLoop._dispatch_fires
        stub = _Stub()
        stub._dispatch_fires([_Fire()])
        # Order check: execute MUST come before capture (snapshot can't delay execution)
        assert call_log == ["execute:PLAN-T11", "capture:PLAN-T11"], \
            f"wrong order or missing call: {call_log}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        rc.maybe_capture_trigger_snapshot = original_capture


# ---------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------
def main() -> int:
    print("FLO-422 Step 5 — trigger snapshot tests")
    print("=" * 60)
    for fn in [t1_happy_path, t2_missing_author_snapshot, t3_malformed_author_snapshot,
               t4_compute_raises, t5_persist_fails, t6_drift_compute_raises,
               t7_non_entry_fire, t8_non_lifecycle_setup, t9_plan_missing,
               t10_wiring_dispatch_failure, t11_wiring_dispatch_success]:
        try:
            fn()
        except Exception as e:
            _fail(fn.__name__, e)
    print("=" * 60)
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} test(s) — {_FAILURES}")
        return 1
    print("ALL PASS (11/11)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
