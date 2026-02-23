"""
Scenario-Based Confidence Threshold Diagnostic
===============================================
Analyzes the impact of different confidence thresholds (35%, 45%, 55%)
on trade filtering by scenario.

Key questions:
1. Win Rate by scenario (for scenarios with ≥10 trades)
2. Forced HOLDs by scenario (signals blocked by confidence threshold)
3. Confidence distribution by scenario

This script has two modes:
- Mode A: Analyze existing CSV (fast, but only shows trades that passed 55%)
- Mode B: Re-run backtest scan to capture ALL signals including blocked ones

Usage:
    python scripts/analyze_scenario_confidence.py           # Mode A only
    python scripts/analyze_scenario_confidence.py --full    # Mode A + B (requires MT5)
"""

import os
import sys
import pandas as pd
import numpy as np
from datetime import datetime
import argparse

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

# CSV with 662 trades that passed the 55% threshold
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")
# CSV with blocked signals (confidence < 55%)
BLOCKED_CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_blocked_signals_20260223_2028.csv")
OUTPUT_PATH = os.path.join(ROOT_DIR, "data", "scenario_confidence_analysis.txt")

# Thresholds to analyze
THRESHOLDS = [35, 45, 55, 65]


def load_trades(csv_path: str) -> pd.DataFrame:
    """Load backtest trades CSV."""
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['win'] = df['profit_pips'] > 0
    return df


def analyze_scenario_stats(df: pd.DataFrame, min_trades: int = 10) -> pd.DataFrame:
    """Analyze win rate and P&L by scenario."""
    scenarios = df.groupby('scenario').agg({
        'ticket': 'count',
        'win': 'sum',
        'profit_usd': ['sum', 'mean'],
        'profit_pips': 'mean',
        'confidence': ['min', 'mean', 'max', 'std'],
    }).reset_index()
    
    scenarios.columns = ['scenario', 'trades', 'wins', 'total_pnl', 'avg_pnl', 
                         'avg_pips', 'conf_min', 'conf_mean', 'conf_max', 'conf_std']
    scenarios['losses'] = scenarios['trades'] - scenarios['wins']
    scenarios['wr_pct'] = scenarios['wins'] / scenarios['trades'] * 100
    
    # Profit factor
    def calc_pf(scenario_name):
        s_df = df[df['scenario'] == scenario_name]
        wins_df = s_df[s_df['win']]
        losses_df = s_df[~s_df['win']]
        gw = wins_df['profit_usd'].sum() if len(wins_df) > 0 else 0
        gl = abs(losses_df['profit_usd'].sum()) if len(losses_df) > 0 else 0.01
        return gw / gl if gl > 0 else float('inf')
    
    scenarios['pf'] = scenarios['scenario'].apply(calc_pf)
    
    # Filter by min trades
    scenarios = scenarios[scenarios['trades'] >= min_trades].sort_values('trades', ascending=False)
    
    return scenarios


def analyze_confidence_distribution(df: pd.DataFrame) -> dict:
    """Analyze confidence distribution near thresholds."""
    results = {}
    
    for scenario in df['scenario'].unique():
        s_df = df[df['scenario'] == scenario]
        conf = s_df['confidence']
        
        results[scenario] = {
            'count': len(s_df),
            'min': conf.min(),
            'mean': conf.mean(),
            'max': conf.max(),
            'std': conf.std(),
            # Count near each threshold
            'near_55': len(s_df[(conf >= 55) & (conf < 60)]),
            'near_45': len(s_df[(conf >= 45) & (conf < 50)]),
            'near_35': len(s_df[(conf >= 35) & (conf < 40)]),
            # Would be blocked at each threshold
            'blocked_at_55': len(s_df[conf < 55]),
            'blocked_at_45': len(s_df[conf < 45]),
            'blocked_at_35': len(s_df[conf < 35]),
        }
    
    return results


