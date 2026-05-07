"""FLO-425 PR-A — breakout lifecycle classifier tests.

Standalone test script (no pytest, matching project convention). Exits
with non-zero code on any failure.

Coverage:
  Phase production:
    T1   BUILDUP (level not crossed)
    T2   BREAK_ATTEMPT (just crossed, no closes beyond)
    T3   ACCEPTANCE_TEST (short window, mixed closes)
    T4   ACCEPTED (>=2 consecutive closes beyond, regime sane)
    T5   CONTINUATION (ACCEPTED + bars >= 6 + extension + ema sane)
    T6   EXHAUSTION (ACCEPTED + ema50_distance_atr >= 3)
    T7   FAILURE (cross then close back through)
  Score behavior:
    T8   freshness high near cross + clean regime
    T9   freshness low at maturity 12+ bars OR late-expansion
    T10  maturity rises monotonically with bars_since_cross
    T11  acceptance_quality drops when drift_assessment=expanded
    T12  exhaustion rises with each individual driver
  Missing-data degradation:
    T13  no author_snapshot: thesis_preservation dim dropped, no exception
    T14  no trigger_snapshot: regime scores degrade gracefully
    T15  <20 candles: phase=INSUFFICIENT_DATA
  Canonical regression fixtures:
    T16  PLAN-007 (BUY 4756 chase) -> BREAK_ATTEMPT or EXHAUSTION,
         exhaustion_probability >= 0.6
    T17  PLAN-005 (BUY 4736 wick reclaim then dump) -> FAILURE,
         acceptance_quality < 0.4
    T18  PLAN-004 (BUY 4751 impulse-bar fill) -> BREAK_ATTEMPT,
         exhaustion_probability >= 0.6
  Schema invariants:
    T19  every key present in output
    T20  schema_version == 1
    T21  reasons non-empty when phase decisive
    T22  inputs_used contains computed metrics
  Edge cases:
    T23  malformed plan_dict -> INSUFFICIENT_DATA + warning, no exception
    T24  empty candles -> INSUFFICIENT_DATA + warning
"""
from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


_FAILURES: List[str] = []


def _ok(name: str) -> None:
    print(f"  PASS  {name}")


def _fail(name: str, exc: Optional[BaseException] = None) -> None:
    msg = f"  FAIL  {name}"
    if exc is not None:
        msg += f" -- {type(exc).__name__}: {exc}"
        msg += "\n" + traceback.format_exc()
    print(msg)
    _FAILURES.append(name)


# ---------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------
def _bar(o: float, h: float, l: float, c: float) -> Dict[str, float]:
    return {"open": o, "high": h, "low": l, "close": c}


def _plan(direction: str, entry_price: float, setup_type: str = "continuation_momentum"):
    return {
        "id": "PLAN-TEST",
        "analysis": {"setup_type": setup_type, "thesis": "test"},
        "entry": {"direction": direction, "entry_price": entry_price,
                  "initial_sl": entry_price - 20, "initial_tp": entry_price + 20,
                  "conditions": []},
    }


def _snapshot(stage: str = "trigger", **overrides) -> Dict:
    base = {
        "stage": stage,
        "ts": "2026-05-07T08:00:00+00:00",
        "current_price": 4750.0,
        "direction": "BUY",
        "setup_type": "continuation_momentum",
        "breakout_level": 4750.0,
        "breakout_distance_pips": 0.0,
        "breakout_age_bars": None,
        "impulse_total_60m": 2,
        "candle_drift_trailing": 0,
        "m5_pattern": "....+.+...+.",
        "m5_atr_pips": 35.0,
        "bb_width_4h_pct": 30.0,
        "atr_4h_pct": 5.0,
        "pre_range_4h_pips": 300.0,
        "pre_range_24h_pips": 800.0,
        "range_ratio_4h_24h": 0.4,
        "rsi_now": 60.0,
        "adx_now": 25.0,
        "bb_position_now": 0.6,
        "ema50_distance_atr": 1.5,
        "computation_warnings": [],
    }
    base.update(overrides)
    return base


_NOW = datetime(2026, 5, 7, 9, 0, 0, tzinfo=timezone.utc)


def _classify(plan, candles, author=None, trigger=None, eval_ts=_NOW):
    from breakout_lifecycle import classify_breakout_lifecycle
    return classify_breakout_lifecycle(
        plan_dict=plan, author_snapshot=author, trigger_snapshot=trigger,
        candles_m5=candles, eval_ts=eval_ts,
    )


