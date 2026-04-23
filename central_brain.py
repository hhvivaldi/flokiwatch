"""
CENTRAL BRAIN - Contextual Decision System
Intelligent coordinator that analyzes raw data from ALL analyzers,
identifies market scenarios, dynamically adjusts weights and makes contextual decisions.

12 Steps:
1. Receive data
2. Analyze technical context
3. Validate ML prediction
4. Evaluate momentum strength
5. Analyze fundamental context
5.5. Analyze economic calendar (5th pillar)
6. Identify market scenario
7. Dynamically adjust weights
8. Calculate final score
9. Make decision
10. Calculate confidence
11. Build explanation
"""

from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional
from datetime import datetime
from tz_utils import utc_now
import config
from logger import log


# ============================================================================
# BRAIN RESULT
# ============================================================================

@dataclass
class BrainResult:
    """Complete analysis result from the Central Brain"""
    decision: str                    # STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL
    final_score: float               # 0-100
    confidence: float                # 0-100
    confidence_level: str            # VERY_HIGH, HIGH, MEDIUM, LOW, VERY_LOW
    scenario: str                    # Identified scenario name
    scenario_description: str        # Scenario description
    explanation: str                 # Full formatted explanation
    adjusted_weights: Dict[str, float]   # Dynamic weights used
    adjusted_scores: Dict[str, float]    # Adjusted scores
    original_scores: Dict[str, float] = field(default_factory=dict)  # FLO-154: kept for compat, unused
    confirmations: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    raw_data: Dict = field(default_factory=dict)  # FLO-154: kept for compat, unused
    gpt_validation: Optional[Dict] = None  # FLO-154: kept for compat, always None now
    mtf_trend: Optional[Dict] = None      # Multi-TF trend data for dashboard
    volume_gate: Optional[Dict] = None    # Volume gate data for dashboard
    timestamp: datetime = field(default_factory=utc_now)  # FLO-286: UTC, not local


# ============================================================================
# BASE WEIGHTS
# ============================================================================

BASE_WEIGHTS = {
    "technical": 0.30,
    "ml": 0.25,
    "momentum": 0.15,
    "news": 0.20,
    "calendar": 0.10,
}


def _effective_weights(weights: dict) -> dict:
    """FLO-187: When ML_ENABLED=False, zero out ML weight and redistribute
    proportionally across the remaining 4 pillars."""
    if config.ML_ENABLED:
        return weights
    ml_w = weights.get("ml", 0)
    if ml_w <= 0:
        return weights
    remaining = {k: v for k, v in weights.items() if k != "ml"}
    total = sum(remaining.values())
    if total <= 0:
        return weights
    scale = 1.0 / total
    result = {k: round(v * scale, 4) for k, v in remaining.items()}
    result["ml"] = 0.0
    return result

_DEFAULT_BASE_WEIGHTS = BASE_WEIGHTS.copy()


def set_base_weights(weights: Dict[str, float]) -> None:
    """Override BASE_WEIGHTS for optimization/backtest. Updates padrao scenario too."""
    global BASE_WEIGHTS
    BASE_WEIGHTS = weights.copy()
    SCENARIO_WEIGHTS["padrao"] = BASE_WEIGHTS.copy()


def reset_base_weights() -> None:
    """Restore original BASE_WEIGHTS."""
    set_base_weights(_DEFAULT_BASE_WEIGHTS)

# Display names for internal identifiers (user-visible output must be English)
DISPLAY_NAMES = {
    # ML patterns
    "continuacao": "continuation",
    "reversao": "reversal",
    "indefinido": "undefined",
    "breakout": "breakout",
    # Scenario keys (fallback if description is missing)
    "momentum_forte_confirmado": "strong confirmed momentum",
    "rsi_extremo_com_momentum": "extreme RSI with momentum",
    "divergencia_tecnica": "technical divergence",
    "breakout_confirmado": "confirmed breakout",
    "lateralizacao": "sideways / ranging",
    "sinais_conflitantes": "conflicting signals",
    "alinhamento_perfeito": "perfect alignment",
    "padrao": "default",
    "post_event_momentum": "post-event momentum",
    "zona_sr_forte": "near strong S/R zone",
}


def display_name(key: str) -> str:
    """Return English display name for an internal identifier."""
    return DISPLAY_NAMES.get(key, key)


# Weights per scenario
SCENARIO_WEIGHTS = {
    "momentum_forte_confirmado": {
        "technical": 0.20,
        "ml": 0.30,
        "momentum": 0.25,
        "news": 0.10,
        "calendar": 0.15,
    },
    "rsi_extremo_com_momentum": {
        "technical": 0.15,
        "ml": 0.35,
        "momentum": 0.30,
        "news": 0.10,
        "calendar": 0.10,
    },
    "divergencia_tecnica": {
        "technical": 0.45,
        "ml": 0.20,
        "momentum": 0.10,
        "news": 0.15,
        "calendar": 0.10,
    },
    "breakout_confirmado": {
        "technical": 0.25,
        "ml": 0.20,
        "momentum": 0.30,
        "news": 0.10,
        "calendar": 0.15,
    },
    "lateralizacao": {
        "technical": 0.25,
        "ml": 0.20,
        "momentum": 0.15,
        "news": 0.25,
        "calendar": 0.15,
    },
    "sinais_conflitantes": {
        "technical": 0.20,
        "ml": 0.20,
        "momentum": 0.20,
        "news": 0.20,
        "calendar": 0.20,
    },
    "alinhamento_perfeito": {
        "technical": 0.22,
        "ml": 0.22,
        "momentum": 0.22,
        "news": 0.22,
        "calendar": 0.12,
    },
    "janela_pos_evento": {
        "technical": 0.25,
        "ml": 0.20,
        "momentum": 0.20,
        "news": 0.10,
        "calendar": 0.25,
    },
    "volatilidade_extrema": {
        "technical": 0.20,
        "ml": 0.20,
        "momentum": 0.20,
        "news": 0.20,
        "calendar": 0.20,
    },
    "zona_sr_forte": {
        "technical": 0.30,
        "ml": 0.25,
        "momentum": 0.20,
        "news": 0.15,
        "calendar": 0.10,
    },
    "ml_vs_tech_conflito": {
        "technical": 0.45,
        "ml": 0.10,
        "momentum": 0.22,
        "news": 0.15,
        "calendar": 0.08,
    },
    "padrao": BASE_WEIGHTS.copy(),
}

# Decision thresholds
THRESHOLDS_NORMAL = {
    "strong_buy": 75,
    "buy": 65,
    "hold_upper": 65,
    "hold_lower": 35,
    "sell": 35,
    "strong_sell": 25,
}

THRESHOLDS_LATERAL = {
    "strong_buy": 80,
    "buy": 70,
    "hold_upper": 70,
    "hold_lower": 30,
    "sell": 30,
    "strong_sell": 20,
}

ML_CONFLICT_KEY = "ml_vs_tech_conflito"
CONFLICT_TECH_MIN = 65.0
CONFLICT_ML_MAX = 40.0
CONFLICT_BUY_THRESHOLD = 58.0
ML_CONFLICT_MULT = 0.95


# ============================================================================
# STEP 2: ANALYZE TECHNICAL CONTEXT
# ============================================================================