def simulate_threshold_impact(df: pd.DataFrame, threshold: float) -> dict:
    """Simulate what happens if we use a different threshold."""
    # Trades that would pass
    passed = df[df['confidence'] >= threshold]
    blocked = df[df['confidence'] < threshold]
    
    if len(passed) == 0:
        return {
            'threshold': threshold,
            'passed': 0, 'blocked': len(blocked),
            'passed_wr': 0, 'blocked_wr': 0,
            'passed_pnl': 0, 'blocked_pnl': 0,
            'passed_pf': 0, 'blocked_pf': 0,
        }
    
    # Stats for passed trades
    passed_wins = passed['win'].sum()
    passed_wr = passed_wins / len(passed) * 100 if len(passed) > 0 else 0
    passed_pnl = passed['profit_usd'].sum()
    passed_gw = passed[passed['win']]['profit_usd'].sum()
    passed_gl = abs(passed[~passed['win']]['profit_usd'].sum())
    passed_pf = passed_gw / passed_gl if passed_gl > 0 else float('inf')
    
    # Stats for blocked trades (what we would have gotten)
    blocked_wins = blocked['win'].sum() if len(blocked) > 0 else 0
    blocked_wr = blocked_wins / len(blocked) * 100 if len(blocked) > 0 else 0
    blocked_pnl = blocked['profit_usd'].sum() if len(blocked) > 0 else 0
    blocked_gw = blocked[blocked['win']]['profit_usd'].sum() if len(blocked) > 0 else 0
    blocked_gl = abs(blocked[~blocked['win']]['profit_usd'].sum()) if len(blocked) > 0 else 0
    blocked_pf = blocked_gw / blocked_gl if blocked_gl > 0 else float('inf')
    
    return {
        'threshold': threshold,
        'passed': len(passed),
        'blocked': len(blocked),
        'passed_wr': passed_wr,
        'blocked_wr': blocked_wr,
        'passed_pnl': passed_pnl,
        'blocked_pnl': blocked_pnl,
        'passed_pf': passed_pf,
        'blocked_pf': blocked_pf,
    }


def simulate_threshold_by_scenario(df: pd.DataFrame, threshold: float) -> pd.DataFrame:
    """Simulate threshold impact broken down by scenario."""
    results = []
    
    for scenario in df['scenario'].unique():
        s_df = df[df['scenario'] == scenario]
        passed = s_df[s_df['confidence'] >= threshold]
        blocked = s_df[s_df['confidence'] < threshold]
        
        # Passed stats
        p_wins = passed['win'].sum() if len(passed) > 0 else 0
        p_wr = p_wins / len(passed) * 100 if len(passed) > 0 else 0
        p_pnl = passed['profit_usd'].sum() if len(passed) > 0 else 0
        
        # Blocked stats
        b_wins = blocked['win'].sum() if len(blocked) > 0 else 0
        b_wr = b_wins / len(blocked) * 100 if len(blocked) > 0 else 0
        b_pnl = blocked['profit_usd'].sum() if len(blocked) > 0 else 0
        
        results.append({
            'scenario': scenario,
            'total': len(s_df),
            'passed': len(passed),
            'blocked': len(blocked),
            'passed_wr': p_wr,
            'blocked_wr': b_wr,
            'passed_pnl': p_pnl,
            'blocked_pnl': b_pnl,
        })
    
    return pd.DataFrame(results).sort_values('total', ascending=False)


