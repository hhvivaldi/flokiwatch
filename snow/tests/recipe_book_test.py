"""FLO-358 — Snow Recipe Book CI guards.

Three classes of guarantee, codified as tests:

1. **Schema integrity** — every recipe parses, has required prose
   sections populated above minimum lengths, and the structured YAML
   block validates against the Pydantic model. Catches malformed
   markdown / typo'd field names early.

2. **Primitive existence** — every `primitive` referenced in any
   recipe's `common_ingredients` matches a real Snow Condition type
   literal in `snow.schema`. Drift (renaming a primitive in the
   schema without updating recipes, or recipe authoring a typo'd
   primitive name) flips this test red.

3. **No prescriptive directives** — recipes use descriptive voice
   ("traders look for X when Y") rather than prescriptive ("you must
   use X"). Aligned with `feedback_no_prescriptive_rules` memory and
   FLO-358's "framing language preserved" requirement.

4. **Diversification floor** — ≥70% of recipes have a non-RSI
   primary_signal per the FLO-358 directive. The recipe book exists
   *because* Floki was over-anchored on RSI; if the book itself
   defaults to RSI we've reproduced the problem we're solving.
"""
from __future__ import annotations

import re
import os

import pytest

from snow.recipe_book import (
    RECIPE_CATEGORIES,
    Recipe,
    RecipeBook,
    load_recipe_book,
    parse_recipe_book_markdown,
    get_recipes_by_category,
)


# ---------------------------------------------------------------------------
# Schema integrity
# ---------------------------------------------------------------------------

class TestRecipeBookSchemaIntegrity:
    def test_recipe_book_loads(self):
        book = load_recipe_book()
        assert isinstance(book, RecipeBook)
        assert len(book.recipes) >= 4, (
            f"Session 1 minimum: 4 recipes; got {len(book.recipes)}"
        )

    def test_version_is_set(self):
        book = load_recipe_book()
        assert book.version
        assert len(book.source_note) >= 40

    def test_each_recipe_has_required_fields(self):
        book = load_recipe_book()
        for r in book.recipes:
            assert r.id, f"recipe missing id"
            assert r.title, f"{r.id}: missing title"
            assert r.category in RECIPE_CATEGORIES, (
                f"{r.id}: invalid category {r.category!r}"
            )
            assert r.primary_signal, f"{r.id}: missing primary_signal"
            assert len(r.common_ingredients) >= 2, (
                f"{r.id}: needs >=2 ingredients (multi-indicator "
                f"confluence is the point); got {len(r.common_ingredients)}"
            )
            assert len(r.when_traders_favor_it) >= 40, (
                f"{r.id}: when_traders_favor_it too short — frame the "
                f"regime / context concretely"
            )
            assert len(r.what_it_captures) >= 40, (
                f"{r.id}: what_it_captures too short"
            )
            assert len(r.framing_note) >= 40, (
                f"{r.id}: framing_note too short — connect to thesis "
                f"shape + setup_type"
            )

    def test_recipe_ids_unique(self):
        book = load_recipe_book()
        ids = [r.id for r in book.recipes]
        assert len(ids) == len(set(ids)), (
            f"duplicate recipe ids: {[i for i in ids if ids.count(i) > 1]}"
        )

    def test_categories_cover_at_least_three(self):
        """Session 1 acceptance: cohort spans multiple categories.
        Session 2 must hit all 4."""
        book = load_recipe_book()
        cats = {r.category for r in book.recipes}
        assert len(cats) >= 3, (
            f"recipe cohort must span >=3 categories; got {sorted(cats)}"
        )


# ---------------------------------------------------------------------------
# Primitive existence — drift guard
# ---------------------------------------------------------------------------

