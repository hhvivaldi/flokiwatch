"""FLO-358 — Snow Recipe Book.

Layer 2 of the 3-layer architecture (Layer 1 = prompt awareness in
`agent_prompts.py`; Layer 3 = source markdown in
`data/_design/snow_recipe_book.md`).

The recipe book exists because Floki, with 44+ tools available,
defaulted to single-indicator RSI patterns in 6 of 7 plans
post-FLO-381 (which framed alternatives but didn't show worked
examples). Prompt guidance alone proved insufficient to shift the
single-indicator anchor.

Recipes are curated from established technical-analysis methodology
(CMT Body of Knowledge themes, classical chart patterns, candlestick
literature, regime-based confluence reading). They are NOT primary
research; they are inspirational templates Floki can pull on demand
to surface multi-indicator confluence patterns he might not reach
for unprompted.

Key contract — descriptive, not prescriptive
--------------------------------------------
Every recipe describes how traders historically frame a setup ("X is
favored when Y") rather than directing Floki to use it ("you must
use X when Y"). Floki retains full agency over plan composition.
The CI guard `test_no_prescriptive_directives` enforces this against
the recipe markdown.

Public API
----------
- `Recipe` — Pydantic model for one recipe.
- `RecipeBook` — Pydantic model for the parsed file.
- `load_recipe_book(path)` — parse the markdown source into a
  RecipeBook. Caches by mtime so repeated tool calls are cheap.
- `get_recipes_by_category(category, ...)` — the function backing
  the `get_snow_recipe_book` agent tool.
- `RECIPE_CATEGORIES` — the closed enum of categories Floki can
  filter by.
"""
from __future__ import annotations

import os
import re
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


RECIPE_CATEGORIES: tuple[str, ...] = (
    "trend",
    "range",
    "reversal",
    "risk_management",
)

RecipeCategory = Literal["trend", "range", "reversal", "risk_management"]


class Ingredient(BaseModel):
    """One element of a recipe's `ingredients` list. The `primitive`
    field MUST name a real Snow Condition primitive type — the CI
    guard `test_recipe_primitives_exist_in_schema` verifies this."""

    primitive: str = Field(
        ...,
        description=(
            "Snow Condition primitive type — must match a `type` "
            "literal in snow.schema. e.g. 'bollinger_position', "
            "'macd_histogram', 'price_at_sr_zone'."
        ),
    )
    role: str = Field(
        ...,
        min_length=3,
        max_length=200,
        description=(
            "Plain-language description of what this ingredient "
            "contributes to the confluence. Descriptive voice."
        ),
    )