def _analyze_technical_context(tech_data: Dict, ml_data: Dict, momentum_data: Dict) -> Tuple[float, List[str], List[str]]:
    """
    Analyze technical context and adjust score.
    
    Returns:
        Tuple: (score_adjustment, confirmations, alerts)
    """
    adjustment = 0.0
    confirmations = []
    alerts = []
    
    rsi = tech_data.get("rsi", {})
    macd = tech_data.get("macd", {})
    ema = tech_data.get("ema", {})
    bollinger = tech_data.get("bollinger", {})
    stochastic = tech_data.get("stochastic", {})
    
    rsi_value = rsi.get("value", 50)
    rsi_level = rsi.get("level", "neutro")
    adx_value = momentum_data.get("adx", {}).get("adx_value", 0)
    ml_prediction = ml_data.get("prediction", "neutral")
    ml_confidence = ml_data.get("max_confidence", 0.5)
    
    # --- RSI overbought BUT strong momentum = impulse, not top ---
    if rsi_level == "overbought" and adx_value >= 25 and ml_prediction == "bullish" and ml_confidence > 0.55:
        adjustment += 30
        confirmations.append(f"RSI overbought ({rsi_value:.0f}) BUT strong momentum (ADX {adx_value:.0f}) - continuation impulse")
    elif rsi_level == "oversold" and adx_value >= 25 and ml_prediction == "bearish" and ml_confidence > 0.55:
        adjustment -= 30
        confirmations.append(f"RSI oversold ({rsi_value:.0f}) BUT strong bearish momentum (ADX {adx_value:.0f}) - drop impulse")
    
    # --- MACD Divergence ---
    divergence = macd.get("divergence", {})
    if divergence.get("detected"):
        div_type = divergence.get("type")
        bars_since = divergence.get("bars_since", 0)
        # Only consider fresh divergence (recent peak/valley, <= 8 bars ago)
        if bars_since <= 8:
            import config as _cfg
            macd_adj = getattr(_cfg, 'MACD_DIVERGENCE_ADJUSTMENT', 15)
            if div_type == "bearish":
                adjustment -= macd_adj
                alerts.append(f"MACD bearish divergence detected ({bars_since} bars ago) - possible downward reversal")
            elif div_type == "bullish":
                adjustment += macd_adj
                alerts.append(f"MACD bullish divergence detected ({bars_since} bars ago) - possible upward reversal")
    
    # --- Price above ALL EMAs + Strong momentum ---
    if ema.get("above_ema20") and ema.get("above_ema50") and ema.get("above_ema200") and adx_value >= 25:
        adjustment += 15
        confirmations.append("Price above all EMAs with strong trend")
    elif not ema.get("above_ema20") and not ema.get("above_ema50") and not ema.get("above_ema200") and adx_value >= 25:
        adjustment -= 15
        confirmations.append("Price below all EMAs with strong bearish trend")
    
    # --- Bollinger Squeeze + Breakout ---
    if bollinger.get("squeeze") and momentum_data.get("breakout", {}).get("breakout_detected"):
        adjustment += 20
        confirmations.append("Bollinger squeeze followed by breakout")
    
    # --- Stochastic agrees with RSI? ---
    stoch_level = stochastic.get("level", "neutro")
    if rsi_level == stoch_level and rsi_level != "neutro":
        confirmations.append(f"RSI and Stochastic agree: both {rsi_level}")
    elif rsi_level != "neutro" and stoch_level != "neutro" and rsi_level != stoch_level:
        adjustment -= 5
        alerts.append(f"RSI ({rsi_level}) and Stochastic ({stoch_level}) diverge")
    
    return adjustment, confirmations, alerts


# ============================================================================
# STEP 3: VALIDATE ML PREDICTION
# ============================================================================

def _validate_ml(ml_data: Dict, momentum_data: Dict, tech_data: Dict) -> Tuple[float, List[str], List[str]]:
    """
    Validate ML reliability in the current context.
    
    Returns:
        Tuple: (ml_weight_adjustment, confirmations, alerts)
    """
    confirmations = []
    alerts = []
    weight_adj = 0.0
    
    confidence = ml_data.get("max_confidence", 0.5)
    prediction = ml_data.get("prediction", "neutral")
    pattern = ml_data.get("pattern", "indefinido")
    adx_value = momentum_data.get("adx", {}).get("adx_value", 0)
    tech_score = tech_data.get("score", 50)
    
    # Model confidence
    if confidence > 0.70:
        weight_adj += 0.10
        confirmations.append(f"ML high confidence detected ({confidence:.0%})")
    elif confidence > 0.60:
        confirmations.append(f"ML moderate confidence ({confidence:.0%})")
    elif confidence > 0.55:
        alerts.append(f"ML low confidence ({confidence:.0%})")
    else:
        weight_adj -= 0.10
        alerts.append(f"ML very weak ({confidence:.0%}) - weight reduced")
    
    # Pattern type
    if pattern in ("continuacao", "breakout"):
        confirmations.append(f"Reliable pattern: {display_name(pattern)}")
    elif pattern == "reversao":
        alerts.append(f"Reversal pattern - higher risk")
    
    # ML aligns with Momentum?
    if prediction == "bullish" and adx_value >= 25:
        confirmations.append("ML bullish confirmed by strong momentum")
    elif prediction == "bearish" and adx_value >= 25:
        confirmations.append("ML bearish confirmed by strong momentum")
    
    # ML aligns with Technical?
    tech_direction = "bullish" if tech_score > 55 else ("bearish" if tech_score < 45 else "neutral")
    if prediction == tech_direction and prediction != "neutral":
        confirmations.append("ML and Technical agree on direction")
    # Note: ML/Tech disagreement is already penalized by sinais_conflitantes scenario (0.80)
    # Do not add alert here to avoid double-counting
    
    return weight_adj, confirmations, alerts


# ============================================================================
# STEP 4: EVALUATE MOMENTUM STRENGTH
# ============================================================================

def _evaluate_momentum(momentum_data: Dict) -> Tuple[str, str, List[str], List[str]]:
    """
    Classify momentum strength and direction.
    
    Returns:
        Tuple: (strength, direction, confirmations, alerts)
    """
    confirmations = []
    alerts = []
    
    adx = momentum_data.get("adx", {})
    volume = momentum_data.get("volume", {})
    candles = momentum_data.get("candles", {})
    breakout = momentum_data.get("breakout", {})
    atr = momentum_data.get("atr", {})
    
    adx_value = adx.get("adx_value", 0)
    adx_class = adx.get("adx_classification", "very_weak")
    vol_ratio = volume.get("volume_ratio", 1.0)
    vol_class = volume.get("volume_classification", "normal")
    consec_count = candles.get("consecutive_count", 0)
    consec_dir = candles.get("consecutive_direction", "neutral")
    has_breakout = breakout.get("breakout_detected", False)
    atr_trend = atr.get("atr_trend", "stable")
    
    # Classify base strength by ADX
    if adx_value >= 40:
        strength = "very_strong"
    elif adx_value >= 30:
        strength = "strong"
    elif adx_value >= 25:
        strength = "moderate"
    elif adx_value >= 20:
        strength = "weak"
    else:
        strength = "very_weak"
    
    # Upgrade by breakout
    strength_levels = ["very_weak", "weak", "moderate", "strong", "very_strong"]
    if has_breakout and strength != "very_weak":
        idx = strength_levels.index(strength)
        if idx < len(strength_levels) - 1:
            strength = strength_levels[idx + 1]
    
    # Confirmations
    if adx_value >= 30:
        confirmations.append(f"ADX very strong: {adx_value:.1f}")
    elif adx_value >= 25:
        confirmations.append(f"ADX strong: {adx_value:.1f}")
    
    if vol_ratio >= 1.5:
        confirmations.append(f"Explosive volume: {vol_ratio:.1f}x average")
    elif vol_ratio >= 1.2:
        confirmations.append(f"High volume: {vol_ratio:.1f}x average")
    elif vol_ratio < 0.8:
        alerts.append(f"Low volume: {vol_ratio:.1f}x average")
    
    if consec_count >= 5:
        confirmations.append(f"{consec_count} consecutive candles - strong impulse")
    elif consec_count >= 3:
        confirmations.append(f"{consec_count} consecutive candles - moderate impulse")
    
    if atr_trend == "increasing":
        confirmations.append("Rising volatility")
    
    if has_breakout:
        confirmations.append(f"Breakout of {breakout.get('breakout_type', 'level')}")
    
    # Direction: multi-signal voting system
    bullish_votes = 0
    bearish_votes = 0
    
    # 1. ADX +DI vs -DI (weight depends on ADX strength)
    #    ADX >= 20 → weight 2 (real trend, DI reliable)
    #    ADX < 20  → weight 0 (no trend, DI erratic — ignore)
    plus_di = adx.get("plus_di", 0)
    minus_di = adx.get("minus_di", 0)
    di_weight = 2 if adx_value >= 20 else 0
    if plus_di > minus_di + 2:
        bullish_votes += di_weight
    elif minus_di > plus_di + 2:
        bearish_votes += di_weight
    
    # 2. Consecutive candles (weight 1)
    if consec_dir == "bullish" and consec_count >= 2:
        bullish_votes += 1
    elif consec_dir == "bearish" and consec_count >= 2:
        bearish_votes += 1
    
    # 3. Breakout type (weight 1)
    if has_breakout:
        breakout_type = breakout.get("breakout_type")
        if breakout_type == "resistance":
            bullish_votes += 1
        elif breakout_type == "support":
            bearish_votes += 1
    
    if bullish_votes > bearish_votes:
        direction = "bullish"
    elif bearish_votes > bullish_votes:
        direction = "bearish"
    else:
        direction = "neutral"
    
    # Penalize high momentum with low volume (false breakout)
    momentum_score = momentum_data.get("score", 50.0)
    if vol_ratio < 0.5 and momentum_score > 60:
        alerts.append(f"Momentum {momentum_score:.0f} with very low volume ({vol_ratio:.1f}x) - possible false breakout")
        idx = strength_levels.index(strength)
        if idx >= 2:
            strength = strength_levels[max(0, idx - 2)]
        elif idx >= 1:
            strength = strength_levels[0]
    elif vol_ratio < 0.7 and momentum_score > 70:
        alerts.append(f"Momentum {momentum_score:.0f} with low volume ({vol_ratio:.1f}x) - caution")
        idx = strength_levels.index(strength)
        if idx >= 1:
            strength = strength_levels[idx - 1]
    
    return strength, direction, confirmations, alerts


