"""Snow primitive reference — Pydantic-introspected schema for Floki.

Phase 7.4 (FLO-357) — Vocabulary discoverability. Floki cannot read
`snow/schema.py` at runtime; the prompt lists primitive names but does
not document parameter shapes. This module produces a condensed,
hand-shaped reference (one entry per primitive: name, category, params
with types/enums/constraints, one-line description) populated by walking
`snow.schema.Condition.model_fields` so that schema and reference can
never drift.

Single source of truth: `_CATEGORIES` below. Every concrete condition
class in `snow.schema.Condition` MUST appear here exactly once. The
bidirectional drift test in `snow/tests/reference_test.py` enforces
this against the discriminated union AND against
`snow.evaluators.dispatch._DISPATCH`.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union, get_args, get_origin
from typing import Literal as _Literal

from snow import schema as _schema


# =============================================================================
# Category mapping — single source of truth
# =============================================================================
#
# Categories match the prompt's grouping (agent_prompts.py "Condition
# primitives:" section). Names are JSON-safe (snake_case, ASCII).

_CATEGORIES: Dict[str, str] = {
    # Price (RFC §2.5 #1-#2)
    "price_above": "price",
    "price_below": "price",
    # Indicator — point-in-time, current value
    "rsi": "indicator",
    "macd_histogram": "indicator",
    "ema_relation": "indicator",
    "atr": "indicator",
    "bollinger_position": "indicator",
    "stochastic": "indicator",
    "indicator_divergence": "indicator",
    # Phase 8b (FLO-359) — stateful indicators
    "indicator_crossover": "indicator",
    "indicator_was": "indicator",
    # Structural / level proximity
    "price_at_sr_zone": "structural",
    "price_at_fibonacci": "structural",
    "price_at_pivot": "structural",
    # Phase 8b (FLO-359) — stateful price-level latch
    "price_crossed_level": "price",
    # Position-state — require ACTIVE plan (a real broker ticket)
    "profit_pips": "position_state",
    "mfe_reached": "position_state",
    "mae_reached": "position_state",
    "profit_retraced_from_peak": "position_state",
    # Time / clock
    "duration_exceeds": "time",
    "time_between": "time",
}


VALID_CATEGORIES: List[str] = sorted(set(_CATEGORIES.values()))


# =============================================================================
# Pydantic class lookup — built once at import from the discriminated union
# =============================================================================

def _build_class_map() -> Dict[str, type]:
    """Walk `snow.schema.Condition` (Annotated[Union[...], Field(...)]),
    extract each member class, and key it by its `type:` Literal value."""
    out: Dict[str, type] = {}
    annotated_args = get_args(_schema.Condition)
    if not annotated_args:
        return out
    union = annotated_args[0]
    for cls in get_args(union):
        type_field = cls.model_fields.get("type")
        if type_field is None:
            continue
        literals = get_args(type_field.annotation)
        if not literals:
            continue
        out[str(literals[0])] = cls
    return out


_CLASS_BY_TYPE: Dict[str, type] = _build_class_map()


# =============================================================================
# Field introspection
# =============================================================================

def _is_literal(ann: Any) -> bool:
    """Robust check across typing module variations."""
    try:
        return get_origin(ann) is _Literal
    except Exception:
        return str(ann).startswith("typing.Literal")


def _strip_optional(ann: Any) -> tuple[Any, bool]:
    """Return (inner, was_optional). Handles Optional[X] / Union[X, None]."""
    origin = get_origin(ann)
    if origin is Union:
        args = get_args(ann)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) < len(args) and len(non_none) == 1:
            return non_none[0], True
    return ann, False


def _type_label(ann: Any) -> str:
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "string"
    if ann is bool:
        return "bool"
    return str(ann)


def _describe_field(fi: Any) -> Dict[str, Any]:
    """Convert a Pydantic FieldInfo into the JSON shape Floki sees.

    Output keys:
      type            — "enum" | "int" | "float" | "string" | "bool" | repr
      values          — present iff Literal (enumerated allowed values)
      required        — bool
      default         — present iff non-required AND default is not None
      description     — present iff Field(..., description=...) was set
      ge/gt/le/lt     — numeric bounds when Field(...) carries them
      pattern         — regex for string fields with Field(pattern=...)
    """
    info: Dict[str, Any] = {}
    ann, optional = _strip_optional(fi.annotation)

    if _is_literal(ann):
        info["type"] = "enum"
        # Convert literal members to JSON-friendly primitives where possible.
        info["values"] = [v for v in get_args(ann)]
    else:
        info["type"] = _type_label(ann)

    info["required"] = bool(fi.is_required())

    # Constraints from Field(ge=, le=, gt=, lt=, pattern=)
    for constraint in fi.metadata or []:
        for attr in ("ge", "gt", "le", "lt"):
            if hasattr(constraint, attr):
                info[attr] = getattr(constraint, attr)
        # Pydantic v2 stores Field(pattern=...) as _PydanticGeneralMetadata.
        pat = getattr(constraint, "pattern", None)
        if pat is not None:
            info["pattern"] = pat

    if not info["required"] and fi.default is not None:
        # Don't surface PydanticUndefined; only real defaults.
        from pydantic_core import PydanticUndefined
        if fi.default is not PydanticUndefined:
            info["default"] = fi.default

    if fi.description:
        info["description"] = fi.description

    return info


def _describe_primitive(type_str: str) -> Dict[str, Any]:
    cls = _CLASS_BY_TYPE[type_str]
    params: Dict[str, Any] = {}
    for fname, fi in cls.model_fields.items():
        if fname == "type":
            continue
        params[fname] = _describe_field(fi)

    # First non-empty line of the docstring → one-line description.
    desc = ""
    if cls.__doc__:
        for line in cls.__doc__.strip().splitlines():
            line = line.strip()
            if line:
                desc = line
                break

    return {
        "name": type_str,
        "category": _CATEGORIES.get(type_str, "unknown"),
        "description": desc,
        "params": params,
    }


# =============================================================================
# Public API
# =============================================================================

def get_primitive_reference(category: Optional[str] = None) -> Dict[str, Any]:
    """Return the full primitive schema, optionally filtered by category.

    Args:
      category: One of `VALID_CATEGORIES` (price, indicator, structural,
        position_state, time). None or "" returns all primitives.

    Returns:
      {"success": True, "categories": [...], "filter": str|None,
       "count": int, "primitives": [{...}, ...]}
      {"success": False, "error": "..."}
    """
    cat = (category or "").strip() or None
    if cat is not None and cat not in VALID_CATEGORIES:
        return {
            "success": False,
            "error": (
                f"Unknown category {cat!r}. "
                f"Valid: {VALID_CATEGORIES}. Pass null/None for all."
            ),
            "categories": VALID_CATEGORIES,
        }
    types = sorted(_CLASS_BY_TYPE.keys())
    if cat is not None:
        types = [t for t in types if _CATEGORIES.get(t) == cat]
    return {
        "success": True,
        "categories": VALID_CATEGORIES,
        "filter": cat,
        "count": len(types),
        "primitives": [_describe_primitive(t) for t in types],
    }


def categorized_types() -> Dict[str, str]:
    """Defensive copy of the category map. Diagnostics / drift tests."""
    return dict(_CATEGORIES)


def schema_class_types() -> List[str]:
    """Sorted list of type-strings present in the Pydantic Condition union.
    Defensive copy; diagnostics / drift tests."""
    return sorted(_CLASS_BY_TYPE.keys())
