"""Snow schema — Pydantic v2 models for plans, contingencies, conditions, actions.

Spec: `data/_design/FLO-347_Snow_RFC_v1.md` §2 and §8.

Design decisions (CTO-approved, frozen 2026-04-23):
  - Pydantic v2 with discriminated unions for Condition / Action zoos
  - 14 condition primitives for v1 (stateful `price_crosses_*` deferred to v2)
  - Priority formula: effective = base + min(base-1, override*10)
    with power-of-2 action bases (128/64/32/16/8/4) — see §8.1
  - Timestamps: ISO-8601 UTC with 'Z' suffix (Rule 22)
  - Plan ID pattern: "PLAN-YYYYMMDD-NNN"

This module is PURE data/validation — no MT5 imports, no executor
imports, no side effects. Dependency graph:
  snow.schema ← snow.validator ← (later: snow.evaluator.*, snow.actions, …)
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import SCHEMA_VERSION


# =============================================================================
# Enums
# =============================================================================

class PlanStatus(str, Enum):
    """Lifecycle states (RFC §3.1)."""
    PENDING   = "pending"
    TRIGGERED = "triggered"    # transient; entry fired, broker call in flight
    ACTIVE    = "active"
    CLOSING   = "closing"      # transient; exit fired, close call in flight
    CLOSED    = "closed"       # terminal
    CANCELLED = "cancelled"    # terminal
    EXPIRED   = "expired"      # terminal
    FAILED    = "failed"       # terminal


class ContingencyState(str, Enum):
    """Per-contingency lifecycle within a plan (RFC §3.3)."""
    ARMED       = "armed"
    FIRED       = "fired"
    FAILED      = "failed"
    DEACTIVATED = "deactivated"    # fires=once and already fired


class ContingencyFires(str, Enum):
    ONCE       = "once"
    EVERY_TIME = "every_time"


class Direction(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"


# MT5 timeframes used by indicator conditions.
Timeframe = Literal["M1", "M5", "M15", "H1", "H4", "D1"]
# Comparison operator for threshold-style conditions.
ComparisonOp = Literal["above", "below"]


# =============================================================================
# Condition primitives (14 for v1 — RFC §2.5)
#
# All concrete condition classes carry a `type: Literal[...]` tag for the
# discriminated union. Any new primitive must add its class below AND to
# the `Condition` union at the bottom of this section.
# =============================================================================

class _Cond(BaseModel):
    """Shared config for all condition models."""
    model_config = ConfigDict(extra="forbid", frozen=False)


# --- §2.5 #1-#2: price-based ---

class PriceAbove(_Cond):
    type: Literal["price_above"] = "price_above"
    level: float = Field(description="Target price; condition true if market > level")


class PriceBelow(_Cond):
    type: Literal["price_below"] = "price_below"
    level: float = Field(description="Target price; condition true if market < level")


# --- §2.5 #3: momentum / RSI ---

class RSI(_Cond):
    type: Literal["rsi"] = "rsi"
    tf: Timeframe
    op: ComparisonOp
    threshold: float = Field(ge=0, le=100)


# --- §2.5 #4: momentum / MACD histogram ---

class MACDHistogram(_Cond):
    type: Literal["macd_histogram"] = "macd_histogram"
    tf: Timeframe
    op: ComparisonOp
    threshold: float


# --- §2.5 #5: trend / EMA relation ---

EMARelationKind = Literal[
    "price_above",       # price above the EMA line
    "price_below",       # price below the EMA line
    "aligned_bull",      # fast > slow > …  (9 > 21 > 50 > 200)
    "aligned_bear",      # fast < slow < …
]


class EMARelation(_Cond):
    type: Literal["ema_relation"] = "ema_relation"
    tf: Timeframe
    period: Literal[9, 21, 50, 200]
    relation: EMARelationKind


# --- §2.5 #6: volatility / ATR ---

class ATR(_Cond):
    type: Literal["atr"] = "atr"
    tf: Timeframe
    op: ComparisonOp
    multiplier: float = Field(gt=0, description="ATR > multiplier × baseline_pips?")
    baseline_pips: float = Field(gt=0)


# --- §2.5 #7: structural / S/R zone ---

ZoneKind = Literal["support", "resistance", "any"]


class PriceAtSRZone(_Cond):
    type: Literal["price_at_sr_zone"] = "price_at_sr_zone"
    zone_type: ZoneKind = "any"
    tolerance_pips: float = Field(gt=0)


# --- §2.5 #8: structural / Fibonacci ---

# Level is a float literal, not a string: Floki will pass JSON numbers
# like 0.618, not quoted "0.618". Pydantic accepts numeric literals
# and rejects any out-of-enum value.
# Phase 7.3 (FLO-355): extended to include the 0.236 retracement and
# the 1.0 / 1.272 / 1.618 extension levels — common XAUUSD setups
# pivot on the extensions when a swing has fully retraced.
FibLevel = Literal[0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618]


class PriceAtFibonacci(_Cond):
    type: Literal["price_at_fibonacci"] = "price_at_fibonacci"
    level: FibLevel
    # Phase 7.3: optional explicit tolerance. Default None preserves
    # backward compatibility — evaluator falls back to its 5-pip default.
    tolerance_pips: Optional[float] = Field(default=None, gt=0)


# --- §2.5 #9: position-state / profit in pips ---

class ProfitPips(_Cond):
    type: Literal["profit_pips"] = "profit_pips"
    op: ComparisonOp
    threshold: float


# --- §2.5 #10: position-state / MFE reached ---

class MFEReached(_Cond):
    type: Literal["mfe_reached"] = "mfe_reached"
    pips: float = Field(gt=0)


# --- §2.5 #11: position-state / MAE reached ---

class MAEReached(_Cond):
    type: Literal["mae_reached"] = "mae_reached"
    pips: float = Field(gt=0)


# --- §2.5 #12: position-state / profit retraced from peak ---

class ProfitRetracedFromPeak(_Cond):
    type: Literal["profit_retraced_from_peak"] = "profit_retraced_from_peak"
    pips: float = Field(gt=0)


# --- §2.5 #13: time / trade age ---

class DurationExceeds(_Cond):
    type: Literal["duration_exceeds"] = "duration_exceeds"
    minutes: int = Field(gt=0)


# --- §2.5 #14: time / UTC window ---

_HHMM_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class TimeBetween(_Cond):
    type: Literal["time_between"] = "time_between"
    start_utc: str = Field(pattern=_HHMM_PATTERN, description="HH:MM UTC, inclusive start")
    end_utc:   str = Field(pattern=_HHMM_PATTERN, description="HH:MM UTC, inclusive end")


# =============================================================================
# Phase 7.3 (FLO-355) — Cat A indicator primitives
#
# All four read pre-computed data from Brain's `_last_agent_data`
# (via SemanticCache). No new computation in Snow / LiveData; just
# wiring the data Brain already publishes into Floki's plan vocabulary.
# Same fail-safe contract as the v1 primitives: missing data → False.
# =============================================================================

# --- §7.3 #15: Bollinger Bands position / squeeze ---

BollingerKind = Literal[
    "above_upper",   # current price > upper band  (touch / breach)
    "below_lower",   # current price < lower band  (touch / breach)
    "above_middle",  # current price > middle band (upper half)
    "below_middle",  # current price < middle band (lower half)
    "in_squeeze",    # bb width is below the squeeze threshold (Brain bool)
]


class BollingerPosition(_Cond):
    """Bollinger Bands relation. Tf is informational; Brain currently
    publishes BB on its primary timeframe (H1) only — non-H1 fields
    silently return False when the data is not in the cache."""
    type: Literal["bollinger_position"] = "bollinger_position"
    tf: Timeframe
    relation: BollingerKind


# --- §7.3 #16: Stochastic ---

class Stochastic(_Cond):
    """Standard Stochastic oscillator value vs threshold. Same shape
    semantics as RSI; reads from Brain's pre-computed indicator dict."""
    type: Literal["stochastic"] = "stochastic"
    tf: Timeframe
    op: ComparisonOp
    threshold: float = Field(ge=0, le=100)


