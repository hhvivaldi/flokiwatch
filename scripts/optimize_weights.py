"""
WEIGHT OPTIMIZER — Central Brain Pillar Weights
=================================================
Cache-and-replay approach:
  1. Run ONE full backtest to cache all pillar inputs per H1 candle
  2. For each weight combo, replay ONLY the brain scoring + decision step (~5s per combo)
  3. Reuse the same trade outcomes (SL/TP/trailing) from the cached run

This turns a 5-min-per-combo full backtest into a 5-sec-per-combo replay.

Usage:
    python scripts/optimize_weights.py                    # Full run (5pp grid)
    python scripts/optimize_weights.py --step 10          # Coarse 10pp grid only
    python scripts/optimize_weights.py --models-dir models_v3_backup
"""

import os
import sys
import json
import time
import itertools
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from copy import deepcopy

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import MetaTrader5 as mt5
import config
from central_brain import (
    analyze_with_brain, is_actionable_signal, get_trade_direction,
    set_base_weights, reset_base_weights,
)

# Import backtest engine functions (in-process)
from run_backtest import (
    connect, collect_all_data, run_backtest, SimTrade,
    BacktestMLPredictor, simulate_trade,
    compute_h4_features, compute_m5_features, compute_m5_status,
    compute_volatility_status, compute_m5_reversal,
    BT_START as _BT_START, BT_END as _BT_END,
    NEUTRAL_NEWS, NEUTRAL_CALENDAR, H1_WARMUP, PIP_SIZE, PIP_VALUE_001,
)
from run_backtest import _make_news_dict
from technical_analyzer import calculate_indicators, analyze_technical_detailed, get_atr_value
from momentum_detector import analyze_momentum
from risk_manager import calculate_sl_tp
import run_backtest as bt_module


# ============================================================================
# CONFIG
# ============================================================================

# Walk-forward periods
TRAIN_START = datetime(2024, 8, 18)
TRAIN_END = datetime(2025, 8, 18)
TEST_START = datetime(2025, 8, 18)
TEST_END = datetime(2026, 2, 16, 12, 0)

# Grid ranges per pillar (percentage points)
PILLAR_RANGES = {
    "technical":  (15, 45),
    "ml":         (10, 40),
    "momentum":   ( 5, 30),
    "news":       ( 5, 35),
    "calendar":   ( 5, 25),
}

# 3-pillar ranges
PILLAR_3P_RANGES = {
    "technical":  (20, 60),
    "ml":         (10, 50),
    "momentum":   (10, 50),
}

# Acceptance criteria
MIN_PF_IMPROVEMENT = 0.10
MIN_PF_ABSOLUTE = 1.5
MIN_WR = 65.0

# Current weights (baseline)
CURRENT_WEIGHTS = {
    "technical": 0.30,
    "ml": 0.25,
    "momentum": 0.15,
    "news": 0.20,
    "calendar": 0.10,
}

RESULTS_FILE = os.path.join(ROOT_DIR, "data", "weight_optimization_results.json")


# ============================================================================
# GRID GENERATION
# ============================================================================

def generate_combos(step: int, ranges: Dict = None, n_pillars: int = 5) -> List[Dict[str, float]]:
    """Generate all valid weight combinations that sum to 100%."""
    if ranges is None:
        ranges = PILLAR_RANGES

    if n_pillars == 5:
        pillars = ["technical", "ml", "momentum", "news", "calendar"]
    else:
        pillars = ["technical", "ml", "momentum"]

    pillar_ranges = [
        range(ranges[p][0], ranges[p][1] + 1, step)
        for p in pillars
    ]

    combos = []
    for vals in itertools.product(*pillar_ranges):
        if sum(vals) == 100:
            combo = {pillars[i]: vals[i] / 100.0 for i in range(len(pillars))}
            if n_pillars == 3:
                combo["news"] = 0.0
                combo["calendar"] = 0.0
            combos.append(combo)

    return combos


# ============================================================================
# CACHED CANDLE DATA
# ============================================================================

