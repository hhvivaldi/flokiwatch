"""
CYCLE MEMORY - Analysis Cycle Memory
Stores the last N analyses and provides temporal context to the GPT Confidence Validator.

Detects patterns such as:
- Consecutive HOLDs during price rise (missed opportunity)
- Persistent strong momentum
- Unstable scenario (choppy market)
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class CycleSnapshot:
    """Snapshot of an analysis cycle"""
    timestamp: datetime
    score: float
    confidence: float
    decision: str
    scenario: str
    tech_score: float
    ml_score: float
    momentum_score: float
    momentum_direction: str
    momentum_strength: str
    news_score: float
    current_price: float


class CycleMemory:
    """Analysis cycle memory with temporal pattern detection"""
    
    def __init__(self, max_cycles: int = 36):  # 36 ciclos × 5 min = 3 horas
        self.cycles: deque = deque(maxlen=max_cycles)
    
    def add(self, snapshot: CycleSnapshot):
        """Add a snapshot to the history"""
        self.cycles.append(snapshot)
    
    def get_trend_summary(self) -> Dict:
        """
        Analyze temporal patterns in the last N cycles.
        
        Returns:
            Dict with trend metrics and detected patterns
        """
        if len(self.cycles) < 3:
            return {"pattern": "insufficient_data"}
        
        recent = list(self.cycles)
        
        # 1. Count consecutive HOLDs (from most recent backwards)
        consecutive_holds = 0
        for c in reversed(recent):
            if c.decision == "HOLD":
                consecutive_holds += 1
            else:
                break
        
        # 2. Consecutive strong momentum (score >= 75 + strong/very_strong)
        strong_momentum_streak = 0
        for c in reversed(recent):
            if c.momentum_score >= 75 and c.momentum_strength in ("strong", "very_strong"):
                strong_momentum_streak += 1
            else:
                break
        
        # 3. Price trend (last 6 cycles = ~30 min)
        if len(recent) >= 6:
            price_start = recent[-6].current_price
            price_end = recent[-1].current_price
            price_change_pct = ((price_end - price_start) / price_start) * 100
        else:
            price_change_pct = 0
        
        # 4. Recent average score (last 6 cycles)
        recent_6 = list(self.cycles)[-6:]
        recent_scores = [c.score for c in recent_6]
        avg_score = sum(recent_scores) / len(recent_scores) if recent_scores else 50
        
        # 5. Stable or unstable scenario
        recent_scenarios = [c.scenario for c in recent_6]
        unique_scenarios = len(set(recent_scenarios))
        scenario_stability = "stable" if unique_scenarios <= 2 else "unstable"
        
        # 6. Detect "missed opportunity" pattern
        missed_opportunity = (
            consecutive_holds >= 12 and          # 1 hour of HOLDs
            strong_momentum_streak >= 4 and      # 20 min of strong momentum
            avg_score >= 55 and                   # Score consistently above neutral
            price_change_pct > 0.3                # Price rose >0.3%
        )
        
        return {
            "consecutive_holds": consecutive_holds,
            "strong_momentum_streak": strong_momentum_streak,
            "price_change_pct": round(price_change_pct, 3),
            "avg_score_30min": round(avg_score, 1),
            "scenario_stability": scenario_stability,
            "unique_scenarios_30min": unique_scenarios,
            "missed_opportunity": missed_opportunity,
            "total_cycles": len(self.cycles),
        }
    
    def format_for_gpt(self) -> str:
        """
        Format compact summary to include in the GPT Confidence Validator prompt.
        
        Returns:
            Formatted string with temporal context
        """
        summary = self.get_trend_summary()
        if summary.get("pattern") == "insufficient_data":
            return "CYCLE HISTORY: insufficient data (< 3 cycles)"
        
        lines = [
            f"CYCLE HISTORY ({summary['total_cycles']} cycles, ~{summary['total_cycles'] * 5} min):",
            f"  Consecutive HOLDs: {summary['consecutive_holds']}",
            f"  Strong momentum streak: {summary['strong_momentum_streak']} cycles",
            f"  Price change (30min): {summary['price_change_pct']:+.3f}%",
            f"  Avg score (30min): {summary['avg_score_30min']:.1f}",
            f"  Scenario stability: {summary['scenario_stability']} ({summary['unique_scenarios_30min']} unique)",
        ]
        if summary["missed_opportunity"]:
            lines.append("  ⚠️ MISSED OPPORTUNITY PATTERN DETECTED")
        return "\n".join(lines)
    
    def clear(self):
        """Clear history (e.g.: when restarting session)"""
        self.cycles.clear()
