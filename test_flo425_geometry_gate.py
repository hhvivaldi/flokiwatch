"""FLO-425 §16f anti-smuggling geometry gate tests.

Standalone test script (no pytest, matching project convention). Exits
with non-zero code on any failure.

Coverage:
  T1   BUY chase blocked (entry > current + threshold)
  T2   BUY at-current allowed (within threshold)
  T3   BUY below-current allowed (pullback shape)
  T4   SELL chase blocked (entry < current - threshold)
  T5   SELL at-current allowed
  T6   SELL above-current allowed (pullback shape)
  T7   threshold boundary: exactly +50p allowed (strict >, not >=)
  T8   threshold boundary: exactly +51p blocked
  T9   env override: FLO425_GEOMETRY_GATE_PIPS=30 -> 35p blocked, 25p allowed
  T10  env override: FLO425_GEOMETRY_GATE_PIPS=0 -> gate disabled
  T11  time-box: ts < GATE_UNTIL -> gate active
  T12  time-box: ts >= GATE_UNTIL -> gate inactive (fail-open)
  T13  MT5 fetch fails -> fail-open with no exception
  T14  malformed FLO425_GEOMETRY_GATE_UNTIL -> fail-open
  T15  PLAN-007 fixture (BUY 4756 vs current 4734.49) -> BLOCKED
  T16  PLAN-005 fixture (BUY 4736 vs current 4751.42) -> ALLOWED (pullback)
  T17  PLAN-004 fixture (BUY 4751 vs current 4751.42) -> ALLOWED (documented limitation)
  T18  rejection error includes all required fields
  T19  setup_type independence: chase blocked under all of breakout_range,
       continuation_momentum, pullback_trend, structural_bounce
"""
from __future__ import annotations
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import List, Optional

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
# Minimal Plan stand-in. The real validator imports the Pydantic Plan
# from snow.schema, but the gate only reads .entry.direction,
# .entry.entry_price, .analysis.setup_type, and .id — duck typing is
# enough for unit testing the gate in isolation.
# ---------------------------------------------------------------------
class _Entry:
    def __init__(self, direction: str, entry_price: float):
        self.direction = direction
        self.entry_price = entry_price


class _Analysis:
    def __init__(self, setup_type: str):
        self.setup_type = setup_type


class _Plan:
    def __init__(self, plan_id: str, direction: str, entry_price: float,
                 setup_type: str = "continuation_momentum"):
        self.id = plan_id
        self.entry = _Entry(direction, entry_price)
        self.analysis = _Analysis(setup_type)


# ---------------------------------------------------------------------
# Patch helpers: redirect MT5 tick + config knobs.
# ---------------------------------------------------------------------
def _install_patches(current_price: Optional[float],
                     threshold_pips: int = 50,
                     until: str = "2099-01-01T00:00:00Z"):
    """Monkey-patch the gate's MT5 read + config attrs."""
    import config
    import snow.validator as v

    originals = {
        "FLO425_GEOMETRY_GATE_PIPS": getattr(config, "FLO425_GEOMETRY_GATE_PIPS", None),
        "FLO425_GEOMETRY_GATE_UNTIL": getattr(config, "FLO425_GEOMETRY_GATE_UNTIL", None),
        "_flo425_get_current_price": v._flo425_get_current_price,
    }
    config.FLO425_GEOMETRY_GATE_PIPS = threshold_pips
    config.FLO425_GEOMETRY_GATE_UNTIL = until
    v._flo425_get_current_price = lambda: current_price
    return originals


def _uninstall_patches(originals):
    import config
    import snow.validator as v

    if originals["FLO425_GEOMETRY_GATE_PIPS"] is None:
        if hasattr(config, "FLO425_GEOMETRY_GATE_PIPS"):
            delattr(config, "FLO425_GEOMETRY_GATE_PIPS")
    else:
        config.FLO425_GEOMETRY_GATE_PIPS = originals["FLO425_GEOMETRY_GATE_PIPS"]
    if originals["FLO425_GEOMETRY_GATE_UNTIL"] is None:
        if hasattr(config, "FLO425_GEOMETRY_GATE_UNTIL"):
            delattr(config, "FLO425_GEOMETRY_GATE_UNTIL")
    else:
        config.FLO425_GEOMETRY_GATE_UNTIL = originals["FLO425_GEOMETRY_GATE_UNTIL"]
    v._flo425_get_current_price = originals["_flo425_get_current_price"]


