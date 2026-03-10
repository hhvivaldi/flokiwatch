"""
AGENT DATA BUILDER
Builds the data package sent to the AI Agent.
Collects raw price data, indicators, Brain analysis, ML predictions,
news/macro data, positions, and session context.
"""

from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from logger import log

logger = log


def _safe_round(value, decimals: int = 2):
    """Safely round a value, handling strings and None."""
    if value is None:
        return 0
    try:
        return round(float(value), decimals)
    except (ValueError, TypeError):
        return 0


def build_data_package(
    brain_result: Any,
    tech_data: Dict,
    ml_data: Dict,
    momentum_data: Dict,
    news_data: Dict,
    calendar_data: Dict,
    h1_candles: List[Dict],
    m5_candles: List[Dict],
    current_price: Dict,
    positions: List[Dict],
    session_context: Dict,
    volatility_status: Dict,
    sr_zones: Optional[List] = None,
    candlestick_patterns: Optional[Dict] = None,
    sr_proximity: Optional[Dict] = None,
    d1_candles: Optional[List[Dict]] = None,
    h4_candles: Optional[List[Dict]] = None,
    agent_memory: Optional[List[Dict]] = None,
    trade_feedback: Optional[Dict] = None,
    delta_context: Optional[Dict] = None,
    portfolio: Optional[Dict] = None,
    regime_context: Optional[Dict] = None,
) -> Dict:
    """
    Build the complete data package for the AI Agent.
    
    Args:
        brain_result: BrainResult from central_brain.py
        tech_data: Technical analysis data
        ml_data: ML predictions
        momentum_data: Momentum detector data
        news_data: News and macro data
        calendar_data: Economic calendar data
        h1_candles: Last 20-30 H1 candles (OHLCV)
        m5_candles: Last 10 M5 candles (OHLCV)
        current_price: Current bid/ask/spread
        positions: Open positions list
        session_context: Session info and recent performance
        volatility_status: Volatility guard status
        sr_zones: List of SRZone objects (4-8 nearest zones)
        candlestick_patterns: Dict from detect_candlestick_patterns()
        sr_proximity: Dict with near_strong_zone and distance info
        d1_candles: Last 5-10 D1 candles (weekly context)
        h4_candles: Last 10-15 H4 candles (2-3 day structure)
        agent_memory: Last 3-5 Agent decisions for self-reference
        trade_feedback: Recent trade results with Agent accuracy
        delta_context: What changed since last cycle
        portfolio: Daily P&L, W/L, drawdown, risk budget
        regime_context: Trending/ranging, ADX/ATR analysis
        
    Returns:
        Complete data package dict for Agent
    """
    try:
        # Get current price value for S/R zone formatting
        price_val = 0
        if current_price:
            price_val = current_price.get("bid", current_price.get("ask", 0))

        formatted_sr_zones = _format_sr_zones(sr_zones or [], price_val)
        nearest_support = _compute_nearest_sr(formatted_sr_zones, side="below")
        nearest_resistance = _compute_nearest_sr(formatted_sr_zones, side="above")
        
        package = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "h1_candles": _format_candles(h1_candles, limit=20),
            "m5_candles": _format_candles(m5_candles, limit=10),
            "d1_candles": _format_candles(d1_candles or [], limit=10),
            "h4_candles": _format_candles(h4_candles or [], limit=20),
            "indicators": _format_indicators(tech_data, momentum_data),
            "brain_analysis": _format_brain_result(brain_result),
            "ml_predictions": _format_ml_data(ml_data),
            "macro": _format_macro_data(news_data, calendar_data),
            "positions": _format_positions(positions),
            "session": _format_session_context(session_context),
            "volatility": _format_volatility(volatility_status),
            "sr_zones": formatted_sr_zones,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "candlestick_patterns": _format_candlestick_patterns(candlestick_patterns),
            "sr_proximity": _format_sr_proximity(sr_proximity),
            "agent_memory": _format_agent_memory(agent_memory or []),
            "trade_feedback": _format_trade_feedback(trade_feedback),
            "delta_context": _format_delta_context(delta_context),
            "portfolio": _format_portfolio(portfolio),
            "regime_context": _format_regime_context(regime_context),
        }
        
        return package
        
    except Exception as e:
        logger.error(f"Error building data package: {e}")
        return _minimal_package(brain_result, current_price)


