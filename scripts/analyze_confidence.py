"""
Confidence-Based Position Sizing - Data Analysis
=================================================
Analyzes 662 backtest trades to evaluate whether scaling
risk by confidence level would improve P&L vs fixed 2% risk.

Usage:
    python scripts/analyze_confidence.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")


# ============================================================================
# TIER DEFINITIONS
# ============================================================================

TIERS = [
    ("Low (55-65)",   55, 65, 0.5),   # 1% risk = 0.5x base
    ("Medium (65-80)", 65, 80, 1.0),   # 2% risk = 1.0x base (unchanged)
    ("High (80-100)", 80, 101, 1.5),   # 3% risk = 1.5x base
]


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['win'] = df['profit_pips'] > 0
    return df


def get_tier(conf: float) -> str:
    for name, lo, hi, _ in TIERS:
        if lo <= conf < hi:
            return name
    return "Unknown"


def get_multiplier(conf: float) -> float:
    for _, lo, hi, mult in TIERS:
        if lo <= conf < hi:
            return mult
    return 1.0


# ============================================================================
# 1. DISTRIBUTION & STATS BY TIER
# ============================================================================

def analyze_tiers(df: pd.DataFrame) -> None:
    total = len(df)
    overall_wr = df['win'].mean() * 100
    overall_pnl = df['profit_usd'].sum()

    print(f"\n{'=' * 95}")
    print(f"  CONFIDENCE TIER ANALYSIS - {total} trades, overall WR {overall_wr:.1f}%, P&L ${overall_pnl:+.2f}")
    print(f"{'=' * 95}")
    print(f"  {'Tier':>18}  {'Trades':>6} {'%Tot':>5}  {'Wins':>5}  {'Losses':>6}  {'WR%':>7}  {'P&L $':>10}  {'Avg pips':>9}  {'PF':>6}")
    print(f"  {'-' * 18}  {'-' * 6} {'-' * 5}  {'-' * 5}  {'-' * 6}  {'-' * 7}  {'-' * 10}  {'-' * 9}  {'-' * 6}")

    for name, lo, hi, mult in TIERS:
        t_df = df[(df['confidence'] >= lo) & (df['confidence'] < hi)]
        n = len(t_df)
        if n == 0:
            continue
        wins_df = t_df[t_df['win']]
        losses_df = t_df[~t_df['win']]
        wins = len(wins_df)
        losses = len(losses_df)
        wr = wins / n * 100
        pnl = t_df['profit_usd'].sum()
        avg_pips = t_df['profit_pips'].mean()
        gw = wins_df['profit_usd'].sum() if len(wins_df) > 0 else 0
        gl = abs(losses_df['profit_usd'].sum()) if len(losses_df) > 0 else 0
        pf = gw / gl if gl > 0 else float('inf')
        pct = n / total * 100

        print(f"  {name:>18}  {n:>6} {pct:>4.1f}%  {wins:>5}  {losses:>6}  {wr:>6.1f}%  ${pnl:>+9.2f}  {avg_pips:>+8.1f}  {pf:>6.2f}")

    print()


# ============================================================================
# 2. GRANULAR CONFIDENCE vs WIN RATE CORRELATION
# ============================================================================

def analyze_correlation(df: pd.DataFrame) -> None:
    print(f"\n{'=' * 95}")
    print(f"  CONFIDENCE vs WIN RATE CORRELATION (5-point buckets)")
    print(f"{'=' * 95}")
    print(f"  {'Conf Range':>12}  {'Trades':>6}  {'Wins':>5}  {'WR%':>7}  {'P&L $':>10}  {'Avg P&L $':>10}  {'Avg pips':>9}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 5}  {'-' * 7}  {'-' * 10}  {'-' * 10}  {'-' * 9}")

    buckets = list(range(55, 101, 5))
    wr_by_bucket = []

    for i in range(len(buckets)):
        lo = buckets[i]
        hi = buckets[i] + 5 if i < len(buckets) - 1 else 101
        label = f"{lo}-{min(hi, 100)}"

        b_df = df[(df['confidence'] >= lo) & (df['confidence'] < hi)]
        n = len(b_df)
        if n == 0:
            continue

        wins = b_df['win'].sum()
        wr = wins / n * 100
        pnl = b_df['profit_usd'].sum()
        avg_pnl = b_df['profit_usd'].mean()
        avg_pips = b_df['profit_pips'].mean()
        wr_by_bucket.append((lo + 2.5, wr, n))

        print(f"  {label:>12}  {n:>6}  {wins:>5.0f}  {wr:>6.1f}%  ${pnl:>+9.2f}  ${avg_pnl:>+9.2f}  {avg_pips:>+8.1f}")

    # Calculate correlation coefficient
    if len(wr_by_bucket) >= 3:
        confs = [x[0] for x in wr_by_bucket]
        wrs = [x[1] for x in wr_by_bucket]
        weights = [x[2] for x in wr_by_bucket]
        corr = np.corrcoef(confs, wrs)[0, 1]

        # Weighted correlation
        mean_c = np.average(confs, weights=weights)
        mean_w = np.average(wrs, weights=weights)
        cov = np.average([(c - mean_c) * (w - mean_w) for c, w in zip(confs, wrs)], weights=weights)
        std_c = np.sqrt(np.average([(c - mean_c) ** 2 for c in confs], weights=weights))
        std_w = np.sqrt(np.average([(w - mean_w) ** 2 for w in wrs], weights=weights))
        w_corr = cov / (std_c * std_w) if std_c > 0 and std_w > 0 else 0

        print(f"\n  Pearson correlation (confidence vs WR):          r = {corr:+.3f}")
        print(f"  Weighted correlation (by sample size):            r = {w_corr:+.3f}")

        if abs(corr) < 0.3:
            verdict = "WEAK - confidence does NOT reliably predict WR"
        elif abs(corr) < 0.6:
            verdict = "MODERATE - some predictive value"
        else:
            verdict = "STRONG - confidence reliably predicts WR"
        print(f"  Interpretation: {verdict}")

    # Also show tier-level WR comparison
    print(f"\n  Tier-level WR comparison:")
    for name, lo, hi, _ in TIERS:
        t_df = df[(df['confidence'] >= lo) & (df['confidence'] < hi)]
        if len(t_df) > 0:
            wr = t_df['win'].mean() * 100
            avg_pnl = t_df['profit_usd'].mean()
            print(f"    {name:>18}: WR {wr:.1f}%, avg P&L ${avg_pnl:+.2f}/trade")

    print()


# ============================================================================
# 3. DYNAMIC SIZING SIMULATION
# ============================================================================

def simulate_dynamic_sizing(df: pd.DataFrame) -> None:
    total = len(df)

    # Fixed sizing: all trades at 1x (current system)
    fixed_pnl = df['profit_usd'].sum()

    # Dynamic sizing: multiply each trade's P&L by its tier multiplier
    df = df.copy()
    df['multiplier'] = df['confidence'].apply(get_multiplier)
    df['dynamic_pnl'] = df['profit_usd'] * df['multiplier']
    dynamic_pnl = df['dynamic_pnl'].sum()

    # Drawdown comparison
    def calc_drawdown(pnl_series):
        cumsum = pnl_series.cumsum()
        peak = cumsum.cummax()
        dd = peak - cumsum
        return dd.max()

    fixed_dd = calc_drawdown(df['profit_usd'])
    dynamic_dd = calc_drawdown(df['dynamic_pnl'])

    # Equity curves
    fixed_cum = df['profit_usd'].cumsum()
    dynamic_cum = df['dynamic_pnl'].cumsum()

    print(f"\n{'=' * 95}")
    print(f"  DYNAMIC SIZING SIMULATION - {total} trades")
    print(f"{'=' * 95}")
    print(f"  Tiers: Low(55-65)=0.5x | Medium(65-80)=1.0x | High(80-100)=1.5x")
    print(f"")
    print(f"  {'Metric':>25}  {'Fixed (2%)':>12}  {'Dynamic':>12}  {'Diff':>12}")
    print(f"  {'-' * 25}  {'-' * 12}  {'-' * 12}  {'-' * 12}")
    print(f"  {'Total P&L':>25}  ${fixed_pnl:>+11.2f}  ${dynamic_pnl:>+11.2f}  ${dynamic_pnl - fixed_pnl:>+11.2f}")
    print(f"  {'Max Drawdown':>25}  ${fixed_dd:>11.2f}  ${dynamic_dd:>11.2f}  ${dynamic_dd - fixed_dd:>+11.2f}")
    print(f"  {'P&L / Max DD ratio':>25}  {fixed_pnl / fixed_dd:>11.2f}x  {dynamic_pnl / dynamic_dd:>11.2f}x")
    print(f"  {'Final equity':>25}  ${fixed_cum.iloc[-1]:>+11.2f}  ${dynamic_cum.iloc[-1]:>+11.2f}")

    # Per-tier contribution
    print(f"\n  Per-tier P&L contribution:")
    print(f"  {'Tier':>18}  {'Trades':>6}  {'Fixed P&L':>12}  {'Dynamic P&L':>12}  {'Diff':>12}")
    print(f"  {'-' * 18}  {'-' * 6}  {'-' * 12}  {'-' * 12}  {'-' * 12}")

    for name, lo, hi, mult in TIERS:
        t_df = df[(df['confidence'] >= lo) & (df['confidence'] < hi)]
        n = len(t_df)
        if n == 0:
            continue
        fp = t_df['profit_usd'].sum()
        dp = t_df['dynamic_pnl'].sum()
        print(f"  {name:>18}  {n:>6}  ${fp:>+11.2f}  ${dp:>+11.2f}  ${dp - fp:>+11.2f}")

    # Worst-case analysis: what if high-confidence trades hit a losing streak?
    high_df = df[df['confidence'] >= 80]
    high_losses = high_df[~high_df['win']]
    if len(high_losses) > 0:
        worst_streak = 0
        current_streak = 0
        for _, row in high_df.iterrows():
            if not row['win']:
                current_streak += 1
                worst_streak = max(worst_streak, current_streak)
            else:
                current_streak = 0

        avg_high_loss_fixed = high_losses['profit_usd'].mean()
        avg_high_loss_dynamic = high_losses['dynamic_pnl'].mean()

        print(f"\n  High-confidence (80+) risk analysis:")
        print(f"    Total high-conf losses: {len(high_losses)}")
        print(f"    Worst losing streak: {worst_streak}")
        print(f"    Avg loss (fixed):   ${avg_high_loss_fixed:.2f}")
        print(f"    Avg loss (dynamic): ${avg_high_loss_dynamic:.2f}  ({mult:.1f}x amplified)")

    print()


# ============================================================================
# 4. ALTERNATIVE TIER SCENARIOS
# ============================================================================

def simulate_alternatives(df: pd.DataFrame) -> None:
    df = df.copy()
    fixed_pnl = df['profit_usd'].sum()

    scenarios = [
        ("Conservative: 0.5x / 1.0x / 1.25x", [(55, 65, 0.5), (65, 80, 1.0), (80, 101, 1.25)]),
        ("Proposed:      0.5x / 1.0x / 1.5x",  [(55, 65, 0.5), (65, 80, 1.0), (80, 101, 1.5)]),
        ("Aggressive:    0.5x / 1.0x / 2.0x",   [(55, 65, 0.5), (65, 80, 1.0), (80, 101, 2.0)]),
        ("Reduce-only:   0.5x / 1.0x / 1.0x",   [(55, 65, 0.5), (65, 80, 1.0), (80, 101, 1.0)]),
    ]

    def calc_dd(series):
        c = series.cumsum()
        return (c.cummax() - c).max()

    print(f"\n{'=' * 95}")
    print(f"  ALTERNATIVE SIZING SCENARIOS")
    print(f"{'=' * 95}")
    print(f"  Fixed baseline: P&L ${fixed_pnl:+.2f}, DD ${calc_dd(df['profit_usd']):.2f}")
    print(f"")
    print(f"  {'Scenario':>40}  {'P&L $':>12}  {'vs Fixed':>10}  {'Max DD':>10}  {'P&L/DD':>8}")
    print(f"  {'-' * 40}  {'-' * 12}  {'-' * 10}  {'-' * 10}  {'-' * 8}")

    for label, tiers in scenarios:
        def get_m(conf):
            for lo, hi, m in tiers:
                if lo <= conf < hi:
                    return m
            return 1.0

        df['sim_pnl'] = df.apply(lambda r: r['profit_usd'] * get_m(r['confidence']), axis=1)
        sim_total = df['sim_pnl'].sum()
        sim_dd = calc_dd(df['sim_pnl'])
        ratio = sim_total / sim_dd if sim_dd > 0 else float('inf')
        diff = sim_total - fixed_pnl

        print(f"  {label:>40}  ${sim_total:>+11.2f}  ${diff:>+9.2f}  ${sim_dd:>9.2f}  {ratio:>7.2f}x")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades from {CSV_PATH}")
    print(f"Confidence range: {df['confidence'].min():.0f} - {df['confidence'].max():.0f}")
    print(f"Confidence mean: {df['confidence'].mean():.1f}, median: {df['confidence'].median():.1f}")

    analyze_tiers(df)
    analyze_correlation(df)
    simulate_dynamic_sizing(df)
    simulate_alternatives(df)

    # Final verdict
    print(f"{'=' * 95}")
    print(f"  VERDICT")
    print(f"{'=' * 95}")

    # Check if WR actually increases with confidence
    low_wr = df[(df['confidence'] >= 55) & (df['confidence'] < 65)]['win'].mean() * 100
    med_wr = df[(df['confidence'] >= 65) & (df['confidence'] < 80)]['win'].mean() * 100
    high_wr = df[(df['confidence'] >= 80)]['win'].mean() * 100

    print(f"  WR by tier: Low={low_wr:.1f}% | Medium={med_wr:.1f}% | High={high_wr:.1f}%")
    wr_spread = high_wr - low_wr
    print(f"  WR spread (High - Low): {wr_spread:+.1f} percentage points")

    if wr_spread > 10:
        print(f"  --> Strong positive correlation. Dynamic sizing is justified.")
    elif wr_spread > 5:
        print(f"  --> Moderate correlation. Dynamic sizing may help, but gains are modest.")
    elif wr_spread > 0:
        print(f"  --> Weak correlation. Dynamic sizing adds complexity with minimal benefit.")
    else:
        print(f"  --> NO positive correlation. Dynamic sizing would INCREASE risk without benefit.")
        print(f"  --> Recommendation: ABANDON this feature or use reduce-only (0.5x for low conf).")

    print()


if __name__ == "__main__":
    main()
