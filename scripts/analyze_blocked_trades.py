"""
ANALYZE BLOCKED TRADES — MTF Trend + Volume Gate
=================================================
Identifies which trades were blocked by MTF/Volume Gate and whether they were winners or losers.

Usage:
    python scripts/analyze_blocked_trades.py
"""

import os
import sys
from datetime import datetime
from typing import Dict, List, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import config
from scripts.run_backtest import (
    connect, collect_all_data, run_backtest, BacktestMLPredictor,
    compute_mtf_trend, _check_mtf_trend_alignment
)
from central_brain import _check_mtf_trend_alignment


def analyze_blocked_trades():
    """Analyze which trades were blocked and their outcomes."""
    print("="*70)
    print("BLOCKED TRADES ANALYSIS — MTF Trend + Volume Gate")
    print("="*70)
    
    if not connect():
        print("❌ Failed to connect to MT5")
        return
    
    data = collect_all_data()
    
    bt_predictor = BacktestMLPredictor()
    if not bt_predictor.load_model():
        print("❌ Failed to load ML models")
        return
    
    # Run baseline (both OFF) to get all trades
    print("\n📊 Running baseline (both OFF)...")
    config.MTF_TREND_ENABLED = False
    config.VOLUME_GATE_ENABLED = False
    baseline_trades, _ = run_backtest(
        data, disable_visual=True, disable_pyramid=True,
        sr_enabled=True, sr_tp_adjust=False, quiet=True, bt_predictor=bt_predictor
    )
    
    # Run MTF only
    print("📊 Running MTF only...")
    config.MTF_TREND_ENABLED = True
    config.VOLUME_GATE_ENABLED = False
    mtf_trades, _ = run_backtest(
        data, disable_visual=True, disable_pyramid=True,
        sr_enabled=True, sr_tp_adjust=False, quiet=True, bt_predictor=bt_predictor
    )
    
    # Run Volume only
    print("📊 Running Volume only...")
    config.MTF_TREND_ENABLED = False
    config.VOLUME_GATE_ENABLED = True
    vol_trades, _ = run_backtest(
        data, disable_visual=True, disable_pyramid=True,
        sr_enabled=True, sr_tp_adjust=False, quiet=True, bt_predictor=bt_predictor
    )
    
    # Run Combined
    print("📊 Running Combined...")
    config.MTF_TREND_ENABLED = True
    config.VOLUME_GATE_ENABLED = True
    combined_trades, _ = run_backtest(
        data, disable_visual=True, disable_pyramid=True,
        sr_enabled=True, sr_tp_adjust=False, quiet=True, bt_predictor=bt_predictor
    )
    
    # Create lookup by entry time for baseline trades
    baseline_by_time = {t.entry_time: t for t in baseline_trades}
    mtf_by_time = {t.entry_time: t for t in mtf_trades}
    vol_by_time = {t.entry_time: t for t in vol_trades}
    combined_by_time = {t.entry_time: t for t in combined_trades}
    
    # Find trades blocked by MTF (in baseline but not in MTF-only)
    mtf_blocked = []
    for t in baseline_trades:
        if t.entry_time not in mtf_by_time:
            mtf_blocked.append(t)
    
    # Find trades blocked by Volume Gate (in baseline but not in Volume-only)
    vol_blocked = []
    for t in baseline_trades:
        if t.entry_time not in vol_by_time:
            vol_blocked.append(t)
    
    # Find trades blocked by Combined (in baseline but not in Combined)
    combined_blocked = []
    for t in baseline_trades:
        if t.entry_time not in combined_by_time:
            combined_blocked.append(t)
    
    # Analyze blocked trades
    def analyze_blocked(blocked_list: List, name: str):
        if not blocked_list:
            print(f"\n{name}: 0 trades blocked")
            return
        
        winners = [t for t in blocked_list if t.profit_pips > 0]
        losers = [t for t in blocked_list if t.profit_pips <= 0]
        
        total_pips_blocked = sum(t.profit_pips for t in blocked_list)
        winner_pips = sum(t.profit_pips for t in winners)
        loser_pips = sum(t.profit_pips for t in losers)
        
        print(f"\n{'='*70}")
        print(f"{name}")
        print(f"{'='*70}")
        print(f"Total blocked: {len(blocked_list)} trades")
        print(f"  Winners blocked: {len(winners)} ({len(winners)/len(blocked_list)*100:.1f}%)")
        print(f"  Losers blocked:  {len(losers)} ({len(losers)/len(blocked_list)*100:.1f}%)")
        print(f"\nP&L Impact:")
        print(f"  Winner pips blocked: {winner_pips:+.1f}")
        print(f"  Loser pips blocked:  {loser_pips:+.1f}")
        print(f"  Net pips blocked:    {total_pips_blocked:+.1f}")
        
        if total_pips_blocked > 0:
            print(f"\n⚠️ WARNING: Blocking MORE winners than losers! Net loss: {total_pips_blocked:.1f} pips")
        else:
            print(f"\n✅ GOOD: Blocking more losers than winners. Net saved: {abs(total_pips_blocked):.1f} pips")
        
        # Show details of blocked trades
        print(f"\nBlocked Trade Details:")
        print(f"{'Time':<20} {'Dir':<5} {'Pips':>10} {'Reason':>12} {'Scenario':<25}")
        print("-"*70)
        for t in sorted(blocked_list, key=lambda x: x.entry_time):
            outcome = "WIN" if t.profit_pips > 0 else "LOSS"
            print(f"{t.entry_time.strftime('%Y-%m-%d %H:%M'):<20} {t.direction:<5} {t.profit_pips:>+10.1f} {outcome:>12} {t.scenario[:25]:<25}")
    
    analyze_blocked(mtf_blocked, "MTF TREND BLOCKED TRADES")
    analyze_blocked(vol_blocked, "VOLUME GATE BLOCKED TRADES")
    analyze_blocked(combined_blocked, "COMBINED (MTF + VOLUME) BLOCKED TRADES")
    
    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"Baseline trades: {len(baseline_trades)}")
    print(f"MTF blocked: {len(mtf_blocked)} trades ({sum(t.profit_pips for t in mtf_blocked):+.1f} pips)")
    print(f"Volume blocked: {len(vol_blocked)} trades ({sum(t.profit_pips for t in vol_blocked):+.1f} pips)")
    print(f"Combined blocked: {len(combined_blocked)} trades ({sum(t.profit_pips for t in combined_blocked):+.1f} pips)")
    
    # Recommendation
    print("\n" + "="*70)
    print("RECOMMENDATION")
    print("="*70)
    
    mtf_net = sum(t.profit_pips for t in mtf_blocked)
    vol_net = sum(t.profit_pips for t in vol_blocked)
    combined_net = sum(t.profit_pips for t in combined_blocked)
    
    if mtf_net > 0:
        print(f"❌ MTF Trend: DISABLE — blocks {mtf_net:.1f} pips of profit")
    else:
        print(f"✅ MTF Trend: ENABLE — saves {abs(mtf_net):.1f} pips of losses")
    
    if vol_net > 0:
        print(f"❌ Volume Gate: DISABLE — blocks {vol_net:.1f} pips of profit")
    else:
        print(f"✅ Volume Gate: ENABLE — saves {abs(vol_net):.1f} pips of losses")


if __name__ == "__main__":
    analyze_blocked_trades()
