"""
Session / Hour Analysis - XAUUSD Backtest Trades
=================================================
Reads backtest CSV and produces win rate, P&L, and trade count
breakdowns by hour (0-23 UTC) and by session.

Usage:
    python scripts/analyze_hours.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['hour'] = df['entry_time'].dt.hour
    df['win'] = df['profit_pips'] > 0
    return df


def get_session(hour: int) -> str:
    """Classify hour into trading session."""
    if 0 <= hour < 7:
        return "Asia (00-07)"
    elif 7 <= hour < 13:
        return "London (07-13)"
    elif 13 <= hour < 16:
        return "Overlap (13-16)"
    elif 16 <= hour < 21:
        return "NY (16-21)"
    else:
        return "Late (21-23)"


def analyze_by_hour(df: pd.DataFrame, label: str) -> None:
    total_trades = len(df)
    overall_wr = df['win'].mean() * 100 if total_trades > 0 else 0

    print(f"\n{'=' * 90}")
    print(f"  HOUR ANALYSIS - {label} ({total_trades} trades, overall WR {overall_wr:.1f}%)")
    print(f"{'=' * 90}")
    print(f"  {'Hour':>4}  {'Trades':>6}  {'Wins':>5}  {'Losses':>6}  {'WR%':>7}  {'P&L $':>10}  {'P&L pips':>10}  {'Avg pips':>9}  {'Flag':>8}")
    print(f"  {'-' * 4}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 7}  {'-' * 10}  {'-' * 10}  {'-' * 9}  {'-' * 8}")

    for hour in range(24):
        h_df = df[df['hour'] == hour]
        n = len(h_df)
        if n == 0:
            print(f"  {hour:>4}  {0:>6}  {'-':>5}  {'-':>6}  {'-':>7}  {'-':>10}  {'-':>10}  {'-':>9}  {'':>8}")
            continue

        wins = h_df['win'].sum()
        losses = n - wins
        wr = wins / n * 100
        pnl_usd = h_df['profit_usd'].sum()
        pnl_pips = h_df['profit_pips'].sum()
        avg_pips = h_df['profit_pips'].mean()

        # Flag logic
        flag = ""
        if n < 10:
            flag = "low n"
        elif wr < 50:
            flag = "!! BLOCK"
        elif wr < overall_wr - 10:
            flag = "!  weak"

        print(f"  {hour:>4}  {n:>6}  {wins:>5.0f}  {losses:>6.0f}  {wr:>6.1f}%  ${pnl_usd:>+9.2f}  {pnl_pips:>+9.1f}  {avg_pips:>+8.1f}  {flag:>8}")

    print()


def analyze_by_session(df: pd.DataFrame, label: str) -> None:
    total_trades = len(df)
    overall_wr = df['win'].mean() * 100 if total_trades > 0 else 0

    df = df.copy()
    df['session'] = df['hour'].apply(get_session)

    sessions_order = ["Asia (00-07)", "London (07-13)", "Overlap (13-16)", "NY (16-21)", "Late (21-23)"]

    print(f"\n{'=' * 90}")
    print(f"  SESSION ANALYSIS - {label} ({total_trades} trades, overall WR {overall_wr:.1f}%)")
    print(f"{'=' * 90}")
    print(f"  {'Session':>18}  {'Trades':>6}  {'Wins':>5}  {'Losses':>6}  {'WR%':>7}  {'P&L $':>10}  {'P&L pips':>10}  {'Avg pips':>9}  {'PF':>6}")
    print(f"  {'-' * 18}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 7}  {'-' * 10}  {'-' * 10}  {'-' * 9}  {'-' * 6}")

    for session in sessions_order:
        s_df = df[df['session'] == session]
        n = len(s_df)
        if n == 0:
            print(f"  {session:>18}  {0:>6}  {'-':>5}  {'-':>6}  {'-':>7}  {'-':>10}  {'-':>10}  {'-':>9}  {'-':>6}")
            continue

        wins_df = s_df[s_df['win']]
        losses_df = s_df[~s_df['win']]
        wins = len(wins_df)
        losses = len(losses_df)
        wr = wins / n * 100
        pnl_usd = s_df['profit_usd'].sum()
        pnl_pips = s_df['profit_pips'].sum()
        avg_pips = s_df['profit_pips'].mean()
        gross_win = wins_df['profit_usd'].sum() if len(wins_df) > 0 else 0
        gross_loss = abs(losses_df['profit_usd'].sum()) if len(losses_df) > 0 else 0
        pf = gross_win / gross_loss if gross_loss > 0 else float('inf')

        print(f"  {session:>18}  {n:>6}  {wins:>5.0f}  {losses:>6.0f}  {wr:>6.1f}%  ${pnl_usd:>+9.2f}  {pnl_pips:>+9.1f}  {avg_pips:>+8.1f}  {pf:>6.2f}")

    print()


def analyze_direction_by_hour(df: pd.DataFrame, label: str) -> None:
    """Show BUY vs SELL WR by hour for hours with WR < 60%."""
    total_trades = len(df)
    overall_wr = df['win'].mean() * 100 if total_trades > 0 else 0

    weak_hours = []
    for hour in range(24):
        h_df = df[df['hour'] == hour]
        n = len(h_df)
        if n >= 5:
            wr = h_df['win'].mean() * 100
            if wr < 60:
                weak_hours.append(hour)

    if not weak_hours:
        return

    print(f"\n{'=' * 90}")
    print(f"  DIRECTION BREAKDOWN FOR WEAK HOURS - {label}")
    print(f"{'=' * 90}")
    print(f"  {'Hour':>4}  {'Dir':>4}  {'Trades':>6}  {'Wins':>5}  {'WR%':>7}  {'P&L $':>10}  {'Avg pips':>9}")
    print(f"  {'-' * 4}  {'-' * 4}  {'-' * 6}  {'-' * 5}  {'-' * 7}  {'-' * 10}  {'-' * 9}")

    for hour in weak_hours:
        h_df = df[df['hour'] == hour]
        for direction in ['BUY', 'SELL']:
            d_df = h_df[h_df['direction'] == direction]
            n = len(d_df)
            if n == 0:
                continue
            wins = d_df['win'].sum()
            wr = wins / n * 100
            pnl = d_df['profit_usd'].sum()
            avg = d_df['profit_pips'].mean()
            print(f"  {hour:>4}  {direction:>4}  {n:>6}  {wins:>5.0f}  {wr:>6.1f}%  ${pnl:>+9.2f}  {avg:>+8.1f}")

    print()


def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades from {CSV_PATH}")
    print(f"Date range: {df['entry_time'].min()} to {df['entry_time'].max()}")

    # Full dataset
    analyze_by_hour(df, "FULL PERIOD (Aug 2024 - Feb 2026)")
    analyze_by_session(df, "FULL PERIOD (Aug 2024 - Feb 2026)")
    analyze_direction_by_hour(df, "FULL PERIOD")

    # Recent 6 months
    cutoff = pd.Timestamp('2025-08-18')
    df_recent = df[df['entry_time'] >= cutoff]
    if len(df_recent) > 0:
        analyze_by_hour(df_recent, f"RECENT 6M (Aug 2025 - Feb 2026, {len(df_recent)} trades)")
        analyze_by_session(df_recent, f"RECENT 6M (Aug 2025 - Feb 2026)")
        analyze_direction_by_hour(df_recent, "RECENT 6M")

    # Summary: blocking candidates
    print(f"\n{'=' * 90}")
    print(f"  BLOCKING CANDIDATES SUMMARY")
    print(f"{'=' * 90}")
    print(f"  Hours where WR < 50% AND n >= 10 in EITHER period:\n")

    candidates = set()
    for period_label, period_df in [("Full", df), ("Recent 6M", df_recent)]:
        for hour in range(24):
            h_df = period_df[period_df['hour'] == hour]
            n = len(h_df)
            if n >= 10:
                wr = h_df['win'].mean() * 100
                if wr < 50:
                    candidates.add(hour)
                    pnl = h_df['profit_usd'].sum()
                    print(f"  Hour {hour:>2} UTC - {period_label}: {n} trades, WR {wr:.1f}%, P&L ${pnl:+.2f}")

    if not candidates:
        print("  No hours with WR < 50% and n >= 10 in either period.")
    else:
        print(f"\n  Candidate hours for blocking: {sorted(candidates)}")

    print()


if __name__ == "__main__":
    main()
