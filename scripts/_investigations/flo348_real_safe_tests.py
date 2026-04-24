"""FLO-348 real-demo tests 1, 3, 4 — SAFE to run alongside live bot.

Replaces FakeMT5 mock-based tests with real MT5 demo account calls.
All three tests are READ-ONLY (no orders placed) and have no financial
side effects. Safe to run while Floki / monitor / Simba / Echo / Luna
/ Rex Monitor are all active.

USAGE

    python scripts/_investigations/flo348_real_safe_tests.py --test 1
    python scripts/_investigations/flo348_real_safe_tests.py --test 3
    python scripts/_investigations/flo348_real_safe_tests.py --test 4 --duration-min 60

Tests:
  1. Read concurrency:        4 threads × 100 calls each, ~30 s
  3. Exception lock release:  force raise inside locked call, ~30 s
  4. Snow load simulator:     daemon thread mimicking Snow's 5 s cycle,
                              default 60 min; use --duration-min to adjust

Exit code: 0 = all selected tests PASS, non-zero = failure.
"""

from __future__ import annotations

import argparse
import gc
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Shared infrastructure
# ---------------------------------------------------------------------------

_FAILURES: list[tuple[str, str]] = []


def _assert(cond: bool, name: str, msg: str = "") -> None:
    if cond:
        print(f"  PASS {name}")
    else:
        detail = f" — {msg}" if msg else ""
        print(f"  FAIL {name}{detail}")
        _FAILURES.append((name, msg))


def _ensure_mt5_alive() -> bool:
    """Verify MT5 is reachable. Initialize if not already."""
    from mt5_safe import mt5
    if not mt5.initialize():
        print(f"  ABORT: mt5.initialize() returned False; last_error={mt5.last_error()}")
        return False
    tick = mt5.symbol_info_tick("XAUUSD")
    if tick is None or tick.bid <= 0:
        print(f"  ABORT: no live tick for XAUUSD; broker likely offline")
        return False
    print(f"  MT5 alive — XAUUSD bid={tick.bid} ask={tick.ask}")
    return True


# ---------------------------------------------------------------------------
# Test 1 — Real MT5 read concurrency
# ---------------------------------------------------------------------------