def generate_report(df: pd.DataFrame) -> str:
    """Generate full diagnostic report."""
    lines = []
    total = len(df)
    overall_wr = df['win'].mean() * 100
    overall_pnl = df['profit_usd'].sum()
    
    lines.append("=" * 90)
    lines.append("  SCENARIO-BASED CONFIDENCE THRESHOLD DIAGNOSTIC")
    lines.append(f"  Dataset: {total} trades (Aug 2024 - Feb 2026)")
    lines.append(f"  Overall: WR {overall_wr:.1f}%, P&L ${overall_pnl:+,.2f}")
    lines.append(f"  Current threshold: 55% (all {total} trades passed this)")
    lines.append("=" * 90)
    
    # =========================================================================
    # SECTION 1: Win Rate by Scenario
    # =========================================================================
    lines.append(f"\n{'─' * 90}")
    lines.append("  1. WIN RATE BY SCENARIO (≥10 trades)")
    lines.append(f"{'─' * 90}")
    
    scenarios = analyze_scenario_stats(df, min_trades=10)
    
    lines.append(f"  {'Scenario':<25} {'Trades':>6} {'Wins':>5} {'Loss':>5} {'WR%':>7} {'Avg Conf':>8} {'Avg P&L':>10} {'PF':>6}")
    lines.append(f"  {'-' * 25} {'-' * 6} {'-' * 5} {'-' * 5} {'-' * 7} {'-' * 8} {'-' * 10} {'-' * 6}")
    
    for _, row in scenarios.iterrows():
        lines.append(
            f"  {row['scenario']:<25} {row['trades']:>6.0f} {row['wins']:>5.0f} {row['losses']:>5.0f} "
            f"{row['wr_pct']:>6.1f}% {row['conf_mean']:>7.1f}% ${row['avg_pnl']:>+9.2f} {row['pf']:>6.2f}"
        )
    
    # =========================================================================
    # SECTION 2: Threshold Comparison (35%, 45%, 55%)
    # =========================================================================
    lines.append(f"\n{'─' * 90}")
    lines.append("  2. THRESHOLD COMPARISON (35% vs 45% vs 55%)")
    lines.append(f"{'─' * 90}")
    lines.append("  Note: All 662 trades passed 55%. Lower thresholds would ADD trades, not remove them.")
    lines.append("  To see blocked trades, we need to re-run backtest with logging. This shows existing data.")
    lines.append("")
    
    for threshold in THRESHOLDS:
        result = simulate_threshold_impact(df, threshold)
        lines.append(f"  Threshold {threshold}%:")
        lines.append(f"    Passed: {result['passed']:>4} trades | WR {result['passed_wr']:.1f}% | P&L ${result['passed_pnl']:+,.2f} | PF {result['passed_pf']:.2f}")
        if result['blocked'] > 0:
            lines.append(f"    Blocked: {result['blocked']:>3} trades | WR {result['blocked_wr']:.1f}% | P&L ${result['blocked_pnl']:+,.2f} | PF {result['blocked_pf']:.2f}")
            # Verdict on blocked trades
            if result['blocked_pf'] < 1.0:
                lines.append(f"    → Blocking these was CORRECT (PF < 1.0)")
            elif result['blocked_pf'] < result['passed_pf']:
                lines.append(f"    → Blocking these was CORRECT (lower PF than passed)")
            else:
                lines.append(f"    → Blocking these was WRONG (higher PF than passed!)")
        else:
            lines.append(f"    Blocked: 0 trades")
        lines.append("")
    
    # =========================================================================
    # SECTION 3: Confidence Distribution by Scenario
    # =========================================================================
    lines.append(f"{'─' * 90}")
    lines.append("  3. CONFIDENCE DISTRIBUTION BY SCENARIO")
    lines.append(f"{'─' * 90}")
    
    conf_dist = analyze_confidence_distribution(df)
    
    lines.append(f"  {'Scenario':<25} {'Count':>5} {'Min':>6} {'Mean':>6} {'Max':>6} {'Std':>6} {'55-60':>6} {'<55':>5}")
    lines.append(f"  {'-' * 25} {'-' * 5} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 6} {'-' * 5}")
    
    for scenario, data in sorted(conf_dist.items(), key=lambda x: -x[1]['count']):
        if data['count'] >= 10:
            lines.append(
                f"  {scenario:<25} {data['count']:>5} {data['min']:>5.0f}% {data['mean']:>5.1f}% "
                f"{data['max']:>5.0f}% {data['std']:>5.1f}% {data['near_55']:>6} {data['blocked_at_55']:>5}"
            )
    
    lines.append("")
    lines.append("  Legend: '55-60' = trades with confidence 55-60% (near threshold)")
    lines.append("          '<55' = trades that would be blocked if threshold were 55% (always 0 in this data)")
    
    # =========================================================================
    # SECTION 4: Threshold Impact by Scenario
    # =========================================================================
    lines.append(f"\n{'─' * 90}")
    lines.append("  4. THRESHOLD IMPACT BY SCENARIO")
    lines.append(f"{'─' * 90}")
    
    for threshold in [55, 45]:
        lines.append(f"\n  At {threshold}% threshold:")
        by_scenario = simulate_threshold_by_scenario(df, threshold)
        
        lines.append(f"  {'Scenario':<25} {'Total':>5} {'Pass':>5} {'Block':>5} {'Pass WR':>8} {'Block WR':>8} {'Block P&L':>10}")
        lines.append(f"  {'-' * 25} {'-' * 5} {'-' * 5} {'-' * 5} {'-' * 8} {'-' * 8} {'-' * 10}")
        
        for _, row in by_scenario.iterrows():
            if row['total'] >= 10:
                b_wr_str = f"{row['blocked_wr']:.1f}%" if row['blocked'] > 0 else "N/A"
                b_pnl_str = f"${row['blocked_pnl']:+.2f}" if row['blocked'] > 0 else "N/A"
                lines.append(
                    f"  {row['scenario']:<25} {row['total']:>5.0f} {row['passed']:>5.0f} {row['blocked']:>5.0f} "
                    f"{row['passed_wr']:>7.1f}% {b_wr_str:>8} {b_pnl_str:>10}"
                )
    
    # =========================================================================
    # SECTION 5: Key Insights
    # =========================================================================
    lines.append(f"\n{'─' * 90}")
    lines.append("  5. KEY INSIGHTS")
    lines.append(f"{'─' * 90}")
    
    # Find scenarios with lowest confidence
    low_conf_scenarios = []
    for scenario, data in conf_dist.items():
        if data['count'] >= 10 and data['mean'] < 70:
            low_conf_scenarios.append((scenario, data['mean'], data['count']))
    
    low_conf_scenarios.sort(key=lambda x: x[1])
    
    lines.append("\n  Scenarios with lowest average confidence (potential threshold impact):")
    for scenario, mean_conf, count in low_conf_scenarios[:5]:
        lines.append(f"    {scenario}: avg {mean_conf:.1f}% ({count} trades)")
    
    # Find scenarios with high variance
    high_var_scenarios = []
    for scenario, data in conf_dist.items():
        if data['count'] >= 10 and data['std'] > 10:
            high_var_scenarios.append((scenario, data['std'], data['min'], data['max']))
    
    high_var_scenarios.sort(key=lambda x: -x[1])
    
    lines.append("\n  Scenarios with high confidence variance (scenario-specific thresholds may help):")
    for scenario, std, min_c, max_c in high_var_scenarios[:5]:
        lines.append(f"    {scenario}: std {std:.1f}% (range {min_c:.0f}%-{max_c:.0f}%)")
    
    # =========================================================================
    # SECTION 6: Recommendation
    # =========================================================================
    lines.append(f"\n{'─' * 90}")
    lines.append("  6. LIMITATION & NEXT STEPS")
    lines.append(f"{'─' * 90}")
    lines.append("")
    lines.append("  ⚠️  CRITICAL LIMITATION:")
    lines.append("  This analysis only shows trades that PASSED the 55% threshold.")
    lines.append("  We cannot see trades that were BLOCKED by confidence < 55%.")
    lines.append("")
    lines.append("  To fully answer 'what would happen at 35% or 45%', we need to:")
    lines.append("  1. Re-run backtest with confidence logging for ALL signals (not just executed trades)")
    lines.append("  2. Capture signals where score >= 65 (BUY) or <= 35 (SELL) but confidence < 55%")
    lines.append("  3. Simulate their outcomes to see if blocking them was correct")
    lines.append("")
    lines.append("  WHAT WE CAN SAY FROM THIS DATA:")
    
    # Calculate stats for trades near threshold
    near_threshold = df[(df['confidence'] >= 55) & (df['confidence'] < 65)]
    if len(near_threshold) > 0:
        nt_wr = near_threshold['win'].mean() * 100
        nt_pnl = near_threshold['profit_usd'].sum()
        nt_gw = near_threshold[near_threshold['win']]['profit_usd'].sum()
        nt_gl = abs(near_threshold[~near_threshold['win']]['profit_usd'].sum())
        nt_pf = nt_gw / nt_gl if nt_gl > 0 else float('inf')
        
        lines.append(f"  - Trades with confidence 55-65% (near threshold): {len(near_threshold)} trades")
        lines.append(f"    WR: {nt_wr:.1f}%, P&L: ${nt_pnl:+,.2f}, PF: {nt_pf:.2f}")
        
        if nt_pf < 1.5:
            lines.append(f"    → These marginal trades have weak PF. Raising threshold might help.")
        else:
            lines.append(f"    → These marginal trades are profitable. Current threshold seems appropriate.")
    
    lines.append("")
    
    return "\n".join(lines)


