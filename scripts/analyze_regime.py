"""
Explicit Regime Detection - Data Analysis
==========================================
Classifies each backtest trade's market regime at entry time
using ADX, EMA50, and ATR computed from H1 OHLC data.

Regimes:
  - Trending: ADX > 25 AND price clearly above/below EMA50
  - Ranging: ADX < 20 OR price oscillating around EMA50
  - Volatile: ATR > 1.5x its 20-period average
  (Regimes can overlap)

Usage:
    python scripts/analyze_regime.py
"""

import os
import sys
import pandas as pd
import numpy as np

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRADES_CSV = os.path.join(ROOT_DIR, "data", "backtest_trades_20260218_2147.csv")
H1_CSV = os.path.join(ROOT_DIR, "data", "XAUUSD_H1_with_indicators.csv")
H1_RAW_CSV = os.path.join(ROOT_DIR, "data", "XAUUSD_H1.csv")

PIP_SIZE = 0.1


# ============================================================================
# INDICATOR COMPUTATION
# ============================================================================

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ATR from OHLC data."""
    high = df['high']
    low = df['low']
    close = df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Compute ADX from OHLC data."""
    high = df['high']
    low = df['low']
    close = df['close']

    plus_dm = high.diff()
    minus_dm = -low.diff()

    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)

    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(alpha=1/period, min_periods=period, adjust=False).mean() / atr)

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return adx


def compute_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


# ============================================================================
# DATA LOADING
# ============================================================================

def load_h1_with_indicators() -> pd.DataFrame:
    """Load H1 data and compute ADX, ATR, EMA50."""
    # Use raw H1 for OHLC (same rows as indicators file)
    df = pd.read_csv(H1_RAW_CSV)
    df['datetime'] = pd.to_datetime(df['datetime'])

    # Also load pre-computed EMA50
    df_ind = pd.read_csv(H1_CSV)
    df_ind['datetime'] = pd.to_datetime(df_ind['datetime'])
    df['ema_50'] = df_ind['ema_50']

    # Compute ATR (14-period)
    df['atr_14'] = compute_atr(df, 14)
    # ATR in pips
    df['atr_pips'] = df['atr_14'] / PIP_SIZE

    # ATR 20-period average (for volatility detection)
    df['atr_avg_20'] = df['atr_14'].rolling(window=20, min_periods=20).mean()
    df['atr_ratio'] = df['atr_14'] / df['atr_avg_20']

    # Compute ADX (14-period)
    df['adx_14'] = compute_adx(df, 14)

    # Price distance from EMA50 (in pips)
    df['dist_ema50_pips'] = (df['close'] - df['ema_50']) / PIP_SIZE

    # Regime classification
    # Trending: ADX > 25 AND |dist_ema50| > 50 pips (clear directional move)
    df['is_trending'] = (df['adx_14'] > 25) & (df['dist_ema50_pips'].abs() > 50)

    # Ranging: ADX < 20 OR |dist_ema50| < 30 pips
    df['is_ranging'] = (df['adx_14'] < 20) | (df['dist_ema50_pips'].abs() < 30)

    # Volatile: ATR > 1.5x its 20-period average
    df['is_volatile'] = df['atr_ratio'] > 1.5

    # Combined regime label
    def regime_label(row):
        labels = []
        if row['is_trending']:
            labels.append('Trending')
        if row['is_ranging']:
            labels.append('Ranging')
        if row['is_volatile']:
            labels.append('Volatile')
        if not labels:
            return 'Normal'
        return '+'.join(labels)

    df['regime'] = df.apply(regime_label, axis=1)

    # Simplified primary regime (non-overlapping)
    def primary_regime(row):
        if row['is_volatile'] and row['is_trending']:
            return 'Volatile-Trend'
        elif row['is_volatile']:
            return 'Volatile'
        elif row['is_trending']:
            return 'Trending'
        elif row['is_ranging']:
            return 'Ranging'
        else:
            return 'Normal'

    df['primary_regime'] = df.apply(primary_regime, axis=1)

    return df


