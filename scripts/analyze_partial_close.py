"""
Partial Close Analysis - Data Analysis
=======================================
Analyzes whether closing 50% of position at an intermediate
profit target would capture profit being left on the table.

Usage:
    python scripts/analyze_partial_close.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

PIP_SIZE = 0.1


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    df['tp_pips'] = (abs(df['tp'] - df['entry_price']) / PIP_SIZE).round(1)
    # How far price went in our favor (max favorable excursion)
    df['tp_reach_pct'] = (df['max_favorable_pips'] / df['tp_pips'] * 100).round(1)
    return df


# ============================================================================
# 1. HOW MANY WINNERS PASS THROUGH 50% TP?
# ============================================================================

def analyze_tp_passage(df: pd.DataFrame) -> None:
    winners = df[df['win']]
    losers = df[~df['win']]
    total_w = len(winners)
    total_l = len(losers)

    print(f"\n{'=' * 100}")
    print(f"  PARTIAL CLOSE FEASIBILITY ANALYSIS - {len(df)} trades ({total_w} winners, {total_l} losers)")
    print(f"{'=' * 100}")

    # Test multiple partial close levels
    levels = [25, 33, 50, 66, 75]

    print(f"\n  What % of trades reached each TP fraction?")
    print(f"  {'TP Level':>10}  {'Winners':>10}  {'% of W':>7}  {'Losers':>10}  {'% of L':>7}  {'All':>10}  {'% of All':>9}")
    print(f"  {'-' * 10}  {'-' * 10}  {'-' * 7}  {'-' * 10}  {'-' * 7}  {'-' * 10}  {'-' * 9}")

    for pct in levels:
        w_reached = len(winners[winners['max_favorable_pips'] >= winners['tp_pips'] * pct / 100])
        l_reached = len(losers[losers['max_favorable_pips'] >= losers['tp_pips'] * pct / 100])
        all_reached = w_reached + l_reached
        w_pct = w_reached / total_w * 100 if total_w > 0 else 0
        l_pct = l_reached / total_l * 100 if total_l > 0 else 0
        a_pct = all_reached / len(df) * 100
        print(f"  {pct:>9}%  {w_reached:>10}  {w_pct:>6.1f}%  {l_reached:>10}  {l_pct:>6.1f}%  {all_reached:>10}  {a_pct:>8.1f}%")

    print()


# ============================================================================
# 2. AVERAGE PIPS AT 50% TP vs FINAL EXIT
# ============================================================================

def analyze_pips_comparison(df: pd.DataFrame) -> None:
    winners = df[df['win']].copy()

    print(f"\n{'=' * 100}")
    print(f"  PIPS COMPARISON: 50% TP MARK vs FINAL EXIT (winners only)")
    print(f"{'=' * 100}")

    # Winners that passed 50% TP
    winners['passed_50pct'] = winners['max_favorable_pips'] >= winners['tp_pips'] * 0.5
    passed = winners[winners['passed_50pct']]
    not_passed = winners[~winners['passed_50pct']]

    half_tp_pips = passed['tp_pips'] * 0.5

    print(f"\n  Winners that passed 50% TP: {len(passed)} ({len(passed) / len(winners) * 100:.1f}%)")
    print(f"  Winners that did NOT pass 50% TP: {len(not_passed)} ({len(not_passed) / len(winners) * 100:.1f}%)")
    print()

    if len(passed) > 0:
        print(f"  For winners that PASSED 50% TP ({len(passed)} trades):")
        print(f"    Avg pips at 50% TP mark:    {half_tp_pips.mean():+.1f} pips (guaranteed partial profit)")
        print(f"    Avg final exit pips:         {passed['profit_pips'].mean():+.1f} pips")
        print(f"    Avg max favorable pips:      {passed['max_favorable_pips'].mean():+.1f} pips")
        print(f"    Avg TP distance:             {passed['tp_pips'].mean():.1f} pips")
        print()

        # How many of these exited BELOW the 50% TP mark?
        # (i.e., price went up past 50% TP, then came back down and exited lower)
        exited_below_50 = passed[passed['profit_pips'] < half_tp_pips]
        exited_above_50 = passed[passed['profit_pips'] >= half_tp_pips]

        print(f"    Exited ABOVE 50% TP mark: {len(exited_above_50)} ({len(exited_above_50) / len(passed) * 100:.1f}%)")
        print(f"    Exited BELOW 50% TP mark: {len(exited_below_50)} ({len(exited_below_50) / len(passed) * 100:.1f}%)")
        print(f"    (These are trades where partial close would have LOCKED IN profit)")

        if len(exited_below_50) > 0:
            print(f"\n    Trades that fell back below 50% TP ({len(exited_below_50)}):")
            print(f"      Avg 50% TP level:   {(exited_below_50['tp_pips'] * 0.5).mean():+.1f} pips")
            print(f"      Avg final exit:     {exited_below_50['profit_pips'].mean():+.1f} pips")
            print(f"      Avg profit lost:    {((exited_below_50['tp_pips'] * 0.5) - exited_below_50['profit_pips']).mean():.1f} pips")
            print(f"      Close reasons: {exited_below_50['close_reason'].value_counts().to_dict()}")

    if len(not_passed) > 0:
        print(f"\n  For winners that did NOT pass 50% TP ({len(not_passed)} trades):")
        print(f"    Avg final exit pips:     {not_passed['profit_pips'].mean():+.1f} pips")
        print(f"    Avg max favorable pips:  {not_passed['max_favorable_pips'].mean():+.1f} pips")
        print(f"    Avg TP distance:         {not_passed['tp_pips'].mean():.1f} pips")
        print(f"    (Partial close at 50% TP would NOT trigger for these)")

    print()


# ============================================================================
# 3. LOSERS THAT PASSED 50% TP (profit left on the table)
# ============================================================================

def analyze_losers_that_passed(df: pd.DataFrame) -> None:
    losers = df[~df['win']].copy()
    total_losers = len(losers)

    print(f"\n{'=' * 100}")
    print(f"  LOSERS THAT PASSED 50% TP (biggest opportunity for partial close)")
    print(f"{'=' * 100}")

    losers['passed_50pct'] = losers['max_favorable_pips'] >= losers['tp_pips'] * 0.5
    passed = losers[losers['passed_50pct']]

    print(f"\n  Losers that reached 50% TP before losing: {len(passed)} ({len(passed) / total_losers * 100:.1f}% of losers)")

    if len(passed) > 0:
        half_tp = passed['tp_pips'] * 0.5
        print(f"    Avg 50% TP level:      {half_tp.mean():+.1f} pips")
        print(f"    Avg max favorable:     {passed['max_favorable_pips'].mean():+.1f} pips")
        print(f"    Avg final exit (loss): {passed['profit_pips'].mean():+.1f} pips")
        print(f"    Avg TP distance:       {passed['tp_pips'].mean():.1f} pips")
        print()

        # If we had closed 50% at the 50% TP mark:
        # - 50% of position: locked in at 50% TP = half_tp pips
        # - 50% of position: continued to final exit = profit_pips
        # - Blended result: (half_tp + profit_pips) / 2
        blended = (half_tp + passed['profit_pips']) / 2
        current_pnl = passed['profit_pips'].sum()
        partial_pnl = blended.sum()

        print(f"    Current total P&L (pips):  {current_pnl:+.1f}")
        print(f"    With 50% partial close:    {partial_pnl:+.1f}")
        print(f"    Improvement:               {partial_pnl - current_pnl:+.1f} pips")
        print(f"    Per trade improvement:     {(partial_pnl - current_pnl) / len(passed):+.1f} pips")

        # These are the trades where partial close genuinely helps
        # Price went in our favor past 50% TP, then reversed to a loss
        print(f"\n    Close reasons: {passed['close_reason'].value_counts().to_dict()}")
        print(f"    Avg duration: {passed['duration_min'].mean():.0f} min")

    # Also check: losers that passed 25% TP
    passed_25 = losers[losers['max_favorable_pips'] >= losers['tp_pips'] * 0.25]
    print(f"\n  For comparison - losers that reached 25% TP: {len(passed_25)} ({len(passed_25) / total_losers * 100:.1f}%)")

    print()


# ============================================================================
# 4. PARTIAL CLOSE P&L SIMULATION
# ============================================================================

def simulate_partial_close(df: pd.DataFrame) -> None:
    """
    Simulate closing 50% of position at 50% TP distance.
    For each trade:
    - If max_favorable >= 50% TP: close 50% at that level, remaining 50% at final exit
    - If max_favorable < 50% TP: no partial close, full position at final exit
    """
    print(f"\n{'=' * 100}")
    print(f"  PARTIAL CLOSE P&L SIMULATION (50% at 50% TP)")
    print(f"{'=' * 100}")

    df = df.copy()
    half_tp = df['tp_pips'] * 0.5
    df['reached_partial'] = df['max_favorable_pips'] >= half_tp

    # Current P&L (full position)
    current_pnl_pips = df['profit_pips'].sum()

    # Partial close P&L
    # Trades that reached 50% TP: blended = (50% TP + final exit) / 2
    # Trades that didn't: unchanged
    reached = df[df['reached_partial']]
    not_reached = df[~df['reached_partial']]

    partial_pnl_reached = ((reached['tp_pips'] * 0.5) + reached['profit_pips']) / 2
    partial_pnl_total = partial_pnl_reached.sum() + not_reached['profit_pips'].sum()

    print(f"\n  Trades reaching 50% TP: {len(reached)} ({len(reached) / len(df) * 100:.1f}%)")
    print(f"  Trades NOT reaching 50% TP: {len(not_reached)} ({len(not_reached) / len(df) * 100:.1f}%)")
    print()

    # Convert to USD (PIP_VALUE_001 = 0.10)
    pip_val = 0.10
    current_usd = current_pnl_pips * pip_val
    partial_usd = partial_pnl_total * pip_val

    print(f"  {'Metric':>30}  {'Current (full)':>14}  {'Partial Close':>14}  {'Diff':>14}")
    print(f"  {'-' * 30}  {'-' * 14}  {'-' * 14}  {'-' * 14}")
    print(f"  {'Total P&L (pips)':>30}  {current_pnl_pips:>+13.1f}  {partial_pnl_total:>+13.1f}  {partial_pnl_total - current_pnl_pips:>+13.1f}")
    print(f"  {'Total P&L ($)':>30}  ${current_usd:>+12.2f}  ${partial_usd:>+12.2f}  ${partial_usd - current_usd:>+12.2f}")

    # Break down by winner/loser
    print(f"\n  Breakdown by outcome:")
    for label, subset in [("Winners", df[df['win']]), ("Losers", df[~df['win']])]:
        s_reached = subset[subset['reached_partial']]
        s_not = subset[~subset['reached_partial']]

        current = subset['profit_pips'].sum()
        if len(s_reached) > 0:
            partial_r = ((s_reached['tp_pips'] * 0.5) + s_reached['profit_pips']) / 2
            partial = partial_r.sum() + s_not['profit_pips'].sum()
        else:
            partial = s_not['profit_pips'].sum()

        diff = partial - current
        print(f"    {label}: current {current:+.1f} pips -> partial {partial:+.1f} pips (diff {diff:+.1f})")
        if len(s_reached) > 0:
            print(f"      {len(s_reached)} trades triggered partial close")

    # Impact on specific trade categories
    print(f"\n  Impact by close reason (trades that reached 50% TP):")
    print(f"  {'Reason':>15}  {'Count':>6}  {'Curr avg pips':>14}  {'Partial avg':>12}  {'Diff':>8}")
    print(f"  {'-' * 15}  {'-' * 6}  {'-' * 14}  {'-' * 12}  {'-' * 8}")

    for reason in ['sl', 'sl_gap', 'tp', 'tp_gap', 'max_duration']:
        r_reached = reached[reached['close_reason'] == reason]
        n = len(r_reached)
        if n == 0:
            continue
        curr_avg = r_reached['profit_pips'].mean()
        partial_avg = ((r_reached['tp_pips'] * 0.5) + r_reached['profit_pips']).mean() / 2
        diff = partial_avg - curr_avg
        print(f"  {reason:>15}  {n:>6}  {curr_avg:>+13.1f}  {partial_avg:>+11.1f}  {diff:>+7.1f}")

    # Max drawdown comparison
    # Current drawdown
    running = 0
    peak = 0
    max_dd_current = 0
    for _, row in df.iterrows():
        running += row['profit_pips'] * pip_val
        peak = max(peak, running)
        max_dd_current = max(max_dd_current, peak - running)

    # Partial close drawdown
    running = 0
    peak = 0
    max_dd_partial = 0
    for _, row in df.iterrows():
        if row['reached_partial']:
            pnl = ((row['tp_pips'] * 0.5) + row['profit_pips']) / 2 * pip_val
        else:
            pnl = row['profit_pips'] * pip_val
        running += pnl
        peak = max(peak, running)
        max_dd_partial = max(max_dd_partial, peak - running)

    ratio_curr = current_usd / max_dd_current if max_dd_current > 0 else 0
    ratio_part = partial_usd / max_dd_partial if max_dd_partial > 0 else 0

    print(f"\n  {'Max Drawdown':>30}  ${max_dd_current:>13.2f}  ${max_dd_partial:>13.2f}  ${max_dd_partial - max_dd_current:>+13.2f}")
    print(f"  {'P&L / Max DD':>30}  {ratio_curr:>13.1f}x  {ratio_part:>13.1f}x")

    print()


# ============================================================================
# 5. SENSITIVITY: DIFFERENT PARTIAL CLOSE LEVELS
# ============================================================================

def sensitivity_partial_levels(df: pd.DataFrame) -> None:
    pip_val = 0.10

    print(f"\n{'=' * 100}")
    print(f"  PARTIAL CLOSE SENSITIVITY (different trigger levels)")
    print(f"{'=' * 100}")
    print(f"  Close 50% of position at X% of TP distance")
    print()

    current_pnl = df['profit_pips'].sum() * pip_val

    print(f"  {'Level':>8}  {'Triggered':>10}  {'P&L $':>12}  {'vs Current':>11}  {'Max DD':>10}  {'P&L/DD':>8}")
    print(f"  {'-' * 8}  {'-' * 10}  {'-' * 12}  {'-' * 11}  {'-' * 10}  {'-' * 8}")

    # Current (no partial)
    running = 0
    peak = 0
    max_dd = 0
    for _, row in df.iterrows():
        running += row['profit_pips'] * pip_val
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    ratio = current_pnl / max_dd if max_dd > 0 else 0
    print(f"  {'None':>8}  {'---':>10}  ${current_pnl:>+11.2f}  {'baseline':>11}  ${max_dd:>9.2f}  {ratio:>7.1f}x")

    for level_pct in [25, 33, 50, 66, 75]:
        trigger_dist = df['tp_pips'] * level_pct / 100
        triggered = df['max_favorable_pips'] >= trigger_dist
        n_triggered = triggered.sum()

        # Calculate blended P&L
        total_pnl = 0
        running = 0
        peak = 0
        max_dd = 0
        for idx, row in df.iterrows():
            if triggered[idx]:
                pnl = ((row['tp_pips'] * level_pct / 100) + row['profit_pips']) / 2 * pip_val
            else:
                pnl = row['profit_pips'] * pip_val
            total_pnl += pnl
            running += pnl
            peak = max(peak, running)
            max_dd = max(max_dd, peak - running)

        diff = total_pnl - current_pnl
        ratio = total_pnl / max_dd if max_dd > 0 else 0
        print(f"  {level_pct:>7}%  {n_triggered:>10}  ${total_pnl:>+11.2f}  ${diff:>+10.2f}  ${max_dd:>9.2f}  {ratio:>7.1f}x")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades ({df['win'].sum()} winners, {(~df['win']).sum()} losers)")

    analyze_tp_passage(df)
    analyze_pips_comparison(df)
    analyze_losers_that_passed(df)
    simulate_partial_close(df)
    sensitivity_partial_levels(df)

    # Verdict
    print(f"{'=' * 100}")
    print(f"  VERDICT")
    print(f"{'=' * 100}")

    winners = df[df['win']]
    losers = df[~df['win']]
    half_tp = df['tp_pips'] * 0.5
    w_passed = winners[winners['max_favorable_pips'] >= winners['tp_pips'] * 0.5]
    l_passed = losers[losers['max_favorable_pips'] >= losers['tp_pips'] * 0.5]

    # Winners that fell back below 50% TP
    w_fell_back = w_passed[w_passed['profit_pips'] < w_passed['tp_pips'] * 0.5]

    print(f"  Winners passing 50% TP: {len(w_passed)}/{len(winners)} ({len(w_passed) / len(winners) * 100:.1f}%)")
    print(f"  Winners that fell back below 50% TP: {len(w_fell_back)} ({len(w_fell_back) / len(w_passed) * 100:.1f}% of those)")
    print(f"  Losers that reached 50% TP: {len(l_passed)}/{len(losers)} ({len(l_passed) / len(losers) * 100:.1f}%)")
    print(f"  (These {len(l_passed)} losers are the primary beneficiaries of partial close)")
    print()


if __name__ == "__main__":
    main()
