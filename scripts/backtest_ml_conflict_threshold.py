"""
BACKTEST STEP 2: ml_vs_tech_conflito + BUY threshold override (60 vs 58)
======================================================================
Run A (baseline) vs Run B (conflict + threshold 60) vs Run C (conflict + threshold 58).
Period: 2025-08-18 → 2026-02-20 23:59

Usage:
    python scripts/backtest_ml_conflict_threshold.py

Output:
    data/backtest_ml_conflict_threshold_<timestamp>.txt
    data/backtest_ml_conflict_threshold_<timestamp>_b60_forced.csv
    data/backtest_ml_conflict_threshold_<timestamp>_b58_forced.csv
"""

import os
import sys
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Tuple

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
    analyze_with_brain,
    is_actionable_signal,
    get_trade_direction,
    _check_mtf_trend_alignment,
)
from risk_manager import calculate_sl_tp
from support_resistance import detect_zones_dual, get_sr_context, adjust_sl_tp_for_sr, is_near_strong_zone

BT_START = datetime(2025, 8, 18)
BT_END = datetime(2026, 2, 20, 23, 59)

ML_CONFLICT_KEY = "ml_vs_tech_conflito"
ML_CONFLICT_WEIGHTS = {
    "technical": 0.45,
    "ml": 0.10,
    "momentum": 0.22,
    "news": 0.15,
    "calendar": 0.08,
}
ML_CONFLICT_MULT = 0.95
CONFLICT_TECH_MIN = 65.0
CONFLICT_ML_MAX = 40.0

CONFLICT_BUY_THRESHOLD_NORMAL = 65.0

NEUTRAL_NEWS = {
    "score": 50.0,
    "dxy": {"value": 104.0, "change_24h": 0.0, "trend": "stable"},
    "yields": {"value": 4.5, "change_24h": 0.0, "trend": "stable"},
    "vix": {"value": 17.0, "level": "low"},
    "sentiment": {"headlines_score": 50, "normalized": 0},
    "high_impact_news_soon": False,
    "geopolitical_risk": "low",
    "anomalies": [],
}


_ORIG_IDENTIFY_SCENARIO = None
_ORIG_MAKE_DECISION = None


def _unpatch_to_baseline():
    """
    Remove ml_vs_tech_conflito from production code so Run A is a true baseline.
    """
    global _ORIG_IDENTIFY_SCENARIO, _ORIG_MAKE_DECISION
    import central_brain as cb

    _ORIG_IDENTIFY_SCENARIO = cb._identify_scenario
    _ORIG_MAKE_DECISION = cb._make_decision

    cb.SCENARIO_WEIGHTS.pop(ML_CONFLICT_KEY, None)

    def _baseline_identify(tech_data, ml_data, momentum_data, news_data, momentum_strength,
                           calendar_data=None, volatility_status=None, sr_data=None):
        result = _ORIG_IDENTIFY_SCENARIO(
            tech_data, ml_data, momentum_data, news_data, momentum_strength,
            calendar_data=calendar_data,
            volatility_status=volatility_status,
            sr_data=sr_data,
        )
        if result[0] == ML_CONFLICT_KEY:
            tech_score = tech_data.get("score", 50)
            ml_score = ml_data.get("score", 50)
            tech_bullish = tech_score >= 55
            tech_bearish = tech_score <= 45
            ml_bullish = ml_score >= 55
            ml_bearish = ml_score <= 45
            if (tech_bullish and ml_bearish) or (tech_bearish and ml_bullish):
                return "sinais_conflitantes", "Technical and ML signals in conflict", 0.80
            return "padrao", "Default scenario", 1.00
        return result

    def _baseline_decision(final_score: float, scenario: str) -> str:
        if scenario == ML_CONFLICT_KEY:
            scenario = "padrao"
        return _ORIG_MAKE_DECISION(final_score, scenario)

    cb._identify_scenario = _baseline_identify
    cb._make_decision = _baseline_decision


def _restore_production():
    """
    Restore production code (with ml_vs_tech_conflito + threshold 58).
    """
    global _ORIG_IDENTIFY_SCENARIO, _ORIG_MAKE_DECISION
    import central_brain as cb

    if _ORIG_IDENTIFY_SCENARIO is not None:
        cb._identify_scenario = _ORIG_IDENTIFY_SCENARIO
    if _ORIG_MAKE_DECISION is not None:
        cb._make_decision = _ORIG_MAKE_DECISION
    cb.SCENARIO_WEIGHTS[ML_CONFLICT_KEY] = ML_CONFLICT_WEIGHTS.copy()