# ============================================================================
# STEP 5: ANALYZE FUNDAMENTAL CONTEXT
# ============================================================================

def _analyze_fundamentals(news_data: Dict) -> Tuple[str, List[str], List[str]]:
    """
    Analyze whether fundamentals support or contradict technical signals.
    
    Returns:
        Tuple: (alignment, confirmations, alerts)
        alignment: "positive" (favors gold), "negative" (disfavors), "neutral"
    """
    confirmations = []
    alerts = []
    positive_count = 0
    negative_count = 0
    
    dxy = news_data.get("dxy", {})
    yields_data = news_data.get("yields", {})
    vix = news_data.get("vix", {})
    sentiment = news_data.get("sentiment", {})
    high_impact = news_data.get("high_impact_news_soon", False)
    geo_risk = news_data.get("geopolitical_risk", "low")
    
    # DXY
    dxy_trend = dxy.get("trend", "stable")
    dxy_change = dxy.get("change_24h", 0)
    if dxy_trend == "falling":
        positive_count += 1
        confirmations.append(f"DXY falling {dxy_change:+.2f}% - supports gold upside")
    elif dxy_trend == "rising":
        negative_count += 1
        alerts.append(f"DXY rising {dxy_change:+.2f}% - pressures gold")
    
    # Yields
    yields_trend = yields_data.get("trend", "stable")
    if yields_trend == "falling":
        positive_count += 1
        confirmations.append("Yields falling - favors gold")
    elif yields_trend == "rising":
        negative_count += 1
        alerts.append("Yields rising - disfavors gold")
    
    # VIX
    vix_value = vix.get("value")
    vix_level = vix.get("level", "low")
    if vix_level == "high" or (vix_value and vix_value > 25):
        positive_count += 2
        confirmations.append(f"VIX high ({vix_value}) - safe haven demand favors gold")
    
    # Sentiment
    sent_norm = sentiment.get("normalized", 0)
    if sent_norm > 0.3:
        positive_count += 1
        confirmations.append(f"Positive sentiment ({sent_norm:+.2f})")
    elif sent_norm < -0.3:
        negative_count += 1
        alerts.append(f"Negative sentiment ({sent_norm:+.2f})")
    
    # High-impact news
    if high_impact:
        alerts.append("High-impact news approaching - CAUTION!")
    
    # Geopolitical risk
    if geo_risk == "high":
        positive_count += 2
        confirmations.append("High geopolitical risk - favors gold (safe haven)")
    elif geo_risk == "medium":
        positive_count += 1
    
    # Determine alignment
    if positive_count > negative_count + 1:
        alignment = "positive"
    elif negative_count > positive_count + 1:
        alignment = "negative"
    else:
        alignment = "neutral"
    
    return alignment, confirmations, alerts


# ============================================================================
# STEP 6: IDENTIFY SCENARIO
# ============================================================================

def _identify_scenario(tech_data: Dict, ml_data: Dict, momentum_data: Dict,
                       news_data: Dict, momentum_strength: str,
                       calendar_data: Optional[Dict] = None,
                       volatility_status: Optional[Dict] = None,
                       sr_data: Optional[Dict] = None) -> Tuple[str, str, float]:
    """
    Identify the current market scenario.
    
    Returns:
        Tuple: (scenario_key, scenario_description, confidence_multiplier)
    """
    rsi_level = tech_data.get("rsi", {}).get("level", "neutro")
    rsi_value = tech_data.get("rsi", {}).get("value", 50)
    macd_div = tech_data.get("macd", {}).get("divergence", {}).get("detected", False)
    ml_confidence = ml_data.get("max_confidence", 0.5)
    ml_score = ml_data.get("score", 50)
    ml_prediction = ml_data.get("prediction", "neutral")
    tech_score = tech_data.get("score", 50)
    momentum_score = momentum_data.get("score", 50)
    has_breakout = momentum_data.get("breakout", {}).get("breakout_detected", False)
    adx_value = momentum_data.get("adx", {}).get("adx_value", 0)
    
    # Calendar data
    cal = calendar_data or {}
    calendar_score = cal.get("score", 50.0)
    calendar_bias = cal.get("bias", "NEUTRAL")
    calendar_phase = cal.get("phase", "normal")
    
    # Directions
    tech_bullish = tech_score > 55
    tech_bearish = tech_score < 45
    ml_bullish = ml_prediction == "bullish" and ml_confidence > 0.55
    ml_bearish = ml_prediction == "bearish" and ml_confidence > 0.55
    momentum_strong = momentum_strength in ("strong", "very_strong")
    momentum_weak = momentum_strength in ("weak", "very_weak")

    is_conflict = tech_score >= CONFLICT_TECH_MIN and ml_score <= CONFLICT_ML_MAX
    
    # Calendar bias aligned with tech?
    cal_aligns_tech = (
        (calendar_bias == "BULLISH" and tech_bullish) or
        (calendar_bias == "BEARISH" and tech_bearish)
    )
    
    # PRIORITY SCENARIO: Extreme Volatility (total block)
    vol = volatility_status or {}
    vol_status = vol.get("status", "NORMAL")
    if vol_status == "EXTREME":
        pct = vol.get("extreme_percent", 0)
        return "volatilidade_extrema", f"Extreme volatility detected ({pct:.1f}% on M5 candle) - BLOCK", 0.0
    
    # SCENARIO SR: Near strong S/R zone (conservative — widens HOLD zone)
    sr = sr_data or {}
    sr_near_zone = sr.get("near_strong_zone", False)
    sr_zone_info = sr.get("near_zone_info")
    if sr_near_zone and sr_zone_info:
        z_mid = sr_zone_info.get("midpoint", 0)
        z_touches = sr_zone_info.get("touches", 0)
        z_type = sr_zone_info.get("zone_type", "?")
        if is_conflict:
            return (
                ML_CONFLICT_KEY,
                (f"{ML_CONFLICT_KEY} (Tech={tech_score:.1f} vs ML={ml_score:.1f}) | "
                 f"BUY threshold overridden: {CONFLICT_BUY_THRESHOLD:.0f} (near S/R)"),
                ML_CONFLICT_MULT,
            )
        return "zona_sr_forte", f"Near strong {z_type} zone at {z_mid:.2f} ({z_touches} touches) - informational", 1.00

    # SCENARIO G: Perfect Alignment — with RSI guard
    # Only enters if RSI is not extreme (avoids parabolic tops/bottoms)
    all_bullish = tech_bullish and ml_bullish and momentum_strong and momentum_score > 60
    all_bearish = tech_bearish and ml_bearish and momentum_strong and momentum_score > 60
    rsi_val = tech_data.get("rsi", {}).get("value", 50) if isinstance(tech_data, dict) else 50
    if all_bullish and rsi_val < 75:
        return "alinhamento_perfeito", "All indicators aligned (RSI OK)", 1.10
    if all_bearish and rsi_val > 25:
        return "alinhamento_perfeito", "All indicators aligned bearish (RSI OK)", 1.10
    # If RSI extreme: fall through to next scenario
    
    # SCENARIO H: Post-event window with momentum
    if calendar_score > 70 and momentum_score > 60 and cal_aligns_tech:
        return "janela_pos_evento", "Post-event window with directional momentum", 1.10
    
    # SCENARIO A: Strong Confirmed Momentum
    if momentum_strong and ml_confidence > 0.60 and ml_score > 60:
        return "momentum_forte_confirmado", "Strong rally/drop with multiple confirmations", 1.30
    
    # SCENARIO B: Extreme RSI with Momentum
    if rsi_level in ("overbought", "oversold") and momentum_strong and (ml_bullish or ml_bearish):
        return "rsi_extremo_com_momentum", "Extreme RSI but strong momentum - continuation impulse", 1.20
    
    # SCENARIO C: Technical Divergence (only if fresh)
    macd_div_bars = tech_data.get("macd", {}).get("divergence", {}).get("bars_since", 99)
    if macd_div and macd_div_bars <= 8:
        return "divergencia_tecnica", "Technical divergence - possible reversal", 1.00
    
    # SCENARIO D: Confirmed Breakout
    if has_breakout and not momentum_weak:
        return "breakout_confirmado", "Resistance/support breakout with momentum", 1.25
    
    # SCENARIO E: Sideways
    if momentum_weak and ml_confidence < 0.55 and 40 <= tech_score <= 60:
        return "lateralizacao", "Sideways market with no clear direction", 0.85
    
    # SCENARIO F: Conflicting Signals
    if (tech_bullish and ml_bearish) or (tech_bearish and ml_bullish):
        if is_conflict:
            return (
                ML_CONFLICT_KEY,
                (f"{ML_CONFLICT_KEY} (Tech={tech_score:.1f} vs ML={ml_score:.1f}) | "
                 f"BUY threshold overridden: {CONFLICT_BUY_THRESHOLD:.0f}"),
                ML_CONFLICT_MULT,
            )
        return "sinais_conflitantes", "Technical and ML signals in conflict", 0.80

    if is_conflict:
        return (
            ML_CONFLICT_KEY,
            (f"{ML_CONFLICT_KEY} (Tech={tech_score:.1f} vs ML={ml_score:.1f}) | "
             f"BUY threshold overridden: {CONFLICT_BUY_THRESHOLD:.0f}"),
            ML_CONFLICT_MULT,
        )
    
    # DEFAULT SCENARIO
    return "padrao", "Default scenario", 1.00


