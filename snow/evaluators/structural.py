"""Structural evaluators: price_at_sr_zone, price_at_fibonacci.

Both read zone/level data from SemanticCache (Floki-cycle data), the
current price from LiveData, and test proximity.

Proximity definition (advisor item #5):
    abs(current_price - zone_price) <= tolerance_pips × PIP_SIZE

Zone type filter (price_at_sr_zone):
    cond.zone_type == "any"         → match any zone
    cond.zone_type == "support"     → match zones with zone_type == "support"
    cond.zone_type == "resistance"  → match zones with zone_type == "resistance"

Fibonacci semantics:
    The SemanticCache path for Fibonacci levels is still being shaped
    (RFC §6.1 references `h1_fib_levels` but agent_data_builder.py does
    not currently expose that as a top-level key — will be wired in
    Phase 4). Our reader is tolerant of two shapes:
      A) `fibonacci` → {"levels": [{"pct": "38.2", "price": 4712.5}, ...]}
      B) `fibonacci` → {"0.382": 4712.5, "0.5": 4715.0, ...}   (flat)
    When the level value can be resolved, we compare against the
    default tolerance (5 pips) because `PriceAtFibonacci` carries no
    explicit tolerance field in the frozen schema. Missing data → False.
"""
from __future__ import annotations

from typing import Optional

from snow.evaluators.context import EvalContext, PIP_SIZE
from snow.schema import PriceAtFibonacci, PriceAtPivot, PriceAtSRZone


# Default proximity tolerance for fibonacci (schema carries no
# explicit field). 5 pips = 0.5 price units on XAUUSD; tight enough
# to be meaningful, loose enough to absorb broker-quote jitter.
_DEFAULT_FIB_TOLERANCE_PIPS: float = 5.0


def evaluate_price_at_sr_zone(cond: PriceAtSRZone, ctx: EvalContext) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    zones = ctx.semantic_cache.get("sr_zones")
    if not isinstance(zones, list) or not zones:
        return False
    tolerance_price = cond.tolerance_pips * PIP_SIZE
    wanted = cond.zone_type  # "support" | "resistance" | "any"
    for z in zones:
        if not isinstance(z, dict):
            continue
        zone_price = z.get("price")
        zone_type = z.get("zone_type")
        if not isinstance(zone_price, (int, float)):
            continue
        # Brain publishes zone_type as UPPERCASE ('SUPPORT' / 'RESISTANCE'
        # / 'FLIP') from support_resistance.SRZone; cond.zone_type is a
        # Pydantic Literal lowercase ('support' / 'resistance' / 'any').
        # Compare case-insensitively so the lowercase Literal matches
        # the upstream uppercase tag. 'flip' zones don't match either
        # 'support' or 'resistance' — match only 'any' (preserves the
        # operator's intent: a flip is neither pure support nor pure
        # resistance).
        if wanted != "any":
            zt_norm = (zone_type or "").lower()
            if zt_norm != wanted:
                continue
        if abs(float(price) - float(zone_price)) <= tolerance_price:
            return True
    return False


def evaluate_price_at_fibonacci(
    cond: PriceAtFibonacci, ctx: EvalContext
) -> bool:
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    level_price = _resolve_fib_level_price(cond.level, ctx)
    if level_price is None:
        return False
    # Phase 7.3: optional explicit tolerance overrides the default.
    tol_pips = (
        float(cond.tolerance_pips)
        if getattr(cond, "tolerance_pips", None) is not None
        else _DEFAULT_FIB_TOLERANCE_PIPS
    )
    tolerance_price = tol_pips * PIP_SIZE
    return abs(float(price) - float(level_price)) <= tolerance_price


# =============================================================================
# Phase 7.3 (FLO-355) — pivot proximity
# =============================================================================

def evaluate_price_at_pivot(cond: PriceAtPivot, ctx: EvalContext) -> bool:
    """Proximity check against a daily pivot point (Classic or Fibonacci
    set). Reads from LiveData.pivot_points() which delegates to the
    SemanticCache `pivot_points` slot Brain populates each cycle.

    Missing data → False (fail-safe). The two-layer lookup
    `daily.{set}.{level}` matches Brain's published shape.
    """
    price = ctx.live_data.price("mid")
    if price is None:
        return False
    pp = ctx.live_data.pivot_points()
    if not isinstance(pp, dict):
        return False
    # Shape-tolerant: accept either the unwrapped {classic, fibonacci}
    # form (real LiveData unwraps `daily` for us) or the wrapped
    # {daily: {...}, weekly: {...}} form (some FakeLiveData / test
    # paths). Defense in depth — evaluator handles both regardless.
    if isinstance(pp.get("daily"), dict):
        pp = pp["daily"]
    set_dict = pp.get(cond.pivot_set)
    if not isinstance(set_dict, dict):
        return False
    level_price = set_dict.get(cond.level)
    if not isinstance(level_price, (int, float)):
        return False
    tolerance_price = float(cond.tolerance_pips) * PIP_SIZE
    return abs(float(price) - float(level_price)) <= tolerance_price


def _resolve_fib_level_price(
    level: float, ctx: EvalContext
) -> Optional[float]:
    """Look up the fibonacci level's absolute price in the semantic cache.

    Shape-tolerant: accepts both the list-of-dicts format used by
    agent_data_builder._compute_fibonacci_from_candles (each entry has
    `pct` as string and `price` as float) and a flat mapping format.
    """
    fib = ctx.semantic_cache.get("fibonacci")
    if not isinstance(fib, dict):
        return None

    # Flat mapping first: { "0.382": 4712.5, ... } or numeric-keyed
    for key in (str(level), f"{level}", f"{level:.3f}"):
        v = fib.get(key)
        if isinstance(v, (int, float)):
            return float(v)

    # List-of-dicts: {"levels": [{"pct": "38.2", "price": ...}, ...]}
    levels = fib.get("levels")
    if isinstance(levels, list):
        # Compare pct as float to dodge "38.2" vs "38.20" formatting
        target_pct = level * 100.0
        for entry in levels:
            if not isinstance(entry, dict):
                continue
            pct_raw = entry.get("pct")
            try:
                pct_val = float(pct_raw) if pct_raw is not None else None
            except (TypeError, ValueError):
                continue
            if pct_val is None:
                continue
            if abs(pct_val - target_pct) < 0.05:  # allow 38.2 ≈ 38.200
                p = entry.get("price")
                if isinstance(p, (int, float)):
                    return float(p)

    return None