def build_proactive_data_package(
    brain_result: Any,
    tech_data: Dict,
    ml_data: Dict,
    momentum_data: Dict,
    news_data: Dict,
    calendar_data: Dict,
    h1_candles: List[Dict],
    m5_candles: List[Dict],
    current_price: Dict,
    positions: List[Dict],
    session_context: Dict,
    volatility_status: Dict,
    sr_zones: Optional[List] = None,
    candlestick_patterns: Optional[Dict] = None,
    sr_proximity: Optional[Dict] = None,
    d1_candles: Optional[List[Dict]] = None,
    h4_candles: Optional[List[Dict]] = None,
    trade_feedback: Optional[Dict] = None,
) -> Dict:
    """Build an independent data package for proactive Agent snapshots.

    Excludes Brain opinion/scoring and agent_memory_context; includes only raw market context.
    """
    try:
        price_val = 0
        if current_price:
            price_val = current_price.get("bid", current_price.get("ask", 0))

        formatted_sr_zones = _format_sr_zones(sr_zones or [], price_val)
        nearest_support = _compute_nearest_sr(formatted_sr_zones, side="below")
        nearest_resistance = _compute_nearest_sr(formatted_sr_zones, side="above")

        mtf_trend = None
        try:
            if brain_result is not None and hasattr(brain_result, "mtf_trend"):
                mtf = getattr(brain_result, "mtf_trend", None) or {}
                mtf_trend = {
                    "d1_direction": mtf.get("d1_direction"),
                    "h4_direction": mtf.get("h4_direction"),
                }
        except Exception:
            mtf_trend = None

        package = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "h1_candles": _format_candles(h1_candles, limit=20),
            "m5_candles": _format_candles(m5_candles, limit=10),
            "d1_candles": _format_candles(d1_candles or [], limit=10),
            "h4_candles": _format_candles(h4_candles or [], limit=20),
            "indicators": _format_indicators(tech_data, momentum_data),
            "ml_predictions": _format_ml_data(ml_data),
            "macro": _format_macro_data(news_data, calendar_data),
            "positions": _format_positions(positions),
            "session": _format_session_context(session_context),
            "volatility": _format_volatility(volatility_status),
            "sr_zones": formatted_sr_zones,
            "nearest_support": nearest_support,
            "nearest_resistance": nearest_resistance,
            "candlestick_patterns": _format_candlestick_patterns(candlestick_patterns),
            "sr_proximity": _format_sr_proximity(sr_proximity),
            "trade_feedback": _format_trade_feedback(trade_feedback),
            "mtf_trend": mtf_trend,
        }

        return package
    except Exception as e:
        logger.error(f"Error building proactive data package: {e}")
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "current_price": _format_current_price(current_price),
            "error": "Partial proactive package - some components failed to load",
        }


def _format_current_price(price_data: Dict) -> Dict:
    """Format current price data"""
    if not price_data:
        return {"bid": 0, "ask": 0, "spread": 0}
    
    return {
        "bid": _safe_round(price_data.get("bid", 0), 2),
        "ask": _safe_round(price_data.get("ask", 0), 2),
        "spread": _safe_round(price_data.get("spread", 0), 1),
    }


def _format_candles(candles: List[Dict], limit: int = 20) -> List[Dict]:
    """
    Format candle data for the Agent.
    Keep only essential OHLCV data, limit to recent candles.
    """
    if not candles:
        return []
    
    # Take most recent candles
    recent = candles[-limit:] if len(candles) > limit else candles
    
    formatted = []
    for c in recent:
        formatted.append({
            "time": c.get("time", ""),
            "o": _safe_round(c.get("open", 0), 2),
            "h": _safe_round(c.get("high", 0), 2),
            "l": _safe_round(c.get("low", 0), 2),
            "c": _safe_round(c.get("close", 0), 2),
            "v": int(c.get("tick_volume", c.get("volume", 0)) or 0),
        })
    
    return formatted