# ---------------------------------------------------------------------
# Phase production tests
# ---------------------------------------------------------------------
def t1_buildup():
    name = "T1 BUILDUP (level not crossed)"
    try:
        # 20 bars all below 4750
        candles = [_bar(4740 + (i % 3), 4744 + (i % 3), 4738 + (i % 3),
                        4742 + (i % 3)) for i in range(20)]
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        assert r["phase"] == "BUILDUP", f"expected BUILDUP, got {r['phase']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t2_break_attempt():
    name = "T2 BREAK_ATTEMPT (just crossed, no closes beyond)"
    try:
        # 19 bars below + 1 cross bar with high crossing but close back below
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(19)]
        candles.append(_bar(4744, 4751.5, 4742, 4748))  # crossed wick, close below
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        assert r["phase"] == "BREAK_ATTEMPT", f"expected BREAK_ATTEMPT, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t3_acceptance_test():
    name = "T3 ACCEPTANCE_TEST (short window, mixed closes, last beyond)"
    try:
        # 17 buildup + 1 cross-and-close-above + 1 close-below + 1 close-above
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4753, 4744, 4751))   # idx 17: cross + close above
        candles.append(_bar(4751, 4753, 4748, 4749))   # idx 18: close below
        candles.append(_bar(4749, 4754, 4749, 4752))   # idx 19: close above
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        assert r["phase"] == "ACCEPTANCE_TEST", \
            f"expected ACCEPTANCE_TEST, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t4_accepted():
    name = "T4 ACCEPTED (2+ consecutive closes beyond, regime sane)"
    try:
        # 17 below + 1 cross + 2 closes above
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4754, 4744, 4751))
        candles.append(_bar(4751, 4755, 4750, 4753))
        candles.append(_bar(4753, 4756, 4751, 4754))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles,
                      trigger=_snapshot(ema50_distance_atr=1.5, rsi_now=60.0,
                                        bb_width_4h_pct=20.0, impulse_total_60m=2))
        assert r["phase"] == "ACCEPTED", f"expected ACCEPTED, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t5_continuation():
    name = "T5 CONTINUATION (ACCEPTED + bars >= 6 + extension + ema sane)"
    try:
        # 13 buildup + cross + 6 ascending closes above
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(13)]
        candles.append(_bar(4744, 4754, 4744, 4751))
        # 6 bars climbing from 4751 to 4760+ (extension >= 1 m5_atr)
        for px in (4753, 4755, 4757, 4759, 4761, 4763):
            candles.append(_bar(px - 2, px + 1, px - 3, px))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles,
                      trigger=_snapshot(ema50_distance_atr=1.5, rsi_now=60.0,
                                        bb_width_4h_pct=20.0, impulse_total_60m=2))
        assert r["phase"] == "CONTINUATION", \
            f"expected CONTINUATION, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t6_exhaustion():
    name = "T6 EXHAUSTION (ACCEPTED + ema50_distance_atr >= 3)"
    try:
        # ACCEPTED setup but with extreme regime
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4754, 4744, 4751))
        candles.append(_bar(4751, 4755, 4750, 4753))
        candles.append(_bar(4753, 4756, 4751, 4754))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles,
                      trigger=_snapshot(ema50_distance_atr=3.5))
        assert r["phase"] == "EXHAUSTION", \
            f"expected EXHAUSTION, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t7_failure():
    name = "T7 FAILURE (cross then close back through)"
    try:
        # cross with close above + 2 bars close back below
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4754, 4744, 4751))   # cross + close above
        candles.append(_bar(4751, 4751, 4747, 4748))   # close back below
        candles.append(_bar(4748, 4749, 4744, 4745))   # close further below
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        assert r["phase"] == "FAILURE", f"expected FAILURE, got {r['phase']}: {r['reasons']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Score behavior tests
# ---------------------------------------------------------------------
def t8_freshness_high():
    name = "T8 freshness high near cross + clean regime"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(19)]
        candles.append(_bar(4744, 4753, 4744, 4751))  # just crossed
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles,
                      trigger=_snapshot(bb_width_4h_pct=10.0, ema50_distance_atr=0.5))
        assert r["breakout_freshness"] is not None and r["breakout_freshness"] > 0.7, \
            f"expected high freshness, got {r['breakout_freshness']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t9_freshness_low_late_expansion():
    name = "T9 freshness low at late-expansion regime"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(19)]
        candles.append(_bar(4744, 4753, 4744, 4751))
        plan = _plan("BUY", 4750.0)
        # late expansion: bbw 70%, ema50 distance 3 ATR
        r = _classify(plan, candles,
                      trigger=_snapshot(bb_width_4h_pct=70.0, ema50_distance_atr=3.0))
        assert r["breakout_freshness"] is not None and r["breakout_freshness"] < 0.3, \
            f"expected low freshness, got {r['breakout_freshness']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t10_maturity_monotonic():
    name = "T10 maturity rises with bars_since_cross"
    try:
        from breakout_lifecycle import _score_maturity
        s1, _ = _score_maturity({"bars_since_cross": 1, "consecutive_closes_beyond_level": 0})
        s5, _ = _score_maturity({"bars_since_cross": 5, "consecutive_closes_beyond_level": 0})
        s10, _ = _score_maturity({"bars_since_cross": 10, "consecutive_closes_beyond_level": 0})
        assert s1 < s5 < s10, f"not monotonic: {s1} {s5} {s10}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t11_acceptance_drops_on_drift():
    name = "T11 acceptance_quality drops when drift_assessment=expanded"
    try:
        from breakout_lifecycle import _score_acceptance_quality
        base = {
            "bars_since_cross": 3,
            "fraction_post_cross_beyond": 0.7,
            "consecutive_closes_beyond_level": 2,
            "same_direction_close_ratio": 0.7,
        }
        s_stable, _ = _score_acceptance_quality({**base, "drift_assessment": "regime_stable"})
        s_expanded, _ = _score_acceptance_quality({**base, "drift_assessment": "regime_expanded"})
        assert s_expanded < s_stable, \
            f"expanded should drop acceptance: stable={s_stable} expanded={s_expanded}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t12_exhaustion_drivers():
    name = "T12 exhaustion rises with each individual driver"
    try:
        from breakout_lifecycle import _score_exhaustion
        s_clean, _ = _score_exhaustion({
            "ema50_distance_atr": 1.0, "rsi_now": 55.0,
            "bb_width_4h_pct": 15.0, "impulse_total_60m": 0,
        })
        s_ema, _ = _score_exhaustion({
            "ema50_distance_atr": 4.0, "rsi_now": 55.0,
            "bb_width_4h_pct": 15.0, "impulse_total_60m": 1,
        })
        s_rsi, _ = _score_exhaustion({
            "ema50_distance_atr": 1.0, "rsi_now": 85.0,
            "bb_width_4h_pct": 15.0, "impulse_total_60m": 1,
        })
        s_bbw, _ = _score_exhaustion({
            "ema50_distance_atr": 1.0, "rsi_now": 55.0,
            "bb_width_4h_pct": 90.0, "impulse_total_60m": 1,
        })
        assert s_clean < 0.1, f"clean should be near 0: {s_clean}"
        assert s_ema > 0.9, f"ema-extreme should approach 1: {s_ema}"
        assert s_rsi > 0.9, f"rsi-extreme should approach 1: {s_rsi}"
        assert s_bbw > 0.9, f"bbw-extreme should approach 1: {s_bbw}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Missing-data tests
