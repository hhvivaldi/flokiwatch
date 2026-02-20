"""
GPT CONFIDENCE VALIDATOR - AI Confidence Validator
Receives the complete Central Brain result and adjusts confidence (CONFIRM/BOOST/REDUCE).
Does not alter score, scenario or direction — only confidence.

Model: gpt-4o-mini (same as headlines)
Fallback: if GPT fails → implicit CONFIRM (original confidence maintained)
Smart cache: only calls GPT if scores changed significantly or scenario changed
"""

import json
import config
from logger import log
from typing import Dict, Optional

try:
    from openai import OpenAI
    _openai_available = True
except ImportError:
    _openai_available = False


# ============================================================================
# SMART CACHE
# ============================================================================

_cache = {
    "last_scores": None,       # Dict of 5 pillars {tech, ml, momentum, news, calendar}
    "last_scenario": None,     # Last identified scenario
    "last_result": None,       # Last GPT response
}


def _should_call_gpt(current_scores: Dict[str, float], current_scenario: str) -> bool:
    """
    Check if GPT should be called or cache reused.
    Calls GPT if:
    - Cache empty (first call)
    - Scenario changed
    - Any pillar changed >= threshold points
    """
    threshold = getattr(config, 'GPT_CONFIDENCE_CACHE_THRESHOLD', 5)
    
    if _cache["last_scores"] is None or _cache["last_result"] is None:
        return True
    
    if current_scenario != _cache["last_scenario"]:
        return True
    
    for pillar, score in current_scores.items():
        last_score = _cache["last_scores"].get(pillar, 50.0)
        if abs(score - last_score) >= threshold:
            return True
    
    return False


def _update_cache(scores: Dict[str, float], scenario: str, result: Dict):
    """Update cache with data from last GPT call."""
    _cache["last_scores"] = scores.copy()
    _cache["last_scenario"] = scenario
    _cache["last_result"] = result.copy()


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

GPT_SYSTEM_PROMPT = """You validate confidence for a gold (XAU/USD) trading system. The system already computed pillar scores, a final score, and a confidence level. You ONLY adjust confidence.

Respond JSON: {"action": "CONFIRM|BOOST|REDUCE", "adjustment": 0-15, "reason": "one sentence"}

TRUST THE SCORES. The SYSTEM ASSESSMENT section contains pre-computed labels (NEUTRAL/BULLISH/BEARISH). Use those labels exactly. Do NOT re-interpret raw indicators to override the scores.

WHEN TO CONFIRM (this should be your most common action, ~50-60%):
- Most pillars are neutral (35-65) with no extreme conditions
- Mixed signals already reflected in the score
- No specific red flag — general caution is NOT a reason to REDUCE

WHEN TO REDUCE (~25-35% of cases):
- Volume is LOW (<0.6x) during a breakout signal — false breakout risk
- RSI >75 or <25 WITHOUT strong ADX — exhaustion, not impulse
- Calendar pre_event or during_event phase — upcoming volatility risk
- ML confidence borderline (55-60%) but system gave high confidence

WHEN TO BOOST (~10-20% of cases):
- All pillars align strongly in same direction
- MISSED OPPORTUNITY pattern detected in cycle history with score 58-64
- Post-event calendar bias confirms the direction

HARD RULES:
- Max adjustment: 15. Use 10-15 only for clear reasons. Use 3-7 for subtle ones.
- When in doubt → CONFIRM.
- NEVER boost during pre_event, during_event, or COOLING_DOWN.
- BOOST on HOLD decision is meaningless → use adjustment=0.
- DXY changes are ALREADY in the News score. Do not cite DXY separately.
- If the assessment says a pillar is NEUTRAL, do NOT call it bearish or bullish in your reason."""


# ============================================================================
# USER PROMPT BUILDER
# ============================================================================

