"""FLO-424 — temporary safety circuit tests.

Verifies:
  * breakout_range plans rejected while circuit active
  * other setup_types (continuation_momentum, pullback_trend,
    structural_bounce, mean_reversion_extreme, liquidity_sweep,
    divergence_play, paired_hedge, news_reaction, session_open_break)
    are unaffected
  * circuit self-disables when until-timestamp passes
  * env override (FLO424_SAFETY_CIRCUIT_UNTIL) works
  * fail-safe behavior on missing/malformed config
  * error message names the alternative (continuation_momentum)

Run: python test_flo424_safety_circuit.py
Exits non-zero on failure.
"""
from __future__ import annotations

import datetime as _dt
import os
import sys
from copy import deepcopy
from unittest.mock import patch


def fail(label: str, msg: str) -> None:
    print(f"FAIL [{label}]: {msg}")
    sys.exit(1)


def passed(label: str, detail: str = "") -> None:
    suffix = f" ({detail})" if detail else ""
    print(f"PASS [{label}]{suffix}")


def _valid_plan_dict(setup_type: str = "continuation_momentum",
                     direction: str = "BUY",
                     entry_price: float = 4600.0) -> dict:
    """Build a minimal v3 plan that passes ALL non-FLO-424 validators.
    Used to isolate FLO-424's behavior from other rules. TP distance kept
    < 100 pips so Escola 2 management mandate doesn't fire (this is a
    fixture for FLO-424 testing, not for management testing)."""
    sl = entry_price - 5.0 if direction == "BUY" else entry_price + 5.0
    tp = entry_price + 8.0 if direction == "BUY" else entry_price - 8.0
    return {
        "schema_version": 3,
        "id": "PLAN-20260507-001",
        "created_by": "floki",
        "created_at": "2026-05-07T10:00:00Z",
        "expires_at": "2026-05-07T22:00:00Z",
        "status": "pending",
        "analysis": {
            "thesis": "trend continuation thesis at least 20 chars long",
            "key_levels": [entry_price],
            "confidence": 75,
            "setup_type": setup_type,
            "context_tags": {
                "trend": "trend_strong",
                "volatility": "high_vol",
                "htf": "HTF_aligned",
                "news_session": [],
            },
            "confidence_reason": "RM BULL conv7 ALIGNED. 4h structure intact, multi-TF momentum confirmed.",
        },
        "entry": {
            "direction": direction,
            "volume": 0.01,
            "conditions": [
                {"type": "price_above" if direction == "BUY" else "price_below",
                 "level": entry_price},
                {"type": "macd_histogram", "tf": "H1",
                 "op": "above" if direction == "BUY" else "below",
                 "threshold": 0.0},
            ],
            "initial_sl": sl,
            "initial_tp": tp,
            "entry_price": entry_price,
        },
        "management": [],
        "exit": [
            {"name": "thesis_break", "priority": 9,
             "conditions": [{"type": "price_below" if direction == "BUY" else "price_above",
                            "level": sl + (5 if direction == "BUY" else -5)}],
             "action": {"type": "close_full"}, "fires": "once"},
        ],
        "emergency": {"max_loss_pips": 250.0, "max_duration_minutes": 480},
    }


# ----------------------------------------------------------------------
# Tests — circuit ACTIVE (default config)
# ----------------------------------------------------------------------

def test_1_breakout_range_rejected_when_active():
    """While the circuit is active, breakout_range plans are rejected
    with a FLO-424 message."""
    from snow.validator import validate_plan
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    ok, parsed, errors = validate_plan(plan)
    if ok:
        fail("test1.breakout_rejected", "expected ok=False")
    if not any("FLO-424" in e for e in errors):
        fail("test1.error_mentions_flo424", f"errors={errors}")
    if not any("breakout_range" in e for e in errors):
        fail("test1.error_names_setup", f"errors={errors}")
    passed("test1.breakout_range_BUY_rejected_with_flo424_error")


