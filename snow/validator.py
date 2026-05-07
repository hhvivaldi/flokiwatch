"""Snow plan validator — business rules beyond Pydantic's type/range checks.

Spec: `data/_design/FLO-347_Snow_RFC_v1.md` §2, §3.4 (invariants),
§9.1 (submit-time validation contract).

Design: the schema-level constraints (field types, ranges, enum values,
discriminated unions) live in `snow.schema`. The validator here handles
cross-field and cross-contingency rules that Pydantic can't express
cleanly, plus the plan-level invariants the runtime relies on.

Invariant coverage (RFC §3.4):
  I1 — A plan has exactly one trade_ticket slot                  (schema)
  I2 — TRIGGERED transient ≤60s                                  (runtime)
  I3 — CLOSING transient ≤60s                                    (runtime)
  I4 — Emergency evaluated every tick                            (runtime)
  I5 — Effective priority ∈ [7, 228]                             (schema +
                                                                   validator)
  I6 — Atomic DB update of plan + trigger                        (Phase 2)
  I7 — UNIQUE live trade_ticket across plans                     (Phase 2)

Phase 1 scope per CTO:
  * Schema-level (Pydantic) — done by schema.py
  * Plus the business rules in `validate_plan` below:
      - schema_version ≤ code version
      - created_at / expires_at parseable + ordered
      - Entry SL/TP on correct sides for direction
      - Contingency names unique within plan (management + exit)
      - Action placement: execute_market forbidden in management/exit
      - time_between: start_utc ≠ end_utc; cross-midnight windows allowed
      - key_levels sanity for XAUUSD range (100 ≤ level ≤ 20000)
      - I5 precheck via the (future) priority formula — Phase 1 asserts
        that every contingency's (action, override) pair is representable.

Runtime invariants (I2, I3, I4, I6, I7) are enforced by the loop (Phase 4),
DB layer (Phase 2), and recovery.reconcile() (Phase 4). Out of Phase 1 scope.

NOTE: on Phase 2+ plan rehydration from disk, apply this same validator to
historical plans before handing them to the runtime. A plan that was valid
yesterday under SCHEMA_VERSION=N may fail today under N+1 — that's the
intended behaviour; treat the failure as a signal to quarantine or
migrate, not to bypass. Do not relax the checks below for old data.

Usage:
    from snow.validator import validate_plan
    ok, plan_or_none, errors = validate_plan(plan_dict)
    if not ok:
        return {"success": False, "validation_errors": errors}
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any, Optional

from pydantic import ValidationError

from . import SCHEMA_VERSION
from .schema import (
    ActionCancelPlan,
    ActionExecuteMarket,
    Contingency,
    Direction,
    Plan,
)


# --- XAUUSD-specific sanity bounds for price/level fields -------------------
# Not enforced on every numeric field in schema.py to keep the schema
# symbol-agnostic; here we apply the project's current symbol envelope.
_MIN_PRICE_XAUUSD = 100.0
# 20000 chosen as XAUUSD ceiling: gold has never printed above ~4000 and the
# 5x headroom catches obvious typos (extra zero → 47000) without false-
# positiving any realistic quote. Widen if the underlying runs past ~5000.
_MAX_PRICE_XAUUSD = 20000.0


def _parse_utc_z(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 UTC-Z timestamp. Returns None if unparseable.

    Pydantic has already confirmed the 'Z' suffix at schema level; here we
    additionally confirm the rest is a valid datetime string.
    """
    if not ts or not ts.endswith("Z"):
        return None
    try:
        return _dt.datetime.fromisoformat(ts[:-1]).replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def _check_timestamps(plan: Plan) -> list[str]:
    errors: list[str] = []
    created = _parse_utc_z(plan.created_at)
    if created is None:
        errors.append(f"created_at is not a valid ISO-8601 UTC-Z timestamp: {plan.created_at!r}")
        return errors  # can't validate downstream without created
    if plan.expires_at:
        expires = _parse_utc_z(plan.expires_at)
        if expires is None:
            errors.append(f"expires_at is not parseable: {plan.expires_at!r}")
        elif expires <= created:
            errors.append(
                f"expires_at ({plan.expires_at}) must be strictly after "
                f"created_at ({plan.created_at})"
            )
    return errors


def _check_entry_sl_tp(plan: Plan) -> list[str]:
    errors: list[str] = []
    entry = plan.entry
    if entry.direction == Direction.BUY:
        # BUY: SL must be below TP (SL below entry, TP above)
        if entry.initial_sl >= entry.initial_tp:
            errors.append(
                f"BUY entry: initial_sl ({entry.initial_sl}) must be strictly "
                f"below initial_tp ({entry.initial_tp})"
            )
    else:
        # SELL: SL must be above TP
        if entry.initial_sl <= entry.initial_tp:
            errors.append(
                f"SELL entry: initial_sl ({entry.initial_sl}) must be strictly "
                f"above initial_tp ({entry.initial_tp})"
            )
    return errors


def _check_price_bounds(plan: Plan) -> list[str]:
    """Sanity-check absolute prices (SL/TP, key_levels, action prices) are
    in a plausible XAUUSD envelope. Catches typos like `47.30` vs `4730`.
    """
    errors: list[str] = []

    def _check(name: str, value: float) -> None:
        if not (_MIN_PRICE_XAUUSD <= value <= _MAX_PRICE_XAUUSD):
            errors.append(
                f"{name}={value} outside XAUUSD sanity envelope "
                f"[{_MIN_PRICE_XAUUSD}, {_MAX_PRICE_XAUUSD}]"
            )

    _check("entry.initial_sl", plan.entry.initial_sl)
    _check("entry.initial_tp", plan.entry.initial_tp)
    for i, lvl in enumerate(plan.analysis.key_levels):
        _check(f"analysis.key_levels[{i}]", lvl)

    # Action prices (where the action carries an absolute price field)
    for block_name, block in (("management", plan.management), ("exit", plan.exit)):
        for ci, cont in enumerate(block):
            for attr in ("price",):
                val = getattr(cont.action, attr, None)
                if val is not None:
                    _check(f"{block_name}[{ci}].action.{attr}", float(val))
    return errors


def _check_contingency_names_unique(plan: Plan) -> list[str]:
    """Names must be unique across management + exit combined (so a reason
    string uniquely identifies a contingency for snow_triggers audit rows).
    """
    seen: set[str] = set()
    dups: set[str] = set()
    for block in (plan.management, plan.exit):
        for c in block:
            if c.name in seen:
                dups.add(c.name)
            seen.add(c.name)
    if dups:
        return [f"contingency name(s) duplicated across management+exit: {sorted(dups)}"]
    return []