# --- §7.3 #17: Pivot point proximity ---

PivotSet = Literal["classic", "fibonacci"]
PivotLevel = Literal["PP", "R1", "R2", "R3", "S1", "S2", "S3"]


class PriceAtPivot(_Cond):
    """Proximity to a daily pivot point (Classic or Fibonacci set).
    Brain computes daily pivots from the previous-day candle and exposes
    them via `pivot_points.daily.{classic,fibonacci}.{PP,R1..R3,S1..S3}`.

    `pivot_set` is the column choice; `level` is the row choice. Field
    is named `pivot_set` (not the Python builtin `set`) to avoid
    confusion in plan dicts."""
    type: Literal["price_at_pivot"] = "price_at_pivot"
    pivot_set: PivotSet = "classic"
    level: PivotLevel
    tolerance_pips: float = Field(gt=0)


# --- §7.3 #18: Indicator divergence ---

DivergenceIndicator = Literal["macd"]
DivergenceDirection = Literal["bullish", "bearish"]


class IndicatorDivergence(_Cond):
    """Brain detects price-vs-indicator divergence each cycle (see
    technical_analyzer.detect_macd_divergence) and publishes the result
    as `indicators.macd.divergence = {detected, type, bars_since}`.
    This primitive returns True iff `detected==True AND type==direction`.

    v1 supports `macd` only; RSI divergence requires a parallel detector
    in Brain (deferred to a follow-up). When that lands, extend
    `DivergenceIndicator` to include "rsi"."""
    type: Literal["indicator_divergence"] = "indicator_divergence"
    indicator: DivergenceIndicator
    direction: DivergenceDirection


