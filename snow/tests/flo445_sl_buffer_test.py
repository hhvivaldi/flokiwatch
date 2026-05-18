"""FLO-445 — SL buffer from structural levels (sweep envelope guard)."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

import snow.validator as validator_mod
from snow.validator import validate_plan


@pytest.fixture
def patch_active_plans(monkeypatch):
    import snow.db as snow_db_mod
    monkeypatch.setattr(snow_db_mod, "get_active_plans", lambda: [])


@pytest.fixture
def patch_m15_atr(monkeypatch):
    holder = {"atr": 50.0}  # default — at this ATR, buffer = max(20, 50) = 50p

    def _fake_fetch():
        return holder["atr"]

    monkeypatch.setattr(validator_mod, "_fetch_m15_atr_pips", _fake_fetch)
    return holder


def _sell_at(plan: dict[str, Any], *, entry: float, sl: float, tp: float,
             key_levels: list[float]) -> dict[str, Any]:
    out = deepcopy(plan)
    out["entry"]["direction"] = "SELL"
    out["entry"]["entry_price"] = entry
    out["entry"]["initial_sl"] = sl
    out["entry"]["initial_tp"] = tp
    out["entry"]["conditions"] = [
        {"type": "price_above", "level": entry - 2},
        {"type": "rsi", "tf": "H1", "op": "above", "threshold": 50},
    ]
    out["analysis"]["key_levels"] = key_levels
    return out


def _buy_at(plan: dict[str, Any], *, entry: float, sl: float, tp: float,
            key_levels: list[float]) -> dict[str, Any]:
    out = deepcopy(plan)
    out["entry"]["direction"] = "BUY"
    out["entry"]["entry_price"] = entry
    out["entry"]["initial_sl"] = sl
    out["entry"]["initial_tp"] = tp
    out["entry"]["conditions"] = [
        {"type": "price_above", "level": entry + 2},
        {"type": "rsi", "tf": "H1", "op": "below", "threshold": 30},
    ]
    out["analysis"]["key_levels"] = key_levels
    return out


class TestSLBufferGate:

    def test_plan001_reproduction_rejected(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """PLAN-20260518-001 verbatim: SELL entry 4554, SL 4582,
        key_levels [4554, 4582, 4485]. SL exactly on the 4582
        structural level → 0p buffer → reject."""
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4582.0, tp=4485.0,
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        sl_errs = [e for e in errors if e.startswith("sl_buffer:")]
        assert sl_errs, errors
        assert "FLO-445" in sl_errs[0]
        assert "4582" in sl_errs[0]
        # Suggested SL = 4582 + 50p = 4587
        assert "4587" in sl_errs[0]

    def test_sell_with_adequate_buffer_passes(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """SELL entry 4554, structural top 4582, SL 4640 → 580p buffer
        (well over the 50p requirement). Pass."""
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4640.0, tp=4485.0,
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("sl_buffer:") for e in errors), errors

    def test_sell_buffer_exactly_at_threshold_passes(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """Buffer = exactly 50p → allow (≥, not >)."""
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4587.0, tp=4485.0,  # 4582 + 50p = 4587
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("sl_buffer:") for e in errors), errors

    def test_sell_buffer_just_under_threshold_rejected(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """Buffer = 30p with M15 ATR 50p → reject (need ≥ 50p)."""
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4585.0, tp=4485.0,  # 4582 + 30p = 4585
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any(e.startswith("sl_buffer:") for e in errors), errors

    def test_buy_zero_buffer_rejected(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """Mirror case: BUY entry 4554, SL 4530, key_levels [4530, 4570]
        → SL exactly on 4530 support → reject."""
        plan = _buy_at(
            valid_plan_dict,
            entry=4554.0, sl=4530.0, tp=4600.0,
            key_levels=[4530.0, 4570.0],
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        sl_errs = [e for e in errors if e.startswith("sl_buffer:")]
        assert sl_errs, errors
        # Suggested SL = 4530 - 50p = 4525
        assert "4525" in sl_errs[0]

    def test_20p_floor_applies_in_low_volatility(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """M15 ATR 12p → buffer floor 20p applies.

        XAUUSD: 1 USD = 10 pips, so 20p buffer = 2.0 USD price gap.
        """
        patch_m15_atr["atr"] = 12.0
        # gap = 15p (1.5 USD), under the 20p floor → reject
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4583.5, tp=4485.0,
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert ok is False
        assert any(e.startswith("sl_buffer:") for e in errors), errors

        # gap = 20p (2.0 USD), exactly at floor → pass
        plan2 = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4584.0, tp=4485.0,
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok2, _, errors2 = validate_plan(plan2)
        assert not any(e.startswith("sl_buffer:") for e in errors2), errors2

    def test_no_key_levels_fails_open(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4582.0, tp=4485.0,
            key_levels=[],
        )
        # Schema requires non-empty key_levels (FLO-366 tagging), so a
        # truly empty list may fail Pydantic earlier. Skip if so.
        try:
            ok, _, errors = validate_plan(plan)
        except Exception:
            pytest.skip("schema rejects empty key_levels — gate path unreachable")
        # Either way, gate should not contribute a sl_buffer error.
        assert not any(e.startswith("sl_buffer:") for e in errors), errors

    def test_key_levels_without_sl_side_level_passes(
        self, valid_plan_dict, patch_active_plans, patch_m15_atr
    ):
        """SELL plan with key_levels only BELOW entry (supports + TP).
        No structural level between entry and SL → nothing to check."""
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4582.0, tp=4485.0,
            key_levels=[4543.0, 4520.0, 4485.0],  # all below 4554 entry
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("sl_buffer:") for e in errors), errors

    def test_m15_atr_unavailable_fails_open(
        self, valid_plan_dict, monkeypatch, patch_active_plans, caplog
    ):
        import logging
        monkeypatch.setattr(validator_mod, "_fetch_m15_atr_pips", lambda: None)
        caplog.set_level(logging.WARNING)
        plan = _sell_at(
            valid_plan_dict,
            entry=4554.0, sl=4582.0, tp=4485.0,
            key_levels=[4554.0, 4582.0, 4485.0],
        )
        ok, _, errors = validate_plan(plan)
        assert not any(e.startswith("sl_buffer:") for e in errors), errors
        assert any(
            "SL_BUFFER_DEGRADED" in r.message for r in caplog.records
        )