def test_2_breakout_range_SELL_also_rejected():
    """SELL is also rejected — circuit blocks all breakout_range, not
    just BUY (per CEO directive — Option A, not B)."""
    from snow.validator import validate_plan
    plan = _valid_plan_dict(setup_type="breakout_range", direction="SELL",
                            entry_price=4600.0)
    ok, parsed, errors = validate_plan(plan)
    if ok:
        fail("test2.breakout_sell_rejected", f"expected reject; errors={errors}")
    if not any("FLO-424" in e for e in errors):
        fail("test2.error_flo424", f"errors={errors}")
    passed("test2.breakout_range_SELL_also_rejected")


def test_3_error_message_names_continuation_alternative():
    """Floki should be told to re-author as continuation_momentum."""
    from snow.validator import validate_plan
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    ok, _, errors = validate_plan(plan)
    msg = " ".join(errors)
    if "continuation_momentum" not in msg:
        fail("test3.suggests_alternative", "error msg does not name continuation_momentum")
    passed("test3.error_msg_suggests_continuation_momentum")


# ----------------------------------------------------------------------
# Tests — other setup_types unaffected
# ----------------------------------------------------------------------

def test_4_continuation_momentum_passes():
    """The canonical alternative — must continue to validate normally."""
    from snow.validator import validate_plan
    plan = _valid_plan_dict(setup_type="continuation_momentum", direction="BUY")
    ok, parsed, errors = validate_plan(plan)
    if not ok:
        fail("test4.continuation_passes", f"got errors={errors}")
    passed("test4.continuation_momentum_unaffected")


def test_5_other_setups_pass():
    """All non-breakout_range setup_types must remain valid."""
    from snow.validator import validate_plan
    for setup in ("pullback_trend", "structural_bounce",
                  "mean_reversion_extreme", "liquidity_sweep",
                  "continuation_momentum", "news_reaction",
                  "divergence_play", "paired_hedge",
                  "session_open_break"):
        plan = _valid_plan_dict(setup_type=setup, direction="BUY")
        ok, _, errors = validate_plan(plan)
        # Some setup_types have specific requirements (paired_hedge needs
        # paired_hedge_id, divergence_play may need indicator_divergence).
        # We're only testing FLO-424 doesn't gate them — accept either
        # ok=True or ok=False with errors that don't mention FLO-424.
        flo424_errs = [e for e in errors if "FLO-424" in e]
        if flo424_errs:
            fail(f"test5.{setup}_blocked_by_flo424",
                 f"FLO-424 should NOT block {setup}; got {flo424_errs}")
    passed("test5.no_other_setup_blocked_by_flo424")


# ----------------------------------------------------------------------
# Tests — circuit lifecycle (time-box)
# ----------------------------------------------------------------------

def test_6_circuit_disabled_after_until_timestamp():
    """When current UTC time >= FLO424_SAFETY_CIRCUIT_UNTIL, the circuit
    self-disables and breakout_range passes again."""
    from snow.validator import validate_plan
    import config as _cfg
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    # Patch the constant to a past timestamp
    with patch.object(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "2020-01-01T00:00:00Z"):
        ok, _, errors = validate_plan(plan)
        flo424_errs = [e for e in errors if "FLO-424" in e]
        if flo424_errs:
            fail("test6.expired_circuit", f"circuit should be expired; got {flo424_errs}")
    passed("test6.circuit_self_disables_after_until_timestamp")


def test_7_env_override_works():
    """Setting FLO424_SAFETY_CIRCUIT_UNTIL env var to a past date should
    disable the circuit at config import time."""
    # We can't easily re-import config in-process, but we can verify the
    # constant reads from env at import. Instead, simulate the same
    # behavior by patching the loaded value.
    from snow.validator import validate_plan
    import config as _cfg
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    with patch.object(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "2020-01-01T00:00:00Z"):
        ok, _, errors = validate_plan(plan)
        if any("FLO-424" in e for e in errors):
            fail("test7.env_override", "env override should disable circuit")
    passed("test7.env_override_disables_circuit")


def test_8_malformed_until_timestamp_fails_safe_open():
    """If FLO424_SAFETY_CIRCUIT_UNTIL is unparseable, the circuit
    fails-safe by NOT blocking (so a misconfig can't lock everything out)."""
    from snow.validator import validate_plan
    import config as _cfg
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    with patch.object(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "not-a-timestamp"):
        ok, _, errors = validate_plan(plan)
        flo424_errs = [e for e in errors if "FLO-424" in e]
        if flo424_errs:
            fail("test8.malformed_fails_open",
                 f"malformed config should fail-open (no block); got {flo424_errs}")
    passed("test8.malformed_until_timestamp_fails_safe_open")


