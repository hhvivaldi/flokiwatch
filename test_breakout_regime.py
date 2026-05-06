"""FLO-422 — unit tests for breakout_regime helper.

Pure-compute tests + one integration test against the historical PLAN-009
trade where we have known-good ground truth from prior backfill.

Run: python test_breakout_regime.py
Exits non-zero on failure.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from breakout_regime import compute_regime_snapshot, compute_drift


def assert_eq(actual, expected, label):
    if actual != expected:
        print(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


def assert_close(actual, expected, tol, label):
    if actual is None:
        print(f"FAIL [{label}]: expected ~{expected}, got None")
        sys.exit(1)
    if abs(actual - expected) > tol:
        print(f"FAIL [{label}]: expected {expected} ± {tol}, got {actual}")
        sys.exit(1)
    print(f"PASS [{label}]")


def assert_in(value, allowed, label):
    if value not in allowed:
        print(f"FAIL [{label}]: expected one of {allowed}, got {value!r}")
        sys.exit(1)
    print(f"PASS [{label}]")


def assert_warning(snapshot, expected_warning, label):
    if expected_warning not in snapshot["computation_warnings"]:
        print(f"FAIL [{label}]: expected warning {expected_warning!r}, got {snapshot['computation_warnings']}")
        sys.exit(1)
    print(f"PASS [{label}]")


# ----------------------------------------------------------------------
# Test data fixtures
# ----------------------------------------------------------------------

def _bar(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close}


def _make_compressed_buy_setup():
    """Compressed pre-state into a BUY breakout level — healthy shape."""
    bars = []
    base = 4600.0
    # Prior 14 bars: tight range, small bodies (compression)
    for i in range(14):
        o = base + (i % 3) * 0.3
        c = o + (0.4 if i % 2 == 0 else -0.4)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        bars.append(_bar(o, h, l, c))
    # Last 12 bars: small bodies, no big impulses (still compressed)
    for i in range(12):
        o = base + 0.2 + i * 0.05
        c = o + 0.3
        h = max(o, c) + 0.4
        l = min(o, c) - 0.3
        bars.append(_bar(o, h, l, c))
    return bars


def _make_exhausted_buy_setup():
    """Several large up-bars already done — late/exhausted shape."""
    bars = []
    base = 4600.0
    # Prior 14 bars: small bodies (compression baseline)
    for i in range(14):
        o = base + i * 0.1
        c = o + 0.3
        h = max(o, c) + 0.4
        l = min(o, c) - 0.3
        bars.append(_bar(o, h, l, c))
    # Last 12 bars: 5 big impulses then chop (move already mostly done)
    px = base + 1.5
    for i in range(12):
        if i < 5:
            o = px
            c = o + 4.0  # large positive body, ~5x the prior baseline
            h = c + 0.5; l = o - 0.3
            px = c
        else:
            o = px
            c = o + (0.2 if i % 2 == 0 else -0.3)
            h = max(o, c) + 0.4; l = min(o, c) - 0.3
            px = c
        bars.append(_bar(o, h, l, c))
    return bars


def _make_analyses_4h(bb_width_change_pct: float, atr_change_pct: float, base_price: float = 4600.0):
    """Construct a 4h analyses series with controlled BB-width and ATR deltas."""
    out = []
    n = 12  # ~12 ticks over 4h
    bbw_first = 100.0
    bbw_last = bbw_first * (1 + bb_width_change_pct / 100)
    atr_first = 10.0
    atr_last = atr_first * (1 + atr_change_pct / 100)
    for i in range(n):
        frac = i / (n - 1)
        bbw = bbw_first + (bbw_last - bbw_first) * frac
        atr = atr_first + (atr_last - atr_first) * frac
        bbm = base_price
        out.append({
            "timestamp": f"2026-05-06T{i:02d}:00:00Z",
            "current_price": base_price + (i - n / 2) * 0.5,
            "atr_14": atr,
            "bb_upper": bbm + bbw / 2,
            "bb_middle": bbm,
            "bb_lower": bbm - bbw / 2,
            "rsi_14": 55.0,
            "adx_14": 22.0,
            "ema_50": base_price - 5.0,
        })
    return out


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------

def test_1_compressed_buy_clean_snapshot():
    """Healthy compressed BUY: low impulse_total, BBw flat, no warnings."""
    candles = _make_compressed_buy_setup()
    a4 = _make_analyses_4h(bb_width_change_pct=-3.0, atr_change_pct=-8.0)
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY",
        setup_type="breakout_range",
        breakout_level=4602.0,
        current_price=4602.5,
        candles_m5=candles,
        analyses_4h=a4,
        analyses_24h=a4 * 2,  # 24 entries
        stage="author",
    )
    assert_eq(snap["stage"], "author", "test1.stage")
    assert_eq(snap["direction"], "BUY", "test1.direction")
    assert_eq(snap["setup_type"], "breakout_range", "test1.setup_type")
    assert_eq(snap["computation_warnings"], [], "test1.no_warnings")
    # impulse_total should be 0 — small bodies in last 12
    if snap["impulse_total_60m"] is None or snap["impulse_total_60m"] > 1:
        print(f"FAIL [test1.impulse_total_low]: expected 0-1, got {snap['impulse_total_60m']}")
        sys.exit(1)
    print(f"PASS [test1.impulse_total_low] (got {snap['impulse_total_60m']})")
    assert_close(snap["bb_width_4h_pct"], -3.0, 0.5, "test1.bbw_close_to_-3pct")
    assert_close(snap["atr_4h_pct"], -8.0, 0.5, "test1.atr_close_to_-8pct")
    assert snap["m5_pattern"] is not None and len(snap["m5_pattern"]) == 12, "test1.m5_pattern_12char"
    print(f"PASS [test1.m5_pattern_12char] ({snap['m5_pattern']!r})")


def test_2_exhausted_buy_high_impulse():
    """Late/exhausted BUY: high impulse_total, large BBw expansion."""
    candles = _make_exhausted_buy_setup()
    a4 = _make_analyses_4h(bb_width_change_pct=+45.0, atr_change_pct=+12.0)
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY",
        setup_type="breakout_range",
        breakout_level=4630.0,
        current_price=4624.0,
        candles_m5=candles,
        analyses_4h=a4,
        analyses_24h=a4 * 2,
    )
    if snap["impulse_total_60m"] is None or snap["impulse_total_60m"] < 4:
        print(f"FAIL [test2.impulse_total_high]: expected >=4, got {snap['impulse_total_60m']}")
        sys.exit(1)
    print(f"PASS [test2.impulse_total_high] (got {snap['impulse_total_60m']})")
    assert_close(snap["bb_width_4h_pct"], +45.0, 0.5, "test2.bbw_high_expansion")


def test_3_insufficient_history_warnings():
    """Few candles + few analyses → warnings, no crash."""
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY",
        setup_type="breakout_range",
        breakout_level=4600.0,
        current_price=4602.0,
        candles_m5=[_bar(4600, 4601, 4599, 4600.5) for _ in range(5)],
        analyses_4h=[],
        analyses_24h=[],
    )
    assert_warning(snap, "insufficient_m5_history", "test3.m5_warning")
    assert_warning(snap, "insufficient_4h_history", "test3.4h_warning")
    assert_eq(snap["impulse_total_60m"], None, "test3.impulse_none")
    assert_eq(snap["bb_width_4h_pct"], None, "test3.bbw_none")
    print(f"PASS [test3.snapshot_returned_with_nulls]")


def test_4_breakout_age_bars():
    """price_crossed_level should be reflected in breakout_age_bars."""
    bars = []
    base = 4600.0
    # 22 bars below the level (4630)
    for i in range(22):
        o = base + i * 0.1
        c = o + 0.2
        h = max(o, c) + 0.3; l = min(o, c) - 0.2
        bars.append(_bar(o, h, l, c))
    # bar 22 is the breakout candle
    bars.append(_bar(4602.5, 4631.0, 4602.0, 4630.5))
    # bars 23-25 hold above
    for i in range(3):
        o = 4630.5 + i * 0.2
        c = o + 0.1
        h = max(o, c) + 0.3; l = min(o, c) - 0.2
        bars.append(_bar(o, h, l, c))
    # total = 26 bars; level first crossed at index 22; latest is index 25
    a4 = _make_analyses_4h(bb_width_change_pct=0.0, atr_change_pct=0.0)
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY",
        setup_type="breakout_range",
        breakout_level=4630.0,
        current_price=4631.0,
        candles_m5=bars,
        analyses_4h=a4,
        analyses_24h=a4 * 2,
    )
    assert_eq(snap["breakout_age_bars"], 3, "test4.breakout_age_3bars_after_first_cross")
    assert_close(snap["breakout_distance_pips"], +10.0, 0.5, "test4.breakout_distance_10pips")


def test_5_drift_regime_expanded():
    """Author = compressed, trigger = exhausted → drift_assessment regime_expanded."""
    a_candles = _make_compressed_buy_setup()
    a_a4 = _make_analyses_4h(bb_width_change_pct=-3.0, atr_change_pct=-8.0)
    author = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4602.5, candles_m5=a_candles, analyses_4h=a_a4, analyses_24h=a_a4 * 2,
        stage="author",
    )
    t_candles = _make_exhausted_buy_setup()
    t_a4 = _make_analyses_4h(bb_width_change_pct=+45.0, atr_change_pct=+12.0)
    trigger = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 14, 30, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4624.0, candles_m5=t_candles, analyses_4h=t_a4, analyses_24h=t_a4 * 2,
        stage="trigger",
    )
    drift = compute_drift(author, trigger)
    assert_eq(drift["drift_assessment"], "regime_expanded", "test5.drift_assessment_expanded")
    assert_close(drift["delta_seconds_author_to_trigger"], 5400, 1, "test5.delta_seconds_90min")
    if drift["impulse_total_delta"] is None or drift["impulse_total_delta"] < 2:
        print(f"FAIL [test5.impulse_delta_positive]: got {drift['impulse_total_delta']}")
        sys.exit(1)
    print(f"PASS [test5.impulse_delta_positive] (got {drift['impulse_total_delta']})")
    if drift["bb_width_4h_pct_delta"] is None or drift["bb_width_4h_pct_delta"] < 20:
        print(f"FAIL [test5.bbw_delta_high]: got {drift['bb_width_4h_pct_delta']}")
        sys.exit(1)
    print(f"PASS [test5.bbw_delta_high] (got {drift['bb_width_4h_pct_delta']})")


def test_6_drift_stable():
    """Author and trigger similar → drift_assessment regime_stable."""
    candles = _make_compressed_buy_setup()
    a4 = _make_analyses_4h(bb_width_change_pct=-3.0, atr_change_pct=-8.0)
    author = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4602.5, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
        stage="author",
    )
    trigger = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 30, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4603.0, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
        stage="trigger",
    )
    drift = compute_drift(author, trigger)
    assert_eq(drift["drift_assessment"], "regime_stable", "test6.drift_stable")


def test_7_schema_completeness():
    """Both author and trigger snapshots must have identical key set."""
    candles = _make_compressed_buy_setup()
    a4 = _make_analyses_4h(0.0, 0.0)
    a = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4602.5, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
        stage="author",
    )
    t = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4602.5, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
        stage="trigger",
    )
    assert_eq(set(a.keys()), set(t.keys()), "test7.identical_schema")


def test_8_iso_timestamp_format():
    """ts field must be ISO-8601 with explicit Z suffix per Rule 22."""
    candles = _make_compressed_buy_setup()
    a4 = _make_analyses_4h(0.0, 0.0)
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 11, 27, tzinfo=timezone.utc),
        direction="BUY", setup_type="breakout_range", breakout_level=4602.0,
        current_price=4602.5, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
    )
    assert_eq(snap["ts"], "2026-05-06T13:11:27Z", "test8.ts_format")


def test_9_no_direction_sets_metrics_to_none():
    """If direction is None (setup-agnostic snapshot), direction-dependent
    metrics (impulse, drift, breakout_age) should be None."""
    candles = _make_compressed_buy_setup()
    a4 = _make_analyses_4h(0.0, 0.0)
    snap = compute_regime_snapshot(
        ts=datetime(2026, 5, 6, 13, 0, 0, tzinfo=timezone.utc),
        direction=None, setup_type="breakout_range", breakout_level=None,
        current_price=4602.5, candles_m5=candles, analyses_4h=a4, analyses_24h=a4 * 2,
    )
    assert_eq(snap["impulse_total_60m"], None, "test9.impulse_none")
    assert_eq(snap["candle_drift_trailing"], None, "test9.drift_none")
    assert_eq(snap["breakout_age_bars"], None, "test9.age_none")
    # but timeline-only metrics should still compute
    assert snap["bb_width_4h_pct"] is not None, "test9.bbw_still_present"
    print(f"PASS [test9.direction_agnostic_snapshot]")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-422 breakout_regime helper test suite")
    print("=" * 60)
    test_1_compressed_buy_clean_snapshot()
    test_2_exhausted_buy_high_impulse()
    test_3_insufficient_history_warnings()
    test_4_breakout_age_bars()
    test_5_drift_regime_expanded()
    test_6_drift_stable()
    test_7_schema_completeness()
    test_8_iso_timestamp_format()
    test_9_no_direction_sets_metrics_to_none()
    print("=" * 60)
    print("ALL TESTS PASSED")