def load_blocked_signals(csv_path: str) -> pd.DataFrame:
    """Load blocked signals CSV."""
    if not os.path.exists(csv_path):
        return pd.DataFrame()
    df = pd.read_csv(csv_path)
    df['time'] = pd.to_datetime(df['time'])
    df['win'] = df['would_have_won']
    return df


def analyze_blocked_signals(blocked_df: pd.DataFrame) -> str:
    """Analyze blocked signals that would have been trades if threshold were lower."""
    lines = []
    
    lines.append(f"\n{'=' * 90}")
    lines.append("  BLOCKED SIGNALS ANALYSIS (Confidence < 55%)")
    lines.append(f"{'=' * 90}")
    
    if len(blocked_df) == 0:
        lines.append("  No blocked signals data available.")
        lines.append("  Run: python scripts/run_backtest.py to generate blocked signals CSV.")
        return "\n".join(lines)
    
    total = len(blocked_df)
    wins = blocked_df['would_have_won'].sum()
    wr = wins / total * 100 if total > 0 else 0
    total_pnl = blocked_df['profit_usd'].sum()
    
    # Profit factor
    wins_df = blocked_df[blocked_df['would_have_won']]
    losses_df = blocked_df[~blocked_df['would_have_won']]
    gw = wins_df['profit_usd'].sum() if len(wins_df) > 0 else 0
    gl = abs(losses_df['profit_usd'].sum()) if len(losses_df) > 0 else 0.01
    pf = gw / gl if gl > 0 else float('inf')
    
    lines.append(f"\n  Total blocked signals: {total}")
    lines.append(f"  Would-have-won: {wins} ({wr:.1f}%)")
    lines.append(f"  Simulated P&L: ${total_pnl:+,.2f}")
    lines.append(f"  Simulated PF: {pf:.2f}")
    
    # By scenario
    lines.append(f"\n  By scenario:")
    lines.append(f"  {'Scenario':<25} {'Count':>5} {'WR%':>7} {'P&L':>12} {'Avg Conf':>9}")
    lines.append(f"  {'-' * 25} {'-' * 5} {'-' * 7} {'-' * 12} {'-' * 9}")
    
    for scenario in blocked_df['scenario'].unique():
        s_df = blocked_df[blocked_df['scenario'] == scenario]
        s_wins = s_df['would_have_won'].sum()
        s_wr = s_wins / len(s_df) * 100 if len(s_df) > 0 else 0
        s_pnl = s_df['profit_usd'].sum()
        s_conf = s_df['confidence'].mean()
        lines.append(f"  {scenario:<25} {len(s_df):>5} {s_wr:>6.1f}% ${s_pnl:>+10.2f} {s_conf:>8.1f}%")
    
    # By confidence bucket
    lines.append(f"\n  By confidence level:")
    lines.append(f"  {'Conf Range':<15} {'Count':>5} {'WR%':>7} {'P&L':>12}")
    lines.append(f"  {'-' * 15} {'-' * 5} {'-' * 7} {'-' * 12}")
    
    buckets = [(0, 35), (35, 45), (45, 55)]
    for lo, hi in buckets:
        b_df = blocked_df[(blocked_df['confidence'] >= lo) & (blocked_df['confidence'] < hi)]
        if len(b_df) > 0:
            b_wins = b_df['would_have_won'].sum()
            b_wr = b_wins / len(b_df) * 100
            b_pnl = b_df['profit_usd'].sum()
            lines.append(f"  {lo}-{hi}%{'':<10} {len(b_df):>5} {b_wr:>6.1f}% ${b_pnl:>+10.2f}")
    
    # Key insight
    lines.append(f"\n  KEY INSIGHT:")
    if pf >= 1.5:
        lines.append(f"  ⚠️ Blocked signals have PF {pf:.2f} — LOWERING threshold would ADD profitable trades!")
    elif pf >= 1.0:
        lines.append(f"  ⚖️ Blocked signals have PF {pf:.2f} — marginal, threshold change has small impact.")
    else:
        lines.append(f"  ✅ Blocked signals have PF {pf:.2f} — BLOCKING them was CORRECT.")
    
    return "\n".join(lines)


