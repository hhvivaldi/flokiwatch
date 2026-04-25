"""Snow primitive reference tests — FLO-357 Phase 7.4.

Bidirectional drift regression: locks `snow.reference._CATEGORIES`,
the Pydantic `Condition` discriminated union, and the runtime
`snow.evaluators.dispatch._DISPATCH` registry into a single source of
truth. Adding a primitive without updating all three (or removing one
from any of the three) MUST fail a test here.

Also asserts the public envelope shape Floki receives so prompt /
tool-description references stay accurate.
"""
from __future__ import annotations

from typing import get_args

import pytest

from snow import reference as _ref
from snow.evaluators.dispatch import _DISPATCH
from snow.schema import Condition


EXPECTED_CATEGORIES = {"price", "indicator", "structural", "position_state", "time"}


def _union_types() -> set[str]:
    """Pull the discriminator literal from every member of `Condition`."""
    annotated_args = get_args(Condition)
    union = annotated_args[0]
    out: set[str] = set()
    for cls in get_args(union):
        type_field = cls.model_fields.get("type")
        literals = get_args(type_field.annotation)
        out.add(str(literals[0]))
    return out


# =============================================================================
# Envelope contract
# =============================================================================

def test_returns_all_primitives_unfiltered():
    res = _ref.get_primitive_reference()
    assert res["success"] is True
    assert res["filter"] is None
    # Don't hardcode the count — derive from schema so adding a primitive
    # without updating tests fails the drift test, not this one.
    assert res["count"] == len(_ref.schema_class_types())
    names = [p["name"] for p in res["primitives"]]
    assert sorted(names) == _ref.schema_class_types()


def test_valid_categories_exact_set():
    """Catch typos in `_CATEGORIES` values (e.g. "indicators" instead of
    "indicator") that Pydantic itself cannot detect."""
    assert set(_ref.VALID_CATEGORIES) == EXPECTED_CATEGORIES
    # Stable order for the prompt's reference line.
    assert _ref.VALID_CATEGORIES == sorted(EXPECTED_CATEGORIES)


@pytest.mark.parametrize("cat", sorted(EXPECTED_CATEGORIES))
def test_filter_by_category_returns_only_that_category(cat: str):
    res = _ref.get_primitive_reference(cat)
    assert res["success"] is True
    assert res["filter"] == cat
    assert res["count"] == len(res["primitives"])
    assert res["count"] >= 1, f"Category {cat!r} produced zero primitives"
    for prim in res["primitives"]:
        assert prim["category"] == cat


def test_invalid_category_returns_error_envelope():
    res = _ref.get_primitive_reference("indicators")  # off-by-one typo
    assert res["success"] is False
    assert "Unknown category" in res["error"]
    assert res["categories"] == _ref.VALID_CATEGORIES
    # Empty string is treated as "no filter", not as invalid.
    res_empty = _ref.get_primitive_reference("")
    assert res_empty["success"] is True
    assert res_empty["filter"] is None


# =============================================================================
# Bidirectional drift — three sets must agree
# =============================================================================

def test_three_way_drift_categories_vs_union_vs_dispatch():
    """The 3 registries that together define a Snow primitive MUST stay
    aligned. Any drift = a primitive that parses but doesn't evaluate,
    OR evaluates but isn't in the reference Floki sees, OR is in the
    reference but the schema rejects.
    """
    cats = set(_ref.categorized_types().keys())
    union = _union_types()
    dispatch = set(_DISPATCH.keys())

    if not (cats == union == dispatch):
        missing_from_cats = (union | dispatch) - cats
        missing_from_union = (cats | dispatch) - union
        missing_from_dispatch = (cats | union) - dispatch
        pytest.fail(
            "Snow primitive registries drifted:\n"
            f"  missing from snow.reference._CATEGORIES: {sorted(missing_from_cats)}\n"
            f"  missing from snow.schema.Condition union: {sorted(missing_from_union)}\n"
            f"  missing from snow.evaluators.dispatch._DISPATCH: "
            f"{sorted(missing_from_dispatch)}"
        )


# =============================================================================
# Per-primitive shape — what Floki actually parses
# =============================================================================

def test_each_primitive_has_required_envelope_keys():
    res = _ref.get_primitive_reference()
    for prim in res["primitives"]:
        assert set(prim.keys()) >= {"name", "category", "description", "params"}
        assert prim["name"] in _ref.schema_class_types()
        assert prim["category"] in EXPECTED_CATEGORIES
        assert isinstance(prim["params"], dict)
        assert prim["params"], f"{prim['name']!r} has no params — suspicious"


def test_param_field_shapes_well_formed():
    """Every param entry must carry `type` and `required`. Enum-typed
    params must carry `values`. Bound metadata, when present, must be
    numeric. Catches the most common Pydantic-introspection regressions
    (missing constraint extraction, missing Literal handling)."""
    res = _ref.get_primitive_reference()
    saw_enum = False
    for prim in res["primitives"]:
        for fname, finfo in prim["params"].items():
            assert "type" in finfo, f"{prim['name']}.{fname}: missing 'type'"
            assert "required" in finfo, f"{prim['name']}.{fname}: missing 'required'"
            assert isinstance(finfo["required"], bool)
            if finfo["type"] == "enum":
                saw_enum = True
                assert "values" in finfo, (
                    f"{prim['name']}.{fname}: enum without 'values'"
                )
                assert finfo["values"], "empty enum values list"
            for bound in ("ge", "gt", "le", "lt"):
                if bound in finfo:
                    assert isinstance(finfo[bound], (int, float))
    # Sanity: at least one enum exists across the suite (e.g. rsi.op,
    # bollinger_position.position). Catches a regression where Literal
    # detection silently breaks and everything falls through to repr.
    assert saw_enum, "no enum-typed params discovered — Literal detection broken?"
