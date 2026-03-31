"""
FLO-139: Market regime detection.

Classifies the current market into one of 7 regimes using ALL available
indicators. Zero AI cost — pure Python logic.

Priority order: VOLATILE > QUIET > BREAKOUT_IMMINENT > TRENDING_BULLISH >
TRENDING_BEARISH > RANGING > TRANSITIONAL
"""

import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from logger import log


# ---------------------------------------------------------------------------
# Temporal state (survives between cycles, not restarts)
# ---------------------------------------------------------------------------
_regime_history: List[Dict[str, Any]] = []  # [{timestamp, old, new}]
_last_regime: Optional[str] = None
_last_regime_change_ts: Optional[float] = None
_prev_adx: Optional[float] = None
_prev_bollinger_width: Optional[float] = None


def detect_market_regime(
    tech_data: Dict[str, Any],
    momentum_data: Dict[str, Any],
    vol_status: Dict[str, Any],
    brain_result: Any,
    current_price: float,
    atr_history: List[float],
    luna_brief: Optional[Dict[str, Any]] = None,
    m5_data: Optional[Dict[str, Any]] = None,
    h1_candles: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Detect market regime from raw analysis data.

    Args:
        tech_data: from analyze_technical_detailed()
        momentum_data: from analyze_momentum()
        vol_status: from get_volatility_status()
        brain_result: from analyze_with_brain()
        current_price: current gold price
        atr_history: rolling ATR values (last 120)
        luna_brief: optional Luna brief for VIX
        m5_data: from get_m5_status() — M5 candle summary
        h1_candles: list of H1 OHLCV dicts (most recent last)

    Returns:
        Dict with regime, confidence, evidence, temporal context
    """
    global _regime_history, _last_regime, _last_regime_change_ts
    global _prev_adx, _prev_bollinger_width

    now = time.time()

    # -----------------------------------------------------------------------
    # Extract indicators (safe defaults)
    # -----------------------------------------------------------------------
    ema_data = tech_data.get("ema", {})
    ema9 = _sf(ema_data.get("ema9"))
    ema21 = _sf(ema_data.get("ema21"))
    ema50 = _sf(ema_data.get("ema50"))

    adx_data = momentum_data.get("adx", {})
    adx = _sf(adx_data.get("adx_value"))
    plus_di = _sf(adx_data.get("plus_di"))
    minus_di = _sf(adx_data.get("minus_di"))

    atr_data = momentum_data.get("atr", {})
    atr_current = _sf(atr_data.get("atr_value"))

    vol_data = momentum_data.get("volume", {})
    volume_ratio = _sf(vol_data.get("volume_ratio"), default=1.0)

    rsi_val = _sf(tech_data.get("rsi", {}).get("value"), default=50.0)

    macd_data = tech_data.get("macd", {})
    macd_hist = _sf(macd_data.get("histogram"))

    bb_data = tech_data.get("bollinger", {})
    bb_upper = _sf(bb_data.get("upper"))
    bb_lower = _sf(bb_data.get("lower"))
    bb_width = (bb_upper - bb_lower) if bb_upper and bb_lower else 0

    volatility_status = str(vol_status.get("status", "NORMAL")).upper()

    mtf_d1 = None
    mtf_h4 = None
    try:
        mtf = getattr(brain_result, "mtf_trend", None)
        if isinstance(mtf, dict):
            mtf_d1 = mtf.get("d1_direction")
            mtf_h4 = mtf.get("h4_direction")
    except Exception:
        pass

    # ATR ratio vs 5-day rolling average
    atr_ratio = 1.0
    if atr_history and len(atr_history) > 20 and atr_current:
        avg = sum(atr_history) / len(atr_history)
        if avg > 0:
            atr_ratio = atr_current / avg

    # VIX from Luna (optional)
    vix_value = None
    try:
        if luna_brief and isinstance(luna_brief, dict):
            ds = luna_brief.get("data_snapshot", {})
            vix_value = _sf(ds.get("vix", {}).get("value"))
    except Exception:
        pass

    # Previous Bollinger width for expansion check
    bb_expanding = False
    if _prev_bollinger_width and _prev_bollinger_width > 0 and bb_width > 0:
        bb_expanding = bb_width > _prev_bollinger_width * 1.5

    # -----------------------------------------------------------------------
    # REGIME 1: VOLATILE (highest priority)
    # -----------------------------------------------------------------------
    volatile_signals = []
    if volatility_status in ("EXTREME", "COOLING_DOWN", "COOLING"):
        volatile_signals.append(f"Volatility Guard: {volatility_status}")
    if atr_ratio > 2.0:
        volatile_signals.append(f"ATR {atr_current:.1f} is {atr_ratio:.1f}x average")
    if vix_value and vix_value > 30:
        volatile_signals.append(f"VIX {vix_value:.1f} > 30")
    if bb_expanding:
        volatile_signals.append("Bollinger bands expanding rapidly")

    if volatile_signals:
        regime = "VOLATILE"
        confidence = "high" if len(volatile_signals) >= 2 else "moderate"
        evidence = volatile_signals
        result = _build_result(regime, confidence, evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # -----------------------------------------------------------------------
    # REGIME 2: QUIET
    # -----------------------------------------------------------------------
    quiet_signals = 0
    quiet_evidence = []
    if atr_ratio < 0.5:
        quiet_signals += 1
        quiet_evidence.append(f"ATR {atr_current:.1f} is {atr_ratio:.2f}x average (very low)")
    if volume_ratio < 0.5:
        quiet_signals += 1
        quiet_evidence.append(f"Volume {volume_ratio:.2f}x average (very thin)")
    if adx and adx < 15:
        quiet_signals += 1
        quiet_evidence.append(f"ADX {adx:.1f} (no trend)")
    # Use ATR as proxy for "normal" band width since we don't track bb_width history
    atr_avg = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history and len(atr_history) >= 20 else 0
    # Bollinger width < 2x ATR average indicates tight bands (normal width ~3-4x ATR)
    if bb_width > 0 and atr_avg > 0 and bb_width < atr_avg * 2.0:
        quiet_signals += 1
        quiet_evidence.append("Bollinger squeeze (tight bands)")

    if quiet_signals >= 3:
        regime = "QUIET"
        confidence = "high" if quiet_signals >= 4 else "moderate"
        result = _build_result(regime, confidence, quiet_evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # -----------------------------------------------------------------------
    # FLO-151 LAYER 1: M5 fast detection (early warning, ~30 min)
    # -----------------------------------------------------------------------
    m5_trending_signals = 0
    m5_trending_evidence = []
    m5_direction = None  # "bullish" or "bearish"

    if m5_data and isinstance(m5_data, dict):
        m5_green = m5_data.get("green_count", 0) or 0
        m5_red = m5_data.get("red_count", 0) or 0
        m5_move = abs(m5_data.get("move_pct", 0) or 0)
        m5_total = m5_green + m5_red

        # Criterion 5: M5 momentum burst — 6+ of last 10 candles same direction
        if m5_total >= 6:
            if m5_green >= 6:
                m5_trending_signals += 1
                m5_trending_evidence.append(f"M5 burst: {m5_green}g/{m5_red}r (bullish)")
                m5_direction = "bullish"
            elif m5_red >= 6:
                m5_trending_signals += 1
                m5_trending_evidence.append(f"M5 burst: {m5_green}g/{m5_red}r (bearish)")
                m5_direction = "bearish"

        # Criterion 6: M5 rate of change — moved > 1x ATR in last 30 min
        if m5_move > 0 and atr_current and atr_current > 0:
            move_points = m5_move / 100.0 * current_price if current_price else 0
            if move_points > atr_current:
                m5_trending_signals += 1
                m5_trending_evidence.append(f"M5 move {move_points:.1f}pts > ATR {atr_current:.1f}")
                if not m5_direction:
                    m5_direction = "bullish" if (m5_data.get("move_pct", 0) or 0) > 0 else "bearish"

        # Criterion 7: M5 volume surge (from momentum_data if available)
        if volume_ratio > 2.0:
            m5_trending_signals += 1
            m5_trending_evidence.append(f"Volume surge {volume_ratio:.1f}x avg")

    # -----------------------------------------------------------------------
    # FLO-151 LAYER 2: H1 confirmation (3+ hours)
    # -----------------------------------------------------------------------
    h1_trending_signals = 0
    h1_trending_evidence = []
    h1_direction = None

    if h1_candles and isinstance(h1_candles, list) and len(h1_candles) >= 3:
        try:
            # Get last 5 H1 candles
            recent = h1_candles[-5:] if len(h1_candles) >= 5 else h1_candles

            # Criterion 1: 3+ consecutive directional H1 closes
            consec_bull = 0
            consec_bear = 0
            for c in recent:
                o = float(c.get("open", c[1]) if isinstance(c, dict) else c[1])
                cl = float(c.get("close", c[4]) if isinstance(c, dict) else c[4])
                if cl > o:
                    consec_bull += 1
                    consec_bear = 0
                elif cl < o:
                    consec_bear += 1
                    consec_bull = 0
                else:
                    consec_bull = 0
                    consec_bear = 0

            if consec_bull >= 3:
                h1_trending_signals += 1
                h1_trending_evidence.append(f"{consec_bull} consecutive bullish H1 closes")
                h1_direction = "bullish"
            elif consec_bear >= 3:
                h1_trending_signals += 1
                h1_trending_evidence.append(f"{consec_bear} consecutive bearish H1 closes")
                h1_direction = "bearish"

            # Criterion 2: Price > 2x ATR above/below EMA50
            if ema50 and current_price and atr_current and atr_current > 0:
                dist = current_price - ema50
                if dist > atr_current * 2:
                    h1_trending_signals += 1
                    h1_trending_evidence.append(f"Price {dist:.1f}pts above EMA50 ({dist/atr_current:.1f}x ATR breakaway)")
                    h1_direction = h1_direction or "bullish"
                elif dist < -atr_current * 2:
                    h1_trending_signals += 1
                    h1_trending_evidence.append(f"Price {abs(dist):.1f}pts below EMA50 ({abs(dist)/atr_current:.1f}x ATR breakdown)")
                    h1_direction = h1_direction or "bearish"

            # Criterion 3: Price moved > 3x ATR in last 4 hours one direction
            if len(h1_candles) >= 4 and atr_current and atr_current > 0:
                last4 = h1_candles[-4:]
                first_o = float(last4[0].get("open", last4[0][1]) if isinstance(last4[0], dict) else last4[0][1])
                last_c = float(last4[-1].get("close", last4[-1][4]) if isinstance(last4[-1], dict) else last4[-1][4])
                h1_4h_move = abs(last_c - first_o)
                if h1_4h_move > atr_current * 3:
                    h1_trending_signals += 1
                    h1_trending_evidence.append(f"4h move {h1_4h_move:.1f}pts ({h1_4h_move/atr_current:.1f}x ATR)")
                    if not h1_direction:
                        h1_direction = "bullish" if last_c > first_o else "bearish"

            # Criterion 4: 4 of last 5 H1 closed in upper/lower 30% of range
            upper_closes = 0
            lower_closes = 0
            for c in recent:
                h = float(c.get("high", c[2]) if isinstance(c, dict) else c[2])
                l = float(c.get("low", c[3]) if isinstance(c, dict) else c[3])
                cl = float(c.get("close", c[4]) if isinstance(c, dict) else c[4])
                rng = h - l
                if rng > 0:
                    pos = (cl - l) / rng
                    if pos >= 0.7:
                        upper_closes += 1
                    elif pos <= 0.3:
                        lower_closes += 1
            if upper_closes >= 4:
                h1_trending_signals += 1
                h1_trending_evidence.append(f"{upper_closes}/5 H1 closed in upper 30%")
                h1_direction = h1_direction or "bullish"
            elif lower_closes >= 4:
                h1_trending_signals += 1
                h1_trending_evidence.append(f"{lower_closes}/5 H1 closed in lower 30%")
                h1_direction = h1_direction or "bearish"
        except Exception as e:
            log.debug(f"REGIME | H1 candle analysis error: {e}")

    # -----------------------------------------------------------------------
    # Fast-path: M5 + H1 signals can override ADX-only trending detection
    # -----------------------------------------------------------------------
    fast_trend_score = m5_trending_signals + h1_trending_signals
    fast_direction = m5_direction or h1_direction

    if fast_trend_score >= 2 and fast_direction:
        all_evidence = m5_trending_evidence + h1_trending_evidence
        if fast_direction == "bullish":
            regime = "TRENDING_BULLISH"
        else:
            regime = "TRENDING_BEARISH"
        confidence = "high" if fast_trend_score >= 3 else "moderate"
        all_evidence.insert(0, f"Fast detection: {fast_trend_score} signals (M5={m5_trending_signals} H1={h1_trending_signals})")
        result = _build_result(regime, confidence, all_evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # If M5 alone has strong signal, flag BREAKOUT_IMMINENT instead
    if m5_trending_signals >= 2 and fast_direction:
        regime = "BREAKOUT_IMMINENT"
        m5_trending_evidence.insert(0, f"M5 fast detection: {m5_trending_signals} signals ({fast_direction})")
        result = _build_result(regime, "moderate", m5_trending_evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # -----------------------------------------------------------------------
    # REGIME 3: BREAKOUT_IMMINENT (original ADX-based)
    # -----------------------------------------------------------------------
    breakout_signals = 0
    breakout_evidence = []
    if bb_width > 0 and atr_avg > 0 and bb_width < atr_avg * 1.5:
        breakout_signals += 1
        breakout_evidence.append("Bollinger at minimum width (squeeze)")
    if volume_ratio > 0.8 and _prev_adx is not None and _prev_adx < 20:
        breakout_signals += 1
        breakout_evidence.append(f"Volume picking up ({volume_ratio:.2f}x)")
    if adx and _prev_adx is not None and adx > _prev_adx and adx > 18:
        breakout_signals += 1
        breakout_evidence.append(f"ADX rising {_prev_adx:.1f} → {adx:.1f}")

    if breakout_signals >= 2:
        regime = "BREAKOUT_IMMINENT"
        confidence = "high" if breakout_signals >= 3 else "moderate"
        result = _build_result(regime, confidence, breakout_evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # -----------------------------------------------------------------------
    # REGIME 4+5: TRENDING (BULLISH / BEARISH)
    # -----------------------------------------------------------------------
    if adx and adx >= 25 and ema9 and ema21 and ema50 and current_price:
        bullish_aligned = ema9 > ema21 > ema50
        bearish_aligned = ema9 < ema21 < ema50
        above_ema50 = current_price > ema50
        below_ema50 = current_price < ema50

        if bullish_aligned and above_ema50:
            evidence = [f"ADX {adx:.1f} (strong trend)", "EMAs bullish aligned (9>21>50)", "Price above EMA50"]
            supporting = 0
            if mtf_d1 == "UP" and mtf_h4 == "UP":
                supporting += 1
                evidence.append("D1+H4 both bullish")
            if volume_ratio > 1.0:
                supporting += 1
                evidence.append(f"Volume {volume_ratio:.2f}x avg (conviction)")
            if macd_hist and macd_hist > 0:
                supporting += 1
                evidence.append(f"MACD histogram +{macd_hist:.2f}")
            if plus_di and minus_di and plus_di > minus_di:
                supporting += 1
                evidence.append(f"+DI {plus_di:.1f} > -DI {minus_di:.1f}")
            confidence = "high" if supporting >= 3 else ("moderate" if supporting >= 1 else "low")
            result = _build_result("TRENDING_BULLISH", confidence, evidence, adx, atr_current, atr_ratio, bb_width)
            _update_temporal("TRENDING_BULLISH", now)
            _prev_adx = adx
            _prev_bollinger_width = bb_width
            return result

        if bearish_aligned and below_ema50:
            evidence = [f"ADX {adx:.1f} (strong trend)", "EMAs bearish aligned (9<21<50)", "Price below EMA50"]
            supporting = 0
            if mtf_d1 == "DOWN" and mtf_h4 == "DOWN":
                supporting += 1
                evidence.append("D1+H4 both bearish")
            if macd_hist and macd_hist < 0:
                supporting += 1
                evidence.append(f"MACD histogram {macd_hist:.2f}")
            if plus_di and minus_di and minus_di > plus_di:
                supporting += 1
                evidence.append(f"-DI {minus_di:.1f} > +DI {plus_di:.1f}")
            confidence = "high" if supporting >= 3 else ("moderate" if supporting >= 1 else "low")
            result = _build_result("TRENDING_BEARISH", confidence, evidence, adx, atr_current, atr_ratio, bb_width)
            _update_temporal("TRENDING_BEARISH", now)
            _prev_adx = adx
            _prev_bollinger_width = bb_width
            return result

    # -----------------------------------------------------------------------
    # REGIME 6: RANGING
    # -----------------------------------------------------------------------
    ranging_evidence = []
    if adx and adx < 20:
        ranging_evidence.append(f"ADX {adx:.1f} (weak trend)")
    elif adx and adx < 25 and ema9 and ema21 and ema50:
        if not (ema9 > ema21 > ema50) and not (ema9 < ema21 < ema50):
            ranging_evidence.append(f"ADX {adx:.1f} with mixed EMA order")
    if 40 <= rsi_val <= 60:
        ranging_evidence.append(f"RSI {rsi_val:.1f} (neutral zone)")
    if volume_ratio < 1.0:
        ranging_evidence.append(f"Volume {volume_ratio:.2f}x avg (below average)")

    if len(ranging_evidence) >= 2:
        regime = "RANGING"
        confidence = "high" if len(ranging_evidence) >= 3 else "moderate"
        result = _build_result(regime, confidence, ranging_evidence, adx, atr_current, atr_ratio, bb_width)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # -----------------------------------------------------------------------
    # REGIME 7: TRANSITIONAL (fallback)
    # -----------------------------------------------------------------------
    evidence = [f"ADX {adx:.1f}" if adx else "ADX unavailable"]
    if ema9 and ema21 and ema50:
        evidence.append(f"EMAs: 9={ema9:.0f} 21={ema21:.0f} 50={ema50:.0f}")
    evidence.append("Mixed signals — no clear regime")
    result = _build_result("TRANSITIONAL", "low", evidence, adx, atr_current, atr_ratio, bb_width)
    _update_temporal("TRANSITIONAL", now)
    _prev_adx = adx
    _prev_bollinger_width = bb_width
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sf(val: Any, default: Optional[float] = None) -> Optional[float]:
    """Safe float extraction. Returns default (None) if val is missing."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _build_result(
    regime: str, confidence: str, evidence: List[str],
    adx: Optional[float], atr: Optional[float], atr_ratio: float, bb_width: float,
) -> Dict[str, Any]:
    """Build the return dict with temporal context."""
    global _last_regime, _last_regime_change_ts, _regime_history

    now = time.time()

    # Duration
    duration_minutes = 0
    duration_display = "just started"
    if _last_regime_change_ts and _last_regime == regime:
        duration_minutes = int((now - _last_regime_change_ts) / 60)
        if duration_minutes < 60:
            duration_display = f"{duration_minutes}m"
        else:
            h = duration_minutes // 60
            m = duration_minutes % 60
            duration_display = f"{h}h {m}m"

    # Previous regime (the regime BEFORE the current one)
    previous_regime = None
    for entry in reversed(_regime_history):
        if entry.get("new") == regime:
            previous_regime = entry.get("old")
            break

    # Transition narrative
    transition = _transition_text(previous_regime, regime, duration_minutes)

    # Changes in 24h
    cutoff = now - 86400
    recent_changes = [e for e in _regime_history if e.get("ts", 0) > cutoff]
    changes_24h = len(recent_changes)
    if changes_24h <= 2:
        stability = "stable"
    elif changes_24h <= 4:
        stability = "moderate"
    else:
        stability = "unstable"

    return {
        "regime": regime,
        "confidence": confidence,
        "evidence": evidence,
        "duration_minutes": duration_minutes,
        "duration_display": duration_display,
        "previous_regime": previous_regime,
        "transition": transition,
        "regime_changes_24h": changes_24h,
        "stability": stability,
        "atr_current": round(atr, 2) if atr else None,
        "atr_ratio": round(atr_ratio, 2),
        "adx": round(adx, 1) if adx else None,
        "bollinger_width_vs_avg": round(bb_width, 2) if bb_width else None,
    }


