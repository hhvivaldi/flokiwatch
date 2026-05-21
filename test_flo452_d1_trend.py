"""FLO-452 — D1 trend score + counter-trend gate tests.

Standalone (no pytest, no live MT5). Score tests use the pure
compute_d1_trend_score; gate tests use SimpleNamespace plan stubs. The
counterfactual runs the score against 5 recent plans and prints an
ALLOW/REJECT-vs-reality table at thresholds 70 (spec) and 55 (tuned).
Run: python test_flo452_d1_trend.py
"""
from types import SimpleNamespace
import regime_detector as rd
from snow.validator import _check_d1_trend_gate, _D1_GATE_THRESHOLD

_FULL_BEAR = {"close": 4500, "ema50": 4700, "ema200": 4800, "atr": 100, "adx": 30,
              "plus_di": 10, "minus_di": 25, "bars_below_ema50": 5, "ema50_slope": -5,
              "swing": "LH_LL"}
_FULL_BULL = {"close": 4900, "ema50": 4700, "ema200": 4600, "atr": 100, "adx": 30,
              "plus_di": 25, "minus_di": 10, "bars_above_ema50": 5, "ema50_slope": 5,
              "swing": "HH_HL"}


def test_score_all_on():
    r = rd.compute_d1_trend_score(_FULL_BEAR)
    assert r["bearish_score"] == 100 and r["direction"] == "BEARISH", r
    assert len(r["factors"]) == 8, r["factors"]
    print("PASS test_score_all_on (100, 8 factors)")


def test_score_all_off():
    r = rd.compute_d1_trend_score(_FULL_BULL)
    assert r["bearish_score"] == 0 and r["bullish_score"] == 100 and r["direction"] == "BULLISH", r
    print("PASS test_score_all_off (bearish 0, bullish 100)")


def test_score_empty():
    r = rd.compute_d1_trend_score({})
    assert r["bearish_score"] == 0 and r["direction"] == "NEUTRAL", r
    print("PASS test_score_empty (0, NEUTRAL)")


def test_score_partial_ema_only():
    # close<ema50 (0.10) + close<ema200 (0.15) only = 25. ema50>ema200 so NO
    # death cross (price below both EMAs but EMA50 still above EMA200).
    r = rd.compute_d1_trend_score({"close": 4300, "ema50": 4500, "ema200": 4400})
    assert r["bearish_score"] == 25, r
    print("PASS test_score_partial_ema_only (25)")


def test_score_adx_factor():
    # adx>25 AND -DI>+DI only = 0.15 = 15
    r = rd.compute_d1_trend_score({"adx": 30, "plus_di": 10, "minus_di": 25})
    assert r["bearish_score"] == 15, r
    print("PASS test_score_adx_factor (15)")


def test_score_no_deathcross_above_ema200():
    # The REAL recent-market shape: below ema50 + 3bars + slope + distance +
    # structure, but ABOVE ema200, no death cross, adx<25 -> 60 (< 70 threshold)
    r = rd.compute_d1_trend_score({"close": 4511, "ema50": 4686, "ema200": 4406, "atr": 109,
                                   "adx": 20.3, "plus_di": 13.4, "minus_di": 23.9,
                                   "bars_below_ema50": 8, "ema50_slope": -8, "swing": "LH_LL"})
    assert r["bearish_score"] == 60, r
    print("PASS test_score_no_deathcross_above_ema200 (60 — below the 70 gate!)")


def _plan(direction, exceptions=None):
    return SimpleNamespace(id="PLAN-TEST", entry=SimpleNamespace(direction=direction),
                           analysis=SimpleNamespace(counter_trend_exceptions=exceptions))


def test_gate_buy_high_few_exceptions_reject():
    errs = _check_d1_trend_gate(_plan("BUY", ["only_one"]), {"bearish_score": 72, "bullish_score": 5})
    assert errs and "d1_trend_gate" in errs[0], errs
    print("PASS test_gate_buy_high_few_exceptions_reject")


def test_gate_buy_low_allow():
    errs = _check_d1_trend_gate(_plan("BUY"), {"bearish_score": 50, "bullish_score": 5})
    assert errs == [], errs
    print("PASS test_gate_buy_low_allow")


def test_gate_sell_high_bear_allow():
    # SELL aligned with a bearish D1 -> opposing (bullish) score is low -> ALLOW
    errs = _check_d1_trend_gate(_plan("SELL"), {"bearish_score": 80, "bullish_score": 10})
    assert errs == [], errs
    print("PASS test_gate_sell_high_bear_allow")


def test_gate_buy_high_3_exceptions_allow():
    errs = _check_d1_trend_gate(_plan("BUY", ["a", "b", "c"]), {"bearish_score": 80, "bullish_score": 5})
    assert errs == [], errs
    print("PASS test_gate_buy_high_3_exceptions_allow")


def test_gate_none_failopen():
    errs = _check_d1_trend_gate(_plan("BUY"), None)
    assert errs == [], errs
    print("PASS test_gate_none_failopen")


def counterfactual():
    """Score the 5 recent plans. D1 structure was ~constant May 18-21 (price
    below a falling EMA50 but ABOVE a rising EMA200, ADX ~20), so all share the
    representative score. Show gate decision at 70 (spec) vs 55 (tuned) vs reality."""
    import sqlite3, json
    rep = rd.compute_d1_trend_score({"close": 4511, "ema50": 4686, "ema200": 4406, "atr": 109,
                                     "adx": 20.3, "plus_di": 13.4, "minus_di": 23.9,
                                     "bars_below_ema50": 8, "ema50_slope": -8, "swing": "LH_LL"})
    score = rep["bearish_score"]
    plans = [("PLAN-20260518-004", "SELL", "+66.51 WIN"), ("PLAN-20260519-001", "SELL", "+19.94 WIN"),
             ("PLAN-20260520-006", "BUY", "+23.48 win"), ("PLAN-20260521-001", "BUY", "-48.02 LOSS"),
             ("PLAN-20260520-005", "BUY", "-14.54 open LOSS")]
    print(f"\n=== COUNTERFACTUAL (representative D1 bearish_score={score}) ===")
    print(f"{'plan':22} {'dir':5} {'reality':16} {'@thr70':8} {'@thr55':8}")
    for pid, d, reality in plans:
        # old plans cite 0 exceptions
        rej70 = (d == "BUY" and score >= 70)
        rej55 = (d == "BUY" and score >= 55)
        print(f"{pid:22} {d:5} {reality:16} {'REJECT' if rej70 else 'ALLOW':8} {'REJECT' if rej55 else 'ALLOW':8}")
    print(f"\nNote: at the spec threshold {_D1_GATE_THRESHOLD}, score {score} < 70 -> gate INERT "
          f"(BUYs ALLOWed, does NOT match reality). At 55, the 3 counter-HTF BUYs REJECT and "
          f"the 2 SELL winners ALLOW -> matches reality (saves -$62, forgoes +$23 = +$39 net).")


if __name__ == "__main__":
    for fn in [test_score_all_on, test_score_all_off, test_score_empty, test_score_partial_ema_only,
               test_score_adx_factor, test_score_no_deathcross_above_ema200,
               test_gate_buy_high_few_exceptions_reject, test_gate_buy_low_allow,
               test_gate_sell_high_bear_allow, test_gate_buy_high_3_exceptions_allow,
               test_gate_none_failopen]:
        fn()
    counterfactual()
    print("\nALL FLO-452 TESTS PASSED")
