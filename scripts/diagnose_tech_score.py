"""
Technical Score Diagnostic Analysis
Project: FlokiWatch XAUUSD Trading Bot

Analyzes the Technical Score distribution and indicator contributions
to understand why Tech Score stays suppressed during confirmed uptrends.

Key questions:
1. What are the RAW indicator contributions for WINNING BUY trades?
2. How does Tech Score compare between WINNING vs LOSING BUY trades?
3. What RSI values occur when Tech < 50 during confirmed uptrends?
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os

# Configuration
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
BACKTEST_FILE = "backtest_trades_20260218_2147.csv"
INDICATORS_FILE = "XAUUSD_H1_with_indicators.csv"
OUTPUT_FILE = "tech_score_diagnostic.txt"


def load_data():
    """Load backtest trades and indicator data."""
    trades_path = os.path.join(DATA_DIR, BACKTEST_FILE)
    indicators_path = os.path.join(DATA_DIR, INDICATORS_FILE)
    
    trades = pd.read_csv(trades_path)
    trades['entry_time'] = pd.to_datetime(trades['entry_time'])
    
    indicators = pd.read_csv(indicators_path)
    indicators['datetime'] = pd.to_datetime(indicators['datetime'])
    
    return trades, indicators


def merge_trades_with_indicators(trades, indicators):
    """Merge trades with indicator data at entry time."""
    # Round entry_time to nearest hour for matching
    trades['entry_hour'] = trades['entry_time'].dt.floor('h')
    
    # Merge on datetime
    merged = trades.merge(
        indicators,
        left_on='entry_hour',
        right_on='datetime',
        how='left',
        suffixes=('', '_ind')
    )
    
    return merged


def calculate_indicator_scores(row):
    """
    Calculate individual indicator score contributions.
    Based on technical_analyzer.py logic.
    """
    scores = {}
    
    # 1. Trend Score (EMAs) - 25 points max
    trend_score = 0
    if pd.notna(row.get('close')) and pd.notna(row.get('ema_9')):
        if row['close'] > row['ema_9']:
            trend_score += 5
    if pd.notna(row.get('close')) and pd.notna(row.get('ema_21')):
        if row['close'] > row['ema_21']:
            trend_score += 5
    if pd.notna(row.get('close')) and pd.notna(row.get('ema_50')):
        if row['close'] > row['ema_50']:
            trend_score += 5
    if pd.notna(row.get('ema_9')) and pd.notna(row.get('ema_21')):
        if row['ema_9'] > row['ema_21']:
            trend_score += 5
    if pd.notna(row.get('ema_21')) and pd.notna(row.get('ema_50')):
        if row['ema_21'] > row['ema_50']:
            trend_score += 5
    scores['trend_ema'] = trend_score
    
    # 2. Momentum Score (RSI) - 20 points max
    rsi = row.get('rsi_14')
    if pd.notna(rsi):
        if 40 <= rsi <= 60:
            momentum_score = 10  # Neutral
        elif 30 <= rsi < 40:
            momentum_score = 15  # Mild oversold (bullish)
        elif rsi < 30:
            momentum_score = 20  # Strong oversold (very bullish)
        elif 60 < rsi <= 70:
            momentum_score = 5   # Mild overbought (bearish)
        else:  # rsi > 70
            momentum_score = 0   # Strong overbought (very bearish)
    else:
        momentum_score = 10
    scores['rsi_momentum'] = momentum_score
    scores['rsi_raw'] = rsi if pd.notna(rsi) else None
    
    # 3. MACD Score - 20 points max
    macd_score = 0
    macd = row.get('macd')
    macd_signal = row.get('macd_signal')
    macd_hist = row.get('macd_hist')
    
    if pd.notna(macd) and pd.notna(macd_signal):
        if macd > macd_signal:
            macd_score += 10
        if pd.notna(macd_hist) and macd_hist > 0:
            macd_score += 5
        # Note: histogram growth bonus requires previous row, skip for simplicity
        macd_score += 5  # Assume average case for histogram growth
    else:
        macd_score = 10
    scores['macd'] = min(macd_score, 20)
    
    # 4. Bollinger Score - 15 points max
    bb_score = 0
    close = row.get('close')
    bb_upper = row.get('bb_upper')
    bb_lower = row.get('bb_lower')
    
    if pd.notna(close) and pd.notna(bb_upper) and pd.notna(bb_lower):
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            bb_position = (close - bb_lower) / bb_range
            if 0.3 <= bb_position <= 0.7:
                bb_score = 7   # Middle of band
            elif bb_position < 0.2:
                bb_score = 15  # Near lower band (bullish)
            elif bb_position > 0.8:
                bb_score = 0   # Near upper band (bearish)
            else:
                bb_score = 10
            scores['bb_position'] = bb_position
        else:
            bb_score = 7
            scores['bb_position'] = 0.5
    else:
        bb_score = 7
        scores['bb_position'] = None
    scores['bollinger'] = bb_score
    
    # 5. Stochastic Score - 10 points max (not in indicators file, estimate)
    scores['stochastic'] = 5  # Neutral estimate
    
    # 6. Price Action Score - 10 points max (requires previous row, estimate)
    scores['price_action'] = 5  # Neutral estimate
    
    # Total (without visual context)
    scores['calculated_total'] = (
        scores['trend_ema'] + 
        scores['rsi_momentum'] + 
        scores['macd'] + 
        scores['bollinger'] + 
        scores['stochastic'] + 
        scores['price_action']
    )
    
    return scores


def analyze_winning_buys(merged):
    """Analyze indicator contributions for winning BUY trades."""
    winning_buys = merged[(merged['direction'] == 'BUY') & (merged['profit_pips'] > 0)]
    
    results = {
        'count': len(winning_buys),
        'avg_tech_score': winning_buys['tech_score'].mean(),
        'median_tech_score': winning_buys['tech_score'].median(),
        'std_tech_score': winning_buys['tech_score'].std(),
        'tech_above_65': (winning_buys['tech_score'] >= 65).sum(),
        'tech_55_to_65': ((winning_buys['tech_score'] >= 55) & (winning_buys['tech_score'] < 65)).sum(),
        'tech_below_55': (winning_buys['tech_score'] < 55).sum(),
        'avg_profit_pips': winning_buys['profit_pips'].mean(),
    }
    
    # Calculate indicator scores for each winning BUY
    indicator_scores = []
    for _, row in winning_buys.iterrows():
        scores = calculate_indicator_scores(row)
        indicator_scores.append(scores)
    
    if indicator_scores:
        scores_df = pd.DataFrame(indicator_scores)
        results['avg_trend_ema'] = scores_df['trend_ema'].mean()
        results['avg_rsi_momentum'] = scores_df['rsi_momentum'].mean()
        results['avg_macd'] = scores_df['macd'].mean()
        results['avg_bollinger'] = scores_df['bollinger'].mean()
        results['avg_stochastic'] = scores_df['stochastic'].mean()
        results['avg_price_action'] = scores_df['price_action'].mean()
        
        # RSI breakdown
        rsi_values = scores_df['rsi_raw'].dropna()
        results['rsi_above_70'] = (rsi_values > 70).sum()
        results['rsi_60_to_70'] = ((rsi_values >= 60) & (rsi_values <= 70)).sum()
        results['rsi_40_to_60'] = ((rsi_values >= 40) & (rsi_values < 60)).sum()
        results['rsi_below_40'] = (rsi_values < 40).sum()
        results['avg_rsi_raw'] = rsi_values.mean()
        
        # Bollinger breakdown
        bb_positions = scores_df['bb_position'].dropna()
        results['bb_above_80'] = (bb_positions > 0.8).sum()
        results['bb_70_to_80'] = ((bb_positions >= 0.7) & (bb_positions <= 0.8)).sum()
        results['bb_30_to_70'] = ((bb_positions >= 0.3) & (bb_positions < 0.7)).sum()
        results['bb_below_30'] = (bb_positions < 0.3).sum()
        results['avg_bb_position'] = bb_positions.mean()
    
    return results


def analyze_losing_buys(merged):
    """Analyze indicator contributions for losing BUY trades."""
    losing_buys = merged[(merged['direction'] == 'BUY') & (merged['profit_pips'] <= 0)]
    
    results = {
        'count': len(losing_buys),
        'avg_tech_score': losing_buys['tech_score'].mean(),
        'median_tech_score': losing_buys['tech_score'].median(),
        'std_tech_score': losing_buys['tech_score'].std(),
        'tech_above_65': (losing_buys['tech_score'] >= 65).sum(),
        'tech_55_to_65': ((losing_buys['tech_score'] >= 55) & (losing_buys['tech_score'] < 65)).sum(),
        'tech_below_55': (losing_buys['tech_score'] < 55).sum(),
        'avg_loss_pips': losing_buys['profit_pips'].mean(),
    }
    
    # Calculate indicator scores for each losing BUY
    indicator_scores = []
    for _, row in losing_buys.iterrows():
        scores = calculate_indicator_scores(row)
        indicator_scores.append(scores)
    
    if indicator_scores:
        scores_df = pd.DataFrame(indicator_scores)
        results['avg_trend_ema'] = scores_df['trend_ema'].mean()
        results['avg_rsi_momentum'] = scores_df['rsi_momentum'].mean()
        results['avg_macd'] = scores_df['macd'].mean()
        results['avg_bollinger'] = scores_df['bollinger'].mean()
        results['avg_stochastic'] = scores_df['stochastic'].mean()
        results['avg_price_action'] = scores_df['price_action'].mean()
        
        # RSI breakdown
        rsi_values = scores_df['rsi_raw'].dropna()
        results['rsi_above_70'] = (rsi_values > 70).sum()
        results['rsi_60_to_70'] = ((rsi_values >= 60) & (rsi_values <= 70)).sum()
        results['rsi_40_to_60'] = ((rsi_values >= 40) & (rsi_values < 60)).sum()
        results['rsi_below_40'] = (rsi_values < 40).sum()
        results['avg_rsi_raw'] = rsi_values.mean()
        
        # Bollinger breakdown
        bb_positions = scores_df['bb_position'].dropna()
        results['bb_above_80'] = (bb_positions > 0.8).sum()
        results['bb_70_to_80'] = ((bb_positions >= 0.7) & (bb_positions <= 0.8)).sum()
        results['bb_30_to_70'] = ((bb_positions >= 0.3) & (bb_positions < 0.7)).sum()
        results['bb_below_30'] = (bb_positions < 0.3).sum()
        results['avg_bb_position'] = bb_positions.mean()
    
    return results


def analyze_suppressed_tech_uptrends(merged):
    """
    Analyze cases where Tech < 55 but price moved up significantly.
    These are the "missed opportunities" due to tech score suppression.
    """
    # Filter: BUY trades with Tech < 55 but max_favorable_pips > 50
    suppressed = merged[
        (merged['direction'] == 'BUY') & 
        (merged['tech_score'] < 55) & 
        (merged['max_favorable_pips'] > 50)
    ]
    
    results = {
        'count': len(suppressed),
        'avg_tech_score': suppressed['tech_score'].mean() if len(suppressed) > 0 else 0,
        'avg_max_favorable': suppressed['max_favorable_pips'].mean() if len(suppressed) > 0 else 0,
        'avg_profit': suppressed['profit_pips'].mean() if len(suppressed) > 0 else 0,
    }
    
    # Calculate indicator scores
    indicator_scores = []
    for _, row in suppressed.iterrows():
        scores = calculate_indicator_scores(row)
        indicator_scores.append(scores)
    
    if indicator_scores:
        scores_df = pd.DataFrame(indicator_scores)
        results['avg_trend_ema'] = scores_df['trend_ema'].mean()
        results['avg_rsi_momentum'] = scores_df['rsi_momentum'].mean()
        results['avg_macd'] = scores_df['macd'].mean()
        results['avg_bollinger'] = scores_df['bollinger'].mean()
        
        # RSI breakdown for suppressed cases
        rsi_values = scores_df['rsi_raw'].dropna()
        results['rsi_above_70'] = (rsi_values > 70).sum()
        results['rsi_60_to_70'] = ((rsi_values >= 60) & (rsi_values <= 70)).sum()
        results['avg_rsi_raw'] = rsi_values.mean()
        
        # Bollinger breakdown
        bb_positions = scores_df['bb_position'].dropna()
        results['bb_above_80'] = (bb_positions > 0.8).sum()
        results['bb_70_to_80'] = ((bb_positions >= 0.7) & (bb_positions <= 0.8)).sum()
        results['avg_bb_position'] = bb_positions.mean()
    
    return results


def analyze_rsi_during_low_tech_uptrend(merged):
    """
    Analyze RSI values when Tech < 50 during confirmed uptrends.
    """
    # Filter: BUY trades with Tech < 50 that were profitable
    low_tech_winners = merged[
        (merged['direction'] == 'BUY') & 
        (merged['tech_score'] < 50) & 
        (merged['profit_pips'] > 0)
    ]
    
    results = {
        'count': len(low_tech_winners),
        'avg_tech_score': low_tech_winners['tech_score'].mean() if len(low_tech_winners) > 0 else 0,
        'avg_profit': low_tech_winners['profit_pips'].mean() if len(low_tech_winners) > 0 else 0,
    }
    
    # Get RSI values
    rsi_values = []
    for _, row in low_tech_winners.iterrows():
        rsi = row.get('rsi_14')
        if pd.notna(rsi):
            rsi_values.append(rsi)
    
    if rsi_values:
        rsi_series = pd.Series(rsi_values)
        results['avg_rsi'] = rsi_series.mean()
        results['rsi_above_70'] = (rsi_series > 70).sum()
        results['rsi_60_to_70'] = ((rsi_series >= 60) & (rsi_series <= 70)).sum()
        results['rsi_40_to_60'] = ((rsi_series >= 40) & (rsi_series < 60)).sum()
        results['rsi_below_40'] = (rsi_series < 40).sum()
        results['pct_rsi_above_70'] = (rsi_series > 70).sum() / len(rsi_series) * 100
    
    return results


def format_report(winning, losing, suppressed, rsi_analysis):
    """Format the diagnostic report."""
    lines = []
    lines.append("=" * 80)
    lines.append("TECHNICAL SCORE DIAGNOSTIC ANALYSIS")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    
    # Section 1: Winning vs Losing BUY Comparison
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 1: WINNING vs LOSING BUY TRADES - TECH SCORE COMPARISON")
    lines.append("=" * 80)
    
    lines.append(f"\n{'Metric':<35} {'Winning BUYs':>15} {'Losing BUYs':>15} {'Delta':>10}")
    lines.append("-" * 75)
    
    lines.append(f"{'Count':<35} {winning['count']:>15} {losing['count']:>15}")
    
    avg_diff = winning['avg_tech_score'] - losing['avg_tech_score']
    lines.append(f"{'Avg Tech Score':<35} {winning['avg_tech_score']:>15.2f} {losing['avg_tech_score']:>15.2f} {avg_diff:>+10.2f}")
    
    med_diff = winning['median_tech_score'] - losing['median_tech_score']
    lines.append(f"{'Median Tech Score':<35} {winning['median_tech_score']:>15.2f} {losing['median_tech_score']:>15.2f} {med_diff:>+10.2f}")
    
    lines.append(f"{'Std Dev Tech Score':<35} {winning['std_tech_score']:>15.2f} {losing['std_tech_score']:>15.2f}")
    
    lines.append(f"\n{'Tech Score Distribution:':<35}")
    lines.append(f"{'  >= 65':<35} {winning['tech_above_65']:>15} {losing['tech_above_65']:>15}")
    lines.append(f"{'  55-64':<35} {winning['tech_55_to_65']:>15} {losing['tech_55_to_65']:>15}")
    lines.append(f"{'  < 55':<35} {winning['tech_below_55']:>15} {losing['tech_below_55']:>15}")
    
    lines.append(f"\n{'Avg Profit/Loss (pips)':<35} {winning['avg_profit_pips']:>15.1f} {losing['avg_loss_pips']:>15.1f}")
    
    # Predictive value assessment
    lines.append("\n" + "-" * 75)
    if abs(avg_diff) < 2:
        lines.append("CONCLUSION: Tech Score has MINIMAL predictive value for BUY trades")
        lines.append(f"            (difference of only {avg_diff:+.2f} points)")
    elif avg_diff > 0:
        lines.append(f"CONCLUSION: Tech Score has SOME predictive value for BUY trades")
        lines.append(f"            (winning BUYs average {avg_diff:+.2f} points higher)")
    else:
        lines.append(f"CONCLUSION: Tech Score is INVERSELY correlated with BUY success!")
        lines.append(f"            (winning BUYs average {avg_diff:.2f} points LOWER)")
    
    # Section 2: Raw Indicator Contributions
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 2: RAW INDICATOR CONTRIBUTIONS (Average Points)")
    lines.append("=" * 80)
    
    lines.append(f"\n{'Indicator':<25} {'Max Pts':>10} {'Win BUY':>12} {'Lose BUY':>12} {'Delta':>10}")
    lines.append("-" * 70)
    
    indicators = [
        ('Trend (EMAs)', 25, 'avg_trend_ema'),
        ('RSI Momentum', 20, 'avg_rsi_momentum'),
        ('MACD', 20, 'avg_macd'),
        ('Bollinger', 15, 'avg_bollinger'),
        ('Stochastic (est)', 10, 'avg_stochastic'),
        ('Price Action (est)', 10, 'avg_price_action'),
    ]
    
    for name, max_pts, key in indicators:
        win_val = winning.get(key, 0)
        lose_val = losing.get(key, 0)
        delta = win_val - lose_val
        lines.append(f"{name:<25} {max_pts:>10} {win_val:>12.2f} {lose_val:>12.2f} {delta:>+10.2f}")
    
    # Section 3: RSI Breakdown
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 3: RSI BREAKDOWN AT ENTRY")
    lines.append("=" * 80)
    
    lines.append(f"\n{'RSI Range':<25} {'Win BUY':>12} {'Lose BUY':>12} {'Score Given':>15}")
    lines.append("-" * 65)
    lines.append(f"{'> 70 (overbought)':<25} {winning.get('rsi_above_70', 0):>12} {losing.get('rsi_above_70', 0):>12} {'0 pts':>15}")
    lines.append(f"{'60-70':<25} {winning.get('rsi_60_to_70', 0):>12} {losing.get('rsi_60_to_70', 0):>12} {'5 pts':>15}")
    lines.append(f"{'40-60 (neutral)':<25} {winning.get('rsi_40_to_60', 0):>12} {losing.get('rsi_40_to_60', 0):>12} {'10 pts':>15}")
    lines.append(f"{'< 40 (oversold)':<25} {winning.get('rsi_below_40', 0):>12} {losing.get('rsi_below_40', 0):>12} {'15-20 pts':>15}")
    lines.append(f"\n{'Avg RSI Value':<25} {winning.get('avg_rsi_raw', 0):>12.1f} {losing.get('avg_rsi_raw', 0):>12.1f}")
    
    # Section 4: Bollinger Breakdown
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 4: BOLLINGER POSITION BREAKDOWN AT ENTRY")
    lines.append("=" * 80)
    
    lines.append(f"\n{'BB Position':<25} {'Win BUY':>12} {'Lose BUY':>12} {'Score Given':>15}")
    lines.append("-" * 65)
    lines.append(f"{'> 80% (near upper)':<25} {winning.get('bb_above_80', 0):>12} {losing.get('bb_above_80', 0):>12} {'0 pts':>15}")
    lines.append(f"{'70-80%':<25} {winning.get('bb_70_to_80', 0):>12} {losing.get('bb_70_to_80', 0):>12} {'10 pts':>15}")
    lines.append(f"{'30-70% (middle)':<25} {winning.get('bb_30_to_70', 0):>12} {losing.get('bb_30_to_70', 0):>12} {'7 pts':>15}")
    lines.append(f"{'< 30% (near lower)':<25} {winning.get('bb_below_30', 0):>12} {losing.get('bb_below_30', 0):>12} {'15 pts':>15}")
    lines.append(f"\n{'Avg BB Position':<25} {winning.get('avg_bb_position', 0)*100:>11.1f}% {losing.get('avg_bb_position', 0)*100:>11.1f}%")
    
    # Section 5: Suppressed Tech During Uptrends
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 5: SUPPRESSED TECH DURING UPTRENDS")
    lines.append("(BUY trades with Tech < 55 but max favorable > 50 pips)")
    lines.append("=" * 80)
    
    lines.append(f"\nCount: {suppressed['count']}")
    lines.append(f"Avg Tech Score: {suppressed.get('avg_tech_score', 0):.2f}")
    lines.append(f"Avg Max Favorable: {suppressed.get('avg_max_favorable', 0):.1f} pips")
    lines.append(f"Avg Actual Profit: {suppressed.get('avg_profit', 0):.1f} pips")
    
    if suppressed['count'] > 0:
        lines.append(f"\nIndicator Breakdown (avg points):")
        lines.append(f"  Trend (EMAs):    {suppressed.get('avg_trend_ema', 0):.1f} / 25")
        lines.append(f"  RSI Momentum:    {suppressed.get('avg_rsi_momentum', 0):.1f} / 20")
        lines.append(f"  MACD:            {suppressed.get('avg_macd', 0):.1f} / 20")
        lines.append(f"  Bollinger:       {suppressed.get('avg_bollinger', 0):.1f} / 15")
        
        lines.append(f"\nRSI at entry:")
        lines.append(f"  > 70 (overbought): {suppressed.get('rsi_above_70', 0)}")
        lines.append(f"  60-70:             {suppressed.get('rsi_60_to_70', 0)}")
        lines.append(f"  Avg RSI:           {suppressed.get('avg_rsi_raw', 0):.1f}")
        
        lines.append(f"\nBollinger position:")
        lines.append(f"  > 80% (near upper): {suppressed.get('bb_above_80', 0)}")
        lines.append(f"  70-80%:             {suppressed.get('bb_70_to_80', 0)}")
        lines.append(f"  Avg position:       {suppressed.get('avg_bb_position', 0)*100:.1f}%")
    
    # Section 6: RSI During Low Tech + Uptrend
    lines.append("\n" + "=" * 80)
    lines.append("SECTION 6: RSI VALUES WHEN TECH < 50 DURING WINNING BUY TRADES")
    lines.append("=" * 80)
    
    lines.append(f"\nCount: {rsi_analysis['count']}")
    lines.append(f"Avg Tech Score: {rsi_analysis.get('avg_tech_score', 0):.2f}")
    lines.append(f"Avg Profit: {rsi_analysis.get('avg_profit', 0):.1f} pips")
    
    if rsi_analysis['count'] > 0:
        lines.append(f"\nRSI Distribution:")
        lines.append(f"  > 70 (overbought): {rsi_analysis.get('rsi_above_70', 0)} ({rsi_analysis.get('pct_rsi_above_70', 0):.1f}%)")
        lines.append(f"  60-70:             {rsi_analysis.get('rsi_60_to_70', 0)}")
        lines.append(f"  40-60:             {rsi_analysis.get('rsi_40_to_60', 0)}")
        lines.append(f"  < 40:              {rsi_analysis.get('rsi_below_40', 0)}")
        lines.append(f"  Avg RSI:           {rsi_analysis.get('avg_rsi', 0):.1f}")
    
    # Summary
    lines.append("\n" + "=" * 80)
    lines.append("SUMMARY & RECOMMENDATIONS")
    lines.append("=" * 80)
    
    # Calculate key insights
    win_rsi_overbought_pct = winning.get('rsi_above_70', 0) / winning['count'] * 100 if winning['count'] > 0 else 0
    lose_rsi_overbought_pct = losing.get('rsi_above_70', 0) / losing['count'] * 100 if losing['count'] > 0 else 0
    
    lines.append(f"\n1. RSI Overbought (>70) at entry:")
    lines.append(f"   - Winning BUYs: {win_rsi_overbought_pct:.1f}%")
    lines.append(f"   - Losing BUYs:  {lose_rsi_overbought_pct:.1f}%")
    
    if win_rsi_overbought_pct > lose_rsi_overbought_pct:
        lines.append(f"   => RSI overbought is MORE common in winning BUYs!")
        lines.append(f"   => Current scoring PENALIZES good entries")
    elif win_rsi_overbought_pct < lose_rsi_overbought_pct:
        lines.append(f"   => RSI overbought is MORE common in losing BUYs")
        lines.append(f"   => Current scoring has some protective value")
    else:
        lines.append(f"   => RSI overbought is equally common in both")
        lines.append(f"   => RSI scoring has no predictive value for BUYs")
    
    win_bb_upper_pct = winning.get('bb_above_80', 0) / winning['count'] * 100 if winning['count'] > 0 else 0
    lose_bb_upper_pct = losing.get('bb_above_80', 0) / losing['count'] * 100 if losing['count'] > 0 else 0
    
    lines.append(f"\n2. Bollinger > 80% (near upper band) at entry:")
    lines.append(f"   - Winning BUYs: {win_bb_upper_pct:.1f}%")
    lines.append(f"   - Losing BUYs:  {lose_bb_upper_pct:.1f}%")
    
    if win_bb_upper_pct > lose_bb_upper_pct:
        lines.append(f"   => Near upper band is MORE common in winning BUYs!")
        lines.append(f"   => Current scoring PENALIZES good entries")
    elif win_bb_upper_pct < lose_bb_upper_pct:
        lines.append(f"   => Near upper band is MORE common in losing BUYs")
        lines.append(f"   => Current scoring has some protective value")
    
    lines.append("\n" + "=" * 80)
    
    return "\n".join(lines)


def main():
    print("Loading data...")
    trades, indicators = load_data()
    
    print(f"Loaded {len(trades)} trades and {len(indicators)} indicator rows")
    
    print("Merging trades with indicators...")
    merged = merge_trades_with_indicators(trades, indicators)
    
    print("Analyzing winning BUY trades...")
    winning = analyze_winning_buys(merged)
    
    print("Analyzing losing BUY trades...")
    losing = analyze_losing_buys(merged)
    
    print("Analyzing suppressed tech during uptrends...")
    suppressed = analyze_suppressed_tech_uptrends(merged)
    
    print("Analyzing RSI during low tech + uptrend...")
    rsi_analysis = analyze_rsi_during_low_tech_uptrend(merged)
    
    print("Generating report...")
    report = format_report(winning, losing, suppressed, rsi_analysis)
    
    # Print to console
    print("\n" + report)
    
    # Save to file
    output_path = os.path.join(DATA_DIR, OUTPUT_FILE)
    with open(output_path, 'w') as f:
        f.write(report)
    
    print(f"\nReport saved to: {output_path}")


if __name__ == "__main__":
    main()