def _format_indicators(tech_data: Dict, momentum_data: Dict) -> Dict:
    """Format technical indicators for the Agent"""
    indicators = {}
    
    # RSI
    rsi = tech_data.get("rsi", {})
    indicators["rsi"] = {
        "value": _safe_round(rsi.get("value", 50), 1),
        "level": rsi.get("level", "neutral"),
    }
    
    # MACD
    macd = tech_data.get("macd", {})
    indicators["macd"] = {
        "histogram": _safe_round(macd.get("histogram", 0), 3),
        "signal": macd.get("signal", "neutral"),
        "trend": macd.get("trend", "neutral"),
    }
    
    # EMAs
    ema = tech_data.get("ema", {})
    indicators["emas"] = {
        "ema9": _safe_round(ema.get("ema9", 0), 2),
        "ema21": _safe_round(ema.get("ema21", 0), 2),
        "ema50": _safe_round(ema.get("ema50", 0), 2),
        "above_ema20": ema.get("above_ema20", False),
        "above_ema50": ema.get("above_ema50", False),
    }
    
    # Bollinger Bands
    bb = tech_data.get("bollinger", {})
    indicators["bollinger"] = {
        "upper": _safe_round(bb.get("upper", 0), 2),
        "middle": _safe_round(bb.get("middle", 0), 2),
        "lower": _safe_round(bb.get("lower", 0), 2),
        "position": _safe_round(bb.get("position", 0.5), 2),  # 0-1 where price is in band
        "squeeze": bb.get("squeeze", False),
    }
    
    # ATR
    atr = momentum_data.get("atr", {})
    indicators["atr"] = {
        "value": _safe_round(atr.get("atr_value", 0), 2),
        "trend": atr.get("atr_trend", "stable"),
    }
    
    # ADX
    adx = momentum_data.get("adx", {})
    indicators["adx"] = {
        "value": _safe_round(adx.get("adx_value", 0), 1),
        "plus_di": _safe_round(adx.get("plus_di", 0), 1),
        "minus_di": _safe_round(adx.get("minus_di", 0), 1),
        "classification": adx.get("adx_classification", "weak"),
    }
    
    # Volume (tick volume - XAU/USD has no real volume data)
    volume = momentum_data.get("volume", {})
    indicators["volume"] = {
        "tick_volume_ratio": _safe_round(volume.get("volume_ratio", 1.0), 2),
        "classification": volume.get("volume_classification", "normal"),
    }
    
    return indicators


