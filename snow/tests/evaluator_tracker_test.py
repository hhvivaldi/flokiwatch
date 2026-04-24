"""PerPlanTracker tests — Phase 3b stateful foundation.

Covers:
  * seed / has / forget lifecycle (+ idempotence)
  * profit_pips sign convention (BUY + SELL, both directions of P&L)
  * MFE = positive magnitude, never negative
  * MAE = positive magnitude representing drawdown
  * profit_retraced_from_peak — zero when peak never positive (advisor #3)
  * update_price advances MFE/MAE/peak atomically
  * Pre-seed queries return None (not 0.0) — distinguishes "no data" from
    "zero loss"
  * Thread-safety: concurrent update_price + queries
"""
from __future__ import annotations

import threading

import pytest

from snow.evaluators.context import PIP_SIZE
from snow.evaluators.tracker import PerPlanTracker
from snow.schema import Direction


# =============================================================================
# Lifecycle
# =============================================================================

class TestLifecycle:

    def test_seed_then_has(self, tracker):
        assert tracker.has("P1") is False
        tracker.seed("P1", 4700.0, Direction.BUY)
        assert tracker.has("P1") is True

    def test_forget_removes_state(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.forget("P1")
        assert tracker.has("P1") is False

    def test_forget_unknown_id_is_noop(self, tracker):
        tracker.forget("NEVER_SEEDED")  # must not raise

    def test_re_seed_overwrites(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4710.0)  # MFE = 100
        tracker.seed("P1", 4800.0, Direction.SELL)
        assert tracker.mfe_pips("P1") == 0.0  # reset


# =============================================================================
# Pre-seed queries — None, not 0.0
# =============================================================================

class TestPreSeedReturnsNone:

    def test_profit_pips_unseeded_is_none(self, tracker):
        assert tracker.profit_pips("P1", 4700.0) is None

    def test_mfe_unseeded_is_none(self, tracker):
        assert tracker.mfe_pips("P1") is None

    def test_mae_unseeded_is_none(self, tracker):
        assert tracker.mae_pips("P1") is None

    def test_retrace_unseeded_is_none(self, tracker):
        assert tracker.retrace_from_peak("P1", 4700.0) is None


# =============================================================================
# profit_pips — signed convention
# =============================================================================

class TestProfitPipsSign:

    def test_buy_winning(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        # +10 price units = +100 pips at PIP_SIZE=0.1
        assert tracker.profit_pips("P1", 4710.0) == pytest.approx(100.0)

    def test_buy_losing(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        assert tracker.profit_pips("P1", 4695.0) == pytest.approx(-50.0)

    def test_sell_winning(self, tracker):
        tracker.seed("P1", 4700.0, Direction.SELL)
        assert tracker.profit_pips("P1", 4690.0) == pytest.approx(100.0)

    def test_sell_losing(self, tracker):
        tracker.seed("P1", 4700.0, Direction.SELL)
        assert tracker.profit_pips("P1", 4712.0) == pytest.approx(-120.0)


# =============================================================================
# MFE — positive magnitude only
# =============================================================================

class TestMFE:

    def test_mfe_starts_at_zero(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        assert tracker.mfe_pips("P1") == 0.0

    def test_mfe_only_tracks_favourable(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4695.0)   # adverse
        assert tracker.mfe_pips("P1") == 0.0  # no favourable yet
        tracker.update_price("P1", 4708.0)   # +80 pips
        assert tracker.mfe_pips("P1") == pytest.approx(80.0)
        tracker.update_price("P1", 4703.0)   # pulled back
        assert tracker.mfe_pips("P1") == pytest.approx(80.0)  # sticky peak

    def test_mfe_sell_direction(self, tracker):
        tracker.seed("P1", 4700.0, Direction.SELL)
        tracker.update_price("P1", 4690.0)   # favourable for SELL
        assert tracker.mfe_pips("P1") == pytest.approx(100.0)


# =============================================================================
# MAE — positive magnitude (drawdown)
# =============================================================================

class TestMAE:

    def test_mae_starts_at_zero(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        assert tracker.mae_pips("P1") == 0.0

    def test_mae_is_positive_drawdown(self, tracker):
        """BUY at 4700, dips to 4690 → MAE = 100 pips (NOT -100)."""
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4690.0)
        assert tracker.mae_pips("P1") == pytest.approx(100.0)

    def test_mae_sticky_peak(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4690.0)   # MAE = 100
        tracker.update_price("P1", 4695.0)   # recovered a bit
        assert tracker.mae_pips("P1") == pytest.approx(100.0)
        tracker.update_price("P1", 4688.0)   # new low
        assert tracker.mae_pips("P1") == pytest.approx(120.0)

    def test_winning_trade_mae_stays_zero(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4710.0)  # only favourable
        assert tracker.mae_pips("P1") == 0.0


# =============================================================================
# retrace_from_peak — advisor item #3
# =============================================================================

class TestRetraceFromPeak:

    def test_retrace_zero_when_never_in_profit(self, tracker):
        """Trades that never went positive have peak=0 → retrace=0."""
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4695.0)   # losing
        tracker.update_price("P1", 4690.0)   # more losing
        # Never reached profit → retracement is 0 for ANY threshold
        assert tracker.retrace_from_peak("P1", 4685.0) == 0.0
        assert tracker.retrace_from_peak("P1", 4700.0) == 0.0

    def test_retrace_after_peak(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4710.0)   # peak = 100 pips
        # Current price 4705 → current_profit = 50, retrace = 50
        assert tracker.retrace_from_peak("P1", 4705.0) == pytest.approx(50.0)

    def test_retrace_floored_at_zero_on_new_high(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4710.0)   # peak = 100
        # Current price 4712 → new high; retrace would be negative → 0
        assert tracker.retrace_from_peak("P1", 4712.0) == 0.0


# =============================================================================
# update_price advances MFE/MAE/peak together
# =============================================================================

class TestUpdatePrice:

    def test_single_update_advances_all_three_metrics(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        tracker.update_price("P1", 4715.0)  # +150 pips
        assert tracker.mfe_pips("P1") == pytest.approx(150.0)
        assert tracker.mae_pips("P1") == 0.0
        assert tracker.retrace_from_peak("P1", 4715.0) == 0.0

    def test_update_on_unseeded_plan_is_silent(self, tracker):
        tracker.update_price("UNKNOWN", 4700.0)  # no raise
        assert tracker.has("UNKNOWN") is False


# =============================================================================
# Thread-safety
# =============================================================================

class TestThreadSafety:

    def test_concurrent_updates_and_queries(self, tracker):
        tracker.seed("P1", 4700.0, Direction.BUY)
        errors: list[BaseException] = []

        def writer():
            try:
                for i in range(500):
                    tracker.update_price("P1", 4700.0 + (i % 20) * 0.5)
            except BaseException as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(500):
                    _ = tracker.profit_pips("P1", 4703.0)
                    _ = tracker.mfe_pips("P1")
                    _ = tracker.mae_pips("P1")
                    _ = tracker.retrace_from_peak("P1", 4703.0)
            except BaseException as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors

    def test_seed_and_forget_during_reads(self, tracker):
        """Racing seed/forget against queries must never raise."""
        errors: list[BaseException] = []

        def toggler():
            try:
                for _ in range(200):
                    tracker.seed("P1", 4700.0, Direction.BUY)
                    tracker.forget("P1")
            except BaseException as e:
                errors.append(e)

        def reader():
            try:
                for _ in range(200):
                    _ = tracker.profit_pips("P1", 4700.0)  # may be None
            except BaseException as e:
                errors.append(e)

        threads = [
            threading.Thread(target=toggler),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert not errors
