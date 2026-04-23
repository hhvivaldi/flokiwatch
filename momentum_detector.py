"""
MOMENTUM DETECTOR - Market Impulse Detection
Analyzes momentum strength and direction using ADX, Volume, Consecutive Candles, ATR and Breakouts.

Score:
- 100 = Very strong momentum (directional impulse)
- 50 = Neutral (no clear momentum)
- 0 = Very weak momentum (sideways market)
"""

import numpy as np
import pandas as pd
from typing import Dict, Optional


# ============================================================================
# ADX (Average Directional Index)
# ============================================================================

def calculate_adx(df: pd.DataFrame, period: int = 14) -> Dict:
    """
    Calculate ADX (Average Directional Index).
    
    ADX measures trend STRENGTH (not direction).
    - ADX > 40: very strong trend
    - ADX > 30: strong trend
    - ADX > 25: moderate trend
    - ADX < 20: sideways/no direction
    
    Returns:
        Dict with adx_value, adx_classification, plus_di, minus_di
    """
    if df is None or len(df) < period * 2:
        return {
            "adx_value": 0,
            "adx_classification": "weak",
            "plus_di": 0,
            "minus_di": 0,
        }
    
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    
    # True Range
    tr = np.zeros(len(df))
    tr[0] = high[0] - low[0]
    for i in range(1, len(df)):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1])
        )
    
    # +DM e -DM
    plus_dm = np.zeros(len(df))
    minus_dm = np.zeros(len(df))
    for i in range(1, len(df)):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
    
    # Smoothed TR, +DM, -DM (Wilder's smoothing)
    smoothed_tr = np.zeros(len(df))
    smoothed_plus_dm = np.zeros(len(df))
    smoothed_minus_dm = np.zeros(len(df))
    
    # First sum
    smoothed_tr[period] = np.sum(tr[1:period + 1])
    smoothed_plus_dm[period] = np.sum(plus_dm[1:period + 1])
    smoothed_minus_dm[period] = np.sum(minus_dm[1:period + 1])
    
    # Wilder's smoothing
    for i in range(period + 1, len(df)):
        smoothed_tr[i] = smoothed_tr[i - 1] - (smoothed_tr[i - 1] / period) + tr[i]
        smoothed_plus_dm[i] = smoothed_plus_dm[i - 1] - (smoothed_plus_dm[i - 1] / period) + plus_dm[i]
        smoothed_minus_dm[i] = smoothed_minus_dm[i - 1] - (smoothed_minus_dm[i - 1] / period) + minus_dm[i]
    
    # +DI e -DI
    plus_di = np.zeros(len(df))
    minus_di = np.zeros(len(df))
    dx = np.zeros(len(df))
    
    for i in range(period, len(df)):
        if smoothed_tr[i] > 0:
            plus_di[i] = (smoothed_plus_dm[i] / smoothed_tr[i]) * 100
            minus_di[i] = (smoothed_minus_dm[i] / smoothed_tr[i]) * 100
        
        di_sum = plus_di[i] + minus_di[i]
        if di_sum > 0:
            dx[i] = (abs(plus_di[i] - minus_di[i]) / di_sum) * 100
    
    # ADX = smoothed average of DX
    adx = np.zeros(len(df))
    start_idx = period * 2
    if start_idx < len(df):
        adx[start_idx] = np.mean(dx[period:start_idx + 1])
        for i in range(start_idx + 1, len(df)):
            adx[i] = ((adx[i - 1] * (period - 1)) + dx[i]) / period
    
    # Current value
    adx_value = float(adx[-1])
    plus_di_value = float(plus_di[-1])
    minus_di_value = float(minus_di[-1])
    
    # Classification
    if adx_value >= 40:
        classification = "very_strong"
    elif adx_value >= 30:
        classification = "strong"
    elif adx_value >= 25:
        classification = "moderate"
    elif adx_value >= 20:
        classification = "weak"
    else:
        classification = "very_weak"
    
    return {
        "adx_value": round(adx_value, 2),
        "adx_classification": classification,
        "plus_di": round(plus_di_value, 2),
        "minus_di": round(minus_di_value, 2),
    }


# ============================================================================
# VOLUME ANALYSIS
# ============================================================================

