"""FLO-427 — Counter-trend regime gate tests.

Eight cases covering:
  - Trending regime + counter-trend direction → REJECT with regime_gate: prefix
  - Trending regime + aligned direction → ACCEPT
  - Non-trending regime → ACCEPT regardless of direction
  - Sub-threshold ADX → ACCEPT (gate inactive)
  - Sub-threshold confidence → ACCEPT (gate inactive)
  - Missing snapshot → ACCEPT + DEGRADED log
  - UNKNOWN regime → ACCEPT + DEGRADED log
"""

from __future__ import annotations

from copy import deepcopy

import pytest

from snow.validator import validate_plan


def _set_direction(plan_dict: dict, direction: str) -> dict:
    """Adjust SL/TP placement so the plan still passes geometry checks
    after flipping direction. The fixture starts SELL; for BUY we need
    initial_sl above entry and initial_tp below — wait, that's backwards.
    BUY: entry below SL means short setup; correct is entry < TP, entry > SL.
    SELL: entry > SL, entry < TP. Flip TPs and SLs by negating the offsets.
    """
    plan = deepcopy(plan_dict)
    entry = plan["entry"]
    entry_price = entry.get("entry_price", 4730.0)
    if direction == "BUY":
        entry["direction"] = "BUY"
        entry["initial_sl"] = entry_price - 15.0
        entry["initial_tp"] = entry_price + 30.0
    else:
        entry["direction"] = "SELL"
        entry["initial_sl"] = entry_price + 15.0
        entry["initial_tp"] = entry_price - 30.0
    return plan


def _strong_trending_bull():
    return {"regime": "TRENDING_BULLISH", "confidence": "high", "adx": 30.0}


def _strong_trending_bear():
    return {"regime": "TRENDING_BEARISH", "confidence": "strong", "adx": 28.5}


def _ranging():
    return {"regime": "RANGING", "confidence": "high", "adx": 18.0}


class TestRegimeCounterTrendGate:

    def test_trending_bull_sell_blocked(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "SELL")
        ok, _, errors = validate_plan(plan, author_regime=_strong_trending_bull())
        assert ok is False
        assert any(e.startswith("regime_gate:") for e in errors), (
            f"expected regime_gate: error, got: {errors}"
        )
        msg = next(e for e in errors if e.startswith("regime_gate:"))
        assert "TRENDING_BULLISH" in msg
        assert "SELL" in msg
        assert "FLO-427" in msg

    def test_trending_bull_buy_allowed(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "BUY")
        ok, _, errors = validate_plan(plan, author_regime=_strong_trending_bull())
        # May still fail on other validators, but NOT on regime_gate.
        assert not any(e.startswith("regime_gate:") for e in errors), errors

    def test_trending_bear_buy_blocked(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "BUY")
        ok, _, errors = validate_plan(plan, author_regime=_strong_trending_bear())
        assert ok is False
        assert any(e.startswith("regime_gate:") for e in errors), errors

    def test_ranging_sell_allowed(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "SELL")
        ok, _, errors = validate_plan(plan, author_regime=_ranging())
        assert not any(e.startswith("regime_gate:") for e in errors), errors

    def test_trending_low_adx_allowed(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "SELL")
        snap = {"regime": "TRENDING_BULLISH", "confidence": "high", "adx": 18.0}
        ok, _, errors = validate_plan(plan, author_regime=snap)
        assert not any(e.startswith("regime_gate:") for e in errors), errors

    def test_trending_weak_confidence_allowed(self, valid_plan_dict):
        plan = _set_direction(valid_plan_dict, "SELL")
        snap = {"regime": "TRENDING_BULLISH", "confidence": "weak", "adx": 30.0}
        ok, _, errors = validate_plan(plan, author_regime=snap)
        assert not any(e.startswith("regime_gate:") for e in errors), errors

    def test_missing_snapshot_allowed(self, valid_plan_dict, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="snow.validator")
        plan = _set_direction(valid_plan_dict, "SELL")
        ok, _, errors = validate_plan(plan, author_regime=None)
        assert not any(e.startswith("regime_gate:") for e in errors), errors
        assert any("REGIME_GATE_DEGRADED" in r.message for r in caplog.records), (
            f"expected REGIME_GATE_DEGRADED log, got: {[r.message for r in caplog.records]}"
        )

    def test_regime_unknown_allowed(self, valid_plan_dict, caplog):
        import logging
        caplog.set_level(logging.WARNING, logger="snow.validator")
        plan = _set_direction(valid_plan_dict, "SELL")
        snap = {"regime": "UNKNOWN", "confidence": "high", "adx": 30.0}
        ok, _, errors = validate_plan(plan, author_regime=snap)
        assert not any(e.startswith("regime_gate:") for e in errors), errors
        assert any("REGIME_GATE_DEGRADED" in r.message for r in caplog.records)


class TestPairedHedgeRemoved:

    def test_paired_hedge_rejected_by_pydantic(self, valid_plan_dict):
        """FLO-427 — paired_hedge removed from SetupType."""
        plan = deepcopy(valid_plan_dict)
        plan["analysis"]["setup_type"] = "paired_hedge"
        ok, parsed, errors = validate_plan(plan)
        assert ok is False
        # Pydantic enum-violation surfaces as schema: error
        assert any("setup_type" in e for e in errors), errors
