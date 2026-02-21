"""
BACKTEST STEP 1 v3: ml_vs_tech_conflito Scenario — 3-way comparison
====================================================================
Run A (baseline) vs Run B1 (tech+ML conflict) vs Run B2 (tech+ML+momentum conflict).
Period: 2025-08-18 → 2026-02-20 23:59

Run B1 trigger: tech >= 65 AND ml <= 40
Run B2 trigger: tech >= 65 AND ml <= 40 AND momentum >= 70

Usage:
    python scripts/backtest_ml_conflict_scenario.py

Output:
    data/backtest_ml_conflict_<timestamp>.txt
    data/backtest_ml_conflict_<timestamp>_b1.csv
    data/backtest_ml_conflict_<timestamp>_b2.csv
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional

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
from technical_analyzer import calculate_indicators, analyze_technical_detailed, get_atr_value
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

# ── Run B1: tech vs ML conflict ─────────────────────────────────────────────
ML_CONFLICT_KEY     = "ml_vs_tech_conflito"
ML_CONFLICT_WEIGHTS = {"technical": 0.45, "ml": 0.10, "momentum": 0.22,
                       "news": 0.15, "calendar": 0.08}
ML_CONFLICT_MULT    = 0.95
CONFLICT_TECH_MIN   = 65.0
CONFLICT_ML_MAX     = 40.0

# ── Run B2: tech + ML + momentum conflict (stricter, more conviction) ────────
ML_CONFLICT_MOM_KEY     = "ml_vs_tech_conflito_mom"
ML_CONFLICT_MOM_WEIGHTS = {"technical": 0.45, "ml": 0.08, "momentum": 0.25,
                           "news": 0.14, "calendar": 0.08}
ML_CONFLICT_MOM_MULT    = 0.97
CONFLICT_MOM_MIN        = 70.0

NEUTRAL_NEWS = {
    "score": 50.0,
    "dxy": {"value": 104.0, "change_24h": 0.0, "trend": "stable"},
    "yields": {"value": 4.5, "change_24h": 0.0, "trend": "stable"},
    "vix": {"value": 17.0, "level": "low"},
    "sentiment": {"headlines_score": 50, "normalized": 0},
    "high_impact_news_soon": False, "geopolitical_risk": "low", "anomalies": [],
}


# ============================================================================
# MONKEY-PATCH
# ============================================================================

def _patch_central_brain():
    """Inject ml_vs_tech_conflito into central_brain at runtime. Returns original fn."""
    import central_brain as cb
    cb.SCENARIO_WEIGHTS[ML_CONFLICT_KEY] = ML_CONFLICT_WEIGHTS.copy()
    _orig = cb._identify_scenario

    def _patched(tech_data, ml_data, momentum_data, news_data, momentum_strength,
                 calendar_data=None, volatility_status=None, sr_data=None):
        tech_score    = tech_data.get("score", 50)
        ml_score      = ml_data.get("score", 50)
        ml_confidence = ml_data.get("max_confidence", 0.5)

        def _conflict():
            return (tech_score >= CONFLICT_TECH_MIN and
                    ml_score   <= CONFLICT_ML_MAX)

        # Extreme volatility (unchanged)
        vol = volatility_status or {}
        if vol.get("status") == "EXTREME":
            pct = vol.get("extreme_percent", 0)
            return "volatilidade_extrema", f"Extreme volatility ({pct:.1f}%) - BLOCK", 0.0

        # S/R zone — conflict check BEFORE early return (the priority bug fix)
        sr = sr_data or {}
        if sr.get("near_strong_zone") and sr.get("near_zone_info"):
            if _conflict():
                return (
                    ML_CONFLICT_KEY,
                    (f"ML bearish ({ml_score:.0f}) conflicts with bullish Tech ({tech_score:.0f}) "
                     f"near S/R zone — ML weight reduced"),
                    ML_CONFLICT_MULT,
                )
            zi = sr["near_zone_info"]
            return "zona_sr_forte", (
                f"Near strong {zi.get('zone_type','?')} zone at {zi.get('midpoint',0):.2f} "
                f"({zi.get('touches',0)} touches) - informational"
            ), 1.00

        # Delegate to original
        result = _orig(tech_data, ml_data, momentum_data, news_data, momentum_strength,
                       calendar_data=calendar_data, volatility_status=volatility_status,
                       sr_data=sr_data)

        # Upgrade sinais_conflitantes when thresholds met
        if result[0] == "sinais_conflitantes" and _conflict():
            return (
                ML_CONFLICT_KEY,
                (f"ML bearish ({ml_score:.0f}) conflicts with bullish Tech ({tech_score:.0f}) "
                 f"— ML weight reduced"),
                ML_CONFLICT_MULT,
            )
        return result

    cb._identify_scenario = _patched
    return _orig


def _patch_central_brain_mom():
    """Inject ml_vs_tech_conflito_mom (tech+ML+momentum) into central_brain. Returns original fn."""
    import central_brain as cb
    cb.SCENARIO_WEIGHTS[ML_CONFLICT_MOM_KEY] = ML_CONFLICT_MOM_WEIGHTS.copy()
    _orig = cb._identify_scenario

    def _patched_mom(tech_data, ml_data, momentum_data, news_data, momentum_strength,
                     calendar_data=None, volatility_status=None, sr_data=None):
        tech_score = tech_data.get("score", 50)
        ml_score   = ml_data.get("score", 50)
        mom_score  = momentum_data.get("score", 50)

        def _conflict_mom():
            return (tech_score >= CONFLICT_TECH_MIN and
                    ml_score   <= CONFLICT_ML_MAX   and
                    mom_score  >= CONFLICT_MOM_MIN)

        # Extreme volatility (unchanged)
        vol = volatility_status or {}
        if vol.get("status") == "EXTREME":
            pct = vol.get("extreme_percent", 0)
            return "volatilidade_extrema", f"Extreme volatility ({pct:.1f}%) - BLOCK", 0.0

        # S/R zone — conflict check BEFORE early return (bug fix)
        sr = sr_data or {}
        if sr.get("near_strong_zone") and sr.get("near_zone_info"):
            if _conflict_mom():
                return (
                    ML_CONFLICT_MOM_KEY,
                    (f"Tech({tech_score:.0f})+Mom({mom_score:.0f}) vs ML({ml_score:.0f}) "
                     f"near S/R — ML weight minimised"),
                    ML_CONFLICT_MOM_MULT,
                )
            zi = sr["near_zone_info"]
            return "zona_sr_forte", (
                f"Near strong {zi.get('zone_type','?')} zone at {zi.get('midpoint',0):.2f} "
                f"({zi.get('touches',0)} touches) - informational"
            ), 1.00

        result = _orig(tech_data, ml_data, momentum_data, news_data, momentum_strength,
                       calendar_data=calendar_data, volatility_status=volatility_status,
                       sr_data=sr_data)

        if result[0] in ("sinais_conflitantes", "momentum_forte") and _conflict_mom():
            return (
                ML_CONFLICT_MOM_KEY,
                (f"Tech({tech_score:.0f})+Mom({mom_score:.0f}) vs ML({ml_score:.0f}) "
                 f"— ML weight minimised"),
                ML_CONFLICT_MOM_MULT,
            )
        return result

    cb._identify_scenario = _patched_mom
    return _orig


def _unpatch_central_brain(orig_fn):
    import central_brain as cb
    cb._identify_scenario = orig_fn
    cb.SCENARIO_WEIGHTS.pop(ML_CONFLICT_KEY, None)
    cb.SCENARIO_WEIGHTS.pop(ML_CONFLICT_MOM_KEY, None)


# ============================================================================
# CORE BACKTEST LOOP
# ============================================================================

def _run_loop(data: Dict, bt_predictor, label: str,
              feb20_log: Optional[list] = None,
              conflict_key: str = ML_CONFLICT_KEY) -> tuple:
    """Returns (trades, scenario_detections, block_counts).

    block_counts: dict of {reason: count} recording why scenario-detected
    candles were blocked before a trade opened. Keys:
      not_actionable, confidence_below_min, gap_buffer, m5_reversal_strong,
      m5_reversal_confidence, cooldown, pyramid, max_positions, opened
    """
    df_h1 = data['h1'].copy()
    df_h4 = data['h4'].copy()
    df_d1 = data.get('d1', pd.DataFrame()).copy()
    df_m5 = data['m5'].copy()

    trades: List[SimTrade] = []
    open_trades: List[SimTrade] = []
    ticket_counter = 4000000
    scenario_detections = 0
    block_counts: Dict[str, int] = {
        'not_actionable':          0,
        'confidence_below_min':    0,
        'gap_buffer':              0,
        'm5_reversal_strong':      0,
        'm5_reversal_confidence':  0,
        'cooldown':                0,
        'pyramid':                 0,
        'max_positions':           0,
        'opened':                  0,
    }

    last_trade_time = {'BUY': None, 'SELL': None}
    last_close_type = {'BUY': None, 'SELL': None}

    bt_mask    = (df_h1['datetime'] >= BT_START) & (df_h1['datetime'] <= BT_END)
    bt_indices = df_h1[bt_mask].index.tolist()

    if not bt_indices:
        print(f"  ❌ No H1 candles for {label}")
        return [], 0, block_counts

    total = len(bt_indices)
    print(f"\n🔄 {label}: {total} H1 candles ({BT_START.date()} → {BT_END.date()})")

    for count, idx in enumerate(bt_indices):
        if idx < H1_WARMUP:
            continue

        h1_candle = df_h1.iloc[idx]
        h1_time   = h1_candle['datetime']
        if hasattr(h1_time, 'to_pydatetime'):
            h1_time = h1_time.to_pydatetime()
        current_price = float(h1_candle['close'])

        if count % 100 == 0:
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
                "score":          float(ml_result['score']),
                "score_h1":       float(ml_result.get('score_h1', ml_result['score'])),
                "score_h4":       float(ml_result.get('score_h4', ml_result['score'])),
                "prediction":     ("bullish" if ml_result['direction'] == 'BUY'
                                   else ("bearish" if ml_result['direction'] == 'SELL'
                                         else "neutral")),
                "probability":    float(ml_result.get('raw_proba', ml_result['probability'])),
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
        m5_status     = compute_m5_status(df_m5, h1_time)
        vol_status    = compute_volatility_status(df_m5, h1_time)

        # S/R
        sr_brain_data = None
        sr_zones      = []
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

        decision   = brain_result.decision
        confidence = brain_result.confidence
        scenario   = brain_result.scenario

        # Count scenario detections (regardless of whether trade opens)
        in_conflict = (scenario == conflict_key)
        if in_conflict:
            scenario_detections += 1

        # Feb 20 validation log (07:00-16:00 UTC)
        if feb20_log is not None:
            if h1_time.date() == datetime(2026, 2, 20).date() and 7 <= h1_time.hour <= 16:
                feb20_log.append({
                    'time': h1_time.strftime('%H:%M'), 'decision': decision,
                    'score': round(brain_result.final_score, 1),
                    'confidence': round(confidence, 1),
                    'scenario': scenario,
                    'tech': round(tech_data.get('score', 50), 1),
                    'ml': round(ml_data.get('score', 50), 1),
                    'momentum': round(momentum_data.get('score', 50), 1),
                })

        if not is_actionable_signal(decision):
            if in_conflict:
                block_counts['not_actionable'] += 1
            continue
        if confidence < config.BRAIN_MIN_CONFIDENCE:
            if in_conflict:
                block_counts['confidence_below_min'] += 1
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
        h1_hour  = h1_time.hour
        ch, oh   = config.MARKET_DAILY_CLOSE_HOUR, config.MARKET_DAILY_OPEN_HOUR
        cb_min   = config.MARKET_CLOSE_BUFFER_MINUTES
        ob_min   = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
        if 0 <= (ch * 60) - (h1_hour * 60) <= cb_min:
            if in_conflict:
                block_counts['gap_buffer'] += 1
            continue
        if 0 <= (h1_hour * 60) - (oh * 60) < ob_min:
            if in_conflict:
                block_counts['gap_buffer'] += 1
            continue

        # M5 reversal
        m5_rev = compute_m5_reversal(df_m5, h1_time, direction)
        if m5_rev['reversal_detected']:
            if m5_rev['reversal_strength'] == "strong":
                if in_conflict:
                    block_counts['m5_reversal_strong'] += 1
                continue
            confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
            if confidence < config.BRAIN_MIN_CONFIDENCE:
                if in_conflict:
                    block_counts['m5_reversal_confidence'] += 1
                continue

        # Cooldown
        lt = last_trade_time.get(direction)
        if lt is not None:
            ct = last_close_type.get(direction)
            min_min = (config.MIN_MINUTES_AFTER_TRAILING if ct == "trailing"
                       else config.MIN_MINUTES_AFTER_SL if ct == "sl"
                       else config.MIN_MINUTES_BETWEEN_TRADES)
            if (h1_time - lt).total_seconds() / 60 < min_min:
                if in_conflict:
                    block_counts['cooldown'] += 1
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
                if in_conflict:
                    block_counts['pyramid'] += 1
                continue

        if len(open_trades) >= config.MAX_POSITIONS:
            if in_conflict:
                block_counts['max_positions'] += 1
            continue

        # Open trade
        atr    = get_atr_value(h1_slice)
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
        if in_conflict:
            block_counts['opened'] += 1

        if trade.close_time and trade.close_time > h1_time:
            open_trades.append(trade)
        else:
            last_close_type[direction] = (
                "tp" if "tp" in trade.close_reason else
                "trailing" if trade.profit_pips >= 0 else "sl"
            )
            last_trade_time[direction] = trade.close_time or h1_time

    return trades, scenario_detections, block_counts


# ============================================================================
# STATISTICS
# ============================================================================

def _calc_stats(trades: List[SimTrade]) -> Dict:
    if not trades:
        return {'trades': 0, 'wins': 0, 'losses': 0, 'wr': 0.0,
                'pnl_usd': 0.0, 'pips': 0.0, 'pf': 0.0, 'max_dd': 0.0,
                'avg_win_pips': 0.0, 'avg_loss_pips': 0.0}
    wins   = [t for t in trades if t.profit_pips > 0]
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
        'trades':        len(trades),
        'wins':          len(wins),
        'losses':        len(losses),
        'wr':            len(wins) / len(trades) * 100,
        'pnl_usd':       sum(t.profit_usd for t in trades),
        'pips':          sum(t.profit_pips for t in trades),
        'pf':            gp / gl if gl > 0 else float('inf'),
        'max_dd':        max_dd,
        'avg_win_pips':  float(np.mean([t.profit_pips for t in wins]))  if wins   else 0.0,
        'avg_loss_pips': float(np.mean([t.profit_pips for t in losses])) if losses else 0.0,
    }


# ============================================================================
# REPORT HELPERS
# ============================================================================

def _scenario_section(lines, trades_run, scen_key, label, detections, block_counts, THIN):
    ct   = [t for t in trades_run if t.scenario == scen_key]
    ct_w = [t for t in ct if t.profit_pips > 0]
    ct_l = [t for t in ct if t.profit_pips <= 0]
    ct_gp = sum(t.profit_usd for t in ct_w)
    ct_gl = abs(sum(t.profit_usd for t in ct_l))
    ct_pf = ct_gp / ct_gl if ct_gl > 0 else float('inf')
    lines.append(f"\n  {label}")
    lines.append(f"  Detected: {detections}x   Trades opened: {len(ct)}")
    if ct:
        lines.append(f"  W/L: {len(ct_w)}W/{len(ct_l)}L   WR={len(ct_w)/len(ct)*100:.1f}%   "
                     f"PF={ct_pf:.2f}   P&L=${sum(t.profit_usd for t in ct):+.2f}   "
                     f"Pips={sum(t.profit_pips for t in ct):+.1f}")
        lines.append(f"  {'Date':>10} {'Time':>5} {'Dir':>4} {'Tech':>5} {'ML':>5} {'Mom':>5} {'Result':>6} {'Pips':>8} {'Close':>12}")
        for t in sorted(ct, key=lambda x: x.entry_time):
            r = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(f"  {t.entry_time.strftime('%Y-%m-%d'):>10} {t.entry_time.strftime('%H:%M'):>5} "
                         f"{t.direction:>4} {t.tech_score:>5.1f} {t.ml_score:>5.1f} {t.momentum_score:>5.1f} "
                         f"{r:>6} {t.profit_pips:>+8.1f} {t.close_reason:>12}")
    if detections > 0:
        opened = block_counts.get('opened', 0)
        lines.append(f"\n  BLOCKING ({detections - opened}/{detections} blocked):")
        bmap = {'not_actionable': 'HOLD decision', 'confidence_below_min': 'Confidence < min',
                'gap_buffer': 'Anti-gap buffer', 'm5_reversal_strong': 'M5 strong reversal',
                'm5_reversal_confidence': 'M5 reversal+confidence drop', 'cooldown': 'Cooldown',
                'pyramid': 'Pyramid profit < min', 'max_positions': 'Max positions reached'}
        for k, lbl in bmap.items():
            c = block_counts.get(k, 0)
            if c > 0:
                lines.append(f"    {c:>3}x ({c/detections*100:>5.1f}%)  {lbl}")


def _added_removed(lines, trades_a, trades_b, label_b, THIN):
    keys_a = {(t.entry_time, t.direction) for t in trades_a}
    keys_b = {(t.entry_time, t.direction) for t in trades_b}
    added   = [t for t in trades_b if (t.entry_time, t.direction) not in keys_a]
    removed = [t for t in trades_a if (t.entry_time, t.direction) not in keys_b]
    lines.append(f"\n{THIN}")
    lines.append(f"  📈 ADDED by {label_b}: {len(added)} trades")
    if added:
        add_w = [t for t in added if t.profit_pips > 0]
        add_gl = abs(sum(t.profit_usd for t in added if t.profit_pips <= 0))
        add_pf = sum(t.profit_usd for t in add_w) / add_gl if add_gl > 0 else float('inf')
        lines.append(f"  {len(add_w)}W/{len(added)-len(add_w)}L  WR={len(add_w)/len(added)*100:.1f}%  "
                     f"PF={add_pf:.2f}  P&L=${sum(t.profit_usd for t in added):+.2f}")
        for t in sorted(added, key=lambda x: x.entry_time):
            r = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(f"    {t.entry_time.strftime('%Y-%m-%d %H:%M')}  {t.direction}  "
                         f"{t.scenario}  {r}  {t.profit_pips:+.1f}p")
    lines.append(f"\n  ⚠️  REMOVED by {label_b}: {len(removed)} trades")
    if removed:
        rem_w = [t for t in removed if t.profit_pips > 0]
        pct = len(rem_w)/len(removed)*100
        flag = "⚠️ HIGH" if pct > 55 else "✅ OK"
        lines.append(f"  {len(rem_w)}W/{len(removed)-len(rem_w)}L  WR={pct:.1f}%  {flag}")
    else:
        lines.append("  (none — no regression)")


def _calc_verdict(sa, sb, label_b):
    pf_a  = sa['pf']  if sa['pf']  != float('inf') else 9.99
    pf_b  = sb['pf']  if sb['pf']  != float('inf') else 9.99
    pf_d  = pf_b - pf_a
    wr_d  = sb['wr'] - sa['wr']
    pnl_d = sb['pnl_usd'] - sa['pnl_usd']
    if pf_d >= 0.10 and wr_d >= -2.0:
        v = "✅ ADOPT"
    elif pf_d <= -0.05 or wr_d <= -5.0:
        v = "❌ ABANDON"
    else:
        v = "⚠️ MONITOR"
    return f"  {label_b}: PF Δ={pf_d:+.2f}  WR Δ={wr_d:+.1f}%  P&L Δ=${pnl_d:+.2f}  →  {v}"


# ============================================================================
# REPORT
# ============================================================================

def generate_report(trades_a, trades_b1, trades_b2,
                    feb20_a, feb20_b1, feb20_b2,
                    det_b1=0, det_b2=0,
                    blk_b1=None, blk_b2=None) -> str:
    blk_b1 = blk_b1 or {}
    blk_b2 = blk_b2 or {}
    lines = []
    SEP  = "=" * 70
    THIN = "─" * 70

    lines.append(SEP)
    lines.append("  BACKTEST STEP 1 v3: ml_vs_tech_conflito — 3-way")
    lines.append(f"  Period : {BT_START.strftime('%Y-%m-%d')} → {BT_END.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  Run A  : Baseline")
    lines.append(f"  Run B1 : tech>={CONFLICT_TECH_MIN:.0f} AND ml<={CONFLICT_ML_MAX:.0f}  "
                 f"| tech={ML_CONFLICT_WEIGHTS['technical']:.0%} ml={ML_CONFLICT_WEIGHTS['ml']:.0%} "
                 f"mom={ML_CONFLICT_WEIGHTS['momentum']:.0%}")
    lines.append(f"  Run B2 : B1 + momentum>={CONFLICT_MOM_MIN:.0f}  "
                 f"| tech={ML_CONFLICT_MOM_WEIGHTS['technical']:.0%} ml={ML_CONFLICT_MOM_WEIGHTS['ml']:.0%} "
                 f"mom={ML_CONFLICT_MOM_WEIGHTS['momentum']:.0%}")
    lines.append(SEP)

    sa  = _calc_stats(trades_a)
    sb1 = _calc_stats(trades_b1)
    sb2 = _calc_stats(trades_b2)

    W = 14
    lines.append(f"\n{'':>24} {'Run A':>{W}} {'Run B1':>{W}} {'Run B2':>{W}} {'ΔB1':>8} {'ΔB2':>8}")
    lines.append(THIN)
    lines.append(f"  {'Trades':<22} {sa['trades']:>{W}} {sb1['trades']:>{W}} {sb2['trades']:>{W}} "
                 f"{sb1['trades']-sa['trades']:>+8} {sb2['trades']-sa['trades']:>+8}")
    lines.append(f"  {'Wins':<22} {sa['wins']:>{W}} {sb1['wins']:>{W}} {sb2['wins']:>{W}} "
                 f"{sb1['wins']-sa['wins']:>+8} {sb2['wins']-sa['wins']:>+8}")
    lines.append(f"  {'Losses':<22} {sa['losses']:>{W}} {sb1['losses']:>{W}} {sb2['losses']:>{W}} "
                 f"{sb1['losses']-sa['losses']:>+8} {sb2['losses']-sa['losses']:>+8}")
    lines.append(f"  {'Win Rate %':<22} {sa['wr']:>{W-1}.1f}% {sb1['wr']:>{W-1}.1f}% {sb2['wr']:>{W-1}.1f}% "
                 f"{sb1['wr']-sa['wr']:>+7.1f}% {sb2['wr']-sa['wr']:>+7.1f}%")
    lines.append(f"  {'P&L $':<22} ${sa['pnl_usd']:>+{W-1}.2f} ${sb1['pnl_usd']:>+{W-1}.2f} ${sb2['pnl_usd']:>+{W-1}.2f} "
                 f"${sb1['pnl_usd']-sa['pnl_usd']:>+6.2f} ${sb2['pnl_usd']-sa['pnl_usd']:>+6.2f}")
    lines.append(f"  {'Pips':<22} {sa['pips']:>+{W}.1f} {sb1['pips']:>+{W}.1f} {sb2['pips']:>+{W}.1f} "
                 f"{sb1['pips']-sa['pips']:>+7.1f} {sb2['pips']-sa['pips']:>+7.1f}")
    pf_a  = sa['pf']  if sa['pf']  != float('inf') else 9.99
    pf_b1 = sb1['pf'] if sb1['pf'] != float('inf') else 9.99
    pf_b2 = sb2['pf'] if sb2['pf'] != float('inf') else 9.99
    lines.append(f"  {'Profit Factor':<22} {pf_a:>{W}.2f} {pf_b1:>{W}.2f} {pf_b2:>{W}.2f} "
                 f"{pf_b1-pf_a:>+8.2f} {pf_b2-pf_a:>+8.2f}")
    lines.append(f"  {'Max Drawdown $':<22} ${sa['max_dd']:>{W-1}.2f} ${sb1['max_dd']:>{W-1}.2f} ${sb2['max_dd']:>{W-1}.2f} "
                 f"${sb1['max_dd']-sa['max_dd']:>+6.2f} ${sb2['max_dd']-sa['max_dd']:>+6.2f}")
    lines.append(f"  {'Avg Win (pips)':<22} {sa['avg_win_pips']:>+{W}.1f} {sb1['avg_win_pips']:>+{W}.1f} {sb2['avg_win_pips']:>+{W}.1f} "
                 f"{sb1['avg_win_pips']-sa['avg_win_pips']:>+7.1f} {sb2['avg_win_pips']-sa['avg_win_pips']:>+7.1f}")
    lines.append(f"  {'Avg Loss (pips)':<22} {sa['avg_loss_pips']:>+{W}.1f} {sb1['avg_loss_pips']:>+{W}.1f} {sb2['avg_loss_pips']:>+{W}.1f} "
                 f"{sb1['avg_loss_pips']-sa['avg_loss_pips']:>+7.1f} {sb2['avg_loss_pips']-sa['avg_loss_pips']:>+7.1f}")

    # ── Scenario trigger analysis ───────────────────────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  🎯 SCENARIO TRIGGER ANALYSIS")
    lines.append(THIN)
    _scenario_section(lines, trades_b1, ML_CONFLICT_KEY,
                      "Run B1 (tech+ML conflict):", det_b1, blk_b1, THIN)
    lines.append("")
    _scenario_section(lines, trades_b2, ML_CONFLICT_MOM_KEY,
                      "Run B2 (tech+ML+momentum):", det_b2, blk_b2, THIN)

    # ── Added / removed ────────────────────────────────────────────────────
    _added_removed(lines, trades_a, trades_b1, "Run B1", THIN)
    _added_removed(lines, trades_a, trades_b2, "Run B2", THIN)

    # ── Feb 20 validation ──────────────────────────────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  📅 FEB 20 VALIDATION (07:00–16:00 UTC) — THE MISSED MOVE")
    lines.append(THIN)
    for run_label, log in [("Run A", feb20_a), ("Run B1", feb20_b1), ("Run B2", feb20_b2)]:
        lines.append(f"\n  {run_label}:")
        if log:
            lines.append(f"  {'Time':>5} {'Decision':>12} {'Score':>6} {'Conf':>6} {'Scenario':>26} {'Tech':>5} {'ML':>5} {'Mom':>5}")
            for d in log:
                lines.append(f"  {d['time']:>5} {d['decision']:>12} {d['score']:>6.1f} "
                             f"{d['confidence']:>6.1f} {d['scenario']:>26} "
                             f"{d['tech']:>5.1f} {d['ml']:>5.1f} {d['momentum']:>5.1f}")

    feb20_date = datetime(2026, 2, 20).date()
    lines.append(f"\n  Trades on Feb 20:")
    for run_lbl, tlist in [("Run A", trades_a), ("Run B1", trades_b1), ("Run B2", trades_b2)]:
        t20 = sorted([t for t in tlist if t.entry_time and t.entry_time.date() == feb20_date],
                     key=lambda x: x.entry_time)
        lines.append(f"  {run_lbl}: {len(t20)} trades")
        for t in t20:
            r = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(f"    {t.entry_time.strftime('%H:%M')} {t.direction} @ {t.entry_price:.2f} "
                         f"→ {t.close_price:.2f} ({t.close_reason}) {r} {t.profit_pips:+.1f}p  [{t.scenario}]")

    # ── Verdict ────────────────────────────────────────────────────────────
    lines.append(f"\n{SEP}")
    lines.append(f"  VERDICT  (ADOPT: PF Δ≥+0.10 AND WR Δ≥-2% | ABANDON: PF Δ≤-0.05 OR WR Δ≤-5%)")
    lines.append(SEP)
    lines.append(_calc_verdict(sa, sb1, "Run B1"))
    lines.append(_calc_verdict(sa, sb2, "Run B2"))
    lines.append(f"\n{'=' * 70}")

    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def _save_csv(trades, scen_key, path):
    ct = [t for t in trades if t.scenario == scen_key]
    if ct:
        rows = [{'entry_time': t.entry_time, 'direction': t.direction,
                 'entry_price': t.entry_price, 'close_price': t.close_price,
                 'close_reason': t.close_reason, 'profit_pips': round(t.profit_pips, 1),
                 'profit_usd': round(t.profit_usd, 2), 'tech_score': t.tech_score,
                 'ml_score': t.ml_score, 'momentum_score': t.momentum_score,
                 'confidence': round(t.confidence, 1), 'scenario': t.scenario}
                for t in sorted(ct, key=lambda x: x.entry_time)]
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"📄 CSV saved: {path}")


def main():
    print("=" * 60)
    print("  BACKTEST STEP 1 v3: ml_vs_tech_conflito (3-way)")
    print("=" * 60)

    if not connect():
        return

    try:
        import scripts.run_backtest as rbt
        rbt.BT_START = BT_START
        rbt.BT_END   = BT_END

        data = collect_all_data()
        for key in ['h1', 'h4', 'm5']:
            if data[key].empty:
                print(f"❌ Missing {key} data. Aborting.")
                return

        bt_predictor = BacktestMLPredictor()
        if not bt_predictor.load_model():
            print("❌ Failed to load ML models")
            return

        feb20_a:  list = []
        feb20_b1: list = []
        feb20_b2: list = []

        # ── Run A: Baseline ────────────────────────────────────────────────
        print("\n" + "─" * 60)
        print("  Run A: Baseline")
        print("─" * 60)
        trades_a, _, _ = _run_loop(data, bt_predictor, "Run A (Baseline)", feb20_log=feb20_a)

        # ── Run B1: tech+ML conflict ────────────────────────────────────────
        print("\n" + "─" * 60)
        print("  Run B1: ml_vs_tech_conflito (tech>=65, ml<=40)")
        print("─" * 60)
        orig_fn = _patch_central_brain()
        try:
            trades_b1, det_b1, blk_b1 = _run_loop(
                data, bt_predictor, "Run B1 (Conflict)",
                feb20_log=feb20_b1, conflict_key=ML_CONFLICT_KEY,
            )
        finally:
            _unpatch_central_brain(orig_fn)

        # ── Run B2: tech+ML+momentum conflict ──────────────────────────────
        print("\n" + "─" * 60)
        print("  Run B2: ml_vs_tech_conflito_mom (tech>=65, ml<=40, mom>=70)")
        print("─" * 60)
        orig_fn2 = _patch_central_brain_mom()
        try:
            trades_b2, det_b2, blk_b2 = _run_loop(
                data, bt_predictor, "Run B2 (Mom Conflict)",
                feb20_log=feb20_b2, conflict_key=ML_CONFLICT_MOM_KEY,
            )
        finally:
            _unpatch_central_brain(orig_fn2)

        # ── Report ─────────────────────────────────────────────────────────
        report = generate_report(
            trades_a, trades_b1, trades_b2,
            feb20_a, feb20_b1, feb20_b2,
            det_b1, det_b2, blk_b1, blk_b2,
        )
        print(report)

        ts = datetime.now().strftime('%Y%m%d_%H%M')
        report_path = os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_{ts}.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n📄 Report saved: {report_path}")

        _save_csv(trades_b1, ML_CONFLICT_KEY,
                  os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_{ts}_b1.csv"))
        _save_csv(trades_b2, ML_CONFLICT_MOM_KEY,
                  os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_{ts}_b2.csv"))

    finally:
        mt5.shutdown()
        print("\n✅ MT5 disconnected. Done.")


if __name__ == "__main__":
    main()
