"""Thread-safe MetaTrader5 proxy (FLO-348).

This module wraps every callable attribute of the MetaTrader5 Python API
in a shared RLock, so concurrent threads (Floki / main loop, Simba,
monitor, Snow — added by FLO-347) cannot race inside the MT5 binding.

USAGE

    # Replace:  import MetaTrader5 as mt5
    # With:     from mt5_safe import mt5
    #
    # All subsequent mt5.xxx(...) calls are transparently locked.
    # Constants (e.g. mt5.TIMEFRAME_M1) pass through unwrapped.

    # If a caller needs atomicity across multiple mt5 calls (e.g. "read
    # tick AND positions in one critical section"), import the lock
    # directly and wrap the sequence:

    from mt5_safe import mt5, mt5_lock

    with mt5_lock:
        tick = mt5.symbol_info_tick("XAUUSD")
        positions = mt5.positions_get(symbol="XAUUSD")

WHY NOT JUST A BARE `threading.Lock()` PER CALL SITE

    With ~40 `import MetaTrader5` statements and 480+ mt5.* call sites
    in the repo, per-call-site locking is error-prone: one missed site
    and the invariant silently breaks. A proxy that auto-wraps every
    attribute resolution is self-maintaining — adding a new mt5.xxx(...)
    call anywhere in the repo is automatically locked.

PERFORMANCE

    On Python 3.12 / CPython:
      - Uncontended RLock acquire: ~40-60 ns
      - `__getattr__` → instance dict cache: hit once per attribute per
        interpreter session (first call), subsequent accesses hit the
        cached wrapper directly (no re-entry into __getattr__).
      - Net cost per mt5.xxx() call: well under 1 microsecond.

    With Snow polling every 5 s and ~50 mt5 calls per cycle, total
    lock overhead is < 50 μs per cycle — negligible against the
    multi-hundred-millisecond MT5 API latencies themselves.

RELATIONSHIP TO FLO-347 (Snow)

    Snow (daemon thread, 5s cadence) shares the same MT5 binding as
    executor.py, monitor.py, and all other callers. FLO-348 hardens
    the binding for that concurrent access BEFORE FLO-347 Phase 2
    implementation begins.

CAVEATS

    1. Nested calls are safe (RLock is re-entrant): if wrapped fn A
       internally calls wrapped fn B from the same thread, no deadlock.
    2. If MetaTrader5 adds a new *class* (as opposed to a function) in
       a future version, calls to that class's methods would bypass
       this proxy. Not currently an issue — the MT5 Python binding
       is all module-level functions.
    3. `mt5.initialize()` and `mt5.shutdown()` are wrapped along with
       everything else. Intended — concurrent init/shutdown would be
       pathological anyway.
    4. `dir(mt5)` lists only previously-accessed names (those cached
       on the proxy's instance dict). For the full MT5 API surface,
       use `dir(mt5_safe._mt5_raw)` or consult the MetaTrader5 docs.
       No correctness impact; debugging convenience only.
"""

from __future__ import annotations

import threading
import MetaTrader5 as _mt5_raw

# Shared lock for all MT5 access.
# Re-entrant: a single thread can call nested wrapped functions without
# deadlocking itself. Cross-thread contention is the only blocking path.
mt5_lock: threading.RLock = threading.RLock()


class _MT5SafeProxy:
    """Lazy, cached proxy that wraps every callable attribute in `mt5_lock`.

    First access to `mt5.xxx` triggers __getattr__, which resolves the
    underlying attribute, wraps it if callable, caches the wrapper on
    the instance dict, and returns it. Subsequent accesses to `mt5.xxx`
    hit the instance dict directly (Python's attribute-lookup order
    checks instance __dict__ before __getattr__).

    The wrapped function preserves the original __name__ and __doc__ so
    tracebacks and IDE hints stay informative.
    """

    def __getattr__(self, name: str):
        raw = getattr(_mt5_raw, name)
        if callable(raw):
            def _wrapped(*args, **kwargs):
                with mt5_lock:
                    return raw(*args, **kwargs)
            _wrapped.__name__ = name
            _wrapped.__doc__ = getattr(raw, "__doc__", None)
            _wrapped.__wrapped__ = raw  # inspect-friendly
            # Cache on instance so subsequent accesses skip __getattr__
            object.__setattr__(self, name, _wrapped)
            return _wrapped
        # Non-callable (constants, classes, etc.): cache and pass through
        object.__setattr__(self, name, raw)
        return raw

    def __repr__(self) -> str:
        return "<MT5SafeProxy — FLO-348 thread-safe wrapper over MetaTrader5>"


# Public singleton. All production code imports this, not the raw module.
mt5: _MT5SafeProxy = _MT5SafeProxy()


__all__ = ["mt5", "mt5_lock"]