@dataclass
class CachedCandle:
    """All weight-independent data for one H1 candle, pre-computed once."""
    idx: int
    h1_time: datetime
    current_price: float
    tech_data: Dict
    ml_data: Dict
    momentum_data: Dict
    m5_status: Dict
    vol_status: Dict
    atr: float
    # M5 reversal data (per direction)
    m5_rev_buy: Dict
    m5_rev_sell: Dict
    # Market hours filter
    blocked_by_hours: bool


def build_cache(data: Dict, predictor, start: datetime, end: datetime) -> List[CachedCandle]:
    """
    Run through all H1 candles once, computing all pillar inputs.
    Returns a list of CachedCandle objects for the requested period.
    """
    df_h1 = data['h1'].copy()
    df_h4 = data['h4'].copy()
    df_m5 = data['m5'].copy()

    bt_mask = (df_h1['datetime'] >= start) & (df_h1['datetime'] <= end)
    bt_indices = df_h1[bt_mask].index.tolist()

    cache = []
    total = len(bt_indices)

    for count, idx in enumerate(bt_indices):
        if idx < H1_WARMUP:
            continue

        h1_candle = df_h1.iloc[idx]
        h1_time = h1_candle['datetime']
        if hasattr(h1_time, 'to_pydatetime'):
            h1_time = h1_time.to_pydatetime()

        current_price = float(h1_candle['close'])

        if count % 200 == 0:
            pct = count / total * 100
            print(f"    Caching: {pct:.0f}% — {h1_time.strftime('%Y-%m-%d %H:%M')}")

        # Build H1 slice with indicators
        h1_slice = df_h1.iloc[:idx + 1].copy()
        h1_slice = calculate_indicators(h1_slice)
        if len(h1_slice) < 50:
            continue

        # PILLAR 1: Technical
        tech_data = analyze_technical_detailed(h1_slice)

        # PILLAR 2: ML
        h4_feats = compute_h4_features(df_h4, h1_time)
        m5_feats = compute_m5_features(df_m5, h1_time)
        predictor.set_h4_features(h4_feats)
        predictor.set_m5_features(m5_feats)

        try:
            ml_result = predictor.predict(h1_slice, NEUTRAL_NEWS)
            ml_data = {
                "score": float(ml_result['score']),
                "score_h1": float(ml_result.get('score_h1', ml_result['score'])),
                "score_h4": float(ml_result.get('score_h4', ml_result['score'])),
                "prediction": "bullish" if ml_result['direction'] == 'BUY' else ("bearish" if ml_result['direction'] == 'SELL' else "neutral"),
                "probability": float(ml_result.get('raw_proba', ml_result['probability'])),
                "max_confidence": float(ml_result.get('max_confidence', 0.5)),
                "pattern": "undefined",
                "similar_patterns_count": None,
                "historical_success_rate": None,
                "error": ml_result.get('error'),
            }
            if ml_data['max_confidence'] > 0.65:
                price_above_ema9 = current_price > float(h1_slice['ema_9'].iloc[-1])
                if ml_data['prediction'] == "bullish":
                    ml_data['pattern'] = "continuation" if price_above_ema9 else "reversal"
                elif ml_data['prediction'] == "bearish":
                    ml_data['pattern'] = "continuation" if not price_above_ema9 else "reversal"
            elif ml_data['max_confidence'] > 0.60:
                ml_data['pattern'] = "breakout"
        except Exception:
            ml_data = {"score": 50.0, "prediction": "neutral", "probability": 0.5,
                       "max_confidence": 0.5, "pattern": "undefined",
                       "similar_patterns_count": None, "historical_success_rate": None,
                       "error": "exception"}

        # PILLAR 3: Momentum
        momentum_data = analyze_momentum(h1_slice)

        # M5 status + volatility
        m5_status = compute_m5_status(df_m5, h1_time)
        vol_status = compute_volatility_status(df_m5, h1_time)

        # ATR
        atr = get_atr_value(h1_slice)

        # M5 reversal (pre-compute for both directions)
        m5_rev_buy = compute_m5_reversal(df_m5, h1_time, "BUY")
        m5_rev_sell = compute_m5_reversal(df_m5, h1_time, "SELL")

        # Market hours filter
        h1_hour = h1_time.hour
        close_hour = config.MARKET_DAILY_CLOSE_HOUR
        open_hour = config.MARKET_DAILY_OPEN_HOUR
        close_buffer = config.MARKET_CLOSE_BUFFER_MINUTES
        open_buffer = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
        minutes_to_close = (close_hour * 60) - (h1_hour * 60)
        minutes_after_open = (h1_hour * 60) - (open_hour * 60)
        blocked_by_hours = (0 <= minutes_to_close <= close_buffer) or (0 <= minutes_after_open < open_buffer)

        cache.append(CachedCandle(
            idx=idx, h1_time=h1_time, current_price=current_price,
            tech_data=tech_data, ml_data=ml_data, momentum_data=momentum_data,
            m5_status=m5_status, vol_status=vol_status, atr=atr,
            m5_rev_buy=m5_rev_buy, m5_rev_sell=m5_rev_sell,
            blocked_by_hours=blocked_by_hours,
        ))

    return cache