def test1_read_concurrency() -> None:
    print("\n=== TEST 1 — Real MT5 read concurrency (4 threads × 100 calls) ===")
    from mt5_safe import mt5, mt5_lock

    if not _ensure_mt5_alive():
        _assert(False, "precheck", "MT5 not alive")
        return

    per_thread_counts = [0, 0, 0, 0]
    per_thread_errors: list[str] = []
    lock_wait_times: list[float] = []

    def reader(thread_id: int) -> None:
        for i in range(100):
            t0 = time.time()
            try:
                acq = mt5_lock.acquire(timeout=5.0)
                wait = time.time() - t0
                lock_wait_times.append(wait)
                if not acq:
                    per_thread_errors.append(f"thread {thread_id} call {i}: lock timeout")
                    return
                try:
                    tick = mt5.symbol_info_tick("XAUUSD")
                finally:
                    mt5_lock.release()
                if tick is None or tick.bid <= 0:
                    per_thread_errors.append(f"thread {thread_id} call {i}: invalid tick")
                    return
                per_thread_counts[thread_id] += 1
            except Exception as e:
                per_thread_errors.append(f"thread {thread_id} call {i}: {e}")
                return

    start = time.time()
    threads = [threading.Thread(target=reader, args=(i,)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=120.0)
    elapsed = time.time() - start

    _assert(all(not t.is_alive() for t in threads), "all 4 reader threads completed")
    _assert(not per_thread_errors, "no errors during concurrent reads",
            str(per_thread_errors[:5]))
    _assert(sum(per_thread_counts) == 400, "all 400 ticks retrieved",
            f"actual={sum(per_thread_counts)}")
    if lock_wait_times:
        p95 = sorted(lock_wait_times)[int(len(lock_wait_times) * 0.95)]
        max_wait = max(lock_wait_times)
        print(f"  lock wait: p95={p95*1000:.2f}ms  max={max_wait*1000:.2f}ms  n={len(lock_wait_times)}")
        _assert(p95 < 1.0, f"p95 lock-wait under 1000 ms",
                f"p95={p95*1000:.2f}ms")
    print(f"  total elapsed: {elapsed:.2f}s")


# ---------------------------------------------------------------------------
# Test 3 — Exception release
# ---------------------------------------------------------------------------

def test3_exception_release() -> None:
    print("\n=== TEST 3 — Forced exception inside locked MT5 call ===")
    import mt5_safe
    from mt5_safe import mt5, mt5_lock

    if not _ensure_mt5_alive():
        _assert(False, "precheck", "MT5 not alive")
        return

    # Save original
    raw = mt5_safe._mt5_raw
    original = getattr(raw, "symbol_info_tick", None)
    if original is None:
        _assert(False, "setup", "symbol_info_tick missing")
        return

    # Inject a raising replacement — this is what the wrapper will see inside the RLock
    class _BoomRaised(RuntimeError):
        pass

    def _boom(*args, **kwargs):
        raise _BoomRaised("forced-exception-inside-locked-call")

    # Flush the proxy cache for this name so our replacement is re-read
    try:
        delattr(mt5, "symbol_info_tick")
    except AttributeError:
        pass

    setattr(raw, "symbol_info_tick", _boom)

    exc_caught: list[Exception] = []
    try:
        try:
            mt5.symbol_info_tick("XAUUSD")
        except _BoomRaised as e:
            exc_caught.append(e)

        _assert(len(exc_caught) == 1, "exception propagated to caller",
                f"caught={len(exc_caught)}")

        # Critical assertion: lock released after exception
        # A different thread tries to acquire within short timeout
        acquired_from_other_thread = threading.Event()

        def try_acquire():
            if mt5_lock.acquire(timeout=1.0):
                try:
                    acquired_from_other_thread.set()
                finally:
                    mt5_lock.release()

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join(timeout=3.0)
        _assert(acquired_from_other_thread.is_set(),
                "lock released after exception (other thread acquired within 1 s)")

    finally:
        # Restore
        setattr(raw, "symbol_info_tick", original)
        try:
            delattr(mt5, "symbol_info_tick")
        except AttributeError:
            pass

    # Post-restore sanity
    tick_after = mt5.symbol_info_tick("XAUUSD")
    _assert(tick_after is not None and tick_after.bid > 0,
            "normal operation resumes after exception + restore",
            f"tick={tick_after}")


# ---------------------------------------------------------------------------
# Test 4 — Snow load simulator
# ---------------------------------------------------------------------------

def _rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0


def _macd_histogram(series: pd.Series) -> float:
    ema_fast = series.ewm(span=12, adjust=False).mean()
    ema_slow = series.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    return float(hist.iloc[-1])


def _ema(series: pd.Series, period: int) -> float:
    return float(series.ewm(span=period, adjust=False).mean().iloc[-1])


def test4_snow_load_simulator(duration_min: float) -> None:
    print(f"\n=== TEST 4 — Snow load simulator ({duration_min:.0f} min soak) ===")
    from mt5_safe import mt5

    if not _ensure_mt5_alive():
        _assert(False, "precheck", "MT5 not alive")
        return

    mem_tracking = False
    start_mem_kb = 0
    get_mem_kb: Optional[Callable[[], int]] = None
    if sys.platform != "win32":
        try:
            import resource
            start_mem_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            get_mem_kb = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem_tracking = True
        except Exception:
            pass
    else:
        try:
            import psutil
            proc = psutil.Process()
            start_mem_kb = proc.memory_info().rss // 1024
            get_mem_kb = lambda: psutil.Process().memory_info().rss // 1024
            mem_tracking = True
        except ImportError:
            pass

    deadline = time.time() + (duration_min * 60)
    stop_evt = threading.Event()

    tick_durations: list[float] = []
    mt5_errors: list[str] = []
    cycle_count = [0]

    def snow_cycle() -> None:
        """One Snow tick: fetch, compute, sleep. Matches production cadence."""
        while not stop_evt.is_set() and time.time() < deadline:
            t0 = time.time()
            try:
                tick = mt5.symbol_info_tick("XAUUSD")
                if tick is None:
                    mt5_errors.append(f"t={time.time():.0f}: tick None")
                    time.sleep(5)
                    continue

                # Fetch M1 bars (Snow's live indicator source)
                rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M1, 0, 120)
                if rates is None or len(rates) < 30:
                    mt5_errors.append(f"t={time.time():.0f}: M1 bars={len(rates) if rates is not None else 0}")
                    time.sleep(5)
                    continue

                # Compute the indicators Snow will actually compute per cycle
                df = pd.DataFrame(rates)
                close = df["close"]
                rsi_val = _rsi(close, 14)
                macd_h = _macd_histogram(close)
                ema50 = _ema(close, 50)
                ema200 = _ema(close, min(200, len(close)))  # fall back to max available
                # Prevent dead-code elim; log every 20th cycle
                if cycle_count[0] % 20 == 0:
                    print(
                        f"  cycle {cycle_count[0]}  "
                        f"bid={tick.bid:.2f}  rsi={rsi_val:.1f}  macd_h={macd_h:+.3f}  "
                        f"ema50={ema50:.2f}  ema200={ema200:.2f}  "
                        f"tick_dur={(time.time()-t0)*1000:.0f}ms"
                    )

                cycle_count[0] += 1
                tick_durations.append(time.time() - t0)

            except Exception as e:
                mt5_errors.append(f"t={time.time():.0f}: {type(e).__name__}: {e}")

            # Sleep to next 5 s boundary (interruptible)
            sleep_target = t0 + 5.0
            while time.time() < sleep_target and not stop_evt.is_set():
                time.sleep(min(0.5, max(0.0, sleep_target - time.time())))

    thread = threading.Thread(target=snow_cycle, name="SnowLoadSim", daemon=True)
    thread.start()

    print(f"  Started Snow load simulator thread; running {duration_min:.0f} minutes...")
    print(f"  Press Ctrl+C once to stop early (will finalize gracefully).")

    try:
        while thread.is_alive() and time.time() < deadline:
            time.sleep(10)
            # Periodic heartbeat
            if cycle_count[0] > 0:
                last_avg = sum(tick_durations[-20:]) / min(20, len(tick_durations))
                remaining_min = max(0, (deadline - time.time()) / 60)
                print(f"  [heartbeat] cycles={cycle_count[0]}  last20-avg={last_avg*1000:.0f}ms  "
                      f"errors={len(mt5_errors)}  remaining={remaining_min:.1f}min")
    except KeyboardInterrupt:
        print("\n  interrupted — stopping gracefully...")

    stop_evt.set()
    thread.join(timeout=10.0)

    # Metrics
    if not tick_durations:
        _assert(False, "any cycles completed", "cycle_count=0")
        return

    avg = sum(tick_durations) / len(tick_durations)
    p95 = sorted(tick_durations)[int(len(tick_durations) * 0.95)]
    p99 = sorted(tick_durations)[int(len(tick_durations) * 0.99)]
    maxd = max(tick_durations)
    elapsed_min = (time.time() - (deadline - duration_min * 60)) / 60

    print(f"\n  --- Snow load simulator summary ---")
    print(f"  duration:        {elapsed_min:.1f} min")
    print(f"  cycles:          {cycle_count[0]}")
    print(f"  mt5 errors:      {len(mt5_errors)}")
    print(f"  tick duration — avg: {avg*1000:.1f}ms  p95: {p95*1000:.1f}ms  "
          f"p99: {p99*1000:.1f}ms  max: {maxd*1000:.1f}ms")

    if mem_tracking and get_mem_kb is not None:
        end_mem_kb = get_mem_kb()
        delta_mb = (end_mem_kb - start_mem_kb) / 1024
        print(f"  memory delta:    +{delta_mb:.1f}MB (rss peak)")
        _assert(delta_mb < 200, f"memory delta under 200 MB (leak sentinel)",
                f"delta={delta_mb:.1f}MB")
    else:
        print(f"  memory delta:    (not tracked — psutil/resource unavailable)")

    _assert(cycle_count[0] >= int(duration_min * 12 * 0.90),
            f"cycle count ~= {int(duration_min * 12)} (>=90% of target)",
            f"actual={cycle_count[0]}")
    _assert(len(mt5_errors) == 0, "zero MT5 errors during soak",
            f"errors={mt5_errors[:5]}")
    _assert(p95 < 2.0, f"p95 cycle duration under 2 s (Snow budget 5 s)",
            f"p95={p95*1000:.1f}ms")
    _assert(maxd < 5.0, f"max cycle duration under 5 s",
            f"max={maxd*1000:.1f}ms")

    if mt5_errors:
        print("  first 5 errors:")
        for err in mt5_errors[:5]:
            print(f"    {err}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="FLO-348 real-demo safe tests")
    parser.add_argument("--test", type=int, choices=[1, 3, 4], required=True,
                        help="Which test to run")
    parser.add_argument("--duration-min", type=float, default=60.0,
                        help="Duration in minutes for test 4 (default: 60)")
    args = parser.parse_args()

    print(f"FLO-348 real-demo tests (safe alongside live bot)")
    print(f"Test: {args.test}")
    print("=" * 60)

    try:
        if args.test == 1:
            test1_read_concurrency()
        elif args.test == 3:
            test3_exception_release()
        elif args.test == 4:
            test4_snow_load_simulator(args.duration_min)
    except Exception as e:
        _FAILURES.append((f"test{args.test}", f"exception: {e}"))
        traceback.print_exc()

    print("\n" + "=" * 60)
    if _FAILURES:
        print(f"FAILURES: {len(_FAILURES)}")
        for name, msg in _FAILURES:
            print(f"  - {name}: {msg}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
