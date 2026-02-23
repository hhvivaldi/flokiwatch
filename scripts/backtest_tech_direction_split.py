"""
BACKTEST: Tech Direction / Tech Risk Split — 4-way comparison
==============================================================
Tests the Tech Direction + Tech Risk split against production baseline.

Variants:
    Baseline: Current tech_score (production code, no changes)
    A: Tech Direction + Tech Risk, conflict scenario DISABLED
    B: Tech Direction + Tech Risk, conflict threshold 75
    C: Tech Direction + Tech Risk, conflict threshold 85

Period: 2025-08-18 → 2026-02-20 23:59

Usage:
    python scripts/backtest_tech_direction_split.py

Output:
    data/backtest_tech_direction_<timestamp>.txt
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import MetaTrader5 as mt5
import config

from scripts.run_backtest import (
    BacktestMLPredictor,
    SimTrade,
    collect_all_data,
    connect,
    compute_h4_features,
    compute_m5_features,
    compute_m5_status,
    compute_volatility_status,
    compute_m5_reversal,
    compute_mtf_trend,
    simulate_trades_concurrent,
    NEUTRAL_CALENDAR,
    H1_WARMUP,
)
from technical_analyzer import (
    calculate_indicators, analyze_technical_detailed, get_atr_value,
    calculate_tech_direction_score, get_tech_risk_data,
)
from momentum_detector import analyze_momentum
from central_brain import (
    analyze_with_brain, is_actionable_signal, get_trade_direction,
    _check_mtf_trend_alignment,
)
from risk_manager import calculate_sl_tp
from support_resistance import (
    detect_zones_dual, get_sr_context, adjust_sl_tp_for_sr, is_near_strong_zone,
)

# ============================================================================
# CONFIG
# ============================================================================
BT_START = datetime(2025, 8, 18)
BT_END   = datetime(2026, 2, 20, 23, 59)

# Conflict scenario thresholds for variants B and C
CONFLICT_ML_MAX = 40.0

NEUTRAL_NEWS = {
    "score": 50.0,
    "dxy": {"value": 104.0, "change_24h": 0.0, "trend": "stable"},
    "yields": {"value": 4.5, "change_24h": 0.0, "trend": "stable"},
    "vix": {"value": 17.0, "level": "low"},
    "sentiment": {"headlines_score": 50, "normalized": 0},
    "high_impact_news_soon": False, "geopolitical_risk": "low", "anomalies": [],
}


# ============================================================================
# TECH RISK CONFIDENCE PENALTIES
# ============================================================================

def apply_tech_risk_penalties(confidence: float, risk_data: Dict, decision: str) -> Tuple[float, List[str]]:
    """
    Apply Tech Risk penalties to confidence based on RSI and Bollinger position.
    
    Penalties (from design v2):
        BUY: RSI>70 → -8, RSI>80 → -12, BB>80% → -8, BB>95% → -12
        SELL: RSI<30 → -5, RSI<20 → -8, BB<20% → -5, BB<5% → -8
    
    Returns:
        Tuple: (adjusted_confidence, list_of_alerts)
    """
    alerts = []
    rsi = risk_data.get('rsi', 50)
    bb_pos = risk_data.get('bb_position', 50)
    
    if decision in ("BUY", "STRONG_BUY"):
        # RSI overbought penalties
        if rsi > 80:
            confidence -= 12
            alerts.append(f"RSI extreme overbought ({rsi:.0f}) - high reversal risk")
        elif rsi > 70:
            confidence -= 8
            alerts.append(f"RSI overbought ({rsi:.0f}) - caution")
        
        # Bollinger upper band penalties
        if bb_pos > 95:
            confidence -= 12
            alerts.append(f"Price at BB extreme ({bb_pos:.0f}%) - high reversal risk")
        elif bb_pos > 80:
            confidence -= 8
            alerts.append(f"Price near BB upper ({bb_pos:.0f}%) - caution")
    
    elif decision in ("SELL", "STRONG_SELL"):
        # RSI oversold penalties (lighter for SELLs per diagnostic)
        if rsi < 20:
            confidence -= 8
            alerts.append(f"RSI extreme oversold ({rsi:.0f}) - high bounce risk")
        elif rsi < 30:
            confidence -= 5
            alerts.append(f"RSI oversold ({rsi:.0f}) - caution")
        
        # Bollinger lower band penalties
        if bb_pos < 5:
            confidence -= 8
            alerts.append(f"Price at BB extreme ({bb_pos:.0f}%) - high bounce risk")
        elif bb_pos < 20:
            confidence -= 5
            alerts.append(f"Price near BB lower ({bb_pos:.0f}%) - caution")
    
    return max(0, min(100, confidence)), alerts


# ============================================================================
# MONKEY-PATCH FUNCTIONS
# ============================================================================

# Store original functions for clean restore
_ORIGINAL_FUNCS = {}


def _patch_tech_direction(conflict_threshold: Optional[float] = None):
    """
    Patch central_brain to use Tech Direction instead of tech_score.
    
    Args:
        conflict_threshold: If None, disable conflict scenario entirely.
                           If set, use this threshold for ml_vs_tech_conflito.
    """
    import central_brain as cb
    
    # Save originals ONCE
    if 'calculate_final_score' not in _ORIGINAL_FUNCS:
        _ORIGINAL_FUNCS['calculate_final_score'] = cb._calculate_final_score
    if 'calculate_confidence' not in _ORIGINAL_FUNCS:
        _ORIGINAL_FUNCS['calculate_confidence'] = cb._calculate_confidence
    if 'identify_scenario' not in _ORIGINAL_FUNCS:
        _ORIGINAL_FUNCS['identify_scenario'] = cb._identify_scenario
    
    orig_calc_score = _ORIGINAL_FUNCS['calculate_final_score']
    orig_calc_conf = _ORIGINAL_FUNCS['calculate_confidence']
    orig_identify = _ORIGINAL_FUNCS['identify_scenario']
    
    # Patched _calculate_final_score: use direction_score instead of score
    def _patched_calculate_final_score(tech_data, ml_data, momentum_data, news_data,
                                        weights, calendar_data=None):
        # Get direction_score if available, else fall back to score
        tech_score = tech_data.get("direction_score", tech_data.get("score", 50))
        ml_score = ml_data.get("score", 50)
        momentum_score = momentum_data.get("score", 50)
        news_score = news_data.get("score", 50)
        calendar_score = calendar_data.get("score", 50) if calendar_data else 50
        
        final_score = (
            tech_score * weights.get("technical", 0.30) +
            ml_score * weights.get("ml", 0.25) +
            momentum_score * weights.get("momentum", 0.15) +
            news_score * weights.get("news", 0.20) +
            calendar_score * weights.get("calendar", 0.10)
        )
        return final_score
    
    # Patched _calculate_confidence: add Tech Risk penalties
    def _patched_calculate_confidence(final_score, decision, tech_data, ml_data,
                                       momentum_data, news_data, scenario_mult,
                                       volatility_status=None, m5_data=None,
                                       sr_data=None, calendar_data=None):
        # Call original confidence calculation
        base_conf = orig_calc_conf(
            final_score, decision, tech_data, ml_data, momentum_data, news_data,
            scenario_mult, volatility_status, m5_data, sr_data, calendar_data
        )
        
        # Apply Tech Risk penalties
        risk_data = tech_data.get("risk_data", {})
        if risk_data:
            adjusted_conf, _ = apply_tech_risk_penalties(base_conf, risk_data, decision)
            return adjusted_conf
        return base_conf
    
    # Patched _identify_scenario: use direction_score for conflict detection
    def _patched_identify_scenario(tech_data, ml_data, momentum_data, news_data,
                                    momentum_strength, calendar_data=None,
                                    volatility_status=None, sr_data=None):
        # If conflict disabled, skip conflict detection entirely
        if conflict_threshold is None:
            # Call original but never trigger ml_vs_tech_conflito
            result = orig_identify(
                tech_data, ml_data, momentum_data, news_data, momentum_strength,
                calendar_data, volatility_status, sr_data
            )
            # If original returned ml_vs_tech_conflito, downgrade to sinais_conflitantes
            if result[0] == "ml_vs_tech_conflito":
                return ("sinais_conflitantes", "Tech/ML conflict (scenario disabled)", 1.0)
            return result
        
        # Use direction_score for conflict threshold check
        tech_score = tech_data.get("direction_score", tech_data.get("score", 50))
        ml_score = ml_data.get("score", 50)
        
        # Check conflict with new threshold
        if tech_score >= conflict_threshold and ml_score <= CONFLICT_ML_MAX:
            # Trigger conflict scenario with adjusted weights
            return (
                "ml_vs_tech_conflito",
                f"Tech Direction ({tech_score:.0f}) vs ML ({ml_score:.0f}) conflict",
                0.95,
            )
        
        # Otherwise call original
        return orig_identify(
            tech_data, ml_data, momentum_data, news_data, momentum_strength,
            calendar_data, volatility_status, sr_data
        )
    
    # Apply patches
    cb._calculate_final_score = _patched_calculate_final_score
    cb._calculate_confidence = _patched_calculate_confidence
    cb._identify_scenario = _patched_identify_scenario


def _unpatch_tech_direction():
    """Restore original central_brain functions."""
    import central_brain as cb
    
    if 'calculate_final_score' in _ORIGINAL_FUNCS:
        cb._calculate_final_score = _ORIGINAL_FUNCS['calculate_final_score']
    if 'calculate_confidence' in _ORIGINAL_FUNCS:
        cb._calculate_confidence = _ORIGINAL_FUNCS['calculate_confidence']
    if 'identify_scenario' in _ORIGINAL_FUNCS:
        cb._identify_scenario = _ORIGINAL_FUNCS['identify_scenario']


def _clear_patch_cache():
    """Clear the original functions cache (call before Baseline run)."""
    _ORIGINAL_FUNCS.clear()


# ============================================================================
# CORE BACKTEST LOOP
# ============================================================================

def _run_loop(data: Dict, bt_predictor, label: str,
              track_tech_direction: bool = False) -> Tuple[List[SimTrade], Dict]:
    """
    Run backtest loop.
    
    Args:
        data: Dict with h1, h4, m5, d1 DataFrames
        bt_predictor: ML predictor instance
        label: Run label for logging
        track_tech_direction: If True, also record tech_direction scores
    
    Returns:
        Tuple: (trades_list, stats_dict)
    """
    df_h1 = data['h1'].copy()
    df_h4 = data['h4'].copy()
    df_d1 = data.get('d1', pd.DataFrame()).copy()
    df_m5 = data['m5'].copy()

    trades: List[SimTrade] = []
    open_trades: List[SimTrade] = []
    ticket_counter = 5000000
    
    # Track conflict scenario triggers
    conflict_detections = 0

    last_trade_time = {'BUY': None, 'SELL': None}
    last_close_type = {'BUY': None, 'SELL': None}

    bt_mask = (df_h1['datetime'] >= BT_START) & (df_h1['datetime'] <= BT_END)
    bt_indices = df_h1[bt_mask].index.tolist()

    if not bt_indices:
        print(f"  ❌ No H1 candles for {label}")
        return [], {'conflict_detections': 0}

    total = len(bt_indices)
    print(f"\n🔄 {label}: {total} H1 candles ({BT_START.date()} → {BT_END.date()})")

    for count, idx in enumerate(bt_indices):
        if idx < H1_WARMUP:
            continue

        h1_candle = df_h1.iloc[idx]
        h1_time = h1_candle['datetime']
        if hasattr(h1_time, 'to_pydatetime'):
            h1_time = h1_time.to_pydatetime()
        current_price = float(h1_candle['close'])

        if count % 200 == 0:
            print(f"   {count/total*100:.0f}% — {h1_time.strftime('%Y-%m-%d %H:%M')} — {len(trades)} trades")

        # Expire open trades
        still_open = []
        for t in open_trades:
            if t.close_time and t.close_time <= h1_time:
                last_close_type[t.direction] = (
                    "tp" if "tp" in t.close_reason else
                    "trailing" if t.profit_pips >= 0 else "sl"
                )
                last_trade_time[t.direction] = t.close_time
            else:
                still_open.append(t)
        open_trades = still_open

        # Indicators
        h1_slice = df_h1.iloc[:idx + 1].copy()
        h1_slice = calculate_indicators(h1_slice)
        if len(h1_slice) < 50:
            continue

        # Pillars
        tech_data = analyze_technical_detailed(h1_slice)

        bt_predictor.set_h4_features(compute_h4_features(df_h4, h1_time))
        bt_predictor.set_m5_features(compute_m5_features(df_m5, h1_time))

        try:
            ml_result = bt_predictor.predict(h1_slice, NEUTRAL_NEWS)
            ml_data = {
                "score": float(ml_result['score']),
                "score_h1": float(ml_result.get('score_h1', ml_result['score'])),
                "score_h4": float(ml_result.get('score_h4', ml_result['score'])),
                "prediction": ("bullish" if ml_result['direction'] == 'BUY'
                               else ("bearish" if ml_result['direction'] == 'SELL'
                                     else "neutral")),
                "probability": float(ml_result.get('raw_proba', ml_result['probability'])),
                "max_confidence": float(ml_result.get('max_confidence', 0.5)),
                "pattern": "undefined", "similar_patterns_count": None,
                "historical_success_rate": None, "error": ml_result.get('error'),
            }
        except Exception as e:
            ml_data = {"score": 50.0, "prediction": "neutral", "probability": 0.5,
                       "max_confidence": 0.5, "pattern": "undefined",
                       "similar_patterns_count": None, "historical_success_rate": None,
                       "error": str(e)}

        momentum_data = analyze_momentum(h1_slice)
        m5_status = compute_m5_status(df_m5, h1_time)
        vol_status = compute_volatility_status(df_m5, h1_time)

        # S/R
        sr_brain_data = None
        sr_zones = []
        h1_for_sr = df_h1.iloc[:idx + 1].copy()
        h4_for_sr = df_h4[df_h4['datetime'] <= h1_time].copy()
        sr_zones = detect_zones_dual(
            h1_for_sr, h4_for_sr,
            merge_pips=config.SR_ZONE_MERGE_PIPS,
            max_age_bars=config.SR_ZONE_MAX_AGE_BARS,
            min_touches=config.SR_MIN_TOUCHES,
        )
        if sr_zones:
            atr_sr = get_atr_value(h1_slice)
            sr_ctx = get_sr_context(
                sr_zones, current_price, atr_sr, direction=None,
                confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
            )
            near_zone, zone_info = is_near_strong_zone(
                sr_zones, current_price, atr_sr,
                min_touches=config.SR_SCENARIO_MIN_TOUCHES,
            )
            sr_brain_data = {
                "confidence_adjustment": 0.0, "confirmations": [], "alerts": [],
                "description": sr_ctx.description,
                "near_strong_zone": near_zone,
                "near_zone_info": {
                    "midpoint": zone_info.midpoint, "touches": zone_info.touches,
                    "zone_type": zone_info.zone_type,
                } if zone_info else None,
                "all_zones": sr_zones,
            }

        # Brain
        brain_result = analyze_with_brain(
            tech_data, ml_data, momentum_data, NEUTRAL_NEWS, current_price,
            calendar_data=NEUTRAL_CALENDAR, volatility_status=vol_status,
            m5_data=m5_status, sr_data=sr_brain_data,
        )

        decision = brain_result.decision
        confidence = brain_result.confidence
        scenario = brain_result.scenario

        # Track conflict detections
        if scenario == "ml_vs_tech_conflito":
            conflict_detections += 1

        if not is_actionable_signal(decision):
            continue
        if confidence < config.BRAIN_MIN_CONFIDENCE:
            continue

        direction = get_trade_direction(decision)
        if direction is None:
            continue

        # S/R direction-aware confidence adjustment
        if sr_zones:
            atr_sr = get_atr_value(h1_slice)
            sr_dir = get_sr_context(
                sr_zones, current_price, atr_sr, direction=direction,
                confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
            )
            if sr_dir.confidence_adjustment != 0:
                confidence = max(0, min(100, confidence + sr_dir.confidence_adjustment))

        # MTF trend
        if getattr(config, 'MTF_TREND_ENABLED', True) and len(df_d1) > 0:
            d1_trend, h4_trend = compute_mtf_trend(
                df_d1, df_h4, h1_time,
                ema_period=getattr(config, 'MTF_EMA_PERIOD', 50),
            )
            mtf_adj, _, _ = _check_mtf_trend_alignment(decision, d1_trend, h4_trend)
            if mtf_adj != 0:
                confidence = max(0, min(100, confidence + mtf_adj))

        # Anti-gap buffer
        h1_hour = h1_time.hour
        ch, oh = config.MARKET_DAILY_CLOSE_HOUR, config.MARKET_DAILY_OPEN_HOUR
        cb_min = config.MARKET_CLOSE_BUFFER_MINUTES
        ob_min = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
        if 0 <= (ch * 60) - (h1_hour * 60) <= cb_min:
            continue
        if 0 <= (h1_hour * 60) - (oh * 60) < ob_min:
            continue

        # M5 reversal
        m5_rev = compute_m5_reversal(df_m5, h1_time, direction)
        if m5_rev['reversal_detected']:
            if m5_rev['reversal_strength'] == "strong":
                continue
            confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
            if confidence < config.BRAIN_MIN_CONFIDENCE:
                continue

        # Cooldown
        lt = last_trade_time.get(direction)
        if lt is not None:
            ct = last_close_type.get(direction)
            min_min = (config.MIN_MINUTES_AFTER_TRAILING if ct == "trailing"
                       else config.MIN_MINUTES_AFTER_SL if ct == "sl"
                       else config.MIN_MINUTES_BETWEEN_TRADES)
            if (h1_time - lt).total_seconds() / 60 < min_min:
                continue

        # Smart pyramid
        same_dir = [t for t in open_trades if t.direction == direction]
        if same_dir:
            blocked = any(
                ((current_price - t.entry_price) / t.entry_price * 100
                 if t.direction == "BUY"
                 else (t.entry_price - current_price) / t.entry_price * 100)
                < config.PYRAMID_MIN_PROFIT_PERCENT
                for t in same_dir
            )
            if blocked:
                continue

        if len(open_trades) >= config.MAX_POSITIONS:
            continue

        # Open trade
        atr = get_atr_value(h1_slice)
        levels = calculate_sl_tp(current_price, direction, atr)
        sl_price = levels.stop_loss
        tp_price = levels.take_profit_1
        if sr_zones:
            adj_sl, adj_tp, _ = adjust_sl_tp_for_sr(
                current_price, sl_price, tp_price,
                direction, atr, sr_zones,
                sl_adjust_enabled=False, tp_adjust_enabled=True,
            )
            sl_price = adj_sl
            tp_price = adj_tp

        ticket_counter += 1
        trade = SimTrade(
            ticket=ticket_counter,
            direction=direction,
            entry_price=current_price,
            entry_time=h1_time,
            sl=sl_price,
            tp=tp_price,
            atr=atr,
            brain_score=brain_result.final_score,
            confidence=confidence,
            scenario=scenario,
            scenario_desc=getattr(brain_result, 'scenario_desc', scenario),
            explanation_snippet="",
            tech_score=float(tech_data.get('score', 50)),
            ml_score=float(ml_data.get('score', 50)),
            momentum_score=float(momentum_data.get('score', 50)),
            is_pyramid=len(same_dir) > 0,
        )

        [trade] = simulate_trades_concurrent([trade], df_m5, early_exit_enabled=False)
        trades.append(trade)

        if trade.close_time and trade.close_time > h1_time:
            open_trades.append(trade)
        else:
            last_close_type[direction] = (
                "tp" if "tp" in trade.close_reason else
                "trailing" if trade.profit_pips >= 0 else "sl"
            )
            last_trade_time[direction] = trade.close_time or h1_time

    return trades, {'conflict_detections': conflict_detections}


# ============================================================================
# STATISTICS
# ============================================================================

def _calc_stats(trades: List[SimTrade]) -> Dict:
    if not trades:
        return {'trades': 0, 'wins': 0, 'losses': 0, 'wr': 0.0,
                'pnl_usd': 0.0, 'pips': 0.0, 'pf': 0.0, 'max_dd': 0.0,
                'avg_win_pips': 0.0, 'avg_loss_pips': 0.0, 'avg_conf_win': 0.0,
                'avg_conf_loss': 0.0}
    wins = [t for t in trades if t.profit_pips > 0]
    losses = [t for t in trades if t.profit_pips <= 0]
    gp = sum(t.profit_usd for t in wins)
    gl = abs(sum(t.profit_usd for t in losses))
    running = peak = max_dd = 0.0
    for t in trades:
        running += t.profit_usd
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    return {
        'trades': len(trades),
        'wins': len(wins),
        'losses': len(losses),
        'wr': len(wins) / len(trades) * 100,
        'pnl_usd': sum(t.profit_usd for t in trades),
        'pips': sum(t.profit_pips for t in trades),
        'pf': gp / gl if gl > 0 else float('inf'),
        'max_dd': max_dd,
        'avg_win_pips': float(np.mean([t.profit_pips for t in wins])) if wins else 0.0,
        'avg_loss_pips': float(np.mean([t.profit_pips for t in losses])) if losses else 0.0,
        'avg_conf_win': float(np.mean([t.confidence for t in wins])) if wins else 0.0,
        'avg_conf_loss': float(np.mean([t.confidence for t in losses])) if losses else 0.0,
    }


# ============================================================================
# REPORT
# ============================================================================

def generate_report(results: Dict[str, Tuple[List[SimTrade], Dict]]) -> str:
    """Generate comparison report for all variants."""
    lines = []
    SEP = "=" * 80
    THIN = "─" * 80
    
    lines.append(SEP)
    lines.append("  BACKTEST: Tech Direction / Tech Risk Split — 4-way comparison")
    lines.append(f"  Period: {BT_START.strftime('%Y-%m-%d')} → {BT_END.strftime('%Y-%m-%d %H:%M')}")
    lines.append(SEP)
    lines.append("")
    lines.append("  Variants:")
    lines.append("    Baseline : Current tech_score (production code)")
    lines.append("    A        : Tech Direction + Tech Risk, conflict DISABLED")
    lines.append("    B        : Tech Direction + Tech Risk, conflict threshold 75")
    lines.append("    C        : Tech Direction + Tech Risk, conflict threshold 85")
    lines.append(SEP)
    
    # Calculate stats for each variant
    stats = {}
    for name, (trades, meta) in results.items():
        stats[name] = _calc_stats(trades)
        stats[name]['conflict_detections'] = meta.get('conflict_detections', 0)
    
    # Main comparison table
    W = 14
    variants = ['Baseline', 'A', 'B', 'C']
    
    lines.append(f"\n{'Metric':<24} " + " ".join(f"{v:>{W}}" for v in variants))
    lines.append(THIN)
    
    metrics = [
        ('Trades', 'trades', '{:>d}'),
        ('Wins', 'wins', '{:>d}'),
        ('Losses', 'losses', '{:>d}'),
        ('Win Rate %', 'wr', '{:>.1f}%'),
        ('P&L $', 'pnl_usd', '${:>+.2f}'),
        ('Pips', 'pips', '{:>+.1f}'),
        ('Profit Factor', 'pf', '{:>.2f}'),
        ('Max Drawdown $', 'max_dd', '${:>.2f}'),
        ('Avg Win (pips)', 'avg_win_pips', '{:>+.1f}'),
        ('Avg Loss (pips)', 'avg_loss_pips', '{:>+.1f}'),
        ('Avg Conf (wins)', 'avg_conf_win', '{:>.1f}'),
        ('Avg Conf (losses)', 'avg_conf_loss', '{:>.1f}'),
        ('Conflict Triggers', 'conflict_detections', '{:>d}'),
    ]
    
    for label, key, fmt in metrics:
        row = f"  {label:<22}"
        for v in variants:
            val = stats[v].get(key, 0)
            if key == 'pf' and val == float('inf'):
                row += f" {'∞':>{W}}"
            elif '%' in fmt:
                row += f" {fmt.format(val):>{W}}"
            else:
                row += f" {fmt.format(val):>{W}}"
        lines.append(row)
    
    # Delta comparison vs Baseline
    lines.append(f"\n{THIN}")
    lines.append("  DELTA vs BASELINE")
    lines.append(THIN)
    
    base = stats['Baseline']
    for v in ['A', 'B', 'C']:
        s = stats[v]
        pf_base = base['pf'] if base['pf'] != float('inf') else 9.99
        pf_v = s['pf'] if s['pf'] != float('inf') else 9.99
        
        lines.append(f"\n  Variant {v}:")
        lines.append(f"    Trades: {s['trades'] - base['trades']:+d}")
        lines.append(f"    Win Rate: {s['wr'] - base['wr']:+.1f}%")
        lines.append(f"    Profit Factor: {pf_v - pf_base:+.2f}")
        lines.append(f"    P&L: ${s['pnl_usd'] - base['pnl_usd']:+.2f}")
        lines.append(f"    Max DD: ${s['max_dd'] - base['max_dd']:+.2f}")
    
    # Verdict
    lines.append(f"\n{SEP}")
    lines.append("  VERDICT (ADOPT: PF Δ≥+0.10 AND WR Δ≥-2% | ABANDON: PF Δ≤-0.05 OR WR Δ≤-5%)")
    lines.append(SEP)
    
    for v in ['A', 'B', 'C']:
        s = stats[v]
        pf_base = base['pf'] if base['pf'] != float('inf') else 9.99
        pf_v = s['pf'] if s['pf'] != float('inf') else 9.99
        pf_d = pf_v - pf_base
        wr_d = s['wr'] - base['wr']
        
        if pf_d >= 0.10 and wr_d >= -2.0:
            verdict = "✅ ADOPT"
        elif pf_d <= -0.05 or wr_d <= -5.0:
            verdict = "❌ ABANDON"
        else:
            verdict = "⚠️ MONITOR"
        
        lines.append(f"  Variant {v}: PF Δ={pf_d:+.2f}  WR Δ={wr_d:+.1f}%  →  {verdict}")
    
    lines.append(f"\n{SEP}")
    
    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 70)
    print("  BACKTEST: Tech Direction / Tech Risk Split (4-way)")
    print("=" * 70)

    if not connect():
        return

    try:
        import scripts.run_backtest as rbt
        rbt.BT_START = BT_START
        rbt.BT_END = BT_END

        data = collect_all_data()
        for key in ['h1', 'h4', 'm5']:
            if data[key].empty:
                print(f"❌ Missing {key} data. Aborting.")
                return

        bt_predictor = BacktestMLPredictor()
        if not bt_predictor.load_model():
            print("❌ Failed to load ML models")
            return

        results = {}

        # ══════════════════════════════════════════════════════════════════════
        # BASELINE: Production code, NO patches
        # ══════════════════════════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  BASELINE: Current production code (no changes)")
        print("─" * 70)
        
        # CRITICAL: Clear any cached patches and ensure clean state
        _clear_patch_cache()
        _unpatch_tech_direction()  # Ensure nothing is patched
        
        trades_baseline, meta_baseline = _run_loop(data, bt_predictor, "Baseline")
        results['Baseline'] = (trades_baseline, meta_baseline)

        # ══════════════════════════════════════════════════════════════════════
        # VARIANT A: Tech Direction + Tech Risk, conflict DISABLED
        # ══════════════════════════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  VARIANT A: Tech Direction + Tech Risk, conflict DISABLED")
        print("─" * 70)
        
        _patch_tech_direction(conflict_threshold=None)  # Disable conflict
        try:
            trades_a, meta_a = _run_loop(data, bt_predictor, "Variant A")
            results['A'] = (trades_a, meta_a)
        finally:
            _unpatch_tech_direction()

        # ══════════════════════════════════════════════════════════════════════
        # VARIANT B: Tech Direction + Tech Risk, conflict threshold 75
        # ══════════════════════════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  VARIANT B: Tech Direction + Tech Risk, conflict threshold 75")
        print("─" * 70)
        
        _clear_patch_cache()  # Clear cache before re-patching
        _patch_tech_direction(conflict_threshold=75)
        try:
            trades_b, meta_b = _run_loop(data, bt_predictor, "Variant B")
            results['B'] = (trades_b, meta_b)
        finally:
            _unpatch_tech_direction()

        # ══════════════════════════════════════════════════════════════════════
        # VARIANT C: Tech Direction + Tech Risk, conflict threshold 85
        # ══════════════════════════════════════════════════════════════════════
        print("\n" + "─" * 70)
        print("  VARIANT C: Tech Direction + Tech Risk, conflict threshold 85")
        print("─" * 70)
        
        _clear_patch_cache()  # Clear cache before re-patching
        _patch_tech_direction(conflict_threshold=85)
        try:
            trades_c, meta_c = _run_loop(data, bt_predictor, "Variant C")
            results['C'] = (trades_c, meta_c)
        finally:
            _unpatch_tech_direction()

        # ══════════════════════════════════════════════════════════════════════
        # REPORT
        # ══════════════════════════════════════════════════════════════════════
        report = generate_report(results)
        print(report)

        ts = datetime.now().strftime('%Y%m%d_%H%M')
        report_path = os.path.join(ROOT_DIR, "data", f"backtest_tech_direction_{ts}.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n📄 Report saved: {report_path}")

    finally:
        mt5.shutdown()
        print("\n✅ MT5 disconnected. Done.")


if __name__ == "__main__":
    main()
