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
# Temporal state (FLO-144: persisted across restarts via regime_state.json)
# ---------------------------------------------------------------------------
_regime_history: List[Dict[str, Any]] = []  # [{timestamp, old, new}]
_last_regime: Optional[str] = None
_last_regime_change_ts: Optional[float] = None
_prev_adx: Optional[float] = None
_prev_bollinger_width: Optional[float] = None
_state_loaded: bool = False
_breakout_imminent_until: Optional[float] = None  # hysteresis: persist for 5 min
_bb_squeeze_bars: int = 0  # consecutive bars in BB squeeze
_volume_history: List[float] = []  # last 5 volume_ratio values

# FLO-293 Part 3: Most recent supplementary signals (computed each cycle)
_last_h4_volume_bias: Optional[Dict[str, Any]] = None
_last_m15_explosive: Optional[Dict[str, Any]] = None

# FLO-298 fix 5: regime-price divergence — stashed per-call for _build_result to read
_last_h1_candles_for_divergence: Optional[list] = None

_STATE_PATH = None  # set lazily


def _get_state_path() -> str:
    global _STATE_PATH
    if _STATE_PATH is None:
        import os
        _STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "regime_state.json")
    return _STATE_PATH


def _load_regime_state() -> None:
    """Load persisted regime state on first call."""
    global _regime_history, _last_regime, _last_regime_change_ts, _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        import json, os
        path = _get_state_path()
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return
        _last_regime = data.get("regime")
        ts = data.get("change_ts")
        if ts is not None:
            _last_regime_change_ts = float(ts)
        hist = data.get("history")
        if isinstance(hist, list):
            _regime_history = hist[-50:]  # cap
        log.info(f"REGIME | state loaded: {_last_regime} since {_last_regime_change_ts}")
    except Exception as e:
        log.debug(f"REGIME | state load failed (ignored): {e}")


def _save_regime_state() -> None:
    """Persist regime state to disk (atomic write)."""
    try:
        import json, os
        path = _get_state_path()
        data = {
            "regime": _last_regime,
            "change_ts": _last_regime_change_ts,
            "history": _regime_history[-50:],
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        log.debug(f"REGIME | state save failed (ignored): {e}")


def _broker_offset_s() -> int:
    """Broker time offset in seconds (FLO-96). Positive = broker ahead of UTC."""
    try:
        from mt5_safe import mt5  # FLO-348
        tick = mt5.symbol_info_tick("XAUUSD")
        if tick and tick.time:
            return int(tick.time) - int(time.time())
    except Exception:
        pass
    return 0


def _compute_h4_volume_bias() -> Optional[Dict[str, Any]]:
    """FLO-293 S9: H4 volume expansion + directional close -> bias (180d conf 70)."""
    try:
        from mt5_safe import mt5  # FLO-348
        import numpy as np
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H4, 0, 25)
        if rates is None or len(rates) < 21:
            return None
        highs = np.asarray([r["high"] for r in rates], dtype=float)
        lows = np.asarray([r["low"] for r in rates], dtype=float)
        closes = np.asarray([r["close"] for r in rates], dtype=float)
        vols = np.asarray([float(r["tick_volume"]) for r in rates], dtype=float)
        vol_avg20 = vols[-21:-1].mean()
        rng = highs[-1] - lows[-1]
        if vol_avg20 <= 0 or rng <= 0:
            return None
        cir = (closes[-1] - lows[-1]) / rng
        bias = "NEUTRAL"
        if vols[-1] > 1.5 * vol_avg20:
            if cir >= 0.6:
                bias = "BULLISH"
            elif cir <= 0.4:
                bias = "BEARISH"
        offset_s = _broker_offset_s()
        bar_ts_utc = int(rates[-1]["time"]) - offset_s
        return {
            "bias": bias,
            "age_min": max(0, int((time.time() - bar_ts_utc) / 60)),
            "confidence": 70,
        }
    except Exception:
        return None