# --- §8b #19: indicator crossover (FLO-359 Phase 8b commit 3 — STATEFUL) ---
#
# First stateful primitive. Fires on the FIRST tick where `indicator`
# crosses `threshold` in `direction`. State carried on the per-condition
# state row (`prev_value`, `prev_above_threshold`); equality at the
# threshold is treated as ambiguous and preserves the last definite
# state per RFC §3.1. Cold-start (no prev) seeds prev=current and
# reports no crossing on that tick — the documented one-tick false-
# negative window after a restart.

CrossoverIndicator = Literal["rsi", "macd_histogram", "stochastic"]


class IndicatorCrossover(_Cond):
    """Crossover detection. State-bearing — requires schema_version >= 2.

    `indicator` selects the LiveData accessor; `threshold` is the level
    that must be crossed; `direction` is the side the crossover travels.
    """
    type: Literal["indicator_crossover"] = "indicator_crossover"
    indicator: CrossoverIndicator
    tf: Timeframe
    direction: ComparisonOp
    threshold: float


# --- §8b #20: indicator was (FLO-359 Phase 8b commit 4 — STATEFUL) ---
#
# Recent-history sliding-window primitive. Fires while ANY of the last
# `within_bars` closed-bar values for `indicator` satisfied
# `op threshold`. Updated on bar-close (deduped via
# `prev_bar_close_at`); cold-start has empty history → False until the
# first bar boundary observed. CEO cap on `within_bars` is 20 (Q2
# decision) — bounds the per-row memory at ~20 floats.


class IndicatorWas(_Cond):
    """Did `indicator` value satisfy `op threshold` in any of the most
    recent `within_bars` closed bars on `tf`?

    Use case: 'RSI was below 30 within last 4 H1 bars' — true even if
    RSI has now recovered to 40+. Combined via AND with other
    primitives, expresses 'recovering from oversold' setups.
    """
    type: Literal["indicator_was"] = "indicator_was"
    indicator: CrossoverIndicator
    tf: Timeframe
    op: ComparisonOp
    threshold: float
    within_bars: int = Field(ge=1, le=20)


# --- §8b #21: price crossed level (FLO-359 Phase 8b commit 5 — STATEFUL) ---
#
# One-shot latch. Once price has crossed `level` in `direction`, the
# condition stays True for the rest of the plan's lifetime (until the
# plan transitions to a terminal status, at which point
# `state_cache.forget_plan` clears the row). Per CEO Q3 decision: no
# mid-plan reset — operators express "fire on each cross" via paired
# plans, not by resetting a single plan's latch.


class PriceCrossedLevel(_Cond):
    """Latch on the first price-vs-level crossing. State-bearing —
    requires schema_version >= 2.

    `direction` is the side the crossing must travel:
      "above" — price moves from at-or-below to strictly above `level`
      "below" — price moves from at-or-above to strictly below `level`

    Building block for sweep / tag detection: combine with
    `price_above`/`price_below` for "tagged-then-bounced" semantics.
    """
    type: Literal["price_crossed_level"] = "price_crossed_level"
    direction: ComparisonOp
    level: float


# --- Discriminated union ---

Condition = Annotated[
    Union[
        PriceAbove, PriceBelow,
        RSI, MACDHistogram, EMARelation, ATR,
        PriceAtSRZone, PriceAtFibonacci,
        ProfitPips, MFEReached, MAEReached, ProfitRetracedFromPeak,
        DurationExceeds, TimeBetween,
        # Phase 7.3 (FLO-355) — Cat A additions
        BollingerPosition, Stochastic, PriceAtPivot, IndicatorDivergence,
        # Phase 8b (FLO-359) — stateful additions
        IndicatorCrossover, IndicatorWas, PriceCrossedLevel,
    ],
    Field(discriminator="type"),
]