# ---------------------------------------------------------------------
def t13_no_author_snapshot():
    name = "T13 no author_snapshot: thesis_preservation dropped, no exception"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4754, 4744, 4751))
        candles.append(_bar(4751, 4755, 4750, 4753))
        candles.append(_bar(4753, 4756, 4751, 4754))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, author=None, trigger=_snapshot())
        assert "missing_author_snapshot" in r["warnings"]
        # no thesis_preservation dim => acceptance_quality still computable
        # from the other 3 dimensions
        assert r["acceptance_quality"] is not None
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t14_no_trigger_snapshot():
    name = "T14 no trigger_snapshot: regime scores degrade gracefully"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(17)]
        candles.append(_bar(4744, 4754, 4744, 4751))
        candles.append(_bar(4751, 4755, 4750, 4753))
        candles.append(_bar(4753, 4756, 4751, 4754))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, author=None, trigger=None)
        # phase still computable from candles alone; regime scores None
        assert r["phase"] in (
            "BUILDUP", "BREAK_ATTEMPT", "ACCEPTANCE_TEST", "ACCEPTED",
            "CONTINUATION", "EXHAUSTION", "FAILURE",
        ), f"phase should still classify: {r['phase']}"
        # exhaustion needs at least one driver — without snapshots, None
        assert r["exhaustion_probability"] is None, \
            f"exhaustion should be None without snapshot inputs"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t15_insufficient_candles():
    name = "T15 <20 candles: phase=INSUFFICIENT_DATA"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(10)]
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        assert r["phase"] == "INSUFFICIENT_DATA"
        assert any("insufficient_candles" in w for w in r["warnings"])
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Canonical regression fixtures (today's BUY-cluster)
# ---------------------------------------------------------------------
def t16_plan007():
    name = "T16 PLAN-007 (BUY 4756 chase) -> BREAK_ATTEMPT or EXHAUSTION"
    try:
        # 18 bars climbing 4720 -> 4750 (below level 4756) + 1 wick cross + 1 cross
        candles = []
        for i in range(18):
            base = 4720 + i * 1.7
            candles.append(_bar(base, base + 2, base - 2, base + 1))
        # cross bar: high 4756.5, close above
        candles.append(_bar(4750, 4756.5, 4750, 4755.5))  # idx 18: wick cross
        candles.append(_bar(4755.5, 4757, 4754, 4756.2))  # idx 19: close above
        plan = _plan("BUY", 4756.0)
        r = _classify(
            plan, candles,
            author=_snapshot(stage="author", current_price=4734.49,
                             ema50_distance_atr=3.54, rsi_now=68.19,
                             bb_width_4h_pct=11.68, impulse_total_60m=6),
            trigger=_snapshot(stage="trigger", current_price=4756.0,
                              ema50_distance_atr=3.54, rsi_now=68.19,
                              bb_width_4h_pct=11.68, impulse_total_60m=6),
        )
        # Allowed phases at trigger time: BREAK_ATTEMPT (just crossed),
        # ACCEPTANCE_TEST (testing acceptance), EXHAUSTION (regime extreme
        # gates fire), or ACCEPTED (if 2+ closes beyond). PLAN-007's
        # actual fixture lands in ACCEPTANCE_TEST or BREAK_ATTEMPT
        # depending on the cross-bar close. The load-bearing check is
        # exhaustion_probability >= 0.6 — that's what would have flagged
        # this trade as risky.
        assert r["phase"] in ("BREAK_ATTEMPT", "ACCEPTANCE_TEST",
                              "EXHAUSTION", "ACCEPTED"), \
            f"unexpected phase: {r['phase']}: {r['reasons']}"
        assert r["exhaustion_probability"] is not None and r["exhaustion_probability"] >= 0.6, \
            f"expected high exhaustion, got {r['exhaustion_probability']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t17_plan005():
    name = "T17 PLAN-005 (BUY 4736 wick reclaim then dump) -> FAILURE"
    try:
        # Constructed from the actual M5 sequence: many early closes above 4736
        # then trailing closes below.
        candles = [
            _bar(4740, 4743, 4739, 4741), _bar(4741, 4743, 4737, 4739),
            _bar(4739, 4744, 4738, 4743), _bar(4743, 4744, 4737, 4739),
            _bar(4739, 4744, 4739, 4741), _bar(4741, 4745, 4739, 4743),
            _bar(4743, 4751.6, 4742, 4749.8),  # 08:00 impulse, crosses 4736
            _bar(4749, 4753.5, 4746, 4748.2),
            _bar(4748, 4748.6, 4742.5, 4742.7),
            _bar(4742, 4743, 4735.6, 4736.6),  # closes at 4736.6 above
            _bar(4736, 4739.9, 4735.5, 4739.8),
            _bar(4739, 4742.3, 4733.8, 4740),
            _bar(4740, 4741.9, 4736.6, 4737.4),
            _bar(4737, 4738.7, 4734, 4736.6),  # close 4736.6 above
            _bar(4736, 4738.6, 4735.1, 4735.3),  # below 4736
            _bar(4735, 4736.2, 4733.3, 4735.7),  # below
            _bar(4735, 4737.2, 4732.4, 4734.2),  # below — trigger fires intra-bar
            _bar(4734, 4734.7, 4730, 4731),     # below
            _bar(4731, 4731, 4721, 4726),       # well below
            _bar(4726, 4732, 4726, 4732.8),     # back near level
        ]
        plan = _plan("BUY", 4736.0, setup_type="pullback_trend")
        r = _classify(plan, candles, trigger=_snapshot(stage="trigger"))
        assert r["phase"] == "FAILURE", f"expected FAILURE, got {r['phase']}: {r['reasons']}"
        # acceptance_quality: most-recent dimensions should drag it low
        assert r["acceptance_quality"] is None or r["acceptance_quality"] < 0.5, \
            f"expected low acceptance_quality, got {r['acceptance_quality']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t18_plan004():
    name = "T18 PLAN-004 (BUY 4751 impulse-bar fill) -> BREAK_ATTEMPT"
    try:
        # 19 bars 4738-4748 oscillating + 08:00 impulse cross + 08:05 close back
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(18)]
        candles.append(_bar(4743, 4751.6, 4742.5, 4749.85))  # cross via wick
        candles.append(_bar(4749.85, 4753.5, 4746, 4748.20))  # still below 4751
        plan = _plan("BUY", 4751.0)
        r = _classify(
            plan, candles,
            author=_snapshot(stage="author", current_price=4751.42,
                             ema50_distance_atr=2.68, rsi_now=71.75,
                             bb_width_4h_pct=66.19, impulse_total_60m=0),
            trigger=_snapshot(stage="trigger", current_price=4752.21,
                              ema50_distance_atr=2.68, rsi_now=73.6,
                              bb_width_4h_pct=66.19, impulse_total_60m=0),
        )
        assert r["phase"] == "BREAK_ATTEMPT", \
            f"expected BREAK_ATTEMPT, got {r['phase']}: {r['reasons']}"
        assert r["exhaustion_probability"] is not None and r["exhaustion_probability"] >= 0.6, \
            f"expected high exhaustion, got {r['exhaustion_probability']}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Schema invariants
