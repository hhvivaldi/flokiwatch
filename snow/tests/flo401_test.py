"""FLO-401 — exit field mandatory (min_length=1) — contract lock tests.

Background: Gemini PLAN-20260429-005 + PLAN-20260429-006 shipped with
exit=[] (vs Qwen baseline 1-2 exits/plan), measurably regressing the
trade-safety profile — management contingencies were left as the entire
downside-protection layer, with no programmatic close path on
thesis-break or target-hit.

Schema change: snow/schema.py:704 `exit: list[Contingency] = Field(...)`
gained `min_length=1`, lost `default_factory=list`.

This file locks the new contract at TWO layers:
  1. Pydantic schema rejects empty/missing exit at construction
     (already covered by schema_test.py invert; mirrored here for
     ticket-level traceability)
  2. validate_plan() — the business-rule wrapper Floki actually
     calls via submit_plan_to_snow — surfaces the same rejection in
     its `(ok, plan, errors)` triple, which is how Floki sees it.

Mirrors the existing FLO-391/FLO-392 contract-test pattern.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from snow.schema import Plan
from snow.validator import validate_plan


def _plan_dict_no_exit(valid_plan_dict):
    d = {**valid_plan_dict}
    d.pop("exit", None)
    return d


class TestFLO401SchemaLayer:
    """Pydantic-level: Plan() construction rejects exit=[] / missing."""

    def test_omitting_exit_raises(self, valid_plan_dict):
        with pytest.raises(ValidationError) as exc:
            Plan(**_plan_dict_no_exit(valid_plan_dict))
        # FLO-401 is the new floor; error message should mention exit.
        assert "exit" in str(exc.value).lower()

    def test_empty_exit_raises(self, valid_plan_dict):
        d = {**valid_plan_dict, "exit": []}
        with pytest.raises(ValidationError) as exc:
            Plan(**d)
        # Pydantic min_length error shape.
        msg = str(exc.value).lower()
        assert "exit" in msg
        assert "least 1" in msg or "min_length" in msg or "too_short" in msg

    def test_one_exit_accepted(self, valid_plan_dict):
        """The new floor is exactly 1 — a single-entry exit list passes."""
        single_exit = [valid_plan_dict["exit"][0]]
        d = {**valid_plan_dict, "exit": single_exit}
        p = Plan(**d)
        assert len(p.exit) == 1


class TestFLO401ValidatorLayer:
    """Business-rule layer: validate_plan() returns ok=False, errors
    surface to Floki via submit_plan_to_snow's response."""

    def test_validate_plan_rejects_empty_exit(self, valid_plan_dict):
        d = {**valid_plan_dict, "exit": []}
        ok, plan, errors = validate_plan(d)
        assert ok is False
        assert plan is None
        # Errors are formatted as 'schema: (path): message' strings.
        # Floki reads these verbatim and self-corrects (we observed this
        # in the FLO-389 first-cycle on context_tags / conditions /
        # management — same mechanism applies here).
        joined = " | ".join(errors).lower()
        assert "exit" in joined

    def test_validate_plan_rejects_missing_exit(self, valid_plan_dict):
        ok, plan, errors = validate_plan(_plan_dict_no_exit(valid_plan_dict))
        assert ok is False
        assert plan is None
        joined = " | ".join(errors).lower()
        assert "exit" in joined

    def test_validate_plan_accepts_single_exit(self, valid_plan_dict):
        """Conftest's valid_plan_dict has 2 exits today — confirm 1 is
        also enough. Belt-and-braces against future regressions that
        might over-tighten the floor."""
        single_exit = [valid_plan_dict["exit"][0]]
        d = {**valid_plan_dict, "exit": single_exit}
        ok, plan, errors = validate_plan(d)
        assert ok is True, f"unexpected errors: {errors}"
        assert plan is not None
        assert len(plan.exit) == 1