# ============================================================================
# STEP 7: DYNAMICALLY ADJUST WEIGHTS
# ============================================================================

def _adjust_weights(scenario: str, ml_confidence: float,
                    calendar_phase: str = "normal",
                    momentum_score: float = 50.0,
                    momentum_strength: str = "moderate") -> Dict[str, float]:
    """
    Adjust weights based on scenario, ML confidence, calendar phase and momentum.
    
    Returns:
        Dict with normalized weights (sum = 1.0)
    """
    # Get scenario weights
    weights = SCENARIO_WEIGHTS.get(scenario, BASE_WEIGHTS).copy()
    
    # Adjust by calendar phase (POST_EVENT → calendar rises to 20%)
    if calendar_phase == "post_event" and scenario != "janela_pos_evento":
        target_cal = 0.20
        current_cal = weights.get("calendar", 0.10)
        cal_boost = target_cal - current_cal
        if cal_boost > 0:
            weights["calendar"] = target_cal
            others = [k for k in weights if k != "calendar"]
            steal_each = cal_boost / len(others)
            for k in others:
                weights[k] -= steal_each
    
    # Adjust by ML confidence
    if ml_confidence > 0.70:
        ml_boost = 0.10
        weights["ml"] += ml_boost
        others = [k for k in weights if k != "ml"]
        steal_each = ml_boost / len(others)
        for k in others:
            weights[k] -= steal_each
    elif ml_confidence < 0.55:
        ml_penalty = 0.10
        weights["ml"] -= ml_penalty
        others = [k for k in weights if k != "ml"]
        give_each = ml_penalty / len(others)
        for k in others:
            weights[k] += give_each
    
    # Adjust for strong momentum: gradually reduce ML weight, redistribute to momentum
    if momentum_score >= 80 and momentum_strength in ("strong", "very_strong"):
        if momentum_score >= 95:
            ml_reduction = 0.15
        elif momentum_score >= 90:
            ml_reduction = 0.10
        else:  # 80-89
            ml_reduction = 0.05
        weights["ml"] -= ml_reduction
        weights["momentum"] += ml_reduction
    
    # ML-specific floor: never below 10%
    if weights["ml"] < 0.10:
        deficit = 0.10 - weights["ml"]
        weights["ml"] = 0.10
        others = sorted([(k, v) for k, v in weights.items() if k != "ml"], key=lambda x: -x[1])
        weights[others[0][0]] -= deficit
    
    # Ensure no weight is negative
    for k in weights:
        weights[k] = max(0.05, weights[k])
    
    # Normalize to sum = 1.0
    total = sum(weights.values())
    if total > 0:
        weights = {k: round(v / total, 4) for k, v in weights.items()}
    
    return weights


# ============================================================================
# STEP 8: CALCULATE FINAL SCORE
# ============================================================================

def _calculate_final_score(adjusted_scores: Dict[str, float], weights: Dict[str, float]) -> float:
    """
    Calculate final score using adjusted scores and dynamic weights.
    FLO-187: When ML disabled, weights are redistributed via _effective_weights.
    """
    w = _effective_weights(weights)
    if not config.ML_ENABLED:
        log.info("ML | DISABLED via config \u2014 weight redistributed")

    score = 0.0
    for component in ["technical", "ml", "momentum", "news", "calendar"]:
        s = adjusted_scores.get(component, 50.0)
        cw = w.get(component, 0.20)
        score += s * cw

    return round(max(0, min(100, score)), 2)


# ============================================================================
# STEP 9: MAKE DECISION
# ============================================================================

def _make_decision(final_score: float, scenario: str) -> str:
    """
    Make decision based on score and scenario.
    """
    thresholds = THRESHOLDS_LATERAL if scenario == "lateralizacao" else THRESHOLDS_NORMAL
    if scenario == ML_CONFLICT_KEY:
        thresholds = thresholds.copy()
        thresholds["buy"] = CONFLICT_BUY_THRESHOLD
        thresholds["hold_upper"] = CONFLICT_BUY_THRESHOLD
    
    if final_score >= thresholds["strong_buy"]:
        return "STRONG_BUY"
    elif final_score >= thresholds["buy"]:
        return "BUY"
    elif final_score <= thresholds["strong_sell"]:
        return "STRONG_SELL"
    elif final_score <= thresholds["sell"]:
        return "SELL"
    else:
        return "HOLD"


# ============================================================================
# STEP 10: CALCULATE CONFIDENCE
# ============================================================================

