"""
FLO-319: Volume Profile by price level.

Aggregates tick_volume from M1/M5/M15 candles into price buckets over a
time window, then reports the classic volume-profile outputs:

  - POC (point of control): highest-volume bucket
  - Value Area: contiguous price range containing 70% of total volume,
                expanded outward from POC standard-method
  - Top N HVNs: high-volume nodes by percentage of total
  - Low-volume gaps: contiguous runs of low-volume buckets (price moves
    fast through these regions)
  - Current price context: distance to nearest HVN, whether inside VA

Data source is M1 `tick_volume` (Capital Point's `real_volume` is always 0
per FLO-318/FLO-312 feasibility tests — tick_volume is what we have).
Bar volume is distributed evenly across the buckets the bar's [low,high]
range covers — standard approximation for volume profile from OHLCV.

Timeframe selection:
  window_hours <= 4   → M1 (max 240 bars)
  window_hours <= 24  → M5 (max 288 bars)
  window_hours  > 24  → M15 (max 672 bars for 7d)

Never raises — returns None on any failure.
"""
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta
from collections import defaultdict

from mt5_safe import mt5  # FLO-348: thread-safe MT5 proxy
from logger import log


PIP_SIZE = 0.1  # XAU/USD
_WINDOW_HARD_CAP_HOURS = 168.0  # 7 days


def _pick_timeframe(window_hours: float):
    if window_hours <= 4:
        return mt5.TIMEFRAME_M1
    if window_hours <= 24:
        return mt5.TIMEFRAME_M5
    return mt5.TIMEFRAME_M15


def _bucket_of(price: float, size: float) -> float:
    """Round price to nearest bucket boundary. Returns 2dp float."""
    return round(round(price / size) * size, 2)