def analyze_volume(df: pd.DataFrame, period: int = 20) -> Dict:
    """
    Analyze volume compared to average.
    
    - Volume > 1.5x average: very strong movement
    - Volume > 1.2x average: strong movement
    - Volume < 0.8x average: weak movement
    
    Returns:
        Dict with volume_ratio, volume_trend, volume_classification
    """
    if df is None or len(df) < period:
        return {
            "volume_ratio": 1.0,
            "volume_classification": "normal",
            "volume_trend": "stable",
        }
    
    volumes = df['volume'].values
    current_volume = float(volumes[-1])
    if len(volumes) >= period + 1:
        avg_volume = float(np.mean(volumes[-period-1:-1]))
    else:
        avg_volume = float(np.mean(volumes[-period:]))
    
    if avg_volume <= 0:
        return {
            "volume_ratio": 1.0,
            "volume_classification": "normal",
            "volume_trend": "stable",
        }
    
    ratio = current_volume / avg_volume
    
    # Classification
    if ratio >= 1.5:
        classification = "explosive"
    elif ratio >= 1.2:
        classification = "high"
    elif ratio >= 0.8:
        classification = "normal"
    else:
        classification = "low"
    
    # Volume trend (last 5 periods vs previous 5)
    if len(volumes) >= 10:
        recent_avg = np.mean(volumes[-5:])
        previous_avg = np.mean(volumes[-10:-5])
        if previous_avg > 0:
            vol_change = (recent_avg - previous_avg) / previous_avg
            if vol_change > 0.1:
                trend = "increasing"
            elif vol_change < -0.1:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "stable"
    else:
        trend = "stable"
    
    return {
        "volume_ratio": round(ratio, 2),
        "volume_classification": classification,
        "volume_trend": trend,
    }


# ============================================================================
# CONSECUTIVE CANDLES
# ============================================================================

def count_consecutive_candles(df: pd.DataFrame) -> Dict:
    """
    Count consecutive candles of the same color.
    
    - 5+ candles same color: very strong impulse
    - 3-4 candles: moderate impulse
    
    Returns:
        Dict with count, direction
    """
    if df is None or len(df) < 2:
        return {
            "consecutive_count": 0,
            "consecutive_direction": "neutral",
        }
    
    # Determine color of each candle
    closes = df['close'].values
    opens = df['open'].values
    
    # Count from end backwards
    if closes[-1] > opens[-1]:
        last_direction = "bullish"
    elif closes[-1] < opens[-1]:
        last_direction = "bearish"
    else:
        last_direction = "neutral"

    if last_direction == "neutral":
        count = 0
    else:
        count = 1
        for i in range(len(df) - 2, -1, -1):
            if closes[i] > opens[i]:
                current_direction = "bullish"
            elif closes[i] < opens[i]:
                current_direction = "bearish"
            else:
                current_direction = "neutral"
            if current_direction == last_direction:
                count += 1
            else:
                break
    
    return {
        "consecutive_count": count,
        "consecutive_direction": last_direction,
    }


# ============================================================================
# ATR TREND
# ============================================================================

def analyze_atr_trend(df: pd.DataFrame, period: int = 14) -> Dict:
    """
    Analyze ATR trend (volatility).
    
    - ATR increasing: market accelerating
    - ATR decreasing: market calming
    
    Returns:
        Dict with atr_current, atr_trend
    """
    if df is None or len(df) < period + 5:
        return {
            "atr_current": 0,
            "atr_trend": "stable",
        }
    
    # Use already calculated ATR if available
    if 'atr_14' in df.columns:
        atr_values = df['atr_14'].values
    else:
        # Calculate ATR
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        tr = np.zeros(len(df))
        tr[0] = high[0] - low[0]
        for i in range(1, len(df)):
            tr[i] = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1])
            )
        
        atr_values = np.zeros(len(df))
        atr_values[period - 1] = np.mean(tr[:period])
        for i in range(period, len(df)):
            atr_values[i] = ((atr_values[i - 1] * (period - 1)) + tr[i]) / period
    
    current_atr = float(atr_values[-1])
    
    # Trend: compare recent ATR vs previous
    if len(atr_values) >= 10:
        recent_atr = np.mean(atr_values[-3:])
        previous_atr = np.mean(atr_values[-8:-3])
        
        if previous_atr > 0:
            atr_change = (recent_atr - previous_atr) / previous_atr
            if atr_change > 0.05:
                trend = "increasing"
            elif atr_change < -0.05:
                trend = "decreasing"
            else:
                trend = "stable"
        else:
            trend = "stable"
    else:
        trend = "stable"
    
    return {
        "atr_current": round(current_atr, 4),
        "atr_trend": trend,
    }