def _calculate_confidence(scenario_multiplier: float, confirmations: List[str],
                          alerts: List[str], ml_confidence: float,
                          momentum_strength: str, fundamental_alignment: str,
                          final_score: float = 50.0,
                          calendar_bias: str = "NEUTRAL",
                          decision: str = "HOLD",
                          volatility_status: Optional[Dict] = None,
                          news_score: float = 50.0,
                          **kwargs) -> Tuple[float, str]:
    """
    Calculate decision confidence level.
    
    Returns:
        Tuple: (confidence_value, confidence_level)
    """
    confidence = 50.0
    
    # Scenario multiplier
    confidence *= scenario_multiplier
    
    # Confirmations and alerts (weight 2 per item, avoids inflation by quantity)
    confidence += len(confirmations) * 2
    confidence -= len(alerts) * 2
    
    # ML (gradual scale, avoids abrupt swings)
    if ml_confidence > 0.70:
        confidence += 15
    elif ml_confidence > 0.60:
        confidence += 10
    elif ml_confidence > 0.55:
        confidence += 5
    elif ml_confidence >= 0.50:
        pass  # neutral, no bonus or penalty
    else:
        confidence -= 10
    
    # Momentum
    momentum_map = {
        "very_strong": 15,
        "strong": 10,
        "moderate": 0,
        "weak": -10,
        "very_weak": -10,
    }
    confidence += momentum_map.get(momentum_strength, 0)
    
    # Volume Gate penalty
    import config as _cfg
    if getattr(_cfg, 'VOLUME_GATE_ENABLED', True):
        volume_ratio = kwargs.get('volume_ratio', 1.0)
        severe_threshold = getattr(_cfg, 'VOLUME_GATE_SEVERE_THRESHOLD', 0.3)
        moderate_threshold = getattr(_cfg, 'VOLUME_GATE_MODERATE_THRESHOLD', 0.5)
        severe_penalty = getattr(_cfg, 'VOLUME_GATE_SEVERE_PENALTY', 25)
        moderate_penalty = getattr(_cfg, 'VOLUME_GATE_MODERATE_PENALTY', 15)
        
        if volume_ratio < severe_threshold:
            confidence -= severe_penalty
        elif volume_ratio < moderate_threshold:
            confidence -= moderate_penalty
    
    # Fundamentals
    if fundamental_alignment == "positive":
        confidence += 10
    elif fundamental_alignment == "negative":
        confidence -= 10
    
    # Calendar bias alignment
    if calendar_bias != "NEUTRAL":
        decision_bullish = decision in ("BUY", "STRONG_BUY")
        decision_bearish = decision in ("SELL", "STRONG_SELL")
        cal_bullish = calendar_bias == "BULLISH"
        cal_bearish = calendar_bias == "BEARISH"
        
        if (cal_bullish and decision_bullish) or (cal_bearish and decision_bearish):
            confidence += 10  # Bias aligned with decision
        elif (cal_bullish and decision_bearish) or (cal_bearish and decision_bullish):
            confidence -= 15  # Bias against decision
    
    # Penalize confidence only in the truly undecided zone (45-55)
    # Score 50 = totally undecided = maximum penalty (-20)
    # Score 55+ or 45- = moderate signal = no penalty
    score_distance = abs(final_score - 50)
    if score_distance < 5:  # Score between 45-55
        # The closer to 50, the higher the penalty (up to -20)
        indecision_penalty = (5 - score_distance) * 4
        confidence -= indecision_penalty
    
    # Volatility Guard override
    vol = volatility_status or {}
    vol_status = vol.get("status", "NORMAL")
    if vol_status == "EXTREME":
        confidence = 0.0
    elif vol_status == "COOLING_DOWN":
        import config as _cfg
        if confidence < _cfg.COOLING_MIN_CONFIDENCE:
            confidence = 0.0
    
    # Cap: Weak ML does not allow high confidence
    if ml_confidence < 0.55 and decision not in ("HOLD",):
        confidence = min(confidence, 65.0)
    
    # Penalize: Bearish news contradicts BUY
    if news_score < 45 and decision in ("BUY", "STRONG_BUY"):
        confidence -= 15
    
    # Penalize alinhamento_perfeito with extreme confidence
    # When EVERYTHING agrees, the market has probably already made the move
    if kwargs.get('scenario') == 'alinhamento_perfeito' and confidence > 75:
        confidence = min(confidence, 75.0)
    
    # Clamp
    confidence = max(0, min(100, confidence))
    
    # Classify
    if confidence >= 80:
        level = "VERY_HIGH"
    elif confidence >= 65:
        level = "HIGH"
    elif confidence >= 50:
        level = "MEDIUM"
    elif confidence >= 35:
        level = "LOW"
    else:
        level = "VERY_LOW"
    
    return round(confidence, 1), level


# ============================================================================
# STEP 11: BUILD EXPLANATION
# ============================================================================

def _build_explanation(decision: str, final_score: float, scenario: str,
                       scenario_desc: str, tech_data: Dict, ml_data: Dict,
                       momentum_data: Dict, news_data: Dict,
                       adjusted_scores: Dict, original_scores: Dict,
                       weights: Dict, confidence: float, confidence_level: str,
                       tech_confirmations: List[str], tech_alerts: List[str],
                       ml_confirmations: List[str], ml_alerts: List[str],
                       momentum_strength: str, momentum_direction: str,
                       momentum_confirmations: List[str], momentum_alerts: List[str],
                       fund_alignment: str, fund_confirmations: List[str],
                       fund_alerts: List[str],
                       calendar_data: Optional[Dict] = None,
                       volatility_status: Optional[Dict] = None,
                       m5_adj_description: str = "",
                       sr_description: str = "") -> str:
    """
    Build complete formatted explanation.
    """
    lines = []
    
    lines.append(f"DECISION: {decision} (Score: {final_score:.1f})")
    lines.append(f"SCENARIO: {scenario_desc}")
    lines.append("")
    
    # Technical
    lines.append("TECHNICAL ANALYSIS:")
    lines.append(f"  Adjusted score: {adjusted_scores.get('technical', 0):.1f} (original: {original_scores.get('technical', 0):.1f})")
    if tech_confirmations:
        lines.append("  Confirmations:")
        for c in tech_confirmations:
            lines.append(f"    + {c}")
    if tech_alerts:
        lines.append("  Alerts:")
        for a in tech_alerts:
            lines.append(f"    ! {a}")
    lines.append("")
    
    # ML
    lines.append("MACHINE LEARNING:")
    ml_pred = ml_data.get("prediction", "neutral")
    ml_conf = ml_data.get("max_confidence", 0.5)
    lines.append(f"  Prediction: {ml_pred} (Confidence: {ml_conf:.0%})")
    lines.append(f"  Adjusted score: {adjusted_scores.get('ml', 0):.1f}")
    if ml_confirmations:
        for c in ml_confirmations:
            lines.append(f"    + {c}")
    if ml_alerts:
        for a in ml_alerts:
            lines.append(f"    ! {a}")
    lines.append("")
    
    # Momentum
    lines.append("MOMENTUM:")
    lines.append(f"  Strength: {momentum_strength} | Direction: {momentum_direction}")
    lines.append(f"  Score: {adjusted_scores.get('momentum', 0):.1f}")
    if momentum_confirmations:
        for c in momentum_confirmations:
            lines.append(f"    + {c}")
    if momentum_alerts:
        for a in momentum_alerts:
            lines.append(f"    ! {a}")
    lines.append("")
    
    # Fundamentals
    lines.append("FUNDAMENTALS:")
    lines.append(f"  Alignment: {fund_alignment}")
    lines.append(f"  Score: {adjusted_scores.get('news', 0):.1f}")
    if fund_confirmations:
        for c in fund_confirmations:
            lines.append(f"    + {c}")
    if fund_alerts:
        for a in fund_alerts:
            lines.append(f"    ! {a}")
    lines.append("")
    
    # Economic Calendar
    cal = calendar_data or {}
    cal_score = cal.get("score", 50.0)
    cal_bias = cal.get("bias", "NEUTRAL")
    cal_phase = cal.get("phase", "normal")
    cal_desc = cal.get("phase_description", "No data")
    cal_source = cal.get("source", "unknown")
    lines.append("ECONOMIC CALENDAR:")
    lines.append(f"  Phase: {cal_phase} | Bias: {cal_bias}")
    lines.append(f"  Score: {cal_score:.1f} | Source: {cal_source}")
    lines.append(f"  {cal_desc}")
    closest = cal.get("closest_event")
    if closest:
        lines.append(f"  Event: {closest.get('name', '?')} ({closest.get('time_server', '?')})")
        actual = closest.get("actual_value")
        forecast = closest.get("forecast_value")
        if actual is not None and forecast is not None:
            lines.append(f"  Actual: {actual} vs Forecast: {forecast}")
    lines.append("")
    
    # Volatility Guard
    vol = volatility_status or {}
    vol_st = vol.get("status", "NORMAL")
    vol_desc = vol.get("description", "No data")
    lines.append("VOLATILITY GUARD:")
    lines.append(f"  Status: {vol_st}")
    lines.append(f"  {vol_desc}")
    extreme_candle = vol.get("last_extreme_candle")
    if extreme_candle:
        lines.append(f"  Extreme candle: {extreme_candle.get('direction', '?')} {extreme_candle.get('move_percent', 0):.2f}% ({extreme_candle.get('minutes_ago', '?')} min ago)")
    lines.append("")
    
    # M5 Adjustment
    if m5_adj_description:
        lines.append("M5 ADJUSTMENT:")
        lines.append(f"  {m5_adj_description}")
        lines.append("")
    
    # Support & Resistance
    if sr_description:
        lines.append("SUPPORT & RESISTANCE:")
        lines.append(f"  {sr_description}")
        lines.append("")
    
    # Weights
    lines.append("APPLIED WEIGHTS:")
    for k, v in weights.items():
        lines.append(f"  {k.capitalize()}: {v:.0%}")
    lines.append("")
    
    # Conclusion
    lines.append(f"CONFIDENCE: {confidence:.1f}/100 ({confidence_level})")
    
    return "\n".join(lines)