def _format_brain_result(brain_result: Any) -> Dict:
    """Format Brain analysis for the Agent"""
    if brain_result is None:
        return {
            "decision": "HOLD",
            "score": 50,
            "confidence": 50,
            "scenario": "unknown",
            "pillar_scores": {},
            "confirmations": [],
            "alerts": [],
            "mtf_trend": {"d1_direction": None, "h4_direction": None, "alignment": "n/a"},
            "volume_gate": {"volume_ratio": 1.0, "status": "normal"},
        }
    
    # Handle both BrainResult object and dict
    if hasattr(brain_result, "decision"):
        # Extract MTF trend data
        mtf_trend = getattr(brain_result, "mtf_trend", None) or {}
        mtf_trend_formatted = {
            "d1_direction": mtf_trend.get("d1_direction"),
            "h4_direction": mtf_trend.get("h4_direction"),
            "alignment": mtf_trend.get("alignment", "n/a"),
        }
        logger.debug(f"[Agent Data] MTF trend: d1={mtf_trend_formatted['d1_direction']}, h4={mtf_trend_formatted['h4_direction']}, alignment={mtf_trend_formatted['alignment']}")
        
        # Extract Volume Gate data
        volume_gate = getattr(brain_result, "volume_gate", None) or {}
        volume_gate_formatted = {
            "volume_ratio": _safe_round(volume_gate.get("volume_ratio", 1.0), 2),
            "status": volume_gate.get("status", "normal"),
        }
        
        return {
            "decision": brain_result.decision,
            "score": _safe_round(brain_result.final_score, 1),
            "confidence": _safe_round(brain_result.confidence, 1),
            "confidence_level": brain_result.confidence_level,
            "scenario": brain_result.scenario,
            "scenario_description": brain_result.scenario_description,
            "pillar_scores": {
                "technical": _safe_round(brain_result.adjusted_scores.get("technical", 50), 1),
                "ml": _safe_round(brain_result.adjusted_scores.get("ml", 50), 1),
                "momentum": _safe_round(brain_result.adjusted_scores.get("momentum", 50), 1),
                "news": _safe_round(brain_result.adjusted_scores.get("news", 50), 1),
                "calendar": _safe_round(brain_result.adjusted_scores.get("calendar", 50), 1),
            },
            "weights_used": brain_result.adjusted_weights,
            "confirmations": brain_result.confirmations[:5],  # Limit to 5
            "alerts": brain_result.alerts[:5],  # Limit to 5
            "mtf_trend": mtf_trend_formatted,
            "volume_gate": volume_gate_formatted,
        }
    else:
        # Dict format
        mtf_trend = brain_result.get("mtf_trend", {}) or {}
        volume_gate = brain_result.get("volume_gate", {}) or {}
        return {
            "decision": brain_result.get("decision", "HOLD"),
            "score": _safe_round(brain_result.get("final_score", 50), 1),
            "confidence": _safe_round(brain_result.get("confidence", 50), 1),
            "scenario": brain_result.get("scenario", "unknown"),
            "pillar_scores": brain_result.get("adjusted_scores", {}),
            "confirmations": brain_result.get("confirmations", [])[:5],
            "alerts": brain_result.get("alerts", [])[:5],
            "mtf_trend": {
                "d1_direction": mtf_trend.get("d1_direction"),
                "h4_direction": mtf_trend.get("h4_direction"),
                "alignment": mtf_trend.get("alignment", "n/a"),
            },
            "volume_gate": {
                "volume_ratio": _safe_round(volume_gate.get("volume_ratio", 1.0), 2),
                "status": volume_gate.get("status", "normal"),
            },
        }


def _format_ml_data(ml_data: Dict) -> Dict:
    """Format ML predictions for the Agent"""
    if not ml_data:
        return {
            "prediction": "neutral",
            "confidence": 0.5,
            "h1": {"bullish_prob": 0.5},
            "h4": {"bullish_prob": 0.5},
        }
    
    return {
        "prediction": ml_data.get("prediction", "neutral"),
        "confidence": _safe_round(ml_data.get("max_confidence", 0.5), 2),
        "pattern": ml_data.get("pattern", "undefined"),
        "h1": {
            "bullish_prob": _safe_round(ml_data.get("h1_bullish_prob", 0.5), 2),
            "confidence": _safe_round(ml_data.get("h1_confidence", 0.5), 2),
        },
        "h4": {
            "bullish_prob": _safe_round(ml_data.get("h4_bullish_prob", 0.5), 2),
            "confidence": _safe_round(ml_data.get("h4_confidence", 0.5), 2),
        },
        "ensemble_agreement": ml_data.get("ensemble_agreement", 0),
    }


def _format_macro_data(news_data: Dict, calendar_data: Dict) -> Dict:
    """Format news and macro data for the Agent"""
    macro = {}
    
    # Headlines (limit to 5 most recent)
    headlines = news_data.get("headlines", [])
    if isinstance(headlines, list):
        macro["headlines"] = headlines[:5]
    else:
        macro["headlines"] = []
    
    # DXY
    dxy = news_data.get("dxy", {})
    macro["dxy"] = {
        "value": _safe_round(dxy.get("value", 0), 2),
        "change_24h": _safe_round(dxy.get("change_24h", 0), 2),
        "trend": dxy.get("trend", "stable"),
    }
    
    # VIX
    vix = news_data.get("vix", {})
    macro["vix"] = {
        "value": _safe_round(vix.get("value", 0), 1),
        "level": vix.get("level", "normal"),
    }
    
    # Yields
    yields = news_data.get("yields", {})
    macro["yields_10y"] = {
        "value": _safe_round(yields.get("value", 0), 2),
        "trend": yields.get("trend", "stable"),
    }
    
    # Calendar
    macro["calendar"] = {
        "phase": calendar_data.get("phase", "normal"),
        "bias": calendar_data.get("bias", "NEUTRAL"),
        "score": _safe_round(calendar_data.get("score", 50), 1),
        "next_event": calendar_data.get("next_event_name", ""),
        "next_event_in": calendar_data.get("next_event_minutes", 0),
    }
    
    # Sentiment
    sentiment = news_data.get("sentiment", {})
    macro["sentiment"] = {
        "normalized": _safe_round(sentiment.get("normalized", 0), 2),
        "label": sentiment.get("label", "neutral"),
    }
    
    return macro