class Recipe(BaseModel):
    """One curated multi-indicator setup.

    Fields chosen to match the FLO-358 directive: common_ingredients
    (multi-indicator confluence), when_traders_favor_it,
    what_it_captures, variations, framing_note.
    """

    id: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="snake_case unique id. Stable; downstream "
        "audits / FLO-378 sub-utilization tracking key on this.",
    )
    category: RecipeCategory
    title: str = Field(..., min_length=5, max_length=120)
    primary_signal: str = Field(
        ...,
        description=(
            "The dominant primitive that anchors the setup. ≥70% of "
            "recipes must have a non-RSI primary_signal per FLO-358 "
            "diversification requirement."
        ),
    )
    setup_type_alignment: list[str] = Field(
        default_factory=list,
        description=(
            "FLO-366 setup_type values this recipe naturally aligns "
            "with. Empty list = recipe is regime-agnostic."
        ),
    )
    common_ingredients: list[Ingredient] = Field(
        ..., min_length=2,
        description=(
            "Multi-indicator confluence — at least 2 primitives "
            "combined. The point of the recipe book is to show "
            "confluence; a single-primitive recipe has no value."
        ),
    )
    when_traders_favor_it: str = Field(
        ..., min_length=40,
        description=(
            "Descriptive voice ('Traders look for this when X') "
            "naming the regime / context where the pattern is "
            "historically documented."
        ),
    )
    what_it_captures: str = Field(
        ..., min_length=40,
        description=(
            "What edge / behavior the setup is trying to capture. "
            "Connects to thesis-shape: continuation vs reversion vs "
            "structural break."
        ),
    )
    variations: list[str] = Field(
        default_factory=list,
        description=(
            "Optional adjustments that preserve Floki's agency — "
            "e.g., 'on lower TFs: tighten the BB threshold to 1.5σ'. "
            "Recipe is starting point, not template."
        ),
    )
    framing_note: str = Field(
        ..., min_length=40,
        description=(
            "Connects the recipe to thesis composition. Reminds "
            "Floki this is one shape among many; mentions the "
            "setup_type it pairs with most naturally."
        ),
    )

    @field_validator("primary_signal")
    @classmethod
    def primary_signal_alphanumeric(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError(
                f"primary_signal must be a snake_case primitive "
                f"name, got {v!r}"
            )
        return v


class RecipeBook(BaseModel):
    """Top-level container — the parsed `data/_design/snow_recipe_book.md`."""

    version: str = Field(..., description="semver-style version bumped per content edit")
    source_note: str = Field(
        ..., min_length=40,
        description=(
            "Honest provenance statement — recipes are curated from "
            "established TA methodology, not primary research."
        ),
    )
    recipes: list[Recipe]

    def by_category(self, category: str) -> list[Recipe]:
        if category not in RECIPE_CATEGORIES:
            return []
        return [r for r in self.recipes if r.category == category]

    def primary_signal_distribution(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.recipes:
            out[r.primary_signal] = out.get(r.primary_signal, 0) + 1
        return out

    def non_rsi_share(self) -> float:
        if not self.recipes:
            return 0.0
        non_rsi = sum(1 for r in self.recipes if r.primary_signal != "rsi")
        return non_rsi / len(self.recipes)


# ---------------------------------------------------------------------------
# Markdown parser
# ---------------------------------------------------------------------------

# Recipe sections are delimited by `## RECIPE:` headings. Each recipe's
# structured fields live in a fenced ```yaml block immediately under
# the heading; descriptive prose follows in fixed subheadings.

_RECIPE_HEADING = re.compile(r"^## RECIPE:\s*(.+?)$", re.MULTILINE)
_BOOK_PREAMBLE_VERSION = re.compile(r"^version:\s*([^\s]+)\s*$", re.MULTILINE)
_FRONTMATTER_BLOCK = re.compile(
    r"^---\s*\n(.*?)\n---\s*$", re.MULTILINE | re.DOTALL,
)


def parse_recipe_book_markdown(text: str) -> RecipeBook:
    """Parse the markdown source. Raises pydantic ValidationError on
    schema problems; raises ValueError on structural problems
    (missing preamble, malformed fences). Source-of-truth-friendly:
    one place that knows the markdown format."""

    # Preamble: a `---` fenced YAML block at the top with version +
    # source_note + (optional) front-matter.
    fm = _FRONTMATTER_BLOCK.search(text)
    if not fm:
        raise ValueError(
            "snow_recipe_book.md must begin with a `---` fenced YAML "
            "preamble containing `version` and `source_note`."
        )
    preamble = yaml.safe_load(fm.group(1)) or {}
    if "version" not in preamble or "source_note" not in preamble:
        raise ValueError(
            "Preamble missing required fields. Expected: version, "
            f"source_note. Got: {sorted(preamble.keys())}"
        )

    # Each recipe = `## RECIPE: <title>` + ```yaml ... ``` block +
    # subheaded prose sections.
    recipes: list[Recipe] = []
    body = text[fm.end():]
    sections = _split_recipe_sections(body)
    for raw in sections:
        recipes.append(_parse_one_recipe(raw))

    return RecipeBook(
        version=str(preamble["version"]),
        source_note=str(preamble["source_note"]),
        recipes=recipes,
    )


def _split_recipe_sections(body: str) -> list[str]:
    """Split body into per-recipe chunks delimited by `## RECIPE:`."""
    headings = list(_RECIPE_HEADING.finditer(body))
    chunks: list[str] = []
    for i, m in enumerate(headings):
        start = m.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        chunks.append(body[start:end])
    return chunks


def _parse_one_recipe(chunk: str) -> Recipe:
    title_match = _RECIPE_HEADING.search(chunk)
    if not title_match:
        raise ValueError("Recipe chunk has no `## RECIPE:` heading")
    title = title_match.group(1).strip()

    # YAML block — first ```yaml ... ``` after the heading.
    yaml_match = re.search(r"```yaml\s*\n(.*?)\n```", chunk, re.DOTALL)
    if not yaml_match:
        raise ValueError(
            f"Recipe {title!r}: missing required ```yaml structured-fields "
            f"block."
        )
    structured = yaml.safe_load(yaml_match.group(1)) or {}

    # Prose sections — bolded subheadings.
    prose = chunk[yaml_match.end():]
    sections = _extract_bold_sections(prose)
    required_prose = (
        "When traders favor it",
        "What it captures",
        "Framing note",
    )
    missing = [s for s in required_prose if s not in sections]
    if missing:
        raise ValueError(
            f"Recipe {title!r}: missing required prose sections "
            f"{missing}. Each recipe needs **When traders favor it**, "
            f"**What it captures**, **Framing note**; **Variations** "
            f"is optional."
        )

    return Recipe(
        title=title,
        when_traders_favor_it=sections["When traders favor it"],
        what_it_captures=sections["What it captures"],
        variations=_split_bullet_list(sections.get("Variations", "")),
        framing_note=sections["Framing note"],
        **{
            k: v for k, v in structured.items()
            if k in {"id", "category", "primary_signal",
                     "setup_type_alignment", "common_ingredients"}
        },
    )


def _extract_bold_sections(prose: str) -> dict[str, str]:
    """Map `**Heading:**` body → {heading: body}.

    A section ends at the next `**...**` heading or end of chunk.
    """
    out: dict[str, str] = {}
    pattern = re.compile(r"\*\*([^*]+?):\*\*", re.MULTILINE)
    matches = list(pattern.finditer(prose))
    for i, m in enumerate(matches):
        key = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(prose)
        out[key] = prose[start:end].strip()
    return out


def _split_bullet_list(text: str) -> list[str]:
    if not text.strip():
        return []
    lines = [
        ln.strip().lstrip("-*").strip()
        for ln in text.splitlines()
        if ln.strip().startswith(("-", "*"))
    ]
    return [ln for ln in lines if ln]


# ---------------------------------------------------------------------------
# Cached loader + tool entry point
# ---------------------------------------------------------------------------

_DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "_design", "snow_recipe_book.md",
)
_CACHE: dict[str, tuple[float, RecipeBook]] = {}


def load_recipe_book(path: Optional[str] = None) -> RecipeBook:
    """Parse + cache by mtime. Tests can pass an alternate path."""
    p = os.path.abspath(path or _DEFAULT_PATH)
    try:
        mtime = os.path.getmtime(p)
    except OSError as e:
        raise FileNotFoundError(
            f"Recipe book not found at {p}. FLO-358 source is "
            f"data/_design/snow_recipe_book.md."
        ) from e
    cached = _CACHE.get(p)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(p, "r", encoding="utf-8") as f:
        text = f.read()
    book = parse_recipe_book_markdown(text)
    _CACHE[p] = (mtime, book)
    return book


def get_recipes_by_category(
    category: Optional[str] = None,
    *,
    path: Optional[str] = None,
) -> dict:
    """Backing function for the `get_snow_recipe_book` agent tool.

    Args:
      category: One of RECIPE_CATEGORIES, or None for all recipes.
      path: Override source path (tests / future migration).

    Returns:
      Dict with `version`, `source_note`, `count`, `recipes` (list
      of recipe dicts). Stable serialization shape — Floki's tool
      result schema. Pydantic models go to dict via `model_dump()`.
    """
    book = load_recipe_book(path=path)
    if category is None:
        recipes = book.recipes
    elif category not in RECIPE_CATEGORIES:
        return {
            "success": False,
            "reason": (
                f"Unknown category {category!r}. Valid categories: "
                f"{list(RECIPE_CATEGORIES)} or None for all."
            ),
        }
    else:
        recipes = book.by_category(category)
    return {
        "success": True,
        "version": book.version,
        "source_note": book.source_note,
        "category_filter": category,
        "count": len(recipes),
        "recipes": [r.model_dump() for r in recipes],
    }
