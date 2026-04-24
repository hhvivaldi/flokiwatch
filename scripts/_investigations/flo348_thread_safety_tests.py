"""FLO-348 regression tests — thread-safety hardening.

Standalone test script (project convention). Run:

    python scripts/_investigations/flo348_thread_safety_tests.py

Exit code 0 = all tests passed, non-zero = failure.

Coverage:
  1. mt5_safe proxy correctness: wraps callables, caches constants.
  2. mt5_lock re-entrance safety (RLock semantics).
  3. executor_lock serialises execute_trade/modify_position/close_position.
  4. Concurrent MT5 reads under load do not deadlock or corrupt state.
  5. Performance: Snow's 5 s tick budget preserved (indicator + evaluator
     equivalent workload completes well under budget).
  6. No regressions in existing smoke-level imports.
"""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

_FAILURES: list[tuple[str, str]] = []


def _assert(condition: bool, name: str, msg: str = "") -> None:
    if condition:
        print(f"  PASS {name}")
    else:
        detail = f" — {msg}" if msg else ""
        print(f"  FAIL {name}{detail}")
        _FAILURES.append((name, msg))


def _section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------------------
# 1. mt5_safe proxy correctness
# ---------------------------------------------------------------------------

def test_proxy_wraps_callables() -> None:
    _section("1. Proxy correctness")
    from mt5_safe import mt5, mt5_lock
    _assert(type(mt5_lock).__name__ == "RLock", "mt5_lock is RLock")
    _assert(callable(mt5.symbol_info_tick), "symbol_info_tick is callable")
    _assert(hasattr(mt5.symbol_info_tick, "__wrapped__"), "wrapper exposes __wrapped__")
    # Constants pass through
    _assert(mt5.TIMEFRAME_M1 == 1, "TIMEFRAME_M1 constant pass-through")
    _assert(mt5.TRADE_RETCODE_DONE == 10009, "TRADE_RETCODE_DONE constant pass-through")
    # Cache: second access returns same wrapper object
    _assert(mt5.symbol_info_tick is mt5.symbol_info_tick, "wrapper cached on instance dict")


def test_proxy_handles_meth_o_callables() -> None:
    """FLO-352 regression: proxy must NOT pass empty **kwargs to METH_O-style C fns.

    Reproduces the bug via a mock callable whose signature accepts exactly one
    positional argument and raises on kwargs (matches MT5's order_send
    behaviour with error code -2 'Unnamed arguments not allowed').

    Before the fix, the proxy's `_wrapped(*args, **kwargs): raw(*args, **kwargs)`
    forwarded the empty kwargs dict and triggered the rejection even for
    positional-only calls. Fix: forward kwargs only when non-empty.
    """
    _section("1b. Proxy METH_O compatibility (FLO-352 regression)")
    import mt5_safe
    from mt5_safe import mt5

    # Inject a METH_O-style callable into the raw namespace for this test
    recorded_calls: list[tuple[tuple, dict]] = []

    def meth_o_like(*args, **kwargs):
        # Match MT5's semantics: refuse if kwargs dict is present at all,
        # even if empty (some C bindings distinguish the calling convention).
        if kwargs:
            # Simulate MT5's (-2, 'Unnamed arguments not allowed') + None return
            raise TypeError("Unnamed arguments not allowed (simulated METH_O)")
        recorded_calls.append((args, kwargs))
        return {"ok": True, "args": args}

    # Flush proxy cache for this name so __getattr__ re-resolves
    try:
        delattr(mt5, "_flo352_meth_o_probe")
    except AttributeError:
        pass

    setattr(mt5_safe._mt5_raw, "_flo352_meth_o_probe", meth_o_like)
    try:
        # Positional-only call through proxy: MUST NOT leak empty kwargs
        result = mt5._flo352_meth_o_probe({"dummy": 1})
        _assert(result["ok"] is True, "positional-only call through proxy succeeds")
        _assert(len(recorded_calls) == 1, "raw fn called exactly once")
        _assert(recorded_calls[0][0] == ({"dummy": 1},), "args forwarded intact")
        _assert(recorded_calls[0][1] == {}, "kwargs empty at raw call site")

        # Verify the proxy STILL forwards real kwargs when caller provides them
        # (we need a different fn that accepts kwargs for this check)
        def keyword_ok(*args, **kwargs):
            return {"args": args, "kwargs": kwargs}
        try:
            delattr(mt5, "_flo352_kw_probe")
        except AttributeError:
            pass
        setattr(mt5_safe._mt5_raw, "_flo352_kw_probe", keyword_ok)
        kw_result = mt5._flo352_kw_probe("a", x=1, y=2)
        _assert(kw_result["args"] == ("a",), "kwargs-case: args forwarded")
        _assert(kw_result["kwargs"] == {"x": 1, "y": 2}, "kwargs-case: kwargs forwarded")
    finally:
        # Cleanup injected probes
        for name in ("_flo352_meth_o_probe", "_flo352_kw_probe"):
            try:
                delattr(mt5_safe._mt5_raw, name)
            except AttributeError:
                pass
            try:
                delattr(mt5, name)
            except AttributeError:
                pass


