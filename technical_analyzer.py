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
    df['ema_200'] = df['close'].ewm(span=200, adjust=False).mean()

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


def _detect_approach_direction(df: pd.DataFrame, lookback: int = 3) -> Optional[str]:
    """
    Detect if price approached from above (bearish) or below (bullish) in last N candles.
    Used for doji context — doji at support is only bullish if price arrived from above.
    
    Returns:
        "bearish" if price moved down toward current level
        "bullish" if price moved up toward current level
        None if sideways/unclear
    """
    if df is None or len(df) < lookback + 1:
        return None
    
    recent = df.tail(lookback + 1)
    start_close = float(recent.iloc[0]['close'])
    end_close = float(recent.iloc[-1]['close'])
    
    if start_close <= 0:
        return None
    
    move_pct = (end_close - start_close) / start_close * 100
    
    if move_pct < -0.15:
        return "bearish"
    elif move_pct > 0.15:
        return "bullish"
    return None


def _get_sr_proximity_multiplier(current_price: float, atr: float, sr_zones: list) -> Tuple[float, Optional[dict]]:
    """
    Calculate S/R proximity multiplier for candlestick pattern scoring.
    
    Returns:
        Tuple: (multiplier, zone_info_dict or None)
        
    Multiplier logic:
        - Within 0.3×ATR of D1 zone (30+ touches): 2.0×
        - Within 0.3×ATR of D1 zone (10-29 touches): 1.75×
        - Within 0.5×ATR of H4 zone (10+ touches) or MTF confluence: 1.5×
        - Within 0.5×ATR of any zone (4+ touches): 1.25×
        - No nearby strong zone: 1.0×
    """
    if not sr_zones or atr <= 0:
        return 1.0, None
    
    best_multiplier = 1.0
    best_zone = None
    
    for zone in sr_zones:
        dist = abs(current_price - zone.midpoint)
        touches = zone.touches
        tf = zone.timeframe
        confluence = getattr(zone, 'confluence', [])
        
        # D1 zone within 0.3×ATR
        if tf == "D1" and dist <= atr * 0.3:
            if touches >= 30 and best_multiplier < 2.0:
                best_multiplier = 2.0
                best_zone = zone
            elif touches >= 10 and best_multiplier < 1.75:
                best_multiplier = 1.75
                best_zone = zone
        
        # H4 zone within 0.5×ATR with 10+ touches or MTF confluence
        if tf == "H4" and dist <= atr * 0.5:
            has_confluence = len(confluence) >= 2
            if (touches >= 10 or has_confluence) and best_multiplier < 1.5:
                best_multiplier = 1.5
                best_zone = zone
        
        # Any zone within 0.5×ATR with 4+ touches
        if dist <= atr * 0.5 and touches >= 4 and best_multiplier < 1.25:
            best_multiplier = 1.25
            best_zone = zone
    
    zone_info = None
    if best_zone:
        zone_info = {
            "price": best_zone.midpoint,
            "touches": best_zone.touches,
            "timeframe": best_zone.timeframe,
            "zone_type": best_zone.zone_type,
            "dist_pips": round(abs(current_price - best_zone.midpoint) / 0.1, 0),
        }
    
    return best_multiplier, zone_info


def _get_nearest_sr_distance(current_price: float, atr: float, sr_zones: list) -> float:
    """
    Get distance to nearest S/R zone in ATR units.
    Used for continuation pattern scoring reduction when far from S/R.
    
    Returns:
        Distance in ATR units (e.g., 1.5 means 1.5×ATR away)
    """
    if not sr_zones or atr <= 0:
        return 999.0
    
    min_dist = float('inf')
    for zone in sr_zones:
        dist = abs(current_price - zone.midpoint)
        if dist < min_dist:
            min_dist = dist
    
    return min_dist / atr