# ============================================================================
# STEP 8.5: M5 ADJUSTMENT ("now" awareness)
# ============================================================================

def _get_mtf_trend_direction(timeframe: str) -> Optional[str]:
    """
    Get trend direction for a specific timeframe using EMA50.
    
    Args:
        timeframe: "D1" or "H4"
    
    Returns:
        "bullish", "bearish", or None if data unavailable
    """
    from logger import log
    
    try:
        from mt5_safe import mt5  # FLO-348
        import config as _cfg
        
        tf_map = {
            "D1": mt5.TIMEFRAME_D1,
            "H4": mt5.TIMEFRAME_H4,
        }
        tf = tf_map.get(timeframe)
        if tf is None:
            log.warning(f"[MTF] Invalid timeframe: {timeframe}")
            return None
        
        ema_period = getattr(_cfg, 'MTF_EMA_PERIOD', 50)
        # Need enough bars for EMA calculation
        bars_needed = ema_period + 10
        
        rates = mt5.copy_rates_from_pos("XAUUSD", tf, 0, bars_needed)
        if rates is None:
            log.warning(f"[MTF] {timeframe} data fetch returned None - MT5 connection issue?")
            return None
        if len(rates) < ema_period:
            log.warning(f"[MTF] {timeframe} insufficient bars: got {len(rates)}, need {ema_period}")
            return None
        
        # Calculate EMA50
        import pandas as pd
        df = pd.DataFrame(rates)
        df['ema'] = df['close'].ewm(span=ema_period, adjust=False).mean()
        
        current_price = float(df['close'].iloc[-1])
        current_ema = float(df['ema'].iloc[-1])
        
        if current_price > current_ema:
            direction = "bullish"
        elif current_price < current_ema:
            direction = "bearish"
        else:
            direction = None
        
        log.debug(f"[MTF] {timeframe}: price={current_price:.2f}, EMA{ema_period}={current_ema:.2f} -> {direction}")
        return direction
            
    except Exception as e:
        log.error(f"[MTF] {timeframe} trend detection failed: {e}")
        return None


def _check_mtf_trend_alignment(decision: str, d1_trend: Optional[str] = None, 
                                h4_trend: Optional[str] = None) -> Tuple[float, List[str], List[str]]:
    """
    Check if trade direction aligns with D1 and H4 trend.
    
    If both D1 and H4 agree on direction:
    - Trade aligns: +10 confidence bonus
    - Trade conflicts: -20 confidence penalty
    
    Args:
        decision: "BUY", "SELL", "HOLD", etc.
        d1_trend: Optional pre-calculated D1 trend (for backtest)
        h4_trend: Optional pre-calculated H4 trend (for backtest)
    
    Returns:
        Tuple: (confidence_adjustment, confirmations, alerts)
    """
    import config as _cfg
    
    if not getattr(_cfg, 'MTF_TREND_ENABLED', True):
        return 0.0, [], []
    
    confirmations = []
    alerts = []
    adjustment = 0.0
    
    # Get trend directions (use provided values or fetch from MT5)
    if d1_trend is None:
        d1_trend = _get_mtf_trend_direction("D1")
    if h4_trend is None:
        h4_trend = _get_mtf_trend_direction("H4")
    
    # If we can't determine both trends, no adjustment
    if d1_trend is None or h4_trend is None:
        return 0.0, [], []
    
    # Check if D1 and H4 agree
    if d1_trend != h4_trend:
        # Mixed signals - no adjustment
        return 0.0, [], []
    
    # Both timeframes agree
    mtf_direction = d1_trend  # "bullish" or "bearish"
    
    # Determine trade direction
    trade_bullish = decision in ("BUY", "STRONG_BUY")
    trade_bearish = decision in ("SELL", "STRONG_SELL")
    
    if decision == "HOLD":
        # No adjustment for HOLD
        return 0.0, [], []
    
    align_bonus = getattr(_cfg, 'MTF_TREND_ALIGN_BONUS', 10)
    conflict_penalty = getattr(_cfg, 'MTF_TREND_CONFLICT_PENALTY', 20)
    
    if mtf_direction == "bullish":
        if trade_bullish:
            adjustment = align_bonus
            confirmations.append(f"MTF trend aligned: D1+H4 bullish, trade BUY (+{align_bonus} conf)")
        elif trade_bearish:
            adjustment = -conflict_penalty
            alerts.append(f"MTF trend CONFLICT: D1+H4 bullish but trade SELL (-{conflict_penalty} conf)")
    elif mtf_direction == "bearish":
        if trade_bearish:
            adjustment = align_bonus
            confirmations.append(f"MTF trend aligned: D1+H4 bearish, trade SELL (+{align_bonus} conf)")
        elif trade_bullish:
            adjustment = -conflict_penalty
            alerts.append(f"MTF trend CONFLICT: D1+H4 bearish but trade BUY (-{conflict_penalty} conf)")
    
    return adjustment, confirmations, alerts


