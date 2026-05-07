"""FLO-425 PR-A — breakout lifecycle classifier (v0 heuristic).

Pure function. Consumes a plan + author/trigger regime snapshots
(from breakout_regime.compute_regime_snapshot, FLO-422 Step 3 / 5)
+ M5 candles, and emits a structured classification.

Architectural frame (FLO-425 §16-19):
  A breakout is a process: BUILDUP -> BREAK_ATTEMPT -> ACCEPTANCE_TEST
  -> ACCEPTED -> CONTINUATION (and risks: EXHAUSTION / FAILURE).
  This module produces the lifecycle classification at any eval_ts;
  shadow consumers log it; production gating is deferred (PR-C).

Safety contract:
  - PURE: no DB, no MT5, no network, no logging from inside.
  - NEVER raises. Top-level entry point catches every exception class
    and returns a structured INSUFFICIENT_DATA dict with a warning.
  - EVERY output key always present. None where unknown. Schema-stable.

Heuristic posture:
  - Thresholds are explicit constants at module top.
  - v0 is a STRUCTURAL SKELETON, not a calibrated model. Tuning
    happens after the historical backtest in
    data/_audits/_breakout_lifecycle_backtest.py reveals which
    constants discriminate.
  - No learned weights. No ML. Each score traceable to a §17/§18
    dimension; each phase rule traceable to §16 lifecycle.
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------
# Heuristic thresholds — explicit constants. Tune via backtest.
# ---------------------------------------------------------------------
SCHEMA_VERSION = 1

# Phase selector
MIN_CANDLES_FOR_CLASSIFICATION = 20

# Freshness decay
FRESHNESS_BARS_HORIZON = 12  # bars_since_cross beyond which freshness -> 0
FRESHNESS_BBW_HORIZON_PCT = 100.0  # bbw_4h_pct beyond which freshness damped to 0
FRESHNESS_EMA50_HORIZON_ATR = 4.0  # ema50_distance_atr beyond which freshness damped to 0

# Maturity
MATURITY_BARS_HORIZON = 12
MATURITY_CLOSES_BONUS_DIVISOR = 4  # consecutive_closes_beyond_level / 4 → bonus
MATURITY_BONUS_WEIGHT = 0.2

# Acceptance quality — drift severity mapping
DRIFT_SEVERITY = {
    "regime_stable": 0.0,
    "regime_compressed": 0.3,
    "regime_expanded": 0.5,
    "insufficient_data": None,
}

# Exhaustion drivers (each clamped to [0, 1], max-aggregated)
EXHAUSTION_EMA50_BASELINE_ATR = 1.5
EXHAUSTION_EMA50_FULL_ATR = 4.0
EXHAUSTION_RSI_ONSET = 70.0
EXHAUSTION_RSI_FULL = 85.0
EXHAUSTION_BBW_ONSET_PCT = 25.0
EXHAUSTION_BBW_FULL_PCT = 85.0
EXHAUSTION_IMPULSE_FULL_BARS = 8

# Phase-rule thresholds
ACCEPTANCE_TEST_BARS_RANGE = (1, 4)
ACCEPTED_MIN_CONSECUTIVE_CLOSES = 2
CONTINUATION_MIN_BARS = 6
CONTINUATION_MIN_EXTENSION_ATR_MULTIPLE = 1.0
CONTINUATION_MAX_EMA50_DISTANCE_ATR = 2.5
EXHAUSTION_GATE_EMA50_DISTANCE_ATR = 3.0
EXHAUSTION_GATE_RSI = 80.0
EXHAUSTION_GATE_BBW_PCT = 50.0
EXHAUSTION_GATE_IMPULSE = 6

# ATR window (M5)
M5_ATR_PERIOD = 14


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------
def classify_breakout_lifecycle(
    *,
    plan_dict: Dict[str, Any],
    author_snapshot: Optional[Dict[str, Any]],
    trigger_snapshot: Optional[Dict[str, Any]],
    candles_m5: List[Dict[str, Any]],
    eval_ts: datetime,
) -> Dict[str, Any]:
    """Pure classifier. Never raises. Always returns full schema.

    Args:
      plan_dict:        full plan JSON; uses analysis.setup_type,
                        entry.direction, entry.entry_price.
      author_snapshot:  FLO-422 Step 3 author-time regime snapshot, or None.
      trigger_snapshot: FLO-422 Step 5 trigger-time regime snapshot, or None.
      candles_m5:       chronological list of dicts with open/high/low/close,
                        last bar = eval_ts neighborhood. Need >=20 bars
                        for full classification; <20 → INSUFFICIENT_DATA.
      eval_ts:          UTC datetime; informational, not load-bearing.

    Returns:
      Dict with keys: phase, phase_confidence, breakout_freshness,
      breakout_maturity, acceptance_quality, exhaustion_probability,
      reasons, warnings, inputs_used, schema_version.
    """
    try:
        return _classify_inner(
            plan_dict=plan_dict,
            author_snapshot=author_snapshot,
            trigger_snapshot=trigger_snapshot,
            candles_m5=candles_m5,
            eval_ts=eval_ts,
        )
    except Exception as e:
        return _empty_result(
            phase="INSUFFICIENT_DATA",
            warnings=[f"classifier_uncaught:{type(e).__name__}:{e}"],
        )


# ---------------------------------------------------------------------
# Inner classifier — separated so the public entry point can wrap
# every code path in a fail-soft try/except.
# ---------------------------------------------------------------------
def _classify_inner(
    *,
    plan_dict: Dict[str, Any],
    author_snapshot: Optional[Dict[str, Any]],
    trigger_snapshot: Optional[Dict[str, Any]],
    candles_m5: List[Dict[str, Any]],
    eval_ts: datetime,
) -> Dict[str, Any]:
    warnings: List[str] = []

    # --- input validation ---
    if not isinstance(plan_dict, dict):
        return _empty_result(
            phase="INSUFFICIENT_DATA",
            warnings=["plan_dict_not_dict"],
        )
    if not isinstance(candles_m5, list):
        return _empty_result(
            phase="INSUFFICIENT_DATA",
            warnings=["candles_m5_not_list"],
        )
    if len(candles_m5) < MIN_CANDLES_FOR_CLASSIFICATION:
        return _empty_result(
            phase="INSUFFICIENT_DATA",
            warnings=[f"insufficient_candles:{len(candles_m5)}<{MIN_CANDLES_FOR_CLASSIFICATION}"],
        )

    analysis = (plan_dict.get("analysis") or {}) if isinstance(plan_dict.get("analysis"), dict) else {}
    entry = (plan_dict.get("entry") or {}) if isinstance(plan_dict.get("entry"), dict) else {}
    direction = entry.get("direction")
    entry_price = entry.get("entry_price")
    setup_type = analysis.get("setup_type")

    if direction not in ("BUY", "SELL") or not isinstance(entry_price, (int, float)):
        return _empty_result(
            phase="INSUFFICIENT_DATA",
            warnings=[f"plan_missing_direction_or_entry_price"],
        )

    # --- compute metrics ---
    metrics = _compute_metrics(
        direction=direction,
        entry_price=float(entry_price),
        author_snapshot=author_snapshot,
        trigger_snapshot=trigger_snapshot,
        candles_m5=candles_m5,
    )

    if author_snapshot is None:
        warnings.append("missing_author_snapshot")
    if trigger_snapshot is None:
        warnings.append("missing_trigger_snapshot")
    if author_snapshot is None or trigger_snapshot is None:
        metrics["drift_assessment"] = None  # cannot compute drift without both

    # --- score each dimension ---
    freshness, fr_reasons = _score_freshness(metrics)
    maturity, ma_reasons = _score_maturity(metrics)
    acceptance, aq_reasons = _score_acceptance_quality(metrics)
    exhaustion, ex_reasons = _score_exhaustion(metrics)

    # --- select phase ---
    phase, phase_confidence, phase_reasons = _select_phase(metrics, {
        "freshness": freshness,
        "maturity": maturity,
        "acceptance": acceptance,
        "exhaustion": exhaustion,
    })

    return {
        "phase": phase,
        "phase_confidence": phase_confidence,
        "breakout_freshness": freshness,
        "breakout_maturity": maturity,
        "acceptance_quality": acceptance,
        "exhaustion_probability": exhaustion,
        "reasons": phase_reasons + fr_reasons + ma_reasons + aq_reasons + ex_reasons,
        "warnings": warnings,
        "inputs_used": metrics,
        "schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------
# Metric computation
# ---------------------------------------------------------------------
def _compute_metrics(
    *,
    direction: str,
    entry_price: float,
    author_snapshot: Optional[Dict[str, Any]],
    trigger_snapshot: Optional[Dict[str, Any]],
    candles_m5: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pure compute. Returns a flat dict of named metrics. None where
    a value can't be computed."""
    n = len(candles_m5)
    last = candles_m5[-1]
    last_close = float(last["close"])

    # cross detection: first index where price crossed the level on the
    # broken side. BUY: high > level. SELL: low < level.
    cross_idx: Optional[int] = None
    for i, b in enumerate(candles_m5):
        h = float(b["high"]); l = float(b["low"])
        if direction == "BUY" and h > entry_price:
            cross_idx = i
            break
        if direction == "SELL" and l < entry_price:
            cross_idx = i
            break

    bars_since_cross: Optional[int] = (n - 1 - cross_idx) if cross_idx is not None else None

    # consecutive closes beyond level, walking back from end
    consecutive_closes_beyond_level = 0
    for b in reversed(candles_m5):
        c = float(b["close"])
        if direction == "BUY" and c > entry_price:
            consecutive_closes_beyond_level += 1
        elif direction == "SELL" and c < entry_price:
            consecutive_closes_beyond_level += 1
        else:
            break

    # post-cross close stats
    post_cross_bars_total = 0
    post_cross_closes_beyond = 0
    post_cross_same_dir_closes = 0  # close > open for BUY; close < open for SELL
    if cross_idx is not None:
        for b in candles_m5[cross_idx:]:
            post_cross_bars_total += 1
            c = float(b["close"]); o = float(b["open"])
            beyond = (direction == "BUY" and c > entry_price) or \
                     (direction == "SELL" and c < entry_price)
            same_dir = (direction == "BUY" and c > o) or \
                       (direction == "SELL" and c < o)
            if beyond:
                post_cross_closes_beyond += 1
            if same_dir:
                post_cross_same_dir_closes += 1

    fraction_post_cross_beyond = (
        post_cross_closes_beyond / post_cross_bars_total
        if post_cross_bars_total > 0 else None
    )
    same_direction_close_ratio = (
        post_cross_same_dir_closes / post_cross_bars_total
        if post_cross_bars_total > 0 else None
    )

    # has price closed back through level AFTER having accepted beyond?
    # FAILURE requires that acceptance was at least partially established
    # (>=1 close beyond level) and price has now returned to the pre-break
    # side. A wick-only cross that never closed beyond is a BREAK_ATTEMPT
    # that rejected, not a FAILURE — those classify as BREAK_ATTEMPT.
    closed_back_through_level = False
    if cross_idx is not None:
        had_close_beyond = False
        for b in candles_m5[cross_idx:]:
            c = float(b["close"])
            if direction == "BUY" and c > entry_price:
                had_close_beyond = True
                break
            if direction == "SELL" and c < entry_price:
                had_close_beyond = True
                break
        most_recent_on_pre_break = (
            (direction == "BUY" and last_close < entry_price) or
            (direction == "SELL" and last_close > entry_price)
        )
        closed_back_through_level = had_close_beyond and most_recent_on_pre_break

    # M5 ATR (last 14 bars), in pips
    m5_atr_pips_recent: Optional[float] = None
    if n >= M5_ATR_PERIOD + 1:
        trs: List[float] = []
        for i in range(n - M5_ATR_PERIOD, n):
            b = candles_m5[i]
            prev_close = float(candles_m5[i - 1]["close"])
            h = float(b["high"]); l = float(b["low"])
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            trs.append(tr)
        if trs:
            m5_atr_pips_recent = round(sum(trs) / len(trs) * 10, 2)

    # extension beyond level (in pips, signed by direction)
    extension_pips: Optional[float] = None
    if direction == "BUY":
        extension_pips = round((last_close - entry_price) * 10, 1)
    else:
        extension_pips = round((entry_price - last_close) * 10, 1)

    # breakout candle body (pips, signed by direction)
    breakout_candle_body_pips: Optional[float] = None
    if cross_idx is not None:
        b = candles_m5[cross_idx]
        body = float(b["close"]) - float(b["open"])
        breakout_candle_body_pips = round(
            body * 10 if direction == "BUY" else -body * 10, 1
        )

    # regime fields: prefer trigger over author, fall back gracefully
    def _regime(field: str) -> Optional[float]:
        if trigger_snapshot and trigger_snapshot.get(field) is not None:
            v = trigger_snapshot.get(field)
            return float(v) if isinstance(v, (int, float)) else None
        if author_snapshot and author_snapshot.get(field) is not None:
            v = author_snapshot.get(field)
            return float(v) if isinstance(v, (int, float)) else None
        return None

    bbw_4h_pct = _regime("bb_width_4h_pct")
    atr_4h_pct = _regime("atr_4h_pct")
    ema50_distance_atr = _regime("ema50_distance_atr")
    rsi_now = _regime("rsi_now")
    adx_now = _regime("adx_now")
    impulse_total_60m = _regime("impulse_total_60m")
    if impulse_total_60m is not None:
        impulse_total_60m = int(impulse_total_60m)

    # drift assessment — only if both snapshots present and shaped right
    drift_assessment: Optional[str] = None
    drift_price_change_pips: Optional[float] = None
    if author_snapshot and trigger_snapshot:
        try:
            from breakout_regime import compute_drift
            drift = compute_drift(author_snapshot, trigger_snapshot)
            drift_assessment = drift.get("drift_assessment")
            v = drift.get("price_change_pips")
            if isinstance(v, (int, float)):
                drift_price_change_pips = float(v)
        except Exception:
            drift_assessment = None

    return {
        "direction": direction,
        "entry_price": entry_price,
        "last_close": last_close,
        "n_candles": n,
        "cross_idx": cross_idx,
        "bars_since_cross": bars_since_cross,
        "consecutive_closes_beyond_level": consecutive_closes_beyond_level,
        "post_cross_bars_total": post_cross_bars_total,
        "post_cross_closes_beyond": post_cross_closes_beyond,
        "fraction_post_cross_beyond": fraction_post_cross_beyond,
        "same_direction_close_ratio": same_direction_close_ratio,
        "closed_back_through_level": closed_back_through_level,
        "m5_atr_pips_recent": m5_atr_pips_recent,
        "extension_pips": extension_pips,
        "breakout_candle_body_pips": breakout_candle_body_pips,
        "bb_width_4h_pct": bbw_4h_pct,
        "atr_4h_pct": atr_4h_pct,
        "ema50_distance_atr": ema50_distance_atr,
        "rsi_now": rsi_now,
        "adx_now": adx_now,
        "impulse_total_60m": impulse_total_60m,
        "drift_assessment": drift_assessment,
        "drift_price_change_pips": drift_price_change_pips,
    }


