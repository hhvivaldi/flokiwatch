"""Standalone unit tests for macro_divergence_detector. Run: python test_macro_divergence.py"""
from macro_divergence_detector import detect_macro_divergence

NOW = 1_700_000_000.0  # arbitrary fixed "now" for deterministic age math


def _rates(n, first_close, last_close, ts_spacing_sec, latest_ts):
    """Build a list of bars with linearly interpolated closes ending at latest_ts."""
    closes = [first_close + (last_close - first_close) * i / max(1, n - 1) for i in range(n)]
    times = [latest_ts - (n - 1 - i) * ts_spacing_sec for i in range(n)]
    return [{"time": times[i], "close": closes[i]} for i in range(n)]


def _xau(n, first_close, last_close, latest_ts=NOW - 60):
    return _rates(n, first_close, last_close, 300, int(latest_ts))  # 300s = 5min


def _m15(n, first_close, last_close, latest_ts=NOW - 60):
    return _rates(n, first_close, last_close, 900, int(latest_ts))  # 900s = 15min


def check(label, actual, expected_signal):
    ok = (actual is None and expected_signal is None) or (
        actual is not None and actual.get("signal") == expected_signal
    )
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}")
    if not ok:
        print(f"    expected signal={expected_signal!r}, got={actual!r}")
    return ok


print("=== macro_divergence_detector tests ===")
passed = 0
total = 0

# 1. None inputs -> None
total += 1
if check("1. all None inputs -> None",
         detect_macro_divergence(None, None, None, NOW), None):
    passed += 1

# 2. Short XAU data -> None
total += 1
if check("2. short XAU (8 bars) -> None",
         detect_macro_divergence(_m15(5, 100, 99.8), _m15(5, 100, 100), _xau(8, 4000, 4000), NOW),
         None):
    passed += 1

# 3. S24 fires: UST dropped 0.2% in 60m, XAU flat
ust_s24 = _m15(5, 100.0, 99.80)   # -0.20% over 4 bars (60m)
dxy_neutral = _m15(3, 100.0, 100.0)
xau_flat = _xau(13, 4000.0, 4000.0)
total += 1
res = detect_macro_divergence(ust_s24, dxy_neutral, xau_flat, NOW)
if check("3. yields surge + XAU flat -> S24 BEARISH", res, "yields_surge_xau_lag"):
    passed += 1
    assert res["bias"] == "BEARISH", res
    assert res["confidence"] == 71, res
    assert 0 <= res["age_min"] <= 1, res  # latest_ts = NOW-60 -> age=1 min
    assert "UST10Y_M6" in res["detail"]

# 4. S24 does NOT fire if XAU already dropped >= 0.1%
ust_s24b = _m15(5, 100.0, 99.80)
xau_dropped = _xau(13, 4010.0, 4000.0)  # -0.25% over 60m -> below -0.1% lag threshold
total += 1
if check("4. yields surge but XAU already dropped -> None",
         detect_macro_divergence(ust_s24b, dxy_neutral, xau_dropped, NOW), None):
    passed += 1

# 5. S25 fires: DXY dropped 0.2% in 30m, XAU flat
ust_neutral = _m15(5, 100.0, 100.0)
dxy_s25 = _m15(3, 100.0, 99.80)   # -0.20% over 2 bars (30m)
xau_flat2 = _xau(13, 4000.0, 4000.0)
total += 1
res = detect_macro_divergence(ust_neutral, dxy_s25, xau_flat2, NOW)
if check("5. dxy drop + XAU flat -> S25 BULLISH", res, "dxy_drop_xau_lag"):
    passed += 1
    assert res["bias"] == "BULLISH", res
    assert res["confidence"] == 65, res
    assert "DXY_M6" in res["detail"]

# 6. Both S24 and S25 conditions met -> S24 wins
ust_s24c = _m15(5, 100.0, 99.80)
dxy_s25c = _m15(3, 100.0, 99.80)
xau_flat3 = _xau(13, 4000.0, 4000.0)
total += 1
res = detect_macro_divergence(ust_s24c, dxy_s25c, xau_flat3, NOW)
if check("6. both conditions -> S24 priority", res, "yields_surge_xau_lag"):
    passed += 1

# 7. Stale UST data (>30 min old) suppresses S24
ust_stale = _m15(5, 100.0, 99.80, latest_ts=NOW - 2000)  # 2000s = 33 min
total += 1
res = detect_macro_divergence(ust_stale, dxy_neutral, xau_flat, NOW)
if check("7. stale UST M15 (>30min) -> None", res, None):
    passed += 1

# 8. S25 fires when UST is stale but DXY is fresh
total += 1
res = detect_macro_divergence(ust_stale, dxy_s25, xau_flat, NOW)
if check("8. stale UST but fresh DXY + drop -> S25", res, "dxy_drop_xau_lag"):
    passed += 1

# 9. Sub-threshold UST move -> None
ust_weak = _m15(5, 100.0, 99.90)  # -0.10% < 0.15% threshold
total += 1
res = detect_macro_divergence(ust_weak, dxy_neutral, xau_flat, NOW)
if check("9. sub-threshold UST move -> None", res, None):
    passed += 1

# 10. age_min is correctly derived from latest bar timestamp
ust_20min_old = _m15(5, 100.0, 99.80, latest_ts=NOW - 1200)  # 20 min
total += 1
res = detect_macro_divergence(ust_20min_old, dxy_neutral, xau_flat, NOW)
if res is not None and res.get("age_min") == 20:
    print(f"  [PASS] 10. age_min=20 derived from latest UST M15 ts")
    passed += 1
else:
    print(f"  [FAIL] 10. expected age_min=20, got {res}")

# 11. XAU has risen (positive return) but still below lag threshold -> S25 fires
xau_slightly_up = _xau(13, 4000.0, 4003.0)  # +0.075%  < +0.1% lag threshold
total += 1
res = detect_macro_divergence(ust_neutral, dxy_s25, xau_slightly_up, NOW)
if check("11. dxy drop + XAU +0.075% (below lag thr) -> S25", res, "dxy_drop_xau_lag"):
    passed += 1

# 12. XAU risen above lag threshold -> S25 suppressed
xau_risen = _xau(13, 4000.0, 4010.0)  # +0.25% > +0.1%
total += 1
res = detect_macro_divergence(ust_neutral, dxy_s25, xau_risen, NOW)
if check("12. dxy drop but XAU already risen -> None", res, None):
    passed += 1

print(f"\n{passed}/{total} tests passed")
if passed != total:
    import sys
    sys.exit(1)