# ============================================================================
# BREAKOUT DETECTION
# ============================================================================

def detect_breakout(df: pd.DataFrame, lookback: int = 20) -> Dict:
    """
    Detect if price broke recent high/low.
    
    Breakout + high volume = strong signal
    
    Returns:
        Dict with breakout_detected, breakout_type
    """
    if df is None or len(df) < lookback + 1:
        return {
            "breakout_detected": False,
            "breakout_type": None,
        }
    
    current_close = float(df['close'].iloc[-1])
    current_high = float(df['high'].iloc[-1])
    current_low = float(df['low'].iloc[-1])
    
    # High and low of last N periods (excluding current candle)
    recent_high = float(df['high'].iloc[-lookback - 1:-1].max())
    recent_low = float(df['low'].iloc[-lookback - 1:-1].min())
    
    breakout_detected = False
    breakout_type = None
    
    # Resistance breakout (upward)
    if current_close > recent_high or current_high > recent_high:
        breakout_detected = True
        breakout_type = "resistance"
    
    # Support breakout (downward)
    elif current_close < recent_low or current_low < recent_low:
        breakout_detected = True
        breakout_type = "support"
    
    return {
        "breakout_detected": breakout_detected,
        "breakout_type": breakout_type,
    }


# ============================================================================
# MOMENTUM SCORE
# ============================================================================

def calculate_momentum_score(adx_data: Dict, volume_data: Dict, candles_data: Dict,
                              atr_data: Dict, breakout_data: Dict) -> float:
    """
    Calculate momentum score (0-100).
    
    Base: 50 (neutral)
    Adds/subtracts points based on components.
    """
    score = 50.0
    
    # ADX
    adx = adx_data["adx_value"]
    if adx >= 40:
        score += 30
    elif adx >= 30:
        score += 20
    elif adx >= 25:
        score += 10
    elif adx < 20:
        score -= 10
    
    # Volume
    ratio = volume_data["volume_ratio"]
    if ratio >= 1.5:
        score += 20
    elif ratio >= 1.2:
        score += 10
    elif ratio < 0.8:
        score -= 10
    
    # Consecutive candles
    count = candles_data["consecutive_count"]
    if count >= 5:
        score += 20
    elif count >= 3:
        score += 10
    
    # ATR trend
    if atr_data["atr_trend"] == "increasing":
        score += 15
    elif atr_data["atr_trend"] == "decreasing":
        score -= 5
    
    # Breakout
    if breakout_data["breakout_detected"]:
        score += 15
    
    # Clamp between 0-100
    return round(max(0, min(100, score)), 2)


# ============================================================================
# MAIN FUNCTION
# ============================================================================

def analyze_momentum(df: pd.DataFrame) -> Dict:
    """
    Complete momentum analysis.
    
    Args:
        df: DataFrame with OHLCV data (and optionally pre-calculated indicators)
    
    Returns:
        Dict with all detailed momentum data + score
    """
    if df is None or len(df) < 30:
        return {
            "score": 50.0,
            "adx": {"adx_value": 0, "adx_classification": "weak", "plus_di": 0, "minus_di": 0},
            "volume": {"volume_ratio": 1.0, "volume_classification": "normal", "volume_trend": "stable"},
            "candles": {"consecutive_count": 0, "consecutive_direction": "neutral"},
            "atr": {"atr_current": 0, "atr_trend": "stable"},
            "breakout": {"breakout_detected": False, "breakout_type": None},
            "error": "Insufficient data for momentum analysis",
        }
    
    # Calculate each component
    adx_data = calculate_adx(df)
    volume_data = analyze_volume(df)
    candles_data = count_consecutive_candles(df)
    atr_data = analyze_atr_trend(df)
    breakout_data = detect_breakout(df)
    
    # Calculate score
    score = calculate_momentum_score(adx_data, volume_data, candles_data, atr_data, breakout_data)
    
    return {
        "score": score,
        "adx": adx_data,
        "volume": volume_data,
        "candles": candles_data,
        "atr": atr_data,
        "breakout": breakout_data,
        "error": None,
    }