def _check_execute_market_placement(plan: Plan) -> list[str]:
    """`execute_market` is for the implicit entry action only — never
    inside a user-authored management/exit contingency. Floki encodes
    entry via EntryBlock; Snow reifies it as a market call at runtime."""
    errors: list[str] = []
    for block_name, block in (("management", plan.management), ("exit", plan.exit)):
        for ci, c in enumerate(block):
            if isinstance(c.action, ActionExecuteMarket):
                errors.append(
                    f"{block_name}[{ci}] ({c.name!r}): `execute_market` action "
                    f"not allowed in {block_name}; entry reification only"
                )
    return errors


def _check_cancel_plan_placement(plan: Plan) -> list[str]:
    """`cancel_plan` must NOT appear as a management/exit contingency action.

    FLO-347 Phase 5b decision: cancel_plan is reachable only via the Floki
    tool `cancel_plan` (Phase 6), which calls the DB layer directly.
    Allowing it as a contingency action would create two code paths to the
    same state transition (Snow's fire dispatcher + Floki's tool) with
    subtly different audit trails. Keeping it a Floki-tool-only action
    makes the boundary clean.
    """
    errors: list[str] = []
    for block_name, block in (("management", plan.management), ("exit", plan.exit)):
        for ci, c in enumerate(block):
            if isinstance(c.action, ActionCancelPlan):
                errors.append(
                    f"{block_name}[{ci}] ({c.name!r}): `cancel_plan` action "
                    f"not allowed in {block_name}; invoke via Floki's "
                    f"cancel_plan tool instead"
                )
    return errors


def _check_time_between_conditions(plan: Plan) -> list[str]:
    """`TimeBetween.start_utc` should differ from `end_utc` (zero-window is
    always false); cross-midnight windows (end < start) are ALLOWED and
    interpreted as wrap-around at runtime.

    The loop below iterates three kinds of container in sequence:
    `plan.entry` (single EntryBlock wrapped in a list) and `plan.management`
    / `plan.exit` (already lists of Contingency). EntryBlock and Contingency
    are not a common base class — we duck-type on `.conditions`, which both
    expose. If a future block grows conditions, adding it here is a
    one-line edit.
    """
    errors: list[str] = []
    for block_name, block in [
        ("entry", [plan.entry]),
        ("management", plan.management),
        ("exit", plan.exit),
    ]:
        for ci, container in enumerate(block):
            for condi, cond in enumerate(container.conditions):
                if cond.type == "time_between":
                    if cond.start_utc == cond.end_utc:
                        errors.append(
                            f"{block_name}[{ci}].conditions[{condi}] time_between: "
                            f"start_utc == end_utc ({cond.start_utc}); zero-width window"
                        )
    return errors


def _check_schema_version(plan: Plan) -> list[str]:
    # SCHEMA_VERSION bumps are breaking changes — older code cannot safely
    # interpret plans from newer schemas (new fields/semantics). Refuse
    # rather than silently ignoring unknown structure.
    if plan.schema_version > SCHEMA_VERSION:
        return [
            f"schema_version={plan.schema_version} is newer than this code's "
            f"SCHEMA_VERSION={SCHEMA_VERSION} (breaking change); upgrade Snow "
            f"or have Floki emit a plan at the current schema version"
        ]
    return []


# Stateful primitive type-strings introduced in schema_version=2 (FLO-359
# Phase 8b). Defined as a string set rather than imported from
# `snow.schema` so this gate ships in commit 1 ahead of the primitive
# class definitions (commits 3-5). A v1 plan that references any of
# these is rejected up-front; a v2 plan reaches the discriminated-union
# parser, which itself rejects unknown types until the matching commit
# lands.
_STATEFUL_PRIMITIVES: frozenset[str] = frozenset({
    "indicator_crossover",   # commit 3
    "indicator_was",         # commit 4
    "price_crossed_level",   # commit 5 (placeholder until that commit lands)
})


def _iter_plan_conditions(plan: Plan):
    """Yield (block_label, condition_index, condition) for every condition
    on the plan."""
    for ci, c in enumerate(plan.entry.conditions):
        yield "entry", ci, c
    for mi, mgmt in enumerate(plan.management):
        for ci, c in enumerate(mgmt.conditions):
            yield f"management[{mi}]", ci, c
    for ei, ex in enumerate(plan.exit):
        for ci, c in enumerate(ex.conditions):
            yield f"exit[{ei}]", ci, c


# =============================================================================
# FLO-383 — management threshold sanity-floor (condition expressiveness)
# =============================================================================

# Pip threshold below which a profit_pips-only management trigger is
# considered noise-floor and therefore mathematically guaranteed to
# scratch under normal price oscillation. XAUUSD 30 pips ≈ 0.07% at
# 4500 spot — under typical M5 ATR + spread + slippage. Empirical
# basis: PLAN-007 (322 pip MFE → -0.4 outcome) had lock_be_at_10
# trigger, fired correctly, then broker SL whipsawed the BE-locked
# position. The fix is to require management triggers to either
# (a) wait for a meaningful absolute advance (>= floor), or (b) use
# peak-relative conditions that adapt to volatility automatically.
_MANAGEMENT_NOISE_FLOOR_PIPS: float = 30.0

# Profit-pips comparison ops that imply a "fire when profit reaches
# this threshold" trigger. The "below" / "lte" ops describe the
# inverse semantic ("fire when profit drops below X") which is a
# different shape — those are typically protective triggers and
# don't fall under the noise-floor concern.
_PROFIT_PIPS_TRIGGER_OPS: frozenset[str] = frozenset({"above", "gte", "gt"})


def _condition_avoids_noise_floor(c) -> bool:
    """Return True when this condition either (a) provides peak-
    relative semantics that adapt to volatility (mfe_reached,
    profit_retraced_from_peak), or (b) is a profit_pips trigger
    above the sanity floor, or (c) is any non-profit-pips condition
    type (indicator-based, structural, time, etc. — domain-aware).
    Returns False ONLY when the condition is a low-threshold
    profit_pips trigger.
    """
    ctype = getattr(c, "type", None)
    if ctype == "profit_pips":
        op = str(getattr(c, "op", "") or "").lower()
        threshold = float(getattr(c, "threshold", 0.0) or 0.0)
        if op in _PROFIT_PIPS_TRIGGER_OPS and threshold < _MANAGEMENT_NOISE_FLOOR_PIPS:
            return False
        return True
    # All other condition types qualify — including peak-relative
    # ones (mfe_reached, profit_retraced_from_peak) and any indicator
    # / structural / time-based conditions.
    return True