def compute_volume_profile(
    symbol: str = "XAUUSD",
    window_hours: float = 1.0,
    bucket_size_points: float = 1.0,
    top_n_nodes: int = 5,
) -> Optional[Dict[str, Any]]:
    """Return a volume-profile dict. None on failure (no data / MT5 error)."""
    try:
        window_hours = max(0.1, min(_WINDOW_HARD_CAP_HOURS, float(window_hours)))
        bucket_size = float(bucket_size_points)
        if bucket_size <= 0:
            return None

        # FLO-96 fix (2026-05-01): copy_rates_range expects broker-local
        # naive datetimes. datetime.now() on a UTC system produces UTC,
        # which MT5 interprets as broker-time = N hours stale (N=3 on
        # Capital Point). Same root cause as the tick_pressure bug GPT-5.4
        # surfaced in the model-comparison test. Use the live tick's time
        # as the canonical broker now (mfe_backfill uses the same pattern).
        import time as _time_mod
        _src = "tick.time"
        _offset = 10800
        try:
            _t = mt5.symbol_info_tick(symbol)
            if _t and _t.time > 0:
                end = datetime.utcfromtimestamp(int(_t.time))
                _offset = int(_t.time) - int(_time_mod.time())
                if not (7200 <= _offset <= 14400):
                    end = datetime.utcnow() + timedelta(hours=3)
                    _offset = 10800
                    _src = "FALLBACK"
            else:
                end = datetime.utcnow() + timedelta(hours=3)
                _src = "constant"
        except Exception:
            end = datetime.utcnow() + timedelta(hours=3)
            _src = "constant"
        try:
            log.info(
                "TIMEZONE_AUDIT | offset={}s ({}) | utc={} | broker={} | site=volume_profile.compute".format(
                    _offset, _src,
                    datetime.utcnow().strftime("%H:%M:%S"),
                    end.strftime("%H:%M:%S"),
                )
            )
        except Exception:
            pass
        start = end - timedelta(hours=window_hours)
        tf = _pick_timeframe(window_hours)

        bars = mt5.copy_rates_range(symbol, tf, start, end)
        if bars is None or len(bars) == 0:
            return None

        # Bucket aggregation — each bar's tick_volume distributed evenly
        # across the buckets spanning its high/low range.
        buckets: Dict[float, int] = defaultdict(int)
        price_min = float("inf")
        price_max = float("-inf")

        for b in bars:
            h, l, v = float(b["high"]), float(b["low"]), int(b["tick_volume"])
            if v <= 0 or h < l:
                continue
            price_min = min(price_min, l)
            price_max = max(price_max, h)
            lo_b = _bucket_of(l, bucket_size)
            hi_b = _bucket_of(h, bucket_size)
            n_buckets = int(round((hi_b - lo_b) / bucket_size)) + 1
            if n_buckets < 1:
                continue
            vpb = v / n_buckets
            for i in range(n_buckets):
                bp = round(lo_b + i * bucket_size, 2)
                buckets[bp] += int(vpb)

        if not buckets:
            return None

        total_vol = sum(buckets.values())
        if total_vol <= 0:
            return None

        # POC = highest-volume bucket
        poc_price, poc_vol = max(buckets.items(), key=lambda kv: kv[1])

        # Value Area (standard): expand outward from POC one bucket at a
        # time, always picking the side with more volume, until 70% of
        # total volume is enclosed. Result is a contiguous price range.
        sorted_by_price = sorted(buckets.items())
        poc_idx = next(i for i, (p, _) in enumerate(sorted_by_price) if p == poc_price)
        va_low_idx = poc_idx
        va_high_idx = poc_idx
        va_volume = poc_vol
        va_target = total_vol * 0.7
        while va_volume < va_target:
            below_vol = sorted_by_price[va_low_idx - 1][1] if va_low_idx > 0 else -1
            above_vol = (
                sorted_by_price[va_high_idx + 1][1]
                if va_high_idx < len(sorted_by_price) - 1
                else -1
            )
            if below_vol < 0 and above_vol < 0:
                break
            if below_vol >= above_vol:
                va_low_idx -= 1
                va_volume += sorted_by_price[va_low_idx][1]
            else:
                va_high_idx += 1
                va_volume += sorted_by_price[va_high_idx][1]

        va_low = sorted_by_price[va_low_idx][0]
        va_high = sorted_by_price[va_high_idx][0]

        # Top N nodes — classify as HVN if bucket volume >= 2× average
        # bucket volume (common VP heuristic).
        avg_bucket_vol = total_vol / len(buckets)
        sorted_by_vol = sorted(buckets.items(), key=lambda kv: -kv[1])
        top_nodes: List[Dict[str, Any]] = []
        for p, v in sorted_by_vol[: max(1, int(top_n_nodes))]:
            pct = v / total_vol * 100
            classification = "HVN" if v >= avg_bucket_vol * 2 else "NODE"
            top_nodes.append(
                {
                    "price": p,
                    "volume": int(v),
                    "pct": round(pct, 1),
                    "classification": classification,
                }
            )

        # Low-volume gaps — contiguous runs of buckets with volume
        # < 30% of average bucket volume. Report top 3 by width (pips).
        gap_threshold = avg_bucket_vol * 0.3
        gaps: List[Dict[str, Any]] = []
        cur_start = None
        cur_vol = 0
        last_gap_price: Optional[float] = None
        for p, v in sorted_by_price:
            if v <= gap_threshold:
                if cur_start is None:
                    cur_start = p
                cur_vol += int(v)
                last_gap_price = p
            else:
                if cur_start is not None and last_gap_price is not None:
                    width_pips = round((last_gap_price - cur_start) / PIP_SIZE, 1)
                    gaps.append(
                        {
                            "price_range": f"{cur_start:.1f}-{last_gap_price:.1f}",
                            "width_pips": width_pips,
                            "volume": cur_vol,
                            "pct": round(cur_vol / total_vol * 100, 1),
                        }
                    )
                    cur_start = None
                    cur_vol = 0
                    last_gap_price = None
        # Trailing gap
        if cur_start is not None and last_gap_price is not None:
            width_pips = round((last_gap_price - cur_start) / PIP_SIZE, 1)
            gaps.append(
                {
                    "price_range": f"{cur_start:.1f}-{last_gap_price:.1f}",
                    "width_pips": width_pips,
                    "volume": cur_vol,
                    "pct": round(cur_vol / total_vol * 100, 1),
                }
            )
        gaps.sort(key=lambda g: -g["width_pips"])
        gaps = gaps[:3]

        # Current price context
        tick = mt5.symbol_info_tick(symbol)
        cur_price: Optional[float] = None
        if tick is not None and tick.bid and tick.ask:
            cur_price = (float(tick.bid) + float(tick.ask)) / 2.0

        nearest_hvn: Optional[Dict[str, Any]] = None
        in_va = False
        if cur_price is not None:
            if top_nodes:
                nearest = min(top_nodes, key=lambda n: abs(n["price"] - cur_price))
                nearest_hvn = {
                    "price": nearest["price"],
                    "dist_pips": round(abs(nearest["price"] - cur_price) / PIP_SIZE, 1),
                    "direction": "below" if nearest["price"] < cur_price else "above",
                }
            in_va = va_low <= cur_price <= va_high

        return {
            "window_hours": round(window_hours, 2),
            "timeframe": {
                mt5.TIMEFRAME_M1: "M1",
                mt5.TIMEFRAME_M5: "M5",
                mt5.TIMEFRAME_M15: "M15",
            }.get(tf, str(tf)),
            "bars_used": int(len(bars)),
            "bucket_size_points": bucket_size,
            "price_low": round(price_min, 2),
            "price_high": round(price_max, 2),
            "total_volume": int(total_vol),
            "poc": {"price": poc_price, "volume": int(poc_vol)},
            "value_area": {"low": va_low, "high": va_high},
            "top_nodes": top_nodes,
            "low_volume_gaps": gaps,
            "current_price_context": {
                "price": round(cur_price, 2) if cur_price is not None else None,
                "nearest_hvn": nearest_hvn,
                "in_value_area": in_va,
            },
        }
    except Exception as e:
        log.debug(f"volume_profile: compute failed: {e}")
        return None
