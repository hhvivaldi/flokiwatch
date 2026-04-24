"""SemanticCache tests — Phase 3a infrastructure.

Covers:
  * TTL snapshot pinning (same key → same value within the TTL window)
  * Semantic staleness (independent of TTL; based on timestamp field)
  * Deep-copy isolation (provider mutation after read cannot tear cache)
  * Thread-safety under contention
  * Provider failure modes (raises, returns None)
  * Missing / malformed timestamp fields
  * Nested-path lookups with missing segments
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from snow.semantic_cache import SemanticCache


def _ts(offset_seconds: int = 0) -> str:
    """Return an ISO-8601 UTC "Z"-suffixed timestamp shifted from now."""
    now = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    # Milliseconds + Z to match tz_utils.utc_iso() output shape
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# =============================================================================
# Basic get / nested path lookup
# =============================================================================

class TestGet:

    def test_get_top_level(self):
        cache = SemanticCache(lambda: {"regime": "TRENDING_BEARISH"})
        assert cache.get("regime") == "TRENDING_BEARISH"

    def test_get_nested(self):
        cache = SemanticCache(lambda: {"indicators": {"rsi": {"value": 62.4}}})
        assert cache.get("indicators", "rsi", "value") == 62.4

    def test_get_missing_top_level_returns_none(self):
        cache = SemanticCache(lambda: {"indicators": {}})
        assert cache.get("nonexistent") is None

    def test_get_missing_nested_returns_none(self):
        cache = SemanticCache(lambda: {"indicators": {"rsi": {"value": 62.4}}})
        assert cache.get("indicators", "macd", "histogram") is None

    def test_get_on_empty_dict(self):
        cache = SemanticCache(lambda: {})
        assert cache.get("anything") is None

    def test_get_no_snapshot_returns_none(self):
        cache = SemanticCache(lambda: None)
        assert cache.get("regime") is None

    def test_path_walks_into_non_dict_returns_none(self):
        cache = SemanticCache(lambda: {"indicators": "flat_string"})
        assert cache.get("indicators", "rsi") is None


# =============================================================================
# TTL snapshot pinning
# =============================================================================

class TestTTLPinning:

    def test_same_key_returns_same_value_within_ttl(self):
        """Repeated reads must return the pinned snapshot — not re-pull."""
        counter = {"n": 0}

        def provider():
            counter["n"] += 1
            return {"regime": f"call_{counter['n']}"}

        cache = SemanticCache(provider, ttl_seconds=60.0)
        v1 = cache.get("regime")
        v2 = cache.get("regime")
        v3 = cache.get("regime")
        assert v1 == v2 == v3 == "call_1"
        assert counter["n"] == 1  # provider called exactly once

    def test_ttl_expiry_triggers_refetch(self):
        """After TTL elapses, next read pulls a fresh snapshot."""
        counter = {"n": 0}

        def provider():
            counter["n"] += 1
            return {"regime": f"call_{counter['n']}"}

        # Tiny TTL so the test runs fast
        cache = SemanticCache(provider, ttl_seconds=0.05)
        assert cache.get("regime") == "call_1"
        time.sleep(0.10)
        assert cache.get("regime") == "call_2"
        assert counter["n"] == 2

    def test_invalidate_forces_refetch(self):
        counter = {"n": 0}

        def provider():
            counter["n"] += 1
            return {"v": counter["n"]}

        cache = SemanticCache(provider, ttl_seconds=60.0)
        assert cache.get("v") == 1
        cache.invalidate()
        assert cache.get("v") == 2

    def test_has_snapshot_tracks_state(self):
        cache = SemanticCache(lambda: {"x": 1})
        assert cache.has_snapshot() is False
        cache.get("x")
        assert cache.has_snapshot() is True
        cache.invalidate()
        assert cache.has_snapshot() is False


# =============================================================================
# Deep-copy isolation (protects against Floki mid-rebuild)
# =============================================================================

class TestDeepCopyIsolation:

    def test_provider_mutation_after_fetch_does_not_tear_snapshot(self):
        """The provider's dict is deep-copied on fetch — subsequent
        mutation by Floki cannot change what Snow sees."""
        data = {"indicators": {"rsi": {"value": 50.0}}}
        cache = SemanticCache(lambda: data, ttl_seconds=60.0)
        assert cache.get("indicators", "rsi", "value") == 50.0
        # Simulate Floki mid-rebuild mutating the dict
        data["indicators"]["rsi"]["value"] = 99.9
        # Cache still returns the pinned value
        assert cache.get("indicators", "rsi", "value") == 50.0

    def test_nested_list_is_copied_too(self):
        data = {"sr_zones": [{"price": 4700.0}, {"price": 4720.0}]}
        cache = SemanticCache(lambda: data)
        zones = cache.get("sr_zones")
        data["sr_zones"].append({"price": 9999.0})
        # Cache snapshot unaffected
        assert len(cache.get("sr_zones")) == 2


# =============================================================================
# Semantic staleness (age-of-Floki-data signal)
# =============================================================================

class TestSemanticStaleness:

    def test_fresh_timestamp_yields_small_stale_seconds(self):
        cache = SemanticCache(lambda: {"timestamp": _ts(0)})
        stale = cache.semantic_stale_seconds()
        assert stale is not None
        assert 0.0 <= stale < 2.0

    def test_old_timestamp_yields_large_stale_seconds(self):
        cache = SemanticCache(lambda: {"timestamp": _ts(-300)})  # 5 min ago
        stale = cache.semantic_stale_seconds()
        assert stale is not None
        assert stale > 290.0

    def test_missing_timestamp_returns_none(self):
        cache = SemanticCache(lambda: {"regime": "TRENDING"})
        assert cache.semantic_stale_seconds() is None

    def test_unparseable_timestamp_returns_none(self):
        cache = SemanticCache(lambda: {"timestamp": "not-a-date"})
        assert cache.semantic_stale_seconds() is None

    def test_no_snapshot_returns_none(self):
        cache = SemanticCache(lambda: None)
        assert cache.semantic_stale_seconds() is None

    def test_staleness_independent_of_ttl(self):
        """A short TTL does not reset the `semantic_stale_seconds`
        measurement — that tracks Floki-cycle age, not cache age."""
        past_ts = _ts(-120)  # 2 min old from Floki's perspective
        cache = SemanticCache(lambda: {"timestamp": past_ts}, ttl_seconds=0.05)
        stale1 = cache.semantic_stale_seconds()
        time.sleep(0.10)  # TTL expired; refetch from provider
        stale2 = cache.semantic_stale_seconds()
        # Both readings are ~120 s because the provider returns the same
        # past timestamp each time. TTL affects when we refetch, not the
        # staleness value itself.
        assert stale1 is not None and stale2 is not None
        assert abs(stale1 - stale2) < 1.0  # both ~120 s


# =============================================================================
# Provider failure modes
# =============================================================================

class TestProviderFailure:

    def test_provider_raises_keeps_last_snapshot(self):
        state = {"mode": "ok", "data": {"v": 1}}

        def provider():
            if state["mode"] == "raise":
                raise RuntimeError("provider boom")
            return state["data"]

        cache = SemanticCache(provider, ttl_seconds=0.05)
        assert cache.get("v") == 1
        # Simulate TTL expiry + provider failure
        time.sleep(0.10)
        state["mode"] = "raise"
        # Must not raise; last good snapshot remains accessible
        assert cache.get("v") == 1

    def test_provider_returns_none_before_first_success(self):
        cache = SemanticCache(lambda: None)
        assert cache.get("anything") is None
        assert cache.has_snapshot() is False


# =============================================================================
# Thread-safety under contention
# =============================================================================

class TestThreadSafety:

    def test_concurrent_readers_see_consistent_snapshot(self):
        """Many threads hammering `.get()` in parallel must each read
        the fully-assembled snapshot, never a partial/torn view."""
        data = {"indicators": {"rsi": {"value": 62.4}}}
        cache = SemanticCache(lambda: data, ttl_seconds=60.0)

        observed = []
        errors: list[BaseException] = []

        def reader():
            try:
                for _ in range(200):
                    val = cache.get("indicators", "rsi", "value")
                    observed.append(val)
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert len(observed) == 8 * 200
        # Every observation must be the pinned value — no partial tears
        assert all(v == 62.4 for v in observed)

    def test_concurrent_readers_across_ttl_refresh(self):
        """Readers that straddle a TTL boundary still see one of the
        two coherent snapshots, never a half-merged view."""
        state = {"n": 0}

        def provider():
            state["n"] += 1
            return {"v": state["n"]}

        cache = SemanticCache(provider, ttl_seconds=0.02)
        observed: list[int] = []
        errors: list[BaseException] = []

        def reader():
            try:
                for _ in range(100):
                    v = cache.get("v")
                    if v is not None:
                        observed.append(int(v))
                    time.sleep(0.0005)  # let other threads interleave
            except BaseException as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        assert observed
        # Every observed value must match some provider-returned integer —
        # no bogus half-state values.
        assert all(1 <= v <= state["n"] for v in observed)