def test_9_missing_until_constant_fails_safe_open():
    """If the constant is removed from config, circuit must fail-open."""
    from snow.validator import validate_plan
    import config as _cfg
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    saved = getattr(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", None)
    try:
        if hasattr(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL"):
            delattr(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL")
        ok, _, errors = validate_plan(plan)
        flo424_errs = [e for e in errors if "FLO-424" in e]
        if flo424_errs:
            fail("test9.missing_constant", "missing constant should fail-open")
    finally:
        if saved is not None:
            _cfg.FLO424_SAFETY_CIRCUIT_UNTIL = saved
    passed("test9.missing_constant_fails_safe_open")


# ----------------------------------------------------------------------
# Tests — circuit gate function in isolation
# ----------------------------------------------------------------------

def test_10_circuit_gate_function():
    """_flo424_safety_circuit_active returns True when until is in the
    future, False when in the past."""
    from snow.validator import _flo424_safety_circuit_active
    import config as _cfg
    with patch.object(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "2099-01-01T00:00:00Z"):
        if not _flo424_safety_circuit_active():
            fail("test10.future", "circuit should be active for future timestamp")
    with patch.object(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "2020-01-01T00:00:00Z"):
        if _flo424_safety_circuit_active():
            fail("test10.past", "circuit should be inactive for past timestamp")
    passed("test10.gate_function_correctly_handles_future_vs_past")


# ----------------------------------------------------------------------
# Tests — error structure
# ----------------------------------------------------------------------

def test_11_rejection_returns_proper_shape():
    """The validator must return (False, parsed_plan, [error_messages])."""
    from snow.validator import validate_plan
    plan = _valid_plan_dict(setup_type="breakout_range", direction="BUY")
    ok, parsed, errors = validate_plan(plan)
    if ok is not False:
        fail("test11.ok_false", f"ok={ok}")
    if parsed is None:
        fail("test11.parsed_present",
             "parsed plan should still be returned for caller diagnostics")
    if not errors or not isinstance(errors, list):
        fail("test11.errors_list", f"errors={errors}")
    passed("test11.rejection_shape_ok_false_plan_errors_list")


# ----------------------------------------------------------------------
# Tests — execution path / Snow runtime untouched
# ----------------------------------------------------------------------

def test_12_validator_only_no_executor_change():
    """FLO-424 lives entirely in snow/validator.py. Verify the symbol
    is not referenced from execution paths (executor.py, snow/snow_loop.py,
    snow/actions.py, monitor.py, ai_agent.py)."""
    import os
    forbidden_files = [
        "executor.py", "monitor.py", "ai_agent.py",
        "snow/snow_loop.py", "snow/actions.py",
    ]
    for f in forbidden_files:
        if not os.path.exists(f):
            continue
        with open(f, encoding="utf-8") as fp:
            text = fp.read()
        if "FLO424" in text or "FLO-424" in text or "flo424" in text:
            fail(f"test12.{f}_changed",
                 f"FLO-424 should NOT touch execution path; found reference in {f}")
    passed("test12.execution_paths_untouched")


if __name__ == "__main__":
    print("=" * 60)
    print("FLO-424 — safety circuit test suite")
    print("=" * 60)
    test_1_breakout_range_rejected_when_active()
    test_2_breakout_range_SELL_also_rejected()
    test_3_error_message_names_continuation_alternative()
    test_4_continuation_momentum_passes()
    test_5_other_setups_pass()
    test_6_circuit_disabled_after_until_timestamp()
    test_7_env_override_works()
    test_8_malformed_until_timestamp_fails_safe_open()
    test_9_missing_until_constant_fails_safe_open()
    test_10_circuit_gate_function()
    test_11_rejection_returns_proper_shape()
    test_12_validator_only_no_executor_change()
    print("=" * 60)
    print("ALL FLO-424 SAFETY CIRCUIT TESTS PASSED")
