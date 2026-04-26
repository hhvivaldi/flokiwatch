"""Snow execution-quality helpers — FLO-365.

Pure helpers used by snow/actions.py to compute the bits that go into
each `snow_execution_quality` row: tick snapshot, latency, and slippage.
Persistence lives in snow.db.insert_execution_quality.

Design notes
------------
* Tick snapshot is taken ONCE at the top of each dispatch call so all
  fields (bid/ask/mid + entry-reference price) come from the same
  microsecond. We do not re-query MT5 after the broker call.
* Slippage convention: positive = UNFAVOURABLE fill.
  - BUY  filled above ask  → positive slippage_pips
  - SELL filled below bid  → positive slippage_pips
* PIP_SIZE comes from `snow.evaluators.context.PIP_SIZE` (0.1 for XAU/USD)
  to match the rest of the engine. We import lazily to avoid a top-level
  cycle through evaluators.
* All conversions are best-effort: a bad tick returns a TickSnapshot
  with all-None numeric fields rather than raising — the row is still
  inserted with NULLs (FLO-365 is observability, not a hard gate).
"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil as _ceil
from typing import Optional


@dataclass(frozen=True)
class TickSnapshot:
    bid: Optional[float]
    ask: Optional[float]
    mid: Optional[float]


def capture_tick(symbol: str) -> TickSnapshot:
    """One-shot tick capture. Never raises — returns all-None on failure."""
    try:
        from mt5_safe import mt5
        info = mt5.symbol_info_tick(symbol)
    except Exception:
        return TickSnapshot(None, None, None)
    if info is None:
        return TickSnapshot(None, None, None)
    bid = float(info.bid) if getattr(info, "bid", None) is not None else None
    ask = float(info.ask) if getattr(info, "ask", None) is not None else None
    mid: Optional[float] = None
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
    return TickSnapshot(bid=bid, ask=ask, mid=mid)


def entry_reference_price(direction: str, tick: TickSnapshot) -> Optional[float]:
    """Expected fill price for a market entry: ask for BUY, bid for SELL."""
    if direction == "BUY":
        return tick.ask
    if direction == "SELL":
        return tick.bid
    return None


def compute_slippage_pips(
    direction: str,
    reference_price: Optional[float],
    actual_price: Optional[float],
) -> Optional[float]:
    """Signed slippage in pips. Positive = unfavourable (worse than ref).

    Returns None if either price is missing or the direction is unknown.
    """
    if reference_price is None or actual_price is None:
        return None
    try:
        from snow.evaluators.context import PIP_SIZE
    except Exception:
        return None
    if PIP_SIZE <= 0:
        return None
    if direction == "BUY":
        diff = actual_price - reference_price
    elif direction == "SELL":
        diff = reference_price - actual_price
    else:
        return None
    return round(diff / PIP_SIZE, 4)


def aggregate_summary(window_days: int = 7) -> dict:
    """Per-action-type aggregation over the last `window_days`.

    Returns:
      {
        "execute_market": {
            "count": 12,
            "success_count": 11,
            "avg_slippage_pips": 1.4,
            "max_slippage_pips": 4.2,
            "p95_latency_ms": 184,
        },
        "adjust_sl": {...},
        ...
      }

    SQLite has no native percentile, so p95 is computed in Python from
    the (small) latency series for each action_type. NULL slippage rows
    are excluded from slippage aggregates but still counted.
    """
    from datetime import datetime, timedelta, timezone
    from tz_utils import utc_iso
    from snow import db as snow_db
    cutoff = utc_iso(
        datetime.now(timezone.utc) - timedelta(days=int(window_days))
    )
    conn = snow_db._connect()
    try:
        rows = list(conn.execute(
            """
            SELECT action_type, status, slippage_pips, latency_ms
              FROM snow_execution_quality
             WHERE executed_at >= ?
            """,
            (cutoff,),
        ))
    finally:
        conn.close()

    by_action: dict[str, dict] = {}
    for r in rows:
        a = r["action_type"]
        bucket = by_action.setdefault(a, {
            "count": 0, "success_count": 0,
            "_slips": [], "_lats": [],
        })
        bucket["count"] += 1
        if r["status"] == "success":
            bucket["success_count"] += 1
        if r["slippage_pips"] is not None:
            bucket["_slips"].append(float(r["slippage_pips"]))
        if r["latency_ms"] is not None:
            bucket["_lats"].append(int(r["latency_ms"]))

    out: dict[str, dict] = {}
    for a, b in by_action.items():
        slips = b["_slips"]
        lats = sorted(b["_lats"])
        out[a] = {
            "count": b["count"],
            "success_count": b["success_count"],
            "avg_slippage_pips":
                round(sum(slips) / len(slips), 2) if slips else None,
            "max_slippage_pips":
                round(max(slips), 2) if slips else None,
            "p95_latency_ms":
                lats[max(0, _ceil(0.95 * len(lats)) - 1)] if lats else None,
        }
    return out


def latency_ms(fired_at_iso: Optional[str], executed_at_iso: str) -> Optional[int]:
    """Wall-clock latency between AND-fold true and broker-call return.

    Both timestamps are ISO-8601 UTC ("...Z"). Returns None if either is
    missing or unparseable.
    """
    if fired_at_iso is None:
        return None
    try:
        from datetime import datetime
        def _parse(s: str) -> datetime:
            # Tolerate "...Z" (utc_iso default) and offset-naive.
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s)
        ms = (_parse(executed_at_iso) - _parse(fired_at_iso)).total_seconds() * 1000.0
        return int(round(ms))
    except Exception:
        return None