def _format_positions(positions: List[Dict]) -> List[Dict]:
    """Format open positions for the Agent"""
    if not positions:
        return []
    
    formatted = []
    for pos in positions[:3]:  # Max 3 positions
        formatted.append({
            "ticket": pos.get("ticket", 0),
            "direction": pos.get("type", "unknown"),
            "entry_price": _safe_round(pos.get("price_open", 0), 2),
            "current_price": _safe_round(pos.get("price_current", 0), 2),
            "profit_pips": _safe_round(pos.get("profit_pips", 0), 1),
            "profit_usd": _safe_round(pos.get("profit", 0), 2),
            "sl": _safe_round(pos.get("sl", 0), 2),
            "tp": _safe_round(pos.get("tp", 0), 2),
            "duration_hours": _safe_round(pos.get("duration_hours", 0), 1),
            "phase": pos.get("phase", "active"),  # active, breakeven, trailing
        })
    
    return formatted


def _format_session_context(session_context: Dict) -> Dict:
    """Format session and recent performance context"""
    if not session_context:
        return {
            "name": "unknown",
            "today_trades": 0,
            "today_wl": "0W/0L",
            "today_pnl": 0,
            "last_5_results": [],
        }
    
    return {
        "name": session_context.get("session_name", "unknown"),
        "hour_utc": session_context.get("hour_utc", 0),
        "today_trades": session_context.get("today_trades", 0),
        "today_wins": session_context.get("today_wins", 0),
        "today_losses": session_context.get("today_losses", 0),
        "today_pnl": _safe_round(session_context.get("today_pnl", 0), 2),
        "last_5_results": session_context.get("last_5_results", []),
        "consecutive_losses": session_context.get("consecutive_losses", 0),
    }


def _format_volatility(volatility_status: Dict) -> Dict:
    """Format volatility guard status"""
    if not volatility_status:
        return {
            "status": "NORMAL",
            "m5_move_pct": 0,
        }
    
    return {
        "status": volatility_status.get("status", "NORMAL"),
        "m5_move_pct": _safe_round(volatility_status.get("extreme_percent", 0), 2),
        "cooling_until": volatility_status.get("cooling_until", ""),
    }


def _format_sr_zones(sr_zones: List, current_price: float, max_zones: int = 8) -> List[Dict]:
    """
    Format S/R zones for the Agent.
    Returns 4 zones above and 4 zones below current price (nearest first).
    
    Args:
        sr_zones: List of SRZone objects from support_resistance.py
        current_price: Current price for distance calculation
        max_zones: Maximum total zones to return (default 8)
    
    Returns:
        List of formatted zone dicts
    """
    if not sr_zones or not current_price:
        return []
    
    PIP_SIZE = 0.01
    
    # Split into above and below current price
    above = []
    below = []
    
    for zone in sr_zones:
        # Handle both SRZone objects and dicts
        if hasattr(zone, "midpoint"):
            midpoint = zone.midpoint
            zone_type = zone.zone_type
            touches = zone.touches
            timeframe = zone.timeframe
            strength = zone.strength
            confluence = getattr(zone, "confluence", [])
        else:
            midpoint = zone.get("midpoint", zone.get("price", 0))
            zone_type = zone.get("zone_type", "UNKNOWN")
            touches = zone.get("touches", 0)
            timeframe = zone.get("timeframe", "H1")
            strength = zone.get("strength", "weak")
            confluence = zone.get("confluence", [])
        
        dist_pips = abs(midpoint - current_price) / PIP_SIZE
        
        formatted = {
            "price": _safe_round(midpoint, 2),
            "zone_type": zone_type,
            "touches": touches,
            "timeframe": timeframe,
            "strength": strength,
            "dist_pips": _safe_round(dist_pips, 0),
            "position": "above" if midpoint > current_price else "below",
            "confluence": confluence if confluence else [],
        }
        
        if midpoint > current_price:
            above.append(formatted)
        else:
            below.append(formatted)
    
    # Sort: above by distance ascending (nearest first), below by distance ascending
    above.sort(key=lambda z: z["dist_pips"])
    below.sort(key=lambda z: z["dist_pips"])
    
    # Take 4 nearest from each side
    half = max_zones // 2
    result = above[:half] + below[:half]
    
    return result