# =============================================================================
# Action primitives (RFC §2.6)
#
# Each action maps 1:1 to an executor call or a Snow-internal state
# transition. The `base` for priority resolution is determined by action
# type — see snow.priority (Phase 4). Phase 1 only enforces type
# well-formedness via the discriminated union.
# =============================================================================

class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False)


class ActionExecuteMarket(_Action):
    """Market-order entry. Applicable to entry reification only; NEVER used
    in management/exit contingencies. Validator enforces placement."""
    type: Literal["execute_market"] = "execute_market"


class ActionAdjustSL(_Action):
    type: Literal["adjust_sl"] = "adjust_sl"
    price: float


class ActionAdjustTP(_Action):
    type: Literal["adjust_tp"] = "adjust_tp"
    price: float


class ActionMoveSLToBreakeven(_Action):
    type: Literal["move_sl_to_breakeven"] = "move_sl_to_breakeven"
    offset_pips: float = 0.0


class ActionMoveSLToPrice(_Action):
    type: Literal["move_sl_to_price"] = "move_sl_to_price"
    price: float


class ActionTrailSL(_Action):
    type: Literal["trail_sl"] = "trail_sl"
    trail_pips: float = Field(gt=0)


class ActionCloseFull(_Action):
    type: Literal["close_full"] = "close_full"


class ActionClosePartial(_Action):
    type: Literal["close_partial"] = "close_partial"
    percent: float = Field(gt=0, lt=100, description="Percent of current position to close (0,100)")


class ActionCancelPlan(_Action):
    type: Literal["cancel_plan"] = "cancel_plan"


class ActionAlertFloki(_Action):
    type: Literal["alert_floki"] = "alert_floki"
    message: str = Field(max_length=500)


class ActionEscalateToFloki(_Action):
    type: Literal["escalate_to_floki"] = "escalate_to_floki"
    message: str = Field(max_length=500)


Action = Annotated[
    Union[
        ActionExecuteMarket,
        ActionAdjustSL, ActionAdjustTP, ActionMoveSLToBreakeven,
        ActionMoveSLToPrice, ActionTrailSL,
        ActionCloseFull, ActionClosePartial,
        ActionCancelPlan,
        ActionAlertFloki, ActionEscalateToFloki,
    ],
    Field(discriminator="type"),
]


# =============================================================================
# Contingency + guards
# =============================================================================

class ContingencyGuards(BaseModel):
    """Opt-in per-contingency guards (CTO decision CK-4).

    Enforced in snow.actions at dispatch time, NOT in executor.py.
    The executor remains permissive per FLO-200; guards are plan-level
    policy Floki chooses into.
    """
    model_config = ConfigDict(extra="forbid")

    only_if_tighter_sl: bool = False
    cooldown_seconds: int = Field(default=0, ge=0)
    min_mfe_pips_required: Optional[float] = Field(default=None, ge=0)
    max_adjustments_total: Optional[int] = Field(default=None, ge=1)


class Contingency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=40, min_length=1)
    priority: int = Field(default=5, ge=1, le=10,
                          description="Floki override (1-10). Default 5. "
                                      "Combined with action_base in snow.priority.")
    conditions: list[Condition] = Field(min_length=1, max_length=8)
    action: Action
    fires: ContingencyFires = ContingencyFires.ONCE
    state: ContingencyState = ContingencyState.ARMED
    fired_at: Optional[str] = None
    evaluated_count: int = Field(default=0, ge=0)
    guards: Optional[ContingencyGuards] = None

    @field_validator("fired_at")
    @classmethod
    def _utc_suffix(cls, v):
        if v is not None and not v.endswith("Z"):
            raise ValueError("fired_at must end with 'Z' (UTC)")
        return v


# =============================================================================
# Plan structural blocks
# =============================================================================

# -----------------------------------------------------------------------------
# Setup tagging vocabulary (FLO-366) — closed enums, validator-enforced.
# Required from schema_version >= 3 onward; v1/v2 plans omit them.
# -----------------------------------------------------------------------------