# ============================================================================
# REPLAY: Re-score cached candles with different weights
# ============================================================================

def replay_with_weights(weights: Dict[str, float], cache: List[CachedCandle],
                        df_m5: pd.DataFrame) -> List[SimTrade]:
    """
    Replay brain scoring + decision + trade simulation using cached pillar data.
    Only the brain scoring step uses the new weights — everything else is cached.
    """
    set_base_weights(weights)
    try:
        trades = []
        open_trades = []
        ticket_counter = 2000000

        last_trade_time = {'BUY': None, 'SELL': None}
        last_close_type = {'BUY': None, 'SELL': None}

        for cc in cache:
            # Close tracking
            still_open = []
            for t in open_trades:
                if t.close_time and t.close_time <= cc.h1_time:
                    if t.profit_pips >= 0:
                        last_close_type[t.direction] = "tp" if "tp" in t.close_reason else "trailing"
                    else:
                        last_close_type[t.direction] = "sl"
                    last_trade_time[t.direction] = t.close_time
                else:
                    still_open.append(t)
            open_trades = still_open

            # Brain analysis (uses current BASE_WEIGHTS via set_base_weights)
            brain_result = analyze_with_brain(
                cc.tech_data, cc.ml_data, cc.momentum_data, NEUTRAL_NEWS,
                cc.current_price,
                calendar_data=NEUTRAL_CALENDAR,
                volatility_status=cc.vol_status,
                m5_data=cc.m5_status,
                sr_data=None,
            )

            decision = brain_result.decision
            confidence = brain_result.confidence
            final_score = brain_result.final_score

            if not is_actionable_signal(decision):
                continue
            if confidence < config.BRAIN_MIN_CONFIDENCE:
                continue

            direction = get_trade_direction(decision)
            if direction is None:
                continue

            # Market hours filter
            if cc.blocked_by_hours:
                continue

            # M5 reversal
            m5_rev = cc.m5_rev_buy if direction == "BUY" else cc.m5_rev_sell
            if m5_rev['reversal_detected']:
                if m5_rev['reversal_strength'] == "strong":
                    continue
                elif m5_rev['reversal_strength'] == "moderate":
                    confidence -= config.M5_REVERSAL_CONFIDENCE_PENALTY
                    if confidence < config.BRAIN_MIN_CONFIDENCE:
                        continue

            # Overtrading cooldown
            lt = last_trade_time.get(direction)
            if lt is not None:
                ct = last_close_type.get(direction)
                if ct == "trailing":
                    min_min = config.MIN_MINUTES_AFTER_TRAILING
                elif ct == "sl":
                    min_min = config.MIN_MINUTES_AFTER_SL
                else:
                    min_min = config.MIN_MINUTES_BETWEEN_TRADES
                elapsed = (cc.h1_time - lt).total_seconds() / 60
                if elapsed < min_min:
                    continue

            # Smart pyramid
            same_dir_open = [t for t in open_trades if t.direction == direction]
            is_pyramid_attempt = len(same_dir_open) > 0

            if is_pyramid_attempt:
                blocked = False
                for t in same_dir_open:
                    if t.direction == "BUY":
                        profit_pct = ((cc.current_price - t.entry_price) / t.entry_price) * 100
                    else:
                        profit_pct = ((t.entry_price - cc.current_price) / t.entry_price) * 100
                    if profit_pct < config.PYRAMID_MIN_PROFIT_PERCENT:
                        blocked = True
                        break
                if blocked:
                    continue

            # Max positions
            if len(open_trades) >= config.MAX_POSITIONS:
                continue

            # Open trade
            levels = calculate_sl_tp(cc.current_price, direction, cc.atr)

            ticket_counter += 1
            trade = SimTrade(
                ticket=ticket_counter,
                direction=direction,
                entry_price=cc.current_price,
                entry_time=cc.h1_time,
                sl=levels.stop_loss,
                tp=levels.take_profit_1,
                atr=cc.atr,
                brain_score=final_score,
                confidence=confidence,
                scenario=brain_result.scenario,
                scenario_desc=brain_result.scenario_description,
                explanation_snippet="",
                is_pyramid=is_pyramid_attempt,
                tech_score=cc.tech_data.get('score', 50),
                ml_score=cc.ml_data.get('score', 50),
                momentum_score=cc.momentum_data.get('score', 50),
            )

            trade = simulate_trade(trade, df_m5)
            trades.append(trade)
            open_trades.append(trade)
            last_trade_time[direction] = cc.h1_time

        return trades
    finally:
        reset_base_weights()


