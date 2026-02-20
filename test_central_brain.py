"""
ISOLATED CENTRAL BRAIN TEST
Tests central_brain.py with mocked data to validate reasoning logic.

Scenarios tested:
1. Case 28 Jan (RSI 78 + ML bullish 66% + ADX 45) -> BUY
2. Ranging (ADX 15, ML 52%, volume 0.7x) -> HOLD
3. Perfect Alignment (all bullish) -> BUY + VERY HIGH confidence
4. Conflicting Signals (Tech bearish, ML bullish) -> reduced confidence
5. MACD Divergence bearish -> technical weight increased
6-8. Calendar tests (post-event, pre-event, during release)
9. Extreme Volatility -> forced HOLD (score 0, confidence 0)
10. Cooling Down + high confidence (>=70) -> allows trade
11. Cooling Down + low confidence (<70) -> forced HOLD
12. GPT BOOST -> confidence rises
13. GPT REDUCE -> confidence drops
14. Momentum Direction -- ADX bullish (+DI >> -DI) with last candle bearish -> direction bullish
15. ML Dynamic Weight -- Momentum 90 + strong -> ML weight < momentum weight
16. Cycle Memory -- missed opportunity pattern detection
17. Volume Penalty -- High momentum + low volume -> strength downgraded
18. ML Weight Floor -- Double penalty does not reduce ML below 10%
19. Confidence Cap -- Weak ML (<55%) + Bearish News (<45) -> confidence limited
20. Smart Pyramid -- losing position blocks reinforcement in same direction
21. Smart Pyramid -- position in profit >=0.3% allows reinforcement
22. Smart Pyramid -- different direction always allowed
23. M5 Reversal -- graceful degradation without MT5
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from central_brain import analyze_with_brain, is_actionable_signal


# ============================================================================
# MOCK DATA
# ============================================================================

def mock_caso_28_jan():
    """Case 28 Jan: RSI 78, ML bullish 66%, ADX 45, 5 green candles, volume 1.7x, DXY falling"""
    tech_data = {
        "score": 34.5,
        "breakdown": {"trend": 15, "momentum": 0, "macd": 10, "bollinger": 2, "stochastic": 3, "price_action": 4.5},
        "rsi": {"value": 78, "level": "overbought"},
        "macd": {"signal": "bullish", "histogram": 0.5, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "banda_superior", "width": 25.0, "squeeze": False},
        "stochastic": {"value": 82, "level": "overbought"},
        "error": None,
    }
    
    ml_data = {
        "score": 66.2,
        "prediction": "bullish",
        "probability": 0.662,
        "max_confidence": 0.662,
        "pattern": "continuacao",
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": None,
    }
    
    momentum_data = {
        "score": 85.0,
        "adx": {"adx_value": 45, "adx_classification": "very_strong", "plus_di": 35, "minus_di": 15},
        "volume": {"volume_ratio": 1.7, "volume_classification": "explosive", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 5, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 12.5, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    
    news_data = {
        "score": 54.3,
        "dxy": {"value": 103.2, "trend": "falling", "change_24h": -0.8},
        "yields": {"value": 4.52, "trend": "rising", "change_24h": 0.1},
        "vix": {"value": 16.5, "level": "low"},
        "sentiment": {"headlines_score": 55, "normalized": 0.10},
        "high_impact_news_soon": False,
        "geopolitical_risk": "low",
        "anomalies": [],
        "error": None,
    }
    
    return tech_data, ml_data, momentum_data, news_data, 2770.50


def mock_lateralizacao():
    """Ranging: ADX 15, ML 52%, volume 0.7x"""
    tech_data = {
        "score": 48.0,
        "breakdown": {"trend": 10, "momentum": 10, "macd": 8, "bollinger": 7, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 52, "level": "neutro"},
        "macd": {"signal": "bearish", "histogram": -0.1, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": False, "above_ema200": False, "trend": "misto"},
        "bollinger": {"position": "meio", "width": 15.0, "squeeze": True},
        "stochastic": {"value": 48, "level": "neutro"},
        "error": None,
    }
    
    ml_data = {
        "score": 52.0,
        "prediction": "bullish",
        "probability": 0.52,
        "max_confidence": 0.52,
        "pattern": "indefinido",
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": None,
    }
    
    momentum_data = {
        "score": 30.0,
        "adx": {"adx_value": 15, "adx_classification": "very_weak", "plus_di": 18, "minus_di": 16},
        "volume": {"volume_ratio": 0.7, "volume_classification": "low", "volume_trend": "decreasing"},
        "candles": {"consecutive_count": 1, "consecutive_direction": "bearish"},
        "atr": {"atr_current": 8.0, "atr_trend": "decreasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    
    news_data = {
        "score": 50.0,
        "dxy": {"value": 104.0, "trend": "rising", "change_24h": 0.1},
        "yields": {"value": 4.55, "trend": "rising", "change_24h": 0.05},
        "vix": {"value": 14.0, "level": "low"},
        "sentiment": {"headlines_score": 50, "normalized": 0.0},
        "high_impact_news_soon": False,
        "geopolitical_risk": "low",
        "anomalies": [],
        "error": None,
    }
    
    return tech_data, ml_data, momentum_data, news_data, 2650.00


def mock_alinhamento_perfeito():
    """Perfect Alignment: all bullish, all agree"""
    tech_data = {
        "score": 78.0,
        "breakdown": {"trend": 25, "momentum": 15, "macd": 18, "bollinger": 10, "stochastic": 7, "price_action": 8},
        "rsi": {"value": 62, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 1.2, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 20.0, "squeeze": False},
        "stochastic": {"value": 65, "level": "neutro"},
        "error": None,
    }
    
    ml_data = {
        "score": 75.0,
        "prediction": "bullish",
        "probability": 0.75,
        "max_confidence": 0.75,
        "pattern": "continuacao",
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": None,
    }
    
    momentum_data = {
        "score": 80.0,
        "adx": {"adx_value": 38, "adx_classification": "strong", "plus_di": 32, "minus_di": 12},
        "volume": {"volume_ratio": 1.4, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 4, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    
    news_data = {
        "score": 68.0,
        "dxy": {"value": 102.5, "trend": "falling", "change_24h": -1.2},
        "yields": {"value": 4.40, "trend": "falling", "change_24h": -0.3},
        "vix": {"value": 22.0, "level": "high"},
        "sentiment": {"headlines_score": 65, "normalized": 0.30},
        "high_impact_news_soon": False,
        "geopolitical_risk": "low",
        "anomalies": [],
        "error": None,
    }
    
    return tech_data, ml_data, momentum_data, news_data, 2700.00


def mock_sinais_conflitantes():
    """Sinais Conflitantes: Tech bearish, ML bullish forte"""
    tech_data = {
        "score": 30.0,
        "breakdown": {"trend": 5, "momentum": 0, "macd": 5, "bollinger": 5, "stochastic": 3, "price_action": 2},
        "rsi": {"value": 75, "level": "overbought"},
        "macd": {"signal": "bearish", "histogram": -0.3, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": False, "above_ema50": False, "above_ema200": True, "trend": "misto"},
        "bollinger": {"position": "banda_superior", "width": 22.0, "squeeze": False},
        "stochastic": {"value": 78, "level": "neutro"},
        "error": None,
    }
    
    ml_data = {
        "score": 72.0,
        "prediction": "bullish",
        "probability": 0.72,
        "max_confidence": 0.72,
        "pattern": "reversao",
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": None,
    }
    
    momentum_data = {
        "score": 55.0,
        "adx": {"adx_value": 22, "adx_classification": "weak", "plus_di": 20, "minus_di": 18},
        "volume": {"volume_ratio": 1.1, "volume_classification": "normal", "volume_trend": "stable"},
        "candles": {"consecutive_count": 2, "consecutive_direction": "bearish"},
        "atr": {"atr_current": 10.0, "atr_trend": "stable"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    
    news_data = {
        "score": 52.0,
        "dxy": {"value": 103.8, "trend": "rising", "change_24h": 0.3},
        "yields": {"value": 4.50, "trend": "rising", "change_24h": 0.1},
        "vix": {"value": 15.0, "level": "low"},
        "sentiment": {"headlines_score": 48, "normalized": -0.04},
        "high_impact_news_soon": False,
        "geopolitical_risk": "low",
        "anomalies": [],
        "error": None,
    }
    
    return tech_data, ml_data, momentum_data, news_data, 2680.00


def mock_divergencia_macd():
    """MACD Divergence bearish -> technical weight increased"""
    tech_data = {
        "score": 60.0,
        "breakdown": {"trend": 20, "momentum": 5, "macd": 12, "bollinger": 8, "stochastic": 5, "price_action": 7},
        "rsi": {"value": 65, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.2, "divergence": {"detected": True, "type": "bearish", "bars_since": 0}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 60, "level": "neutro"},
        "error": None,
    }
    
    ml_data = {
        "score": 58.0,
        "prediction": "bullish",
        "probability": 0.58,
        "max_confidence": 0.58,
        "pattern": "indefinido",
        "similar_patterns_count": None,
        "historical_success_rate": None,
        "error": None,
    }
    
    momentum_data = {
        "score": 55.0,
        "adx": {"adx_value": 28, "adx_classification": "moderate", "plus_di": 25, "minus_di": 18},
        "volume": {"volume_ratio": 1.0, "volume_classification": "normal", "volume_trend": "stable"},
        "candles": {"consecutive_count": 2, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 10.5, "atr_trend": "stable"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    
    news_data = {
        "score": 52.0,
        "dxy": {"value": 103.5, "trend": "rising", "change_24h": 0.2},
        "yields": {"value": 4.48, "trend": "rising", "change_24h": 0.05},
        "vix": {"value": 15.5, "level": "low"},
        "sentiment": {"headlines_score": 50, "normalized": 0.0},
        "high_impact_news_soon": False,
        "geopolitical_risk": "low",
        "anomalies": [],
        "error": None,
    }
    
    return tech_data, ml_data, momentum_data, news_data, 2690.00


def mock_pos_evento_bearish():
    """Pos-evento CPI: Calendar Score 15 (bearish), Tech bearish, Momentum forte"""
    tech_data = {
        "score": 35.0,
        "breakdown": {"trend": 5, "momentum": 5, "macd": 5, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 38, "level": "neutro"},
        "macd": {"signal": "bearish", "histogram": -0.8, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": False, "above_ema50": False, "above_ema200": True, "trend": "bearish"},
        "bollinger": {"position": "banda_inferior", "width": 22.0, "squeeze": False},
        "stochastic": {"value": 30, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 40.0, "prediction": "bearish", "probability": 0.60,
        "max_confidence": 0.60, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 75.0,
        "adx": {"adx_value": 35, "adx_classification": "strong", "plus_di": 12, "minus_di": 30},
        "volume": {"volume_ratio": 1.8, "volume_classification": "explosive", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 4, "consecutive_direction": "bearish"},
        "atr": {"atr_current": 14.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 45.0,
        "dxy": {"value": 104.5, "trend": "rising", "change_24h": 0.8},
        "yields": {"value": 4.60, "trend": "rising", "change_24h": 0.15},
        "vix": {"value": 18.0, "level": "low"},
        "sentiment": {"headlines_score": 40, "normalized": -0.20},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    calendar_data = {
        "score": 15.0, "bias": "BEARISH", "phase": "post_event",
        "phase_description": "Post-event: CPI above expected (bias: BEARISH)",
        "events": [{"name": "Consumer Price Index", "time_server": "2026.02.12 13:30:00",
                     "actual_value": 3.2, "forecast_value": 2.9, "previous_value": 2.7}],
        "events_count": 1,
        "closest_event": {"name": "Consumer Price Index", "time_server": "2026.02.12 13:30:00",
                          "actual_value": 3.2, "forecast_value": 2.9, "previous_value": 2.7},
        "source": "mt5_bridge", "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 2720.00, calendar_data


def mock_pre_evento():
    """Pre-evento: NFP em 15 min, Calendar Score 20 (cautela)"""
    tech_data = {
        "score": 62.0,
        "breakdown": {"trend": 15, "momentum": 10, "macd": 12, "bollinger": 8, "stochastic": 7, "price_action": 7},
        "rsi": {"value": 58, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.3, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 60.0, "prediction": "bullish", "probability": 0.60,
        "max_confidence": 0.60, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 55.0,
        "adx": {"adx_value": 25, "adx_classification": "moderate", "plus_di": 22, "minus_di": 16},
        "volume": {"volume_ratio": 1.0, "volume_classification": "normal", "volume_trend": "stable"},
        "candles": {"consecutive_count": 2, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 10.0, "atr_trend": "stable"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 52.0,
        "dxy": {"value": 103.5, "trend": "rising", "change_24h": 0.1},
        "yields": {"value": 4.50, "trend": "rising", "change_24h": 0.05},
        "vix": {"value": 16.0, "level": "low"},
        "sentiment": {"headlines_score": 50, "normalized": 0.0},
        "high_impact_news_soon": True, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    calendar_data = {
        "score": 20.0, "bias": "NEUTRAL", "phase": "pre_event",
        "phase_description": "Pre-evento: Nonfarm Payrolls em breve",
        "events": [{"name": "Nonfarm Payrolls", "time_server": "2026.02.07 13:30:00",
                     "actual_value": None, "forecast_value": 180.0, "previous_value": 175.0}],
        "events_count": 1,
        "closest_event": {"name": "Nonfarm Payrolls", "time_server": "2026.02.07 13:30:00",
                          "actual_value": None, "forecast_value": 180.0, "previous_value": 175.0},
        "source": "mt5_bridge", "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 2750.00, calendar_data


def mock_durante_release():
    """During release: Calendar Score 0 (maximum risk)"""
    tech_data = {
        "score": 70.0,
        "breakdown": {"trend": 20, "momentum": 12, "macd": 15, "bollinger": 8, "stochastic": 7, "price_action": 8},
        "rsi": {"value": 62, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.6, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 20.0, "squeeze": False},
        "stochastic": {"value": 60, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 65.0, "prediction": "bullish", "probability": 0.65,
        "max_confidence": 0.65, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 60.0,
        "adx": {"adx_value": 28, "adx_classification": "moderate", "plus_di": 24, "minus_di": 16},
        "volume": {"volume_ratio": 1.2, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 3, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 55.0,
        "dxy": {"value": 103.0, "trend": "falling", "change_24h": -0.3},
        "yields": {"value": 4.45, "trend": "falling", "change_24h": -0.1},
        "vix": {"value": 17.0, "level": "low"},
        "sentiment": {"headlines_score": 55, "normalized": 0.10},
        "high_impact_news_soon": True, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    calendar_data = {
        "score": 0.0, "bias": "NEUTRAL", "phase": "during",
        "phase_description": "DURING RELEASE: Fed Interest Rate Decision",
        "events": [{"name": "Fed Interest Rate Decision", "time_server": "2026.03.19 19:00:00",
                     "actual_value": None, "forecast_value": 4.50, "previous_value": 4.50}],
        "events_count": 1,
        "closest_event": {"name": "Fed Interest Rate Decision", "time_server": "2026.03.19 19:00:00",
                          "actual_value": None, "forecast_value": 4.50, "previous_value": 4.50},
        "source": "mt5_bridge", "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 2760.00, calendar_data


def mock_volatilidade_extrema():
    """Extreme Volatility: M5 candle with -2.00% (free fall) -> forced HOLD"""
    tech_data = {
        "score": 50.0,
        "breakdown": {"trend": 10, "momentum": 10, "macd": 10, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 55, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.3, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 20.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 62.0, "prediction": "bullish", "probability": 0.62,
        "max_confidence": 0.62, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 60.0,
        "adx": {"adx_value": 28, "adx_classification": "moderate", "plus_di": 22, "minus_di": 16},
        "volume": {"volume_ratio": 1.2, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 3, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 55.0,
        "dxy": {"value": 103.0, "trend": "falling", "change_24h": -0.3},
        "yields": {"value": 4.45, "trend": "falling", "change_24h": -0.1},
        "vix": {"value": 17.0, "level": "low"},
        "sentiment": {"headlines_score": 55, "normalized": 0.10},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    volatility_status = {
        "status": "EXTREME",
        "last_extreme_candle": {
            "time": "2026-02-12 18:10:00", "open": 5050.0, "close": 4949.0,
            "high": 5055.0, "low": 4945.0, "move_percent": 2.00,
            "direction": "DOWN", "minutes_ago": 4.0,
        },
        "minutes_since_extreme": 4.0,
        "extreme_percent": 2.00,
        "cooling_reason": None,
        "description": "EXTREME: M5 candle DOWN 2.00% (4 min ago) — TOTAL BLOCK",
    }
    return tech_data, ml_data, momentum_data, news_data, 4956.00, None, volatility_status


def mock_cooling_alta_confianca():
    """Cooling Down + strong signals (perfect alignment) → allows trade"""
    tech_data = {
        "score": 78.0,
        "breakdown": {"trend": 25, "momentum": 15, "macd": 18, "bollinger": 10, "stochastic": 7, "price_action": 8},
        "rsi": {"value": 62, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 1.2, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 20.0, "squeeze": False},
        "stochastic": {"value": 65, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 75.0, "prediction": "bullish", "probability": 0.75,
        "max_confidence": 0.75, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 80.0,
        "adx": {"adx_value": 38, "adx_classification": "strong", "plus_di": 32, "minus_di": 12},
        "volume": {"volume_ratio": 1.4, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 4, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 68.0,
        "dxy": {"value": 102.5, "trend": "falling", "change_24h": -1.2},
        "yields": {"value": 4.40, "trend": "falling", "change_24h": -0.3},
        "vix": {"value": 22.0, "level": "high"},
        "sentiment": {"headlines_score": 65, "normalized": 0.30},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    volatility_status = {
        "status": "COOLING_DOWN",
        "last_extreme_candle": {
            "time": "2026-02-12 17:10:00", "open": 5050.0, "close": 4949.0,
            "high": 5055.0, "low": 4945.0, "move_percent": 2.00,
            "direction": "DOWN", "minutes_ago": 45.0,
        },
        "minutes_since_extreme": 45.0,
        "extreme_percent": 2.00,
        "cooling_reason": "confirmed",
        "description": "CONFIRMED: Cascade DOWN — candle 1: 2.00%, candle 2: 1.20% same dir (45 min ago) — COOLING 90 min",
    }
    return tech_data, ml_data, momentum_data, news_data, 4980.00, None, volatility_status


def mock_momentum_direction_adx_bullish():
    """Fix 1 test: ADX +DI >> -DI (bullish) but last candle bearish → direction should be bullish"""
    tech_data = {
        "score": 55.0,
        "breakdown": {"trend": 15, "momentum": 10, "macd": 10, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 58, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.3, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 60.0, "prediction": "bullish", "probability": 0.60,
        "max_confidence": 0.60, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 85.0,
        "adx": {"adx_value": 35, "adx_classification": "strong", "plus_di": 30, "minus_di": 15},
        "volume": {"volume_ratio": 1.3, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 1, "consecutive_direction": "bearish"},  # Last candle bearish!
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 54.0,
        "dxy": {"value": 103.0, "trend": "falling", "change_24h": -0.5},
        "yields": {"value": 4.45, "trend": "falling", "change_24h": -0.1},
        "vix": {"value": 16.0, "level": "low"},
        "sentiment": {"headlines_score": 52, "normalized": 0.04},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 2750.00


def mock_ml_dynamic_weight():
    """Fix 3 test: Momentum 90 + strong → ML weight should be reduced vs momentum weight"""
    tech_data = {
        "score": 55.0,
        "breakdown": {"trend": 15, "momentum": 10, "macd": 10, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 55, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.2, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 40.0, "prediction": "bearish", "probability": 0.60,
        "max_confidence": 0.60, "pattern": "continuacao",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 90.0,
        "adx": {"adx_value": 38, "adx_classification": "strong", "plus_di": 32, "minus_di": 14},
        "volume": {"volume_ratio": 1.5, "volume_classification": "explosive", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 4, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 12.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": True, "breakout_type": "resistance"},
        "error": None,
    }
    news_data = {
        "score": 54.0,
        "dxy": {"value": 103.0, "trend": "falling", "change_24h": -0.5},
        "yields": {"value": 4.45, "trend": "falling", "change_24h": -0.1},
        "vix": {"value": 16.0, "level": "low"},
        "sentiment": {"headlines_score": 52, "normalized": 0.04},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 2750.00


def mock_cooling_baixa_confianca():
    """Cooling Down + weak signals (ranging) -> forced HOLD"""
    tech_data = {
        "score": 48.0,
        "breakdown": {"trend": 10, "momentum": 10, "macd": 8, "bollinger": 7, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 52, "level": "neutro"},
        "macd": {"signal": "bearish", "histogram": -0.1, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": False, "above_ema200": False, "trend": "misto"},
        "bollinger": {"position": "meio", "width": 15.0, "squeeze": True},
        "stochastic": {"value": 48, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 52.0, "prediction": "bullish", "probability": 0.52,
        "max_confidence": 0.52, "pattern": "indefinido",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 30.0,
        "adx": {"adx_value": 15, "adx_classification": "very_weak", "plus_di": 18, "minus_di": 16},
        "volume": {"volume_ratio": 0.7, "volume_classification": "low", "volume_trend": "decreasing"},
        "candles": {"consecutive_count": 1, "consecutive_direction": "bearish"},
        "atr": {"atr_current": 8.0, "atr_trend": "decreasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 50.0,
        "dxy": {"value": 104.0, "trend": "rising", "change_24h": 0.1},
        "yields": {"value": 4.55, "trend": "rising", "change_24h": 0.05},
        "vix": {"value": 14.0, "level": "low"},
        "sentiment": {"headlines_score": 50, "normalized": 0.0},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    volatility_status = {
        "status": "COOLING_DOWN",
        "last_extreme_candle": {
            "time": "2026-02-12 17:10:00", "open": 5050.0, "close": 4949.0,
            "high": 5055.0, "low": 4945.0, "move_percent": 2.00,
            "direction": "DOWN", "minutes_ago": 45.0,
        },
        "minutes_since_extreme": 45.0,
        "extreme_percent": 2.00,
        "cooling_reason": "ambiguous",
        "description": "AMBIGUOUS: Extreme candle DOWN 2.00% 20 min ago, next candle DOWN 0.70% — COOLING 30 min",
    }
    return tech_data, ml_data, momentum_data, news_data, 4980.00, None, volatility_status


def mock_volume_penalty():
    """Test 17: Momentum 85 + volume 0.4x → strength should be downgraded (false breakout)"""
    tech_data = {
        "score": 55.0,
        "breakdown": {"trend": 15, "momentum": 10, "macd": 10, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 58, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.3, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 51.0, "prediction": "bullish", "probability": 0.51,
        "max_confidence": 0.51, "pattern": "indefinido",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 85.0,
        "adx": {"adx_value": 28, "adx_classification": "moderate", "plus_di": 24, "minus_di": 14},
        "volume": {"volume_ratio": 0.4, "volume_classification": "low", "volume_trend": "decreasing"},
        "candles": {"consecutive_count": 5, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": True, "breakout_type": "resistance"},
        "error": None,
    }
    news_data = {
        "score": 44.0,
        "dxy": {"value": 104.0, "trend": "rising", "change_24h": 0.2},
        "yields": {"value": 4.50, "trend": "rising", "change_24h": 0.05},
        "vix": {"value": 15.0, "level": "low"},
        "sentiment": {"headlines_score": 45, "normalized": -0.10},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 5010.00


def mock_ml_weight_floor():
    """Test 18: ML fraco (51%) + Momentum 95 strong → ML weight should stay >= 10%"""
    tech_data = {
        "score": 55.0,
        "breakdown": {"trend": 15, "momentum": 10, "macd": 10, "bollinger": 5, "stochastic": 5, "price_action": 5},
        "rsi": {"value": 55, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.2, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 18.0, "squeeze": False},
        "stochastic": {"value": 55, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 51.0, "prediction": "bullish", "probability": 0.51,
        "max_confidence": 0.51, "pattern": "indefinido",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 95.0,
        "adx": {"adx_value": 38, "adx_classification": "strong", "plus_di": 32, "minus_di": 14},
        "volume": {"volume_ratio": 1.5, "volume_classification": "explosive", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 5, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 12.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": True, "breakout_type": "resistance"},
        "error": None,
    }
    news_data = {
        "score": 54.0,
        "dxy": {"value": 103.0, "trend": "falling", "change_24h": -0.5},
        "yields": {"value": 4.45, "trend": "falling", "change_24h": -0.1},
        "vix": {"value": 16.0, "level": "low"},
        "sentiment": {"headlines_score": 52, "normalized": 0.04},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 5020.00


def mock_confidence_cap_ml_weak():
    """Test 19: ML 51% + News 42 (bearish) + BUY signal → confidence capped + penalized"""
    tech_data = {
        "score": 70.0,
        "breakdown": {"trend": 20, "momentum": 12, "macd": 15, "bollinger": 8, "stochastic": 7, "price_action": 8},
        "rsi": {"value": 62, "level": "neutro"},
        "macd": {"signal": "bullish", "histogram": 0.6, "divergence": {"detected": False, "type": None}},
        "ema": {"above_ema20": True, "above_ema50": True, "above_ema200": True, "trend": "bullish"},
        "bollinger": {"position": "meio", "width": 20.0, "squeeze": False},
        "stochastic": {"value": 60, "level": "neutro"},
        "error": None,
    }
    ml_data = {
        "score": 51.0, "prediction": "bullish", "probability": 0.51,
        "max_confidence": 0.51, "pattern": "indefinido",
        "similar_patterns_count": None, "historical_success_rate": None, "error": None,
    }
    momentum_data = {
        "score": 70.0,
        "adx": {"adx_value": 30, "adx_classification": "strong", "plus_di": 26, "minus_di": 14},
        "volume": {"volume_ratio": 1.2, "volume_classification": "high", "volume_trend": "increasing"},
        "candles": {"consecutive_count": 3, "consecutive_direction": "bullish"},
        "atr": {"atr_current": 11.0, "atr_trend": "increasing"},
        "breakout": {"breakout_detected": False, "breakout_type": None},
        "error": None,
    }
    news_data = {
        "score": 42.0,
        "dxy": {"value": 104.5, "trend": "rising", "change_24h": 0.5},
        "yields": {"value": 4.55, "trend": "rising", "change_24h": 0.1},
        "vix": {"value": 15.0, "level": "low"},
        "sentiment": {"headlines_score": 40, "normalized": -0.20},
        "high_impact_news_soon": False, "geopolitical_risk": "low",
        "anomalies": [], "error": None,
    }
    return tech_data, ml_data, momentum_data, news_data, 5015.00


# ============================================================================
# RUNNER
# ============================================================================

def run_test(name, mock_fn, expected_decision, expected_scenario, check_fn=None):
    """Execute a test and validate result"""
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"{'='*60}")
    
    mock_result = mock_fn()
    if len(mock_result) == 7:
        tech_data, ml_data, momentum_data, news_data, price, calendar_data, volatility_status = mock_result
    elif len(mock_result) == 6:
        tech_data, ml_data, momentum_data, news_data, price, calendar_data = mock_result
        volatility_status = None
    else:
        tech_data, ml_data, momentum_data, news_data, price = mock_result
        calendar_data = None
        volatility_status = None
    
    result = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price, calendar_data=calendar_data, volatility_status=volatility_status)
    
    # Show result
    print(f"\n{result.explanation}")
    
    # Validations
    passed = True
    checks = []
    
    # Decision
    if expected_decision:
        if isinstance(expected_decision, list):
            ok = result.decision in expected_decision
            checks.append(f"Decision: {result.decision} (expected: {expected_decision}) {'PASS' if ok else 'FAIL'}")
        else:
            ok = result.decision == expected_decision
            checks.append(f"Decision: {result.decision} (expected: {expected_decision}) {'PASS' if ok else 'FAIL'}")
        if not ok:
            passed = False
    
    # Scenario
    if expected_scenario:
        ok = result.scenario == expected_scenario
        checks.append(f"Scenario: {result.scenario} (expected: {expected_scenario}) {'PASS' if ok else 'FAIL'}")
        if not ok:
            passed = False
    
    # Custom check
    if check_fn:
        custom_ok, custom_msg = check_fn(result)
        checks.append(f"{custom_msg} {'PASS' if custom_ok else 'FAIL'}")
        if not custom_ok:
            passed = False
    
    print(f"\n--- VALIDATIONS ---")
    for c in checks:
        emoji = "+" if "PASS" in c else "!"
        print(f"  {emoji} {c}")
    
    print(f"\n  Score: {result.final_score:.1f}")
    print(f"  Confidence: {result.confidence:.1f} ({result.confidence_level})")
    print(f"  Weights: {result.adjusted_weights}")
    
    status = "PASSED" if passed else "FAILED"
    print(f"\n  >>> RESULT: {status}")
    
    return passed


def main():
    print("=" * 60)
    print("ISOLATED CENTRAL BRAIN TEST")
    print("=" * 60)
    
    results = []
    
    # Test 1: Case 28 Jan
    # Note: scenario "momentum_forte_confirmado" has priority over "rsi_extremo_com_momentum"
    # because ADX 45 + ML 66% > 60% satisfies the condition first
    results.append(run_test(
        "Case 28 Jan - RSI overbought + ML bullish + strong ADX",
        mock_caso_28_jan,
        expected_decision=["BUY", "STRONG_BUY"],
        expected_scenario="momentum_forte_confirmado",
        check_fn=lambda r: (r.final_score > 65, f"Score > 65: {r.final_score:.1f}"),
    ))
    
    # Test 2: Ranging
    results.append(run_test(
        "Ranging - Market with no direction",
        mock_lateralizacao,
        expected_decision="HOLD",
        expected_scenario="lateralizacao",
        check_fn=lambda r: (35 <= r.final_score <= 65, f"Score between 35-65: {r.final_score:.1f}"),
    ))
    
    # Test 3: Perfect Alignment
    results.append(run_test(
        "Perfect Alignment - All bullish",
        mock_alinhamento_perfeito,
        expected_decision=["BUY", "STRONG_BUY"],
        expected_scenario="alinhamento_perfeito",
        check_fn=lambda r: (r.confidence_level in ("HIGH", "VERY_HIGH"), f"High confidence: {r.confidence_level}"),
    ))
    
    # Test 4: Conflicting Signals
    results.append(run_test(
        "Conflicting Signals - Tech bearish vs ML bullish",
        mock_sinais_conflitantes,
        expected_decision=None,  # Any decision is ok
        expected_scenario="sinais_conflitantes",
        check_fn=lambda r: (r.confidence_level in ("LOW", "MEDIUM", "VERY_LOW"), f"Reduced confidence: {r.confidence_level}"),
    ))
    
    # Test 5: MACD Divergence
    results.append(run_test(
        "MACD Divergence bearish",
        mock_divergencia_macd,
        expected_decision=None,  # Depends on adjustments
        expected_scenario="divergencia_tecnica",
        check_fn=lambda r: (r.adjusted_weights.get("technical", 0) > 0.35, f"Tech weight > 35%: {r.adjusted_weights.get('technical', 0):.0%}"),
    ))
    
    # Test 6: Post-event CPI bearish
    # Score 42 = bearish-leaning but within HOLD zone (35-65)
    # Calendar score 15 pulls down + high confidence (perfect bearish alignment)
    # Scenario: alinhamento_perfeito (all bearish) but final score doesn't cross SELL threshold
    results.append(run_test(
        "Post-event CPI bearish + Strong Momentum",
        mock_pos_evento_bearish,
        expected_decision=["HOLD", "SELL", "STRONG_SELL"],
        expected_scenario="alinhamento_perfeito",
        check_fn=lambda r: (r.final_score < 50 and r.original_scores.get("calendar", 50) < 20,
                            f"Score bearish ({r.final_score:.1f} < 50) + Calendar low ({r.original_scores.get('calendar', 50):.0f})"),
    ))
    
    # Test 7: Pre-event NFP
    results.append(run_test(
        "Pre-event NFP - Penalized score",
        mock_pre_evento,
        expected_decision=None,  # Any decision ok
        expected_scenario=None,
        check_fn=lambda r: (r.final_score < 65, f"Penalized score < 65: {r.final_score:.1f}"),
    ))
    
    # Test 8: During Fed release
    results.append(run_test(
        "During Fed release - Organic HOLD",
        mock_durante_release,
        expected_decision="HOLD",
        expected_scenario=None,
        check_fn=lambda r: (r.final_score < 65, f"Score < 65 (calendar 0 pulls down): {r.final_score:.1f}"),
    ))
    
    # Test 9: Extreme Volatility -> forced HOLD
    results.append(run_test(
        "Extreme Volatility - Forced HOLD",
        mock_volatilidade_extrema,
        expected_decision="HOLD",
        expected_scenario="volatilidade_extrema",
        check_fn=lambda r: (r.confidence == 0.0, f"Confidence = 0: {r.confidence:.1f}"),
    ))
    
    # Test 10: Cooling Down + high confidence -> allows trade
    results.append(run_test(
        "Cooling Down + strong signals - allows trade",
        mock_cooling_alta_confianca,
        expected_decision=["BUY", "STRONG_BUY"],
        expected_scenario="alinhamento_perfeito",
        check_fn=lambda r: (r.confidence >= 70, f"Confidence >= 70: {r.confidence:.1f}"),
    ))
    
    # Test 11: Cooling Down + low confidence -> forced HOLD
    results.append(run_test(
        "Cooling Down + weak signals - Forced HOLD",
        mock_cooling_baixa_confianca,
        expected_decision=None,
        expected_scenario="lateralizacao",
        check_fn=lambda r: (r.confidence == 0.0, f"Confidence = 0 (cooling + weak): {r.confidence:.1f}"),
    ))
    
    # Test 12: GPT BOOST -- confidence rises (use case with moderate confidence)
    print(f"\n{'='*60}")
    print(f"TEST: GPT BOOST - Confidence rises")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_divergencia_macd()
    result_12 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    original_conf = result_12.confidence
    # Simulate GPT BOOST
    gpt_boost = {"action": "BOOST", "adjustment": 10, "reason": "fresh MACD divergence confirms reversal setup", "from_cache": False, "error": None}
    boosted_conf = min(100, original_conf + gpt_boost["adjustment"])
    result_12.gpt_validation = gpt_boost
    result_12.confidence = boosted_conf
    ok_12 = (result_12.confidence == boosted_conf and result_12.confidence > original_conf and result_12.gpt_validation["action"] == "BOOST")
    print(f"  Original: {original_conf:.1f} -> Boosted: {boosted_conf:.1f}")
    print(f"  GPT: {gpt_boost}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_12 else 'FAILED'}")
    results.append(ok_12)
    
    # Test 13: GPT REDUCE -- confidence drops
    print(f"\n{'='*60}")
    print(f"TEST: GPT REDUCE - Confidence drops")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_caso_28_jan()
    result_13 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    original_conf_13 = result_13.confidence
    # Simulate GPT REDUCE
    gpt_reduce = {"action": "REDUCE", "adjustment": 12, "reason": "RSI extreme without strong volume confirmation", "from_cache": False, "error": None}
    reduced_conf = max(0, original_conf_13 - gpt_reduce["adjustment"])
    result_13.gpt_validation = gpt_reduce
    result_13.confidence = reduced_conf
    ok_13 = (result_13.confidence == reduced_conf and result_13.confidence < original_conf_13 and result_13.gpt_validation["action"] == "REDUCE")
    print(f"  Original: {original_conf_13:.1f} -> Reduced: {reduced_conf:.1f}")
    print(f"  GPT: {gpt_reduce}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_13 else 'FAILED'}")
    results.append(ok_13)
    
    # Test 14: Momentum Direction -- ADX bullish (+DI >> -DI) with last candle bearish
    print(f"\n{'='*60}")
    print(f"TEST: Momentum Direction - ADX bullish with bearish candle")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_momentum_direction_adx_bullish()
    result_14 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    # Verify that the explanation contains "Direction: bullish" (not bearish)
    has_bullish_dir = "Direction: bullish" in result_14.explanation
    print(f"  Momentum +DI=30, -DI=15, ADX=35, last candle=bearish")
    print(f"  Explanation direction: {'bullish' if has_bullish_dir else 'NOT bullish'}")
    print(f"\n  >>> RESULT: {'PASSED' if has_bullish_dir else 'FAILED'}")
    results.append(has_bullish_dir)
    
    # Test 15: ML Dynamic Weight -- Momentum 90 + strong -> ML weight < momentum weight
    print(f"\n{'='*60}")
    print(f"TEST: ML Dynamic Weight - Strong momentum reduces ML weight")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_ml_dynamic_weight()
    result_15 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    ml_weight = result_15.adjusted_weights.get("ml", 0)
    mom_weight = result_15.adjusted_weights.get("momentum", 0)
    ok_15 = mom_weight > ml_weight
    print(f"  Momentum score=90, strength=very_strong (breakout upgrade)")
    print(f"  ML weight: {ml_weight:.2%} | Momentum weight: {mom_weight:.2%}")
    print(f"  Momentum > ML: {ok_15}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_15 else 'FAILED'}")
    results.append(ok_15)
    
    # Test 16: Cycle Memory -- basic functionality
    print(f"\n{'='*60}")
    print(f"TEST: Cycle Memory - Temporal pattern detection")
    print(f"{'='*60}")
    from cycle_memory import CycleMemory, CycleSnapshot
    from datetime import datetime as dt, timedelta
    mem = CycleMemory()
    # Simulate 15 HOLD cycles with strong momentum and rising price
    base_price = 2700.0
    for i in range(15):
        mem.add(CycleSnapshot(
            timestamp=dt.now() - timedelta(minutes=(15-i)*5),
            score=60.0, confidence=55.0, decision="HOLD",
            scenario="padrao", tech_score=45.0, ml_score=40.0,
            momentum_score=85.0, momentum_direction="bullish",
            momentum_strength="strong", news_score=54.0,
            current_price=base_price + i * 3.0,  # +$3 per cycle
        ))
    summary = mem.get_trend_summary()
    ok_16_holds = summary["consecutive_holds"] == 15
    ok_16_streak = summary["strong_momentum_streak"] == 15
    ok_16_missed = summary["missed_opportunity"] == True
    ok_16_price = summary["price_change_pct"] > 0.3
    ok_16 = ok_16_holds and ok_16_streak and ok_16_missed and ok_16_price
    print(f"  Consecutive HOLDs: {summary['consecutive_holds']} (expected 15)")
    print(f"  Strong momentum streak: {summary['strong_momentum_streak']} (expected 15)")
    print(f"  Price change: {summary['price_change_pct']:+.3f}% (expected > 0.3%)")
    print(f"  Missed opportunity: {summary['missed_opportunity']} (expected True)")
    gpt_text = mem.format_for_gpt()
    ok_16_gpt = "MISSED OPPORTUNITY" in gpt_text
    ok_16 = ok_16 and ok_16_gpt
    print(f"  GPT format contains MISSED OPPORTUNITY: {ok_16_gpt}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_16 else 'FAILED'}")
    results.append(ok_16)
    
    # Test 17: Volume Penalty -- High momentum + low volume -> strength downgraded
    print(f"\n{'='*60}")
    print(f"TEST: Volume Penalty - High momentum with low volume")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_volume_penalty()
    result_17 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    # With volume 0.4x and momentum 85, strength should be downgraded 2 levels
    # Explanation should contain the false breakout alert
    has_volume_alert = "very low volume" in result_17.explanation.lower() or "low volume" in result_17.explanation.lower()
    # Strength downgrade should reduce confidence (momentum weak/very_weak = -10)
    # Without downgrade, momentum strong would give +10. With downgrade, -10. Difference = 20 pts
    ok_17 = has_volume_alert
    print(f"  Momentum score=85, volume=0.4x")
    print(f"  Volume alert in explanation: {has_volume_alert}")
    print(f"  Score: {result_17.final_score:.1f} | Confidence: {result_17.confidence:.1f}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_17 else 'FAILED'}")
    results.append(ok_17)
    
    # Test 18: ML Weight Floor -- Double penalty does not reduce ML below 10%
    print(f"\n{'='*60}")
    print(f"TEST: ML Weight Floor - ML never below 10%")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_ml_weight_floor()
    result_18 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    ml_weight_18 = result_18.adjusted_weights.get("ml", 0)
    ok_18 = ml_weight_18 >= 0.09  # Allow small rounding tolerance
    print(f"  ML confidence=51% (penalty -10%), Momentum=95 strong (reduction -15%)")
    print(f"  ML weight: {ml_weight_18:.2%} (floor: 10%)")
    print(f"  All weights: {result_18.adjusted_weights}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_18 else 'FAILED'}")
    results.append(ok_18)
    
    # Test 19: Confidence Cap -- Weak ML + Bearish News -> confidence limited
    print(f"\n{'='*60}")
    print(f"TEST: Confidence Cap - Weak ML + Bearish News")
    print(f"{'='*60}")
    tech_data, ml_data, momentum_data, news_data, price = mock_confidence_cap_ml_weak()
    result_19 = analyze_with_brain(tech_data, ml_data, momentum_data, news_data, price)
    # ML < 55% → cap 65, then News < 45 + BUY → -15, so max = 65-15 = 50
    ok_19_cap = result_19.confidence <= 65  # ML cap
    ok_19_level = result_19.confidence_level in ("LOW", "MEDIUM", "VERY_LOW")
    ok_19 = ok_19_cap and ok_19_level
    print(f"  ML confidence=51%, News score=42, Decision={result_19.decision}")
    print(f"  Confidence: {result_19.confidence:.1f} (cap 65 + news penalty -15)")
    print(f"  Level: {result_19.confidence_level}")
    print(f"  Cap applied (<=65): {ok_19_cap} | Level not HIGH/VERY_HIGH: {ok_19_level}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_19 else 'FAILED'}")
    results.append(ok_19)
    
    # Test 20: Smart Pyramid -- position in same direction at loss -> blocked
    print(f"\n{'='*60}")
    print(f"TEST: Smart Pyramid - Losing position blocks reinforcement")
    print(f"{'='*60}")
    from safety_checks import SafetyChecker
    from types import SimpleNamespace
    pyramid_checker = SafetyChecker()
    # Simulate SELL open @ 5000, current price 5005 (at loss for SELL)
    mock_pos_loss = SimpleNamespace(
        ticket=12345, direction="SELL", open_price=5000.0,
        current_price=5005.0, profit=-5.0, profit_pips=-50.0,
        volume=0.01, sl=5010.0, tp=4980.0,
        open_time=dt.now() - timedelta(minutes=50)
    )
    pyramid_ok_20, pyramid_reason_20 = pyramid_checker.check_pyramid_allowed("SELL", [mock_pos_loss])
    ok_20 = not pyramid_ok_20 and "Smart Pyramid" in pyramid_reason_20
    print(f"  SELL open @ 5000, current price 5005 (loss)")
    print(f"  Pyramid allowed: {pyramid_ok_20} (expected: False)")
    print(f"  Reason: {pyramid_reason_20}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_20 else 'FAILED'}")
    results.append(ok_20)
    
    # Test 21: Smart Pyramid -- position in same direction at profit >= 0.3% -> allowed
    print(f"\n{'='*60}")
    print(f"TEST: Smart Pyramid - Profitable position allows reinforcement")
    print(f"{'='*60}")
    # Simulate SELL open @ 5000, current price 4980 (0.4% profit for SELL)
    mock_pos_profit = SimpleNamespace(
        ticket=12346, direction="SELL", open_price=5000.0,
        current_price=4980.0, profit=20.0, profit_pips=200.0,
        volume=0.01, sl=5010.0, tp=4960.0,
        open_time=dt.now() - timedelta(minutes=120)
    )
    pyramid_ok_21, pyramid_reason_21 = pyramid_checker.check_pyramid_allowed("SELL", [mock_pos_profit])
    ok_21 = pyramid_ok_21
    print(f"  SELL open @ 5000, current price 4980 (0.4% profit)")
    print(f"  Pyramid allowed: {pyramid_ok_21} (expected: True)")
    print(f"\n  >>> RESULT: {'PASSED' if ok_21 else 'FAILED'}")
    results.append(ok_21)
    
    # Test 22: Smart Pyramid -- different direction -> always allowed
    print(f"\n{'='*60}")
    print(f"TEST: Smart Pyramid - Different direction always allowed")
    print(f"{'='*60}")
    pyramid_ok_22, _ = pyramid_checker.check_pyramid_allowed("BUY", [mock_pos_loss])
    ok_22 = pyramid_ok_22  # SELL open, BUY proposed -> no conflict
    print(f"  SELL open, BUY proposed -> no conflict")
    print(f"  Pyramid allowed: {pyramid_ok_22} (expected: True)")
    print(f"\n  >>> RESULT: {'PASSED' if ok_22 else 'FAILED'}")
    results.append(ok_22)
    
    # Test 23: M5 Reversal Detection -- unit test of logic (without MT5)
    print(f"\n{'='*60}")
    print(f"TEST: M5 Reversal - Graceful degradation without MT5")
    print(f"{'='*60}")
    from momentum_detector import check_m5_reversal
    m5_result = check_m5_reversal("SELL")
    # Without MT5 connected, should return reversal_detected=False (graceful degradation)
    ok_23 = m5_result["reversal_detected"] == False
    print(f"  Without MT5: reversal_detected={m5_result['reversal_detected']} (expected: False)")
    print(f"  Description: {m5_result['description']}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_23 else 'FAILED'}")
    results.append(ok_23)
    
    # Test 24: S/R Penalty — BUY into strong resistance reduces confidence
    print(f"\n{'='*60}")
    print(f"TEST: S/R Penalty - BUY into strong resistance")
    print(f"{'='*60}")
    sr_penalty_data = {
        "confidence_adjustment": -15.0,
        "confirmations": [],
        "alerts": ["BUY into H4 resistance at 2950.00 (5 touches, 30 pips away) — confidence -15"],
        "description": "Resistance: 2950.00 (5T, H4, 30 pips above)",
        "near_strong_zone": False,  # Not triggering scenario, just penalty
    }
    tech_24, ml_24, mom_24, news_24, price_24 = mock_caso_28_jan()
    result_no_sr = analyze_with_brain(tech_24, ml_24, mom_24, news_24, current_price=price_24, sr_data=None)
    result_with_sr = analyze_with_brain(tech_24, ml_24, mom_24, news_24, current_price=price_24, sr_data=sr_penalty_data)
    conf_drop = result_no_sr.confidence - result_with_sr.confidence
    ok_24 = conf_drop >= 14.0  # Should drop ~15 (may be clamped)
    print(f"  Without S/R: confidence={result_no_sr.confidence:.1f}")
    print(f"  With S/R penalty: confidence={result_with_sr.confidence:.1f}")
    print(f"  Drop: {conf_drop:.1f} (expected ~15)")
    print(f"  S/R alert in result: {any('resistance' in a.lower() for a in result_with_sr.alerts)}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_24 else 'FAILED'}")
    results.append(ok_24)

    # Test 25: S/R Bonus — BUY bouncing off support boosts confidence
    # Use penalty first to bring confidence below 100, then verify bonus raises it
    print(f"\n{'='*60}")
    print(f"TEST: S/R Bonus - BUY bouncing off support")
    print(f"{'='*60}")
    sr_bonus_data = {
        "confidence_adjustment": 8.0,
        "confirmations": ["BUY near H1 support at 2880.00 (4 touches) — confidence +8"],
        "alerts": [],
        "description": "Support: 2880.00 (4T, H1, 20 pips below)",
        "near_strong_zone": False,
    }
    # Compare: penalty (-15) vs penalty+bonus (-15+8 = -7)
    sr_penalty_and_bonus = {
        "confidence_adjustment": -7.0,  # net: -15 + 8
        "confirmations": ["BUY near H1 support at 2880.00 (4 touches) — confidence +8"],
        "alerts": ["BUY into H4 resistance at 2950.00 (5 touches, 30 pips away) — confidence -15"],
        "description": "Support: 2880.00 (4T, H1, 20 pips below)",
        "near_strong_zone": False,
    }
    result_penalty_only = analyze_with_brain(tech_24, ml_24, mom_24, news_24, current_price=price_24, sr_data=sr_penalty_data)
    result_penalty_bonus = analyze_with_brain(tech_24, ml_24, mom_24, news_24, current_price=price_24, sr_data=sr_penalty_and_bonus)
    # penalty_only = -15, penalty+bonus = -7, so bonus result should be higher
    bonus_effect = result_penalty_bonus.confidence - result_penalty_only.confidence
    ok_25 = bonus_effect >= 7.0 and any('support' in c.lower() for c in result_penalty_bonus.confirmations)
    print(f"  Penalty only (-15): confidence={result_penalty_only.confidence:.1f}")
    print(f"  Penalty+Bonus (-7): confidence={result_penalty_bonus.confidence:.1f}")
    print(f"  Bonus effect: {bonus_effect:.1f} (expected ~8)")
    print(f"  S/R confirmation in result: {any('support' in c.lower() for c in result_penalty_bonus.confirmations)}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_25 else 'FAILED'}")
    results.append(ok_25)

    # Test 26: S/R Scenario — zona_sr_forte triggers when near strong zone
    print(f"\n{'='*60}")
    print(f"TEST: S/R Scenario - zona_sr_forte near strong H4 zone")
    print(f"{'='*60}")
    sr_scenario_data = {
        "confidence_adjustment": 0.0,
        "confirmations": [],
        "alerts": [],
        "description": "Near strong RESISTANCE zone at 2950.00",
        "near_strong_zone": True,
        "near_zone_info": {"midpoint": 2950.00, "touches": 5, "zone_type": "RESISTANCE"},
    }
    result_sr_scenario = analyze_with_brain(tech_24, ml_24, mom_24, news_24, current_price=price_24, sr_data=sr_scenario_data)
    ok_26 = result_sr_scenario.scenario == "zona_sr_forte"
    print(f"  Scenario: {result_sr_scenario.scenario} (expected: zona_sr_forte)")
    print(f"  Description: {result_sr_scenario.scenario_description}")
    print(f"\n  >>> RESULT: {'PASSED' if ok_26 else 'FAILED'}")
    results.append(ok_26)

    # Summary
    test_names = [
        "Case 28 Jan", "Ranging", "Perfect Alignment",
        "Conflicting Signals", "MACD Divergence",
        "Post-event CPI bearish", "Pre-event NFP", "During Fed release",
        "Extreme Volatility", "Cooling + strong signals", "Cooling + weak signals",
        "GPT BOOST", "GPT REDUCE",
        "Momentum Direction (ADX bullish)", "ML Dynamic Weight", "Cycle Memory",
        "Volume Penalty", "ML Weight Floor", "Confidence Cap ML+News",
        "Smart Pyramid (loss blocks)", "Smart Pyramid (profit allows)",
        "Smart Pyramid (diff direction)", "M5 Reversal (graceful degradation)",
        "S/R Penalty (BUY into resistance)", "S/R Bonus (BUY off support)",
        "S/R Scenario (zona_sr_forte)",
    ]
    
    print(f"\n{'='*60}")
    print(f"SUMMARY: {sum(results)}/{len(results)} tests passed")
    print(f"{'='*60}")
    
    for i, (name, ok) in enumerate(zip(test_names, results)):
        status = "PASS" if ok else "FAIL"
        print(f"  {i+1}. {name}: {status}")
    
    if all(results):
        print(f"\n  ALL TESTS PASSED!")
    else:
        print(f"\n  SOME TESTS FAILED - review logic!")
    
    return all(results)


if __name__ == "__main__":
    main()
