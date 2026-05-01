"""
FLO-320: Tick pressure proxy — directional bid/ask imbalance.

IMPORTANT: this is a PROXY, not true order flow. Capital Point's tick
feed does not carry buy/sell flags (TICK_FLAG_BUY/SELL are 0% of ticks,
confirmed in the FLO-319 feasibility tests), `last` is 0, and `volume`
per tick is 0 — the broker publishes a quote stream, not a trade tape.
Without trade-initiated-side information, true delta is unbuildable.

What this DOES compute: the classic mid-price tick rule.
  mid_t > mid_t-1 → "uptick" (buyer pressure proxy)
  mid_t < mid_t-1 → "downtick" (seller pressure proxy)
  mid_t == mid_t-1 → carry previous direction (last-tick rule)

High uptick ratio = bid/ask has been moving up on average = buyers have
been more aggressive in lifting the offer. NOT the same as "aggressive
buyers consumed X units of sell-side liquidity" — it's a directional
pressure signal inferred from quote movement.

All output fields include a "note" flag so Floki sees the proxy caveat
every time he calls this tool.
"""
from typing import Any, Dict, Optional
from datetime import datetime, timedelta

from mt5_safe import mt5  # FLO-348: thread-safe MT5 proxy
from logger import log


_WINDOW_MIN_SEC = 30
_WINDOW_MAX_SEC = 3600


def _broker_now(symbol: str) -> datetime:
    """Return current broker-local naive datetime — what mt5.copy_ticks_range
    expects.

    FLO-96 fix (2026-05-01): the prior code used `datetime.now()`, which on
    a UTC system produces UTC. MT5 interprets the start/end args as
    broker-local, so a UTC datetime queries ticks from N hours ago (where
    N = broker offset; ~3h on Capital Point). Empirical impact: cycle
    2026-05-01T14:56:54Z saw price ~4632 in chart_patterns / S/R / regime
    evidence, but get_tick_pressure returned price_end=4578.07 — a 54-USD
    gap matching the 3-hour stale window. GPT-5.4 caught this in the
    model-comparison test.

    Canonical broker now = mt5.symbol_info_tick().time interpreted as a
    UTC epoch yields a naive datetime equal to broker wall clock. Same
    pattern used by mfe_backfill._utc_to_broker_naive."""
    try:
        t = mt5.symbol_info_tick(symbol)
        if t and t.time > 0:
            return datetime.utcfromtimestamp(int(t.time))
    except Exception:
        pass
    # Fallback: system UTC + 3h (Capital Point typical broker offset).
    # Worse than the live-tick path but better than naive datetime.now().
    return datetime.utcnow() + timedelta(hours=3)


def compute_tick_pressure(
    symbol: str = "XAUUSD",
    window_seconds: int = 300,
    recent_seconds: int = 30,
) -> Optional[Dict[str, Any]]:
    """Return tick-pressure proxy dict. None on failure."""
    try:
        window_seconds = max(_WINDOW_MIN_SEC, min(_WINDOW_MAX_SEC, int(window_seconds)))
        recent_seconds = max(5, min(window_seconds, int(recent_seconds)))

        end = _broker_now(symbol)
        start = end - timedelta(seconds=window_seconds)
        ticks = mt5.copy_ticks_range(symbol, start, end, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) < 2:
            return None

        # Tick rule on mid-price
        prev_mid = None
        prev_dir = 0  # -1 down, 0 flat (carry over), +1 up
        upticks = 0
        downticks = 0
        first_mid: Optional[float] = None
        last_mid: Optional[float] = None

        # Recent window bookkeeping
        recent_unix = int((end - timedelta(seconds=recent_seconds)).timestamp())
        recent_upticks = 0
        recent_downticks = 0

        for t in ticks:
            bid = float(t["bid"])
            ask = float(t["ask"])
            if bid <= 0 or ask <= 0:
                continue
            mid = (bid + ask) / 2.0

            if first_mid is None:
                first_mid = mid
                prev_mid = mid
                continue

            if mid > prev_mid:
                upticks += 1
                prev_dir = 1
                if int(t["time"]) >= recent_unix:
                    recent_upticks += 1
            elif mid < prev_mid:
                downticks += 1
                prev_dir = -1
                if int(t["time"]) >= recent_unix:
                    recent_downticks += 1
            else:
                # Flat — carry previous direction (last-tick rule)
                if prev_dir > 0:
                    upticks += 1
                    if int(t["time"]) >= recent_unix:
                        recent_upticks += 1
                elif prev_dir < 0:
                    downticks += 1
                    if int(t["time"]) >= recent_unix:
                        recent_downticks += 1

            prev_mid = mid
            last_mid = mid

        total = upticks + downticks
        if total == 0 or first_mid is None or last_mid is None:
            return None

        uptick_ratio = upticks / total
        net_delta = upticks - downticks
        intensity_per_sec = round(total / window_seconds, 2)

        # Recent pressure label — requires a clear majority in the short window
        recent_total = recent_upticks + recent_downticks
        if recent_total >= 5:
            rr = recent_upticks / recent_total
            if rr >= 0.60:
                recent_pressure = "BUY"
            elif rr <= 0.40:
                recent_pressure = "SELL"
            else:
                recent_pressure = "NEUTRAL"
        else:
            recent_pressure = "NEUTRAL"  # too few ticks to call

        price_change_points = round(last_mid - first_mid, 2)

        return {
            "window_seconds": window_seconds,
            "recent_seconds": recent_seconds,
            "total_ticks": int(len(ticks)),
            "classified_ticks": total,
            "upticks": upticks,
            "downticks": downticks,
            "uptick_ratio": round(uptick_ratio, 3),
            "net_delta": net_delta,
            "intensity_per_sec": intensity_per_sec,
            "recent_pressure": recent_pressure,
            "recent_upticks": recent_upticks,
            "recent_downticks": recent_downticks,
            "price_start": round(first_mid, 2),
            "price_end": round(last_mid, 2),
            "price_change_points": price_change_points,
            "note": (
                "PROXY based on bid/ask mid-price tick rule. NOT true "
                "buy/sell-initiated volume delta — that data is not available "
                "on this broker (no TICK_FLAG_BUY/SELL, no trade volume). "
                "High uptick_ratio suggests aggressive buying pressure but "
                "does not directly measure executed order flow."
            ),
        }
    except Exception as e:
        log.debug(f"tick_pressure: compute failed: {e}")
        return None