def detect_candlestick_patterns(df: pd.DataFrame, sr_zones: list = None, 
                                 current_price: float = None, atr: float = None) -> Dict:
    """
    Detect candlestick patterns with S/R proximity scaling.
    
    NOTE: As of Feb 2026, this is an INFORMATIONAL feature only.
    The ±8 point cap on visual adjustments is too small to move the final score
    across BUY/SELL/HOLD thresholds. Patterns show in dashboard Intel Feed for
    visual reference but do not affect trading decisions.
    Future option: raise cap to ±12 or move adjustment to confidence.
    
    Patterns detected (reversal only — continuation patterns disabled):
        - Morning Star / Evening Star (3-candle reversal)
        - Hammer / Shooting Star (1-candle reversal)
        - Doji at key levels (1-candle indecision, only scores near S/R)
        - Engulfing (2-candle reversal)
        - Pin Bar (1-candle rejection)
    
    Priority: Multi-candle > Single-candle structured > Doji
    No double-counting: only highest-priority pattern scores.
    
    Returns:
        Dict with detected patterns, scores, and S/R context
    """
    result = {
        "patterns": [],
        "primary_pattern": None,
        "total_adjustment": 0.0,
        "sr_multiplier": 1.0,
        "sr_context": None,
    }
    
    if df is None or len(df) < 4:
        return result
    
    sr_zones = sr_zones or []
    if current_price is None:
        current_price = float(df.iloc[-1]['close'])
    if atr is None:
        atr = float((df['high'] - df['low']).tail(14).mean())
    
    # Get S/R proximity multiplier
    sr_mult, sr_zone_info = _get_sr_proximity_multiplier(current_price, atr, sr_zones)
    result["sr_multiplier"] = sr_mult
    result["sr_context"] = sr_zone_info
    
    # Get distance to nearest S/R for continuation pattern adjustment
    sr_distance_atr = _get_nearest_sr_distance(current_price, atr, sr_zones)
    
    # Get approach direction for doji context
    approach_dir = _detect_approach_direction(df, lookback=3)
    
    # Candle data
    c0 = df.iloc[-1]  # Current candle
    c1 = df.iloc[-2]  # Previous candle
    c2 = df.iloc[-3]  # 2 candles ago
    c3 = df.iloc[-4] if len(df) >= 4 else None  # 3 candles ago
    
    detected_patterns = []
    
    # ========== 3-CANDLE PATTERNS (highest priority) ==========
    
    # Morning Star: bearish → small body/doji → bullish (body > 50% of first)
    if c3 is not None:
        c2_body = c2['close'] - c2['open']
        c1_body = abs(c1['close'] - c1['open'])
        c1_range = c1['high'] - c1['low']
        c0_body = c0['close'] - c0['open']
        
        c2_bearish = c2_body < 0
        c1_small = c1_range > 0 and c1_body < c1_range * 0.3
        c0_bullish = c0_body > 0
        c0_strong = abs(c0_body) > abs(c2_body) * 0.5
        
        if c2_bearish and c1_small and c0_bullish and c0_strong:
            base_score = 4.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Morning Star",
                "direction": "bullish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 1,
            })
    
    # Evening Star: bullish → small body/doji → bearish (body > 50% of first)
    if c3 is not None:
        c2_body = c2['close'] - c2['open']
        c1_body = abs(c1['close'] - c1['open'])
        c1_range = c1['high'] - c1['low']
        c0_body = c0['close'] - c0['open']
        
        c2_bullish = c2_body > 0
        c1_small = c1_range > 0 and c1_body < c1_range * 0.3
        c0_bearish = c0_body < 0
        c0_strong = abs(c0_body) > abs(c2_body) * 0.5
        
        if c2_bullish and c1_small and c0_bearish and c0_strong:
            base_score = -4.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Evening Star",
                "direction": "bearish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 1,
            })
    
    # NOTE: Three White Soldiers / Three Black Crows DISABLED
    # Backtest showed 40% WR and -$194 P&L — continuation patterns hurt the system
    # Keeping only reversal patterns (Morning Star, Evening Star, Engulfing, Pin Bar, Hammer, Shooting Star, Doji)
    
    # ========== 2-CANDLE PATTERNS ==========
    
    # Engulfing (existing logic, priority 2)
    c0_body = c0['close'] - c0['open']
    c1_body = c1['close'] - c1['open']
    
    # Bullish engulfing
    if c1_body < 0 and c0_body > 0:
        if c0['close'] > c1['open'] and c0['open'] < c1['close']:
            base_score = 4.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Bullish Engulfing",
                "direction": "bullish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 2,
            })
    
    # Bearish engulfing
    if c1_body > 0 and c0_body < 0:
        if c0['close'] < c1['open'] and c0['open'] > c1['close']:
            base_score = -4.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Bearish Engulfing",
                "direction": "bearish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 2,
            })
    
    # ========== 1-CANDLE PATTERNS (priority 2 for structured, 3 for doji) ==========
    
    c0_high = c0['high']
    c0_low = c0['low']
    c0_open = c0['open']
    c0_close = c0['close']
    c0_range = c0_high - c0_low
    c0_body_size = abs(c0_close - c0_open)
    
    if c0_range > 0:
        upper_shadow = c0_high - max(c0_open, c0_close)
        lower_shadow = min(c0_open, c0_close) - c0_low
        body_ratio = c0_body_size / c0_range
        
        # Hammer: small body at top, lower shadow ≥ 2× body, minimal upper shadow
        is_hammer = (
            body_ratio < 0.35 and
            lower_shadow >= c0_body_size * 2 and
            upper_shadow < c0_body_size * 0.5
        )
        
        if is_hammer:
            base_score = 2.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Hammer",
                "direction": "bullish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 2,
            })
        
        # Shooting Star: small body at bottom, upper shadow ≥ 2× body, minimal lower shadow
        is_shooting_star = (
            body_ratio < 0.35 and
            upper_shadow >= c0_body_size * 2 and
            lower_shadow < c0_body_size * 0.5
        )
        
        if is_shooting_star:
            base_score = -2.0
            final_score = base_score * sr_mult
            detected_patterns.append({
                "name": "Shooting Star",
                "direction": "bearish",
                "base_score": base_score,
                "sr_multiplier": sr_mult,
                "final_score": round(final_score, 1),
                "priority": 2,
            })
        
        # Pin Bar (existing logic, priority 2)
        if body_ratio < 0.3:
            if lower_shadow > c0_range * 0.6:
                base_score = 3.0
                final_score = base_score * sr_mult
                detected_patterns.append({
                    "name": "Bullish Pin Bar",
                    "direction": "bullish",
                    "base_score": base_score,
                    "sr_multiplier": sr_mult,
                    "final_score": round(final_score, 1),
                    "priority": 2,
                })
            elif upper_shadow > c0_range * 0.6:
                base_score = -3.0
                final_score = base_score * sr_mult
                detected_patterns.append({
                    "name": "Bearish Pin Bar",
                    "direction": "bearish",
                    "base_score": base_score,
                    "sr_multiplier": sr_mult,
                    "final_score": round(final_score, 1),
                    "priority": 2,
                })
        
        # Doji at key level (priority 3, only scores near S/R with approach)
        is_doji = body_ratio < 0.1
        
        if is_doji and sr_mult > 1.0 and approach_dir is not None:
            # Doji at support with bearish approach → bullish signal
            # Doji at resistance with bullish approach → bearish signal
            zone_type = sr_zone_info.get("zone_type", "") if sr_zone_info else ""
            
            doji_direction = None
            if zone_type in ("SUPPORT", "FLIP") and approach_dir == "bearish":
                doji_direction = "bullish"
            elif zone_type in ("RESISTANCE", "FLIP") and approach_dir == "bullish":
                doji_direction = "bearish"
            
            if doji_direction:
                base_score = 2.0 if doji_direction == "bullish" else -2.0
                final_score = base_score * sr_mult
                detected_patterns.append({
                    "name": "Doji at Key Level",
                    "direction": doji_direction,
                    "base_score": base_score,
                    "sr_multiplier": sr_mult,
                    "final_score": round(final_score, 1),
                    "priority": 3,
                    "approach": approach_dir,
                })
    
    # ========== SELECT PRIMARY PATTERN (highest priority, no double-counting) ==========
    
    if detected_patterns:
        # Sort by priority (lower = higher priority), then by absolute score
        detected_patterns.sort(key=lambda p: (p["priority"], -abs(p["final_score"])))
        primary = detected_patterns[0]
        result["primary_pattern"] = primary
        result["total_adjustment"] = primary["final_score"]
    
    result["patterns"] = detected_patterns
    
    return result