def _compute_nearest_sr(formatted_sr_zones: List[Dict], side: str) -> Optional[Dict]:
    """Compute nearest support/resistance from formatted zones."""
    if not formatted_sr_zones:
        return None

    nearest = None
    for z in formatted_sr_zones:
        if z.get("position") != side:
            continue
        if z.get("price") is None or z.get("dist_pips") is None:
            continue
        if nearest is None or float(z.get("dist_pips", 1e9)) < float(nearest.get("distance_pips", 1e9)):
            nearest = {
                "level": z.get("price"),
                "distance_pips": z.get("dist_pips"),
            }

    return nearest


def _format_candlestick_patterns(patterns_data: Dict) -> Dict:
    """
    Format candlestick patterns for the Agent.
    
    Args:
        patterns_data: Dict from detect_candlestick_patterns()
    
    Returns:
        Formatted dict with primary pattern and all detected patterns
    """
    if not patterns_data:
        return {
            "primary_pattern": None,
            "patterns": [],
            "sr_multiplier": 1.0,
            "sr_context": None,
        }
    
    primary = patterns_data.get("primary_pattern")
    primary_formatted = None
    if primary:
        primary_formatted = {
            "name": primary.get("name", ""),
            "direction": primary.get("direction", ""),
            "base_score": primary.get("base_score", 0),
            "sr_multiplier": primary.get("sr_multiplier", 1.0),
            "final_score": primary.get("final_score", 0),
        }
    
    # Format all patterns (limit to 3)
    all_patterns = []
    for p in patterns_data.get("patterns", [])[:3]:
        all_patterns.append({
            "name": p.get("name", ""),
            "direction": p.get("direction", ""),
            "score": p.get("final_score", 0),
        })
    
    return {
        "primary_pattern": primary_formatted,
        "patterns": all_patterns,
        "sr_multiplier": patterns_data.get("sr_multiplier", 1.0),
        "sr_context": patterns_data.get("sr_context"),
    }


def _format_sr_proximity(sr_proximity_data: Dict) -> Dict:
    """
    Format S/R proximity data for the Agent.
    
    Args:
        sr_proximity_data: Dict with near_strong_zone and distance info
    
    Returns:
        Formatted dict
    """
    if not sr_proximity_data:
        return {
            "near_strong_zone": False,
            "nearest_zone_dist_pips": None,
            "nearest_zone_info": None,
        }
    
    zone_info = sr_proximity_data.get("near_zone_info")
    zone_info_formatted = None
    if zone_info:
        zone_info_formatted = {
            "price": zone_info.get("price"),
            "zone_type": zone_info.get("zone_type"),
            "touches": zone_info.get("touches"),
            "timeframe": zone_info.get("timeframe"),
        }
    
    return {
        "near_strong_zone": sr_proximity_data.get("near_strong_zone", False),
        "nearest_zone_dist_pips": sr_proximity_data.get("dist_to_nearest_pips"),
        "nearest_zone_info": zone_info_formatted,
    }


