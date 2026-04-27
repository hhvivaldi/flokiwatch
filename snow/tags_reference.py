"""Snow setup-tagging reference — FLO-366.

Discoverability layer for the closed `setup_type` / `context_tags` /
`confidence_reason` vocabulary added in schema v3. Floki calls
`get_snow_tags_reference()` (via agent_tools) when drafting a v3 plan
and needs the exact enum values + a few worked examples.

Why a manual description map (vs introspecting Pydantic):
  Pydantic Literals don't carry per-value descriptions natively. The
  enum membership is enforced by `snow.schema` (single source of
  truth — adding a new value there without updating this file would
  show up as a drift test failure; see the matching test). The
  per-value descriptions and worked examples live here because
  they're documentation, not contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, get_args

from snow.schema import (
    HtfTag,
    NewsSessionTag,
    SetupType,
    TrendTag,
    VolatilityTag,
)


# -----------------------------------------------------------------------------
# Per-value descriptions
# -----------------------------------------------------------------------------

_SETUP_TYPE_DESCRIPTIONS: Dict[str, str] = {
    "breakout_range":
        "Price breaking out of a defined range after consolidation. "
        "Use when range bounds are clear and breakout direction is "
        "confirmed by close beyond the level.",
    "pullback_trend":
        "Pullback within an established trend, expecting trend "
        "continuation. Use when HTF trend is clear and pullback "
        "stops at a support/resistance or moving average.",
    "mean_reversion_extreme":
        "Counter-trend trade at an extreme reading (RSI / Stoch / "
        "BB extension). Use when momentum is overstretched and "
        "reversion to mean is the higher-probability move.",
    "liquidity_sweep":
        "Price taking out a recent high/low (sweeping stops) before "
        "reversing. Use when the sweep wick is clear and reversal "
        "candles confirm.",
    "continuation_momentum":
        "Strong directional momentum with trend continuation. Use "
        "when ADX is high and price is making consecutive directional "
        "candles with volume.",
    "news_reaction":
        "Trading the impulse following a scheduled news release. Use "
        "when initial direction has settled and follow-through is "
        "developing. Almost always paired with `near_news` or "
        "`post_news` context tag.",
    "divergence_play":
        "RSI / MACD divergence vs price. Use when divergence is "
        "confirmed on closed bars and a trigger candle prints.",
    "paired_hedge":
        "One leg of a paired-plan bidirectional setup (PAIRED PLANS). "
        "Use when neither direction is decisively favoured and you "
        "submit BUY + SELL plans in the same cycle.",
    "structural_bounce":
        "Bounce / rejection at a structural level (S/R zone, fib, "
        "pivot, prior day high/low). Use when the level is multi-"
        "tested and reaction candles confirm.",
    "session_open_break":
        "Break of the prior session range at the new session's open "
        "(London / NY open). Use when initial drive establishes "
        "direction within the first 30-60 min.",
}

_TREND_DESCRIPTIONS: Dict[str, str] = {
    "trend_strong":
        "ADX >= 25 with clear directional structure (HH-HL or LL-LH).",
    "trend_weak":
        "Directional bias present but ADX < 25 or structure mixed.",
    "range_tight":
        "Range bounds <= ~30 pips on H1; price oscillating without "
        "clear directional commitment.",
    "range_wide":
        "Range bounds > ~30 pips on H1; oscillation between distinct "
        "S/R zones.",
}

_VOLATILITY_DESCRIPTIONS: Dict[str, str] = {
    "high_vol":
        "ATR_M15 elevated relative to recent average; bar ranges "
        "expanding.",
    "low_vol":
        "ATR_M15 compressed; bar ranges contracting; common in "
        "Asia session and pre-news.",
}

_HTF_DESCRIPTIONS: Dict[str, str] = {
    "HTF_aligned":
        "H4 / D1 trend agrees with the plan's direction.",
    "HTF_counter":
        "H4 / D1 trend opposes the plan's direction. Counter-trend "
        "trade — flag for tighter risk and shorter targets.",
    "HTF_neutral":
        "H4 / D1 ranging or mixed; HTF gives no directional vote.",
}

_NEWS_SESSION_DESCRIPTIONS: Dict[str, str] = {
    "near_news":
        "High-impact news scheduled within the next 60 minutes. "
        "Mutually exclusive with `post_news`.",
    "post_news":
        "High-impact news fired within the last 60 minutes; "
        "trading the reaction. Mutually exclusive with `near_news`.",
    "session_overlap":
        "Within the London / NY overlap window (~13:00–16:00 UTC).",
    "session_thin":
        "Asia or after-hours session; low typical volume.",
}


# -----------------------------------------------------------------------------
# Worked examples — common tag combinations for typical setups.
# -----------------------------------------------------------------------------

_EXAMPLES: List[Dict[str, Any]] = [
    {
        "scenario":
            "London open break-and-go: gold breaks Asia range with "
            "strong momentum, H4 aligned bullish, no near-term news.",
        "setup_type": "session_open_break",
        "context_tags": {
            "trend": "trend_strong",
            "volatility": "high_vol",
            "htf": "HTF_aligned",
            "news_session": ["session_overlap"],
        },
        "confidence_reason":
            "Asia range cleared with M15 close + RSI H1 above 60; "
            "EMA stack aligned; ADX 28; no NFP today.",
    },
    {
        "scenario":
            "Pullback to H1 pullback in established uptrend during "
            "thin Asia session.",
        "setup_type": "pullback_trend",
        "context_tags": {
            "trend": "trend_strong",
            "volatility": "low_vol",
            "htf": "HTF_aligned",
            "news_session": ["session_thin"],
        },
        "confidence_reason":
            "H1 retrace to 50% fib + EMA21 confluence; trend intact; "
            "Asia session — small lots, smaller target.",
    },
    {
        "scenario":
            "Counter-trend bounce at multi-touch H4 support; bullish "
            "RSI divergence; pre-FOMC.",
        "setup_type": "structural_bounce",
        "context_tags": {
            "trend": "trend_weak",
            "volatility": "high_vol",
            "htf": "HTF_counter",
            "news_session": ["near_news"],
        },
        "confidence_reason":
            "21-touch H4 support holding; M15 RSI bullish divergence; "
            "FOMC in 45 min — half lot sizing.",
    },
    {
        "scenario":
            "Paired bidirectional setup ahead of a balanced range "
            "before a CPI print.",
        "setup_type": "paired_hedge",
        "context_tags": {
            "trend": "range_wide",
            "volatility": "low_vol",
            "htf": "HTF_neutral",
            "news_session": ["near_news"],
        },
        "confidence_reason":
            "Ranging between 4720 and 4760 last 8h; CPI in 30 min; "
            "submit both legs and let Snow take whichever side resolves.",
    },
]


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def _items_with_descriptions(
    literal_type, descriptions: Dict[str, str],
) -> List[Dict[str, str]]:
    """Build [{name, description}, ...] from a Pydantic Literal alias.

    The Literal acts as the source of truth; if a value is added to
    `snow.schema` without an entry here, the description shows as
    `"(no description)"` so the drift is visible to operators.
    """
    return [
        {
            "name": v,
            "description": descriptions.get(v, "(no description)"),
        }
        for v in get_args(literal_type)
    ]


def get_tags_reference() -> Dict[str, Any]:
    """Return the FLO-366 tagging vocabulary + worked examples.

    Schema v3 plans MUST carry `analysis.setup_type` (one of the 10
    setup types), `analysis.context_tags` (trend + volatility + htf +
    optional news_session list), and `analysis.confidence_reason` (a
    20–150 char free-text rationale).

    Returns:
      {
        "success": True,
        "schema_version": 3,
        "setup_type":   [{name, description}, ...],
        "context_tags": {
          "trend":        [{name, description}, ...],
          "volatility":   [{name, description}, ...],
          "htf":          [{name, description}, ...],
          "news_session": [{name, description}, ...],
        },
        "rules": [str, ...],
        "confidence_reason": {"min_length": 20, "max_length": 150},
        "examples": [{scenario, setup_type, context_tags, ...}, ...]
      }
    """
    return {
        "success": True,
        "schema_version": 3,
        "setup_type": _items_with_descriptions(SetupType, _SETUP_TYPE_DESCRIPTIONS),
        "context_tags": {
            "trend": _items_with_descriptions(TrendTag, _TREND_DESCRIPTIONS),
            "volatility": _items_with_descriptions(VolatilityTag, _VOLATILITY_DESCRIPTIONS),
            "htf": _items_with_descriptions(HtfTag, _HTF_DESCRIPTIONS),
            "news_session": _items_with_descriptions(NewsSessionTag, _NEWS_SESSION_DESCRIPTIONS),
        },
        "rules": [
            "setup_type: pick exactly 1 of 10.",
            "context_tags.trend / volatility / htf: pick exactly 1 each.",
            "context_tags.news_session: 0 or more flags; "
            "near_news and post_news are mutually exclusive; "
            "duplicates not allowed; max 4 flags total.",
            "confidence_reason: 20–150 chars; specific evidence, "
            "not platitudes.",
        ],
        "confidence_reason": {"min_length": 20, "max_length": 150},
        "examples": _EXAMPLES,
    }