def analyze_marginal_trades(df: pd.DataFrame) -> str:
    """Deep dive into trades near the 55% threshold."""
    lines = []
    
    lines.append(f"\n{'=' * 90}")
    lines.append("  MARGINAL TRADES ANALYSIS (Confidence 55-65%)")
    lines.append(f"{'=' * 90}")
    
    marginal = df[(df['confidence'] >= 55) & (df['confidence'] < 65)]
    high_conf = df[df['confidence'] >= 65]
    
    if len(marginal) == 0:
        lines.append("  No marginal trades found.")
        return "\n".join(lines)
    
    # Overall comparison
    m_wr = marginal['win'].mean() * 100
    h_wr = high_conf['win'].mean() * 100
    m_pnl = marginal['profit_usd'].sum()
    h_pnl = high_conf['profit_usd'].sum()
    
    m_gw = marginal[marginal['win']]['profit_usd'].sum()
    m_gl = abs(marginal[~marginal['win']]['profit_usd'].sum())
    m_pf = m_gw / m_gl if m_gl > 0 else float('inf')
    
    h_gw = high_conf[high_conf['win']]['profit_usd'].sum()
    h_gl = abs(high_conf[~high_conf['win']]['profit_usd'].sum())
    h_pf = h_gw / h_gl if h_gl > 0 else float('inf')
    
    lines.append(f"\n  {'Category':<25} {'Trades':>6} {'WR%':>7} {'P&L':>12} {'PF':>6}")
    lines.append(f"  {'-' * 25} {'-' * 6} {'-' * 7} {'-' * 12} {'-' * 6}")
    lines.append(f"  {'Marginal (55-65%)':<25} {len(marginal):>6} {m_wr:>6.1f}% ${m_pnl:>+10.2f} {m_pf:>6.2f}")
    lines.append(f"  {'High confidence (65%+)':<25} {len(high_conf):>6} {h_wr:>6.1f}% ${h_pnl:>+10.2f} {h_pf:>6.2f}")
    
    # By scenario for marginal trades
    lines.append(f"\n  Marginal trades by scenario:")
    lines.append(f"  {'Scenario':<25} {'Count':>5} {'WR%':>7} {'P&L':>10} {'PF':>6}")
    lines.append(f"  {'-' * 25} {'-' * 5} {'-' * 7} {'-' * 10} {'-' * 6}")
    
    for scenario in marginal['scenario'].unique():
        s_df = marginal[marginal['scenario'] == scenario]
        if len(s_df) >= 5:  # At least 5 trades
            s_wr = s_df['win'].mean() * 100
            s_pnl = s_df['profit_usd'].sum()
            s_gw = s_df[s_df['win']]['profit_usd'].sum()
            s_gl = abs(s_df[~s_df['win']]['profit_usd'].sum())
            s_pf = s_gw / s_gl if s_gl > 0 else float('inf')
            lines.append(f"  {scenario:<25} {len(s_df):>5} {s_wr:>6.1f}% ${s_pnl:>+9.2f} {s_pf:>6.2f}")
    
    # What if we raised threshold to 65%?
    lines.append(f"\n  SIMULATION: What if threshold were 65% instead of 55%?")
    lines.append(f"  - Would block: {len(marginal)} trades")
    lines.append(f"  - Lost P&L: ${m_pnl:+.2f}")
    lines.append(f"  - Remaining trades: {len(high_conf)}")
    lines.append(f"  - Remaining WR: {h_wr:.1f}% (vs {df['win'].mean()*100:.1f}% overall)")
    lines.append(f"  - Remaining PF: {h_pf:.2f} (vs {2.14:.2f} overall)")
    
    if m_pf < 1.5 and h_pf > m_pf:
        lines.append(f"\n  → RECOMMENDATION: Consider raising threshold to 60-65%")
        lines.append(f"     Marginal trades have weak PF ({m_pf:.2f}). Blocking them would improve quality.")
    elif m_pf >= 1.5:
        lines.append(f"\n  → RECOMMENDATION: Keep threshold at 55%")
        lines.append(f"     Marginal trades are still profitable (PF {m_pf:.2f}).")
    
    return "\n".join(lines)