def _format_agent_memory(recent_decisions: List[Dict]) -> Dict:
    """
    Format Agent memory (recent decisions) for self-reference.
    Converts timestamps to relative time.
    """
    if not recent_decisions:
        return {"recent_decisions": []}
    
    now = datetime.now(timezone.utc)
    formatted = []
    
    for decision in recent_decisions[:5]:  # Max 5
        timestamp_str = decision.get("timestamp", "")
        relative_time = "unknown"
        
        # Convert timestamp to relative time
        if timestamp_str:
            try:
                # Parse ISO timestamp
                if "T" in timestamp_str:
                    dt = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                else:
                    dt = datetime.fromisoformat(timestamp_str)
                
                # Make timezone-aware if needed
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                
                delta = now - dt
                minutes = int(delta.total_seconds() / 60)
                
                if minutes < 1:
                    relative_time = "just now"
                elif minutes < 60:
                    relative_time = f"{minutes} min ago"
                elif minutes < 1440:
                    hours = minutes // 60
                    relative_time = f"{hours} hr ago"
                else:
                    days = minutes // 1440
                    relative_time = f"{days} day ago"
            except Exception:
                relative_time = "unknown"
        
        formatted.append({
            "time": relative_time,
            "trigger": decision.get("trigger", "SIGNAL"),
            "decision": decision.get("decision", "UNKNOWN"),
            "reasoning_summary": decision.get("reasoning_summary", ""),
        })
    
    return {"recent_decisions": formatted}


def _format_trade_feedback(feedback_data: Optional[Dict]) -> Dict:
    """Format trade feedback with Agent accuracy stats."""
    if not feedback_data:
        return {
            "last_trades": [],
            "agent_accuracy": {
                "total_decisions": 0,
                "correct_rejects": 0,
                "incorrect_rejects": 0,
                "correct_opens": 0,
                "incorrect_opens": 0,
            }
        }
    
    return {
        "last_trades": feedback_data.get("last_trades", [])[:5],
        "agent_accuracy": feedback_data.get("agent_accuracy", {}),
    }


def _format_delta_context(delta_data: Optional[Dict]) -> Dict:
    """Format delta context (what changed since last cycle)."""
    if not delta_data:
        return {
            "price_change_pips": 0,
            "rsi_change": 0,
            "volume_change_pct": 0,
            "significant_events": [],
        }
    
    return {
        "price_change_pips": _safe_round(delta_data.get("price_change_pips", 0), 1),
        "rsi_change": _safe_round(delta_data.get("rsi_change", 0), 1),
        "volume_change_pct": _safe_round(delta_data.get("volume_change_pct", 0), 1),
        "significant_events": delta_data.get("significant_events", [])[:5],
    }


def _format_portfolio(portfolio_data: Optional[Dict]) -> Dict:
    """Format portfolio awareness data."""
    if not portfolio_data:
        return {
            "daily_pnl": 0,
            "daily_wins": 0,
            "daily_losses": 0,
            "win_rate_today": 0,
            "drawdown_pct": 0,
            "risk_budget_remaining_pct": 100,
        }
    
    return {
        "daily_pnl": _safe_round(portfolio_data.get("daily_pnl", 0), 2),
        "daily_wins": portfolio_data.get("daily_wins", 0),
        "daily_losses": portfolio_data.get("daily_losses", 0),
        "win_rate_today": _safe_round(portfolio_data.get("win_rate_today", 0), 1),
        "drawdown_pct": _safe_round(portfolio_data.get("drawdown_pct", 0), 2),
        "risk_budget_remaining_pct": _safe_round(portfolio_data.get("risk_budget_remaining_pct", 100), 1),
    }


def _format_regime_context(regime_data: Optional[Dict]) -> Dict:
    """Format regime context (trending/ranging, ADX/ATR analysis)."""
    if not regime_data:
        return {
            "regime": "unknown",
            "adx_hours_above_25": 0,
            "atr_vs_weekly_avg": 1.0,
            "trend_strength": "unknown",
        }
    
    return {
        "regime": regime_data.get("regime", "unknown"),
        "adx_hours_above_25": regime_data.get("adx_hours_above_25", 0),
        "atr_vs_weekly_avg": _safe_round(regime_data.get("atr_vs_weekly_avg", 1.0), 2),
        "trend_strength": regime_data.get("trend_strength", "unknown"),
    }


