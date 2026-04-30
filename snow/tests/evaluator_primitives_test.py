"""Primitive evaluator tests — 10 pure primitives (all non-position-state).

Layout:
  TestPriceAboveBelow
  TestRSI
  TestMACDHistogram
  TestEMARelation
  TestATR
  TestPriceAtSRZone
  TestPriceAtFibonacci
  TestDurationExceeds
  TestTimeBetween

Each class tests: happy path (True), negation (False), missing data → False,
and relevant boundary cases. Position-state primitives live in their own
file because they share the tracker-seeding preamble.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snow.evaluators.context import PIP_SIZE
from snow.evaluators.dispatch import evaluate_condition
from snow.schema import (
    ATR,
    DurationExceeds,
    EMARelation,
    MACDHistogram,
    PriceAbove,
    PriceAtFibonacci,
    PriceAtSRZone,
    PriceBelow,
    RSI,
    TimeBetween,
)


# =============================================================================
# price_above / price_below
# =============================================================================

class TestPriceAboveBelow:

    def test_price_above_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4735.0))
        assert evaluate_condition(PriceAbove(level=4730.0), ctx) is True

    def test_price_above_false_at_exact_level(self, eval_ctx, fake_live):
        # Strict > — equality is False
        ctx = eval_ctx(live_data=fake_live(price_mid=4730.0))
        assert evaluate_condition(PriceAbove(level=4730.0), ctx) is False

    def test_price_above_false_below(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4700.0))
        assert evaluate_condition(PriceAbove(level=4730.0), ctx) is False

    def test_price_above_missing_price_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=None))
        assert evaluate_condition(PriceAbove(level=4730.0), ctx) is False

    def test_price_below_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4700.0))
        assert evaluate_condition(PriceBelow(level=4730.0), ctx) is True

    def test_price_below_false_at_exact(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4730.0))
        assert evaluate_condition(PriceBelow(level=4730.0), ctx) is False

    def test_price_below_missing_price_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=None))
        assert evaluate_condition(PriceBelow(level=4730.0), ctx) is False


# =============================================================================
# rsi
# =============================================================================

class TestRSI:

    def test_above_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(rsi_by_tf={"H1": 72.0}))
        assert evaluate_condition(
            RSI(tf="H1", op="above", threshold=70), ctx
        ) is True

    def test_above_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(rsi_by_tf={"H1": 65.0}))
        assert evaluate_condition(
            RSI(tf="H1", op="above", threshold=70), ctx
        ) is False

    def test_above_false_at_threshold(self, eval_ctx, fake_live):
        # strict >
        ctx = eval_ctx(live_data=fake_live(rsi_by_tf={"H1": 70.0}))
        assert evaluate_condition(
            RSI(tf="H1", op="above", threshold=70), ctx
        ) is False

    def test_below_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(rsi_by_tf={"M5": 28.0}))
        assert evaluate_condition(
            RSI(tf="M5", op="below", threshold=30), ctx
        ) is True

    def test_missing_rsi_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(rsi_by_tf={}))
        assert evaluate_condition(
            RSI(tf="H1", op="above", threshold=70), ctx
        ) is False


# =============================================================================
# macd_histogram
# =============================================================================

class TestMACDHistogram:

    def test_above_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(macd_hist_by_tf={"M5": 0.5}))
        assert evaluate_condition(
            MACDHistogram(tf="M5", op="above", threshold=0.0), ctx
        ) is True

    def test_below_true_negative_hist(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(macd_hist_by_tf={"H1": -0.25}))
        assert evaluate_condition(
            MACDHistogram(tf="H1", op="below", threshold=0.0), ctx
        ) is True

    def test_missing_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(macd_hist_by_tf={}))
        assert evaluate_condition(
            MACDHistogram(tf="M1", op="above", threshold=0), ctx
        ) is False


# =============================================================================
# ema_relation
# =============================================================================

class TestEMARelation:

    def test_price_above_ema(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(
            price_mid=4710.0,
            ema_by_key={("H1", 50): 4700.0},
        ))
        assert evaluate_condition(
            EMARelation(tf="H1", period=50, relation="price_above"), ctx
        ) is True

    def test_price_below_ema(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(
            price_mid=4690.0,
            ema_by_key={("H1", 50): 4700.0},
        ))
        assert evaluate_condition(
            EMARelation(tf="H1", period=50, relation="price_below"), ctx
        ) is True

    def test_aligned_bull(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(ema_by_key={
            ("H1", 9):   4720.0,
            ("H1", 21):  4715.0,
            ("H1", 50):  4710.0,
            ("H1", 200): 4700.0,
        }))
        # FLO-404 follow-up: period must be omitted for aligned_*
        # (evaluator reads all 4 EMAs regardless of any period value).
        assert evaluate_condition(
            EMARelation(tf="H1", relation="aligned_bull"), ctx
        ) is True

    def test_aligned_bull_broken(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(ema_by_key={
            ("H1", 9):   4720.0,
            ("H1", 21):  4715.0,
            ("H1", 50):  4710.0,
            ("H1", 200): 4712.0,   # inversion
        }))
        assert evaluate_condition(
            EMARelation(tf="H1", relation="aligned_bull"), ctx
        ) is False

    def test_aligned_bear(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(ema_by_key={
            ("H1", 9):   4700.0,
            ("H1", 21):  4710.0,
            ("H1", 50):  4715.0,
            ("H1", 200): 4720.0,
        }))
        assert evaluate_condition(
            EMARelation(tf="H1", relation="aligned_bear"), ctx
        ) is True

    def test_missing_ema_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(price_mid=4710.0))
        assert evaluate_condition(
            EMARelation(tf="H1", period=50, relation="price_above"), ctx
        ) is False


# =============================================================================
# atr — arithmetic verified per advisor item #4
# =============================================================================

class TestATR:
    """threshold_price = multiplier × baseline_pips × PIP_SIZE (= 0.1).

    With `multiplier=1.5, baseline_pips=100`, threshold_price = 15.0.

    Case A: ATR = 20.0, op=above  → 20.0 > 15.0 → True
    Case B: ATR = 13.0, op=above  → 13.0 > 15.0 → False (advisor's numbers)
    Case C: ATR = 10.0, op=below  → 10.0 < 15.0 → True
    """

    def test_above_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(atr_by_tf={"M1": 20.0}))
        cond = ATR(tf="M1", op="above", multiplier=1.5, baseline_pips=100.0)
        assert evaluate_condition(cond, ctx) is True

    def test_above_false_under_threshold(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(atr_by_tf={"M1": 13.0}))
        cond = ATR(tf="M1", op="above", multiplier=1.5, baseline_pips=100.0)
        assert evaluate_condition(cond, ctx) is False

    def test_below_true(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(atr_by_tf={"M1": 10.0}))
        cond = ATR(tf="M1", op="below", multiplier=1.5, baseline_pips=100.0)
        assert evaluate_condition(cond, ctx) is True

    def test_missing_atr_false(self, eval_ctx, fake_live):
        ctx = eval_ctx(live_data=fake_live(atr_by_tf={}))
        cond = ATR(tf="M1", op="above", multiplier=1.0, baseline_pips=50.0)
        assert evaluate_condition(cond, ctx) is False


# =============================================================================
# price_at_sr_zone — 3×3 matrix advisor item #5
# =============================================================================

class TestPriceAtSRZone:

    # With PIP_SIZE=0.1, tolerance_pips=10 → tolerance_price = 1.0 units
    _zones = [
        {"price": 4720.0, "zone_type": "support"},
        {"price": 4750.0, "zone_type": "resistance"},
    ]

    def test_at_support_with_support_filter(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4720.5),     # 5 pips from 4720
            semantic_cache=fake_semantic({"sr_zones": self._zones}),
        )
        cond = PriceAtSRZone(zone_type="support", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is True

    def test_at_support_with_resistance_filter_false(
        self, eval_ctx, fake_live, fake_semantic
    ):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4720.5),
            semantic_cache=fake_semantic({"sr_zones": self._zones}),
        )
        cond = PriceAtSRZone(zone_type="resistance", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is False

    def test_any_zone_type_matches_both(
        self, eval_ctx, fake_live, fake_semantic
    ):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4750.5),
            semantic_cache=fake_semantic({"sr_zones": self._zones}),
        )
        cond = PriceAtSRZone(zone_type="any", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is True

    def test_outside_tolerance_false(
        self, eval_ctx, fake_live, fake_semantic
    ):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4730.0),    # 100 pips from 4720
            semantic_cache=fake_semantic({"sr_zones": self._zones}),
        )
        cond = PriceAtSRZone(zone_type="any", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is False

    def test_uppercase_zone_type_matches_lowercase_filter(
        self, eval_ctx, fake_live, fake_semantic
    ):
        """Brain publishes zone_type as UPPERCASE
        'SUPPORT' / 'RESISTANCE' / 'FLIP' from support_resistance.SRZone.
        cond.zone_type is a Pydantic Literal lowercase. Pre-fix the
        case-sensitive comparison meant the filter NEVER matched
        against real upstream data — any plan using zone_type='support'
        or 'resistance' was guaranteed False. Pin case-insensitive
        contract here."""
        upper_zones = [
            {"price": 4720.0, "zone_type": "SUPPORT"},
            {"price": 4750.0, "zone_type": "RESISTANCE"},
        ]
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4750.5),  # 5 pips from 4750
            semantic_cache=fake_semantic({"sr_zones": upper_zones}),
        )
        cond = PriceAtSRZone(zone_type="resistance", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is True

    def test_flip_zone_does_not_match_support_or_resistance(
        self, eval_ctx, fake_live, fake_semantic
    ):
        """A FLIP zone (former-resistance-now-support) is neither pure
        support nor pure resistance. zone_type='support' and
        zone_type='resistance' filters must NOT match it; only
        zone_type='any' matches. Preserves operator intent."""
        flip_zones = [{"price": 4720.0, "zone_type": "FLIP"}]
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4720.5),
            semantic_cache=fake_semantic({"sr_zones": flip_zones}),
        )
        assert evaluate_condition(
            PriceAtSRZone(zone_type="support", tolerance_pips=10.0), ctx
        ) is False
        assert evaluate_condition(
            PriceAtSRZone(zone_type="resistance", tolerance_pips=10.0), ctx
        ) is False
        assert evaluate_condition(
            PriceAtSRZone(zone_type="any", tolerance_pips=10.0), ctx
        ) is True

    def test_no_sr_zones_false(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4720.0),
            semantic_cache=fake_semantic({"sr_zones": []}),
        )
        cond = PriceAtSRZone(zone_type="any", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is False

    def test_no_price_false(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=None),
            semantic_cache=fake_semantic({"sr_zones": self._zones}),
        )
        cond = PriceAtSRZone(zone_type="any", tolerance_pips=10.0)
        assert evaluate_condition(cond, ctx) is False


# =============================================================================
# price_at_fibonacci — both shape formats
# =============================================================================

class TestPriceAtFibonacci:

    def test_flat_format_match(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4712.5),
            semantic_cache=fake_semantic({
                "fibonacci": {"0.382": 4712.5, "0.5": 4715.0},
            }),
        )
        assert evaluate_condition(
            PriceAtFibonacci(level=0.382), ctx
        ) is True

    def test_list_format_match(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4712.5),
            semantic_cache=fake_semantic({
                "fibonacci": {
                    "levels": [
                        {"pct": "38.2", "price": 4712.5},
                        {"pct": "50.0", "price": 4715.0},
                    ],
                },
            }),
        )
        assert evaluate_condition(
            PriceAtFibonacci(level=0.382), ctx
        ) is True

    def test_outside_tolerance_false(
        self, eval_ctx, fake_live, fake_semantic
    ):
        # Tolerance = 5 pips = 0.5 units; 4712.5 vs level 4700.0 → far off
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4700.0),
            semantic_cache=fake_semantic({
                "fibonacci": {"0.382": 4712.5},
            }),
        )
        assert evaluate_condition(
            PriceAtFibonacci(level=0.382), ctx
        ) is False

    def test_missing_fibonacci_false(
        self, eval_ctx, fake_live, fake_semantic
    ):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=4712.5),
            semantic_cache=fake_semantic({}),
        )
        assert evaluate_condition(
            PriceAtFibonacci(level=0.382), ctx
        ) is False

    def test_no_price_false(self, eval_ctx, fake_live, fake_semantic):
        ctx = eval_ctx(
            live_data=fake_live(price_mid=None),
            semantic_cache=fake_semantic({"fibonacci": {"0.382": 4712.5}}),
        )
        assert evaluate_condition(
            PriceAtFibonacci(level=0.382), ctx
        ) is False


# =============================================================================
# duration_exceeds
# =============================================================================

class TestDurationExceeds:

    def _ctx_with_created_at(
        self, eval_ctx, fake_live, valid_plan_dict, created_at: str,
        *, ticket: int = 12345, now: datetime = None,
    ):
        from snow.schema import Plan
        valid_plan_dict = dict(valid_plan_dict)
        valid_plan_dict["created_at"] = created_at
        # Keep expires_at after created_at to pass schema validation
        from datetime import datetime as _dt, timedelta
        c = _dt.fromisoformat(created_at.replace("Z", "+00:00"))
        valid_plan_dict["expires_at"] = (c + timedelta(hours=12)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        plan = Plan(**valid_plan_dict)
        return eval_ctx(
            live_data=fake_live(),
            plan=plan, ticket=ticket, now=now,
        )

    def test_exceeds_true(self, eval_ctx, fake_live, valid_plan_dict):
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        ctx = self._ctx_with_created_at(
            eval_ctx, fake_live, valid_plan_dict,
            created_at="2026-04-24T08:00:00Z",
            now=now,  # 4 hours = 240 minutes after created_at
        )
        assert evaluate_condition(
            DurationExceeds(minutes=120), ctx
        ) is True

    def test_not_exceeds_false(self, eval_ctx, fake_live, valid_plan_dict):
        now = datetime(2026, 4, 24, 9, 0, 0, tzinfo=timezone.utc)
        ctx = self._ctx_with_created_at(
            eval_ctx, fake_live, valid_plan_dict,
            created_at="2026-04-24T08:00:00Z",
            now=now,  # 60 min elapsed
        )
        assert evaluate_condition(
            DurationExceeds(minutes=120), ctx
        ) is False

    def test_pre_entry_false(self, eval_ctx, fake_live, valid_plan_dict):
        """No ticket yet → condition cannot fire even if clock elapsed."""
        now = datetime(2026, 4, 24, 14, 0, 0, tzinfo=timezone.utc)
        ctx = self._ctx_with_created_at(
            eval_ctx, fake_live, valid_plan_dict,
            created_at="2026-04-24T08:00:00Z",
            now=now, ticket=None,
        )
        assert evaluate_condition(
            DurationExceeds(minutes=60), ctx
        ) is False

    def test_at_exact_boundary_true(self, eval_ctx, fake_live, valid_plan_dict):
        # elapsed = cond.minutes exactly → ">=" → True
        now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        ctx = self._ctx_with_created_at(
            eval_ctx, fake_live, valid_plan_dict,
            created_at="2026-04-24T08:00:00Z",
            now=now,  # 120 min
        )
        assert evaluate_condition(
            DurationExceeds(minutes=120), ctx
        ) is True


# =============================================================================
# time_between — advisor item #6: cross-midnight + inclusive boundaries
# =============================================================================

class TestTimeBetween:

    def _ctx_at(self, eval_ctx, fake_live, hh: int, mm: int):
        now = datetime(2026, 4, 24, hh, mm, 0, tzinfo=timezone.utc)
        return eval_ctx(live_data=fake_live(), now=now)

    # --- normal window [06:00, 20:00] ---

    def test_inside_normal_window(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 12, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="06:00", end_utc="20:00"), ctx
        ) is True

    def test_outside_normal_window(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 22, 30)
        assert evaluate_condition(
            TimeBetween(start_utc="06:00", end_utc="20:00"), ctx
        ) is False

    def test_exact_start_inclusive(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 6, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="06:00", end_utc="20:00"), ctx
        ) is True

    def test_exact_end_inclusive(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 20, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="06:00", end_utc="20:00"), ctx
        ) is True

    # --- cross-midnight [22:00, 06:00] ---

    def test_cross_midnight_late_evening_true(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 23, 30)
        assert evaluate_condition(
            TimeBetween(start_utc="22:00", end_utc="06:00"), ctx
        ) is True

    def test_cross_midnight_early_morning_true(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 2, 15)
        assert evaluate_condition(
            TimeBetween(start_utc="22:00", end_utc="06:00"), ctx
        ) is True

    def test_cross_midnight_mid_day_false(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 7, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="22:00", end_utc="06:00"), ctx
        ) is False

    def test_cross_midnight_exact_start(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 22, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="22:00", end_utc="06:00"), ctx
        ) is True

    def test_cross_midnight_exact_end(self, eval_ctx, fake_live):
        ctx = self._ctx_at(eval_ctx, fake_live, 6, 0)
        assert evaluate_condition(
            TimeBetween(start_utc="22:00", end_utc="06:00"), ctx
        ) is True