class TestRecipePrimitivesExistInSchema:
    def _real_primitive_names(self) -> set[str]:
        """Pull every Condition `type` literal from snow.schema."""
        from snow import schema as snow_schema
        names: set[str] = set()
        # All Condition variants are Pydantic models with a `type`
        # Literal. Walk module attributes; collect any class with a
        # type-Literal field.
        import inspect
        from pydantic import BaseModel
        for _name, obj in inspect.getmembers(snow_schema, inspect.isclass):
            if not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            type_field = obj.model_fields.get("type")
            if type_field is None:
                continue
            anno = type_field.annotation
            # Literal["..."] case
            try:
                from typing import get_args, get_origin, Literal
                if get_origin(anno) is Literal:
                    for v in get_args(anno):
                        if isinstance(v, str):
                            names.add(v)
            except Exception:
                pass
        return names

    def test_every_recipe_primitive_exists(self):
        real = self._real_primitive_names()
        assert real, (
            "Bootstrap: failed to enumerate any primitive `type` "
            "literals from snow.schema — check schema layout"
        )
        book = load_recipe_book()
        unknown: list[tuple[str, str]] = []
        for r in book.recipes:
            for ing in r.common_ingredients:
                if ing.primitive not in real:
                    unknown.append((r.id, ing.primitive))
        assert not unknown, (
            f"recipe primitives not found in snow.schema Condition "
            f"types: {unknown}\nValid types: {sorted(real)}"
        )

    def test_primary_signal_is_a_real_primitive(self):
        real = self._real_primitive_names()
        book = load_recipe_book()
        bad: list[tuple[str, str]] = []
        for r in book.recipes:
            if r.primary_signal not in real:
                bad.append((r.id, r.primary_signal))
        assert not bad, (
            f"recipes have primary_signal not matching any schema "
            f"Condition type: {bad}"
        )


# ---------------------------------------------------------------------------
# No prescriptive directives — framing-language guard
# ---------------------------------------------------------------------------

# Patterns we forbid in directive position. The recipe book is
# inspirational; "you must use X" / "always do Y" / "never use Z" is
# not the voice. We allow these words in non-directive position
# (e.g., "must be ≥0" in a numeric constraint, "always-true latch").
_PRESCRIPTIVE_DIRECTIVE_PATTERNS = [
    r"\byou must (use|do|set|wire)\b",
    r"\bmust use\b",
    r"\bmust always\b",
    r"\bnever use\b",
    r"\balways use\b",
    r"\brequired to (use|set|wire)\b",
    r"\bdo not (use|set|wire)\b",
    r"\bshould always (use|set|wire)\b",
]


