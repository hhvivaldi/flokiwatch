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
    errors += _check_management_reachability(plan)

    if errors:
        return False, plan, errors
    return True, plan, []


__all__ = ["validate_plan"]