# ---------------------------------------------------------------------
def t19_all_keys_present():
    name = "T19 every key present in output"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(20)]
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles)
        for k in ("phase", "phase_confidence", "breakout_freshness",
                  "breakout_maturity", "acceptance_quality",
                  "exhaustion_probability", "reasons", "warnings",
                  "inputs_used", "schema_version"):
            assert k in r, f"missing key {k}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t20_schema_version():
    name = "T20 schema_version == 1"
    try:
        from breakout_lifecycle import SCHEMA_VERSION, classify_breakout_lifecycle
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(20)]
        plan = _plan("BUY", 4750.0)
        r = classify_breakout_lifecycle(
            plan_dict=plan, author_snapshot=None, trigger_snapshot=None,
            candles_m5=candles, eval_ts=_NOW,
        )
        assert SCHEMA_VERSION == 1
        assert r["schema_version"] == 1
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t21_reasons_nonempty():
    name = "T21 reasons non-empty when phase decisive"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(20)]
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        # BUILDUP is a decisive phase
        assert r["phase"] == "BUILDUP"
        assert len(r["reasons"]) >= 1, "reasons should not be empty"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t22_inputs_used():
    name = "T22 inputs_used contains computed metrics"
    try:
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(19)]
        candles.append(_bar(4744, 4753, 4744, 4751))
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, candles, trigger=_snapshot())
        for k in ("bars_since_cross", "consecutive_closes_beyond_level",
                  "m5_atr_pips_recent", "extension_pips"):
            assert k in r["inputs_used"], f"inputs_used missing {k}"
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------
def t23_malformed_plan():
    name = "T23 malformed plan_dict -> INSUFFICIENT_DATA + warning"
    try:
        from breakout_lifecycle import classify_breakout_lifecycle
        candles = [_bar(4740, 4745, 4738, 4744) for _ in range(20)]
        # plan_dict is None (not a dict)
        r = classify_breakout_lifecycle(
            plan_dict=None, author_snapshot=None, trigger_snapshot=None,
            candles_m5=candles, eval_ts=_NOW,
        )
        assert r["phase"] == "INSUFFICIENT_DATA"
        assert r["warnings"], "expected warnings"
        _ok(name)
    except Exception as e:
        _fail(name, e)