def _run_gate(plan):
    from snow.validator import _check_flo425_geometry_gate
    return _check_flo425_geometry_gate(plan)


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------
def t1_buy_chase_blocked():
    name = "T1 BUY chase blocked (entry > current + threshold)"
    o = _install_patches(current_price=4700.0)
    try:
        plan = _Plan("PLAN-T1", "BUY", 4760.0)  # +600p chase
        errors = _run_gate(plan)
        assert errors, "expected rejection"
        assert "geometry gate" in errors[0].lower()
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t2_buy_at_current_allowed():
    name = "T2 BUY at-current allowed (within threshold)"
    o = _install_patches(current_price=4700.0)
    try:
        # +30p — under default 50 threshold
        plan = _Plan("PLAN-T2", "BUY", 4703.0)
        errors = _run_gate(plan)
        assert not errors, f"unexpected rejection: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t3_buy_below_current_allowed():
    name = "T3 BUY below-current allowed (pullback shape)"
    o = _install_patches(current_price=4750.0)
    try:
        # -150p — pullback shape; gate doesn't apply
        plan = _Plan("PLAN-T3", "BUY", 4735.0, setup_type="pullback_trend")
        errors = _run_gate(plan)
        assert not errors, f"unexpected rejection: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t4_sell_chase_blocked():
    name = "T4 SELL chase blocked (entry < current - threshold)"
    o = _install_patches(current_price=4750.0)
    try:
        # -100p chase: entry 4740 with current 4750 = +100p (chase side for SELL)
        plan = _Plan("PLAN-T4", "SELL", 4740.0)
        errors = _run_gate(plan)
        assert errors, "expected rejection"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t5_sell_at_current_allowed():
    name = "T5 SELL at-current allowed"
    o = _install_patches(current_price=4750.0)
    try:
        plan = _Plan("PLAN-T5", "SELL", 4747.0)  # -30p chase, under 50
        errors = _run_gate(plan)
        assert not errors, f"unexpected rejection: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t6_sell_above_current_allowed():
    name = "T6 SELL above-current allowed (pullback shape)"
    o = _install_patches(current_price=4750.0)
    try:
        plan = _Plan("PLAN-T6", "SELL", 4765.0, setup_type="pullback_trend")
        errors = _run_gate(plan)
        assert not errors, f"unexpected rejection: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t7_boundary_50p_allowed():
    name = "T7 boundary: exactly +50p allowed (strict >, not >=)"
    o = _install_patches(current_price=4700.0)
    try:
        plan = _Plan("PLAN-T7", "BUY", 4705.0)  # +50p exactly
        errors = _run_gate(plan)
        assert not errors, f"50p should be allowed, got: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t8_boundary_51p_blocked():
    name = "T8 boundary: +51p blocked"
    o = _install_patches(current_price=4700.0)
    try:
        plan = _Plan("PLAN-T8", "BUY", 4705.1)  # +51p
        errors = _run_gate(plan)
        assert errors, "51p should be blocked"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t9_env_threshold_override():
    name = "T9 env override pips=30: 35p blocked, 25p allowed"
    o = _install_patches(current_price=4700.0, threshold_pips=30)
    try:
        plan_block = _Plan("PLAN-T9a", "BUY", 4703.5)  # +35p
        errors = _run_gate(plan_block)
        assert errors, "35p with threshold=30 should be blocked"
        plan_allow = _Plan("PLAN-T9b", "BUY", 4702.5)  # +25p
        errors = _run_gate(plan_allow)
        assert not errors, f"25p with threshold=30 should be allowed: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t10_env_disabled():
    name = "T10 env override pips=0: gate disabled"
    o = _install_patches(current_price=4700.0, threshold_pips=0)
    try:
        plan = _Plan("PLAN-T10", "BUY", 4900.0)  # +2000p — should pass
        errors = _run_gate(plan)
        assert not errors, f"gate should be disabled, got: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t11_timebox_active():
    name = "T11 time-box: now < GATE_UNTIL -> gate active"
    future = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat().replace(
        "+00:00", "Z"
    )
    o = _install_patches(current_price=4700.0, until=future)
    try:
        plan = _Plan("PLAN-T11", "BUY", 4760.0)
        errors = _run_gate(plan)
        assert errors, "gate should be active before until-date"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t12_timebox_expired():
    name = "T12 time-box: now >= GATE_UNTIL -> gate inactive (fail-open)"
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )
    o = _install_patches(current_price=4700.0, until=past)
    try:
        plan = _Plan("PLAN-T12", "BUY", 4900.0)  # +2000p — would normally block
        errors = _run_gate(plan)
        assert not errors, f"gate should be expired, got: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t13_mt5_fetch_fails():
    name = "T13 MT5 fetch fails -> fail-open, no exception"
    o = _install_patches(current_price=None)  # MT5 read returns None
    try:
        plan = _Plan("PLAN-T13", "BUY", 4900.0)
        errors = _run_gate(plan)
        assert not errors, f"should fail-open on MT5 failure, got: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t14_malformed_until():
    name = "T14 malformed FLO425_GEOMETRY_GATE_UNTIL -> fail-open"
    o = _install_patches(current_price=4700.0, until="not-a-timestamp")
    try:
        plan = _Plan("PLAN-T14", "BUY", 4900.0)
        errors = _run_gate(plan)
        assert not errors, f"malformed until should fail-open: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t15_plan007_fixture():
    name = "T15 PLAN-007 fixture (BUY 4756 vs 4734.49 = +215p) -> BLOCKED"
    o = _install_patches(current_price=4734.49)
    try:
        plan = _Plan("PLAN-20260507-007", "BUY", 4756.0,
                     setup_type="continuation_momentum")
        errors = _run_gate(plan)
        assert errors, "PLAN-007 must be blocked"
        assert "+215.1" in errors[0] or "215.1" in errors[0], \
            f"distance not in error: {errors[0]}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t16_plan005_fixture():
    name = "T16 PLAN-005 fixture (BUY 4736 vs 4751.42 = -154p) -> ALLOWED"
    o = _install_patches(current_price=4751.42)
    try:
        plan = _Plan("PLAN-20260507-005", "BUY", 4736.0,
                     setup_type="pullback_trend")
        errors = _run_gate(plan)
        assert not errors, f"PLAN-005 (pullback) must be allowed: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t17_plan004_fixture():
    name = "T17 PLAN-004 fixture (BUY 4751 vs 4751.42 = -4p) -> ALLOWED (documented)"
    o = _install_patches(current_price=4751.42)
    try:
        plan = _Plan("PLAN-20260507-004", "BUY", 4751.0,
                     setup_type="continuation_momentum")
        errors = _run_gate(plan)
        # Documented limitation: at-current entries pass the geometry gate.
        # Acceptance semantics (FLO-425 §17) is the right defense for that
        # class, not this gate.
        assert not errors, f"PLAN-004 (at-current) is allowed by design: {errors}"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t18_rejection_error_format():
    name = "T18 rejection error includes all required fields"
    o = _install_patches(current_price=4700.0)
    try:
        plan = _Plan("PLAN-T18", "BUY", 4760.0,
                     setup_type="continuation_momentum")
        errors = _run_gate(plan)
        assert errors, "expected rejection"
        msg = errors[0]
        for required in ("setup_type", "direction", "entry_price",
                         "current_price", "distance_pips", "threshold_pips",
                         "gate_active_until", "BUY", "continuation_momentum"):
            assert required in msg, f"missing {required!r} in rejection msg"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


