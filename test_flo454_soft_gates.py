"""FLO-454 — gate reclassification: 5 HARD (block) + 4 SOFT (warn, don't block).
Standalone. Run: python test_flo454_soft_gates.py
"""
import inspect
from types import SimpleNamespace

import snow.validator as V

_SRC = inspect.getsource(V.validate_plan)


def test_hard_gates_still_block():
    # The 5 HARD gates remain `errors += ...` (blocking) in validate_plan.
    for hard in ("_check_daily_loss_limit", "_check_active_plan_cap",
                 "_check_sl_buffer_from_structure", "_check_news_blackout_gate",
                 "_check_d1_trend_gate"):
        assert f"errors += {hard}" in _SRC, f"HARD gate {hard} must stay blocking"
    print("PASS test_hard_gates_still_block (439/428/445/436/452 all errors+=)")


def test_soft_gates_demoted():
    # The 4 SOFT gates (FLO-427 regime, FLO-430 ADX-override [inside regime],
    # FLO-453 setup-regime, FLO-429 give-back) must NOT be `errors += ...`.
    for soft in ("_check_regime_counter_trend_gate", "_check_setup_regime_gate",
                 "_check_give_back_calibration"):
        assert f"errors += {soft}" not in _SRC, f"{soft} must be SOFT (not blocking)"
        assert soft in _SRC, f"{soft} must still RUN (advisory)"
    assert "_SOFT_WARNING" in _SRC, "soft gates must emit a *_SOFT_WARNING"
    print("PASS test_soft_gates_demoted (regime/setup_regime/give_back not blocking, log SOFT_WARNING)")


def test_soft_detectors_still_run():
    # Functions still detect (so they can log a warning) — softening is at the
    # call site, the detectors are unchanged.
    p = SimpleNamespace(id="X", analysis=SimpleNamespace(setup_type="continuation_momentum"))
    assert V._check_setup_regime_gate(p, {"adx": 20, "adx_rising": True}), "setup_regime still detects ADX mismatch"
    print("PASS test_soft_detectors_still_run")


def test_hard_d1_gate_rejects_directly():
    p = SimpleNamespace(id="X", entry=SimpleNamespace(direction="BUY"),
                        analysis=SimpleNamespace(counter_trend_exceptions=None))
    assert V._check_d1_trend_gate(p, {"bearish_score": 60, "bullish_score": 40}), "d1_trend (HARD) still REJECTs"
    assert V._check_d1_trend_gate(p, {"bearish_score": 40, "bullish_score": 40}) == [], "d1_trend below threshold ALLOWs"
    print("PASS test_hard_d1_gate_rejects_directly")


if __name__ == "__main__":
    test_hard_gates_still_block()
    test_soft_gates_demoted()
    test_soft_detectors_still_run()
    test_hard_d1_gate_rejects_directly()
    print("\nALL FLO-454 TESTS PASSED")