# ============================================================================
# STATS HELPERS
# ============================================================================

def calc_stats(trades: List[SimTrade]) -> Dict:
    """Calculate PF, WR, P&L, max DD from a list of SimTrade objects."""
    if not trades:
        return {'pf': 0, 'wr': 0, 'trades': 0, 'pnl': 0, 'max_dd': 0, 'pips': 0}

    wins = [t for t in trades if t.profit_usd > 0]
    losses = [t for t in trades if t.profit_usd <= 0]

    gross_win = sum(t.profit_usd for t in wins)
    gross_loss = abs(sum(t.profit_usd for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float('inf')
    if pf == float('inf'):
        pf = 99.0
    wr = len(wins) / len(trades) * 100

    running, peak, max_dd = 0, 0, 0
    for t in trades:
        running += t.profit_usd
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)

    return {
        'pf': round(pf, 3),
        'wr': round(wr, 1),
        'trades': len(trades),
        'pnl': round(sum(t.profit_usd for t in trades), 2),
        'pips': round(sum(t.profit_pips for t in trades), 1),
        'max_dd': round(max_dd, 2),
    }


def trade_keys(trades: List[SimTrade]) -> set:
    return {(str(t.entry_time), t.direction) for t in trades}


# ============================================================================
# MAIN OPTIMIZATION
# ============================================================================

def run_optimization(step: int = 5, models_dir: str = None):
    """
    Main optimization loop using cache-and-replay.

    1. Connect MT5, load data
    2. Cache all pillar inputs for TRAIN and TEST periods (~10 min total)
    3. Replay brain scoring for each weight combo (~5s per combo)
    4. 3-pillar comparison
    5. Report with trade diff analysis
    """
    print("\n" + "=" * 70)
    print("  WEIGHT OPTIMIZER — Central Brain Pillars")
    print("  Cache-and-replay (pillar data computed once, brain replayed per combo)")
    print("=" * 70)

    # ── Connect MT5 and load data ──
    if not connect():
        return

    bt_module.BT_START = TRAIN_START
    bt_module.BT_END = TEST_END

    print(f"\n  Period: {TRAIN_START.date()} → {TEST_END.date()}")
    print(f"  Train:  {TRAIN_START.date()} → {TRAIN_END.date()}")
    print(f"  Test:   {TEST_START.date()} → {TEST_END.date()}")

    data = collect_all_data()
    for key in ['h1', 'h4', 'm5']:
        if data[key].empty:
            print(f"  ❌ Missing {key} data. Aborting.")
            return

    print(f"\n  Data loaded: H1={len(data['h1'])}, H4={len(data['h4'])}, M5={len(data['m5'])}")

    # ── Pre-load ML predictor ──
    predictor = BacktestMLPredictor(models_dir=models_dir)
    if not predictor.load_model():
        print("  ❌ Failed to load ML models. Aborting.")
        return
    print(f"  ML models loaded")

    # ── Build cache for TRAIN and TEST periods ──
    print(f"\n  Caching TRAIN period pillar data...")
    t0 = time.time()
    cache_train = build_cache(data, predictor, TRAIN_START, TRAIN_END)
    print(f"    → {len(cache_train)} candles cached in {time.time()-t0:.0f}s")

    print(f"  Caching TEST period pillar data...")
    t1 = time.time()
    cache_test = build_cache(data, predictor, TEST_START, TEST_END)
    print(f"    → {len(cache_test)} candles cached in {time.time()-t1:.0f}s")

    df_m5 = data['m5'].copy()

    # ── Baseline ──
    print(f"\n{'─' * 70}")
    print(f"  BASELINE: Tech=30 ML=25 Mom=15 News=20 Cal=10")
    print(f"{'─' * 70}")

    bl_train_trades = replay_with_weights(CURRENT_WEIGHTS, cache_train, df_m5)
    bl_test_trades = replay_with_weights(CURRENT_WEIGHTS, cache_test, df_m5)
    bl_train = calc_stats(bl_train_trades)
    bl_test = calc_stats(bl_test_trades)
    bl_train_keys = trade_keys(bl_train_trades)
    bl_test_keys = trade_keys(bl_test_trades)

    print(f"  Train: {bl_train['trades']} trades, PF={bl_train['pf']:.2f}, WR={bl_train['wr']:.1f}%, P&L=${bl_train['pnl']:+.2f}")
    print(f"  Test:  {bl_test['trades']} trades, PF={bl_test['pf']:.2f}, WR={bl_test['wr']:.1f}%, P&L=${bl_test['pnl']:+.2f}")

    # ── Grid search ──
    combos_5p = generate_combos(step=step)
    combos_5p = [c for c in combos_5p if c != CURRENT_WEIGHTS]

    print(f"\n{'═' * 70}")
    print(f"  GRID SEARCH ({step}pp steps) — {len(combos_5p)} 5-pillar combos")
    print(f"{'═' * 70}")

    results_5p = []
    t2 = time.time()

    for i, combo in enumerate(combos_5p):
        w_str = "/".join(f"{v*100:.0f}" for v in combo.values())
        elapsed = time.time() - t2
        eta = (elapsed / max(i, 1)) * (len(combos_5p) - i) if i > 0 else 0

        train_trades = replay_with_weights(combo, cache_train, df_m5)
        test_trades = replay_with_weights(combo, cache_test, df_m5)

        train_s = calc_stats(train_trades)
        test_s = calc_stats(test_trades)

        tk_train = trade_keys(train_trades)
        tk_test = trade_keys(test_trades)
        diff = {
            'train_added': len(tk_train - bl_train_keys),
            'train_removed': len(bl_train_keys - tk_train),
            'test_added': len(tk_test - bl_test_keys),
            'test_removed': len(bl_test_keys - tk_test),
        }
        diff['total_changed'] = sum(diff.values())

        results_5p.append({
            'weights': combo,
            'train': train_s,
            'test': test_s,
            'trade_diff': diff,
            'is_3pillar': False,
        })

        if (i + 1) % 10 == 0 or i == 0:
            print(f"  [{i+1}/{len(combos_5p)}] {w_str}"
                  f" → Trn PF={train_s['pf']:.2f} WR={train_s['wr']:.0f}% N={train_s['trades']}"
                  f" | Tst PF={test_s['pf']:.2f} WR={test_s['wr']:.0f}% N={test_s['trades']}"
                  f" | ETA {eta/60:.1f}m")

    results_5p.sort(key=lambda r: r['test']['pf'], reverse=True)
    elapsed_grid = time.time() - t2
    print(f"\n  Grid complete: {len(results_5p)} combos in {elapsed_grid/60:.1f} min")

    # ── 3-pillar comparison ──
    combos_3p = generate_combos(step=max(step, 10), ranges=PILLAR_3P_RANGES, n_pillars=3)
    print(f"\n{'═' * 70}")
    print(f"  3-PILLAR COMPARISON (informational) — {len(combos_3p)} combos")
    print(f"{'═' * 70}")

    results_3p = []
    for i, combo in enumerate(combos_3p):
        train_trades = replay_with_weights(combo, cache_train, df_m5)
        test_trades = replay_with_weights(combo, cache_test, df_m5)

        train_s = calc_stats(train_trades)
        test_s = calc_stats(test_trades)

        tk_train = trade_keys(train_trades)
        tk_test = trade_keys(test_trades)
        diff = {
            'train_added': len(tk_train - bl_train_keys),
            'train_removed': len(bl_train_keys - tk_train),
            'test_added': len(tk_test - bl_test_keys),
            'test_removed': len(bl_test_keys - tk_test),
        }
        diff['total_changed'] = sum(diff.values())

        results_3p.append({
            'weights': combo,
            'train': train_s,
            'test': test_s,
            'trade_diff': diff,
            'is_3pillar': True,
        })

        w_str = f"T={combo['technical']*100:.0f}/ML={combo['ml']*100:.0f}/M={combo['momentum']*100:.0f}"
        print(f"  [{i+1}/{len(combos_3p)}] {w_str}"
              f" → Trn PF={train_s['pf']:.2f} N={train_s['trades']}"
              f" | Tst PF={test_s['pf']:.2f} N={test_s['trades']}")

    results_3p.sort(key=lambda r: r['test']['pf'], reverse=True)

    # ── REPORT ──
    generate_report(results_5p, results_3p, bl_train, bl_test)

    # ── Save ──
    save_data = {
        'timestamp': datetime.now().isoformat(),
        'baseline': {
            'weights': CURRENT_WEIGHTS,
            'train': bl_train,
            'test': bl_test,
        },
        'results_5pillar': results_5p[:30],
        'results_3pillar': results_3p[:10],
        'acceptance_criteria': {
            'min_pf_improvement': MIN_PF_IMPROVEMENT,
            'min_pf_absolute': MIN_PF_ABSOLUTE,
            'min_wr': MIN_WR,
        },
        'total_combos_evaluated': len(results_5p) + len(results_3p) + 1,
        'grid_step': step,
    }
    with open(RESULTS_FILE, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\n  📄 Results saved: {RESULTS_FILE}")

    total_time = time.time() - t0
    print(f"  Total time: {total_time/60:.1f} min")

    mt5.shutdown()


# ============================================================================
# REPORT
# ============================================================================

def generate_report(results_5p: List[Dict], results_3p: List[Dict],
                    bl_train: Dict, bl_test: Dict):
    """Print the final optimization report."""
    print("\n" + "=" * 70)
    print("  WEIGHT OPTIMIZATION RESULTS")
    print("=" * 70)

    # ── Top 5-pillar combos ──
    print(f"\n{'─' * 95}")
    print(f"  TOP 5-PILLAR COMBOS (ranked by TEST PF)")
    print(f"{'─' * 95}")
    print(f"  {'#':>3} {'Tech':>5} {'ML':>5} {'Mom':>5} {'News':>5} {'Cal':>5} │ "
          f"{'TrnPF':>6} {'TrnWR':>6} {'TrnN':>5} {'Trn$':>8} │ "
          f"{'TstPF':>6} {'TstWR':>6} {'TstN':>5} {'Tst$':>8} │ "
          f"{'Chg':>4} {'Pass':>4}")

    shown = min(20, len(results_5p))
    for i, r in enumerate(results_5p[:shown]):
        w = r['weights']
        chg = r['trade_diff']['total_changed']
        passes = (
            r['train']['pf'] >= MIN_PF_ABSOLUTE and
            r['test']['pf'] >= MIN_PF_ABSOLUTE and
            r['train']['pf'] >= bl_train['pf'] + MIN_PF_IMPROVEMENT and
            r['test']['pf'] >= bl_test['pf'] + MIN_PF_IMPROVEMENT and
            r['train']['wr'] >= MIN_WR and
            r['test']['wr'] >= MIN_WR
        )
        ps = "✅" if passes else "❌"

        print(f"  {i+1:>3} {w['technical']*100:>5.0f} {w['ml']*100:>5.0f} {w['momentum']*100:>5.0f} "
              f"{w['news']*100:>5.0f} {w['calendar']*100:>5.0f} │ "
              f"{r['train']['pf']:>6.2f} {r['train']['wr']:>5.1f}% {r['train']['trades']:>5} "
              f"${r['train']['pnl']:>+7.0f} │ "
              f"{r['test']['pf']:>6.2f} {r['test']['wr']:>5.1f}% {r['test']['trades']:>5} "
              f"${r['test']['pnl']:>+7.0f} │ "
              f"{chg:>4} {ps:>4}")

    print(f"\n  BASE {'30':>5} {'25':>5} {'15':>5} {'20':>5} {'10':>5} │ "
          f"{bl_train['pf']:>6.2f} {bl_train['wr']:>5.1f}% {bl_train['trades']:>5} "
          f"${bl_train['pnl']:>+7.0f} │ "
          f"{bl_test['pf']:>6.2f} {bl_test['wr']:>5.1f}% {bl_test['trades']:>5} "
          f"${bl_test['pnl']:>+7.0f} │ "
          f"{'0':>4} {'REF':>4}")

    # ── Trade diff detail for top 5 ──
    print(f"\n{'─' * 70}")
    print(f"  TRADE SELECTION CHANGES (top 5 vs baseline)")
    print(f"{'─' * 70}")

    for i, r in enumerate(results_5p[:5]):
        w = r['weights']
        td = r['trade_diff']
        w_str = "/".join(f"{v*100:.0f}" for v in [w['technical'], w['ml'], w['momentum'], w['news'], w['calendar']])
        print(f"\n  #{i+1} ({w_str}):")
        print(f"    Train: +{td['train_added']} added, -{td['train_removed']} removed")
        print(f"    Test:  +{td['test_added']} added, -{td['test_removed']} removed")
        print(f"    Total changed: {td['total_changed']}")

    # ── 3-pillar results ──
    if results_3p:
        print(f"\n{'─' * 70}")
        print(f"  3-PILLAR RESULTS (informational — NOT for live deployment)")
        print(f"{'─' * 70}")
        print(f"  {'#':>3} {'Tech':>5} {'ML':>5} {'Mom':>5} │ "
              f"{'TrnPF':>6} {'TrnWR':>6} {'TrnN':>5} │ "
              f"{'TstPF':>6} {'TstWR':>6} {'TstN':>5} │ {'Chg':>4}")

        for i, r in enumerate(results_3p[:10]):
            w = r['weights']
            chg = r['trade_diff']['total_changed']
            print(f"  {i+1:>3} {w['technical']*100:>5.0f} {w['ml']*100:>5.0f} {w['momentum']*100:>5.0f} │ "
                  f"{r['train']['pf']:>6.2f} {r['train']['wr']:>5.1f}% {r['train']['trades']:>5} │ "
                  f"{r['test']['pf']:>6.2f} {r['test']['wr']:>5.1f}% {r['test']['trades']:>5} │ {chg:>4}")

        print(f"\n  BASE (5P) {'30':>3} {'25':>5} {'15':>5} │ "
              f"{bl_train['pf']:>6.2f} {bl_train['wr']:>5.1f}% {bl_train['trades']:>5} │ "
              f"{bl_test['pf']:>6.2f} {bl_test['wr']:>5.1f}% {bl_test['trades']:>5}")

    # ── Dilution analysis ──
    if results_3p:
        best_3p = results_3p[0]
        print(f"\n{'─' * 70}")
        print(f"  DILUTION ANALYSIS (neutral News/Calendar impact)")
        print(f"{'─' * 70}")
        print(f"  Best 3-pillar PF (test): {best_3p['test']['pf']:.2f}")
        print(f"  Baseline 5-pillar PF (test): {bl_test['pf']:.2f}")
        delta = best_3p['test']['pf'] - bl_test['pf']
        if delta > 0.1:
            print(f"  ⚠️ Removing neutral pillars improves PF by {delta:+.2f}")
            print(f"     → Neutral News/Calendar dilution costs ~{delta:.2f} PF in backtest")
            print(f"     → In live trading with real data, this cost may be offset by their signal value")
        elif delta < -0.1:
            print(f"  ✅ 3-pillar is WORSE by {delta:+.2f} — neutral pillars provide useful regularization")
        else:
            print(f"  ➖ Minimal difference ({delta:+.2f}) — neutral pillars have negligible impact")

    # ── Recommendation ──
    print(f"\n{'─' * 70}")
    print(f"  RECOMMENDATION")
    print(f"{'─' * 70}")

    passing = [r for r in results_5p if (
        r['train']['pf'] >= MIN_PF_ABSOLUTE and
        r['test']['pf'] >= MIN_PF_ABSOLUTE and
        r['train']['pf'] >= bl_train['pf'] + MIN_PF_IMPROVEMENT and
        r['test']['pf'] >= bl_test['pf'] + MIN_PF_IMPROVEMENT and
        r['train']['wr'] >= MIN_WR and
        r['test']['wr'] >= MIN_WR
    )]

    if passing:
        best = passing[0]
        w = best['weights']
        print(f"  ✅ {len(passing)} combo(s) pass all acceptance criteria.")
        print(f"  Best: Tech={w['technical']*100:.0f}% ML={w['ml']*100:.0f}% Mom={w['momentum']*100:.0f}% "
              f"News={w['news']*100:.0f}% Cal={w['calendar']*100:.0f}%")
        print(f"    Train: PF={best['train']['pf']:.2f} WR={best['train']['wr']:.1f}% ({best['train']['trades']} trades)")
        print(f"    Test:  PF={best['test']['pf']:.2f} WR={best['test']['wr']:.1f}% ({best['test']['trades']} trades)")
        print(f"    Trade selection changes: {best['trade_diff']['total_changed']}")
    else:
        print(f"  ❌ No combo passes all acceptance criteria.")
        print(f"     Criteria: PF improvement >{MIN_PF_IMPROVEMENT} on BOTH periods, PF >{MIN_PF_ABSOLUTE}, WR >{MIN_WR}%")
        if results_5p:
            best = results_5p[0]
            w = best['weights']
            print(f"  Closest: Tech={w['technical']*100:.0f}% ML={w['ml']*100:.0f}% Mom={w['momentum']*100:.0f}% "
                  f"News={w['news']*100:.0f}% Cal={w['calendar']*100:.0f}%")
            print(f"    Train: PF={best['train']['pf']:.2f} WR={best['train']['wr']:.1f}%")
            print(f"    Test:  PF={best['test']['pf']:.2f} WR={best['test']['wr']:.1f}%")
        print(f"\n  Current weights may already be near-optimal for this data.")


# ============================================================================
# MAIN
# ============================================================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Weight Optimizer — Central Brain Pillars")
    parser.add_argument('--step', type=int, default=5,
                        help='Grid step size in percentage points (default: 5)')
    parser.add_argument('--models-dir', type=str, default=None,
                        help='Override models directory')
    args = parser.parse_args()

    models_dir = None
    if args.models_dir:
        models_dir = os.path.join(ROOT_DIR, args.models_dir) if not os.path.isabs(args.models_dir) else args.models_dir

    combos = generate_combos(step=args.step)
    combos_3p = generate_combos(step=max(args.step, 10), ranges=PILLAR_3P_RANGES, n_pillars=3)
    print(f"  Grid ({args.step}pp): {len(combos)} 5-pillar + {len(combos_3p)} 3-pillar combos")
    print(f"  Current: Tech={CURRENT_WEIGHTS['technical']*100:.0f}% ML={CURRENT_WEIGHTS['ml']*100:.0f}% "
          f"Mom={CURRENT_WEIGHTS['momentum']*100:.0f}% News={CURRENT_WEIGHTS['news']*100:.0f}% "
          f"Cal={CURRENT_WEIGHTS['calendar']*100:.0f}%")

    run_optimization(step=args.step, models_dir=models_dir)


if __name__ == "__main__":
    main()
