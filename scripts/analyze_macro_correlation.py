"""
Multi-Asset Correlation Filter - Data Analysis
===============================================
Downloads historical DXY, 10Y Yields, and VIX from Yahoo Finance,
computes direction (5-day change), and analyzes correlation with
trade direction and win rate.

Usage:
    python scripts/analyze_macro_correlation.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_CSV = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")

# Yahoo Finance tickers
TICKERS = {
    'DXY': 'DX-Y.NYB',
    'Yields_10Y': '^TNX',
    'VIX': '^VIX',
}

# Direction thresholds (5-day % change)
RISING_THRESHOLD = 0.3   # > 0.3% = rising
FALLING_THRESHOLD = -0.3  # < -0.3% = falling
# Between = flat

# Gold correlation expectations:
# DXY: inverse (gold up when DXY down)
# Yields: inverse (gold up when yields down)
# VIX: positive (gold up when VIX up — risk-off)
CORRELATIONS = {
    'DXY': 'inverse',       # BUY gold aligned with DXY falling
    'Yields_10Y': 'inverse', # BUY gold aligned with yields falling
    'VIX': 'positive',       # BUY gold aligned with VIX rising
}


def download_macro_data(start: str = '2024-07-01', end: str = '2026-03-01') -> pd.DataFrame:
    """Download daily data from Yahoo Finance for DXY, 10Y Yields, VIX."""
    try:
        import yfinance as yf
    except ImportError:
        print("ERROR: yfinance not installed. Run: pip install yfinance")
        sys.exit(1)

    all_data = {}
    for name, ticker in TICKERS.items():
        print(f"  Downloading {name} ({ticker})...")
        try:
            df = yf.download(ticker, start=start, end=end, progress=False)
            if len(df) == 0:
                print(f"    WARNING: No data for {ticker}")
                continue
            # Use Close price
            series = df['Close'].squeeze()
            if isinstance(series, pd.DataFrame):
                series = series.iloc[:, 0]
            all_data[name] = series
            print(f"    Got {len(series)} daily bars")
        except Exception as e:
            print(f"    ERROR downloading {ticker}: {e}")

    if not all_data:
        print("ERROR: No macro data downloaded")
        sys.exit(1)

    # Combine into single DataFrame
    macro = pd.DataFrame(all_data)
    macro.index = pd.to_datetime(macro.index)
    macro.index = macro.index.tz_localize(None)  # Remove timezone if present

    # Compute 5-day change %
    for name in all_data.keys():
        macro[f'{name}_chg5d'] = macro[name].pct_change(5) * 100

        # Direction classification
        def classify(chg):
            if pd.isna(chg):
                return 'unknown'
            if chg > RISING_THRESHOLD:
                return 'rising'
            elif chg < FALLING_THRESHOLD:
                return 'falling'
            else:
                return 'flat'

        macro[f'{name}_dir'] = macro[f'{name}_chg5d'].apply(classify)

    return macro


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADES_CSV)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['entry_date'] = df['entry_time'].dt.date
    df['win'] = df['profit_pips'] > 0
    return df


def merge_trades_macro(trades: pd.DataFrame, macro: pd.DataFrame) -> pd.DataFrame:
    """Join trades with macro data by entry date (use most recent available)."""
    trades = trades.copy()
    macro = macro.copy()
    macro['date'] = macro.index.date

    # For each trade, find the most recent macro data <= entry_date
    merged_rows = []
    for _, trade in trades.iterrows():
        entry_date = trade['entry_date']
        # Find most recent macro row
        mask = macro['date'] <= entry_date
        if mask.any():
            macro_row = macro[mask].iloc[-1]
            row = trade.to_dict()
            for col in macro.columns:
                if col != 'date':
                    row[f'macro_{col}'] = macro_row[col]
            merged_rows.append(row)
        else:
            row = trade.to_dict()
            merged_rows.append(row)

    return pd.DataFrame(merged_rows)


def get_alignment(direction: str, macro_dir: str, correlation: str) -> str:
    """Determine if trade direction aligns with macro direction."""
    if macro_dir == 'unknown' or macro_dir == 'flat':
        return 'flat'

    if correlation == 'inverse':
        # BUY gold + macro falling = aligned
        # BUY gold + macro rising = conflict
        if direction == 'BUY':
            return 'aligned' if macro_dir == 'falling' else 'conflict'
        else:  # SELL
            return 'aligned' if macro_dir == 'rising' else 'conflict'
    else:  # positive
        # BUY gold + macro rising = aligned
        # BUY gold + macro falling = conflict
        if direction == 'BUY':
            return 'aligned' if macro_dir == 'rising' else 'conflict'
        else:  # SELL
            return 'aligned' if macro_dir == 'falling' else 'conflict'


# ============================================================================
# ANALYSIS
# ============================================================================

def analyze_correlation(df: pd.DataFrame, macro_name: str) -> None:
    dir_col = f'macro_{macro_name}_dir'
    chg_col = f'macro_{macro_name}_chg5d'
    corr_type = CORRELATIONS[macro_name]

    if dir_col not in df.columns:
        print(f"  {macro_name}: no data available")
        return

    # Filter out unknown
    valid = df[df[dir_col] != 'unknown'].copy()
    if len(valid) == 0:
        print(f"  {macro_name}: no valid data")
        return

    # Compute alignment
    valid['alignment'] = valid.apply(
        lambda r: get_alignment(r['direction'], r[dir_col], corr_type), axis=1)

    print(f"\n  {macro_name} (correlation: {corr_type} with gold)")
    print(f"  {'-' * 80}")

    # Direction distribution
    for direction in ['BUY', 'SELL']:
        d_df = valid[valid['direction'] == direction]
        if len(d_df) == 0:
            continue

        print(f"\n    {direction} gold trades ({len(d_df)}):")
        print(f"    {'Macro Dir':>12}  {'Alignment':>10}  {'Trades':>6}  {'Wins':>5}  {'WR%':>6}  {'P&L $':>10}")
        print(f"    {'-' * 12}  {'-' * 10}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}")

        for macro_dir in ['rising', 'flat', 'falling']:
            m_df = d_df[d_df[dir_col] == macro_dir]
            n = len(m_df)
            if n == 0:
                continue
            wins = m_df['win'].sum()
            wr = wins / n * 100
            pnl = m_df['profit_usd'].sum()
            alignment = get_alignment(direction, macro_dir, corr_type)
            print(f"    {macro_dir:>12}  {alignment:>10}  {n:>6}  {wins:>5.0f}  {wr:>5.1f}%  ${pnl:>+9.2f}")

    # Summary: aligned vs conflict vs flat
    print(f"\n    Summary:")
    print(f"    {'Alignment':>12}  {'Trades':>6}  {'Wins':>5}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}")
    print(f"    {'-' * 12}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}  {'-' * 6}")

    for alignment in ['aligned', 'flat', 'conflict']:
        a_df = valid[valid['alignment'] == alignment]
        n = len(a_df)
        if n == 0:
            continue
        wins = a_df['win'].sum()
        wr = wins / n * 100
        pnl = a_df['profit_usd'].sum()
        w = a_df[a_df['win']]
        l = a_df[~a_df['win']]
        gw = w['profit_usd'].sum() if len(w) > 0 else 0
        gl = abs(l['profit_usd'].sum()) if len(l) > 0 else 0
        pf = gw / gl if gl > 0 else float('inf')
        print(f"    {alignment:>12}  {n:>6}  {wins:>5.0f}  {wr:>5.1f}%  ${pnl:>+9.2f}  {pf:>6.2f}")

    # WR gap
    aligned = valid[valid['alignment'] == 'aligned']
    conflict = valid[valid['alignment'] == 'conflict']
    if len(aligned) >= 10 and len(conflict) >= 10:
        wr_a = aligned['win'].mean() * 100
        wr_c = conflict['win'].mean() * 100
        gap = wr_a - wr_c
        print(f"\n    WR gap (aligned - conflict): {gap:+.1f}pp")
        if abs(gap) > 5:
            print(f"    --> Meaningful gap")
        else:
            print(f"    --> Small gap, not actionable")


def print_overall_summary(df: pd.DataFrame) -> None:
    print(f"\n{'=' * 100}")
    print(f"  OVERALL SUMMARY: MACRO ALIGNMENT vs WIN RATE")
    print(f"{'=' * 100}")

    results = []
    for macro_name in CORRELATIONS.keys():
        dir_col = f'macro_{macro_name}_dir'
        if dir_col not in df.columns:
            continue

        valid = df[df[dir_col] != 'unknown'].copy()
        if len(valid) == 0:
            continue

        corr_type = CORRELATIONS[macro_name]
        valid['alignment'] = valid.apply(
            lambda r: get_alignment(r['direction'], r[dir_col], corr_type), axis=1)

        aligned = valid[valid['alignment'] == 'aligned']
        conflict = valid[valid['alignment'] == 'conflict']

        if len(aligned) >= 5 and len(conflict) >= 5:
            wr_a = aligned['win'].mean() * 100
            wr_c = conflict['win'].mean() * 100
            pnl_a = aligned['profit_usd'].sum()
            pnl_c = conflict['profit_usd'].sum()
            results.append({
                'macro': macro_name, 'n_aligned': len(aligned), 'n_conflict': len(conflict),
                'wr_aligned': wr_a, 'wr_conflict': wr_c, 'gap': wr_a - wr_c,
                'pnl_aligned': pnl_a, 'pnl_conflict': pnl_c,
            })

    if results:
        print(f"\n  {'Macro':>12}  {'Aligned':>10}  {'Conflict':>10}  {'WR Aligned':>11}  {'WR Conflict':>12}  {'Gap':>8}  {'P&L Aligned':>12}  {'P&L Conflict':>13}")
        print(f"  {'-' * 12}  {'-' * 10}  {'-' * 10}  {'-' * 11}  {'-' * 12}  {'-' * 8}  {'-' * 12}  {'-' * 13}")

        for r in results:
            print(f"  {r['macro']:>12}  {r['n_aligned']:>10}  {r['n_conflict']:>10}  {r['wr_aligned']:>10.1f}%  {r['wr_conflict']:>11.1f}%  {r['gap']:>+7.1f}pp  ${r['pnl_aligned']:>+11.2f}  ${r['pnl_conflict']:>+12.2f}")

        # Any macro with conflict P&L negative?
        print()
        any_actionable = False
        for r in results:
            if r['pnl_conflict'] < 0:
                print(f"  !! {r['macro']}: Conflict trades are NET NEGATIVE (${r['pnl_conflict']:+.2f})")
                any_actionable = True
            if abs(r['gap']) > 10:
                print(f"  !! {r['macro']}: WR gap > 10pp ({r['gap']:+.1f}pp) - potentially actionable")
                any_actionable = True

        if not any_actionable:
            print(f"  All macro alignments profitable. No conflict scenario is net negative.")
            print(f"  Blocking conflicting trades would remove profit, not add it.")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 100)
    print("  MULTI-ASSET CORRELATION ANALYSIS")
    print("=" * 100)

    # Download macro data
    print("\nDownloading macro data from Yahoo Finance...")
    macro = download_macro_data(start='2024-07-01', end='2026-03-01')
    print(f"\nMacro data: {len(macro)} daily bars")
    for name in TICKERS.keys():
        if name in macro.columns:
            valid = macro[name].dropna()
            print(f"  {name}: {len(valid)} bars, range {valid.min():.2f} - {valid.max():.2f}")

    # Load trades
    print("\nLoading trades...")
    trades = load_trades()
    print(f"  {len(trades)} trades")

    # Merge
    print("\nMerging trades with macro data...")
    merged = merge_trades_macro(trades, macro)

    # Check coverage
    for name in TICKERS.keys():
        dir_col = f'macro_{name}_dir'
        if dir_col in merged.columns:
            valid = merged[merged[dir_col] != 'unknown']
            print(f"  {name}: {len(valid)}/{len(merged)} trades matched")

    # Analysis per macro indicator
    for macro_name in CORRELATIONS.keys():
        print(f"\n{'=' * 100}")
        print(f"  {macro_name} CORRELATION ANALYSIS")
        print(f"{'=' * 100}")
        analyze_correlation(merged, macro_name)

    # Overall summary
    print_overall_summary(merged)

    # Verdict
    print(f"{'=' * 100}")
    print(f"  VERDICT")
    print(f"{'=' * 100}")
    print(f"  The backtest ran with NEUTRAL macro data (DXY=104, Yields=4.5, VIX=17, all static).")
    print(f"  This analysis overlays ACTUAL macro conditions at each trade's entry time.")
    print(f"  If correlation filtering would help, we'd see conflicting trades with lower WR/negative P&L.")
    print()


if __name__ == "__main__":
    main()