def _check_management_threshold_floor(plan: Plan) -> list[str]:
    """Enforce condition-expressiveness on management contingencies.

    A management contingency QUALIFIES if it has at least one
    non-noise-floor condition. A plan PASSES if at least one
    management contingency qualifies (or management is empty).

    Plans where ALL non-empty management contingencies trigger only
    on low-threshold profit_pips are rejected because BE-locked at
    noise level mathematically scratches under normal oscillation
    (PLAN-007 evidence). Floki retains agency on which qualifying
    primitive to use — the validator only enforces the floor.
    """
    if not plan.management:
        return []
    qualifying = []
    for mi, mgmt in enumerate(plan.management):
        # A contingency qualifies if ANY of its conditions avoids
        # the noise floor. A multi-condition trigger that AND-gates
        # a low profit_pips with an indicator condition is fine —
        # the indicator gate prevents premature noise-fire.
        any_qualifying = any(
            _condition_avoids_noise_floor(c) for c in mgmt.conditions
        )
        qualifying.append((mi, mgmt.name, any_qualifying))
    if any(q for _, _, q in qualifying):
        return []
    # All management contingencies are noise-floor-violating. Reject
    # with a message that names the alternatives so Floki can revise
    # without guessing.
    names = ", ".join(f"{n!r}" for _, n, _ in qualifying)
    return [
        f"management: every contingency ({names}) triggers only on "
        f"profit_pips below the {int(_MANAGEMENT_NOISE_FLOOR_PIPS)}-pip "
        f"noise floor. XAUUSD {int(_MANAGEMENT_NOISE_FLOOR_PIPS)} pips "
        f"≈ 0.07% — below typical M5 ATR + spread + slippage, so a "
        f"BE-or-similar trigger at this level is statistically "
        f"guaranteed to scratch under routine pullback. Use at least "
        f"ONE of: (a) profit_pips with threshold >= "
        f"{int(_MANAGEMENT_NOISE_FLOOR_PIPS)} pips, (b) mfe_reached "
        f"(peak-relative), (c) profit_retraced_from_peak (give-back "
        f"guard), or (d) AND-gate the low profit_pips with an "
        f"indicator/structural/time condition."
    ]


# =============================================================================
# FLO-Path4 — minimum entry conditions (narrow gate)
# =============================================================================

# Empirical basis: PLAN-011 (and similar) submitted with a single
# `price_above` entry condition — minimal-compliance schema fill that
# produces under-conditioned setups. Forcing >= 2 entry conditions
# catches the path-of-least-resistance pattern without dictating which
# primitives Floki uses. Floki retains full agency on which 2+ conditions
# qualify the entry.
#
# Implemented as a business rule (not a Pydantic schema change) so
# existing test fixtures with 1-condition plans remain valid for
# negative-test-case construction (e.g. Pydantic-level validation
# tests that build minimal valid plans).
_MIN_ENTRY_CONDITIONS: int = 2


def _check_min_entry_conditions(plan: Plan) -> list[str]:
    """Reject entry blocks with fewer than `_MIN_ENTRY_CONDITIONS`
    conditions. Single-condition entries are typically minimal-
    compliance schema fills (e.g. only `price_above 4575`) that do
    not reflect the multi-indicator confluence the plan model is
    designed to express.

    Floki retains agency on which conditions qualify -- the validator
    only enforces the count floor.
    """
    n = len(plan.entry.conditions)
    if n < _MIN_ENTRY_CONDITIONS:
        return [
            f"entry: requires at least {_MIN_ENTRY_CONDITIONS} conditions; "
            f"got {n}. Single-condition entries are typically minimal-"
            f"compliance schema fills that bypass the multi-indicator "
            f"confluence the plan is designed to express. Add a second "
            f"condition that gates the entry meaningfully (indicator "
            f"threshold, structural confluence, time window, etc.)."
        ]
    return []


# FLO-419 (CEO 2026-05-04): structural / level-proximity primitives that
# resolve their target price from live SemanticCache data at trigger
# time. Banned in entry.conditions because the trigger price would
# silently shift if Brain re-ranks the nearest zone / pivot / fib level.
# The plan must commit to the exact number Floki authored — if the
# structure shifts, author a new plan next cycle. These primitives
# remain permitted in exit / management blocks where live-structure
# semantics are intended.
_DYNAMIC_LEVEL_ENTRY_BAN: frozenset[str] = frozenset({
    "price_at_sr_zone",
    "price_at_pivot",
    "price_at_fibonacci",
})


def _check_no_dynamic_level_in_entry(plan: Plan) -> list[str]:
    """Reject entry conditions that resolve their target price from the
    live SemanticCache (price_at_sr_zone / price_at_pivot /
    price_at_fibonacci). Trigger price would silently move if Brain's
    zone / pivot / fib ranking changes between authoring and fire.
    Force fixed price_above / price_below with the exact level Floki's
    thesis names.

    Empirical motivation (PLAN-20260503-001, 2026-05-04): authored with
    nearest-support cluster at 4605-4612 in mind, used `price_at_sr_zone
    zone_type=support tolerance_pips=8` (no fixed price). If
    support_resistance later demoted those zones and surfaced 4585 as
    the new nearest support, the plan would have fired at a price with
    no thesis behind it.
    """
    bad = [
        (i, getattr(c, "type", None))
        for i, c in enumerate(plan.entry.conditions)
        if getattr(c, "type", None) in _DYNAMIC_LEVEL_ENTRY_BAN
    ]
    if not bad:
        return []
    # Report all offenders, not just the first — saves a re-submit cycle.
    msgs = []
    for i, t in bad:
        msgs.append(
            f"entry.conditions[{i}]: {t!r} resolves its target price "
            f"from the live S/R / pivot / fib cache at trigger time "
            f"and would silently shift if Brain re-ranks the nearest "
            f"level. Plans must commit to the exact price your thesis "
            f"names. Replace with `price_above {{level: N}}` or "
            f"`price_below {{level: N}}` using the literal number from "
            f"your analysis. {t!r} remains permitted in exit and "
            f"management blocks where live-structure semantics apply."
        )
    return msgs