def _patch_threshold_60():
    """
    For Run B: override BUY threshold to 60 instead of production's 58.
    """
    import central_brain as cb

    _orig_decision = cb._make_decision

    def _decision_60(final_score: float, scenario: str) -> str:
        if scenario == ML_CONFLICT_KEY:
            if final_score >= 75:
                return "STRONG_BUY"
            elif final_score >= 60:
                return "BUY"
            elif final_score <= 25:
                return "STRONG_SELL"
            elif final_score <= 35:
                return "SELL"
            else:
                return "HOLD"
        return _orig_decision(final_score, scenario)

    cb._make_decision = _decision_60
    return _orig_decision


def _unpatch_threshold(orig_fn):
    import central_brain as cb
    cb._make_decision = orig_fn


def _calc_stats(trades: List[SimTrade]) -> Dict:
    if not trades:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "wr": 0.0,
            "pnl_usd": 0.0,
            "pips": 0.0,
            "pf": 0.0,
            "max_dd": 0.0,
        }

    pnl = [t.profit_usd for t in trades]
    eq = np.cumsum(pnl)
    peaks = np.maximum.accumulate(eq)
    dd = peaks - eq
    max_dd = float(np.max(dd)) if len(dd) else 0.0

    wins = [t for t in trades if t.profit_usd > 0]
    losses = [t for t in trades if t.profit_usd <= 0]
    gp = sum(t.profit_usd for t in wins)
    gl = abs(sum(t.profit_usd for t in losses))

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "wr": (len(wins) / len(trades) * 100) if trades else 0.0,
        "pnl_usd": sum(t.profit_usd for t in trades),
        "pips": sum(t.profit_pips for t in trades),
        "pf": (gp / gl) if gl > 0 else float("inf"),
        "max_dd": max_dd,
    }


