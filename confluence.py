"""
CONFLUENCE ENGINE - Decision System
Combines Technical Analysis + News Score + ML Prediction
"""

from dataclasses import dataclass
from typing import Dict, Tuple, Optional
from datetime import datetime
import config


@dataclass
class ConfluenceResult:
    """Confluence analysis result"""
    final_score: float
    decision: str
    confidence: str
    technical_score: float
    news_score: float
    ml_score: float
    ml_probability: float
    ml_included: bool
    breakdown: Dict[str, float]
    timestamp: datetime


def calculate_confluence_score(
    technical_score: float,
    news_score: float,
    ml_score: float,
    ml_probability: float
) -> Tuple[float, Dict[str, float], bool]:
    """
    Calculate final confluence score.
    
    Args:
        technical_score: Technical score (0-100)
        news_score: Hybrid news score (0-100)
        ml_score: ML score (0-100)
        ml_probability: ML probability (0-1)
    
    Returns:
        Tuple: (final_score, breakdown_dict, ml_included)
    """
    # Validate inputs
    technical_score = max(0, min(100, technical_score))
    news_score = max(0, min(100, news_score))
    ml_score = max(0, min(100, ml_score))
    ml_probability = max(0, min(1, ml_probability))
    
    # Check if ML is reliable
    ml_included = ml_probability >= config.ML_MIN_PROBABILITY
    
    if ml_included:
        # Include ML in confluence
        final_score = (
            technical_score * config.WEIGHT_TECHNICAL +
            news_score * config.WEIGHT_NEWS +
            ml_score * config.WEIGHT_ML
        )
        breakdown = {
            'technical': technical_score * config.WEIGHT_TECHNICAL,
            'news': news_score * config.WEIGHT_NEWS,
            'ml': ml_score * config.WEIGHT_ML,
            'weights': {
                'technical': config.WEIGHT_TECHNICAL,
                'news': config.WEIGHT_NEWS,
                'ml': config.WEIGHT_ML
            }
        }
    else:
        # ML not reliable - ignore
        final_score = (
            technical_score * config.WEIGHT_TECHNICAL_NO_ML +
            news_score * config.WEIGHT_NEWS_NO_ML
        )
        breakdown = {
            'technical': technical_score * config.WEIGHT_TECHNICAL_NO_ML,
            'news': news_score * config.WEIGHT_NEWS_NO_ML,
            'ml': 0,
            'weights': {
                'technical': config.WEIGHT_TECHNICAL_NO_ML,
                'news': config.WEIGHT_NEWS_NO_ML,
                'ml': 0
            },
            'ml_ignored_reason': f'probability {ml_probability:.2f} < {config.ML_MIN_PROBABILITY}'
        }
    
    return round(final_score, 2), breakdown, ml_included


def make_decision(final_score: float) -> Tuple[str, str]:
    """
    Make decision based on final score.
    
    Args:
        final_score: Confluence score (0-100)
    
    Returns:
        Tuple: (decision, confidence)
        - decision: STRONG_BUY, BUY, HOLD, SELL, STRONG_SELL
        - confidence: high, medium, low
    """
    if final_score > config.STRONG_BUY_THRESHOLD:
        return "STRONG_BUY", "high"
    
    elif final_score > config.BUY_THRESHOLD:
        return "BUY", "medium"
    
    elif final_score > config.WEAK_BUY_THRESHOLD:
        # WEAK_BUY treated as HOLD
        return "HOLD", "low"
    
    elif final_score >= config.NEUTRAL_LOW:
        return "HOLD", "low"
    
    elif final_score >= config.WEAK_SELL_THRESHOLD:
        # WEAK_SELL treated as HOLD (score 35-45)
        return "HOLD", "low"
    
    elif final_score >= config.STRONG_SELL_THRESHOLD:
        # SELL (score 30-35)
        return "SELL", "medium"
    
    else:
        # Score < 30 = STRONG_SELL
        return "STRONG_SELL", "high"


def analyze_confluence(
    technical_score: float,
    news_score: float,
    ml_score: float,
    ml_probability: float
) -> ConfluenceResult:
    """
    Complete confluence analysis.
    
    Args:
        technical_score: Technical score (0-100)
        news_score: Hybrid news score (0-100)
        ml_score: ML score (0-100)
        ml_probability: ML probability (0-1)
    
    Returns:
        ConfluenceResult with all data
    """
    # Calculate confluence
    final_score, breakdown, ml_included = calculate_confluence_score(
        technical_score, news_score, ml_score, ml_probability
    )
    
    # Make decision
    decision, confidence = make_decision(final_score)
    
    return ConfluenceResult(
        final_score=final_score,
        decision=decision,
        confidence=confidence,
        technical_score=technical_score,
        news_score=news_score,
        ml_score=ml_score,
        ml_probability=ml_probability,
        ml_included=ml_included,
        breakdown=breakdown,
        timestamp=datetime.now()
    )


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


# ============================================================================
# VALIDATION WITH EXAMPLES
# ============================================================================

def validate_examples():
    """Validate the system with user examples"""
    print("=" * 60)
    print("🧪 CONFLUENCE ENGINE VALIDATION")
    print("=" * 60)
    
    examples = [
        {
            'name': 'Example 1 (Strong buy)',
            'technical': 78,
            'news': 75,
            'ml': 68,
            'ml_prob': 0.62
        },
        {
            'name': 'Example 2 (Neutral)',
            'technical': 52,
            'news': 48,
            'ml': 55,
            'ml_prob': 0.58
        },
        {
            'name': 'Example 3 (ML ignored)',
            'technical': 65,
            'news': 60,
            'ml': 45,
            'ml_prob': 0.51
        },
        {
            'name': 'Example 4 (Strong sell)',
            'technical': 22,
            'news': 28,
            'ml': 35,
            'ml_prob': 0.60
        },
        {
            'name': 'Example 5 (Mixed signals)',
            'technical': 70,
            'news': 40,
            'ml': 60,
            'ml_prob': 0.56
        }
    ]
    
    for ex in examples:
        print(f"\n📊 {ex['name']}")
        print(f"   Inputs: Tech={ex['technical']} News={ex['news']} ML={ex['ml']} (prob={ex['ml_prob']})")
        
        result = analyze_confluence(
            ex['technical'], ex['news'], ex['ml'], ex['ml_prob']
        )
        
        print(f"   ML included: {'✅ Yes' if result.ml_included else '❌ No'}")
        print(f"   Breakdown:")
        print(f"      Tech contribution: {result.breakdown['technical']:.2f}")
        print(f"      News contribution: {result.breakdown['news']:.2f}")
        print(f"      ML contribution:   {result.breakdown['ml']:.2f}")
        print(f"   Final Score: {result.final_score:.2f}")
        print(f"   Decision: {result.decision} (confidence: {result.confidence})")
        print(f"   Action: {'🔔 TRADE' if is_actionable_signal(result.decision) else '⏸️ WAIT'}")


if __name__ == "__main__":
    validate_examples()