def load_trades() -> pd.DataFrame:
    df = pd.read_csv(TRADES_CSV)
    df['entry_time'] = pd.to_datetime(df['entry_time'])
    df['close_time'] = pd.to_datetime(df['close_time'])
    df['win'] = df['profit_pips'] > 0
    df['sl_pips'] = (abs(df['entry_price'] - df['sl']) / PIP_SIZE).round(1)
    df['tp_pips'] = (abs(df['tp'] - df['entry_price']) / PIP_SIZE).round(1)
    return df


def merge_trades_with_regime(trades: pd.DataFrame, h1: pd.DataFrame) -> pd.DataFrame:
    """Join each trade with the H1 candle at its entry time."""
    # Round entry_time to nearest hour for matching
    trades = trades.copy()
    trades['entry_hour'] = trades['entry_time'].dt.floor('h')

    # Merge on datetime
    h1_cols = ['datetime', 'adx_14', 'atr_pips', 'atr_ratio', 'dist_ema50_pips',
               'is_trending', 'is_ranging', 'is_volatile', 'regime', 'primary_regime']
    merged = trades.merge(h1[h1_cols], left_on='entry_hour', right_on='datetime', how='left')

    # Report unmatched
    unmatched = merged['adx_14'].isna().sum()
    if unmatched > 0:
        print(f"  WARNING: {unmatched} trades could not be matched to H1 data (likely in date gap)")

    return merged


# ============================================================================
# ANALYSIS
# ============================================================================

def calc_stats(subset: pd.DataFrame) -> dict:
    n = len(subset)
    if n == 0:
        return {'trades': 0, 'wins': 0, 'wr': 0, 'pnl': 0, 'pf': 0, 'avg_conf': 0}
    wins = subset[subset['win']]
    losses = subset[~subset['win']]
    gw = wins['profit_usd'].sum() if len(wins) > 0 else 0
    gl = abs(losses['profit_usd'].sum()) if len(losses) > 0 else 0
    pf = gw / gl if gl > 0 else float('inf')
    return {
        'trades': n, 'wins': len(wins),
        'wr': len(wins) / n * 100,
        'pnl': subset['profit_usd'].sum(),
        'pf': pf,
        'avg_conf': subset['confidence'].mean(),
    }


def analyze_by_primary_regime(df: pd.DataFrame) -> None:
    matched = df.dropna(subset=['primary_regime'])

    print(f"\n{'=' * 110}")
    print(f"  PERFORMANCE BY PRIMARY REGIME ({len(matched)} matched trades)")
    print(f"{'=' * 110}")
    print(f"  {'Regime':>18}  {'Trades':>6}  {'%':>5}  {'Wins':>5}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}  {'Avg Conf':>9}  {'Avg ADX':>8}  {'Avg ATR':>8}")
    print(f"  {'-' * 18}  {'-' * 6}  {'-' * 5}  {'-' * 5}  {'-' * 6}  {'-' * 10}  {'-' * 6}  {'-' * 9}  {'-' * 8}  {'-' * 8}")

    regimes = ['Trending', 'Ranging', 'Normal', 'Volatile', 'Volatile-Trend']
    for regime in regimes:
        r_df = matched[matched['primary_regime'] == regime]
        n = len(r_df)
        if n == 0:
            continue
        s = calc_stats(r_df)
        avg_adx = r_df['adx_14'].mean()
        avg_atr = r_df['atr_pips'].mean()
        pct = n / len(matched) * 100
        print(f"  {regime:>18}  {n:>6}  {pct:>4.1f}%  {s['wins']:>5}  {s['wr']:>5.1f}%  ${s['pnl']:>+9.2f}  {s['pf']:>6.2f}  {s['avg_conf']:>8.1f}  {avg_adx:>7.1f}  {avg_atr:>7.0f}p")

    # Overall
    s = calc_stats(matched)
    print(f"  {'ALL':>18}  {len(matched):>6}  100.0%  {s['wins']:>5}  {s['wr']:>5.1f}%  ${s['pnl']:>+9.2f}  {s['pf']:>6.2f}  {s['avg_conf']:>8.1f}  {matched['adx_14'].mean():>7.1f}  {matched['atr_pips'].mean():>7.0f}p")
    print()


