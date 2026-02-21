"""
BACKTEST STEP 1: ml_vs_tech_conflito Scenario
==============================================
Run A (baseline) vs Run B (new scenario) over 2025-08-18 → 2026-02-20 23:59.

New scenario trigger: tech_score >= 65 AND ml_score <= 40 AND ml_confidence > 0.65
Effect: technical=40%, ml=15% (vs ML dominating at 35%)
Bug fix: conflict check runs INSIDE zona_sr_forte block before its early return.

Usage:
    python scripts/backtest_ml_conflict_scenario.py

Output:
    data/backtest_ml_conflict_<timestamp>.txt
    data/backtest_ml_conflict_<timestamp>_affected.csv
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

ML_CONFLICT_KEY     = "ml_vs_tech_conflito"
ML_CONFLICT_WEIGHTS = {"technical": 0.45, "ml": 0.10, "momentum": 0.22,
                       "news": 0.15, "calendar": 0.08}
ML_CONFLICT_MULT    = 0.95
CONFLICT_TECH_MIN   = 65.0
CONFLICT_ML_MAX     = 40.0
# ml_confidence condition REMOVED per user request

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


def _unpatch_central_brain(orig_fn):
    import central_brain as cb
    cb._identify_scenario = orig_fn
    cb.SCENARIO_WEIGHTS.pop(ML_CONFLICT_KEY, None)


# ============================================================================
# CORE BACKTEST LOOP
# ============================================================================

def _run_loop(data: Dict, bt_predictor, label: str,
              feb20_log: Optional[list] = None) -> tuple:
    """Returns (trades, scenario_detections) where scenario_detections is count of times ml_vs_tech_conflito was detected."""
    df_h1 = data['h1'].copy()
    df_h4 = data['h4'].copy()
    df_d1 = data.get('d1', pd.DataFrame()).copy()
    df_m5 = data['m5'].copy()

    trades: List[SimTrade] = []
    open_trades: List[SimTrade] = []
    ticket_counter = 4000000
    scenario_detections = 0  # Count scenario detections (not just trades)

    last_trade_time = {'BUY': None, 'SELL': None}
    last_close_type = {'BUY': None, 'SELL': None}

    bt_mask    = (df_h1['datetime'] >= BT_START) & (df_h1['datetime'] <= BT_END)
    bt_indices = df_h1[bt_mask].index.tolist()

    if not bt_indices:
        print(f"  ❌ No H1 candles for {label}")
        return [], 0

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
        if scenario == ML_CONFLICT_KEY:
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
        h1_hour  = h1_time.hour
        ch, oh   = config.MARKET_DAILY_CLOSE_HOUR, config.MARKET_DAILY_OPEN_HOUR
        cb_min   = config.MARKET_CLOSE_BUFFER_MINUTES
        ob_min   = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
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

        if trade.close_time and trade.close_time > h1_time:
            open_trades.append(trade)
        else:
            last_close_type[direction] = (
                "tp" if "tp" in trade.close_reason else
                "trailing" if trade.profit_pips >= 0 else "sl"
            )
            last_trade_time[direction] = trade.close_time or h1_time

    return trades, scenario_detections


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
# REPORT
# ============================================================================

def generate_report(trades_a: List[SimTrade], trades_b: List[SimTrade],
                    feb20_a: list, feb20_b: list,
                    detections_a: int = 0, detections_b: int = 0) -> str:
    lines = []
    SEP  = "=" * 70
    THIN = "─" * 70

    lines.append(SEP)
    lines.append("  BACKTEST STEP 1: ml_vs_tech_conflito Scenario")
    lines.append(f"  Period : {BT_START.strftime('%Y-%m-%d')} → {BT_END.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"  Run A  : Baseline (current system, unmodified)")
    lines.append(f"  Run B  : ml_vs_tech_conflito ACTIVE")
    lines.append(f"  Trigger: tech >= {CONFLICT_TECH_MIN:.0f}  AND  ml <= {CONFLICT_ML_MAX:.0f}")
    lines.append(f"  Weights: tech={ML_CONFLICT_WEIGHTS['technical']:.0%}  ml={ML_CONFLICT_WEIGHTS['ml']:.0%}  "
                 f"mom={ML_CONFLICT_WEIGHTS['momentum']:.0%}  news={ML_CONFLICT_WEIGHTS['news']:.0%}  "
                 f"cal={ML_CONFLICT_WEIGHTS['calendar']:.0%}")
    lines.append(SEP)

    sa = _calc_stats(trades_a)
    sb = _calc_stats(trades_b)

    lines.append(f"\n{'':>28} {'Run A (Baseline)':>18} {'Run B (Conflict)':>18} {'Delta':>10}")
    lines.append(THIN)
    lines.append(f"  {'Trades':<26} {sa['trades']:>18} {sb['trades']:>18} {sb['trades']-sa['trades']:>+10}")
    lines.append(f"  {'Wins':<26} {sa['wins']:>18} {sb['wins']:>18} {sb['wins']-sa['wins']:>+10}")
    lines.append(f"  {'Losses':<26} {sa['losses']:>18} {sb['losses']:>18} {sb['losses']-sa['losses']:>+10}")
    lines.append(f"  {'Win Rate %':<26} {sa['wr']:>17.1f}% {sb['wr']:>17.1f}% {sb['wr']-sa['wr']:>+9.1f}%")
    lines.append(f"  {'P&L $':<26} ${sa['pnl_usd']:>+16.2f} ${sb['pnl_usd']:>+16.2f} ${sb['pnl_usd']-sa['pnl_usd']:>+8.2f}")
    lines.append(f"  {'Pips':<26} {sa['pips']:>+17.1f} {sb['pips']:>+17.1f} {sb['pips']-sa['pips']:>+9.1f}")
    lines.append(f"  {'Profit Factor':<26} {sa['pf']:>18.2f} {sb['pf']:>18.2f} {sb['pf']-sa['pf']:>+10.2f}")
    lines.append(f"  {'Max Drawdown $':<26} ${sa['max_dd']:>16.2f} ${sb['max_dd']:>16.2f} ${sb['max_dd']-sa['max_dd']:>+8.2f}")
    lines.append(f"  {'Avg Win (pips)':<26} {sa['avg_win_pips']:>+17.1f} {sb['avg_win_pips']:>+17.1f} {sb['avg_win_pips']-sa['avg_win_pips']:>+9.1f}")
    lines.append(f"  {'Avg Loss (pips)':<26} {sa['avg_loss_pips']:>+17.1f} {sb['avg_loss_pips']:>+17.1f} {sb['avg_loss_pips']-sa['avg_loss_pips']:>+9.1f}")

    # ── Scenario trigger analysis ───────────────────────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  🎯 SCENARIO TRIGGER ANALYSIS (Run B only)")
    lines.append(THIN)

    ct   = [t for t in trades_b if t.scenario == ML_CONFLICT_KEY]
    ct_w = [t for t in ct if t.profit_pips > 0]
    ct_l = [t for t in ct if t.profit_pips <= 0]
    ct_gp = sum(t.profit_usd for t in ct_w)
    ct_gl = abs(sum(t.profit_usd for t in ct_l))
    ct_pf = ct_gp / ct_gl if ct_gl > 0 else float('inf')

    lines.append(f"\n  Scenario detected  : {detections_b} times (H1 candles where trigger fired)")
    lines.append(f"  Trades opened      : {len(ct)} (subset that passed all filters)")
    if ct:
        lines.append(f"  Win / Loss         : {len(ct_w)} W / {len(ct_l)} L")
        lines.append(f"  Win Rate           : {len(ct_w)/len(ct)*100:.1f}%")
        lines.append(f"  P&L $              : ${sum(t.profit_usd for t in ct):+.2f}")
        lines.append(f"  Pips               : {sum(t.profit_pips for t in ct):+.1f}")
        lines.append(f"  Profit Factor      : {ct_pf:.2f}")
        lines.append(f"\n  {'Date':>10} {'Time':>5} {'Dir':>4} {'Tech':>5} {'ML':>5} {'Result':>6} {'Pips':>8} {'Close':>12}")
        for t in sorted(ct, key=lambda x: x.entry_time):
            result = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(
                f"  {t.entry_time.strftime('%Y-%m-%d'):>10} "
                f"{t.entry_time.strftime('%H:%M'):>5} "
                f"{t.direction:>4} "
                f"{t.tech_score:>5.1f} "
                f"{t.ml_score:>5.1f} "
                f"{result:>6} "
                f"{t.profit_pips:>+8.1f} "
                f"{t.close_reason:>12}"
            )
    else:
        lines.append(f"  (scenario never triggered in Run B)")

    # ── Trades added by Run B ───────────────────────────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  📈 TRADES ADDED BY RUN B (new signals not in baseline)")
    lines.append(THIN)

    keys_a  = {(t.entry_time, t.direction) for t in trades_a}
    keys_b  = {(t.entry_time, t.direction) for t in trades_b}
    added   = [t for t in trades_b if (t.entry_time, t.direction) not in keys_a]
    removed = [t for t in trades_a if (t.entry_time, t.direction) not in keys_b]

    if added:
        add_w  = [t for t in added if t.profit_pips > 0]
        add_gp = sum(t.profit_usd for t in add_w)
        add_gl = abs(sum(t.profit_usd for t in added if t.profit_pips <= 0))
        add_pf = add_gp / add_gl if add_gl > 0 else float('inf')
        lines.append(f"\n  Count: {len(added)}  |  {len(add_w)}W/{len(added)-len(add_w)}L  "
                     f"|  WR={len(add_w)/len(added)*100:.1f}%  |  PF={add_pf:.2f}  "
                     f"|  P&L=${sum(t.profit_usd for t in added):+.2f}")
        lines.append(f"  {'Date':>10} {'Time':>5} {'Dir':>4} {'Scenario':>22} {'Result':>6} {'Pips':>8}")
        for t in sorted(added, key=lambda x: x.entry_time):
            result = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(f"  {t.entry_time.strftime('%Y-%m-%d'):>10} "
                         f"{t.entry_time.strftime('%H:%M'):>5} "
                         f"{t.direction:>4} "
                         f"{t.scenario:>22} "
                         f"{result:>6} "
                         f"{t.profit_pips:>+8.1f}")
    else:
        lines.append(f"\n  (no new trades added by Run B)")

    # ── Trades removed by Run B (regression check) ─────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  ⚠️  TRADES REMOVED BY RUN B (regression check)")
    lines.append(THIN)

    if removed:
        rem_w  = [t for t in removed if t.profit_pips > 0]
        lines.append(f"\n  Count: {len(removed)}  |  {len(rem_w)}W/{len(removed)-len(rem_w)}L  "
                     f"|  WR={len(rem_w)/len(removed)*100:.1f}%  "
                     f"|  P&L=${sum(t.profit_usd for t in removed):+.2f}")
        lines.append(f"  {'Date':>10} {'Time':>5} {'Dir':>4} {'Scenario (A)':>22} {'Result':>6} {'Pips':>8}")
        for t in sorted(removed, key=lambda x: x.entry_time):
            result = "WIN" if t.profit_pips > 0 else "LOSS"
            lines.append(f"  {t.entry_time.strftime('%Y-%m-%d'):>10} "
                         f"{t.entry_time.strftime('%H:%M'):>5} "
                         f"{t.direction:>4} "
                         f"{t.scenario:>22} "
                         f"{result:>6} "
                         f"{t.profit_pips:>+8.1f}")
        rem_wr = len(rem_w) / len(removed) * 100
        if rem_wr > 55:
            lines.append(f"\n  ⚠️ WARNING: {rem_wr:.0f}% of removed trades were winners — scenario may be too aggressive")
        else:
            lines.append(f"\n  ✅ {rem_wr:.0f}% of removed trades were winners — acceptable regression")
    else:
        lines.append(f"\n  (no trades removed — no regression)")

    # ── Feb 20 validation ──────────────────────────────────────────────────
    lines.append(f"\n{THIN}")
    lines.append(f"  📅 FEB 20 VALIDATION (07:00–16:00 UTC) — THE MISSED MOVE")
    lines.append(THIN)

    for run_label, log in [("Run A (Baseline)", feb20_a), ("Run B (Conflict)", feb20_b)]:
        lines.append(f"\n  {run_label}:")
        if log:
            lines.append(f"  {'Time':>5} {'Decision':>12} {'Score':>6} {'Conf':>6} {'Scenario':>24} {'Tech':>5} {'ML':>5} {'Mom':>5}")
            for d in log:
                lines.append(
                    f"  {d['time']:>5} {d['decision']:>12} {d['score']:>6.1f} "
                    f"{d['confidence']:>6.1f} {d['scenario']:>24} "
                    f"{d['tech']:>5.1f} {d['ml']:>5.1f} {d['momentum']:>5.1f}"
                )
        else:
            lines.append(f"  (no data)")

    # Feb 20 trades
    feb20_trades_b = [t for t in trades_b
                      if t.entry_time and t.entry_time.date() == datetime(2026, 2, 20).date()]
    feb20_trades_a = [t for t in trades_a
                      if t.entry_time and t.entry_time.date() == datetime(2026, 2, 20).date()]

    lines.append(f"\n  Trades on Feb 20:")
    lines.append(f"  Run A: {len(feb20_trades_a)} trades")
    for t in sorted(feb20_trades_a, key=lambda x: x.entry_time):
        result = "WIN" if t.profit_pips > 0 else "LOSS"
        lines.append(f"    {t.entry_time.strftime('%H:%M')} {t.direction} @ {t.entry_price:.2f} "
                     f"→ {t.close_price:.2f} ({t.close_reason}) {result} {t.profit_pips:+.1f}p")
    lines.append(f"  Run B: {len(feb20_trades_b)} trades")
    for t in sorted(feb20_trades_b, key=lambda x: x.entry_time):
        result = "WIN" if t.profit_pips > 0 else "LOSS"
        lines.append(f"    {t.entry_time.strftime('%H:%M')} {t.direction} @ {t.entry_price:.2f} "
                     f"→ {t.close_price:.2f} ({t.close_reason}) {result} {t.profit_pips:+.1f}p  [{t.scenario}]")

    # ── Verdict ────────────────────────────────────────────────────────────
    lines.append(f"\n{SEP}")
    lines.append(f"  VERDICT")
    lines.append(SEP)

    pf_delta = sb['pf'] - sa['pf']
    wr_delta = sb['wr'] - sa['wr']
    pnl_delta = sb['pnl_usd'] - sa['pnl_usd']

    lines.append(f"\n  PF delta  : {pf_delta:>+.2f}  (threshold: ADOPT >= +0.10, ABANDON <= -0.05)")
    lines.append(f"  WR delta  : {wr_delta:>+.1f}%  (threshold: ADOPT >= -2%, ABANDON <= -5%)")
    lines.append(f"  P&L delta : ${pnl_delta:>+.2f}")

    if pf_delta >= 0.10 and wr_delta >= -2.0:
        verdict = "✅ ADOPT"
        detail  = "PF and WR both meet thresholds — deploy after review"
    elif pf_delta <= -0.05 or wr_delta <= -5.0:
        verdict = "❌ ABANDON"
        detail  = "PF or WR degraded beyond threshold — do NOT deploy"
    else:
        verdict = "⚠️ MONITOR"
        detail  = "Mixed results — review affected trades before deciding"

    lines.append(f"\n  >>> {verdict}: {detail}")
    lines.append(f"\n{'=' * 70}")

    return "\n".join(lines)


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print("  BACKTEST STEP 1: ml_vs_tech_conflito")
    print("=" * 60)

    if not connect():
        return

    try:
        # Override BT_START/BT_END in run_backtest module BEFORE collect_all_data
        import scripts.run_backtest as rbt
        rbt.BT_START = BT_START
        rbt.BT_END   = BT_END

        data = collect_all_data()
        for key in ['h1', 'h4', 'm5']:
            if data[key].empty:
                print(f"❌ Missing {key} data. Aborting.")
                return

        # Load ML predictor once (shared between both runs)
        bt_predictor = BacktestMLPredictor()
        if not bt_predictor.load_model():
            print("❌ Failed to load ML models")
            return

        feb20_a: list = []
        feb20_b: list = []

        # ── Run A: Baseline ────────────────────────────────────────────────
        print("\n" + "─" * 60)
        print("  Run A: Baseline (no changes)")
        print("─" * 60)
        trades_a, detections_a = _run_loop(data, bt_predictor, "Run A (Baseline)", feb20_log=feb20_a)

        # ── Run B: ml_vs_tech_conflito active ─────────────────────────────
        print("\n" + "─" * 60)
        print("  Run B: ml_vs_tech_conflito ACTIVE")
        print("─" * 60)
        orig_fn = _patch_central_brain()
        try:
            trades_b, detections_b = _run_loop(data, bt_predictor, "Run B (Conflict)", feb20_log=feb20_b)
        finally:
            _unpatch_central_brain(orig_fn)

        # ── Report ─────────────────────────────────────────────────────────
        report = generate_report(trades_a, trades_b, feb20_a, feb20_b, detections_a, detections_b)
        print(report)

        # Save report
        ts = datetime.now().strftime('%Y%m%d_%H%M')
        report_path = os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_{ts}.txt")
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(f"\n📄 Report saved: {report_path}")

        # Save affected trades CSV
        ct = [t for t in trades_b if t.scenario == ML_CONFLICT_KEY]
        if ct:
            rows = [{
                'entry_time': t.entry_time, 'direction': t.direction,
                'entry_price': t.entry_price, 'close_price': t.close_price,
                'close_reason': t.close_reason, 'profit_pips': round(t.profit_pips, 1),
                'profit_usd': round(t.profit_usd, 2),
                'tech_score': t.tech_score, 'ml_score': t.ml_score,
                'momentum_score': t.momentum_score, 'confidence': round(t.confidence, 1),
                'scenario': t.scenario,
            } for t in sorted(ct, key=lambda x: x.entry_time)]
            csv_path = os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_{ts}_affected.csv")
            pd.DataFrame(rows).to_csv(csv_path, index=False)
            print(f"📄 Affected trades CSV: {csv_path}")

    finally:
        mt5.shutdown()
        print("\n✅ MT5 disconnected. Done.")


if __name__ == "__main__":
    main()