def calculate_visual_features(df: pd.DataFrame, sr_zones: list = None,
                               current_price: float = None, atr: float = None) -> Dict:
    """
    Calculate visual context features that a human trader would see on the chart.
    
    Features:
        1. consecutive_candles_H1: consecutive candles in the same direction
        2. body_size_trend_H1: bodies growing (momentum) vs shrinking (exhaustion)
        3. price_vs_range_20H1: price position in range of last 20 candles (0-1)
        4. candlestick_patterns: detected patterns with S/R proximity scaling
    
    Args:
        df: DataFrame with OHLCV data
        sr_zones: List of SRZone objects for proximity scaling
        current_price: Current price (optional, defaults to last close)
        atr: ATR value (optional, calculated from data if not provided)
    
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
        "candlestick_patterns": None,
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
    
    # 4. Candlestick patterns with S/R proximity scaling (replaces old engulfing/pin_bar)
    pattern_result = detect_candlestick_patterns(df, sr_zones, current_price, atr)
    result["candlestick_patterns"] = pattern_result
    
    primary = pattern_result.get("primary_pattern")
    if primary:
        pattern_adj = primary.get("final_score", 0.0)
        adjustments["candlestick_pattern"] = pattern_adj
        total_adj += pattern_adj
        
        # Set legacy fields for backward compatibility
        if "Engulfing" in primary.get("name", ""):
            result["engulfing"] = primary.get("direction")
        if "Pin Bar" in primary.get("name", ""):
            result["pin_bar"] = primary.get("direction")
    
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
    ema200 = float(last['ema_200']) if 'ema_200' in last.index else close

    above_ema9 = close > ema9
    above_ema21 = close > ema21
    above_ema50 = close > ema50
    above_ema200 = close > ema200

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
            "ema9": round(ema9, 2),
            "ema21": round(ema21, 2),
            "ema50": round(ema50, 2),
            "ema200": round(ema200, 2),
            "above_ema20": above_ema9,  # EMA9 as proxy for EMA20
            "above_ema50": above_ema50,
            "above_ema200": above_ema200,
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