def analyze_overlapping_regimes(df: pd.DataFrame) -> None:
    matched = df.dropna(subset=['is_trending'])

    print(f"\n{'=' * 110}")
    print(f"  OVERLAPPING REGIME FLAGS")
    print(f"{'=' * 110}")

    flags = [
        ('Trending', 'is_trending'),
        ('Ranging', 'is_ranging'),
        ('Volatile', 'is_volatile'),
    ]

    print(f"  {'Flag':>12}  {'Trades':>6}  {'%':>5}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}  {'Avg Conf':>9}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}  {'-' * 6}  {'-' * 9}")

    for label, col in flags:
        for val, suffix in [(True, 'YES'), (False, 'NO')]:
            r_df = matched[matched[col] == val]
            n = len(r_df)
            if n == 0:
                continue
            s = calc_stats(r_df)
            pct = n / len(matched) * 100
            print(f"  {label + '=' + suffix:>12}  {n:>6}  {pct:>4.1f}%  {s['wr']:>5.1f}%  ${s['pnl']:>+9.2f}  {s['pf']:>6.2f}  {s['avg_conf']:>8.1f}")
        print()


def analyze_scenario_by_regime(df: pd.DataFrame) -> None:
    matched = df.dropna(subset=['primary_regime'])

    print(f"\n{'=' * 110}")
    print(f"  SCENARIO DISTRIBUTION BY REGIME")
    print(f"{'=' * 110}")

    regimes = ['Trending', 'Ranging', 'Normal', 'Volatile', 'Volatile-Trend']
    for regime in regimes:
        r_df = matched[matched['primary_regime'] == regime]
        n = len(r_df)
        if n < 10:
            continue

        print(f"\n  {regime} ({n} trades):")
        scenarios = r_df['scenario'].value_counts()
        print(f"  {'Scenario':>30}  {'Count':>6}  {'%':>6}  {'WR%':>6}  {'P&L $':>10}")
        print(f"  {'-' * 30}  {'-' * 6}  {'-' * 6}  {'-' * 6}  {'-' * 10}")

        for scenario, count in scenarios.head(8).items():
            s_df = r_df[r_df['scenario'] == scenario]
            wr = s_df['win'].mean() * 100
            pnl = s_df['profit_usd'].sum()
            pct = count / n * 100
            print(f"  {scenario:>30}  {count:>6}  {pct:>5.1f}%  {wr:>5.1f}%  ${pnl:>+9.2f}")

    print()


def analyze_adx_buckets(df: pd.DataFrame) -> None:
    matched = df.dropna(subset=['adx_14'])

    print(f"\n{'=' * 110}")
    print(f"  PERFORMANCE BY ADX BUCKET")
    print(f"{'=' * 110}")
    print(f"  {'ADX Range':>12}  {'Trades':>6}  {'%':>5}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}  {'Avg ATR':>8}")
    print(f"  {'-' * 12}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}  {'-' * 6}  {'-' * 8}")

    buckets = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 40), (40, 100)]
    for lo, hi in buckets:
        b = matched[(matched['adx_14'] >= lo) & (matched['adx_14'] < hi)]
        n = len(b)
        if n == 0:
            continue
        s = calc_stats(b)
        avg_atr = b['atr_pips'].mean()
        pct = n / len(matched) * 100
        print(f"  {f'{lo}-{hi}':>12}  {n:>6}  {pct:>4.1f}%  {s['wr']:>5.1f}%  ${s['pnl']:>+9.2f}  {s['pf']:>6.2f}  {avg_atr:>7.0f}p")

    print()