def analyze_combined_thresholds(df: pd.DataFrame, blocked_df: pd.DataFrame) -> str:
    """Analyze what happens at different threshold levels with complete data."""
    lines = []
    
    lines.append(f"\n{'=' * 90}")
    lines.append("  COMPLETE THRESHOLD ANALYSIS (Executed + Blocked)")
    lines.append(f"{'=' * 90}")
    
    if len(blocked_df) == 0:
        lines.append("  No blocked signals data. Run backtest first.")
        return "\n".join(lines)
    
    # Combine executed and blocked into one dataset
    executed = df.copy()
    executed['status'] = 'executed'
    executed['win'] = executed['profit_pips'] > 0
    
    blocked = blocked_df.copy()
    blocked['status'] = 'blocked'
    blocked['win'] = blocked['would_have_won']
    blocked['profit_pips'] = blocked['profit_pips']
    blocked['profit_usd'] = blocked['profit_usd']
    
    # Standardize columns
    common_cols = ['scenario', 'confidence', 'profit_pips', 'profit_usd', 'win', 'status', 'direction']
    executed_std = executed[common_cols].copy()
    blocked_std = blocked[common_cols].copy()
    
    all_signals = pd.concat([executed_std, blocked_std], ignore_index=True)
    
    lines.append(f"\n  Total signals: {len(all_signals)} (executed: {len(executed)}, blocked: {len(blocked)})")
    
    # Simulate different thresholds
    lines.append(f"\n  {'Threshold':<12} {'Trades':>6} {'WR%':>7} {'P&L':>14} {'PF':>6} {'vs 55%':>10}")
    lines.append(f"  {'-' * 12} {'-' * 6} {'-' * 7} {'-' * 14} {'-' * 6} {'-' * 10}")
    
    baseline_pnl = None
    for threshold in [35, 45, 55, 65]:
        passed = all_signals[all_signals['confidence'] >= threshold]
        if len(passed) == 0:
            continue
        
        wins = passed['win'].sum()
        wr = wins / len(passed) * 100
        pnl = passed['profit_usd'].sum()
        
        wins_df = passed[passed['win']]
        losses_df = passed[~passed['win']]
        gw = wins_df['profit_usd'].sum() if len(wins_df) > 0 else 0
        gl = abs(losses_df['profit_usd'].sum()) if len(losses_df) > 0 else 0.01
        pf = gw / gl if gl > 0 else float('inf')
        
        if threshold == 55:
            baseline_pnl = pnl
            diff_str = "(baseline)"
        else:
            diff = pnl - baseline_pnl if baseline_pnl else 0
            diff_str = f"${diff:+.2f}"
        
        lines.append(f"  {threshold}%{'':<9} {len(passed):>6} {wr:>6.1f}% ${pnl:>+12.2f} {pf:>6.2f} {diff_str:>10}")
    
    # Recommendation
    lines.append(f"\n  RECOMMENDATION:")
    
    # Calculate metrics for each threshold
    t35 = all_signals[all_signals['confidence'] >= 35]
    t45 = all_signals[all_signals['confidence'] >= 45]
    t55 = all_signals[all_signals['confidence'] >= 55]
    t65 = all_signals[all_signals['confidence'] >= 65]
    
    pnl_35 = t35['profit_usd'].sum() if len(t35) > 0 else 0
    pnl_45 = t45['profit_usd'].sum() if len(t45) > 0 else 0
    pnl_55 = t55['profit_usd'].sum() if len(t55) > 0 else 0
    pnl_65 = t65['profit_usd'].sum() if len(t65) > 0 else 0
    
    best_threshold = 55
    best_pnl = pnl_55
    for t, p in [(35, pnl_35), (45, pnl_45), (65, pnl_65)]:
        if p > best_pnl:
            best_threshold = t
            best_pnl = p
    
    if best_threshold == 55:
        lines.append(f"  ✅ Current threshold (55%) is OPTIMAL. No change needed.")
    elif best_threshold < 55:
        lines.append(f"  ⚠️ LOWER threshold to {best_threshold}% would ADD ${best_pnl - pnl_55:+.2f} P&L")
        lines.append(f"     But consider: more trades = more risk exposure, more monitoring needed.")
    else:
        lines.append(f"  📈 RAISE threshold to {best_threshold}% would ADD ${best_pnl - pnl_55:+.2f} P&L")
        lines.append(f"     Fewer trades but higher quality.")
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description='Scenario-based confidence threshold diagnostic')
    parser.add_argument('--full', action='store_true', help='Run full backtest scan (requires MT5)')
    args = parser.parse_args()
    
    if not os.path.exists(CSV_PATH):
        print(f"❌ CSV not found: {CSV_PATH}")
        return
    
    print(f"Loading trades from {CSV_PATH}...")
    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} executed trades")
    print(f"Confidence range: {df['confidence'].min():.0f}% - {df['confidence'].max():.0f}%")
    print(f"Scenarios: {df['scenario'].nunique()}")
    
    # Load blocked signals
    blocked_df = load_blocked_signals(BLOCKED_CSV_PATH)
    if len(blocked_df) > 0:
        print(f"Loaded {len(blocked_df)} blocked signals from {BLOCKED_CSV_PATH}")
    else:
        print(f"⚠️ No blocked signals CSV found. Run backtest to generate.")
    
    # Generate report
    report = generate_report(df)
    
    # Add blocked signals analysis
    blocked_report = analyze_blocked_signals(blocked_df)
    report += blocked_report
    
    # Add combined threshold analysis
    combined_report = analyze_combined_thresholds(df, blocked_df)
    report += combined_report
    
    # Add marginal trades analysis
    marginal_report = analyze_marginal_trades(df)
    report += marginal_report
    
    # Print to console
    print(report)
    
    # Save to file
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(report)
        f.write(f"\n\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    print(f"\n📄 Report saved: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