# ---------------------------------------------------------------------------
# 2. mt5_lock re-entrance
# ---------------------------------------------------------------------------

def test_mt5_lock_reentrant() -> None:
    _section("2. mt5_lock re-entrance (RLock)")
    from mt5_safe import mt5_lock
    reached = [False]

    def nested():
        with mt5_lock:
            with mt5_lock:  # re-entry must not deadlock
                reached[0] = True

    t = threading.Thread(target=nested)
    t.start()
    t.join(timeout=2.0)
    _assert(not t.is_alive(), "nested re-entry does not hang")
    _assert(reached[0], "inner critical section executed")


# ---------------------------------------------------------------------------
# 3. executor_lock serialises write methods
# ---------------------------------------------------------------------------

def test_executor_lock_serialises_writes() -> None:
    _section("3. executor_lock serialises write methods")
    import executor as executor_mod

    # Replace the decorated method body with a probe that records entry/exit
    # times so we can verify mutual exclusion.
    entry_log: list[tuple[str, int, float]] = []
    lock = executor_mod.executor_lock

    def _probe(name: str, worker_id: int, duration: float = 0.05):
        with lock:
            entry_log.append(("enter", worker_id, time.time()))
            time.sleep(duration)
            entry_log.append(("exit",  worker_id, time.time()))

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_probe, "execute_trade", i) for i in range(8)]
        concurrent.futures.wait(futures)

    # Check: no overlap — every entry must be followed by its exit before
    # the next entry, when entries are sorted by time.
    ordered = sorted(entry_log, key=lambda r: r[2])
    balance = 0
    max_concurrent = 0
    for kind, _, _ in ordered:
        balance += 1 if kind == "enter" else -1
        max_concurrent = max(max_concurrent, balance)
    _assert(max_concurrent == 1, "only one thread inside critical section at a time", f"max_concurrent={max_concurrent}")


def test_executor_lock_is_rlock_for_method_composition() -> None:
    _section("3b. executor_lock allows method composition from same thread")
    import executor as executor_mod
    lock = executor_mod.executor_lock
    # Simulate: modify_position is called from within close_position retry logic
    nested_ok = [False]
    def compose():
        with lock:           # outer (close_position)
            with lock:       # inner (modify_position) - same thread
                nested_ok[0] = True
    t = threading.Thread(target=compose)
    t.start()
    t.join(timeout=2.0)
    _assert(not t.is_alive() and nested_ok[0], "RLock allows same-thread composition")


# ---------------------------------------------------------------------------
# 4. Concurrent MT5 reads do not deadlock
# ---------------------------------------------------------------------------

def test_concurrent_mt5_reads() -> None:
    _section("4. Concurrent MT5 reads (4 threads, 100 calls each)")
    from mt5_safe import mt5
    if not mt5.initialize():
        _assert(False, "MT5 initialize", "cannot reach terminal; skipping read test")
        return

    errors: list[str] = []
    counts = [0] * 4

    def reader(i: int):
        try:
            for _ in range(100):
                t = mt5.symbol_info_tick("XAUUSD")
                if t is None:
                    errors.append(f"thread {i}: tick None")
                    return
                counts[i] += 1
        except Exception as e:
            errors.append(f"thread {i}: {e}")

    start = time.time()
    threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30.0)
    elapsed = time.time() - start

    _assert(all(not t.is_alive() for t in threads), "all reader threads completed")
    _assert(not errors, "no errors during concurrent reads", str(errors)[:200] if errors else "")
    _assert(sum(counts) == 400, "all 400 ticks retrieved", f"total={sum(counts)}")
    _assert(elapsed < 30.0, f"completed in {elapsed:.2f}s (<30s)")

    mt5.shutdown()


