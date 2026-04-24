"""Snow semantic cache — 60 s TTL wrapper over Floki's `_last_agent_data`.

Two INDEPENDENT concerns, both required:

  1. **TTL snapshot pinning (tick coherency).**
     Repeated reads within `ttl_seconds` return the same cached snapshot.
     This protects the Snow loop from torn reads when Floki is mid-way
     through rebuilding `_last_agent_data` (RFC §14.3 item 3). Without
     this, two contingencies in the same tick could see a different
     `indicators.rsi` value — one from before Floki's rebuild, one from
     after — and fire inconsistently.

  2. **Semantic staleness surface (age-of-data signal).**
     `semantic_stale_seconds()` returns how old the Floki-computed data
     itself is, independent of the TTL. The TTL keeps the snapshot
     stable for 60 s; `_last_agent_data.timestamp` tells you the
     Floki-cycle that produced it, which may be minutes old. RFC §14.3
     item 2 flags stale semantic data in evaluation logs.

Thread-safety:
  All access routed through a single `threading.RLock`. Reentry is fine
  (same thread can call `.get()` inside a `.semantic_stale_seconds()`
  computation). Cross-thread contention blocks on the lock; typical
  hold time is a dict lookup (<< 1 ms).

Invalidation rules:
  * Automatic: on next access after `ttl_seconds` has elapsed.
  * Manual: `invalidate()` clears the snapshot — next access refetches.
  * Never on write: this cache is read-only. Floki writes to the
    underlying `_last_agent_data`; Snow only reads.

Provider pattern:
  `SemanticCache(data_provider)` takes a zero-arg callable returning
  the current `_last_agent_data` dict (or None). Production wires it
  to `lambda: bot._last_agent_data`; tests inject trivial fakes. This
  keeps Snow zero-coupled to the `XAUUSDBot` class.
"""
from __future__ import annotations

import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable, Optional


class SemanticCache:

    def __init__(
        self,
        data_provider: Callable[[], Optional[dict]],
        ttl_seconds: float = 60.0,
    ):
        self._provider = data_provider
        self._ttl = float(ttl_seconds)
        self._lock = threading.RLock()
        self._snapshot: Optional[dict] = None
        self._snapshot_at: Optional[float] = None  # time.monotonic()

    # -- Internals -----------------------------------------------------------

    def _refresh_if_stale(self) -> None:
        """Fetch a fresh snapshot from the provider if TTL expired.
        Must be called under `self._lock`.
        """
        now = time.monotonic()
        if self._snapshot_at is None or (now - self._snapshot_at) > self._ttl:
            try:
                fresh = self._provider()
            except Exception:
                # Provider failure is non-fatal — keep whatever we had.
                # Snow's fail-safe rule (RFC §6.5) treats missing data
                # as False, which is the safe direction.
                return
            if fresh is not None:
                # Deep copy: Floki may mutate _last_agent_data at any
                # moment. Our snapshot must be immune to that.
                self._snapshot = deepcopy(fresh)
                self._snapshot_at = now

    # -- Public API ----------------------------------------------------------

    def get(self, *path: str) -> Any:
        """Return value at nested `path`, or None if any segment missing.

        Example:
            cache.get("indicators", "rsi", "value")  # → 62.4 or None

        The snapshot is refreshed on entry if TTL has elapsed, then the
        path is walked against the pinned snapshot — repeated calls with
        the same path return the same value for the full TTL window.
        """
        with self._lock:
            self._refresh_if_stale()
            if self._snapshot is None:
                return None
            node: Any = self._snapshot
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    return None
                node = node[key]
            return node

    def semantic_stale_seconds(self) -> Optional[float]:
        """Return seconds elapsed since Floki computed the current
        snapshot, based on the `timestamp` field embedded in
        `_last_agent_data` (written as UTC ISO-8601 with "Z" suffix per
        Rule 22 / `tz_utils.utc_iso()`).

        Independent of the TTL — reflects the Floki-cycle cadence, not
        Snow's own cache refresh.

        Returns None if:
          * no snapshot yet (provider never returned data),
          * the snapshot has no `timestamp` field,
          * the timestamp is unparseable.
        """
        with self._lock:
            self._refresh_if_stale()
            if self._snapshot is None:
                return None
            ts = self._snapshot.get("timestamp")
            if not isinstance(ts, str) or not ts:
                return None
            try:
                # Handle both "...Z" and "...+00:00" forms
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            now_utc = datetime.now(timezone.utc)
            return (now_utc - dt).total_seconds()

    def invalidate(self) -> None:
        """Force the next read to re-fetch from the provider. Used in
        tests and during controlled restarts; not expected in prod."""
        with self._lock:
            self._snapshot = None
            self._snapshot_at = None

    def has_snapshot(self) -> bool:
        """Diagnostic — True if the cache currently holds any data."""
        with self._lock:
            return self._snapshot is not None
