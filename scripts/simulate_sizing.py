"""
Confidence-Based Position Sizing - Compounding Backtest
=======================================================
Replays 662 backtest trades with balance tracking to compare
fixed 2% risk vs dynamic confidence-based sizing with compounding.

Usage:
    python scripts/simulate_sizing.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

# ============================================================================
# PARAMETERS (matching config.py / risk_manager.py)
# ============================================================================
STARTING_BALANCE = 1000.0
PIP_SIZE = 0.1           # XAU/USD: 1 pip = $0.1
PIP_VALUE_PER_LOT = 10.0 # $10 per pip for 1 standard lot
LOT_STEP = 0.01
MIN_LOT = 0.01
MAX_LOT = 0.02

# Sizing modes
FIXED_RISK_PCT = 2.0

DYNAMIC_TIERS = [
    # (lo, hi, risk_pct)
    (55, 65, 1.0),   # Low confidence: 1% risk
    (65, 80, 2.0),   # Medium confidence: 2% risk
    (80, 101, 3.0),  # High confidence: 3% risk
]


def get_dynamic_risk(confidence: float) -> float:
    for lo, hi, risk in DYNAMIC_TIERS:
        if lo <= confidence < hi:
            return risk
    return 2.0  # fallback


def calc_lot_size(balance: float, risk_pct: float, sl_pips: float) -> float:
    """Calculate lot size using same formula as risk_manager.py"""
    risk_amount = balance * (risk_pct / 100.0)
    if sl_pips > 0:
        lot = risk_amount / (sl_pips * PIP_VALUE_PER_LOT)
    else:
        lot = MIN_LOT
    # Round to lot step
    lot = round(lot / LOT_STEP) * LOT_STEP
    # Clamp
    lot = max(MIN_LOT, min(lot, MAX_LOT))
    return lot


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    # Calculate SL pips from entry_price and sl columns
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    # Sort by entry time
    df = df.sort_values('entry_time').reset_index(drop=True)
    return df


def simulate(df: pd.DataFrame, mode: str) -> dict:
    """
    Simulate trades with compounding balance.
    mode: 'fixed' or 'dynamic'
    Returns dict with results and per-trade details.
    """
    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    max_dd = 0.0
    max_dd_pct = 0.0

    trades_detail = []

    for _, row in df.iterrows():
        conf = row['confidence']
        sl_pips = row['sl_pips']
        profit_pips = row['profit_pips']

        if mode == 'fixed':
            risk_pct = FIXED_RISK_PCT
        else:
            risk_pct = get_dynamic_risk(conf)

        lot = calc_lot_size(balance, risk_pct, sl_pips)
        pip_value = lot * PIP_VALUE_PER_LOT
        trade_pnl = profit_pips * pip_value

        balance_before = balance
        balance += trade_pnl
        peak_balance = max(peak_balance, balance)
        dd = peak_balance - balance
        dd_pct = (dd / peak_balance * 100) if peak_balance > 0 else 0
        max_dd = max(max_dd, dd)
        max_dd_pct = max(max_dd_pct, dd_pct)

        trades_detail.append({
            'entry_time': row['entry_time'],
            'confidence': conf,
            'sl_pips': sl_pips,
            'profit_pips': profit_pips,
            'risk_pct': risk_pct,
            'lot': lot,
            'pip_value': pip_value,
            'trade_pnl': trade_pnl,
            'balance_before': balance_before,
            'balance_after': balance,
            'win': row['win'],
            'tier': get_tier_label(conf),
        })

    detail_df = pd.DataFrame(trades_detail)
    wins = detail_df[detail_df['win']]
    losses = detail_df[~detail_df['win']]
    gross_profit = wins['trade_pnl'].sum() if len(wins) > 0 else 0
    gross_loss = abs(losses['trade_pnl'].sum()) if len(losses) > 0 else 0
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    return {
        'mode': mode,
        'trades': len(detail_df),
        'wins': len(wins),
        'wr': len(wins) / len(detail_df) * 100 if len(detail_df) > 0 else 0,
        'total_pnl': balance - STARTING_BALANCE,
        'final_balance': balance,
        'gross_profit': gross_profit,
        'gross_loss': gross_loss,
        'pf': pf,
        'max_dd': max_dd,
        'max_dd_pct': max_dd_pct,
        'detail': detail_df,
    }


def get_tier_label(conf: float) -> str:
    if 55 <= conf < 65:
        return "Low (55-65)"
    elif 65 <= conf < 80:
        return "Med (65-80)"
    elif 80 <= conf <= 100:
        return "High (80+)"
    return "Unknown"


# ============================================================================
# OUTPUT
# ============================================================================

def print_comparison(fixed: dict, dynamic: dict) -> None:
    print(f"\n{'=' * 95}")
    print(f"  COMPOUNDING BACKTEST: FIXED 2% vs DYNAMIC SIZING")
    print(f"  Starting balance: ${STARTING_BALANCE:,.0f} | Period: Aug 2024 - Feb 2026 | {fixed['trades']} trades")
    print(f"{'=' * 95}")
    print(f"")
    print(f"  {'Metric':>25}  {'Fixed 2%':>14}  {'Dynamic':>14}  {'Diff':>14}")
    print(f"  {'-' * 25}  {'-' * 14}  {'-' * 14}  {'-' * 14}")
    print(f"  {'Trades':>25}  {fixed['trades']:>14}  {dynamic['trades']:>14}  {'(same)':>14}")
    print(f"  {'Wins':>25}  {fixed['wins']:>14}  {dynamic['wins']:>14}  {'(same)':>14}")
    print(f"  {'Win Rate':>25}  {fixed['wr']:>13.1f}%  {dynamic['wr']:>13.1f}%  {'(same)':>14}")
    print(f"  {'Total P&L':>25}  ${fixed['total_pnl']:>+13.2f}  ${dynamic['total_pnl']:>+13.2f}  ${dynamic['total_pnl'] - fixed['total_pnl']:>+13.2f}")
    print(f"  {'Final Balance':>25}  ${fixed['final_balance']:>13.2f}  ${dynamic['final_balance']:>13.2f}  ${dynamic['final_balance'] - fixed['final_balance']:>+13.2f}")
    print(f"  {'Gross Profit':>25}  ${fixed['gross_profit']:>13.2f}  ${dynamic['gross_profit']:>13.2f}")
    print(f"  {'Gross Loss':>25}  ${fixed['gross_loss']:>13.2f}  ${dynamic['gross_loss']:>13.2f}")
    print(f"  {'Profit Factor':>25}  {fixed['pf']:>14.2f}  {dynamic['pf']:>14.2f}")
    print(f"  {'Max Drawdown $':>25}  ${fixed['max_dd']:>13.2f}  ${dynamic['max_dd']:>13.2f}  ${dynamic['max_dd'] - fixed['max_dd']:>+13.2f}")
    print(f"  {'Max Drawdown %':>25}  {fixed['max_dd_pct']:>13.1f}%  {dynamic['max_dd_pct']:>13.1f}%")

    fixed_ratio = fixed['total_pnl'] / fixed['max_dd'] if fixed['max_dd'] > 0 else 0
    dyn_ratio = dynamic['total_pnl'] / dynamic['max_dd'] if dynamic['max_dd'] > 0 else 0
    print(f"  {'P&L / Max DD ratio':>25}  {fixed_ratio:>13.2f}x  {dyn_ratio:>13.2f}x")
    print()


def print_lot_differentiation(fixed: dict, dynamic: dict) -> None:
    fd = fixed['detail']
    dd = dynamic['detail']

    print(f"\n{'=' * 95}")
    print(f"  LOT SIZE DIFFERENTIATION ANALYSIS")
    print(f"{'=' * 95}")

    # Merge to compare lot sizes
    fd_lots = fd['lot'].values
    dd_lots = dd['lot'].values
    same = (fd_lots == dd_lots).sum()
    diff = (fd_lots != dd_lots).sum()

    print(f"  Trades with SAME lot (fixed vs dynamic): {same} ({same / len(fd) * 100:.1f}%)")
    print(f"  Trades with DIFFERENT lot:               {diff} ({diff / len(fd) * 100:.1f}%)")
    print()

    # Per-tier breakdown
    print(f"  Per-tier lot differentiation:")
    print(f"  {'Tier':>14}  {'Trades':>6}  {'Same lot':>8}  {'Diff lot':>8}  {'% Diff':>7}  {'Avg lot Fixed':>13}  {'Avg lot Dyn':>12}")
    print(f"  {'-' * 14}  {'-' * 6}  {'-' * 8}  {'-' * 8}  {'-' * 7}  {'-' * 13}  {'-' * 12}")

    for tier in ["Low (55-65)", "Med (65-80)", "High (80+)"]:
        mask = dd['tier'] == tier
        n = mask.sum()
        if n == 0:
            continue
        tier_same = (fd_lots[mask] == dd_lots[mask]).sum()
        tier_diff = n - tier_same
        pct_diff = tier_diff / n * 100
        avg_f = fd_lots[mask].mean()
        avg_d = dd_lots[mask].mean()
        print(f"  {tier:>14}  {n:>6}  {tier_same:>8}  {tier_diff:>8}  {pct_diff:>6.1f}%  {avg_f:>13.4f}  {avg_d:>12.4f}")

    print()

    # At what balance do tiers differentiate?
    # For a typical SL of ~150 pips:
    # lot = balance * risk% / (sl_pips * pip_value_per_lot)
    # lot = balance * risk% / (150 * 10) = balance * risk% / 1500
    # For lot to round to 0.02 instead of 0.01: lot >= 0.015
    # balance * risk% / 1500 >= 0.015
    # balance >= 0.015 * 1500 / risk% = 22.5 / risk%
    # At 1%: balance >= $2,250
    # At 2%: balance >= $1,125
    # At 3%: balance >= $750

    print(f"  Balance thresholds for lot differentiation (typical SL ~150 pips):")
    print(f"  {'Risk %':>8}  {'Lot = 0.01 below':>18}  {'Lot = 0.02 above':>18}")
    print(f"  {'-' * 8}  {'-' * 18}  {'-' * 18}")
    for risk_pct in [1.0, 2.0, 3.0]:
        # lot = balance * risk / (sl * pip_val) >= 0.015 for rounding to 0.02
        threshold = 0.015 * 150 * PIP_VALUE_PER_LOT / (risk_pct / 100)
        print(f"  {risk_pct:>7.0f}%  ${threshold:>17,.0f}  {'(above this)':>18}")

    print()

    # Show when first differentiation happens
    first_diff_idx = None
    for i in range(len(fd_lots)):
        if fd_lots[i] != dd_lots[i]:
            first_diff_idx = i
            break

    if first_diff_idx is not None:
        row_f = fd.iloc[first_diff_idx]
        row_d = dd.iloc[first_diff_idx]
        print(f"  First lot differentiation at trade #{first_diff_idx + 1}:")
        print(f"    Time: {row_d['entry_time']}")
        print(f"    Balance: ${row_d['balance_before']:.2f}")
        print(f"    Confidence: {row_d['confidence']:.0f}% (tier: {row_d['tier']})")
        print(f"    SL: {row_d['sl_pips']:.0f} pips")
        print(f"    Fixed lot: {row_f['lot']:.2f} | Dynamic lot: {row_d['lot']:.2f}")
    else:
        print(f"  No lot differentiation occurred (all trades had same lot in both modes)")

    print()


def print_tier_performance(fixed: dict, dynamic: dict) -> None:
    dd = dynamic['detail']

    print(f"\n{'=' * 95}")
    print(f"  PER-TIER PERFORMANCE (Dynamic sizing)")
    print(f"{'=' * 95}")
    print(f"  {'Tier':>14}  {'Trades':>6}  {'Wins':>5}  {'WR%':>6}  {'Risk%':>6}  {'Avg Lot':>8}  {'P&L $':>12}  {'Avg P&L':>10}")
    print(f"  {'-' * 14}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 6}  {'-' * 8}  {'-' * 12}  {'-' * 10}")

    for tier in ["Low (55-65)", "Med (65-80)", "High (80+)"]:
        t_df = dd[dd['tier'] == tier]
        n = len(t_df)
        if n == 0:
            continue
        wins = t_df['win'].sum()
        wr = wins / n * 100
        risk = t_df['risk_pct'].iloc[0]
        avg_lot = t_df['lot'].mean()
        pnl = t_df['trade_pnl'].sum()
        avg_pnl = t_df['trade_pnl'].mean()
        print(f"  {tier:>14}  {n:>6}  {wins:>5.0f}  {wr:>5.1f}%  {risk:>5.0f}%  {avg_lot:>8.4f}  ${pnl:>+11.2f}  ${avg_pnl:>+9.2f}")

    print()


def print_equity_milestones(fixed: dict, dynamic: dict) -> None:
    fd = fixed['detail']
    dd = dynamic['detail']

    print(f"\n{'=' * 95}")
    print(f"  EQUITY MILESTONES")
    print(f"{'=' * 95}")

    milestones = [1100, 1250, 1500, 2000, 2500, 3000, 4000, 5000]
    print(f"  {'Balance':>10}  {'Fixed (trade #)':>16}  {'Dynamic (trade #)':>18}")
    print(f"  {'-' * 10}  {'-' * 16}  {'-' * 18}")

    for target in milestones:
        f_idx = None
        d_idx = None
        for i, row in fd.iterrows():
            if row['balance_after'] >= target and f_idx is None:
                f_idx = i + 1
        for i, row in dd.iterrows():
            if row['balance_after'] >= target and d_idx is None:
                d_idx = i + 1

        f_str = f"#{f_idx}" if f_idx else "not reached"
        d_str = f"#{d_idx}" if d_idx else "not reached"
        print(f"  ${target:>9,}  {f_str:>16}  {d_str:>18}")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(CSV_PATH):
        print(f"CSV not found: {CSV_PATH}")
        return

    df = load_trades(CSV_PATH)
    print(f"Loaded {len(df)} trades from CSV")
    print(f"SL pips range: {df['sl_pips'].min():.0f} - {df['sl_pips'].max():.0f} (median: {df['sl_pips'].median():.0f})")

    # Run both simulations
    fixed = simulate(df, 'fixed')
    dynamic = simulate(df, 'dynamic')

    # Output all tables
    print_comparison(fixed, dynamic)
    print_lot_differentiation(fixed, dynamic)
    print_tier_performance(fixed, dynamic)
    print_equity_milestones(fixed, dynamic)

    # Final summary
    pnl_diff = dynamic['total_pnl'] - fixed['total_pnl']
    pnl_pct = (pnl_diff / fixed['total_pnl'] * 100) if fixed['total_pnl'] > 0 else 0
    dd_diff = dynamic['max_dd'] - fixed['max_dd']

    print(f"{'=' * 95}")
    print(f"  SUMMARY")
    print(f"{'=' * 95}")
    print(f"  Dynamic sizing vs Fixed 2%:")
    print(f"    P&L improvement: ${pnl_diff:+.2f} ({pnl_pct:+.1f}%)")
    print(f"    Max DD change:   ${dd_diff:+.2f}")
    print(f"    Final balance:   ${fixed['final_balance']:.2f} (fixed) vs ${dynamic['final_balance']:.2f} (dynamic)")

    # Count trades where lot actually differed
    same = (fixed['detail']['lot'].values == dynamic['detail']['lot'].values).sum()
    diff = len(df) - same
    print(f"    Lot differentiation: {diff}/{len(df)} trades ({diff / len(df) * 100:.1f}%) had different lot sizes")

    if pnl_diff > 0 and dd_diff <= 0:
        print(f"  --> CLEAR WIN: Higher P&L with same or lower drawdown")
    elif pnl_diff > 0 and dd_diff > 0:
        print(f"  --> TRADEOFF: Higher P&L but also higher drawdown")
    else:
        print(f"  --> NO BENEFIT: Dynamic sizing did not improve results")

    print()


if __name__ == "__main__":
    main()