def _compute_regime_price_divergence(
    regime: str, h1_candles: Optional[list]
) -> Optional[Dict[str, Any]]:
    """FLO-298 fix 5: Detect when current H1 price action disagrees with the
    regime label. Regime classifier can trail reversals by 25-60 min; this
    gives Floki the raw fact that price and label have diverged.

    Fires when:
      - regime == "TRENDING_BEARISH" and last 3 H1 closes are strictly ascending
      - regime == "TRENDING_BULLISH" and last 3 H1 closes are strictly descending

    Other regimes (RANGING, BREAKOUT_IMMINENT, VOLATILE, QUIET, TRANSITIONAL)
    are not directional, so no divergence check applies.
    """
    try:
        if regime not in ("TRENDING_BEARISH", "TRENDING_BULLISH"):
            return None
        if not h1_candles or len(h1_candles) < 4:
            return None
        # Use last 4 closed H1 bars -> 3 transitions
        closes = [float(c.get("close", 0) or 0) for c in h1_candles[-4:]]
        if any(c <= 0 for c in closes):
            return None
        up_trans = sum(1 for i in range(1, 4) if closes[i] > closes[i - 1])
        down_trans = sum(1 for i in range(1, 4) if closes[i] < closes[i - 1])

        price_direction = None
        if up_trans == 3:
            price_direction = "bullish"
        elif down_trans == 3:
            price_direction = "bearish"
        else:
            return None

        if regime == "TRENDING_BEARISH" and price_direction == "bullish":
            return {
                "detected": True,
                "price_direction": "bullish",
                "regime_label": regime,
                "conflicting_bars": 3,
                "detail": (
                    f"3 consecutive H1 higher closes "
                    f"({closes[0]:.2f} -> {closes[-1]:.2f}) "
                    f"while regime label is TRENDING_BEARISH"
                ),
            }
        if regime == "TRENDING_BULLISH" and price_direction == "bearish":
            return {
                "detected": True,
                "price_direction": "bearish",
                "regime_label": regime,
                "conflicting_bars": 3,
                "detail": (
                    f"3 consecutive H1 lower closes "
                    f"({closes[0]:.2f} -> {closes[-1]:.2f}) "
                    f"while regime label is TRENDING_BULLISH"
                ),
            }
        return None
    except Exception:
        return None