def _run_loop(data: Dict, bt_predictor, label: str,
              feb20_log: Optional[list] = None,
              conflict_buy_threshold: Optional[float] = None) -> Tuple[List[SimTrade], int, List[SimTrade]]:
    df_h1 = data["h1"].copy().reset_index(drop=True)
    df_h4 = data["h4"].copy().reset_index(drop=True)
    df_d1 = data.get("d1", pd.DataFrame()).copy().reset_index(drop=True)
    df_m5 = data["m5"].copy().reset_index(drop=True)

    trades: List[SimTrade] = []
    open_trades: List[SimTrade] = []
    forced_trades: List[SimTrade] = []

    ticket_counter = 5000000
    scenario_detections = 0

    last_trade_time = {"BUY": None, "SELL": None}
    last_close_type = {"BUY": None, "SELL": None}

    bt_mask = (df_h1["datetime"] >= BT_START) & (df_h1["datetime"] <= BT_END)
    bt_indices = df_h1[bt_mask].index.tolist()
    if not bt_indices:
        print(f"  ❌ No H1 candles for {label}")
        return [], 0, []

    total = len(bt_indices)
    print(f"\n🔄 {label}: {total} H1 candles ({BT_START:%Y-%m-%d} → {BT_END:%Y-%m-%d})")

    for idx_i, idx in enumerate(bt_indices):
        h1_candle = df_h1.iloc[idx]
        h1_time = h1_candle["datetime"]
        if hasattr(h1_time, "to_pydatetime"):
            h1_time = h1_time.to_pydatetime()
        current_price = float(h1_candle["close"])

        if idx_i % 100 == 0:
            print(f"  {idx_i/total*100:>3.0f}% — {h1_time:%Y-%m-%d %H:%M} — {len(trades)} trades")

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

        if idx < H1_WARMUP:
            continue

        h1_slice = df_h1.iloc[: idx + 1].copy()

        h1_slice = calculate_indicators(h1_slice)
        if len(h1_slice) < 50:
            continue
        tech_data = analyze_technical_detailed(h1_slice)

        h4_feats = compute_h4_features(df_h4, h1_time)
        m5_feats = compute_m5_features(df_m5, h1_time)
        bt_predictor.set_h4_features(h4_feats)
        bt_predictor.set_m5_features(m5_feats)

        try:
            ml_result = bt_predictor.predict(h1_slice, NEUTRAL_NEWS)
            ml_data = {
                "score": float(ml_result["score"]),
                "score_h1": float(ml_result.get("score_h1", ml_result["score"])),
                "score_h4": float(ml_result.get("score_h4", ml_result["score"])),
                "prediction": (
                    "bullish" if ml_result.get("direction") == "BUY" else
                    ("bearish" if ml_result.get("direction") == "SELL" else "neutral")
                ),
                "probability": float(ml_result.get("raw_proba", ml_result.get("probability", 0.5))),
                "max_confidence": float(ml_result.get("max_confidence", 0.5)),
                "pattern": "undefined",
                "similar_patterns_count": None,
                "historical_success_rate": None,
                "error": ml_result.get("error"),
            }
        except Exception as e:
            ml_data = {
                "score": 50.0,
                "prediction": "neutral",
                "probability": 0.5,
                "max_confidence": 0.5,
                "pattern": "undefined",
                "similar_patterns_count": None,
                "historical_success_rate": None,
                "error": str(e),
            }
        momentum_data = analyze_momentum(h1_slice)

        m5_status = compute_m5_status(df_m5, h1_time)
        vol_status = compute_volatility_status(df_m5, h1_time)

        sr_brain_data = None
        sr_zones = []
        h1_for_sr = df_h1.iloc[: idx + 1].copy()
        h4_for_sr = df_h4[df_h4["datetime"] <= h1_time].copy()
        sr_zones = detect_zones_dual(
            h1_for_sr,
            h4_for_sr,
            merge_pips=config.SR_ZONE_MERGE_PIPS,
            max_age_bars=config.SR_ZONE_MAX_AGE_BARS,
            min_touches=config.SR_MIN_TOUCHES,
        )
        if sr_zones:
            atr_sr = get_atr_value(h1_slice)
            sr_ctx = get_sr_context(
                sr_zones,
                float(h1_slice["close"].iloc[-1]),
                atr_sr,
                direction=None,
                confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
            )
            near_zone, zone_info = is_near_strong_zone(
                sr_zones,
                float(h1_slice["close"].iloc[-1]),
                atr_sr,
                min_touches=config.SR_SCENARIO_MIN_TOUCHES,
            )
            sr_brain_data = {
                "confidence_adjustment": 0.0,
                "confirmations": [],
                "alerts": [],
                "description": sr_ctx.description,
                "near_strong_zone": near_zone,
                "near_zone_info": {
                    "midpoint": zone_info.midpoint,
                    "touches": zone_info.touches,
                    "zone_type": zone_info.zone_type,
                } if zone_info else None,
                "all_zones": sr_zones,
            }

        brain_result = analyze_with_brain(
            tech_data,
            ml_data,
            momentum_data,
            NEUTRAL_NEWS,
            current_price,
            calendar_data=NEUTRAL_CALENDAR,
            volatility_status=vol_status,
            m5_data=m5_status,
            sr_data=sr_brain_data,
        )

        decision = brain_result.decision
        confidence = brain_result.confidence
        scenario = brain_result.scenario
        final_score = float(getattr(brain_result, "final_score", 50.0))

        if scenario == ML_CONFLICT_KEY:
            scenario_detections += 1

        forced_override = False
        if conflict_buy_threshold is not None and scenario == ML_CONFLICT_KEY:
            if decision == "HOLD" and final_score >= float(conflict_buy_threshold):
                decision = "BUY"
                forced_override = True

        if feb20_log is not None:
            if h1_time.date() == datetime(2026, 2, 20).date() and 7 <= h1_time.hour <= 16:
                feb20_log.append({
                    "time": h1_time.strftime("%H:%M"),
                    "decision": decision,
                    "score": round(final_score, 1),
                    "confidence": round(float(confidence), 1),
                    "scenario": scenario,
                    "tech": round(float(tech_data.get("score", 50)), 1),
                    "ml": round(float(ml_data.get("score", 50)), 1),
                    "momentum": round(float(momentum_data.get("score", 50)), 1),
                    "forced": forced_override,
                })

        if not is_actionable_signal(decision):
            continue
        if float(confidence) < config.BRAIN_MIN_CONFIDENCE:
            continue

        direction = get_trade_direction(decision)
        if direction is None:
            continue

        if sr_zones:
            atr_sr = get_atr_value(h1_slice)
            sr_dir = get_sr_context(
                sr_zones,
                current_price,
                atr_sr,
                direction=direction,
                confidence_penalty_max=config.SR_CONFIDENCE_PENALTY_MAX,
                confidence_bonus_max=config.SR_CONFIDENCE_BONUS_MAX,
                penalty_proximity_atr=config.SR_PENALTY_PROXIMITY_ATR,
                penalty_min_touches=config.SR_PENALTY_MIN_TOUCHES,
            )
            if sr_dir.confidence_adjustment != 0:
                confidence = max(0, min(100, float(confidence) + sr_dir.confidence_adjustment))

        if getattr(config, 'MTF_TREND_ENABLED', True) and len(df_d1) > 0 and len(df_h4) > 0:
            d1_trend, h4_trend = compute_mtf_trend(
                df_d1,
                df_h4,
                h1_time,
                ema_period=getattr(config, 'MTF_EMA_PERIOD', 50),
            )
            mtf_adj, _, _ = _check_mtf_trend_alignment(decision, d1_trend, h4_trend)
            if mtf_adj != 0:
                confidence = max(0, min(100, float(confidence) + mtf_adj))

        h1_hour = h1_time.hour
        ch, oh = config.MARKET_DAILY_CLOSE_HOUR, config.MARKET_DAILY_OPEN_HOUR
        cb_min = config.MARKET_CLOSE_BUFFER_MINUTES
        ob_min = getattr(config, "MARKET_OPEN_BUFFER_MINUTES", 60)
        if 0 <= (ch * 60) - (h1_hour * 60) <= cb_min:
            continue
        if 0 <= (h1_hour * 60) - (oh * 60) < ob_min:
            continue

        m5_rev = compute_m5_reversal(df_m5, h1_time, direction)
        if m5_rev["reversal_detected"]:
            if m5_rev["reversal_strength"] == "strong":
                continue
            confidence = float(confidence) - config.M5_REVERSAL_CONFIDENCE_PENALTY
            if confidence < config.BRAIN_MIN_CONFIDENCE:
                continue

        lt = last_trade_time.get(direction)
        if lt is not None:
            ct = last_close_type.get(direction)
            min_min = (
                config.MIN_MINUTES_AFTER_TRAILING
                if ct == "trailing"
                else config.MIN_MINUTES_AFTER_SL
                if ct == "sl"
                else config.MIN_MINUTES_BETWEEN_TRADES
            )
            if (h1_time - lt).total_seconds() / 60 < min_min:
                continue

        same_dir = [t for t in open_trades if t.direction == direction]
        if same_dir:
            blocked = any(
                (
                    ((current_price - t.entry_price) / t.entry_price * 100)
                    if t.direction == "BUY"
                    else ((t.entry_price - current_price) / t.entry_price * 100)
                )
                < config.PYRAMID_MIN_PROFIT_PERCENT
                for t in same_dir
            )
            if blocked:
                continue

        if len(open_trades) >= config.MAX_POSITIONS:
            continue

        atr = get_atr_value(h1_slice)
        levels = calculate_sl_tp(current_price, direction, atr)
        sl_price = levels.stop_loss
        tp_price = levels.take_profit_1

        if sr_zones:
            adj_sl, adj_tp, _ = adjust_sl_tp_for_sr(
                current_price,
                sl_price,
                tp_price,
                direction,
                atr,
                sr_zones,
                sl_adjust_enabled=False,
                tp_adjust_enabled=True,
            )
            sl_price, tp_price = adj_sl, adj_tp

        ticket_counter += 1
        trade = SimTrade(
            ticket=ticket_counter,
            direction=direction,
            entry_price=current_price,
            entry_time=h1_time,
            sl=float(sl_price),
            tp=float(tp_price),
            atr=float(atr),
            brain_score=final_score,
            confidence=float(confidence),
            scenario=scenario,
            scenario_desc=getattr(brain_result, "scenario_desc", scenario),
            explanation_snippet="",
            tech_score=float(tech_data.get("score", 50)),
            ml_score=float(ml_data.get("score", 50)),
            momentum_score=float(momentum_data.get("score", 50)),
            is_pyramid=len(same_dir) > 0,
        )

        [trade] = simulate_trades_concurrent([trade], df_m5, early_exit_enabled=False)
        trades.append(trade)
        if forced_override:
            forced_trades.append(trade)

        if trade.close_time and trade.close_time > h1_time:
            open_trades.append(trade)
        else:
            last_close_type[direction] = (
                "tp"
                if "tp" in trade.close_reason
                else "trailing"
                if trade.profit_pips >= 0
                else "sl"
            )
            last_trade_time[direction] = trade.close_time or h1_time

    return trades, scenario_detections, forced_trades


