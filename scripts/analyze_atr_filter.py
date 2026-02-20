"""
ATR Minimum Filter Simulation
==============================
Simulates blocking trades when ATR is below a threshold.
Tests whether a low-ATR filter improves overall performance.

Usage:
    python scripts/analyze_atr_filter.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

PIP_SIZE = 0.1
SL_ATR_MULT = 1.5
MIN_SL_PIPS = 150


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    df['hour'] = df['entry_time'].dt.hour

    # SL/TP distances
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    df['tp_pips'] = (abs(df['tp'] - df['entry_price']) / PIP_SIZE).round(1)

    # ATR estimation
    # For unclamped trades (SL > 150): ATR = SL / 1.5 (exact)
    # For clamped trades (SL = 150): ATR < 100p (unknown exact value)
    # We estimate clamped ATR as SL / 1.5 = 100p, but flag it
    df['is_clamped'] = df['sl_pips'] <= 151
    df['atr_est'] = (df['sl_pips'] / SL_ATR_MULT).round(1)
    # For clamped trades, true ATR is BELOW 100p — we don't know how far below
    # This means ATR filters at <=100p will catch ALL clamped trades

    # Session
    def get_session(h):
        if 0 <= h < 7: return "Asia"
        elif 7 <= h < 13: return "London"
        elif 13 <= h < 16: return "Overlap"
        elif 16 <= h < 21: return "NY"
        else: return "Off-hours"
    df['session'] = df['hour'].apply(get_session)

    return df


def calc_stats(subset: pd.DataFrame) -> dict:
    n = len(subset)
    if n == 0:
        return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pf': 0, 'max_dd': 0, 'max_dd_pct': 0}
    wins = subset[subset['win']]
    losses = subset[~subset['win']]
    gw = wins['profit_usd'].sum() if len(wins) > 0 else 0
    gl = abs(losses['profit_usd'].sum()) if len(losses) > 0 else 0
    pf = gw / gl if gl > 0 else float('inf')

    # Max drawdown (sequential)
    running = 0
    peak = 0
    max_dd = 0
    for _, row in subset.iterrows():
        running += row['profit_usd']
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)

    return {
        'trades': n,
        'wins': len(wins),
        'wr': len(wins) / n * 100,
        'pnl': subset['profit_usd'].sum(),
        'pf': pf,
        'max_dd': max_dd,
    }


# ============================================================================
# MAIN ANALYSIS
# ============================================================================

def analyze_atr_filter(df: pd.DataFrame) -> None:
    baseline = calc_stats(df)

    print(f"\n{'=' * 105}")
    print(f"  ATR MINIMUM FILTER SIMULATION - {len(df)} trades")
    print(f"{'=' * 105}")
    print(f"  Baseline (no filter): {baseline['trades']} trades, WR {baseline['wr']:.1f}%, "
          f"P&L ${baseline['pnl']:+.2f}, PF {baseline['pf']:.2f}, Max DD ${baseline['max_dd']:.2f}")
    print()

    # Note about ATR estimation for clamped trades
    print(f"  NOTE: {df['is_clamped'].sum()} trades ({df['is_clamped'].mean() * 100:.1f}%) are clamped at MIN_SL=150p.")
    print(f"  For these, true ATR < 100p (exact value unknown). All ATR filters <= 100p block ALL clamped trades.")
    print(f"  ATR filter > 100p also blocks some unclamped trades with ATR in [100, threshold).")
    print()

    # Test thresholds
    thresholds = [60, 80, 100, 110, 120, 140]

    print(f"  {'ATR Min':>8}  {'Blocked':>8}  {'Remain':>7}  {'Blk WR%':>8}  {'Rem WR%':>8}  {'Rem P&L':>12}  {'P&L Diff':>10}  {'Rem PF':>7}  {'Rem DD':>10}  {'DD Diff':>10}  {'P&L/DD':>7}")
    print(f"  {'-' * 8}  {'-' * 8}  {'-' * 7}  {'-' * 8}  {'-' * 8}  {'-' * 12}  {'-' * 10}  {'-' * 7}  {'-' * 10}  {'-' * 10}  {'-' * 7}")

    for threshold in thresholds:
        # Block trades where ATR < threshold
        # For clamped trades (atr_est = 100): blocked if threshold > 100
        # For unclamped trades: blocked if atr_est < threshold
        # Special case: for clamped trades, true ATR < 100, so any threshold >= 100 blocks them
        # For thresholds < 100: we can't know which clamped trades to block
        # (their true ATR could be 50p or 99p)
        # Conservative approach: for threshold < 100, assume clamped ATR ~ uniformly distributed [50, 100]
        # More practical: just use atr_est directly (100p for clamped)

        if threshold <= 100:
            # Only block unclamped trades with ATR < threshold (none exist since unclamped ATR >= 100)
            # AND clamped trades — but we don't know their true ATR
            # For threshold < 100: we can't reliably filter clamped trades
            # Use the SL-based proxy: block trades where SL < threshold * 1.5
            blocked = df[df['atr_est'] < threshold]
        else:
            blocked = df[df['atr_est'] < threshold]

        remaining = df.drop(blocked.index)

        blk_stats = calc_stats(blocked)
        rem_stats = calc_stats(remaining)

        pnl_diff = rem_stats['pnl'] - baseline['pnl']
        dd_diff = rem_stats['max_dd'] - baseline['max_dd']
        ratio = rem_stats['pnl'] / rem_stats['max_dd'] if rem_stats['max_dd'] > 0 else 0

        blk_wr = f"{blk_stats['wr']:.1f}%" if blk_stats['trades'] > 0 else "n/a"

        print(f"  {threshold:>7}p  {blk_stats['trades']:>8}  {rem_stats['trades']:>7}  {blk_wr:>8}  {rem_stats['wr']:>7.1f}%"
              f"  ${rem_stats['pnl']:>+11.2f}  ${pnl_diff:>+9.2f}  {rem_stats['pf']:>7.2f}"
              f"  ${rem_stats['max_dd']:>9.2f}  ${dd_diff:>+9.2f}  {ratio:>6.1f}x")

    print()


def analyze_blocked_detail(df: pd.DataFrame) -> None:
    """Detailed breakdown of blocked trades at key thresholds."""

    for threshold in [80, 100, 120]:
        blocked = df[df['atr_est'] < threshold]
        remaining = df.drop(blocked.index)

        if len(blocked) == 0:
            continue

        print(f"\n{'=' * 105}")
        print(f"  BLOCKED TRADES DETAIL: ATR < {threshold}p")
        print(f"{'=' * 105}")

        n = len(blocked)
        wins = blocked['win'].sum()
        losses = n - wins
        pnl = blocked['profit_usd'].sum()

        print(f"  Blocked: {n} trades | Wins: {wins} | Losses: {losses} | WR: {wins / n * 100:.1f}% | P&L: ${pnl:+.2f}")
        print()

        # By session
        print(f"  Blocked by session:")
        print(f"  {'Session':>12}  {'Blocked':>8}  {'Total':>6}  {'% Blocked':>10}  {'Blk WR%':>8}  {'Blk P&L':>10}")
        print(f"  {'-' * 12}  {'-' * 8}  {'-' * 6}  {'-' * 10}  {'-' * 8}  {'-' * 10}")

        for session in ["Asia", "London", "Overlap", "NY", "Off-hours"]:
            s_blk = blocked[blocked['session'] == session]
            s_all = df[df['session'] == session]
            n_b = len(s_blk)
            n_a = len(s_all)
            if n_b == 0:
                continue
            pct = n_b / n_a * 100 if n_a > 0 else 0
            wr = s_blk['win'].mean() * 100
            s_pnl = s_blk['profit_usd'].sum()
            print(f"  {session:>12}  {n_b:>8}  {n_a:>6}  {pct:>9.1f}%  {wr:>7.1f}%  ${s_pnl:>+9.2f}")

        # By close reason
        print(f"\n  Blocked by close reason:")
        for reason in ['sl', 'sl_gap', 'tp', 'tp_gap', 'max_duration']:
            r_df = blocked[blocked['close_reason'] == reason]
            if len(r_df) > 0:
                print(f"    {reason:>15}: {len(r_df)} ({len(r_df) / n * 100:.1f}%)")

        # Remaining performance
        rem = calc_stats(remaining)
        base = calc_stats(df)
        ratio_base = base['pnl'] / base['max_dd'] if base['max_dd'] > 0 else 0
        ratio_rem = rem['pnl'] / rem['max_dd'] if rem['max_dd'] > 0 else 0

        print(f"\n  Remaining performance ({rem['trades']} trades):")
        print(f"    WR: {rem['wr']:.1f}% (was {base['wr']:.1f}%)")
        print(f"    P&L: ${rem['pnl']:+.2f} (was ${base['pnl']:+.2f}, diff ${rem['pnl'] - base['pnl']:+.2f})")
        print(f"    PF: {rem['pf']:.2f} (was {base['pf']:.2f})")
        print(f"    Max DD: ${rem['max_dd']:.2f} (was ${base['max_dd']:.2f})")
        print(f"    P&L/DD: {ratio_rem:.1f}x (was {ratio_base:.1f}x)")

        # Key question: are we blocking more losers than winners?
        blk_losers = n - wins
        blk_winners = wins
        print(f"\n  Blocking {blk_losers} losers and {blk_winners} winners")
        if blk_losers > blk_winners:
            print(f"  --> Blocking {blk_losers - blk_winners} more losers than winners (good)")
        else:
            print(f"  --> Blocking {blk_winners - blk_losers} more winners than losers (BAD)")

        print()


def analyze_time_evolution(df: pd.DataFrame) -> None:
    """Show how ATR filter impact changes over time (early vs late in backtest)."""
    print(f"\n{'=' * 105}")
    print(f"  ATR FILTER IMPACT BY TIME PERIOD (ATR < 100p filter)")
    print(f"{'=' * 105}")

    df = df.copy()
    df['month'] = df['entry_time'].dt.to_period('M')
    months = sorted(df['month'].unique())

    print(f"  {'Month':>10}  {'Total':>6}  {'Clamped':>8}  {'% Clamp':>8}  {'WR All':>7}  {'WR Clamp':>9}  {'WR Unclamp':>11}")
    print(f"  {'-' * 10}  {'-' * 6}  {'-' * 8}  {'-' * 8}  {'-' * 7}  {'-' * 9}  {'-' * 11}")

    for month in months:
        m_df = df[df['month'] == month]
        n = len(m_df)
        if n < 5:
            continue
        clamped = m_df[m_df['is_clamped']]
        unclamped = m_df[~m_df['is_clamped']]
        n_c = len(clamped)
        pct_c = n_c / n * 100
        wr_all = m_df['win'].mean() * 100
        wr_c = clamped['win'].mean() * 100 if n_c > 0 else 0
        wr_u = unclamped['win'].mean() * 100 if len(unclamped) > 0 else 0
        c_str = f"{wr_c:.1f}%" if n_c >= 3 else f"{wr_c:.1f}%*"
        u_str = f"{wr_u:.1f}%" if len(unclamped) >= 3 else f"{wr_u:.1f}%*"
        print(f"  {str(month):>10}  {n:>6}  {n_c:>8}  {pct_c:>7.1f}%  {wr_all:>6.1f}%  {c_str:>9}  {u_str:>11}")

    print(f"\n  * = fewer than 3 trades in category")
    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades")

    analyze_atr_filter(df)
    analyze_blocked_detail(df)
    analyze_time_evolution(df)

    # Final verdict
    print(f"{'=' * 105}")
    print(f"  VERDICT")
    print(f"{'=' * 105}")

    # Compare key scenarios
    base = calc_stats(df)
    for threshold in [80, 100, 120]:
        blocked = df[df['atr_est'] < threshold]
        remaining = df.drop(blocked.index)
        rem = calc_stats(remaining)
        blk = calc_stats(blocked)
        ratio_base = base['pnl'] / base['max_dd'] if base['max_dd'] > 0 else 0
        ratio_rem = rem['pnl'] / rem['max_dd'] if rem['max_dd'] > 0 else 0

        pnl_diff = rem['pnl'] - base['pnl']
        blocked_losers = blk['trades'] - blk['wins']
        blocked_winners = blk['wins']

        print(f"  ATR >= {threshold}p: Block {blk['trades']} trades ({blocked_winners}W/{blocked_losers}L), "
              f"P&L ${pnl_diff:+.2f}, P&L/DD {ratio_base:.1f}x -> {ratio_rem:.1f}x")

    print()


if __name__ == "__main__":
    main()