def _check_exit_geometry_vs_sl(plan: Plan) -> list[str]:
    """FLO-419 (CEO 2026-05-04): reject plans whose exit contingencies use
    a price-side trigger positioned beyond the broker SL — making the exit
    geometrically unreachable.

    Empirical motivation: PLAN-20260504-009 (BUY entry 4574, SL 4543, exit
    price_below 4525 = 18 USD past SL) lost -$65 with the thesis_invalidation
    exit never armed. Audit of last 10 closed plans showed 4 broken plans
    (009, 010, 012, 002) plus 2 boundary cases (006, 031) — six of ten with
    exits incapable of firing before broker SL.

    Rule:
      BUY plan + exit `price_below level` : level MUST be > initial_sl
      SELL plan + exit `price_above level`: level MUST be < initial_sl

    The opposite shapes (BUY+price_above, SELL+price_below) are TP-side
    triggers (testing favorable price action, not invalidation) and have
    no SL-ordering constraint — skipped here.

    Boundary case `level == initial_sl` is rejected (strict inequality)
    because it provides no earlier capture than the broker SL itself.

    Compound exit conditions: only the price_above/price_below leg(s) are
    geometry-checked. Other legs (rsi, profit_pips, duration_exceeds, etc.)
    are evaluated by their own primitives and don't depend on price
    reaching SL first.
    """
    errors: list[str] = []
    # plan.entry.direction is a Direction enum; .value gives "BUY"/"SELL".
    # Belt-and-braces: tolerate raw string too (defensive).
    _d = getattr(plan.entry, "direction", None)
    direction = str(getattr(_d, "value", _d) or "").upper()
    if direction not in ("BUY", "SELL"):
        return errors  # let other validators flag the bad direction
    sl = float(plan.entry.initial_sl)

    for ei, ex in enumerate(plan.exit or []):
        name = getattr(ex, "name", f"exit[{ei}]")
        for c in (ex.conditions or []):
            ctype = getattr(c, "type", None)
            if ctype not in ("price_above", "price_below"):
                continue

            level = float(getattr(c, "level"))

            if direction == "BUY" and ctype == "price_below":
                if level <= sl:
                    errors.append(
                        f"exit[{ei}] ({name!r}): price_below {level} is "
                        f"AT OR BELOW the SL {sl}. For a BUY plan, the "
                        f"broker SL fires when price drops to {sl}; this "
                        f"exit's trigger at {level} would never be reached "
                        f"BEFORE the broker SL. Set the exit level ABOVE "
                        f"the SL (typical: 1-2 USD above SL = 10-20 pips "
                        f"buffer) so the exit fires first, giving Snow the "
                        f"chance to close on thesis break before the broker "
                        f"hits SL. Or remove this exit if the broker SL is "
                        f"the intended invalidation level."
                    )
            elif direction == "SELL" and ctype == "price_above":
                if level >= sl:
                    errors.append(
                        f"exit[{ei}] ({name!r}): price_above {level} is "
                        f"AT OR ABOVE the SL {sl}. For a SELL plan, the "
                        f"broker SL fires when price rises to {sl}; this "
                        f"exit's trigger at {level} would never be reached "
                        f"BEFORE the broker SL. Set the exit level BELOW "
                        f"the SL (typical: 1-2 USD below SL = 10-20 pips "
                        f"buffer) so the exit fires first, giving Snow the "
                        f"chance to close on thesis break before the broker "
                        f"hits SL. Or remove this exit if the broker SL is "
                        f"the intended invalidation level."
                    )
    return errors


# =============================================================================
# FLO-391 / FLO-392 — management primitive reachability (semantic coherence)
# =============================================================================
#
# FLO-391 ships the geometric reachability gate using `|TP - SL|` as a
# conservative upper bound (no false positives, but loose).
#
# FLO-392 tightens the bound to `|TP - entry_price|` when the plan
# carries the optional `entry_price` hint. This catches the structural
# pattern where the trigger fires too close to TP for management to do
# anything useful — the live PLAN-20260428-011 case (entry=4578.42,
# TP=4604, mfe_reached=200; conservative bound 520 pips passes, tight
# bound 256 pips with 0.75 buffer = 192 → REJECT).
#
# Buffer: when the tight bound applies, the trigger must fire with at
# least 25% of the TP envelope still ahead of it — gives the management
# action (BE move, trail) meaningful room to act before TP closes the
# trade. The conservative fallback uses no buffer (FLO-391 boundary
# semantics preserved for plans that don't carry entry_price).

# PIP_SIZE for XAUUSD. Mirrors `snow.evaluators.context.PIP_SIZE` (0.1)
# but defined locally to avoid coupling the validator to the runtime
# evaluator package — validator runs at submit time, before evaluator
# context exists.
_PIP_SIZE_XAUUSD: float = 0.1

# Fraction of TP-from-entry envelope the trigger threshold must NOT
# exceed when the tight bound applies. 0.75 leaves 25% remaining for
# the management action to operate before TP closes the trade.
# PLAN-011 verification: tp_from_entry=256, threshold=200, bound=192 →
# 200 > 192 → REJECT.
_REACHABILITY_BUFFER_PCT: float = 0.75

# Trigger ops that imply a "fire when X reaches/exceeds threshold"
# semantic. The inverse semantic ("below/lte/lt X") describes a
# protective drop-trigger and has no upper-bound reachability concern.
_REACH_TRIGGER_OPS: frozenset[str] = frozenset({"above", "gte", "gt"})


def _check_entry_price_in_range(plan: Plan) -> list[str]:
    """FLO-392: when `entry_price` is provided, it must lie strictly
    between `initial_sl` and `initial_tp` per direction. A degenerate
    value (entry at SL, at TP, or outside the corridor) would corrupt
    the TP-from-entry bound calculation and silently weaken the
    reachability gate.

    `entry_price` is None → no check (fallback path applies).
    """
    ep = plan.entry.entry_price
    if ep is None:
        return []
    sl = plan.entry.initial_sl
    tp = plan.entry.initial_tp
    if plan.entry.direction == Direction.BUY:
        if not (sl < ep < tp):
            return [
                f"entry.entry_price={ep} must lie strictly between "
                f"initial_sl ({sl}) and initial_tp ({tp}) for BUY "
                f"(SL < entry_price < TP)"
            ]
    else:
        if not (tp < ep < sl):
            return [
                f"entry.entry_price={ep} must lie strictly between "
                f"initial_tp ({tp}) and initial_sl ({sl}) for SELL "
                f"(TP < entry_price < SL)"
            ]
    return []


def _plan_max_profit_pips(plan: Plan) -> float:
    """Upper bound on profit-pip distance achievable by this plan.

    FLO-392 (tight): when `plan.entry.entry_price` is provided,
    bound = |initial_tp - entry_price| / pip_size. Exact TP distance
    from intended entry. The 0.75 buffer is applied at the gate
    callsite (`_check_management_reachability`), not here.

    FLO-391 (conservative fallback): when `entry_price` is None, fall
    back to |initial_tp - initial_sl| / pip_size. Hard upper bound; no
    buffer applied at the callsite (preserves FLO-391 boundary
    semantics for plans not yet emitting entry_price).
    """
    ep = plan.entry.entry_price
    tp = plan.entry.initial_tp
    if ep is not None:
        return abs(tp - ep) / _PIP_SIZE_XAUUSD
    return abs(tp - plan.entry.initial_sl) / _PIP_SIZE_XAUUSD


def _plan_bound_mode(plan: Plan) -> str:
    """'tight' when entry_price is provided (FLO-392), else 'conservative'
    (FLO-391). Used by the gate to decide whether the buffer applies."""
    return "tight" if plan.entry.entry_price is not None else "conservative"


