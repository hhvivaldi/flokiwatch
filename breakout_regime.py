"""FLO-422 — breakout regime snapshot computation.

Pure-compute helper. No MT5 calls, no DB reads, no logging. Caller fetches
inputs and passes them in; this module returns the snapshot dict matching
the schema in data/_design/FLO-422_breakout_regime_observability.md.

The same function is used at three call sites:
  - get_breakout_regime_metrics tool (active, Floki-facing)
  - submit_plan_to_snow auto-snapshot (passive, in ai_agent.py)
  - snow/snow_loop.py trigger-time snapshot

Identical schema at every site → trivial author-vs-trigger drift comparison.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def compute_regime_snapshot(
    *,
    ts: datetime,
    direction: Optional[str],
    setup_type: Optional[str],
    breakout_level: Optional[float],
    current_price: float,
    candles_m5: List[Dict[str, Any]],
    analyses_4h: List[Dict[str, Any]],
    analyses_24h: List[Dict[str, Any]],
    stage: str = "author",
) -> Dict[str, Any]:
    """Compute a regime snapshot. Pure function.

    Args:
      ts: aware UTC datetime — moment of the snapshot.
      direction: "BUY" or "SELL", or None for setup-agnostic snapshots.
      setup_type: copied through to the snapshot for downstream filtering.
      breakout_level: literal price the entry trigger references, or None.
      current_price: price at the snapshot moment.
      candles_m5: list of dicts with keys open/high/low/close (chronological,
        last >=26 M5 bars ending at `ts`). If <26 bars provided, m5 metrics
        degrade gracefully and add `insufficient_m5_history` warning.
      analyses_4h: list of dicts with keys timestamp/atr_14/bb_upper/bb_middle/
        bb_lower/rsi_14/adx_14/ema_50 covering the prior ~4 hours, chronological.
      analyses_24h: same shape, covering prior ~24 hours, chronological. Used
        only for `pre_range_24h_pips` and `range_ratio_4h_24h`.
      stage: "author" or "trigger". Recorded in the snapshot.

    Returns:
      Dict matching the schema. Always returns something — fields that can't
      be computed are None and reasons are listed in `computation_warnings`.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    warnings: List[str] = []

    # ---- M5 metrics ----
    m5_atr_pips: Optional[float] = None
    impulse_total_60m: Optional[int] = None
    candle_drift_trailing: Optional[int] = None
    m5_pattern: Optional[str] = None
    breakout_age_bars: Optional[int] = None

    if len(candles_m5) < 26:
        warnings.append("insufficient_m5_history")
    else:
        prior14 = candles_m5[-26:-12]
        last12 = candles_m5[-12:]
        # M5 ATR over prior 14 (excludes the 60-min window itself)
        trs: List[float] = []
        for i, b in enumerate(prior14):
            if i == 0:
                trs.append(b["high"] - b["low"])
            else:
                pc = prior14[i - 1]["close"]
                trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
        m5_atr = statistics.mean(trs) if trs else 0.0
        m5_atr_pips = round(m5_atr * 10, 2)

        if direction in ("BUY", "SELL") and m5_atr > 0:
            target_pos = direction == "BUY"
            pattern_chars: List[str] = []
            for b in last12:
                body = b["close"] - b["open"]
                same_dir = (body > 0) == target_pos
                magnitude_ok = abs(body) >= 0.5 * m5_atr
                if same_dir and magnitude_ok:
                    pattern_chars.append("+")
                elif same_dir:
                    pattern_chars.append(".")
                elif body == 0:
                    pattern_chars.append("o")
                else:
                    pattern_chars.append("-")
            m5_pattern = "".join(pattern_chars)
            impulse_total_60m = sum(1 for ch in pattern_chars if ch == "+")
            candle_drift_trailing = 0
            for ch in reversed(pattern_chars):
                if ch in ("+", "."):
                    candle_drift_trailing += 1
                else:
                    break

        # breakout_age_bars: M5 bars since price first crossed breakout_level
        if breakout_level is not None and direction in ("BUY", "SELL"):
            age = _count_bars_since_first_cross(candles_m5, breakout_level, direction)
            breakout_age_bars = age  # may be None if level hasn't been crossed yet in window

    # ---- 4h analyses-derived metrics ----
    bb_width_4h_pct: Optional[float] = None
    atr_4h_pct: Optional[float] = None
    pre_range_4h_pips: Optional[float] = None
    rsi_now: Optional[float] = None
    adx_now: Optional[float] = None
    bb_position_now: Optional[float] = None
    ema50_distance_atr: Optional[float] = None

    if len(analyses_4h) < 6:
        warnings.append("insufficient_4h_history")
    else:
        first = analyses_4h[0]
        last = analyses_4h[-1]
        # BB width Δ
        bbw_first = (first.get("bb_upper") or 0) - (first.get("bb_lower") or 0)
        bbw_last = (last.get("bb_upper") or 0) - (last.get("bb_lower") or 0)
        if bbw_first > 0:
            bb_width_4h_pct = round((bbw_last - bbw_first) / bbw_first * 100, 2)
        # ATR Δ
        atr_first = first.get("atr_14") or 0
        atr_last = last.get("atr_14") or 0
        if atr_first > 0:
            atr_4h_pct = round((atr_last - atr_first) / atr_first * 100, 2)
        # Range over 4h
        prices = [a.get("current_price") for a in analyses_4h if a.get("current_price")]
        if prices:
            pre_range_4h_pips = round((max(prices) - min(prices)) * 10, 1)
        # Snapshot indicators (now)
        rsi_now = last.get("rsi_14")
        adx_now = last.get("adx_14")
        bbu, bbm = last.get("bb_upper"), last.get("bb_middle")
        if bbu is not None and bbm is not None and bbu != bbm:
            bb_position_now = round((current_price - bbm) / (bbu - bbm), 3)
        ema50, atr14 = last.get("ema_50"), last.get("atr_14")
        if ema50 is not None and atr14 and atr14 > 0:
            ema50_distance_atr = round(abs(current_price - ema50) / atr14, 2)

    # ---- 24h range ----
    pre_range_24h_pips: Optional[float] = None
    range_ratio_4h_24h: Optional[float] = None
    if len(analyses_24h) < 12:
        # 24h history sparse — common during weekends or fresh restarts
        if "insufficient_4h_history" not in warnings:
            warnings.append("insufficient_24h_history")
    else:
        prices24 = [a.get("current_price") for a in analyses_24h if a.get("current_price")]
        if prices24:
            pre_range_24h_pips = round((max(prices24) - min(prices24)) * 10, 1)
            if pre_range_24h_pips and pre_range_4h_pips is not None and pre_range_24h_pips > 0:
                range_ratio_4h_24h = round(pre_range_4h_pips / pre_range_24h_pips, 3)

    # ---- breakout_distance ----
    breakout_distance_pips: Optional[float] = None
    if breakout_level is not None and direction in ("BUY", "SELL"):
        sign = 1 if direction == "BUY" else -1
        breakout_distance_pips = round((current_price - breakout_level) * 10 * sign, 1)

    return {
        "stage": stage,
        "ts": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "current_price": round(current_price, 2),
        "direction": direction,
        "setup_type": setup_type,
        "breakout_level": breakout_level,
        "breakout_distance_pips": breakout_distance_pips,
        "breakout_age_bars": breakout_age_bars,

        "impulse_total_60m": impulse_total_60m,
        "candle_drift_trailing": candle_drift_trailing,
        "m5_pattern": m5_pattern,
        "m5_atr_pips": m5_atr_pips,

        "bb_width_4h_pct": bb_width_4h_pct,
        "atr_4h_pct": atr_4h_pct,
        "pre_range_4h_pips": pre_range_4h_pips,
        "pre_range_24h_pips": pre_range_24h_pips,
        "range_ratio_4h_24h": range_ratio_4h_24h,

        "rsi_now": rsi_now,
        "adx_now": adx_now,
        "bb_position_now": bb_position_now,
        "ema50_distance_atr": ema50_distance_atr,

        "computation_warnings": warnings,
    }