def _added_removed(trades_a: List[SimTrade], trades_b: List[SimTrade]):
    keys_a = {(t.entry_time, t.direction) for t in trades_a}
    keys_b = {(t.entry_time, t.direction) for t in trades_b}
    added = [t for t in trades_b if (t.entry_time, t.direction) not in keys_a]
    removed = [t for t in trades_a if (t.entry_time, t.direction) not in keys_b]
    return added, removed


def _first_time_over(thr: float, feb_log: list) -> Optional[str]:
    for d in feb_log:
        if d.get("scenario") == ML_CONFLICT_KEY and float(d.get("score", 0)) >= thr:
            return d.get("time")
    return None


def generate_report(trades_a, trades_b60, trades_b58,
                    feb_a, feb_b60, feb_b58,
                    det_b60, det_b58,
                    forced_b60, forced_b58) -> str:
    lines = []
    SEP = "=" * 70
    THIN = "─" * 70

    sa = _calc_stats(trades_a)
    sb60 = _calc_stats(trades_b60)
    sb58 = _calc_stats(trades_b58)

    pf_a = sa["pf"] if sa["pf"] != float("inf") else 9.99
    pf_b60 = sb60["pf"] if sb60["pf"] != float("inf") else 9.99
    pf_b58 = sb58["pf"] if sb58["pf"] != float("inf") else 9.99

    lines.append(SEP)
    lines.append("  BACKTEST STEP 2: conflict scenario + BUY threshold override")
    lines.append(f"  Period: {BT_START:%Y-%m-%d} → {BT_END:%Y-%m-%d %H:%M}")
    lines.append(f"  Run A : Baseline")
    lines.append(f"  Run B : Conflict + BUY>=60 (only when {ML_CONFLICT_KEY})")
    lines.append(f"  Run C : Conflict + BUY>=58 (only when {ML_CONFLICT_KEY})")
    lines.append(SEP)

    W = 14
    lines.append(f"\n{'':>24} {'Run A':>{W}} {'Run B':>{W}} {'Run C':>{W}} {'ΔB':>8} {'ΔC':>8}")
    lines.append(THIN)
    lines.append(f"  {'Trades':<22} {sa['trades']:>{W}} {sb60['trades']:>{W}} {sb58['trades']:>{W}} "
                 f"{sb60['trades']-sa['trades']:>+8} {sb58['trades']-sa['trades']:>+8}")
    lines.append(f"  {'Win Rate %':<22} {sa['wr']:>{W-1}.1f}% {sb60['wr']:>{W-1}.1f}% {sb58['wr']:>{W-1}.1f}% "
                 f"{sb60['wr']-sa['wr']:>+7.1f}% {sb58['wr']-sa['wr']:>+7.1f}%")
    lines.append(f"  {'P&L $':<22} ${sa['pnl_usd']:>+{W-1}.2f} ${sb60['pnl_usd']:>+{W-1}.2f} ${sb58['pnl_usd']:>+{W-1}.2f} "
                 f"${sb60['pnl_usd']-sa['pnl_usd']:>+6.2f} ${sb58['pnl_usd']-sa['pnl_usd']:>+6.2f}")
    lines.append(f"  {'Profit Factor':<22} {pf_a:>{W}.2f} {pf_b60:>{W}.2f} {pf_b58:>{W}.2f} "
                 f"{pf_b60-pf_a:>+8.2f} {pf_b58-pf_a:>+8.2f}")
    lines.append(f"  {'Max Drawdown $':<22} ${sa['max_dd']:>{W-1}.2f} ${sb60['max_dd']:>{W-1}.2f} ${sb58['max_dd']:>{W-1}.2f} "
                 f"${sb60['max_dd']-sa['max_dd']:>+6.2f} ${sb58['max_dd']-sa['max_dd']:>+6.2f}")

    lines.append(f"\n{THIN}")
    lines.append("  🎯 CONFLICT DETECTIONS")
    lines.append(THIN)
    lines.append(f"  Run B detections: {det_b60} | forced trades opened: {len(forced_b60)}")
    lines.append(f"  Run C detections: {det_b58} | forced trades opened: {len(forced_b58)}")

    def _forced_section(title, forced):
        if not forced:
            lines.append(f"\n  {title}: 0")
            return
        wins = [t for t in forced if t.profit_pips > 0]
        losses = [t for t in forced if t.profit_pips <= 0]
        gp = sum(t.profit_usd for t in wins)
        gl = abs(sum(t.profit_usd for t in losses))
        pf = gp / gl if gl > 0 else float('inf')
        lines.append(f"\n  {title}: {len(forced)} trades")
        lines.append(f"  {len(wins)}W/{len(losses)}L  WR={len(wins)/len(forced)*100:.1f}%  PF={pf:.2f}  P&L=${sum(t.profit_usd for t in forced):+.2f}")
        lines.append(f"  {'Date':>10} {'Time':>5} {'Dir':>4} {'Score':>6} {'Conf':>6} {'Tech':>5} {'ML':>5} {'Mom':>5} {'Pips':>8} {'Close':>6}")
        for t in sorted(forced, key=lambda x: x.entry_time):
            lines.append(f"  {t.entry_time:%Y-%m-%d} {t.entry_time:%H:%M} {t.direction:>4} {t.brain_score:>6.1f} {t.confidence:>6.1f} "
                         f"{t.tech_score:>5.1f} {t.ml_score:>5.1f} {t.momentum_score:>5.1f} {t.profit_pips:>+8.1f} {t.close_reason:>6}")

    _forced_section("NEW trades generated by threshold (Run B BUY>=60)", forced_b60)
    _forced_section("NEW trades generated by threshold (Run C BUY>=58)", forced_b58)

    added_b, removed_b = _added_removed(trades_a, trades_b60)
    added_c, removed_c = _added_removed(trades_a, trades_b58)

    lines.append(f"\n{THIN}")
    lines.append("  📈 ADDED / REMOVED vs BASELINE")
    lines.append(THIN)
    lines.append(f"  Run B added: {len(added_b)} | removed: {len(removed_b)}")
    lines.append(f"  Run C added: {len(added_c)} | removed: {len(removed_c)}")

    if removed_b or removed_c:
        lines.append(f"\n  REMOVED trade details (baseline trades missing in variant):")
        lines.append(f"  {'Run':>4} {'Date':>10} {'Time':>5} {'Dir':>4} {'Scenario':>20} {'Pips':>8} {'USD':>9}")
        for run_lbl, rem in [("B", removed_b), ("C", removed_c)]:
            for t in sorted(rem, key=lambda x: x.entry_time):
                lines.append(
                    f"  {run_lbl:>4} {t.entry_time:%Y-%m-%d} {t.entry_time:%H:%M} {t.direction:>4} "
                    f"{t.scenario[:20]:>20} {t.profit_pips:>+8.1f} {t.profit_usd:>+9.2f}"
                )

    lines.append(f"\n{THIN}")
    lines.append("  📅 FEB 20 VALIDATION (07:00–16:00 UTC)")
    lines.append(THIN)

    first60 = _first_time_over(60.0, feb_b60)
    first58 = _first_time_over(58.0, feb_b58)
    lines.append(f"  First conflict candle with score>=60 on Feb 20 (Run B): {first60 or 'NONE'}")
    lines.append(f"  First conflict candle with score>=58 on Feb 20 (Run C): {first58 or 'NONE'}")

    def _print_feb(label, log):
        lines.append(f"\n  {label}:")
        if not log:
            lines.append("  (no data)")
            return
        lines.append(f"  {'Time':>5} {'Decision':>8} {'Score':>6} {'Conf':>6} {'Scenario':>22} {'Forced':>6} {'Tech':>5} {'ML':>5} {'Mom':>5}")
        for d in log:
            lines.append(f"  {d['time']:>5} {d['decision']:>8} {d['score']:>6.1f} {d['confidence']:>6.1f} {d['scenario'][:22]:>22} "
                         f"{('Y' if d.get('forced') else 'N'):>6} {d['tech']:>5.1f} {d['ml']:>5.1f} {d['momentum']:>5.1f}")

    _print_feb("Run A", feb_a)
    _print_feb("Run B (>=60)", feb_b60)
    _print_feb("Run C (>=58)", feb_b58)

    lines.append(f"\n{SEP}")
    lines.append("  VERDICT (vs baseline)")
    lines.append(SEP)

    def _verdict(sx, sy, label):
        pf_x = sx["pf"] if sx["pf"] != float("inf") else 9.99
        pf_y = sy["pf"] if sy["pf"] != float("inf") else 9.99
        pf_d = pf_y - pf_x
        wr_d = sy["wr"] - sx["wr"]
        pnl_d = sy["pnl_usd"] - sx["pnl_usd"]
        if pf_d >= 0.10 and wr_d >= -2.0:
            v = "ADOPT"
        elif pf_d <= -0.05 or wr_d <= -5.0:
            v = "ABANDON"
        else:
            v = "MONITOR"
        lines.append(f"  {label}: PF Δ={pf_d:+.2f}  WR Δ={wr_d:+.1f}%  P&L Δ=${pnl_d:+.2f}  →  {v}")

    _verdict(sa, sb60, "Run B (>=60)")
    _verdict(sa, sb58, "Run C (>=58)")

    lines.append(f"\n{'=' * 70}")
    return "\n".join(lines)