def _apply_m5_adjustment(final_score: float, m5_data: Dict) -> tuple:
    """
    Apply adjustment to final score based on recent M5 state.
    
    If score implies SELL (< 45) but M5 shows rise → pull score up (more neutral).
    If score implies BUY (> 55) but M5 shows drop → pull score down.
    If M5 confirms direction → small bonus (+2).
    
    Args:
        final_score: Brain final score (0-100)
        m5_data: Dict from get_m5_status() with move_pct, green_count, red_count
    
    Returns:
        Tuple: (adjusted_score, adjustment, description)
    """
    import config as _cfg
    
    move_pct = m5_data.get("move_pct", 0.0)
    abs_move = abs(move_pct)
    threshold = getattr(_cfg, 'M5_SCORE_ADJUST_THRESHOLD', 0.15)
    max_adj = getattr(_cfg, 'M5_SCORE_ADJUST_MAX', 7)
    full_move = getattr(_cfg, 'M5_SCORE_ADJUST_FULL_MOVE', 0.40)
    confirm_bonus = getattr(_cfg, 'M5_SCORE_CONFIRM_BONUS', 2)
    
    # Determine implied direction from score
    if final_score < 45:
        implied_direction = "SELL"
    elif final_score > 55:
        implied_direction = "BUY"
    else:
        # Score in HOLD zone (45-55) → no M5 adjustment
        return final_score, 0.0, ""
    
    # Check if M5 contradicts or confirms
    m5_bullish = move_pct > 0
    m5_bearish = move_pct < 0
    
    adjustment = 0.0
    description = ""
    
    if implied_direction == "SELL" and m5_bullish and abs_move >= threshold:
        # M5 against SELL: price rising → pull score up (more neutral)
        # Linear scale: threshold → +3, full_move → +max_adj
        frac = min(1.0, (abs_move - threshold) / max(full_move - threshold, 0.01))
        adjustment = 3.0 + frac * (max_adj - 3.0)
        adjustment = round(min(adjustment, max_adj), 1)
        description = f"score {final_score:.1f} → {final_score + adjustment:.1f} (M5 against SELL: +{move_pct:.2f}%)"
        
    elif implied_direction == "BUY" and m5_bearish and abs_move >= threshold:
        # M5 against BUY: price falling → pull score down
        frac = min(1.0, (abs_move - threshold) / max(full_move - threshold, 0.01))
        adjustment = -(3.0 + frac * (max_adj - 3.0))
        adjustment = round(max(adjustment, -max_adj), 1)
        description = f"score {final_score:.1f} → {final_score + adjustment:.1f} (M5 against BUY: {move_pct:.2f}%)"
        
    elif implied_direction == "SELL" and m5_bearish and abs_move >= threshold:
        # M5 confirms SELL: price falling → small bonus
        adjustment = -confirm_bonus
        description = f"score {final_score:.1f} → {final_score + adjustment:.1f} (M5 confirms SELL: {move_pct:.2f}%)"
        
    elif implied_direction == "BUY" and m5_bullish and abs_move >= threshold:
        # M5 confirms BUY: price rising → small bonus
        adjustment = confirm_bonus
        description = f"score {final_score:.1f} → {final_score + adjustment:.1f} (M5 confirms BUY: +{move_pct:.2f}%)"
    
    new_score = round(max(0, min(100, final_score + adjustment)), 2)
    return new_score, adjustment, description


# ============================================================================
# MAIN FUNCTION: ANALYZE WITH BRAIN
# ============================================================================