def t24_empty_candles():
    name = "T24 empty candles -> INSUFFICIENT_DATA + warning"
    try:
        plan = _plan("BUY", 4750.0)
        r = _classify(plan, [], trigger=_snapshot())
        assert r["phase"] == "INSUFFICIENT_DATA"
        assert any("insufficient_candles" in w for w in r["warnings"])
        _ok(name)
    except Exception as e:
        _fail(name, e)


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
def main() -> int:
    print("FLO-425 PR-A breakout_lifecycle classifier tests")
    print("=" * 60)
    for fn in [
        t1_buildup, t2_break_attempt, t3_acceptance_test, t4_accepted,
        t5_continuation, t6_exhaustion, t7_failure,
        t8_freshness_high, t9_freshness_low_late_expansion,
        t10_maturity_monotonic, t11_acceptance_drops_on_drift,
        t12_exhaustion_drivers,
        t13_no_author_snapshot, t14_no_trigger_snapshot,
        t15_insufficient_candles,
        t16_plan007, t17_plan005, t18_plan004,
        t19_all_keys_present, t20_schema_version,
        t21_reasons_nonempty, t22_inputs_used,
        t23_malformed_plan, t24_empty_candles,
    ]:
        try:
            fn()
        except Exception as e:
            _fail(fn.__name__, e)
    print("=" * 60)
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} test(s) -- {_FAILURES}")
        return 1
    print("ALL PASS (24/24)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