class TestRecipeBookNoPrescriptiveDirectives:
    def test_recipe_prose_has_no_prescriptive_directives(self):
        """feedback_no_prescriptive_rules: never tell Floki what he
        MUST do at the recipe level. Frame as how traders historically
        approach the setup, not as directives."""
        book = load_recipe_book()
        violations: list[tuple[str, str, str]] = []  # (recipe_id, field, match)
        for r in book.recipes:
            for field_name in (
                "when_traders_favor_it",
                "what_it_captures",
                "framing_note",
            ):
                value = getattr(r, field_name)
                for pattern in _PRESCRIPTIVE_DIRECTIVE_PATTERNS:
                    m = re.search(pattern, value, re.IGNORECASE)
                    if m:
                        violations.append((r.id, field_name, m.group(0)))
            for v in r.variations:
                for pattern in _PRESCRIPTIVE_DIRECTIVE_PATTERNS:
                    m = re.search(pattern, v, re.IGNORECASE)
                    if m:
                        violations.append((r.id, "variations", m.group(0)))
        assert not violations, (
            f"prescriptive directive language detected: {violations}\n"
            f"Frame as 'traders look for X when Y' / 'pairs naturally "
            f"with X' / 'avoid X because' — never 'you must use X'."
        )

    def test_source_note_acknowledges_curation_origin(self):
        """Honesty: recipes are curated from TA literature exposure,
        not primary research. The source_note must say so."""
        book = load_recipe_book()
        note = book.source_note.lower()
        # At least one of these phrases should be present.
        markers = [
            "curated", "established", "methodology",
            "inspirational", "literature",
        ]
        assert any(m in note for m in markers), (
            f"source_note should acknowledge curation origin (one of "
            f"{markers}); got: {book.source_note[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Diversification floor (FLO-358 directive)
# ---------------------------------------------------------------------------

class TestRecipeBookDiversification:
    def test_non_rsi_primary_signal_majority(self):
        """FLO-358: ≥70% of recipes must have a non-RSI primary_signal.
        Session 1 cohort target: 4/4 = 100% (the recipes we ship today
        are deliberately non-RSI). Session 2 may include some RSI-
        primary recipes (e.g., RSI divergence at HTF level), but the
        70% floor must hold."""
        book = load_recipe_book()
        share = book.non_rsi_share()
        assert share >= 0.7, (
            f"non-RSI primary_signal share {share*100:.0f}% below 70% "
            f"floor. Distribution: {book.primary_signal_distribution()}"
        )

    def test_at_least_three_distinct_primary_signals(self):
        """The book exists to break Floki's single-indicator anchor.
        If every recipe used the same primary signal we'd have just
        moved the anchor from RSI to BB."""
        book = load_recipe_book()
        signals = {r.primary_signal for r in book.recipes}
        assert len(signals) >= 3, (
            f"recipe cohort uses too few distinct primary signals; "
            f"got {sorted(signals)} — need >=3"
        )


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

class TestGetRecipesByCategoryTool:
    def test_no_filter_returns_all(self):
        out = get_recipes_by_category()
        assert out["success"] is True
        book = load_recipe_book()
        assert out["count"] == len(book.recipes)
        assert out["category_filter"] is None

    def test_category_filter_returns_only_matching(self):
        for cat in RECIPE_CATEGORIES:
            out = get_recipes_by_category(category=cat)
            if out["count"] == 0:
                continue  # category may be unrepresented in Session 1
            assert out["success"] is True
            assert out["category_filter"] == cat
            for r in out["recipes"]:
                assert r["category"] == cat

    def test_invalid_category_returns_failure(self):
        out = get_recipes_by_category(category="not_a_category")
        assert out["success"] is False
        assert "Unknown category" in out["reason"]

    def test_recipes_serialize_with_full_shape(self):
        out = get_recipes_by_category()
        assert out["recipes"], "Session 1 must ship recipes"
        first = out["recipes"][0]
        for required_key in (
            "id", "title", "category", "primary_signal",
            "common_ingredients", "when_traders_favor_it",
            "what_it_captures", "variations", "framing_note",
            "setup_type_alignment",
        ):
            assert required_key in first, (
                f"recipe dict missing key {required_key!r}"
            )

    def test_agent_tools_get_snow_recipe_book(self):
        """End-to-end via the AgentTools surface — Floki's call path."""
        from agent_tools import AgentTools
        tools = AgentTools.__new__(AgentTools)
        tools._log_tool = lambda *a, **k: None
        tools._log_fail = lambda *a, **k: None
        out = tools.get_snow_recipe_book(category="trend")
        assert out["success"] is True
        assert out["count"] >= 1


# ---------------------------------------------------------------------------
# Parser robustness
# ---------------------------------------------------------------------------

class TestParserRobustness:
    def test_missing_preamble_raises(self):
        with pytest.raises(ValueError, match="preamble"):
            parse_recipe_book_markdown("# No preamble here\n\n## RECIPE: X")

    def test_recipe_missing_yaml_block_raises(self):
        text = (
            "---\nversion: 0.1\nsource_note: curated test source.\n---\n\n"
            "## RECIPE: Test\n\n**When traders favor it:** abc...\n"
        )
        with pytest.raises(ValueError, match="yaml"):
            parse_recipe_book_markdown(text)

    def test_recipe_missing_required_prose_section_raises(self):
        text = (
            "---\nversion: 0.1\nsource_note: curated test source.\n---\n\n"
            "## RECIPE: Test\n\n"
            "```yaml\nid: test\ncategory: trend\nprimary_signal: rsi\n"
            "common_ingredients:\n"
            "  - primitive: rsi\n    role: stub role.\n"
            "  - primitive: macd_histogram\n    role: stub role.\n"
            "```\n\n"
            "**When traders favor it:** This is a long enough sentence to satisfy the 40-char minimum length contract.\n"
        )
        with pytest.raises(ValueError, match="missing required prose"):
            parse_recipe_book_markdown(text)