def _check_management_reachability(plan: Plan) -> list[str]:
    """Reject management triggers whose threshold provably cannot fire
    before TP closes the trade (FLO-391 conservative bound), or whose
    threshold leaves no room for management to act before TP (FLO-392
    tight bound + 0.75 buffer).

    Empirical basis: PLAN-20260428-011 shipped with `mfe_reached
    pips=200` against entry=4578.42, TP=4604 (256 pips from entry).
    Under the conservative |TP-SL| bound (520 pips) the trigger passes
    but is structurally useless — fires only in the last 56 pips before
    TP, leaving no room for the trail action to do anything. CEO had
    to defend SL manually.

    Affected primitives:
      * `mfe_reached pips=X`: peak-relative; gated.
      * `profit_pips op∈{above,gte,gt} threshold=X`: profit-direction
        trigger; gated.

    `profit_retraced_from_peak` is intentionally NOT gated — retracement
    threshold is independent of absolute peak.

    Floki retains agency on threshold values. Validator only rejects
    geometrically/operationally unreachable thresholds.
    """
    if not plan.management:
        return []
    max_pips = _plan_max_profit_pips(plan)
    mode = _plan_bound_mode(plan)
    if max_pips <= 0:
        # SL == TP (degenerate); _check_entry_sl_tp already rejects.
        return []
    if mode == "tight":
        # Buffer applies: trigger must leave 25% of envelope.
        effective_bound = max_pips * _REACHABILITY_BUFFER_PCT
        bound_label = (
            f"{effective_bound:g} pips (= {max_pips:g} pips TP-from-entry "
            f"× {_REACHABILITY_BUFFER_PCT} buffer)"
        )
    else:
        # Conservative fallback: no buffer (FLO-391 semantics preserved).
        effective_bound = max_pips
        bound_label = (
            f"{max_pips:g} pips (= |initial_tp - initial_sl| / pip_size; "
            f"declare entry.entry_price for tighter FLO-392 bound)"
        )
    errors: list[str] = []
    for mi, mgmt in enumerate(plan.management):
        for ci, c in enumerate(mgmt.conditions):
            ctype = getattr(c, "type", None)
            threshold_pips: Optional[float] = None
            if ctype == "mfe_reached":
                threshold_pips = float(getattr(c, "pips", 0.0) or 0.0)
            elif ctype == "profit_pips":
                op = str(getattr(c, "op", "") or "").lower()
                if op in _REACH_TRIGGER_OPS:
                    threshold_pips = float(
                        getattr(c, "threshold", 0.0) or 0.0
                    )
            if threshold_pips is None or threshold_pips <= 0:
                continue
            if threshold_pips > effective_bound:
                errors.append(
                    f"management[{mi}] ({mgmt.name!r}).conditions[{ci}] "
                    f"({ctype}): threshold {threshold_pips:g} pips exceeds "
                    f"the plan's reachability bound {bound_label}. Trigger "
                    f"is unreachable / leaves no room for management to "
                    f"act before TP closes the trade. Either lower the "
                    f"trigger threshold or widen TP."
                )
    return errors


_MGMT_BE_FLOOR_PIPS: int = 100

# FLO-419 Phase 2 (CEO directive 2026-05-01) — minimum analysis.confidence for
# any submitted plan. Empirical bucketing of 15 Gemini-era executed plans:
#   65-69%   n=3   wins=0   net -$17.86
#   70-74%   n=4   wins=0   net -$30.08
#   75-79%   n=8   wins=3   net +$8.98
#
# Floor was set to 75 for Gemini's calibration. Lowered to 70 (CEO 2026-05-01
# pre-Claude switch) to accommodate Claude Opus 4.6's more cautious calibration
# — Claude tends to author at lower confidence values for the same setup
# quality, and the Gemini-era 75% bucket data is not directly comparable. The
# 70-74% bucket showed 0/4 wins on Gemini; we accept that risk for this
# evaluation period and will revisit empirically after 20-30 Claude-authored
# trades land. Combined with the prompt's 50%-ceiling rule for concerns-named-
# failure-mode plans, the floor still auto-rejects them (50 < 70).
_CONFIDENCE_FLOOR: int = 70


def _check_confidence_floor(plan: Plan) -> list[str]:
    """FLO-419 Phase 2 hard gate: reject plans whose authored confidence
    is below the floor. Replaces the prompt-only suggestion that Floki
    should self-cap; empirical observation through 2026-05-01 showed the
    prompt rule was loaded but routinely violated (PLAN-014/022/035/037
    all authored above 50% with concerns naming the failure mode). A
    code gate cannot be ignored.

    Combined with the prompt's THESIS-VS-CONCERNS CONFLICT CHECK rule
    (50% ceiling when concerns describe the failure mode), every
    concerns-named-failure plan is now auto-rejected (50 < 75). Floki
    must either remove the concern (because he resolved it) or not
    submit the plan."""
    conf = int(plan.analysis.confidence or 0)
    if conf < _CONFIDENCE_FLOOR:
        return [(
            f"Plan rejected: confidence {conf}% is below the "
            f"{int(_CONFIDENCE_FLOOR)}% minimum. Only submit plans you "
            f"strongly believe in."
        )]
    return []