def _compute_m15_explosive() -> Optional[Dict[str, Any]]:
    """FLO-293 S5: M15 range > 2x M15 ATR(14). Checks latest and prior bar."""
    try:
        from mt5_safe import mt5  # FLO-348
        import numpy as np
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M15, 0, 16)
        if rates is None or len(rates) < 15:
            return None
        highs = np.asarray([r["high"] for r in rates], dtype=float)
        lows = np.asarray([r["low"] for r in rates], dtype=float)
        closes = np.asarray([r["close"] for r in rates], dtype=float)
        opens = np.asarray([r["open"] for r in rates], dtype=float)
        tr = np.maximum.reduce([
            highs[1:] - lows[1:],
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ])
        if len(tr) < 14:
            return None
        atr = float(tr[0])
        for i in range(1, len(tr)):
            atr = (atr * 13 + float(tr[i])) / 14
        if atr <= 0:
            return None
        offset_s = _broker_offset_s()
        for offset in (-1, -2):
            rng = highs[offset] - lows[offset]
            if rng > 2 * atr:
                if closes[offset] > opens[offset]:
                    direction = "bull"
                elif closes[offset] < opens[offset]:
                    direction = "bear"
                else:
                    continue
                bar_ts_utc = int(rates[offset]["time"]) - offset_s
                return {
                    "direction": direction,
                    "age_min": max(0, int((time.time() - bar_ts_utc) / 60)),
                }
        return None
    except Exception:
        return None


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
    global _breakout_imminent_until, _bb_squeeze_bars, _volume_history

    _load_regime_state()  # FLO-144: restore from disk on first call
    now = time.time()

    # FLO-293 Part 3: compute supplementary signals once per cycle
    global _last_h4_volume_bias, _last_m15_explosive, _last_h1_candles_for_divergence
    _last_h4_volume_bias = _compute_h4_volume_bias()
    _last_m15_explosive = _compute_m15_explosive()
    # FLO-298 fix 5: stash h1_candles for _build_result to compute divergence
    _last_h1_candles_for_divergence = h1_candles

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

    # BB squeeze bar counter (for earlier breakout detection)
    atr_avg = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history and len(atr_history) >= 20 else 0
    if bb_width > 0 and atr_avg > 0 and bb_width < atr_avg * 2.0:
        _bb_squeeze_bars += 1
    else:
        _bb_squeeze_bars = 0

    # Volume trend tracking (last 5 readings)
    _volume_history.append(volume_ratio)
    if len(_volume_history) > 5:
        _volume_history[:] = _volume_history[-5:]

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
        result = _build_result(regime, confidence, evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
        quiet_evidence.append(f"ADX {adx:.1f}")
    # Use ATR as proxy for "normal" band width since we don't track bb_width history
    atr_avg = sum(atr_history[-20:]) / len(atr_history[-20:]) if atr_history and len(atr_history) >= 20 else 0
    # Bollinger width < 2x ATR average indicates tight bands (normal width ~3-4x ATR)
    if bb_width > 0 and atr_avg > 0 and bb_width < atr_avg * 2.0:
        quiet_signals += 1
        quiet_evidence.append("Bollinger squeeze (tight bands)")

    if quiet_signals >= 3:
        regime = "QUIET"
        confidence = "high" if quiet_signals >= 4 else "moderate"
        result = _build_result(regime, confidence, quiet_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
            # FLO-175: Helper to access candle fields from dicts or tuples/arrays.
            # Python eagerly evaluates default args, so c.get("open", c[1]) crashes
            # with KeyError when c is a dict — c[1] is evaluated before get() runs.
            def _cf(c, key, idx):
                if isinstance(c, dict):
                    return float(c[key])
                return float(c[idx])

            # Get last 5 H1 candles
            recent = h1_candles[-5:] if len(h1_candles) >= 5 else h1_candles

            # Criterion 1: 3+ consecutive directional H1 closes
            consec_bull = 0
            consec_bear = 0
            for c in recent:
                o = _cf(c, "open", 1)
                cl = _cf(c, "close", 4)
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
                first_o = _cf(last4[0], "open", 1)
                last_c = _cf(last4[-1], "close", 4)
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
                h = _cf(c, "high", 2)
                l = _cf(c, "low", 3)
                cl = _cf(c, "close", 4)
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
        # FLO-144: Range compression boost — if previous regime was RANGING/QUIET for >4h
        _compression_min = 240  # configurable threshold
        try:
            import config as _rcfg
            _compression_min = int(getattr(_rcfg, "REGIME_COMPRESSION_THRESHOLD_MINUTES", 240))
        except Exception:
            pass
        if _last_regime in ("RANGING", "QUIET") and _last_regime_change_ts:
            _prev_duration_min = (now - _last_regime_change_ts) / 60.0
            if _prev_duration_min >= _compression_min and confidence == "moderate":
                confidence = "high"
                all_evidence.append(f"Range compression: {_prev_duration_min:.0f}min of {_last_regime}")
        all_evidence.insert(0, f"Fast detection: {fast_trend_score} signals (M5={m5_trending_signals} H1={h1_trending_signals})")
        result = _build_result(regime, confidence, all_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
        _update_temporal(regime, now)
        _prev_adx = adx
        _prev_bollinger_width = bb_width
        return result

    # If M5 alone has strong signal, flag BREAKOUT_IMMINENT instead
    if m5_trending_signals >= 2 and fast_direction:
        regime = "BREAKOUT_IMMINENT"
        _breakout_imminent_until = now + 300  # 5-minute hysteresis
        m5_trending_evidence.insert(0, f"M5 fast detection: {m5_trending_signals} signals ({fast_direction})")
        result = _build_result(regime, "moderate", m5_trending_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
        breakout_evidence.append(f"ADX rising {_prev_adx:.1f} -> {adx:.1f}")
    # BB squeeze duration (multi-bar energy accumulation)
    if _bb_squeeze_bars >= 5:
        breakout_signals += 1
        breakout_evidence.append(f"BB squeeze building ({_bb_squeeze_bars} bars)")
    # Volume trend (rising over last 5 readings)
    if len(_volume_history) >= 5:
        _v_rising = sum(1 for i in range(1, len(_volume_history)) if _volume_history[i] > _volume_history[i - 1])
        if _v_rising >= 3 and volume_ratio > 0.8:
            breakout_signals += 1
            breakout_evidence.append(f"Volume rising trend ({_v_rising}/4 bars up, current {volume_ratio:.2f}x)")

    if breakout_signals >= 2:
        regime = "BREAKOUT_IMMINENT"
        _breakout_imminent_until = now + 300  # 5-minute hysteresis
        confidence = "high" if breakout_signals >= 3 else "moderate"
        result = _build_result(regime, confidence, breakout_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
            evidence = [f"ADX {adx:.1f}", "EMAs bullish aligned (9>21>50)", "Price above EMA50"]
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
            result = _build_result("TRENDING_BULLISH", confidence, evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
            _update_temporal("TRENDING_BULLISH", now)
            _prev_adx = adx
            _prev_bollinger_width = bb_width
            return result

        if bearish_aligned and below_ema50:
            evidence = [f"ADX {adx:.1f}", "EMAs bearish aligned (9<21<50)", "Price below EMA50"]
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
            result = _build_result("TRENDING_BEARISH", confidence, evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
            _update_temporal("TRENDING_BEARISH", now)
            _prev_adx = adx
            _prev_bollinger_width = bb_width
            return result

    # -----------------------------------------------------------------------
    # REGIME 6: RANGING
    # -----------------------------------------------------------------------
    ranging_evidence = []
    if adx and adx < 20:
        ranging_evidence.append(f"ADX {adx:.1f}")
    elif adx and adx < 25 and ema9 and ema21 and ema50:
        if not (ema9 > ema21 > ema50) and not (ema9 < ema21 < ema50):
            ranging_evidence.append(f"ADX {adx:.1f} with mixed EMA order")
    if 40 <= rsi_val <= 60:
        ranging_evidence.append(f"RSI {rsi_val:.1f} (neutral zone)")
    if volume_ratio < 1.0:
        ranging_evidence.append(f"Volume {volume_ratio:.2f}x avg (below average)")

    if len(ranging_evidence) >= 2:
        # Hysteresis: if BREAKOUT_IMMINENT was recently triggered, persist it
        if _breakout_imminent_until and now < _breakout_imminent_until:
            _remaining_m = int((_breakout_imminent_until - now) / 60)
            _hyst_evidence = [f"BREAKOUT_IMMINENT persisting ({_remaining_m}m remaining)"] + ranging_evidence
            result = _build_result("BREAKOUT_IMMINENT", "moderate", _hyst_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
            _update_temporal("BREAKOUT_IMMINENT", now)
            _prev_adx = adx
            _prev_bollinger_width = bb_width
            return result
        regime = "RANGING"
        confidence = "high" if len(ranging_evidence) >= 3 else "moderate"
        result = _build_result(regime, confidence, ranging_evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
    result = _build_result("TRANSITIONAL", "low", evidence, adx, atr_current, atr_ratio, bb_width, mtf_d1=mtf_d1, mtf_h4=mtf_h4)
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
    mtf_d1: Optional[str] = None, mtf_h4: Optional[str] = None,
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

    # Changes in 24h with decay weighting (recent changes count more)
    import math
    cutoff = now - 86400
    recent_changes = [e for e in _regime_history if e.get("ts", 0) > cutoff]
    changes_24h = len(recent_changes)
    weighted_score = 0.0
    for e in recent_changes:
        age_hours = (now - e.get("ts", now)) / 3600.0
        weighted_score += math.exp(-age_hours / 4.0)
    if weighted_score <= 1.0:
        stability = "stable"
    elif weighted_score <= 2.5:
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
        # FLO-430 — expose D1/H4 EMA50 alignment for the ADX override
        # in snow.validator._check_regime_counter_trend_gate. Values are
        # "bullish" / "bearish" / None per _get_mtf_trend_direction.
        "d1_direction": mtf_d1,
        "h4_direction": mtf_h4,
        # FLO-452 — 8-factor D1 trend score for the counter-trend gate + Floki
        # STEP-0 check. Fail-soft None on MT5 hiccup (gate fails open).
        "d1_trend_score": build_d1_trend_score(),
        "bollinger_width_vs_avg": round(bb_width, 2) if bb_width else None,
        "h4_volume_bias": _last_h4_volume_bias,
        "m15_explosive": _last_m15_explosive,
        "regime_price_divergence": _compute_regime_price_divergence(
            regime, _last_h1_candles_for_divergence
        ),
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

    _save_regime_state()  # FLO-144: persist to disk


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


# =============================================================================
# FLO-452 — D1 Bearish/Bullish Trend Score (8 weighted factors)
# =============================================================================
# Multi-factor structural-trend score on the daily timeframe, to catch the
# documented bias where Floki overrides HTF structure (price far below D1 EMA50)
# with narrative-rich M15 reversals. 3 counter-HTF BUYs lost a net -$39 in a
# market 4.5% below the D1 EMA50. compute_d1_trend_score is PURE (testable);
# build_d1_trend_score assembles the inputs from live D1 candles.

_D1_FACTOR_WEIGHTS = [
    ("close_below_ema50", 0.10),
    ("below_ema50_3bars", 0.10),
    ("ema50_slope_down", 0.15),
    ("close_below_ema200", 0.15),
    ("death_cross", 0.10),
    ("distance_gt_half_atr", 0.10),
    ("adx_bear", 0.15),
    ("structure_lh_ll", 0.15),
]
_D1_BULL_WEIGHTS = [
    ("close_above_ema50", 0.10),
    ("above_ema50_3bars", 0.10),
    ("ema50_slope_up", 0.15),
    ("close_above_ema200", 0.15),
    ("golden_cross", 0.10),
    ("distance_gt_half_atr_up", 0.10),
    ("adx_bull", 0.15),
    ("structure_hh_hl", 0.15),
]


def compute_d1_trend_score(d1):
    """FLO-452 - pure 8-factor D1 trend score. Returns dict with direction,
    score, bearish_score, bullish_score, factors, bullish_factors. Missing
    fields make their factor False (fail-soft). All 8 bearish true -> 100."""
    def _f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    close = _f(d1.get("close")); ema50 = _f(d1.get("ema50")); ema200 = _f(d1.get("ema200"))
    atr = _f(d1.get("atr")); adx = _f(d1.get("adx"))
    pdi = _f(d1.get("plus_di")); mdi = _f(d1.get("minus_di"))
    bars_below = d1.get("bars_below_ema50") or 0
    bars_above = d1.get("bars_above_ema50") or 0
    slope = _f(d1.get("ema50_slope")); swing = d1.get("swing")

    bear = {
        "close_below_ema50": close is not None and ema50 is not None and close < ema50,
        "below_ema50_3bars": bars_below >= 3,
        "ema50_slope_down": slope is not None and slope < 0,
        "close_below_ema200": close is not None and ema200 is not None and close < ema200,
        "death_cross": ema50 is not None and ema200 is not None and ema50 < ema200,
        "distance_gt_half_atr": (close is not None and ema50 is not None and atr is not None
                                 and (ema50 - close) > 0.5 * atr),
        "adx_bear": (adx is not None and adx > 25 and pdi is not None and mdi is not None and mdi > pdi),
        "structure_lh_ll": swing == "LH_LL",
    }
    bull = {
        "close_above_ema50": close is not None and ema50 is not None and close > ema50,
        "above_ema50_3bars": bars_above >= 3,
        "ema50_slope_up": slope is not None and slope > 0,
        "close_above_ema200": close is not None and ema200 is not None and close > ema200,
        "golden_cross": ema50 is not None and ema200 is not None and ema50 > ema200,
        "distance_gt_half_atr_up": (close is not None and ema50 is not None and atr is not None
                                    and (close - ema50) > 0.5 * atr),
        "adx_bull": (adx is not None and adx > 25 and pdi is not None and mdi is not None and pdi > mdi),
        "structure_hh_hl": swing == "HH_HL",
    }
    bear_score = int(round(sum(w for k, w in _D1_FACTOR_WEIGHTS if bear[k]) * 100))
    bull_score = int(round(sum(w for k, w in _D1_BULL_WEIGHTS if bull[k]) * 100))
    if bear_score > bull_score:
        direction = "BEARISH"
    elif bull_score > bear_score:
        direction = "BULLISH"
    else:
        direction = "NEUTRAL"
    return {
        "direction": direction,
        "score": max(bear_score, bull_score),
        "bearish_score": bear_score,
        "bullish_score": bull_score,
        "factors": [k for k, w in _D1_FACTOR_WEIGHTS if bear[k]],
        "bullish_factors": [k for k, w in _D1_BULL_WEIGHTS if bull[k]],
    }


def _d1_swing_structure(df):
    """Last-two-swing structure from 3-bar fractals: LH_LL (bearish) / HH_HL
    (bullish) / None."""
    try:
        h = df["high"].values; l = df["low"].values; n = len(df)
        highs = [h[i] for i in range(2, n - 2)
                 if h[i] > h[i - 1] and h[i] > h[i - 2] and h[i] > h[i + 1] and h[i] > h[i + 2]]
        lows = [l[i] for i in range(2, n - 2)
                if l[i] < l[i - 1] and l[i] < l[i - 2] and l[i] < l[i + 1] and l[i] < l[i + 2]]
        if len(highs) >= 2 and len(lows) >= 2:
            if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
                return "LH_LL"
            if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
                return "HH_HL"
        return None
    except Exception:
        return None


def _d1_adx(df, period=14):
    """Wilder ADX(14) + DI from a candle DataFrame. (adx, +DI, -DI) or Nones."""
    try:
        import pandas as pd
        h, l, c = df["high"], df["low"], df["close"]
        up = h.diff(); dn = -l.diff()
        plus_dm = ((up > dn) & (up > 0)) * up
        minus_dm = ((dn > up) & (dn > 0)) * dn
        prev_c = c.shift(1)
        tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / period, adjust=False).mean()
        pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr
        dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
        adx = dx.ewm(alpha=1 / period, adjust=False).mean()
        return float(adx.iloc[-1]), float(pdi.iloc[-1]), float(mdi.iloc[-1])
    except Exception:
        return None, None, None


def build_d1_trend_score():
    """FLO-452 - assemble factor inputs from live D1 candles and return the
    score dict. Fail-soft -> None on MT5/compute error (gate fails open)."""
    try:
        import pandas as pd
        from mt5_safe import mt5, mt5_lock
        with mt5_lock:
            rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_D1, 0, 250)
        if rates is None or len(rates) < 60:
            return None
        df = pd.DataFrame(rates)
        ema50_s = df["close"].ewm(span=50, adjust=False).mean()
        ema200_s = df["close"].ewm(span=200, adjust=False).mean()
        close = float(df["close"].iloc[-1])
        ema50 = float(ema50_s.iloc[-1])
        ema200 = float(ema200_s.iloc[-1]) if len(df) >= 200 else None
        h, l, c = df["high"], df["low"], df["close"]
        prev_c = c.shift(1)
        tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])
        below = (df["close"] < ema50_s).tolist()
        bars_below = 0
        for v in reversed(below):
            if v:
                bars_below += 1
            else:
                break
        bars_above = 0
        for v in reversed(below):
            if not v:
                bars_above += 1
            else:
                break
        slope = float(ema50_s.diff().iloc[-5:].mean())
        swing = _d1_swing_structure(df)
        adx, pdi, mdi = _d1_adx(df)
        d1 = {
            "close": close, "ema50": ema50, "ema200": ema200, "atr": atr,
            "adx": adx, "plus_di": pdi, "minus_di": mdi,
            "bars_below_ema50": bars_below, "bars_above_ema50": bars_above,
            "ema50_slope": slope, "swing": swing,
        }
        return compute_d1_trend_score(d1)
    except Exception as e:
        log.warning(f"D1_TREND_SCORE build failed: {type(e).__name__}: {e}")
        return None