def _update_temporal(regime: str, now: float) -> None:
    """Update temporal tracking state."""
    global _last_regime, _last_regime_change_ts, _regime_history

    if _last_regime != regime:
        _regime_history.append({
            "ts": now,
            "old": _last_regime,
            "new": regime,
        })
        # Keep last 50 entries
        if len(_regime_history) > 50:
            _regime_history = _regime_history[-50:]
        _last_regime = regime
        _last_regime_change_ts = now
    elif _last_regime_change_ts is None:
        _last_regime = regime
        _last_regime_change_ts = now


_TRANSITION_TEXTS = {
    ("QUIET", "TRENDING_BULLISH"): "Breakout from quiet market — fresh bullish trend",
    ("QUIET", "TRENDING_BEARISH"): "Breakout from quiet market — fresh bearish trend",
    ("QUIET", "BREAKOUT_IMMINENT"): "Quiet market coiling — breakout building",
    ("QUIET", "VOLATILE"): "Quiet market exploded — volatility spike",
    ("RANGING", "TRENDING_BULLISH"): "Range broken to the upside",
    ("RANGING", "TRENDING_BEARISH"): "Range broken to the downside",
    ("RANGING", "VOLATILE"): "Range broken by volatility spike",
    ("TRENDING_BULLISH", "RANGING"): "Bullish trend exhaustion — momentum fading",
    ("TRENDING_BEARISH", "RANGING"): "Bearish trend exhaustion — momentum fading",
    ("TRENDING_BULLISH", "TRENDING_BEARISH"): "Trend reversal — bulls to bears",
    ("TRENDING_BEARISH", "TRENDING_BULLISH"): "Trend reversal — bears to bulls",
    ("TRENDING_BULLISH", "VOLATILE"): "Trend disrupted by volatility",
    ("TRENDING_BEARISH", "VOLATILE"): "Trend disrupted by volatility",
    ("VOLATILE", "TRENDING_BULLISH"): "Volatility resolving into bullish trend",
    ("VOLATILE", "TRENDING_BEARISH"): "Volatility resolving into bearish trend",
    ("VOLATILE", "RANGING"): "Volatility settling into range",
    ("BREAKOUT_IMMINENT", "TRENDING_BULLISH"): "Breakout confirmed — bullish",
    ("BREAKOUT_IMMINENT", "TRENDING_BEARISH"): "Breakout confirmed — bearish",
}


def _transition_text(previous: Optional[str], current: str, duration: int) -> str:
    """Generate human-readable transition narrative."""
    if duration > 1440:  # 24h+
        return "Established regime — high confidence"
    if previous is None:
        return "First detection this session"
    return _TRANSITION_TEXTS.get((previous, current), f"Transitioned from {previous}")