# 10 trading setups, mutually exclusive per plan.
SetupType = Literal[
    "breakout_range",
    "pullback_trend",
    "mean_reversion_extreme",
    "liquidity_sweep",
    "continuation_momentum",
    "news_reaction",
    "divergence_play",
    "paired_hedge",
    "structural_bounce",
    "session_open_break",
]

# Trend / range character — one and only one per plan.
TrendTag = Literal["trend_strong", "trend_weak", "range_tight", "range_wide"]

# Volatility regime — one per plan.
VolatilityTag = Literal["high_vol", "low_vol"]

# Higher-timeframe alignment vs the plan's direction — one per plan.
HtfTag = Literal["HTF_aligned", "HTF_counter", "HTF_neutral"]

# News / session flags — zero or more, with one mutual-exclusion rule
# (near_news ⊕ post_news, enforced by ContextTags model_validator below).
NewsSessionTag = Literal[
    "near_news",
    "post_news",
    "session_overlap",
    "session_thin",
]


class ContextTags(BaseModel):
    """Plan context tags (FLO-366). All fields except `news_session` are
    single-value Literals so contradictions are unrepresentable. The
    `news_session` list carries one explicit contradiction check
    (`near_news` and `post_news` are mutually exclusive)."""
    model_config = ConfigDict(extra="forbid")

    trend: TrendTag = Field(
        description="Trend / range character. trend_strong/weak vs "
                    "range_tight/wide are mutually exclusive by design.",
    )
    volatility: VolatilityTag = Field(
        description="Volatility regime: high_vol or low_vol.",
    )
    htf: HtfTag = Field(
        description="Higher-timeframe alignment relative to the plan's "
                    "direction: HTF_aligned, HTF_counter, or HTF_neutral.",
    )
    news_session: list[NewsSessionTag] = Field(
        default_factory=list,
        max_length=4,
        description="Zero or more news / session flags. `near_news` and "
                    "`post_news` are mutually exclusive.",
    )

    @model_validator(mode="after")
    def _check_news_session_consistency(self):
        ns = self.news_session
        if "near_news" in ns and "post_news" in ns:
            raise ValueError(
                "context_tags.news_session: 'near_news' is mutually "
                "exclusive with 'post_news' — pick one, not both."
            )
        # Reject duplicates so two of the same flag don't silently survive.
        if len(ns) != len(set(ns)):
            seen = set()
            dupes = sorted({x for x in ns if (x in seen or seen.add(x))})
            raise ValueError(
                f"context_tags.news_session: duplicate values not allowed "
                f"(duplicates: {dupes})."
            )
        return self


class PlanAnalysis(BaseModel):
    """Free-text rationale from Floki — audit trail, not evaluated.

    FLO-366 (schema_version >= 3): `setup_type`, `context_tags`, and
    `confidence_reason` become required. They stay Optional on the model
    so v1/v2 plans round-trip cleanly; the version-conditional check
    lives on `Plan` (which knows its own `schema_version`).
    """
    model_config = ConfigDict(extra="forbid")

    thesis: str = Field(max_length=2000, min_length=1)
    key_levels: list[float] = Field(default_factory=list, max_length=10)
    confidence: int = Field(ge=0, le=100)
    regime_assumed: Optional[str] = Field(default=None, max_length=40)

    # FLO-366: required from schema_version >= 3 (enforced on Plan).
    setup_type: Optional[SetupType] = Field(
        default=None,
        description="Setup family. One of 10 closed values; required for "
                    "schema_version >= 3.",
    )
    context_tags: Optional[ContextTags] = Field(
        default=None,
        description="Trend / volatility / HTF / news-session tags; "
                    "required for schema_version >= 3.",
    )
    confidence_reason: Optional[str] = Field(
        default=None,
        min_length=20,
        max_length=150,
        description="Free-text rationale supporting the confidence score "
                    "(20-150 chars). Required for schema_version >= 3.",
    )


class EntryBlock(BaseModel):
    """Entry spec — conditions + order params. The implicit entry action is
    a market order in `direction` at `volume` size. `initial_sl` and
    `initial_tp` are absolute prices."""
    model_config = ConfigDict(extra="forbid")

    direction: Direction
    volume: float = Field(gt=0, le=2.0, description="Lot size (0,2.0]; XAUUSD demo typical 0.01-0.1")
    conditions: list[Condition] = Field(min_length=1, max_length=8)
    initial_sl: float = Field(gt=0)
    initial_tp: float = Field(gt=0)
    reason_for_direct_action: Optional[str] = Field(default=None, max_length=500)


