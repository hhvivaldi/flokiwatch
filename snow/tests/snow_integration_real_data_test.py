"""FLO-415 — Real-data integration tests for Snow primitives.

CONTEXT
-------
On 2026-04-30 seven P0 bugs shipped to production despite 1328 passing
unit tests. Every single one was the same failure mode: mock fixtures
hand-built dicts in the shape the test author *expected* Brain to
publish; reality differed. The unit-test suite verified the producer/
consumer contract the test thought existed, not the contract that
existed.

Specifically (see commits b247a88, 6fce88c, 1fdd194, d839e5b, d0d3162,
64be6df, 1feb3d8, 7fafed8 from that day):

  * `bb_position` renamed to `bb.position_pct` — Snow read None
  * `macd.divergence` dropped from dp.indicators.macd — Snow read None
  * stochastic block missing entirely from dp.indicators
  * ema flat scalars at wrong path (indicators.emas.ema50 vs ema_50)
  * macd_hist flat alias missing for non-M1 readers
  * SRZone.zone_type uppercase but cond.zone_type lowercase
  * MT5 numpy void rows have no .get() method

TEST STRATEGY
-------------
This file loads a REAL `dp` snapshot — the exact dict Snow's
SemanticCache reads from `_last_agent_data` — captured at the end of
each `_analysis_cycle` by `bot._write_dp_snapshot()` (main.py).

For every Snow primitive accessor that doesn't require live MT5 ticks,
this file asserts:
  1. The accessor returns NOT-None against the real snapshot.
  2. The accessor raises NO exception.
  3. Where the value has structural sub-keys (bollinger.position,
     macd_divergence.detected), those sub-keys exist.

Failure messages name the EXACT field path expected, so a regression
debug is one line away.

The synthetic-plan test (TestSyntheticPlanRunsClean) builds a Plan
covering every primitive Snow supports and runs evaluate_condition
on each. Assertion: every condition returns a `bool` — no
AttributeError, KeyError, or None.

WORKFLOW
--------
* DEV runs the bot locally for ≥1 cycle to populate
  `data/_test_snapshots/dp_snapshot_latest.json`.
* DEV runs `pytest snow/tests/snow_integration_real_data_test.py`
  before any push touching:
    - main.py (the dp rebuild block, ~line 1880-2238)
    - snow/live_data.py (consumer accessors)
    - snow/evaluators/ (downstream consumers)
    - agent_data_builder.py (intel_feed.sr_zones path)
* CI skips this file (no snapshot in fresh checkouts).

CLAUDE.md Rule 24 (proposed): any change to the producer/consumer
seam must run this test against a fresh local snapshot before push.
The 2026-04-30 7-P0 incident is the canonical evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

# -----------------------------------------------------------------------------
# Snapshot location & module-level skip
# -----------------------------------------------------------------------------

# Repo root is two levels up from this file: snow/tests/ -> snow/ -> repo
_REPO_ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT_PATH = _REPO_ROOT / "data" / "_test_snapshots" / "dp_snapshot_latest.json"

pytestmark = pytest.mark.skipif(
    not SNAPSHOT_PATH.exists(),
    reason=(
        f"No real dp snapshot at {SNAPSHOT_PATH}. "
        f"Run the bot at least once locally (it writes the snapshot at "
        f"the end of each _analysis_cycle via _write_dp_snapshot) and "
        f"re-run this test."
    ),
)


# Ensure repo root is on sys.path so `from snow.live_data import ...`
# resolves when pytest is run from inside the snow/tests dir directly.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# -----------------------------------------------------------------------------
# Fixtures — load real dp once per test session
# -----------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_dp() -> Dict[str, Any]:
    """The full _last_agent_data dict from the most recent analysis cycle."""
    with open(SNAPSHOT_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def real_live_data(real_dp):
    """LiveData wired to a SemanticCache that returns the real dp.

    No `refresh()` call: we don't have live MT5 in the test process,
    so M1 local computations will return None. The non-M1 paths
    exercised here read entirely from the snapshot — exactly what
    Snow's daemon does on each tick after refresh().
    """
    from snow.live_data import LiveData
    from snow.semantic_cache import SemanticCache
    cache = SemanticCache(lambda: real_dp, ttl_seconds=60.0)
    return LiveData("XAUUSD", cache)


# =============================================================================
# Per-primitive accessor smoke — every Snow live_data path resolves
# =============================================================================

class TestEverySnowPrimitiveResolvesAgainstRealDp:
    """Each primitive's accessor path must resolve to a non-None value
    against the real Brain snapshot. None at this layer means a field
    is missing, renamed, or at the wrong path — exactly the 7-P0 class
    that shipped on 2026-04-30.

    Per-TF accessors are parametrised over every TF Snow allows in
    cond schemas (M5/M15/H1/H4/D1). M1 is excluded because it needs
    live MT5 ticks (covered by the existing live_data_test.py
    TestIndicator parametrisations).
    """

    NON_M1_TIMEFRAMES = ["M5", "M15", "H1", "H4", "D1"]

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_rsi_resolves(self, real_live_data, tf):
        v = real_live_data.rsi(tf=tf)
        assert v is not None, (
            f"rsi(tf={tf!r}) returned None — check "
            f"dp.multi_tf_indicators[{tf!r}].rsi"
        )
        assert isinstance(v, float)
        assert 0.0 <= v <= 100.0, f"RSI {v} out of [0, 100]"

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_atr_resolves(self, real_live_data, tf):
        v = real_live_data.atr(tf=tf, period=14)
        assert v is not None, (
            f"atr(tf={tf!r}) returned None — check "
            f"dp.multi_tf_indicators[{tf!r}].atr"
        )
        assert isinstance(v, float)
        assert v >= 0.0

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    @pytest.mark.parametrize("period", [9, 21, 50, 200])
    def test_ema_resolves(self, real_live_data, tf, period):
        v = real_live_data.ema(tf=tf, period=period)
        assert v is not None, (
            f"ema(tf={tf!r}, period={period}) returned None — check "
            f"dp.multi_tf_indicators[{tf!r}].ema{period}"
        )
        assert isinstance(v, float)
        # XAUUSD price range sanity — EMAs should be in the gold price
        # band, not e.g. zero from a stale-zero default.
        assert 1000.0 < v < 10000.0, f"EMA {v} outside plausible XAU range"

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_macd_histogram_resolves(self, real_live_data, tf):
        v = real_live_data.macd_histogram(tf=tf)
        assert v is not None, (
            f"macd_histogram(tf={tf!r}) returned None — check "
            f"dp.multi_tf_indicators[{tf!r}].macd.histogram"
        )
        assert isinstance(v, float)

    # FLO-411: bollinger / stochastic / macd_divergence are no longer
    # H1-only — compute_indicators_from_candles writes them per TF.
    # Test all 5 TFs to lock in the fix.

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_bollinger_resolves(self, real_live_data, tf):
        bb = real_live_data.bollinger(tf=tf)
        # H1 reads slow-cycle dp.indicators.bollinger; non-H1 reads
        # mtf[tf].bollinger. Both paths must produce the same shape.
        assert bb is not None, (
            f"bollinger(tf={tf!r}) returned None — for H1 check "
            f"dp.indicators.bollinger; for non-H1 check "
            f"dp.multi_tf_indicators[{tf!r}].bollinger (FLO-411)"
        )
        assert "position" in bb, "bb.position missing"
        assert "squeeze" in bb, "bb.squeeze missing"
        assert isinstance(bb["squeeze"], bool)

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_stochastic_resolves(self, real_live_data, tf):
        v = real_live_data.stochastic(tf=tf)
        # H1 reads dp.indicators.stochastic.value (slow cycle);
        # non-H1 reads mtf[tf].stochastic.value (FLO-411).
        assert v is not None, (
            f"stochastic(tf={tf!r}) returned None — for H1 check "
            f"dp.indicators.stochastic.value; for non-H1 check "
            f"dp.multi_tf_indicators[{tf!r}].stochastic.value (FLO-411)"
        )
        assert isinstance(v, float)
        assert 0.0 <= v <= 100.0

    @pytest.mark.parametrize("tf", NON_M1_TIMEFRAMES)
    def test_macd_divergence_resolves(self, real_live_data, tf):
        d = real_live_data.macd_divergence(tf=tf)
        # H1 reads dp.indicators.macd.divergence (slow cycle);
        # non-H1 reads mtf[tf].macd.divergence (FLO-411 — Brain runs
        # detect_macd_divergence per TF inside
        # compute_indicators_from_candles).
        assert d is not None, (
            f"macd_divergence(tf={tf!r}) returned None — for H1 check "
            f"dp.indicators.macd.divergence; for non-H1 check "
            f"dp.multi_tf_indicators[{tf!r}].macd.divergence (FLO-411)"
        )
        assert "detected" in d, "divergence.detected missing"
        assert isinstance(d["detected"], bool)

    def test_pivot_points_resolves(self, real_live_data):
        pp = real_live_data.pivot_points()
        assert pp is not None, "pivot_points() returned None"
        # Snow's evaluate_price_at_pivot reads pp.classic.<level> and
        # pp.fibonacci.<level>. Either or both must be present.
        assert "classic" in pp or "fibonacci" in pp, (
            f"pivot_points keys: {sorted(pp.keys())} — expected at least "
            f"one of 'classic' or 'fibonacci'"
        )
        if "classic" in pp:
            classic = pp["classic"]
            for level in ("PP", "R1", "R2", "R3", "S1", "S2", "S3"):
                assert level in classic, f"classic.{level} missing"
                assert isinstance(classic[level], (int, float)), (
                    f"classic.{level} is {type(classic[level]).__name__}, "
                    f"expected number"
                )


# =============================================================================
# sr_zones consumer-shape contract
# =============================================================================

class TestSRZonesShapeContract:
    """Snow's evaluate_price_at_sr_zone reads `sr_zones` from the
    semantic cache. Shape contract: each zone has `price` (numeric) and
    `zone_type` (string). Pre-fix b247a88 + d839e5b the case-mismatch
    and FLIP-exclusion bugs both stemmed from misreading this layer."""

    def test_sr_zones_present_in_dp(self, real_dp):
        """Snow reads `sr_zones` directly from the top-level dp dict
        (per snow/snow_loop.py:_semantic = SemanticCache(lambda: bot._last_agent_data))
        — NOT from the dashboard's last_analysis.intel_feed.sr_zones path.
        If the snapshot has zones at intel_feed but not at dp.sr_zones,
        Snow will read None and price_at_sr_zone always returns False."""
        zones = real_dp.get("sr_zones")
        if zones is None:
            # Some cycles publish SR zones via the dashboard path only.
            # Surface this via a clear failure rather than letting it
            # slip — Snow needs them at dp.sr_zones.
            intel_zones = (
                real_dp.get("last_analysis", {})
                .get("intel_feed", {})
                .get("sr_zones")
            )
            if isinstance(intel_zones, list) and intel_zones:
                pytest.fail(
                    "sr_zones missing at dp.sr_zones (Snow reads here) "
                    "but present at last_analysis.intel_feed.sr_zones "
                    "(dashboard reads here). The dp enrichment block in "
                    "main.py needs to copy them to dp.sr_zones for Snow "
                    "to see them."
                )
            pytest.skip(
                "No sr_zones in this snapshot — Brain may not have "
                "published them this cycle (zone detection is opportunistic)."
            )
        assert isinstance(zones, list), (
            f"sr_zones is {type(zones).__name__}, expected list"
        )
        if not zones:
            pytest.skip("sr_zones list is empty this cycle")

    def test_each_zone_has_price_and_zone_type(self, real_dp):
        zones = real_dp.get("sr_zones")
        if not isinstance(zones, list) or not zones:
            pytest.skip("No sr_zones in snapshot")
        for i, z in enumerate(zones):
            assert isinstance(z, dict), f"zone[{i}] is {type(z).__name__}"
            assert isinstance(z.get("price"), (int, float)), (
                f"zone[{i}].price is {type(z.get('price')).__name__}, "
                f"expected number — Snow's evaluate_price_at_sr_zone "
                f"reads z.get('price') and skips zones where this is "
                f"missing/non-numeric. Zone shape: {z}"
            )
            assert isinstance(z.get("zone_type"), str), (
                f"zone[{i}].zone_type is "
                f"{type(z.get('zone_type')).__name__}, expected str. "
                f"Zone shape: {z}"
            )

    def test_zone_types_are_recognized_values(self, real_dp):
        """Brain produces 'SUPPORT' / 'RESISTANCE' / 'FLIP' (uppercase).
        Snow's case-fix in b247a88 makes the comparison case-insensitive
        on the consumer side. This test verifies the producer is still
        emitting one of the three known values — not a drift like
        'breakdown' or 'pivot' that would silently miss every filter."""
        zones = real_dp.get("sr_zones")
        if not isinstance(zones, list) or not zones:
            pytest.skip("No sr_zones in snapshot")
        recognised = {"SUPPORT", "RESISTANCE", "FLIP"}
        for z in zones:
            zt = (z.get("zone_type") or "").upper()
            assert zt in recognised, (
                f"zone_type {z.get('zone_type')!r} not in "
                f"{recognised} — Snow's filter would never match. "
                f"Producer drift in support_resistance.SRZone."
            )


# =============================================================================
# Per-TF independence — non-M1 reads must NOT all return the same value
# =============================================================================

class TestPerTFIndependence:
    """Pre-FLO-410, every non-M1 read returned the SAME H1 value because
    `_semantic_indicator(name)` was TF-agnostic. After the fix, each TF
    must return its OWN data. Test: at least two TFs return DIFFERENT
    RSI / EMA9 / ATR values. (Same value across all TFs would be
    suspicious — gold's RSI on M5 vs D1 should rarely match exactly.)
    """

    def test_rsi_per_tf_values_not_uniform(self, real_live_data):
        values = {
            tf: real_live_data.rsi(tf=tf)
            for tf in ("M5", "M15", "H1", "H4", "D1")
        }
        non_none = {k: v for k, v in values.items() if v is not None}
        assert len(non_none) >= 2, (
            f"<2 TFs returned non-None RSI: {values}"
        )
        unique_values = set(non_none.values())
        assert len(unique_values) >= 2, (
            f"All TFs returned the same RSI {next(iter(unique_values))!r}: "
            f"{non_none}. This is the FLO-410 regression signature — "
            f"_semantic_indicator() is being called instead of "
            f"_multi_tf_indicator()."
        )

    def test_ema9_per_tf_values_not_uniform(self, real_live_data):
        values = {
            tf: real_live_data.ema(tf=tf, period=9)
            for tf in ("M5", "M15", "H1", "H4", "D1")
        }
        non_none = {k: v for k, v in values.items() if v is not None}
        assert len(non_none) >= 2
        assert len(set(non_none.values())) >= 2, (
            f"All TFs returned the same EMA9: {non_none}"
        )


# =============================================================================
# Synthetic-plan end-to-end exercise
# =============================================================================

class TestSyntheticPlanRunsClean:
    """Build a plan whose conditions cover every Snow primitive at
    least once. Run evaluate_condition on each. Assert each returns
    a bool — no AttributeError, KeyError, or None-returns from a
    field path that's missing or renamed.

    This is the broadest catch-net: even if a per-accessor smoke test
    above missed a specific primitive, this end-to-end test exercises
    the actual evaluator path that production runs."""

    def _build_eval_ctx(self, real_dp, plan):
        """Construct an EvalContext mirroring what snow_loop._tick
        builds. We can't refresh() LiveData against MT5 in tests, so
        primitives that require ctx.live_data.price() (price_above /
        price_below / price_at_sr_zone / price_at_fibonacci /
        price_at_pivot / ema_relation price_above|below) will return
        False — that's a clean fail-safe outcome, NOT a None or
        exception. The contract this test pins is "no crash"."""
        from snow.evaluators.dispatch import EvalContext
        from snow.live_data import LiveData
        from snow.semantic_cache import SemanticCache

        cache = SemanticCache(lambda: real_dp, ttl_seconds=60.0)
        live = LiveData("XAUUSD", cache)

        # Minimal tracker shim — position-state primitives need it.
        # Without a real position, these all return None → False, which
        # is correct fail-safe. We don't crash though.
        class _NoOpTracker:
            def profit_pips(self, plan_id, current_price): return None
            def mfe_pips(self, plan_id): return None
            def mae_pips(self, plan_id): return None
            def retrace_from_peak(self, plan_id, current_price): return None

        return EvalContext(
            live_data=live,
            semantic_cache=cache,
            tracker=_NoOpTracker(),
            plan=plan,
            ticket=None,  # no live position
            # `now` and `state_cache` left at None defaults: time-window
            # primitives read wall clock; stateful primitives detect the
            # missing state cache and short-circuit to False with a WARN.
        )

    def test_every_primitive_evaluates_without_crashing(self, real_dp):
        """The full set of 21 Snow Condition primitives, each exercised
        through evaluate_condition. Pin: every result is a bool.
        Crashes from missing field paths fail this test.

        Primitives covered (per snow/schema.py):
          price_above, price_below, rsi, macd_histogram, ema_relation,
          atr, price_at_sr_zone, price_at_fibonacci, profit_pips,
          mfe_reached, mae_reached, profit_retraced_from_peak,
          duration_exceeds, time_between, bollinger_position,
          stochastic, price_at_pivot, indicator_divergence,
          indicator_crossover, indicator_was, price_crossed_level
        """
        from snow.schema import (
            ATR, BollingerPosition, DurationExceeds, EMARelation,
            IndicatorCrossover, IndicatorDivergence, IndicatorWas,
            MACDHistogram, MAEReached, MFEReached, PriceAbove,
            PriceAtFibonacci, PriceAtPivot, PriceAtSRZone, PriceBelow,
            PriceCrossedLevel, ProfitPips, ProfitRetracedFromPeak, RSI,
            Stochastic, TimeBetween,
        )
        from snow.evaluators.dispatch import evaluate_condition

        # Stateless primitives — evaluator only takes (cond, ctx).
        stateless_conditions = [
            PriceAbove(level=4500.0),
            PriceBelow(level=4700.0),
            RSI(tf="H1", op="above", threshold=50.0),
            MACDHistogram(tf="H1", op="above", threshold=0.0),
            EMARelation(tf="H1", relation="aligned_bull"),
            ATR(tf="H1", op="above", multiplier=1.0, baseline_pips=50.0),
            PriceAtSRZone(zone_type="any", tolerance_pips=10.0),
            PriceAtFibonacci(level=0.5, tolerance_pips=10.0),
            ProfitPips(op="above", threshold=10.0),
            MFEReached(pips=20.0),
            MAEReached(pips=20.0),
            ProfitRetracedFromPeak(pips=10.0),
            DurationExceeds(minutes=60),
            TimeBetween(start_utc="08:00", end_utc="20:00"),
            BollingerPosition(tf="H1", relation="above_upper"),
            Stochastic(tf="H1", op="above", threshold=70.0),
            PriceAtPivot(level="PP", tolerance_pips=10.0),
            IndicatorDivergence(indicator="macd", direction="bullish"),
        ]

        # Build a minimal Plan stub for ctx.plan.id.
        class _PlanStub:
            id = "PLAN-INTEGRATION-TEST"
            entry = type("_E", (), {"direction": "BUY"})()

        plan = _PlanStub()
        ctx = self._build_eval_ctx(real_dp, plan)

        for cond in stateless_conditions:
            try:
                result = evaluate_condition(cond, ctx)
            except Exception as e:
                pytest.fail(
                    f"evaluate_condition({type(cond).__name__}) raised "
                    f"{type(e).__name__}: {e}. The evaluator should "
                    f"NEVER crash on real data — fail-safe is False, "
                    f"not exception."
                )
            assert isinstance(result, bool), (
                f"evaluate_condition({type(cond).__name__}) returned "
                f"{result!r} ({type(result).__name__}), expected bool"
            )

        # Stateful primitives (indicator_crossover, indicator_was,
        # price_crossed_level) need a state row. They're invoked through
        # the dispatch layer, which routes them to their stateful
        # evaluator with state from ctx. Without a state row in tests,
        # the dispatch layer should still produce a bool (cold-start
        # path) — pin that here.
        stateful_conditions = [
            IndicatorCrossover(
                indicator="macd_histogram", tf="H1",
                direction="above", threshold=0.0,
            ),
            IndicatorWas(
                indicator="rsi", tf="H1",
                op="below", threshold=30.0, within_bars=4,
            ),
            PriceCrossedLevel(level=4600.0, direction="above"),
        ]
        for cond in stateful_conditions:
            try:
                result = evaluate_condition(cond, ctx)
            except Exception as e:
                pytest.fail(
                    f"stateful evaluate_condition({type(cond).__name__}) "
                    f"raised {type(e).__name__}: {e}"
                )
            assert isinstance(result, bool), (
                f"stateful evaluate_condition({type(cond).__name__}) "
                f"returned {result!r}, expected bool"
            )


# =============================================================================
# Snapshot freshness — sanity that we're testing recent data
# =============================================================================

class TestSnapshotFreshness:
    """The snapshot is overwritten every cycle. If it's >24h old, the
    DEV may be running tests against stale data — fail loudly so they
    re-run the bot first."""

    MAX_SNAPSHOT_AGE_HOURS = 24.0

    def test_snapshot_is_recent(self):
        import time
        age_seconds = time.time() - SNAPSHOT_PATH.stat().st_mtime
        age_hours = age_seconds / 3600.0
        assert age_hours <= self.MAX_SNAPSHOT_AGE_HOURS, (
            f"Snapshot is {age_hours:.1f} hours old (>"
            f"{self.MAX_SNAPSHOT_AGE_HOURS}h threshold). Run the bot "
            f"to refresh it before relying on these tests."
        )
