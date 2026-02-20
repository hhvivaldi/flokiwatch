"""
Sanity Test - Validate scores in specific periods
"""

import pandas as pd

# Load data with scores
df = pd.read_csv('data/XAUUSD_H1_with_scores.csv', parse_dates=['datetime'])

print('=' * 70)
print('SANITY TEST - SCORES BY PERIOD')
print('=' * 70)

# Price change per month
df['month'] = df['datetime'].dt.to_period('M')
monthly = df.groupby('month').agg({
    'close': ['first', 'last'],
    'technical_score': 'mean'
}).round(2)
monthly.columns = ['open', 'close', 'avg_score']
monthly['variacao_pct'] = ((monthly['close'] - monthly['open']) / monthly['open'] * 100).round(2)

print('\nMONTHLY GOLD CHANGE (last 24 months):')
print('-' * 70)
for idx in monthly.tail(24).index:
    row = monthly.loc[idx]
    trend = '📈' if row['variacao_pct'] > 2 else ('📉' if row['variacao_pct'] < -2 else '➖')
    print(f"{idx} | Var: {row['variacao_pct']:+6.2f}% | Avg score: {row['avg_score']:5.1f} | {trend}")

# 1. BEARISH PERIOD - find month with largest drop
print('\n' + '=' * 70)
print('1. BEARISH PERIOD (largest drop)')
print('=' * 70)
worst_month = monthly['variacao_pct'].idxmin()
worst_data = monthly.loc[worst_month]
print(f"Month: {worst_month}")
print(f"Change: {worst_data['variacao_pct']:+.2f}%")
print(f"Avg score: {worst_data['avg_score']:.1f}")
result1 = "✅ OK" if worst_data['avg_score'] < 40 else "⚠️ CHECK"
print(f"Expected: < 40 | Result: {result1}")

# 2. STRONG BULLISH PERIOD - Oct/Nov/Dec 2025
print('\n' + '=' * 70)
print('2. STRONG BULLISH PERIOD (Oct-Dec 2025)')
print('=' * 70)
alta = df[(df['datetime'] >= '2025-10-01') & (df['datetime'] < '2026-01-01')]
if len(alta) > 0:
    score_alta = alta['technical_score'].mean()
    var_alta = (alta['close'].iloc[-1] - alta['close'].iloc[0]) / alta['close'].iloc[0] * 100
    print(f"Period: Oct-Dec 2025")
    print(f"Change: {var_alta:+.2f}%")
    print(f"Avg score: {score_alta:.1f}")
    result2 = "✅ OK" if score_alta > 60 else "⚠️ CHECK"
    print(f"Expected: > 60 | Result: {result2}")

# 3. SIDEWAYS PERIOD - find months with smallest change
print('\n' + '=' * 70)
print('3. SIDEWAYS PERIOD (smallest change)')
print('=' * 70)
monthly['var_abs'] = monthly['variacao_pct'].abs()
lateral_months = monthly[monthly['var_abs'] < 1.5]
if len(lateral_months) > 0:
    lateral_score = lateral_months['avg_score'].mean()
    print(f"Months with change < 1.5%: {len(lateral_months)}")
    for idx in lateral_months.index:
        row = lateral_months.loc[idx]
        print(f"  - {idx}: Var {row['variacao_pct']:+.2f}%, Score {row['avg_score']:.1f}")
    print(f"\nAvg score in those months: {lateral_score:.1f}")
    result3 = "✅ OK" if 40 < lateral_score < 60 else "⚠️ CHECK"
    print(f"Expected: ~50 | Result: {result3}")
else:
    most_lateral = monthly['var_abs'].idxmin()
    lat_data = monthly.loc[most_lateral]
    print(f"Most sideways month: {most_lateral}")
    print(f"Change: {lat_data['variacao_pct']:+.2f}%")
    print(f"Avg score: {lat_data['avg_score']:.1f}")
    result3 = "✅ OK" if 40 < lat_data['avg_score'] < 60 else "⚠️ CHECK"
    print(f"Expected: ~50 | Result: {result3}")

print('\n' + '=' * 70)
print('SANITY TEST SUMMARY')
print('=' * 70)