class EmergencyBlock(BaseModel):
    """Snow-level safeguards; evaluated loop-level, bypass contingency priority
    (RFC §11.5). NEVER use as a normal contingency."""
    model_config = ConfigDict(extra="forbid")

    max_loss_pips: float = Field(default=150.0, gt=0, le=1000)
    max_duration_minutes: int = Field(default=480, gt=0, le=10080)  # ≤7 days
    on_broker_error: Literal["alert_floki", "close_full", "cancel_plan"] = "alert_floki"


# =============================================================================
# Top-level Plan
# =============================================================================

PLAN_ID_PATTERN = r"^PLAN-\d{8}-\d{3}$"


class Plan(BaseModel):
    """Top-level Plan model. Submitted via `submit_plan_to_snow`; validated
    at submit-time by snow.validator.validate_plan; persisted in snow_plans
    as both a typed row AND `plan_json` (for schema evolution).
    """
    model_config = ConfigDict(extra="forbid")

    # Identity + versioning
    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    id: str = Field(pattern=PLAN_ID_PATTERN,
                    description="PLAN-YYYYMMDD-NNN; daily counter")
    created_by: Literal["floki"] = "floki"

    # Timestamps (Rule 22: UTC, Z-suffixed ISO-8601)
    created_at: str
    expires_at: Optional[str] = None
    entered_at: Optional[str] = None
    closed_at: Optional[str] = None

    # Lifecycle
    status: PlanStatus = PlanStatus.PENDING

    # Core spec
    analysis: PlanAnalysis
    entry: EntryBlock
    management: list[Contingency] = Field(default_factory=list, max_length=10)
    exit: list[Contingency] = Field(default_factory=list, max_length=10)
    emergency: EmergencyBlock = Field(default_factory=EmergencyBlock)

    # Outcome fields (populated as plan progresses)
    trade_ticket: Optional[int] = Field(default=None, gt=0)
    outcome_pips: Optional[float] = None
    outcome_usd: Optional[float] = None

    @field_validator("created_at", "expires_at", "entered_at", "closed_at")
    @classmethod
    def _utc_suffix(cls, v):
        if v is not None and not v.endswith("Z"):
            raise ValueError("all timestamps must end with 'Z' (UTC per Rule 22)")
        return v

    @model_validator(mode="after")
    def _check_v3_tagging_required(self):
        """FLO-366: from schema_version >= 3, plans MUST carry setup_type,
        context_tags, and confidence_reason on `analysis`. Older versions
        round-trip unchanged (forward-only enforcement)."""
        if self.schema_version >= 3:
            missing = []
            if self.analysis.setup_type is None:
                missing.append("setup_type")
            if self.analysis.context_tags is None:
                missing.append("context_tags")
            if self.analysis.confidence_reason is None:
                missing.append("confidence_reason")
            if missing:
                raise ValueError(
                    f"analysis: schema_version={self.schema_version} requires "
                    f"setup tagging — missing field(s): {', '.join(missing)}. "
                    f"Call get_snow_tags_reference() for the closed vocabulary."
                )
        return self


__all__ = [
    # Enums
    "PlanStatus", "ContingencyState", "ContingencyFires", "Direction",
    "Timeframe", "ComparisonOp", "EMARelationKind", "ZoneKind", "FibLevel",
    # Conditions
    "Condition",
    "PriceAbove", "PriceBelow",
    "RSI", "MACDHistogram", "EMARelation", "ATR",
    "PriceAtSRZone", "PriceAtFibonacci",
    "ProfitPips", "MFEReached", "MAEReached", "ProfitRetracedFromPeak",
    "DurationExceeds", "TimeBetween",
    # Actions
    "Action",
    "ActionExecuteMarket",
    "ActionAdjustSL", "ActionAdjustTP",
    "ActionMoveSLToBreakeven", "ActionMoveSLToPrice", "ActionTrailSL",
    "ActionCloseFull", "ActionClosePartial",
    "ActionCancelPlan", "ActionAlertFloki", "ActionEscalateToFloki",
    # Contingency
    "Contingency", "ContingencyGuards",
    # Plan
    "Plan", "PlanAnalysis", "EntryBlock", "EmergencyBlock",
    # FLO-366 setup tagging
    "ContextTags", "SetupType", "TrendTag", "VolatilityTag", "HtfTag",
    "NewsSessionTag",
    # Constants
    "PLAN_ID_PATTERN", "SCHEMA_VERSION",
]
