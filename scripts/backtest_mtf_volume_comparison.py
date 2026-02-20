"""
BACKTEST COMPARISON: MTF Trend + Volume Gate
=============================================
Runs 4 scenarios to compare the impact of MTF Trend Confirmation and Volume Gate:
1. Baseline: Current logic (MTF OFF, Volume Gate OFF)
2. MTF only: MTF ON, Volume Gate OFF
3. Volume only: MTF OFF, Volume Gate ON
4. Combined: MTF ON, Volume Gate ON

Usage:
    python scripts/backtest_mtf_volume_comparison.py
"""

import os
import sys
from datetime import datetime

# Add parent dir to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import config
from scripts.run_backtest import (
    connect, collect_all_data, run_backtest, BacktestMLPredictor
)


def run_scenario(name: str, data: dict, bt_predictor, mtf_enabled: bool, volume_gate_enabled: bool):
    """Run a single backtest scenario with specific settings."""
    print(f"\n{'='*60}")
    print(f"SCENARIO: {name}")
    print(f"  MTF_TREND_ENABLED = {mtf_enabled}")
    print(f"  VOLUME_GATE_ENABLED = {volume_gate_enabled}")
    print(f"{'='*60}")
    
    # Temporarily override config
    original_mtf = getattr(config, 'MTF_TREND_ENABLED', True)
    original_vol = getattr(config, 'VOLUME_GATE_ENABLED', True)
    
    config.MTF_TREND_ENABLED = mtf_enabled
    config.VOLUME_GATE_ENABLED = volume_gate_enabled
    
    try:
        trades, pyramid_stats = run_backtest(
            data,
            disable_visual=True,
            disable_pyramid=True,
            sr_enabled=True,
            sr_tp_adjust=False,
            quiet=True,
            bt_predictor=bt_predictor,
        )
        
        # Calculate metrics
        total_trades = len(trades)
        wins = [t for t in trades if t.profit_pips > 0]
        losses = [t for t in trades if t.profit_pips <= 0]
        win_rate = len(wins) / total_trades * 100 if total_trades > 0 else 0
        
        total_pips = sum(t.profit_pips for t in trades)
        total_usd = sum(t.profit_usd for t in trades)
        
        gross_profit = sum(t.profit_pips for t in wins)
        gross_loss = abs(sum(t.profit_pips for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        # Max drawdown (simple)
        cumulative = 0
        peak = 0
        max_dd = 0
        for t in trades:
            cumulative += t.profit_pips
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        
        return {
            'name': name,
            'mtf_enabled': mtf_enabled,
            'volume_gate_enabled': volume_gate_enabled,
            'total_trades': total_trades,
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': win_rate,
            'total_pips': total_pips,
            'total_usd': total_usd,
            'profit_factor': profit_factor,
            'max_drawdown_pips': max_dd,
            'trades': trades,
        }
        
    finally:
        # Restore original config
        config.MTF_TREND_ENABLED = original_mtf
        config.VOLUME_GATE_ENABLED = original_vol


def main():
    print("="*60)
    print("MTF TREND + VOLUME GATE BACKTEST COMPARISON")
    print("="*60)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Connect to MT5
    if not connect():
        print("❌ Failed to connect to MT5")
        return
    
    # Collect data (includes D1 for MTF)
    data = collect_all_data()
    
    if 'd1' not in data or len(data['d1']) == 0:
        print("⚠️ No D1 data available - MTF trend check will be skipped")
    
    # Load ML predictor once (reuse across scenarios)
    bt_predictor = BacktestMLPredictor()
    if not bt_predictor.load_model():
        print("❌ Failed to load ML models")
        return
    
    # Run 4 scenarios
    results = []
    
    # 1. Baseline (both OFF)
    results.append(run_scenario(
        "Baseline (MTF OFF, Volume OFF)",
        data, bt_predictor,
        mtf_enabled=False, volume_gate_enabled=False
    ))
    
    # 2. MTF only
    results.append(run_scenario(
        "MTF Only (MTF ON, Volume OFF)",
        data, bt_predictor,
        mtf_enabled=True, volume_gate_enabled=False
    ))
    
    # 3. Volume only
    results.append(run_scenario(
        "Volume Only (MTF OFF, Volume ON)",
        data, bt_predictor,
        mtf_enabled=False, volume_gate_enabled=True
    ))
    
    # 4. Combined (both ON)
    results.append(run_scenario(
        "Combined (MTF ON, Volume ON)",
        data, bt_predictor,
        mtf_enabled=True, volume_gate_enabled=True
    ))
    
    # Print comparison table
    print("\n" + "="*80)
    print("COMPARISON RESULTS")
    print("="*80)
    print(f"{'Scenario':<35} {'Trades':>7} {'Win%':>7} {'Pips':>10} {'PF':>7} {'MaxDD':>8}")
    print("-"*80)
    
    for r in results:
        pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "∞"
        print(f"{r['name']:<35} {r['total_trades']:>7} {r['win_rate']:>6.1f}% {r['total_pips']:>+10.1f} {pf_str:>7} {r['max_drawdown_pips']:>8.1f}")
    
    print("-"*80)
    
    # Calculate deltas vs baseline
    baseline = results[0]
    print("\nDELTA vs BASELINE:")
    print("-"*80)
    for r in results[1:]:
        trades_delta = r['total_trades'] - baseline['total_trades']
        pips_delta = r['total_pips'] - baseline['total_pips']
        wr_delta = r['win_rate'] - baseline['win_rate']
        dd_delta = r['max_drawdown_pips'] - baseline['max_drawdown_pips']
        
        print(f"{r['name']:<35} {trades_delta:>+7} {wr_delta:>+6.1f}% {pips_delta:>+10.1f} {'':>7} {dd_delta:>+8.1f}")
    
    # Save report
    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
    report_path = f"data/backtest_mtf_volume_{timestamp}.txt"
    
    with open(report_path, 'w') as f:
        f.write("MTF TREND + VOLUME GATE BACKTEST COMPARISON\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("="*80 + "\n\n")
        
        f.write(f"{'Scenario':<35} {'Trades':>7} {'Win%':>7} {'Pips':>10} {'PF':>7} {'MaxDD':>8}\n")
        f.write("-"*80 + "\n")
        
        for r in results:
            pf_str = f"{r['profit_factor']:.2f}" if r['profit_factor'] != float('inf') else "inf"
            f.write(f"{r['name']:<35} {r['total_trades']:>7} {r['win_rate']:>6.1f}% {r['total_pips']:>+10.1f} {pf_str:>7} {r['max_drawdown_pips']:>8.1f}\n")
        
        f.write("\n\nDELTA vs BASELINE:\n")
        f.write("-"*80 + "\n")
        for r in results[1:]:
            trades_delta = r['total_trades'] - baseline['total_trades']
            pips_delta = r['total_pips'] - baseline['total_pips']
            wr_delta = r['win_rate'] - baseline['win_rate']
            dd_delta = r['max_drawdown_pips'] - baseline['max_drawdown_pips']
            f.write(f"{r['name']:<35} {trades_delta:>+7} {wr_delta:>+6.1f}% {pips_delta:>+10.1f} {'':>7} {dd_delta:>+8.1f}\n")
        
        f.write("\n\nCONFIG SETTINGS:\n")
        f.write(f"  MTF_TREND_ALIGN_BONUS = {getattr(config, 'MTF_TREND_ALIGN_BONUS', 10)}\n")
        f.write(f"  MTF_TREND_CONFLICT_PENALTY = {getattr(config, 'MTF_TREND_CONFLICT_PENALTY', 20)}\n")
        f.write(f"  MTF_EMA_PERIOD = {getattr(config, 'MTF_EMA_PERIOD', 50)}\n")
        f.write(f"  VOLUME_GATE_MODERATE_THRESHOLD = {getattr(config, 'VOLUME_GATE_MODERATE_THRESHOLD', 0.5)}\n")
        f.write(f"  VOLUME_GATE_MODERATE_PENALTY = {getattr(config, 'VOLUME_GATE_MODERATE_PENALTY', 15)}\n")
        f.write(f"  VOLUME_GATE_SEVERE_THRESHOLD = {getattr(config, 'VOLUME_GATE_SEVERE_THRESHOLD', 0.3)}\n")
        f.write(f"  VOLUME_GATE_SEVERE_PENALTY = {getattr(config, 'VOLUME_GATE_SEVERE_PENALTY', 25)}\n")
        f.write(f"  MACD_DIVERGENCE_ADJUSTMENT = {getattr(config, 'MACD_DIVERGENCE_ADJUSTMENT', 15)}\n")
    
    print(f"\n📊 Report saved to: {report_path}")
    print(f"\nCompleted: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