# ============================================================================
# M5 REVERSAL DETECTION (anti-lag filter)
# ============================================================================

def get_m5_status() -> Dict:
    """
    Return current M5 candle state for logging (no direction).
    
    Lightweight: only move_pct and green/red count.
    Called each analysis cycle for visibility.
    
    Returns:
        Dict with move_pct, green_count, red_count, description
    """
    import config as _cfg
    
    default_result = {
        "move_pct": 0.0,
        "green_count": 0,
        "red_count": 0,
        "description": "M5 unavailable",
    }
    
    try:
        from mt5_safe import mt5  # FLO-348
        
        n_candles = getattr(_cfg, 'M5_REVERSAL_CANDLES', 6)
        
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, n_candles + 1)
        
        if rates is None or len(rates) < n_candles:
            return default_result
        
        # Use only complete candles
        if len(rates) > n_candles:
            rates = rates[:-1]
        
        open_price = float(rates[0]['open'])
        close_price = float(rates[-1]['close'])
        
        if open_price <= 0:
            return default_result
        
        move_pct = ((close_price - open_price) / open_price) * 100
        
        green_count = 0
        red_count = 0
        for r in rates:
            if r['close'] > r['open']:
                green_count += 1
            elif r['close'] < r['open']:
                red_count += 1
        
        return {
            "move_pct": round(move_pct, 4),
            "green_count": green_count,
            "red_count": red_count,
            "description": f"{move_pct:+.2f}% ({green_count}g/{red_count}r)",
        }
        
    except Exception as e:
        default_result["description"] = f"M5 error: {e}"
        return default_result


def check_m5_reversal(direction: str) -> Dict:
    """
    Check if recent M5 candles contradict the proposed direction.
    
    Detects if price is already reversing on M5 while H1 indicators
    still reflect the previous movement (lag problem).
    
    Args:
        direction: "BUY" or "SELL"
    
    Returns:
        Dict with reversal_detected, reversal_strength, recent_move_pct, etc.
    """
    import config as _cfg
    
    default_result = {
        "reversal_detected": False,
        "reversal_strength": "none",
        "recent_move_pct": 0.0,
        "green_count": 0,
        "red_count": 0,
        "description": "M5 data unavailable — no filter",
    }
    
    try:
        from mt5_safe import mt5  # FLO-348
        
        n_candles = getattr(_cfg, 'M5_REVERSAL_CANDLES', 6)
        moderate_threshold = getattr(_cfg, 'M5_REVERSAL_MODERATE_THRESHOLD', 0.20)
        strong_threshold = getattr(_cfg, 'M5_REVERSAL_STRONG_THRESHOLD', 0.40)
        
        # Fetch last N+1 M5 candles (N+1 to have N complete + current incomplete)
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, n_candles + 1)
        
        if rates is None or len(rates) < n_candles:
            return default_result
        
        # Use only complete candles (exclude last which may be incomplete)
        # If we have n_candles+1, use first n_candles (all complete)
        if len(rates) > n_candles:
            rates = rates[:-1]  # Remove incomplete candle
        
        # Calculate move % in last N candles
        open_price = float(rates[0]['open'])
        close_price = float(rates[-1]['close'])
        
        if open_price <= 0:
            return default_result
        
        move_pct = ((close_price - open_price) / open_price) * 100
        
        # Count green/red candles
        green_count = 0
        red_count = 0
        for r in rates:
            if r['close'] > r['open']:
                green_count += 1
            elif r['close'] < r['open']:
                red_count += 1
        
        # Check if move contradicts proposed direction
        direction = direction.upper()
        reversal_detected = False
        reversal_strength = "none"
        
        if direction == "SELL" and move_pct > 0:
            # Want to SELL but price is RISING on M5
            if move_pct >= strong_threshold:
                reversal_detected = True
                reversal_strength = "strong"
            elif move_pct >= moderate_threshold:
                reversal_detected = True
                reversal_strength = "moderate"
        elif direction == "BUY" and move_pct < 0:
            # Want to BUY but price is FALLING on M5
            abs_move = abs(move_pct)
            if abs_move >= strong_threshold:
                reversal_detected = True
                reversal_strength = "strong"
            elif abs_move >= moderate_threshold:
                reversal_detected = True
                reversal_strength = "moderate"
        
        # Build description
        if reversal_detected:
            contra_dir = "rising" if direction == "SELL" else "falling"
            description = (
                f"M5 reversal {reversal_strength}: price {contra_dir} {abs(move_pct):.2f}% "
                f"in last {n_candles} M5 candles ({green_count}G/{red_count}R) — "
                f"contradicts {direction}"
            )
        else:
            description = (
                f"M5 OK: move {move_pct:+.2f}% in last {n_candles} M5 candles "
                f"({green_count}G/{red_count}R) — compatible with {direction}"
            )
        
        return {
            "reversal_detected": reversal_detected,
            "reversal_strength": reversal_strength,
            "recent_move_pct": round(move_pct, 4),
            "green_count": green_count,
            "red_count": red_count,
            "description": description,
        }
        
    except Exception as e:
        default_result["description"] = f"M5 reversal check error: {e} — no filter"
        return default_result