def analyze_with_brain(tech_data: Dict, ml_data: Dict, momentum_data: Dict,
                       news_data: Dict, current_price: float = 0,
                       calendar_data: Optional[Dict] = None,
                       volatility_status: Optional[Dict] = None,
                       m5_data: Optional[Dict] = None,
                       sr_data: Optional[Dict] = None) -> BrainResult:
    """
    Complete Central Brain analysis.
    
    Receives detailed data from all 5 analyzers and executes
    the 12 steps of contextual reasoning.
    
    Args:
        tech_data: Dict from analyze_technical_detailed()
        ml_data: Dict from get_ml_detailed()
        momentum_data: Dict from analyze_momentum()
        news_data: Dict from get_news_detailed()
        current_price: Current price (informational)
        calendar_data: Dict from economic_calendar.get_calendar_data()
        volatility_status: Dict from volatility_guard.get_volatility_status()
        sr_data: Dict from support_resistance.get_sr_context()
    
    Returns:
        BrainResult with decision, score, confidence and full explanation
    """
    
    # Default calendar_data if not provided
    if calendar_data is None:
        calendar_data = {
            "score": 50.0, "bias": "NEUTRAL", "phase": "normal",
            "phase_description": "No calendar data",
            "events": [], "events_count": 0, "closest_event": None,
            "source": "default", "error": None,
        }
    
    # STEP 1: Receive data (already received as args)
    original_scores = {
        "technical": tech_data.get("score", 50.0),
        "ml": ml_data.get("score", 50.0),
        "momentum": momentum_data.get("score", 50.0),
        "news": news_data.get("score", 50.0),
        "calendar": calendar_data.get("score", 50.0),
    }
    
    # STEP 2: Analyze technical context
    tech_adjustment, tech_confirmations, tech_alerts = _analyze_technical_context(
        tech_data, ml_data, momentum_data
    )
    
    # STEP 3: Validate ML
    ml_weight_adj, ml_confirmations, ml_alerts = _validate_ml(
        ml_data, momentum_data, tech_data
    )
    
    # STEP 4: Evaluate momentum
    momentum_strength, momentum_direction, momentum_confirmations, momentum_alerts = _evaluate_momentum(
        momentum_data
    )
    
    # STEP 5: Analyze fundamentals
    fund_alignment, fund_confirmations, fund_alerts = _analyze_fundamentals(
        news_data
    )
    
    # STEP 5.5: Economic calendar (5th pillar)
    calendar_score = calendar_data.get("score", 50.0)
    calendar_bias = calendar_data.get("bias", "NEUTRAL")
    calendar_phase = calendar_data.get("phase", "normal")
    
    # Consolidate confirmations and alerts
    all_confirmations = tech_confirmations + ml_confirmations + momentum_confirmations + fund_confirmations
    all_alerts = tech_alerts + ml_alerts + momentum_alerts + fund_alerts
    
    # Calendar-specific confirmations/alerts
    if calendar_phase == "pre_event":
        all_alerts.append(f"Economic event approaching - caution (Calendar Score: {calendar_score:.0f})")
    elif calendar_phase == "during":
        all_alerts.append(f"DURING economic RELEASE - maximum risk (Calendar Score: {calendar_score:.0f})")
    elif calendar_phase == "post_event" and calendar_bias != "NEUTRAL":
        all_confirmations.append(f"Post-event with {calendar_bias} bias (Calendar Score: {calendar_score:.0f})")
    
    # Volatility Guard confirmations/alerts
    vol = volatility_status or {}
    vol_status = vol.get("status", "NORMAL")
    if vol_status == "EXTREME":
        all_alerts.append(f"EXTREME VOLATILITY - entry blocked ({vol.get('extreme_percent', 0):.1f}% on M5 candle)")
    elif vol_status == "COOLING_DOWN":
        all_alerts.append(f"Cooling down after extreme volatility ({vol.get('minutes_since_extreme', 0):.0f} min ago) - strong signals only")
    
    # STEP 6: Identify scenario
    scenario, scenario_desc, confidence_multiplier = _identify_scenario(
        tech_data, ml_data, momentum_data, news_data, momentum_strength,
        calendar_data=calendar_data,
        volatility_status=volatility_status,
        sr_data=sr_data
    )
    
    # STEP 7: Adjust weights
    ml_confidence = ml_data.get("max_confidence", 0.5)
    momentum_score_val = momentum_data.get("score", 50.0)
    weights = _adjust_weights(scenario, ml_confidence, calendar_phase=calendar_phase,
                              momentum_score=momentum_score_val, momentum_strength=momentum_strength)
    
    # Calculate adjusted scores
    adjusted_scores = {
        "technical": max(0, min(100, original_scores["technical"] + tech_adjustment)),
        "ml": original_scores["ml"],  # ML score is not adjusted, weight changes instead
        "momentum": original_scores["momentum"],
        "news": original_scores["news"],
        "calendar": original_scores["calendar"],
    }
    
    # STEP 8: Calculate final score
    final_score = _calculate_final_score(adjusted_scores, weights)
    
    # STEP 8.5: M5 adjustment ("now" awareness)
    m5_adjustment = 0.0
    m5_adj_description = ""
    if m5_data and m5_data.get("move_pct") is not None:
        final_score, m5_adjustment, m5_adj_description = _apply_m5_adjustment(
            final_score, m5_data
        )
        if m5_adjustment != 0:
            if m5_adjustment > 0:
                all_confirmations.append(f"M5 adjustment: {m5_adj_description}")
            else:
                all_alerts.append(f"M5 adjustment: {m5_adj_description}")
    
    # STEP 9: Make decision
    decision = _make_decision(final_score, scenario)
    
    # STEP 9.5: Momentum-vs-ML override
    # When real market momentum strongly contradicts the decision, block
    momentum_score_val = original_scores.get("momentum", 50.0)
    tech_ema_data = tech_data.get("ema", {})
    price_below_all_emas = not tech_ema_data.get("above_ema20", True) and not tech_ema_data.get("above_ema50", True)
    price_above_all_emas = tech_ema_data.get("above_ema20", False) and tech_ema_data.get("above_ema50", False)
    
    if decision in ("BUY", "STRONG_BUY") and momentum_score_val < 30 and price_below_all_emas:
        all_alerts.append(f"BLOCK: Strong bearish momentum ({momentum_score_val:.0f}) + price below EMAs contradicts BUY — override to HOLD")
        decision = "HOLD"
    elif decision in ("SELL", "STRONG_SELL") and momentum_score_val > 70 and price_above_all_emas:
        all_alerts.append(f"BLOCK: Strong bullish momentum ({momentum_score_val:.0f}) + price above EMAs contradicts SELL — override to HOLD")
        decision = "HOLD"
    
    # STEP 9.6: Parabolic exhaustion penalty (confidence -30%)
    # Extreme RSI + extreme momentum = possible top/bottom — reduce confidence, don't block
    parabolic_penalty = False
    rsi_val = tech_data.get("rsi", {}).get("value", 50)
    if decision in ("BUY", "STRONG_BUY") and rsi_val > 80 and momentum_score_val > 90:
        all_alerts.append(f"PENALTY: Parabolic exhaustion (RSI={rsi_val:.0f} + Momentum={momentum_score_val:.0f}) — confidence -30%")
        parabolic_penalty = True
    elif decision in ("SELL", "STRONG_SELL") and rsi_val < 20 and momentum_score_val > 90:
        all_alerts.append(f"PENALTY: Parabolic exhaustion (RSI={rsi_val:.0f} + Momentum={momentum_score_val:.0f}) — confidence -30%")
        parabolic_penalty = True
    
    # STEP 10: Calculate confidence
    # Get volume ratio for Volume Gate
    volume_ratio = momentum_data.get("volume", {}).get("volume_ratio", 1.0)
    
    confidence, confidence_level = _calculate_confidence(
        confidence_multiplier, all_confirmations, all_alerts,
        ml_confidence, momentum_strength, fund_alignment,
        final_score=final_score,
        calendar_bias=calendar_bias,
        decision=decision,
        volatility_status=volatility_status,
        news_score=original_scores["news"],
        scenario=scenario,
        volume_ratio=volume_ratio
    )
    
    # STEP 10.3: Multi-TF Trend Alignment
    d1_trend = _get_mtf_trend_direction("D1")
    h4_trend = _get_mtf_trend_direction("H4")
    mtf_adj, mtf_confs, mtf_alerts = _check_mtf_trend_alignment(decision, d1_trend, h4_trend)
    if mtf_adj != 0:
        confidence = max(0, min(100, confidence + mtf_adj))
    all_confirmations.extend(mtf_confs)
    all_alerts.extend(mtf_alerts)
    
    # Build MTF trend data for dashboard
    mtf_trend_data = {
        "d1_direction": d1_trend,
        "h4_direction": h4_trend,
        "alignment": "n/a",
        "confidence_adjustment": mtf_adj,
    }
    if d1_trend and h4_trend and d1_trend == h4_trend:
        trade_bullish = decision in ("BUY", "STRONG_BUY")
        trade_bearish = decision in ("SELL", "STRONG_SELL")
        if decision == "HOLD":
            mtf_trend_data["alignment"] = "n/a"
        elif (d1_trend == "bullish" and trade_bullish) or (d1_trend == "bearish" and trade_bearish):
            mtf_trend_data["alignment"] = "aligned"
        else:
            mtf_trend_data["alignment"] = "conflict"
    elif d1_trend and h4_trend:
        mtf_trend_data["alignment"] = "mixed"
    
    # Add Volume Gate alert if penalty was applied
    import config as _cfg
    volume_gate_data = {
        "volume_ratio": round(volume_ratio, 2),
        "status": "normal",
        "confidence_adjustment": 0,
    }
    if getattr(_cfg, 'VOLUME_GATE_ENABLED', True):
        severe_threshold = getattr(_cfg, 'VOLUME_GATE_SEVERE_THRESHOLD', 0.3)
        moderate_threshold = getattr(_cfg, 'VOLUME_GATE_MODERATE_THRESHOLD', 0.5)
        severe_penalty = getattr(_cfg, 'VOLUME_GATE_SEVERE_PENALTY', 25)
        moderate_penalty = getattr(_cfg, 'VOLUME_GATE_MODERATE_PENALTY', 15)
        
        if volume_ratio < severe_threshold:
            all_alerts.append(f"Volume Gate: {volume_ratio:.1f}x average (severe) → -{severe_penalty} conf")
            volume_gate_data["status"] = "very_low"
            volume_gate_data["confidence_adjustment"] = -severe_penalty
        elif volume_ratio < moderate_threshold:
            all_alerts.append(f"Volume Gate: {volume_ratio:.1f}x average (moderate) → -{moderate_penalty} conf")
            volume_gate_data["status"] = "low"
            volume_gate_data["confidence_adjustment"] = -moderate_penalty
    
    # Apply parabolic penalty (-30%) after confidence calculation
    if parabolic_penalty:
        orig_conf = confidence
        confidence = confidence * 0.70
        all_alerts.append(f"Confidence reduced: {orig_conf:.0f}% → {confidence:.0f}% (parabolic penalty -30%)")
    
    # STEP 10.5: S/R confidence adjustment
    sr_adj_description = ""
    if sr_data and isinstance(sr_data, dict):
        sr_conf_adj = sr_data.get("confidence_adjustment", 0.0)
        sr_confs = sr_data.get("confirmations", [])
        sr_alerts_list = sr_data.get("alerts", [])
        sr_adj_description = sr_data.get("description", "")
        if sr_conf_adj != 0:
            orig_conf = confidence
            confidence = max(0, min(100, confidence + sr_conf_adj))
        all_confirmations.extend(sr_confs)
        all_alerts.extend(sr_alerts_list)
    
    # STEP 11: Build explanation
    explanation = _build_explanation(
        decision, final_score, scenario, scenario_desc,
        tech_data, ml_data, momentum_data, news_data,
        adjusted_scores, original_scores, weights,
        confidence, confidence_level,
        tech_confirmations, tech_alerts,
        ml_confirmations, ml_alerts,
        momentum_strength, momentum_direction, momentum_confirmations, momentum_alerts,
        fund_alignment, fund_confirmations, fund_alerts,
        calendar_data=calendar_data,
        volatility_status=volatility_status,
        m5_adj_description=m5_adj_description,
        sr_description=sr_adj_description
    )
    
    # Build raw_data
    raw_data = {
        "technical": tech_data,
        "ml": ml_data,
        "momentum": momentum_data,
        "news": news_data,
        "calendar": calendar_data,
        "volatility": volatility_status,
        "sr": sr_data,
        "current_price": current_price,
    }
    
    return BrainResult(
        decision=decision,
        final_score=final_score,
        confidence=confidence,
        confidence_level=confidence_level,
        scenario=scenario,
        scenario_description=scenario_desc,
        explanation=explanation,
        adjusted_weights=weights,
        adjusted_scores=adjusted_scores,
        original_scores=original_scores,
        confirmations=all_confirmations,
        alerts=all_alerts,
        raw_data=raw_data,
        mtf_trend=mtf_trend_data,
        volume_gate=volume_gate_data,
    )


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def is_actionable_signal(decision: str) -> bool:
    """Check if the decision requires action (open trade)"""
    return decision in ["STRONG_BUY", "BUY", "SELL", "STRONG_SELL"]


def get_trade_direction(decision: str) -> Optional[str]:
    """Return trade direction based on decision"""
    if decision in ["STRONG_BUY", "BUY"]:
        return "BUY"
    elif decision in ["STRONG_SELL", "SELL"]:
        return "SELL"
    return None