def compute_drift(
    author_snapshot: Dict[str, Any],
    trigger_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Compute drift metrics between author-time and trigger-time snapshots.

    Both snapshots must follow the same schema (compute_regime_snapshot output).
    Returns a flat dict of deltas plus a categorical `drift_assessment`.
    """
    def _delta(key: str) -> Optional[float]:
        a, t = author_snapshot.get(key), trigger_snapshot.get(key)
        if a is None or t is None:
            return None
        return round(t - a, 3)

    delta_seconds: Optional[float] = None
    try:
        a_ts = datetime.fromisoformat(author_snapshot["ts"].replace("Z", "+00:00"))
        t_ts = datetime.fromisoformat(trigger_snapshot["ts"].replace("Z", "+00:00"))
        delta_seconds = (t_ts - a_ts).total_seconds()
    except (KeyError, ValueError):
        pass

    a_px = author_snapshot.get("current_price")
    t_px = trigger_snapshot.get("current_price")
    direction = author_snapshot.get("direction")
    price_change_pips: Optional[float] = None
    if a_px is not None and t_px is not None and direction in ("BUY", "SELL"):
        sign = 1 if direction == "BUY" else -1
        price_change_pips = round((t_px - a_px) * 10 * sign, 1)

    drift = {
        "delta_seconds_author_to_trigger": delta_seconds,
        "price_change_pips": price_change_pips,
        "impulse_total_delta": _delta("impulse_total_60m"),
        "bb_width_4h_pct_delta": _delta("bb_width_4h_pct"),
        "atr_4h_pct_delta": _delta("atr_4h_pct"),
        "candle_drift_trailing_delta": _delta("candle_drift_trailing"),
        "breakout_age_bars_at_trigger": trigger_snapshot.get("breakout_age_bars"),
        "drift_assessment": _classify_drift(author_snapshot, trigger_snapshot),
    }
    return drift


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------

def _count_bars_since_first_cross(
    candles_m5: List[Dict[str, Any]],
    breakout_level: float,
    direction: str,
) -> Optional[int]:
    """Count M5 bars since price first crossed breakout_level.

    For BUY: first bar whose high >= level (price reached the level from below).
    For SELL: first bar whose low <= level (price reached from above).
    Returns None if level was never crossed in the provided window.
    """
    target_high = direction == "BUY"
    first_cross_idx: Optional[int] = None
    for i, b in enumerate(candles_m5):
        if target_high and b["high"] >= breakout_level:
            first_cross_idx = i
            break
        if (not target_high) and b["low"] <= breakout_level:
            first_cross_idx = i
            break
    if first_cross_idx is None:
        return None
    return len(candles_m5) - 1 - first_cross_idx


def _classify_drift(author: Dict[str, Any], trigger: Dict[str, Any]) -> str:
    """Tentative qualitative drift classification.

    Thresholds are intentionally conservative. After 30 days of data we can
    refine the cutoffs without changing the snapshot schema.
    """
    a_imp = author.get("impulse_total_60m")
    t_imp = trigger.get("impulse_total_60m")
    a_bbw = author.get("bb_width_4h_pct")
    t_bbw = trigger.get("bb_width_4h_pct")

    if None in (a_imp, t_imp, a_bbw, t_bbw):
        return "insufficient_data"

    impulse_delta = t_imp - a_imp
    bbw_delta = t_bbw - a_bbw

    if impulse_delta >= 2 or bbw_delta >= 20:
        return "regime_expanded"
    if impulse_delta <= -2 or bbw_delta <= -20:
        return "regime_compressed"

    # setup_invalidated heuristic: breakout_distance_pips dropped sharply or
    # ATR collapsed (volatility-event passed). Conservative — defer until v2.

    return "regime_stable"