# ============================================================================
# TEST
# ============================================================================

def test_momentum_detector():
    """Test the momentum detector"""
    print("=" * 60)
    print("🧪 MOMENTUM DETECTOR TEST")
    print("=" * 60)
    
    from mt5_safe import mt5  # FLO-348
    
    use_simulated = False
    df = None
    
    if mt5.initialize():
        print("✅ MT5 connected")
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, 100)
        mt5.shutdown()
        
        if rates is not None and len(rates) > 0:
            df = pd.DataFrame(rates)
            df['datetime'] = pd.to_datetime(df['time'], unit='s')
            df = df.rename(columns={'tick_volume': 'volume'})
        else:
            use_simulated = True
    else:
        use_simulated = True
    
    if use_simulated:
        print("⚠️ Using simulated data")
        np.random.seed(42)
        n = 100
        base_price = 2650
        prices = base_price + np.cumsum(np.random.randn(n) * 2)
        
        df = pd.DataFrame({
            'datetime': pd.date_range(end=pd.Timestamp.now(), periods=n, freq='H'),
            'open': prices - np.random.rand(n),
            'high': prices + np.random.rand(n) * 3,
            'low': prices - np.random.rand(n) * 3,
            'close': prices,
            'volume': np.random.randint(1000, 5000, n),
        })
    
    print(f"\n📊 Data: {len(df)} bars")
    
    # Analyze momentum
    result = analyze_momentum(df)
    
    print(f"\n🚀 MOMENTUM SCORE: {result['score']}/100")
    
    print(f"\n📈 ADX:")
    print(f"   Value: {result['adx']['adx_value']}")
    print(f"   Classification: {result['adx']['adx_classification']}")
    print(f"   +DI: {result['adx']['plus_di']} | -DI: {result['adx']['minus_di']}")
    
    print(f"\n📊 Volume:")
    print(f"   Ratio: {result['volume']['volume_ratio']}x average")
    print(f"   Classification: {result['volume']['volume_classification']}")
    print(f"   Trend: {result['volume']['volume_trend']}")
    
    print(f"\n🕯️ Consecutive Candles:")
    print(f"   Count: {result['candles']['consecutive_count']}")
    print(f"   Direction: {result['candles']['consecutive_direction']}")
    
    print(f"\n📏 ATR:")
    print(f"   Current: {result['atr']['atr_current']}")
    print(f"   Trend: {result['atr']['atr_trend']}")
    
    print(f"\n💥 Breakout:")
    print(f"   Detected: {'Yes' if result['breakout']['breakout_detected'] else 'No'}")
    if result['breakout']['breakout_type']:
        print(f"   Type: {result['breakout']['breakout_type']}")
    
    print(f"\n✅ Test complete!")


if __name__ == "__main__":
    test_momentum_detector()