def _minimal_package(brain_result: Any, current_price: Dict) -> Dict:
    """Create minimal package when full build fails"""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "current_price": _format_current_price(current_price),
        "brain_analysis": _format_brain_result(brain_result),
        "error": "Partial data package - some components failed to load",
    }


def get_session_name(hour_utc: int) -> str:
    """
    Get trading session name from UTC hour.
    
    Sessions:
    - Asian: 00:00-08:00 UTC
    - London: 08:00-16:00 UTC
    - New York: 13:00-21:00 UTC (overlaps with London 13:00-16:00)
    """
    if 0 <= hour_utc < 8:
        return "Asian"
    elif 8 <= hour_utc < 13:
        return "London"
    elif 13 <= hour_utc < 16:
        return "London/NY"
    elif 16 <= hour_utc < 21:
        return "New York"
    else:
        return "After Hours"


# =============================================================================
# TESTS
# =============================================================================

def _test_data_builder():
    """Test the data builder with mock data"""
    print("=" * 60)
    print("📦 DATA BUILDER TEST")
    print("=" * 60)
    
    # Mock Brain result
    class MockBrainResult:
        decision = "BUY"
        final_score = 68.2
        confidence = 72.0
        confidence_level = "HIGH"
        scenario = "momentum_forte_confirmado"
        scenario_description = "Strong momentum confirmed"
        adjusted_scores = {"technical": 65, "ml": 70, "momentum": 75, "news": 55, "calendar": 50}
        adjusted_weights = {"technical": 0.30, "ml": 0.25, "momentum": 0.15, "news": 0.20, "calendar": 0.10}
        confirmations = ["ADX strong: 32", "ML bullish confirmed"]
        alerts = ["DXY rising +0.3%"]
    
    # Mock data
    mock_tech = {
        "rsi": {"value": 62, "level": "neutral"},
        "macd": {"histogram": 0.5, "signal": "bullish"},
        "ema": {"ema9": 2912, "ema21": 2908, "ema50": 2900},
        "bollinger": {"upper": 2930, "middle": 2915, "lower": 2900, "position": 0.5},
    }
    
    mock_momentum = {
        "adx": {"adx_value": 32, "plus_di": 28, "minus_di": 18},
        "atr": {"atr_value": 28.5, "atr_trend": "stable"},
        "volume": {"volume_ratio": 1.2, "volume_classification": "high"},
    }
    
    mock_news = {
        "headlines": ["Fed signals patience", "Gold steady"],
        "dxy": {"value": 103.8, "change_24h": 0.3, "trend": "rising"},
        "vix": {"value": 18.2, "level": "normal"},
        "yields": {"value": 4.25, "trend": "stable"},
        "sentiment": {"normalized": 0.1, "label": "neutral"},
    }
    
    mock_calendar = {
        "phase": "normal",
        "bias": "NEUTRAL",
        "score": 50,
    }
    
    mock_candles = [
        {"time": "2026-03-05T10:00:00", "open": 2910, "high": 2918, "low": 2908, "close": 2915, "tick_volume": 1234},
    ]
    
    mock_price = {"bid": 2915.50, "ask": 2915.80, "spread": 3.0}
    
    mock_positions = []
    
    mock_session = {
        "session_name": "London",
        "hour_utc": 10,
        "today_trades": 2,
        "today_wins": 1,
        "today_losses": 1,
        "today_pnl": 12.50,
    }
    
    mock_volatility = {"status": "NORMAL"}
    
    # Build package
    package = build_data_package(
        brain_result=MockBrainResult(),
        tech_data=mock_tech,
        ml_data={},
        momentum_data=mock_momentum,
        news_data=mock_news,
        calendar_data=mock_calendar,
        h1_candles=mock_candles,
        m5_candles=mock_candles,
        current_price=mock_price,
        positions=mock_positions,
        session_context=mock_session,
        volatility_status=mock_volatility,
    )
    
    import json
    print("\nGenerated package:")
    print(json.dumps(package, indent=2, default=str))
    
    # Estimate tokens
    json_str = json.dumps(package)
    est_tokens = len(json_str) // 4
    print(f"\nEstimated tokens: ~{est_tokens}")


if __name__ == "__main__":
    _test_data_builder()
