"""
TECHNICAL ANALYZER - Technical Analysis
Calculates technical score based on indicators
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
import MetaTrader5 as mt5
import config


def get_mt5_data(symbol: str = None, timeframe: str = None, bars: int = None) -> Optional[pd.DataFrame]:
    """
    Get data from MT5.
    
    Args:
        symbol: Symbol (default: config.SYMBOL)
        timeframe: Timeframe (default: config.TIMEFRAME)
        bars: Number of bars (default: config.ANALYSIS_BARS)
    
    Returns:
        DataFrame with OHLCV or None if error
    """
    symbol = symbol or config.SYMBOL
    bars = bars or config.ANALYSIS_BARS
    
    # Map timeframe
    tf_map = {
        'M1': mt5.TIMEFRAME_M1,
        'M5': mt5.TIMEFRAME_M5,
        'M15': mt5.TIMEFRAME_M15,
        'M30': mt5.TIMEFRAME_M30,
        'H1': mt5.TIMEFRAME_H1,
        'H4': mt5.TIMEFRAME_H4,
        'D1': mt5.TIMEFRAME_D1,
    }
    tf = tf_map.get(timeframe or config.TIMEFRAME, mt5.TIMEFRAME_H1)
    
    # Obter dados
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
    
    if rates is None or len(rates) == 0:
        return None
    
    df = pd.DataFrame(rates)
    df['datetime'] = pd.to_datetime(df['time'], unit='s')
    df = df.rename(columns={'tick_volume': 'volume'})
    
    return df


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate technical indicators"""
    df = df.copy()
    
    # EMAs
    df['ema_9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi_14'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema_12 = df['close'].ewm(span=12, adjust=False).mean()
    ema_26 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = ema_12 - ema_26
    df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']
    
    # Bollinger Bands
    df['bb_middle'] = df['close'].rolling(window=20).mean()
    bb_std = df['close'].rolling(window=20).std()
    df['bb_upper'] = df['bb_middle'] + (bb_std * 2)
    df['bb_lower'] = df['bb_middle'] - (bb_std * 2)
    
    # ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr_14'] = tr.rolling(window=14).mean()
    
    # Stochastic
    low_14 = df['low'].rolling(window=14).min()
    high_14 = df['high'].rolling(window=14).max()
    df['stoch_k'] = 100 * (df['close'] - low_14) / (high_14 - low_14)
    df['stoch_d'] = df['stoch_k'].rolling(window=3).mean()
    
    return df


def calculate_visual_features(df: pd.DataFrame) -> Dict:
    """
    Calculate visual context features that a human trader would see on the chart.
    
    Features:
        1. consecutive_candles_H1: consecutive candles in the same direction
        2. body_size_trend_H1: bodies growing (momentum) vs shrinking (exhaustion)
        3. price_vs_range_20H1: price position in range of last 20 candles (0-1)
        4. engulfing_pattern: last candle engulfed the previous one (reversal)
        5. pin_bar: long shadow vs small body (price rejection)
    
    Returns:
        Dict with raw features, individual adjustments, and total adjustment (capped +/-8)
    """
    VISUAL_CAP = 8  # Cap total ±8 pontos
    
    result = {
        "consecutive_candles": 0,
        "body_size_trend": "neutral",
        "price_vs_range": 0.5,
        "engulfing": None,
        "pin_bar": None,
        "adjustments": {},
        "total_adjustment": 0.0,
    }
    
    if df is None or len(df) < 20:
        return result
    
    total_adj = 0.0
    adjustments = {}
    
    # 1. Consecutive candles in same direction
    consecutive = 0
    last_dir = None
    for i in range(len(df) - 1, max(len(df) - 10, -1), -1):
        row = df.iloc[i]
        if row['close'] > row['open']:
            d = "bullish"
        elif row['close'] < row['open']:
            d = "bearish"
        else:
            break
        if last_dir is None:
            last_dir = d
            consecutive = 1
        elif d == last_dir:
            consecutive += 1
        else:
            break
    
    result["consecutive_candles"] = consecutive if last_dir == "bullish" else -consecutive
    
    if consecutive >= 4:
        adj = 5.0 if last_dir == "bullish" else -5.0
        adjustments["consecutive_candles"] = adj
        total_adj += adj
    elif consecutive == 3:
        adj = 3.0 if last_dir == "bullish" else -3.0
        adjustments["consecutive_candles"] = adj
        total_adj += adj
    
    # 2. Body size trend (last 5 candles)
    bodies = []
    for i in range(-5, 0):
        if abs(i) <= len(df):
            row = df.iloc[i]
            bodies.append(abs(row['close'] - row['open']))
    
    if len(bodies) >= 3:
        first_half = np.mean(bodies[:len(bodies)//2])
        second_half = np.mean(bodies[len(bodies)//2:])
        
        if first_half > 0:
            ratio = second_half / first_half
        else:
            ratio = 1.0
        
        last_row = df.iloc[-1]
        last_bullish = last_row['close'] > last_row['open']
        
        if ratio > 1.3:  # Bodies growing
            result["body_size_trend"] = "growing"
            adj = 3.0 if last_bullish else -3.0
            adjustments["body_size_trend"] = adj
            total_adj += adj
        elif ratio < 0.6:  # Bodies shrinking (exhaustion)
            result["body_size_trend"] = "shrinking"
            adj = -2.0 if last_bullish else 2.0  # Against current direction (exhaustion)
            adjustments["body_size_trend"] = adj
            total_adj += adj
        else:
            result["body_size_trend"] = "neutral"
    
    # 3. Price vs range of last 20 candles
    last_20 = df.tail(20)
    range_high = last_20['high'].max()
    range_low = last_20['low'].min()
    range_span = range_high - range_low
    
    if range_span > 0:
        price_pos = (df.iloc[-1]['close'] - range_low) / range_span
    else:
        price_pos = 0.5
    
    result["price_vs_range"] = round(price_pos, 4)
    
    if price_pos > 0.85:
        adjustments["price_vs_range"] = -3.0  # Near resistance
        total_adj -= 3.0
    elif price_pos < 0.15:
        adjustments["price_vs_range"] = 3.0  # Near support
        total_adj += 3.0
    
    # 4. Engulfing pattern
    last = df.iloc[-1]
    prev = df.iloc[-2]
    last_body = last['close'] - last['open']
    prev_body = prev['close'] - prev['open']
    
    # Bullish engulfing: prev bearish, last bullish, last body covers prev body
    if prev_body < 0 and last_body > 0:
        if last['close'] > prev['open'] and last['open'] < prev['close']:
            result["engulfing"] = "bullish"
            adjustments["engulfing"] = 4.0
            total_adj += 4.0
    # Bearish engulfing: prev bullish, last bearish, last body covers prev body
    elif prev_body > 0 and last_body < 0:
        if last['close'] < prev['open'] and last['open'] > prev['close']:
            result["engulfing"] = "bearish"
            adjustments["engulfing"] = -4.0
            total_adj -= 4.0
    
    # 5. Pin bar detection
    last_high = last['high']
    last_low = last['low']
    last_open = last['open']
    last_close = last['close']
    total_range = last_high - last_low
    body_size = abs(last_close - last_open)
    
    if total_range > 0 and body_size < total_range * 0.3:
        upper_shadow = last_high - max(last_open, last_close)
        lower_shadow = min(last_open, last_close) - last_low
        
        # Bullish pin bar: long lower shadow (rejection of lower prices)
        if lower_shadow > total_range * 0.6:
            result["pin_bar"] = "bullish"
            adjustments["pin_bar"] = 3.0
            total_adj += 3.0
        # Bearish pin bar: long upper shadow (rejection of higher prices)
        elif upper_shadow > total_range * 0.6:
            result["pin_bar"] = "bearish"
            adjustments["pin_bar"] = -3.0
            total_adj -= 3.0
    
    # Apply cap ±8
    capped_adj = max(-VISUAL_CAP, min(VISUAL_CAP, total_adj))
    
    result["adjustments"] = adjustments
    result["total_adjustment"] = round(capped_adj, 1)
    
    return result


def calculate_technical_score(df: pd.DataFrame) -> Tuple[float, Dict]:
    """
    Calculate technical score (0-100).
    
    Args:
        df: DataFrame with calculated indicators
    
    Returns:
        Tuple: (score, breakdown_dict)
    """
    if df is None or len(df) < 50:
        return 50.0, {'error': 'Insufficient data'}
    
    # Get last row
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    scores = {}
    
    # 1. Trend Score (EMAs) - 25 points
    trend_score = 0
    if last['close'] > last['ema_9']:
        trend_score += 5
    if last['close'] > last['ema_21']:
        trend_score += 5
    if last['close'] > last['ema_50']:
        trend_score += 5
    if last['ema_9'] > last['ema_21']:
        trend_score += 5
    if last['ema_21'] > last['ema_50']:
        trend_score += 5
    scores['trend'] = trend_score
    
    # 2. Momentum Score (RSI) - 20 points
    rsi = last['rsi_14']
    if 40 <= rsi <= 60:
        momentum_score = 10  # Neutral
    elif 30 <= rsi < 40:
        momentum_score = 15  # Mild oversold (bullish)
    elif rsi < 30:
        momentum_score = 20  # Strong oversold (very bullish)
    elif 60 < rsi <= 70:
        momentum_score = 5  # Mild overbought (bearish)
    else:
        momentum_score = 0  # Strong overbought (very bearish)
    scores['momentum'] = momentum_score
    
    # 3. MACD Score - 20 points
    macd_score = 0
    if last['macd'] > last['macd_signal']:
        macd_score += 10
    if last['macd_hist'] > 0:
        macd_score += 5
    if last['macd_hist'] > prev['macd_hist']:
        macd_score += 5
    scores['macd'] = macd_score
    
    # 4. Bollinger Score - 15 points
    bb_score = 0
    bb_range = last['bb_upper'] - last['bb_lower']
    if bb_range > 0:
        bb_position = (last['close'] - last['bb_lower']) / bb_range
        if 0.3 <= bb_position <= 0.7:
            bb_score = 7  # Middle of band
        elif bb_position < 0.2:
            bb_score = 15  # Near lower band (bullish)
        elif bb_position > 0.8:
            bb_score = 0  # Near upper band (bearish)
        else:
            bb_score = 10
    scores['bollinger'] = bb_score
    
    # 5. Stochastic Score - 10 points
    stoch_score = 0
    stoch_k = last['stoch_k']
    if stoch_k < 20:
        stoch_score = 10  # Oversold
    elif stoch_k < 30:
        stoch_score = 7
    elif stoch_k > 80:
        stoch_score = 0  # Overbought
    elif stoch_k > 70:
        stoch_score = 3
    else:
        stoch_score = 5  # Neutral
    scores['stochastic'] = stoch_score
    
    # 6. Price Action Score - 10 points
    pa_score = 0
    # Current candle bullish?
    if last['close'] > last['open']:
        pa_score += 5
    # Higher high?
    if last['high'] > prev['high']:
        pa_score += 2.5
    # Higher low?
    if last['low'] > prev['low']:
        pa_score += 2.5
    scores['price_action'] = pa_score
    
    # 7. Visual Context Features - capped adjustment +/-8
    import config as _cfg
    if getattr(_cfg, 'VISUAL_FEATURES_ENABLED', True):
        visual = calculate_visual_features(df)
        visual_adj = visual.get("total_adjustment", 0.0)
    else:
        visual_adj = 0.0
    scores['visual_context'] = visual_adj
    
    # Total
    total_score = sum(scores.values())
    
    # Normalize to 0-100
    max_possible = 100
    normalized_score = (total_score / max_possible) * 100
    normalized_score = max(0, min(100, normalized_score))
    
    return round(normalized_score, 2), scores


def get_atr_value(df: pd.DataFrame) -> float:
    """Return current ATR value"""
    if df is None or 'atr_14' not in df.columns:
        return 10.0  # Default
    return df['atr_14'].iloc[-1]


def analyze_technical() -> Tuple[float, Dict, float]:
    """
    Complete technical analysis.
    
    Returns:
        Tuple: (score, breakdown, atr_value)
    """
    # Get data
    df = get_mt5_data()
    
    if df is None:
        return 50.0, {'error': 'No MT5 data'}, 10.0
    
    # Calculate indicators
    df = calculate_indicators(df)
    
    # Calculate score
    score, breakdown = calculate_technical_score(df)
    
    # Get ATR
    atr = get_atr_value(df)
    
    return score, breakdown, atr


# ============================================================================
# MACD DIVERGENCE DETECTION
# ============================================================================

def detect_macd_divergence(df: pd.DataFrame, lookback: int = 20, min_gap: int = 5) -> Dict:
    """
    Detect divergence between price and MACD histogram.
    
    Bullish Divergence: price makes lower low, MACD makes higher low
    Bearish Divergence: price makes higher high, MACD makes lower high
    
    Args:
        df: DataFrame with calculated indicators
        lookback: periods to search for peaks/valleys
        min_gap: minimum bars between peaks/valleys (anti-noise)
    
    Returns:
        Dict with detected (bool), type ("bullish"/"bearish"/None)
    """
    if df is None or len(df) < lookback or 'macd_hist' not in df.columns:
        return {"detected": False, "type": None}
    
    prices = df['close'].values[-lookback:]
    macd_hist = df['macd_hist'].values[-lookback:]
    
    def find_peaks(data):
        """Find peaks (local maxima) with minimum gap"""
        peaks = []
        for i in range(1, len(data) - 1):
            if data[i] > data[i - 1] and data[i] > data[i + 1]:
                if not peaks or (i - peaks[-1]) >= min_gap:
                    peaks.append(i)
        return peaks
    
    def find_valleys(data):
        """Find valleys (local minima) with minimum gap"""
        valleys = []
        for i in range(1, len(data) - 1):
            if data[i] < data[i - 1] and data[i] < data[i + 1]:
                if not valleys or (i - valleys[-1]) >= min_gap:
                    valleys.append(i)
        return valleys
    
    # Bearish Divergence: higher high in price + lower high in MACD
    price_peaks = find_peaks(prices)
    macd_peaks = find_peaks(macd_hist)
    
    if len(price_peaks) >= 2 and len(macd_peaks) >= 2:
        pp1, pp2 = price_peaks[-2], price_peaks[-1]
        mp1, mp2 = macd_peaks[-2], macd_peaks[-1]
        
        if prices[pp2] > prices[pp1] and macd_hist[mp2] < macd_hist[mp1]:
            # Recency: bars since most recent peak to end of window
            bars_since = lookback - 1 - max(pp2, mp2)
            return {"detected": True, "type": "bearish", "bars_since": bars_since}
    
    # Bullish Divergence: lower low in price + higher low in MACD
    price_valleys = find_valleys(prices)
    macd_valleys = find_valleys(macd_hist)
    
    if len(price_valleys) >= 2 and len(macd_valleys) >= 2:
        pv1, pv2 = price_valleys[-2], price_valleys[-1]
        mv1, mv2 = macd_valleys[-2], macd_valleys[-1]
        
        if prices[pv2] < prices[pv1] and macd_hist[mv2] > macd_hist[mv1]:
            bars_since = lookback - 1 - max(pv2, mv2)
            return {"detected": True, "type": "bullish", "bars_since": bars_since}
    
    return {"detected": False, "type": None}


# ============================================================================
# DETAILED ANALYSIS (for the Central Brain)
# ============================================================================

def analyze_technical_detailed(df: pd.DataFrame) -> Dict:
    """
    Detailed technical analysis returning ALL raw data.
    Used by the Central Brain for contextual reasoning.
    
    Args:
        df: DataFrame with already calculated indicators
    
    Returns:
        Dict with score + all detailed raw data
    """
    if df is None or len(df) < 50:
        return {
            "score": 50.0,
            "breakdown": {},
            "rsi": {"value": 50, "level": "neutro"},
            "macd": {"signal": "neutro", "histogram": 0, "divergence": {"detected": False, "type": None}},
            "ema": {"above_ema20": False, "above_ema50": False, "above_ema200": False, "trend": "neutro"},
            "bollinger": {"position": "meio", "width": 0, "squeeze": False},
            "stochastic": {"value": 50, "level": "neutro"},
            "error": "Insufficient data",
        }
    
    last = df.iloc[-1]
    
    # Original score
    score, breakdown = calculate_technical_score(df)
    
    # RSI
    rsi_value = float(last['rsi_14']) if 'rsi_14' in last.index else 50
    if rsi_value > 70:
        rsi_level = "overbought"
    elif rsi_value < 30:
        rsi_level = "oversold"
    else:
        rsi_level = "neutro"
    
    # MACD
    macd_val = float(last['macd']) if 'macd' in last.index else 0
    macd_sig = float(last['macd_signal']) if 'macd_signal' in last.index else 0
    macd_hist_val = float(last['macd_hist']) if 'macd_hist' in last.index else 0
    macd_signal = "bullish" if macd_val > macd_sig else "bearish"
    macd_divergence = detect_macd_divergence(df)
    
    # EMA positions
    close = float(last['close'])
    ema9 = float(last['ema_9']) if 'ema_9' in last.index else close
    ema21 = float(last['ema_21']) if 'ema_21' in last.index else close
    ema50 = float(last['ema_50']) if 'ema_50' in last.index else close
    
    above_ema9 = close > ema9
    above_ema21 = close > ema21
    above_ema50 = close > ema50
    
    # EMA trend (using EMA9 as proxy for EMA20, EMA50 already exists)
    if above_ema9 and above_ema21 and above_ema50:
        ema_trend = "bullish"
    elif not above_ema9 and not above_ema21 and not above_ema50:
        ema_trend = "bearish"
    else:
        ema_trend = "misto"
    
    # Bollinger Bands
    bb_upper = float(last['bb_upper']) if 'bb_upper' in last.index else close + 10
    bb_lower = float(last['bb_lower']) if 'bb_lower' in last.index else close - 10
    bb_middle = float(last['bb_middle']) if 'bb_middle' in last.index else close
    bb_width = bb_upper - bb_lower
    
    if bb_width > 0:
        bb_pos_ratio = (close - bb_lower) / bb_width
        if bb_pos_ratio > 0.8:
            bb_position = "banda_superior"
        elif bb_pos_ratio < 0.2:
            bb_position = "banda_inferior"
        else:
            bb_position = "meio"
    else:
        bb_pos_ratio = 0.5
        bb_position = "meio"
    
    # Squeeze detection: very narrow bands (width < 50% of average)
    if 'bb_upper' in df.columns and 'bb_lower' in df.columns:
        bb_widths = (df['bb_upper'] - df['bb_lower']).values
        avg_width = float(np.mean(bb_widths[-20:])) if len(bb_widths) >= 20 else bb_width
        bb_squeeze = bb_width < (avg_width * 0.5) if avg_width > 0 else False
    else:
        bb_squeeze = False
    
    # Stochastic
    stoch_value = float(last['stoch_k']) if 'stoch_k' in last.index else 50
    if stoch_value > 80:
        stoch_level = "overbought"
    elif stoch_value < 20:
        stoch_level = "oversold"
    else:
        stoch_level = "neutro"
    
    # Visual context features
    import config as _cfg
    if getattr(_cfg, 'VISUAL_FEATURES_ENABLED', True):
        visual = calculate_visual_features(df)
    else:
        visual = {
            "consecutive_candles": 0, "body_size_trend": "neutral",
            "price_vs_range": 0.5, "engulfing": None, "pin_bar": None,
            "adjustments": {}, "total_adjustment": 0.0,
        }
    
    return {
        "score": score,
        "breakdown": breakdown,
        "rsi": {
            "value": round(rsi_value, 2),
            "level": rsi_level,
        },
        "macd": {
            "signal": macd_signal,
            "histogram": round(macd_hist_val, 4),
            "divergence": macd_divergence,
        },
        "ema": {
            "above_ema20": above_ema9,  # EMA9 as proxy for EMA20
            "above_ema50": above_ema50,
            "above_ema200": above_ema50,  # No EMA200 available, using EMA50 as proxy
            "trend": ema_trend,
        },
        "bollinger": {
            "position": bb_position,
            "width": round(bb_width, 4),
            "squeeze": bb_squeeze,
        },
        "stochastic": {
            "value": round(stoch_value, 2),
            "level": stoch_level,
        },
        "visual_context": visual,
        "error": None,
    }


# ============================================================================
# TEST
# ============================================================================

def test_technical_analyzer():
    """Test the technical analyzer"""
    print("=" * 60)
    print("🧪 TECHNICAL ANALYZER TEST")
    print("=" * 60)
    
    # Try to connect MT5
    if not mt5.initialize():
        print("⚠️ MT5 not available, using simulated data")
        
        # Create simulated data
        np.random.seed(42)
        n = 100
        dates = pd.date_range(end=pd.Timestamp.now(), periods=n, freq='H')
        
        base_price = 2650
        prices = base_price + np.cumsum(np.random.randn(n) * 2)
        
        df = pd.DataFrame({
            'datetime': dates,
            'open': prices - np.random.rand(n),
            'high': prices + np.random.rand(n) * 3,
            'low': prices - np.random.rand(n) * 3,
            'close': prices,
            'volume': np.random.randint(1000, 5000, n)
        })
    else:
        print("✅ MT5 connected")
        df = get_mt5_data()
        mt5.shutdown()
    
    if df is None:
        print("❌ No data available")
        return
    
    print(f"\n📊 Data: {len(df)} bars")
    print(f"   Period: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
    
    # Calculate indicators
    df = calculate_indicators(df)
    
    print(f"\n📈 Latest values:")
    last = df.iloc[-1]
    print(f"   Close: {last['close']:.2f}")
    print(f"   EMA 9: {last['ema_9']:.2f}")
    print(f"   EMA 21: {last['ema_21']:.2f}")
    print(f"   RSI: {last['rsi_14']:.2f}")
    print(f"   MACD: {last['macd']:.4f}")
    print(f"   ATR: {last['atr_14']:.2f}")
    
    # Calculate score
    score, breakdown = calculate_technical_score(df)
    
    print(f"\n🎯 Technical Score: {score}/100")
    print(f"\n📋 Breakdown:")
    for component, value in breakdown.items():
        print(f"   {component}: {value}")
    
    # Interpretation
    if score > 70:
        signal = "STRONG BUY 🟢"
    elif score > 55:
        signal = "BUY 🟢"
    elif score > 45:
        signal = "NEUTRAL 🟡"
    elif score > 30:
        signal = "SELL 🔴"
    else:
        signal = "STRONG SELL 🔴"
    
    print(f"\n📊 Signal: {signal}")


if __name__ == "__main__":
    test_technical_analyzer()