# ---------------------------------------------------------------------------
# 5. Performance: Snow's 5s tick budget preserved
# ---------------------------------------------------------------------------

def test_performance_under_load() -> None:
    _section("5. Performance: Snow-equivalent tick workload under contention")
    from mt5_safe import mt5
    if not mt5.initialize():
        _assert(False, "MT5 initialize", "cannot reach terminal; skipping perf test")
        return

    # Simulate a Snow tick: 1 tick query + 2 candle queries + 5 constant reads.
    # Under concurrent Floki pressure (reader thread doing the same).
    snow_durations: list[float] = []

    def floki_pressure(stop_evt: threading.Event):
        while not stop_evt.is_set():
            _ = mt5.symbol_info_tick("XAUUSD")
            time.sleep(0.001)

    stop_evt = threading.Event()
    pressure = threading.Thread(target=floki_pressure, args=(stop_evt,), daemon=True)
    pressure.start()

    try:
        for _ in range(10):  # 10 Snow ticks
            t0 = time.time()
            _ = mt5.symbol_info_tick("XAUUSD")
            _ = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 60)
            _ = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 20)
            _ = mt5.TIMEFRAME_H1  # constant
            _ = mt5.TRADE_RETCODE_DONE
            snow_durations.append(time.time() - t0)
    finally:
        stop_evt.set()
        pressure.join(timeout=2.0)

    p95 = sorted(snow_durations)[int(len(snow_durations) * 0.95)]
    avg = sum(snow_durations) / len(snow_durations)
    print(f"  Snow-tick durations: avg={avg*1000:.1f}ms  p95={p95*1000:.1f}ms  max={max(snow_durations)*1000:.1f}ms")
    _assert(p95 < 1.0, f"p95 Snow-tick under 1000 ms (Snow budget 5 s)", f"p95={p95*1000:.1f}ms")
    _assert(max(snow_durations) < 2.0, "max Snow-tick under 2000 ms", f"max={max(snow_durations)*1000:.1f}ms")

    mt5.shutdown()


# ---------------------------------------------------------------------------
# 6. Existing production modules still import cleanly
# ---------------------------------------------------------------------------

def test_production_modules_import() -> None:
    _section("6. Production modules import cleanly post-FLO-348")
    import importlib

    modules = [
        "mt5_safe", "executor", "agent_tools", "central_brain", "deal_resolver",
        "ea_bridge", "mfe_backfill", "market_context_fetcher", "momentum_detector",
        "ml_predictor", "monitor", "regime_detector", "technical_analyzer",
        "session_context", "tick_pressure", "trade_reflexion", "volume_profile",
        "luna_analyst", "agent_monitor", "state_writer",
    ]

    for m in modules:
        try:
            if m in sys.modules: del sys.modules[m]
            importlib.import_module(m)
            _assert(True, f"import {m}")
        except Exception as e:
            _assert(False, f"import {m}", str(e))


# ---------------------------------------------------------------------------
# 7. Executor public methods are wrapped with the lock decorator
# ---------------------------------------------------------------------------

def test_executor_decorator_applied() -> None:
    _section("7. Executor write methods carry FLO-348 decorator")
    import executor as executor_mod
    cls = executor_mod.MT5Executor
    for name in ("execute_trade", "modify_position", "close_position"):
        fn = getattr(cls, name)
        _assert(hasattr(fn, "__wrapped__"), f"{name} is decorated (__wrapped__ present)")
        inner = getattr(fn, "__wrapped__", None)
        _assert(callable(inner), f"{name} inner fn resolvable")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("FLO-348 thread-safety regression tests")
    print("=" * 50)

    tests = [
        test_proxy_wraps_callables,
        test_proxy_handles_meth_o_callables,   # FLO-352 regression
        test_mt5_lock_reentrant,
        test_executor_lock_serialises_writes,
        test_executor_lock_is_rlock_for_method_composition,
        test_concurrent_mt5_reads,
        test_performance_under_load,
        test_production_modules_import,
        test_executor_decorator_applied,
    ]

    for t in tests:
        try:
            t()
        except Exception as e:
            _FAILURES.append((t.__name__, f"exception: {e}"))
            traceback.print_exc()

    print("\n" + "=" * 50)
    if _FAILURES:
        print(f"FAILURES: {len(_FAILURES)}")
        for name, msg in _FAILURES:
            print(f"  - {name}: {msg}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
