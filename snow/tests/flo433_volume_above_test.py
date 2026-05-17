"""FLO-433 — volume_above primitive tests.

Covers:
  - schema accepts/rejects volume_above conditions
  - evaluator returns True/False/None per ratio + live volume_ratio
  - dispatch routes "volume_above" correctly
"""

from __future__ import annotations

from copy import deepcopy
from typing import Optional

import pytest

from snow.schema import Plan
from snow.evaluators.dispatch import evaluate_condition, registered_condition_types


class _FakeLDVolume:
    """Minimal LiveData stand-in exposing only volume_ratio."""
    def __init__(self, ratio: Optional[float]):
        self._ratio = ratio

    def volume_ratio(self, tf: str = "H1", period: int = 20) -> Optional[float]:
        return self._ratio


# -----------------------------------------------------------------------------
# Schema
# -----------------------------------------------------------------------------

class TestVolumeAboveSchema:

    def test_minimal_accepts(self, valid_plan_dict):
        out = deepcopy(valid_plan_dict)
        out["entry"]["conditions"] = [
            {"type": "price_above", "level": 4730.0},
            {"type": "volume_above", "tf": "H1", "period": 20, "ratio": 0.5},
        ]
        Plan(**out)  # raises on invalid

    def test_period_below_floor_rejected(self, valid_plan_dict):
        out = deepcopy(valid_plan_dict)
        out["entry"]["conditions"].append(
            {"type": "volume_above", "tf": "H1", "period": 2, "ratio": 0.5}
        )
        with pytest.raises(Exception):
            Plan(**out)

    def test_ratio_negative_rejected(self, valid_plan_dict):
        out = deepcopy(valid_plan_dict)
        out["entry"]["conditions"].append(
            {"type": "volume_above", "tf": "H1", "period": 20, "ratio": -0.1}
        )
        with pytest.raises(Exception):
            Plan(**out)

    def test_defaults_apply(self, valid_plan_dict):
        out = deepcopy(valid_plan_dict)
        out["entry"]["conditions"].append(
            {"type": "volume_above", "tf": "H1"}
        )
        p = Plan(**out)
        vol = next(c for c in p.entry.conditions if c.type == "volume_above")
        assert vol.period == 20
        assert vol.ratio == 0.5


# -----------------------------------------------------------------------------
# Evaluator
# -----------------------------------------------------------------------------

class TestVolumeAboveEvaluator:

    @staticmethod
    def _make_cond(valid_plan_dict, *, ratio: float = 0.5):
        out = deepcopy(valid_plan_dict)
        out["entry"]["conditions"] = [
            {"type": "volume_above", "tf": "H1", "period": 20, "ratio": ratio},
        ]
        return Plan(**out).entry.conditions[0]

    def test_above_ratio_true(self, valid_plan_dict, eval_ctx):
        cond = self._make_cond(valid_plan_dict, ratio=0.5)
        ctx = eval_ctx(live_data=_FakeLDVolume(0.7))
        assert evaluate_condition(cond, ctx) is True

    def test_below_ratio_false(self, valid_plan_dict, eval_ctx):
        cond = self._make_cond(valid_plan_dict, ratio=0.5)
        ctx = eval_ctx(live_data=_FakeLDVolume(0.3))
        assert evaluate_condition(cond, ctx) is False

    def test_equal_ratio_true(self, valid_plan_dict, eval_ctx):
        cond = self._make_cond(valid_plan_dict, ratio=0.5)
        ctx = eval_ctx(live_data=_FakeLDVolume(0.5))
        # Boundary: 0.5 >= 0.5 → True
        assert evaluate_condition(cond, ctx) is True

    def test_none_volume_returns_false(self, valid_plan_dict, eval_ctx):
        cond = self._make_cond(valid_plan_dict, ratio=0.5)
        ctx = eval_ctx(live_data=_FakeLDVolume(None))
        assert evaluate_condition(cond, ctx) is False


# -----------------------------------------------------------------------------
# Dispatch wiring
# -----------------------------------------------------------------------------

class TestVolumeAboveDispatch:

    def test_registered(self):
        assert "volume_above" in registered_condition_types()