def _save_forced_csv(trades, path):
    if not trades:
        return
    rows = [
        {
            "entry_time": t.entry_time,
            "direction": t.direction,
            "brain_score": round(float(t.brain_score), 2),
            "confidence": round(float(t.confidence), 2),
            "tech_score": round(float(t.tech_score), 2),
            "ml_score": round(float(t.ml_score), 2),
            "momentum_score": round(float(t.momentum_score), 2),
            "profit_pips": round(float(t.profit_pips), 1),
            "profit_usd": round(float(t.profit_usd), 2),
            "close_reason": t.close_reason,
            "scenario": t.scenario,
        }
        for t in sorted(trades, key=lambda x: x.entry_time)
    ]
    pd.DataFrame(rows).to_csv(path, index=False)


def main():
    print("=" * 60)
    print("  BACKTEST STEP 2: conflict threshold (60 vs 58)")
    print("=" * 60)

    if not connect():
        return

    try:
        import scripts.run_backtest as rbt
        rbt.BT_START = BT_START
        rbt.BT_END = BT_END

        data = collect_all_data()
        for key in ["h1", "h4", "m5"]:
            if data[key].empty:
                print(f"❌ Missing {key} data. Aborting.")
                return

        bt_predictor = BacktestMLPredictor()
        if not bt_predictor.load_model():
            print("❌ Failed to load ML models")
            return

        feb_a: list = []
        feb_b60: list = []
        feb_b58: list = []

        _unpatch_to_baseline()
        print("\n" + "─" * 60)
        print("  Run A: Baseline (no ml_vs_tech_conflito)")
        print("─" * 60)
        trades_a, _, _ = _run_loop(data, bt_predictor, "Run A (Baseline)", feb20_log=feb_a)

        _restore_production()

        orig_60 = _patch_threshold_60()
        try:
            print("\n" + "─" * 60)
            print("  Run B: Conflict + BUY>=60")
            print("─" * 60)
            trades_b60, det_b60, forced_b60 = _run_loop(
                data,
                bt_predictor,
                "Run B (BUY>=60)",
                feb20_log=feb_b60,
                conflict_buy_threshold=60.0,
            )
        finally:
            _unpatch_threshold(orig_60)

        print("\n" + "─" * 60)
        print("  Run C: Conflict + BUY>=58 (production)")
        print("─" * 60)
        trades_b58, det_b58, forced_b58 = _run_loop(
            data,
            bt_predictor,
            "Run C (BUY>=58)",
            feb20_log=feb_b58,
            conflict_buy_threshold=58.0,
        )

        report = generate_report(
            trades_a,
            trades_b60,
            trades_b58,
            feb_a,
            feb_b60,
            feb_b58,
            det_b60,
            det_b58,
            forced_b60,
            forced_b58,
        )
        print(report)

        ts = datetime.now().strftime("%Y%m%d_%H%M")
        report_path = os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_threshold_{ts}.txt")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n📄 Report saved: {report_path}")

        _save_forced_csv(
            forced_b60,
            os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_threshold_{ts}_b60_forced.csv"),
        )
        _save_forced_csv(
            forced_b58,
            os.path.join(ROOT_DIR, "data", f"backtest_ml_conflict_threshold_{ts}_b58_forced.csv"),
        )

    finally:
        mt5.shutdown()
        print("\n✅ MT5 disconnected. Done.")


if __name__ == "__main__":
    main()