def t19_setup_type_independence():
    name = "T19 setup_type independence: chase blocked under all 4 setup_types"
    o = _install_patches(current_price=4700.0)
    try:
        for st in ("breakout_range", "continuation_momentum",
                   "pullback_trend", "structural_bounce"):
            plan = _Plan(f"PLAN-T19-{st}", "BUY", 4760.0, setup_type=st)
            errors = _run_gate(plan)
            assert errors, f"chase under setup_type={st} should be blocked"
        _ok(name)
    except Exception as e:
        _fail(name, e)
    finally:
        _uninstall_patches(o)


# ---------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------
def main() -> int:
    print("FLO-425 §16f geometry gate tests")
    print("=" * 60)
    for fn in [t1_buy_chase_blocked, t2_buy_at_current_allowed,
               t3_buy_below_current_allowed, t4_sell_chase_blocked,
               t5_sell_at_current_allowed, t6_sell_above_current_allowed,
               t7_boundary_50p_allowed, t8_boundary_51p_blocked,
               t9_env_threshold_override, t10_env_disabled,
               t11_timebox_active, t12_timebox_expired,
               t13_mt5_fetch_fails, t14_malformed_until,
               t15_plan007_fixture, t16_plan005_fixture, t17_plan004_fixture,
               t18_rejection_error_format, t19_setup_type_independence]:
        try:
            fn()
        except Exception as e:
            _fail(fn.__name__, e)
    print("=" * 60)
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} test(s) -- {_FAILURES}")
        return 1
    print("ALL PASS (19/19)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