def _build_user_prompt(brain_result, tech_data: Dict, ml_data: Dict,
                       momentum_data: Dict, news_data: Dict,
                       calendar_data: Optional[Dict], volatility_status: Optional[Dict],
                       current_price: float, cycle_history: str = "") -> str:
    """Build compact user prompt with all granular data."""
    
    # Technical details
    rsi = tech_data.get("rsi", {})
    macd = tech_data.get("macd", {})
    ema = tech_data.get("ema", {})
    bb = tech_data.get("bollinger", {})
    stoch = tech_data.get("stochastic", {})
    
    div = macd.get("divergence", {})
    if div.get("detected"):
        div_info = f"{div.get('type', 'unknown')} ({div.get('bars_since', '?')} bars ago)"
    else:
        div_info = "none"
    
    bb_squeeze_info = " (SQUEEZE)" if bb.get("squeeze") else ""
    
    # Momentum details
    adx = momentum_data.get("adx", {})
    vol = momentum_data.get("volume", {})
    candles = momentum_data.get("candles", {})
    breakout = momentum_data.get("breakout", {})
    atr = momentum_data.get("atr", {})
    
    if breakout.get("breakout_detected"):
        breakout_info = f"YES ({breakout.get('breakout_type', 'unknown')})"
    else:
        breakout_info = "none"
    
    # News details
    dxy = news_data.get("dxy", {})
    yields = news_data.get("yields", {})
    vix = news_data.get("vix", {})
    sentiment = news_data.get("sentiment", {})
    
    # Calendar details
    cal = calendar_data or {}
    cal_event = "none"
    closest = cal.get("closest_event")
    if closest and isinstance(closest, dict):
        cal_event = closest.get("name", "unknown")
    
    # Volatility details
    vol_st = volatility_status or {}
    vol_status_str = vol_st.get("status", "NORMAL")
    vol_desc = vol_st.get("description", "Normal")
    
    # Volume flag
    vol_ratio = vol.get('volume_ratio', 1.0)
    if vol_ratio < 0.6:
        vol_flag = f"LOW ({vol_ratio:.1f}x avg) — genuine concern for false breakout"
    elif vol_ratio < 0.8:
        vol_flag = f"BELOW AVG ({vol_ratio:.1f}x)"
    elif vol_ratio > 1.5:
        vol_flag = f"HIGH ({vol_ratio:.1f}x avg) — confirms momentum"
    else:
        vol_flag = f"NORMAL ({vol_ratio:.1f}x)"
    
    # Build SYSTEM ASSESSMENT (pre-digested, replaces raw confirmations/alerts)
    tech_label = _score_label('Technical', tech_data.get('score', 50))
    ml_label = _score_label('ML', ml_data.get('score', 50))
    mom_label = _score_label('Momentum', momentum_data.get('score', 50))
    news_label = _score_label('News', news_data.get('score', 50))
    cal_label = _score_label('Calendar', cal.get('score', 50))
    
    # Count how many pillars are extreme
    scores_list = [
        tech_data.get('score', 50), ml_data.get('score', 50),
        momentum_data.get('score', 50), news_data.get('score', 50),
        cal.get('score', 50),
    ]
    n_neutral = sum(1 for s in scores_list if 35 <= s <= 65)
    n_extreme = 5 - n_neutral
    
    if n_neutral >= 4:
        assessment_note = f"Most pillars neutral ({n_neutral}/5) — no strong directional signal. CONFIRM is likely appropriate."
    elif n_extreme >= 3:
        assessment_note = f"Multiple extreme pillars ({n_extreme}/5) — strong signal, evaluate if confidence matches."
    else:
        assessment_note = f"Mixed signals ({n_neutral} neutral, {n_extreme} extreme) — brain score already reflects this mix."
    
    prompt = f"""Market snapshot for XAU/USD at {current_price:.2f}:

BRAIN DECISION: {brain_result.decision} | Score: {brain_result.final_score:.1f}/100 | Confidence: {brain_result.confidence:.1f}/100
SCENARIO: {brain_result.scenario} — {brain_result.scenario_description}

SYSTEM ASSESSMENT (use THESE labels, do NOT re-interpret):
  {tech_label}
  {ml_label}
  {mom_label}
  {news_label}
  {cal_label}
  Volume: {vol_flag}
  Volatility: {vol_status_str}
  Calendar phase: {cal.get('phase', 'normal')} | Bias: {cal.get('bias', 'NEUTRAL')}
  → {assessment_note}

KEY INDICATORS (for context only — scores above are authoritative):
  RSI: {rsi.get('value', 50):.0f} | ADX: {adx.get('adx_value', 0):.0f} | MACD: {macd.get('signal', 'neutral')} | Divergence: {div_info}
  ML confidence: {ml_data.get('max_confidence', 0.5):.0%} | ML prediction: {ml_data.get('prediction', 'neutral')}
  DXY: {dxy.get('change_24h', 0):+.2f}% | VIX: {vix.get('value', 0)} | Consecutive candles: {candles.get('consecutive_count', 0)} {candles.get('consecutive_direction', 'neutral')}"""
    
    if cycle_history:
        prompt += f"\n\n{cycle_history}"
    
    return prompt


def _score_label(name: str, score) -> str:
    """Return human-readable label for a pillar score."""
    s = float(score) if score is not None else 50.0
    if s > 65:
        return f"{name}={s:.0f} (BULLISH)"
    if s < 35:
        return f"{name}={s:.0f} (BEARISH)"
    return f"{name}={s:.0f} (NEUTRAL)"


# ============================================================================
# GPT CALL
# ============================================================================