def analyze_atr_ratio_buckets(df: pd.DataFrame) -> None:
    matched = df.dropna(subset=['atr_ratio'])

    print(f"\n{'=' * 110}")
    print(f"  PERFORMANCE BY VOLATILITY (ATR vs 20-period avg)")
    print(f"{'=' * 110}")
    print(f"  {'ATR Ratio':>12}  {'Label':>12}  {'Trades':>6}  {'%':>5}  {'WR%':>6}  {'P&L $':>10}  {'PF':>6}")
    print(f"  {'-' * 12}  {'-' * 12}  {'-' * 6}  {'-' * 5}  {'-' * 6}  {'-' * 10}  {'-' * 6}")

    buckets = [
        (0, 0.7, 'Very Calm'),
        (0.7, 0.9, 'Below Avg'),
        (0.9, 1.1, 'Normal'),
        (1.1, 1.5, 'Above Avg'),
        (1.5, 10, 'Volatile'),
    ]
    for lo, hi, label in buckets:
        b = matched[(matched['atr_ratio'] >= lo) & (matched['atr_ratio'] < hi)]
        n = len(b)
        if n == 0:
            continue
        s = calc_stats(b)
        pct = n / len(matched) * 100
        print(f"  {f'{lo}-{hi}x':>12}  {label:>12}  {n:>6}  {pct:>4.1f}%  {s['wr']:>5.1f}%  ${s['pnl']:>+9.2f}  {s['pf']:>6.2f}")

    print()


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not os.path.exists(TRADES_CSV) or not os.path.exists(H1_RAW_CSV):
        print("Required CSV files not found")
        return

    print("Loading H1 data and computing indicators...")
    h1 = load_h1_with_indicators()
    print(f"  H1 candles: {len(h1)}, date range: {h1['datetime'].iloc[0]} to {h1['datetime'].iloc[-1]}")
    print(f"  ADX range: {h1['adx_14'].min():.1f} - {h1['adx_14'].max():.1f}")
    print(f"  ATR range: {h1['atr_pips'].min():.0f} - {h1['atr_pips'].max():.0f} pips")

    print("\nLoading trades...")
    trades = load_trades()
    print(f"  Trades: {len(trades)}")

    print("\nMerging trades with H1 regime data...")
    merged = merge_trades_with_regime(trades, h1)
    matched = merged.dropna(subset=['adx_14'])
    print(f"  Matched: {len(matched)}/{len(trades)} trades")

    analyze_by_primary_regime(merged)
    analyze_overlapping_regimes(merged)
    analyze_adx_buckets(merged)
    analyze_atr_ratio_buckets(merged)
    analyze_scenario_by_regime(merged)

    # Verdict
    print(f"{'=' * 110}")
    print(f"  KEY FINDINGS")
    print(f"{'=' * 110}")

    # Find worst regime
    regimes = ['Trending', 'Ranging', 'Normal', 'Volatile', 'Volatile-Trend']
    worst_wr = 100
    worst_regime = None
    for regime in regimes:
        r_df = matched[matched['primary_regime'] == regime]
        if len(r_df) >= 10:
            wr = r_df['win'].mean() * 100
            pnl = r_df['profit_usd'].sum()
            if wr < worst_wr:
                worst_wr = wr
                worst_regime = regime
                worst_pnl = pnl
                worst_n = len(r_df)

    if worst_regime:
        print(f"  Worst regime: {worst_regime} (WR {worst_wr:.1f}%, {worst_n} trades, P&L ${worst_pnl:+.2f})")
        overall_wr = matched['win'].mean() * 100
        gap = overall_wr - worst_wr
        print(f"  Gap vs overall ({overall_wr:.1f}%): {gap:.1f}pp")

        if worst_pnl < 0:
            print(f"  --> {worst_regime} regime is NET NEGATIVE - candidate for blocking")
        elif gap > 10:
            print(f"  --> Significant WR gap but still profitable - blocking would remove profit")
        else:
            print(f"  --> No regime is significantly worse - no action needed")

    # Check if any regime is net negative
    any_negative = False
    for regime in regimes:
        r_df = matched[matched['primary_regime'] == regime]
        if len(r_df) >= 10 and r_df['profit_usd'].sum() < 0:
            any_negative = True
            print(f"  !! {regime}: NET NEGATIVE P&L ${r_df['profit_usd'].sum():+.2f}")

    if not any_negative:
        print(f"  All regimes with 10+ trades are net profitable.")

    print()


if __name__ == "__main__":
    main()