def _flo424_safety_circuit_active() -> bool:
    """FLO-424 — temporary safety circuit gate.

    Returns True while the circuit is active (i.e., breakout_range plans
    should be rejected). Reads `config.FLO424_SAFETY_CIRCUIT_UNTIL`
    (env-overridable) and compares to current UTC time. If the constant
    is missing or unparseable, fail-safe: returns False (circuit
    disabled) so a misconfiguration cannot accidentally block all plans.

    Designed to self-disable after the until-timestamp passes, so a
    forgotten circuit does not silently block authoring forever.
    """
    try:
        import config as _cfg
        until_str = getattr(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", None)
        if not until_str:
            return False
        until = _parse_utc_z(until_str)
        if until is None:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        return now < until
    except Exception:
        return False


def _check_flo424_safety_circuit(plan: Plan) -> list[str]:
    """FLO-424 — temporary safety circuit for breakout_range setup_type.

    Empirical 15-day window (Apr 23 – May 7, 2026):
      breakout_range:        9 fired, 22% WR, -236p net
      continuation_momentum: 10 fired, 70% WR, +349p net (UNCHANGED)

    While the safety circuit is active (config.FLO424_SAFETY_CIRCUIT_UNTIL),
    breakout_range plans are rejected at submit time. Floki's trend-
    continuation theses should re-author as continuation_momentum, which
    is profitable on the same window with the same triggers.

    Other setup_types (pullback_trend, structural_bounce, etc.) are NOT
    affected — only breakout_range is gated. The circuit self-disables
    once now() >= FLO424_SAFETY_CIRCUIT_UNTIL.
    """
    if not _flo424_safety_circuit_active():
        return []
    setup_type = getattr(plan.analysis, "setup_type", None)
    if setup_type != "breakout_range":
        return []
    try:
        import config as _cfg
        until = getattr(_cfg, "FLO424_SAFETY_CIRCUIT_UNTIL", "(unknown)")
    except Exception:
        until = "(unknown)"
    return [(
        f"Plan rejected: setup_type='breakout_range' is temporarily "
        f"disabled by the FLO-424 safety circuit until {until}. "
        f"Empirical 15-day window: 9 fired plans, 22% WR, -236p net. "
        f"continuation_momentum on the same window was 70% WR (+349p). "
        f"Re-author as setup_type='continuation_momentum' if your thesis "
        f"is trend-continuation; otherwise wait for the circuit to expire "
        f"or for the post-review formula update. Other setup_types "
        f"(pullback_trend, structural_bounce, mean_reversion_extreme, "
        f"liquidity_sweep, divergence_play, paired_hedge, news_reaction, "
        f"session_open_break, continuation_momentum) are unaffected."
    )]


def _flo425_geometry_gate_active() -> bool:
    """FLO-425 §16f — temporary anti-smuggling geometry gate.

    Returns True while the gate is active. Reads
    `config.FLO425_GEOMETRY_GATE_UNTIL` (env-overridable). If the
    constant is missing or unparseable, fail-safe: returns False
    (gate disabled) so a misconfiguration cannot accidentally
    block all plans. Mirrors the FLO-424 pattern.
    """
    try:
        import config as _cfg
        until_str = getattr(_cfg, "FLO425_GEOMETRY_GATE_UNTIL", None)
        if not until_str:
            return False
        until = _parse_utc_z(until_str)
        if until is None:
            return False
        now = _dt.datetime.now(_dt.timezone.utc)
        return now < until
    except Exception:
        return False


def _flo425_get_current_price() -> Optional[float]:
    """Read XAUUSD bid via the thread-safe MT5 proxy. Returns None on
    any failure — the caller MUST treat None as fail-open (do not
    block). Bounded work: one tick read, no candle fetch.
    """
    try:
        from mt5_safe import mt5, mt5_lock
        with mt5_lock:
            if not mt5.initialize():
                return None
            tick = mt5.symbol_info_tick("XAUUSD")
        if tick is None:
            return None
        bid = float(getattr(tick, "bid", 0) or 0)
        return bid if bid > 0 else None
    except Exception:
        return None


def _check_flo425_geometry_gate(plan: Plan) -> list[str]:
    """FLO-425 §16f — anti-smuggling geometry gate.

    Plan-independent validator rule. Rejects entries whose trigger
    price sits more than FLO425_GEOMETRY_GATE_PIPS above current
    price for BUY (or below current for SELL), regardless of
    setup_type. Catches the "chase" geometry exemplified by
    PLAN-20260507-007 (BUY 4756 vs current 4734.49 = +215p above)
    that was smuggled as continuation_momentum after FLO-424
    disabled breakout_range.

    Plan-independent by design: setup_type is no longer the primary
    truth (FLO-425 §19). The gate operates on entry geometry only.

    Pullback shapes (BUY entry below current, SELL entry above
    current) are unaffected — the geometric definition of "chase"
    does not apply.

    Does NOT catch at-current spike entries (PLAN-20260507-004's
    class). That requires acceptance semantics (FLO-425 §17), out
    of scope for this gate.

    Fail-open posture:
      - MT5 read fails → log+allow (do not block on infra failure)
      - threshold env malformed → already fail-safe at config load;
        if config attribute missing, allow
      - gate_active_until past → no-op (gate inactive)
    """
    if not _flo425_geometry_gate_active():
        return []

    try:
        import config as _cfg
        threshold_pips = int(getattr(_cfg, "FLO425_GEOMETRY_GATE_PIPS", 0) or 0)
        until_str = getattr(_cfg, "FLO425_GEOMETRY_GATE_UNTIL", "(unknown)")
    except Exception:
        return []

    if threshold_pips <= 0:
        return []  # disabled via env (FLO425_GEOMETRY_GATE_PIPS=0)

    direction = getattr(plan.entry, "direction", None)
    entry_price = getattr(plan.entry, "entry_price", None)
    setup_type = getattr(plan.analysis, "setup_type", None)
    if direction not in ("BUY", "SELL") or entry_price is None:
        return []  # malformed; defensive — schema layer should have caught

    current_price = _flo425_get_current_price()
    if current_price is None:
        # Fail-open. The validator does not block on infra failure.
        # No log here — the validator pattern (cf. FLO-424) does not
        # log; the caller will log "validation_passed" and downstream
        # any actual issue surfaces in the broker leg.
        return []

    # Geometry: distance is positive only when entry is on the
    # "chase side" relative to direction. Pullback shapes produce
    # negative distance and are not subject to this gate.
    if direction == "BUY":
        distance_pips = round((float(entry_price) - current_price) * 10, 1)
    else:  # SELL
        distance_pips = round((current_price - float(entry_price)) * 10, 1)

    if distance_pips <= threshold_pips:
        return []  # under threshold OR pullback shape (negative distance)

    return [(
        f"Plan rejected: FLO-425 §16f anti-smuggling geometry gate. "
        f"setup_type={setup_type!r} direction={direction} "
        f"entry_price={float(entry_price):.2f} "
        f"current_price={current_price:.2f} "
        f"distance_pips={distance_pips:+.1f} "
        f"threshold_pips={threshold_pips} "
        f"gate_active_until={until_str}. "
        f"This entry is more than {threshold_pips}p on the chase side "
        f"of current price — a Phase-2-only break-attempt geometry "
        f"that today's BUY-cluster losses shared (PLAN-007 was +215p "
        f"above current). Re-author with entry price within "
        f"±{threshold_pips}p of current, or wait for price to come "
        f"to your structural level. Pullback shapes (BUY below "
        f"current, SELL above current) are unaffected. Gate "
        f"self-disables at {until_str}."
    )]


def _check_management_hybrid_constraints(plan: Plan) -> list[str]:
    """FLO-419 Phase 3 / Escola 2 (CEO directive 2026-05-01, evening):
    Claude authors full SL management (BE trigger + trail distance)
    in each plan. Snow executes mechanically. Qwen Trade Manager is
    DISABLED for active SL management (regime-driven closes burned
    a +125p MFE trade for +11p — PLAN-042 evidence).

    Escola 2 patterns Claude is taught to use:
      - Option A: BE when MFE reaches 60% of TP distance
      - Option B: BE when MFE reaches 1R (= SL distance)
      - After BE: trail SL at fixed distance (typ. 100-150p) behind price
      - Claude picks the rule that fits each setup's geometry

    Permitted management contents:
      (a) empty (plan opts out of all Snow management), OR
      (b) up to TWO contingencies, each one of:
          - move_sl_to_breakeven with mfe_reached.pips > 0
          - trail_sl with mfe_reached.pips > 0 and trail_pips > 0
            (schema enforces trail_pips > 0)

    Rejected:
      - adjust_sl / move_sl_to_price (still raw tactical — Claude
        should express SL intent through BE+trail, not bare price moves)
      - move_sl_to_breakeven / trail_sl without an mfe_reached condition
      - move_sl_to_breakeven / trail_sl with mfe_reached.pips <= 0
      - more than two management contingencies

    Monotonic SL guard at executor.modify_position (FLO-419, commits
    a9a8f4a + 7a1a1c9) prevents trail_sl from walking SL backward —
    the failure mode that motivated banning trail_sl in the previous
    iteration of this function. Re-enabled on top of that guard.

    Reference: data/_audits/gemini_era_trade_audit_2026-05-01.md
    (PLAN-042 evidence motivates the TM-disable + Escola-2 pivot).

    Empty-management policy (CEO directive 2026-05-01 evening, addendum):
    Empty `management` is REJECTED unless TP-distance-from-entry < 100
    pips (too tight for any meaningful BE). This prevents the
    PLAN-20260501-036/037 opt-out pattern where Floki authored zero
    management and let losers run to full SL with no protection.
    """
    errors: list[str] = []

    if not plan.management:
        try:
            entry = plan.entry
            if entry.entry_price is not None:
                tp_distance_pips = abs(entry.initial_tp - entry.entry_price) / 0.1
            else:
                tp_distance_pips = abs(entry.initial_tp - entry.initial_sl) / 0.1
        except Exception:
            tp_distance_pips = 0.0  # conservative — force rejection on geometry error
        if tp_distance_pips >= 100.0:
            errors.append(
                f"management: empty management is rejected under Escola "
                f"2 when TP-distance-from-entry ({tp_distance_pips:.0f} "
                f"pips) >= 100 pips. Author at least one contingency "
                f"(`move_sl_to_breakeven` or `trail_sl`) on `mfe_reached`. "
                f"Empty-management is the PLAN-036/037 opt-out pattern "
                f"that the Escola 2 architecture exists to prevent. The "
                f"<100 pip carve-out is the only exception; below that "
                f"BE+trail adds no meaningful protection."
            )
        return errors

    _MAX = 2
    _ALLOWED = {"move_sl_to_breakeven", "trail_sl"}

    if len(plan.management) > _MAX:
        names = ", ".join(f"{m.name!r}" for m in plan.management)
        errors.append(
            f"management: Escola 2 architecture (FLO-419 Phase 3) "
            f"allows at most {_MAX} contingencies per plan. Got "
            f"{len(plan.management)}: {names}. Typical pattern is one "
            f"`move_sl_to_breakeven` + one `trail_sl`."
        )

    for mi, mgmt in enumerate(plan.management):
        try:
            action_type = str(getattr(mgmt.action, "type", "") or "")
        except Exception:
            action_type = ""

        if action_type not in _ALLOWED:
            errors.append(
                f"management[{mi}] ({mgmt.name!r}): action.type="
                f"{action_type!r} not allowed under Escola 2. Permitted "
                f"actions are `move_sl_to_breakeven` and `trail_sl`. "
                f"Express SL intent through BE+trail, not raw "
                f"adjust_sl/move_sl_to_price."
            )
            continue

        # BE and trail both require an mfe_reached trigger > 0.
        mfe_pips: Optional[float] = None
        for c in mgmt.conditions:
            if getattr(c, "type", None) == "mfe_reached":
                p = float(getattr(c, "pips", 0) or 0)
                if mfe_pips is None or p > mfe_pips:
                    mfe_pips = p

        if mfe_pips is None:
            errors.append(
                f"management[{mi}] ({mgmt.name!r}): {action_type} must "
                f"trigger on `mfe_reached`. Got conditions with no "
                f"mfe_reached. Add `mfe_reached: pips >= N` (Escola 2: "
                f"60% of TP distance, or 1R)."
            )
        elif mfe_pips <= 0:
            errors.append(
                f"management[{mi}] ({mgmt.name!r}): {action_type} "
                f"trigger mfe_reached.pips must be > 0 (got {mfe_pips})."
            )

    return errors



def _check_ema_relation_period_consistency(plan: Plan) -> list[str]:
    """FLO-404 follow-up (CEO directive 2026-04-30) — cross-field rule
    on EMARelation: `period` is REQUIRED for `price_above`/`price_below`
    (the evaluator reads exactly EMA(tf, period) — single-EMA flip
    primitive) but FORBIDDEN for `aligned_bull`/`aligned_bear`
    (the evaluator reads ALL FOUR periods 9/21/50/200 in strict
    alignment — regime gate, period field is silently ignored).

    Pre-FLO-404 the schema accepted period for any relation, producing
    the PLAN-20260429-012 misuse: Floki used `aligned_bull` with
    `period: 21` thinking it meant "price above EMA21," but the
    primitive required EMA9>EMA21>EMA50>EMA200 (full bullish stack)
    which never held during the bounce. Plan never fired.

    This check rejects both inconsistencies with educational messages
    that point Floki at the correct primitive.
    """
    errors: list[str] = []
    for label, ci, c in _iter_plan_conditions(plan):
        if getattr(c, "type", None) != "ema_relation":
            continue
        relation = getattr(c, "relation", None)
        period = getattr(c, "period", None)
        if relation in ("price_above", "price_below") and period is None:
            errors.append(
                f"{label}.conditions[{ci}]: ema_relation with "
                f"relation={relation!r} REQUIRES the `period` field "
                f"(one of 9, 21, 50, 200) — the evaluator reads exactly "
                f"EMA(tf, period) for this primitive. Add `period: N` "
                f"to the condition."
            )
        elif relation in ("aligned_bull", "aligned_bear") and period is not None:
            errors.append(
                f"{label}.conditions[{ci}]: ema_relation with "
                f"relation={relation!r} must NOT carry a `period` field "
                f"— the evaluator reads all four EMAs (9, 21, 50, 200) "
                f"on `tf` regardless of period (full-stack regime gate). "
                f"If you meant the regime check, omit `period`. If you "
                f"meant 'price above EMA{period}' (single-EMA flip), "
                f"use `relation: price_above` with period={period}."
            )
    return errors


def _check_stateful_in_v1(plan: Plan) -> list[str]:
    """v1 plans MUST NOT reference stateful primitives.

    Stateful primitives need the `state_cache_json` column + the
    in-memory PerConditionStateCache (commit 2) + per-class evaluators
    (commits 3-5) — all v2-schema infrastructure. A plan declaring
    `schema_version=1` while embedding a stateful primitive type would
    silently bypass that machinery. Reject at submit-time with a
    structured error naming the field path.
    """
    if plan.schema_version >= 2:
        return []
    errors: list[str] = []
    for label, ci, c in _iter_plan_conditions(plan):
        ctype = getattr(c, "type", None)
        if isinstance(ctype, str) and ctype in _STATEFUL_PRIMITIVES:
            errors.append(
                f"{label}.conditions[{ci}]: {ctype!r} requires "
                f"schema_version >= 2; got {plan.schema_version}"
            )
    return errors


# =============================================================================
# FLO-400 — pre-validation JSON-string decoder
# =============================================================================

# The four nested-object paths Gemini stringified on its first FLO-389
# brain-comparison cycle (PLAN-005 retry, observed 2026-04-29). Pydantic
# rejects the JSON-encoded strings with four "should be a valid dictionary"
# errors at exactly these paths. Gemini self-corrects on the next attempt
# from the validator's error text — but at the cost of one wasted cycle
# (~95s, 264k input tokens) per occurrence.
#
# This pre-decoder handles the common-case correction in-process so the
# first attempt validates. Targeted, NOT recursive: we touch only the four
# known paths so a legitimate string field elsewhere (e.g. `analysis.thesis`)
# can never be silently parsed as JSON.
#
# Provider-agnostic: Qwen and Kimi don't emit JSON-strings at these paths,
# so this is a no-op for non-Gemini cycles. If Gemini ever stops emitting
# strings (model improvement / Pydantic-aware fine-tune), this is also
# a no-op — graceful obsolescence.

# Paths we attempt to decode. Order doesn't matter; each is independent.
_GEMINI_STRING_LEAK_PATHS: tuple[tuple[str, ...], ...] = (
    ("analysis", "context_tags"),  # → dict
    ("entry", "conditions", "*"),  # list-item → dict
    ("management", "*"),           # list-item → dict
    ("exit", "*"),                 # list-item → dict
)


def _try_decode_to_dict(v: Any) -> Any:
    """Return parsed JSON dict if `v` is a string that decodes to dict;
    otherwise return `v` unchanged. JSON parse errors are swallowed —
    Pydantic will surface its own native error on the original value."""
    if not isinstance(v, str):
        return v
    try:
        parsed = json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return v
    if isinstance(parsed, dict):
        return parsed
    return v


def _decode_known_string_paths(plan_dict: dict[str, Any]) -> dict[str, Any]:
    """FLO-400: pre-validation JSON-string decoder for the four paths
    Gemini stringifies on first attempt (analysis.context_tags,
    entry.conditions[*], management[*], exit[*]).

    Returns a NEW dict; original is untouched (only nested containers
    along the four touched paths are shallow-copied to avoid mutating
    caller state). Anything else passes through by reference.

    Targeted by design — see _GEMINI_STRING_LEAK_PATHS comment above for
    why we don't recursively walk.
    """
    if not isinstance(plan_dict, dict):
        return plan_dict
    out: dict[str, Any] = dict(plan_dict)

    # analysis.context_tags
    analysis = out.get("analysis")
    if isinstance(analysis, dict):
        analysis = dict(analysis)
        analysis["context_tags"] = _try_decode_to_dict(analysis.get("context_tags"))
        out["analysis"] = analysis

    # entry.conditions[*]
    entry = out.get("entry")
    if isinstance(entry, dict):
        entry = dict(entry)
        conds = entry.get("conditions")
        if isinstance(conds, list):
            entry["conditions"] = [_try_decode_to_dict(c) for c in conds]
        out["entry"] = entry

    # management[*] and exit[*]
    for key in ("management", "exit"):
        items = out.get(key)
        if isinstance(items, list):
            out[key] = [_try_decode_to_dict(item) for item in items]

    return out


# =============================================================================
# Public entry point
# =============================================================================

def validate_plan(
    plan_dict: dict[str, Any],
) -> tuple[bool, Optional[Plan], list[str]]:
    """Validate a submitted plan dict.

    Returns (ok, plan_or_none, errors):
      ok=True    → plan is valid; returned Plan model is ready to persist
      ok=False   → errors is a non-empty list of human-readable diagnostic
                   strings; plan_or_none is None if Pydantic parsing failed,
                   or the parsed Plan if only business-rule checks failed
                   (caller MAY show the parsed view to help Floki revise).

    Exceptions are NOT raised for validation failures — they're returned
    in the errors list so the caller (submit_plan_to_snow tool) can return
    them to Floki as a structured tool response.

    A truly malformed input (non-dict, non-utf8, etc.) will surface as a
    Pydantic ValidationError captured here.
    """
    # --- 0. FLO-400 pre-decoder ---
    # Gemini's first-attempt failure mode is JSON-stringifying nested
    # objects at four known paths. Decode them here so the first call
    # validates instead of burning a cycle on the retry. Targeted, not
    # recursive — see _decode_known_string_paths.
    plan_dict = _decode_known_string_paths(plan_dict)

    # --- 1. Pydantic parse ---
    try:
        plan = Plan(**plan_dict)
    except ValidationError as e:
        errs = [f"schema: {err['loc']}: {err['msg']}" for err in e.errors()]
        return False, None, errs
    except TypeError as e:
        # e.g. plan_dict was not a dict
        return False, None, [f"schema: could not construct Plan: {e}"]

    # --- 2. Business-rule checks ---
    errors: list[str] = []
    errors += _check_schema_version(plan)
    errors += _check_stateful_in_v1(plan)
    errors += _check_ema_relation_period_consistency(plan)
    errors += _check_timestamps(plan)
    errors += _check_entry_sl_tp(plan)
    errors += _check_price_bounds(plan)
    errors += _check_contingency_names_unique(plan)
    errors += _check_execute_market_placement(plan)
    errors += _check_cancel_plan_placement(plan)
    errors += _check_time_between_conditions(plan)
    errors += _check_entry_price_in_range(plan)
    errors += _check_management_threshold_floor(plan)
    errors += _check_min_entry_conditions(plan)
    errors += _check_no_dynamic_level_in_entry(plan)
    errors += _check_exit_geometry_vs_sl(plan)
    errors += _check_management_reachability(plan)
    errors += _check_management_hybrid_constraints(plan)
    errors += _check_confidence_floor(plan)
    errors += _check_flo424_safety_circuit(plan)
    errors += _check_flo425_geometry_gate(plan)

    if errors:
        return False, plan, errors
    return True, plan, []


__all__ = ["validate_plan"]