# ---------------------------------------------------------------------
# Score functions — each returns (score_or_None, reasons)
# ---------------------------------------------------------------------
def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _score_freshness(m: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    """High when bars_since_cross small AND regime not in late-expansion AND
    impulse just starting."""
    bars = m.get("bars_since_cross")
    bbw = m.get("bb_width_4h_pct")
    ema_d = m.get("ema50_distance_atr")
    if bars is None:
        return None, ["freshness:no_cross_yet"]

    bars_factor = max(0.0, 1.0 - bars / FRESHNESS_BARS_HORIZON)
    bbw_factor = 1.0 - _clamp((bbw or 0.0) / FRESHNESS_BBW_HORIZON_PCT) \
        if bbw is not None else 1.0
    ema_factor = 1.0 - _clamp((ema_d or 0.0) / FRESHNESS_EMA50_HORIZON_ATR) \
        if ema_d is not None else 1.0
    score = round(bars_factor * bbw_factor * ema_factor, 3)

    reasons = [f"freshness:bars_factor={bars_factor:.2f}"]
    if bbw is not None:
        reasons.append(f"freshness:bbw_factor={bbw_factor:.2f}")
    if ema_d is not None:
        reasons.append(f"freshness:ema_factor={ema_factor:.2f}")
    return score, reasons


def _score_maturity(m: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    """Increases with bars_since_cross AND consecutive_closes_beyond_level."""
    bars = m.get("bars_since_cross")
    if bars is None:
        return None, ["maturity:no_cross_yet"]
    closes = m.get("consecutive_closes_beyond_level") or 0

    base = _clamp(bars / MATURITY_BARS_HORIZON)
    bonus = MATURITY_BONUS_WEIGHT * (closes / MATURITY_CLOSES_BONUS_DIVISOR)
    score = round(_clamp(base * (1.0 + bonus)), 3)
    return score, [f"maturity:bars={bars} consec_closes={closes}"]


def _score_acceptance_quality(m: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    """Composite of §17c five dimensions, simplified to v0:
       time, structural, participation, thesis_preservation."""
    if m.get("bars_since_cross") is None:
        return None, ["acceptance:no_cross_yet"]

    dims: List[Tuple[str, float]] = []

    # time
    frac_beyond = m.get("fraction_post_cross_beyond")
    if frac_beyond is not None:
        dims.append(("time", _clamp(frac_beyond)))

    # structural — consecutive closes
    closes = m.get("consecutive_closes_beyond_level")
    if closes is not None:
        dims.append(("structural", _clamp(closes / 3.0)))

    # participation — directional closes
    same_dir = m.get("same_direction_close_ratio")
    if same_dir is not None:
        dims.append(("participation", _clamp(same_dir)))

    # thesis preservation — derived from drift
    drift = m.get("drift_assessment")
    if drift in DRIFT_SEVERITY:
        sev = DRIFT_SEVERITY[drift]
        if sev is not None:
            dims.append(("thesis_preservation", _clamp(1.0 - sev)))

    if not dims:
        return None, ["acceptance:no_dimensions_computable"]

    score = round(sum(v for _, v in dims) / len(dims), 3)
    reasons = [f"acceptance:{name}={v:.2f}" for name, v in dims]
    return score, reasons


def _score_exhaustion(m: Dict[str, Any]) -> Tuple[Optional[float], List[str]]:
    """Max-aggregated across exhaustion drivers. Each driver clamped to [0,1].
    Returns None if no driver inputs are available."""
    drivers: List[Tuple[str, float]] = []

    ema_d = m.get("ema50_distance_atr")
    if ema_d is not None:
        v = _clamp(
            (ema_d - EXHAUSTION_EMA50_BASELINE_ATR)
            / (EXHAUSTION_EMA50_FULL_ATR - EXHAUSTION_EMA50_BASELINE_ATR)
        )
        drivers.append(("ema50_distance", v))

    rsi = m.get("rsi_now")
    if rsi is not None:
        v = _clamp(
            (rsi - EXHAUSTION_RSI_ONSET)
            / (EXHAUSTION_RSI_FULL - EXHAUSTION_RSI_ONSET)
        )
        drivers.append(("rsi", v))

    bbw = m.get("bb_width_4h_pct")
    if bbw is not None:
        v = _clamp(
            (bbw - EXHAUSTION_BBW_ONSET_PCT)
            / (EXHAUSTION_BBW_FULL_PCT - EXHAUSTION_BBW_ONSET_PCT)
        )
        drivers.append(("bbw_4h", v))

    impulse = m.get("impulse_total_60m")
    if impulse is not None:
        v = _clamp(impulse / EXHAUSTION_IMPULSE_FULL_BARS)
        drivers.append(("impulse_60m", v))

    if not drivers:
        return None, ["exhaustion:no_driver_inputs"]

    score = round(max(v for _, v in drivers), 3)
    top = max(drivers, key=lambda x: x[1])
    reasons = [f"exhaustion:dominant={top[0]}={top[1]:.2f}"]
    for name, v in drivers:
        if v >= 0.3 and name != top[0]:
            reasons.append(f"exhaustion:also_{name}={v:.2f}")
    return score, reasons


# ---------------------------------------------------------------------
# Phase selector — decision tree
# ---------------------------------------------------------------------
def _select_phase(
    m: Dict[str, Any],
    scores: Dict[str, Optional[float]],
) -> Tuple[str, Optional[float], List[str]]:
    """Decision tree per FLO-425 §16-18. Returns (phase, confidence, reasons).
    Confidence is heuristic: 1.0 unambiguous, 0.6 partial-input fallback,
    0.3 ambiguous."""
    bars = m.get("bars_since_cross")
    consec = m.get("consecutive_closes_beyond_level") or 0
    closed_back = m.get("closed_back_through_level")
    ema_d = m.get("ema50_distance_atr")
    rsi = m.get("rsi_now")
    bbw = m.get("bb_width_4h_pct")
    impulse = m.get("impulse_total_60m")
    extension = m.get("extension_pips")
    m5_atr = m.get("m5_atr_pips_recent")

    last_close = m.get("last_close")
    direction = m.get("direction")
    entry_price = m.get("entry_price")
    last_close_beyond = (
        (direction == "BUY" and last_close is not None and last_close > entry_price)
        or (direction == "SELL" and last_close is not None and last_close < entry_price)
    )

    # Rule 1: BUILDUP — level not yet crossed
    if bars is None:
        return "BUILDUP", 1.0, ["phase:BUILDUP:level_not_crossed"]

    # Rule 8 (checked early so failure trumps acceptance fragments):
    # price closed back through level after a prior cross
    if closed_back:
        return "FAILURE", 1.0, [
            f"phase:FAILURE:closed_back_through_level "
            f"bars_since_cross={bars} consec_beyond={consec} last_close={last_close}"
        ]

    # Rule 2: BREAK_ATTEMPT — just crossed, no acceptance yet
    if bars in (0, 1) and consec == 0:
        confidence = 1.0 if last_close_beyond is False else 0.6
        return "BREAK_ATTEMPT", confidence, [
            f"phase:BREAK_ATTEMPT:bars={bars} consec_beyond={consec}"
        ]

    # Rule 3: ACCEPTANCE_TEST — short window, mixed closes
    in_test_window = ACCEPTANCE_TEST_BARS_RANGE[0] <= bars <= ACCEPTANCE_TEST_BARS_RANGE[1]
    frac_beyond = m.get("fraction_post_cross_beyond")
    mixed = (frac_beyond is not None) and (0.0 < frac_beyond < 1.0)
    accepted_yet = consec >= ACCEPTED_MIN_CONSECUTIVE_CLOSES and last_close_beyond
    if in_test_window and mixed and not accepted_yet:
        return "ACCEPTANCE_TEST", 0.8, [
            f"phase:ACCEPTANCE_TEST:bars={bars} frac_beyond={frac_beyond:.2f}"
        ]

    # Rule 4-6-7: ACCEPTED / CONTINUATION / EXHAUSTION
    if accepted_yet:
        # exhaustion gate (overrides continuation if regime is already extended)
        is_exhausted = (
            (ema_d is not None and ema_d >= EXHAUSTION_GATE_EMA50_DISTANCE_ATR)
            or (rsi is not None and rsi >= EXHAUSTION_GATE_RSI)
            or (bbw is not None and bbw >= EXHAUSTION_GATE_BBW_PCT)
            or (impulse is not None and impulse >= EXHAUSTION_GATE_IMPULSE)
        )
        if is_exhausted:
            return "EXHAUSTION", 0.9, [
                f"phase:EXHAUSTION:ema_d={ema_d} rsi={rsi} "
                f"bbw={bbw} impulse_60m={impulse}"
            ]
        # CONTINUATION criteria
        ext_ok = (
            extension is not None and m5_atr is not None
            and extension >= CONTINUATION_MIN_EXTENSION_ATR_MULTIPLE * m5_atr
        )
        ema_ok = (ema_d is None) or (ema_d < CONTINUATION_MAX_EMA50_DISTANCE_ATR)
        if bars >= CONTINUATION_MIN_BARS and ext_ok and ema_ok:
            return "CONTINUATION", 0.9, [
                f"phase:CONTINUATION:bars={bars} ext={extension} "
                f"m5_atr={m5_atr} ema_d={ema_d}"
            ]
        return "ACCEPTED", 0.85, [
            f"phase:ACCEPTED:bars={bars} consec_beyond={consec}"
        ]

    # Fallback: cross happened but no rule matches cleanly
    if bars > ACCEPTANCE_TEST_BARS_RANGE[1] and consec == 0:
        # post-test window without acceptance and not closed_back yet —
        # ambiguous; treat as FAILURE-trending if last close on pre-break side
        if last_close_beyond is False:
            return "FAILURE", 0.6, [
                f"phase:FAILURE:post_test_no_acceptance bars={bars}"
            ]
    return "BREAK_ATTEMPT", 0.3, [
        f"phase:BREAK_ATTEMPT:fallback bars={bars} consec_beyond={consec}"
    ]


# ---------------------------------------------------------------------
# Empty-result helper for fail-soft paths
# ---------------------------------------------------------------------
def _empty_result(phase: str, warnings: List[str]) -> Dict[str, Any]:
    return {
        "phase": phase,
        "phase_confidence": None,
        "breakout_freshness": None,
        "breakout_maturity": None,
        "acceptance_quality": None,
        "exhaustion_probability": None,
        "reasons": [],
        "warnings": list(warnings),
        "inputs_used": {},
        "schema_version": SCHEMA_VERSION,
    }


__all__ = [
    "classify_breakout_lifecycle",
    "SCHEMA_VERSION",
]
