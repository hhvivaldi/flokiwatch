"""FLO-452 wiring fix — cache + bot_state fallback so the D1_TREND_GATE stops
degrading on real plans. Standalone. Run: python test_flo452_wiring.py
"""
import json
import os
from types import SimpleNamespace

import regime_detector as rd
import mt5_safe
from snow.validator import _check_d1_trend_gate


class _FakeMT5:
    TIMEFRAME_D1 = 16

    def copy_rates_from_pos(self, *a, **k):
        return None  # simulate the intermittent MT5 None


def test_cache_returns_last_good_when_mt5_none():
    """build_d1_trend_score returns the cached score (not None) when MT5 hiccups."""
    rd._LAST_D1_TREND_SCORE = {"direction": "BEARISH", "score": 60, "bearish_score": 60,
                              "bullish_score": 40, "factors": [], "bullish_factors": []}
    orig = mt5_safe.mt5
    mt5_safe.mt5 = _FakeMT5()
    try:
        r = rd.build_d1_trend_score()
        assert r is not None and r["bearish_score"] == 60, r
    finally:
        mt5_safe.mt5 = orig
    print("PASS test_cache_returns_last_good_when_mt5_none (returns cache, not None)")


def test_no_cache_still_none():
    """With no cache seeded and MT5 None, returns None (first-ever cycle)."""
    rd._LAST_D1_TREND_SCORE = None
    orig = mt5_safe.mt5
    mt5_safe.mt5 = _FakeMT5()
    try:
        assert rd.build_d1_trend_score() is None
    finally:
        mt5_safe.mt5 = orig
    print("PASS test_no_cache_still_none")


def test_botstate_fallback_source_present():
    """The agent_tools fallback reads bot_state.json:d1_trend_score — confirm it's there."""
    bs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bot_state.json")
    with open(bs, "r", encoding="utf-8") as f:
        d = json.load(f)
    s = d.get("d1_trend_score")
    assert isinstance(s, dict) and "bearish_score" in s, s
    print(f"PASS test_botstate_fallback_source_present (bearish_score={s.get('bearish_score')})")


def test_gate_evaluates_with_fallback_score():
    """Simulate _last_regime_context=None -> fallback provides a score -> the gate
    EVALUATES (real REJECT) instead of DEGRADING. And None -> fail-open."""
    score = {"bearish_score": 60, "bullish_score": 40}
    buy = SimpleNamespace(id="X", entry=SimpleNamespace(direction="BUY"),
                          analysis=SimpleNamespace(counter_trend_exceptions=None))
    errs = _check_d1_trend_gate(buy, score)        # 60>=55, BUY, 0 exc -> REJECT
    assert errs and "d1_trend_gate" in errs[0], errs
    assert _check_d1_trend_gate(buy, None) == [], "None must fail-open (degraded)"
    print("PASS test_gate_evaluates_with_fallback_score (score->REJECT, None->fail-open)")


if __name__ == "__main__":
    test_cache_returns_last_good_when_mt5_none()
    test_no_cache_still_none()
    test_botstate_fallback_source_present()
    test_gate_evaluates_with_fallback_score()
    print("\nALL FLO-452 WIRING TESTS PASSED")