def _call_gpt(user_prompt: str) -> Optional[Dict]:
    """
    Call GPT-4o-mini and return validated response.
    Returns None if it fails (fallback to CONFIRM).
    """
    api_key = getattr(config, 'OPENAI_API_KEY', '')
    if not api_key:
        return None
    
    try:
        client = OpenAI(api_key=api_key)
        
        model = getattr(config, 'GPT_MODEL', 'gpt-4o-mini')
        temperature = getattr(config, 'GPT_CONFIDENCE_TEMPERATURE', 0.1)
        timeout = getattr(config, 'GPT_CONFIDENCE_TIMEOUT', 12)
        
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": GPT_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            response_format={"type": "json_object"},
            timeout=timeout,
        )
        
        raw = response.choices[0].message.content
        data = json.loads(raw)
        
        # Schema validation
        action = data.get("action", "CONFIRM").upper()
        if action not in ("CONFIRM", "BOOST", "REDUCE"):
            log.warning(f"GPT Confidence: invalid action '{action}', using CONFIRM")
            action = "CONFIRM"
        
        adjustment = data.get("adjustment", 0)
        if not isinstance(adjustment, (int, float)):
            adjustment = 0
        
        max_adj = getattr(config, 'GPT_CONFIDENCE_MAX_ADJUSTMENT', 15)
        adjustment = max(0, min(int(adjustment), max_adj))
        
        reason = data.get("reason", "no reason provided")
        if not isinstance(reason, str):
            reason = str(reason)
        
        return {
            "action": action,
            "adjustment": adjustment,
            "reason": reason[:200],
            "from_cache": False,
            "error": None,
        }
        
    except json.JSONDecodeError as e:
        log.warning(f"GPT Confidence: invalid JSON — {e}")
        return None
    except Exception as e:
        log.warning(f"GPT Confidence: error — {e}")
        return None


# ============================================================================
# PUBLIC FUNCTION
# ============================================================================

def validate_confidence(brain_result, tech_data: Dict, ml_data: Dict,
                        momentum_data: Dict, news_data: Dict,
                        calendar_data: Optional[Dict] = None,
                        volatility_status: Optional[Dict] = None,
                        current_price: float = 0,
                        cycle_history: str = "") -> Dict:
    """
    Validate Central Brain confidence using GPT-4o-mini.
    
    Args:
        brain_result: BrainResult from central_brain
        tech_data, ml_data, momentum_data, news_data: detailed pillar data
        calendar_data: economic calendar data
        volatility_status: Volatility Guard status
        current_price: current price
    
    Returns:
        Dict with action, adjustment, reason, from_cache, error
    """
    fallback = {
        "action": "CONFIRM",
        "adjustment": 0,
        "reason": "fallback — GPT unavailable",
        "from_cache": False,
        "error": None,
    }
    
    # Check if GPT is available
    if not _openai_available:
        fallback["error"] = "openai package not installed"
        return fallback
    
    if not getattr(config, 'USE_GPT_CONFIDENCE', False):
        fallback["error"] = "USE_GPT_CONFIDENCE disabled"
        return fallback
    
    # Extract current pillar scores
    cal = calendar_data or {}
    current_scores = {
        "technical": tech_data.get("score", 50.0),
        "ml": ml_data.get("score", 50.0),
        "momentum": momentum_data.get("score", 50.0),
        "news": news_data.get("score", 50.0),
        "calendar": cal.get("score", 50.0),
    }
    current_scenario = brain_result.scenario
    
    # Smart cache: check if GPT call is needed
    if not _should_call_gpt(current_scores, current_scenario):
        cached = _cache["last_result"].copy()
        cached["from_cache"] = True
        log.debug("   🤖 GPT Confidence: using cache (stable scores)")
        return cached
    
    # Build prompt and call GPT
    user_prompt = _build_user_prompt(
        brain_result, tech_data, ml_data, momentum_data,
        news_data, calendar_data, volatility_status, current_price,
        cycle_history=cycle_history
    )
    
    # Log score labels to confirm the fix is active
    cal = calendar_data or {}
    labels = " | ".join([
        _score_label('Tech', tech_data.get('score', 50)),
        _score_label('ML', ml_data.get('score', 50)),
        _score_label('Momentum', momentum_data.get('score', 50)),
        _score_label('News', news_data.get('score', 50)),
        _score_label('Calendar', cal.get('score', 50)),
    ])
    log.debug(f"   🤖 GPT Confidence: SCORE LABELS → {labels}")
    
    result = _call_gpt(user_prompt)
    
    if result is None:
        fallback["error"] = "GPT call failed"
        return fallback
    
    # Update cache
    _update_cache(current_scores, current_scenario, result)
    
    return result
