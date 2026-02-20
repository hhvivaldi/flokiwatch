"""
Technical Score System - XAU/USD
Project: Trading Bot XAU/USD
Step 4: Calculate score from 0-100 based on technical indicators

Score:
- 100 = Very strong BUY signal
- 0 = Very strong SELL signal
- 50 = NEUTRAL
"""

import pandas as pd
import numpy as np
from datetime import datetime
import os

# Configuration
DATA_DIR = "data"
SYMBOL = "XAUUSD"


def calculate_ema_score(row, prev_rows=None):
    """
    EMAs - 25 points maximum
    - EMA9 > EMA21 > EMA50 (bullish aligned) = +25
    - EMA9 < EMA21 < EMA50 (bearish aligned) = 0
    - Not aligned (ranging) = +12
    - BONUS: EMA9/EMA21 crossover in last 3 candles = +5
    """
    ema9 = row['ema_9']
    ema21 = row['ema_21']
    ema50 = row['ema_50']
    
    # Check NaN
    if pd.isna(ema9) or pd.isna(ema21) or pd.isna(ema50):
        return 12  # Neutral if no data
    
    score = 0
    
    # Aligned trend
    if ema9 > ema21 > ema50:
        score = 25  # Bullish aligned
    elif ema9 < ema21 < ema50:
        score = 0   # Bearish aligned
    else:
        score = 12  # Ranging
    
    # BONUS: Recent crossover (if we have previous data)
    if prev_rows is not None and len(prev_rows) >= 3:
        # Check if EMA9 crossed above EMA21 in last 3 candles
        for i in range(len(prev_rows)):
            prev = prev_rows.iloc[i]
            if not pd.isna(prev['ema_9']) and not pd.isna(prev['ema_21']):
                # If previously below and now above = bullish crossover
                if prev['ema_9'] < prev['ema_21'] and ema9 > ema21:
                    score = min(score + 5, 30)  # Crossover bonus
                    break
    
    return min(score, 30)  # Cap at 30 (25 + 5 bonus)


def calculate_rsi_score(row):
    """
    RSI - 20 points maximum
    - 30 < RSI < 70 (healthy zone) = +20
    - RSI > 70 (overbought) = +5
    - RSI < 30 (oversold) = +5
    - BONUS: 40 < RSI < 60 (sweet spot) = full score
    """
    rsi = row['rsi_14']
    
    if pd.isna(rsi):
        return 10  # Neutral
    
    if 40 <= rsi <= 60:
        return 20  # Sweet spot
    elif 30 < rsi < 70:
        return 20  # Healthy zone
    elif rsi >= 70:
        return 5   # Overbought (caution)
    else:  # rsi <= 30
        return 5   # Oversold
    
    return 10


def calculate_macd_score(row, prev_row=None):
    """
    MACD - 25 points maximum
    - MACD > Signal AND both > 0 = +25 (strong bullish)
    - MACD > Signal AND both < 0 = +15 (starting bullish)
    - MACD < Signal AND both > 0 = +10 (weakening)
    - MACD < Signal AND both < 0 = 0 (strong bearish)
    - BONUS: Histogram growing = +3
    """
    macd = row['macd']
    signal = row['macd_signal']
    hist = row['macd_hist']
    
    if pd.isna(macd) or pd.isna(signal):
        return 12  # Neutral
    
    score = 0
    
    if macd > signal:
        if macd > 0 and signal > 0:
            score = 25  # Strong bullish
        else:
            score = 15  # Starting bullish
    else:  # macd < signal
        if macd > 0 and signal > 0:
            score = 10  # Weakening
        else:
            score = 0   # Strong bearish
    
    # BONUS: Histogram growing
    if prev_row is not None and not pd.isna(prev_row['macd_hist']) and not pd.isna(hist):
        if hist > prev_row['macd_hist']:
            score = min(score + 3, 28)
    
    return min(score, 28)  # Cap at 28 (25 + 3 bonus)


def calculate_bollinger_score(row):
    """
    Bollinger Bands - 15 points maximum
    - Price < 25% of band (near lower) = +15 (oversold, good for buying)
    - Price > 75% of band (near upper) = +3 (overbought)
    - Price in middle (40-60%) = +10 (neutral)
    """
    close = row['close']
    bb_upper = row['bb_upper']
    bb_lower = row['bb_lower']
    
    if pd.isna(bb_upper) or pd.isna(bb_lower) or pd.isna(close):
        return 7  # Neutral
    
    # Calculate percentage position in band
    bb_range = bb_upper - bb_lower
    if bb_range == 0:
        return 7
    
    position = (close - bb_lower) / bb_range * 100
    
    if position < 25:
        return 15  # Oversold - good for buying
    elif position > 75:
        return 3   # Overbought
    elif 40 <= position <= 60:
        return 10  # Neutral/middle
    elif position < 40:
        return 12  # Slightly oversold
    else:  # 60 < position <= 75
        return 6   # Slightly overbought
    
    return 7


def calculate_price_vs_ema50_score(row):
    """
    Price vs EMA 50 - 15 points maximum
    - Close > EMA50 = +15 (bull market)
    - Close < EMA50 = 0 (bear market)
    """
    close = row['close']
    ema50 = row['ema_50']
    
    if pd.isna(close) or pd.isna(ema50):
        return 7  # Neutral
    
    if close > ema50:
        # The higher above, the better (up to a limit)
        diff_pct = (close - ema50) / ema50 * 100
        if diff_pct > 2:
            return 15  # Well above
        elif diff_pct > 0.5:
            return 12  # Moderately above
        else:
            return 10  # Slightly above
    else:
        # Below EMA50
        diff_pct = (ema50 - close) / ema50 * 100
        if diff_pct > 2:
            return 0   # Well below
        elif diff_pct > 0.5:
            return 3   # Moderately below
        else:
            return 5   # Slightly below
    
    return 7


def calculate_technical_score(row, prev_rows=None, prev_row=None):
    """
    Calculate total technical score (0-100)
    
    Distribution:
    - EMAs: 25 points (+5 bonus)
    - RSI: 20 points
    - MACD: 25 points (+3 bonus)
    - Bollinger: 15 points
    - Price vs EMA50: 15 points
    
    Base total: 100 points
    With bonus: up to ~108 (normalized to 100)
    """
    ema_score = calculate_ema_score(row, prev_rows)
    rsi_score = calculate_rsi_score(row)
    macd_score = calculate_macd_score(row, prev_row)
    bb_score = calculate_bollinger_score(row)
    price_ema_score = calculate_price_vs_ema50_score(row)
    
    total = ema_score + rsi_score + macd_score + bb_score + price_ema_score
    
    # Normalize to 0-100 (considering maximum bonus of ~108)
    normalized = min(max(total * 100 / 108, 0), 100)
    
    return round(normalized, 2)


def get_score_interpretation(score):
    """Return textual interpretation of the score"""
    if score >= 80:
        return "🟢 STRONG BUY"
    elif score >= 65:
        return "🟢 BUY"
    elif score >= 55:
        return "🟡 WEAK BUY"
    elif score >= 45:
        return "⚪ NEUTRAL"
    elif score >= 35:
        return "🟡 WEAK SELL"
    elif score >= 20:
        return "🔴 SELL"
    else:
        return "🔴 STRONG SELL"


def apply_scores_to_dataframe(df):
    """Apply score calculation to the entire dataframe"""
    scores = []
    
    for i in range(len(df)):
        row = df.iloc[i]
        
        # Get previous rows for crossover bonus
        prev_rows = df.iloc[max(0, i-3):i] if i > 0 else None
        prev_row = df.iloc[i-1] if i > 0 else None
        
        score = calculate_technical_score(row, prev_rows, prev_row)
        scores.append(score)
    
    df['technical_score'] = scores
    return df


def calculate_statistics(df):
    """Calculate score distribution statistics"""
    scores = df['technical_score'].dropna()
    
    stats = {
        'total_rows': len(scores),
        'mean': scores.mean(),
        'median': scores.median(),
        'std': scores.std(),
        'min': scores.min(),
        'max': scores.max(),
        'distribution': {
            'strong_sell (0-20)': (scores < 20).sum() / len(scores) * 100,
            'sell (20-35)': ((scores >= 20) & (scores < 35)).sum() / len(scores) * 100,
            'weak_sell (35-45)': ((scores >= 35) & (scores < 45)).sum() / len(scores) * 100,
            'neutral (45-55)': ((scores >= 45) & (scores < 55)).sum() / len(scores) * 100,
            'weak_buy (55-65)': ((scores >= 55) & (scores < 65)).sum() / len(scores) * 100,
            'buy (65-80)': ((scores >= 65) & (scores < 80)).sum() / len(scores) * 100,
            'strong_buy (80-100)': (scores >= 80).sum() / len(scores) * 100,
        }
    }
    
    return stats


def show_examples(df, n=5):
    """Show practical examples of different scores"""
    print("\n" + "=" * 80)
    print("PRACTICAL EXAMPLES")
    print("=" * 80)
    
    # Get examples from different score ranges
    examples = []
    
    # High score (>75)
    high_scores = df[df['technical_score'] >= 75]
    if len(high_scores) > 0:
        examples.append(('STRONG BUY', high_scores.sample(1).iloc[0]))
    
    # Medium-high score (60-75)
    med_high = df[(df['technical_score'] >= 60) & (df['technical_score'] < 75)]
    if len(med_high) > 0:
        examples.append(('BUY', med_high.sample(1).iloc[0]))
    
    # Neutral score (45-55)
    neutral = df[(df['technical_score'] >= 45) & (df['technical_score'] < 55)]
    if len(neutral) > 0:
        examples.append(('NEUTRAL', neutral.sample(1).iloc[0]))
    
    # Medium-low score (25-45)
    med_low = df[(df['technical_score'] >= 25) & (df['technical_score'] < 45)]
    if len(med_low) > 0:
        examples.append(('SELL', med_low.sample(1).iloc[0]))
    
    # Low score (<25)
    low_scores = df[df['technical_score'] < 25]
    if len(low_scores) > 0:
        examples.append(('STRONG SELL', low_scores.sample(1).iloc[0]))
    
    for label, row in examples:
        print(f"\n{'─' * 80}")
        print(f"📊 Example: {label}")
        print(f"{'─' * 80}")
        print(f"Date/Time: {row['datetime']}")
        print(f"Close: {row['close']:.2f}")
        print()
        
        # Indicator breakdown
        print("Indicators:")
        print(f"  EMA9: {row['ema_9']:.2f} | EMA21: {row['ema_21']:.2f} | EMA50: {row['ema_50']:.2f}")
        
        # Check EMA alignment
        if row['ema_9'] > row['ema_21'] > row['ema_50']:
            ema_status = "✅ Bullish aligned (+25)"
        elif row['ema_9'] < row['ema_21'] < row['ema_50']:
            ema_status = "❌ Bearish aligned (0)"
        else:
            ema_status = "➖ Ranging (+12)"
        print(f"  → {ema_status}")
        
        print(f"\n  RSI: {row['rsi_14']:.2f}")
        if 40 <= row['rsi_14'] <= 60:
            print(f"  → ✅ Sweet spot (+20)")
        elif 30 < row['rsi_14'] < 70:
            print(f"  -> ✅ Healthy zone (+20)")
        else:
            print(f"  -> ⚠️ Extreme zone (+5)")
        
        print(f"\n  MACD: {row['macd']:.2f} | Signal: {row['macd_signal']:.2f} | Hist: {row['macd_hist']:.2f}")
        if row['macd'] > row['macd_signal']:
            if row['macd'] > 0:
                print(f"  -> ✅ MACD > Signal, both positive (+25)")
            else:
                print(f"  -> ✅ MACD > Signal, starting bullish (+15)")
        else:
            if row['macd'] < 0:
                print(f"  -> ❌ MACD < Signal, both negative (0)")
            else:
                print(f"  -> ➖ MACD < Signal, weakening (+10)")
        
        # Bollinger position
        bb_range = row['bb_upper'] - row['bb_lower']
        bb_pos = (row['close'] - row['bb_lower']) / bb_range * 100 if bb_range > 0 else 50
        print(f"\n  BB Upper: {row['bb_upper']:.2f} | BB Lower: {row['bb_lower']:.2f}")
        print(f"  Position in band: {bb_pos:.1f}%")
        if bb_pos < 25:
            print(f"  -> ✅ Oversold (+15)")
        elif bb_pos > 75:
            print(f"  -> ⚠️ Overbought (+3)")
        else:
            print(f"  -> ➖ Middle of band (+10)")
        
        print(f"\n  Close vs EMA50: {row['close']:.2f} vs {row['ema_50']:.2f}")
        if row['close'] > row['ema_50']:
            print(f"  -> ✅ Above EMA50 (+15)")
        else:
            print(f"  -> ❌ Below EMA50 (0)")
        
        print(f"\n{'─' * 40}")
        print(f"SCORE FINAL: {row['technical_score']:.1f}/100 → {get_score_interpretation(row['technical_score'])}")
        print(f"{'─' * 40}")


def main():
    print("=" * 70)
    print("TECHNICAL SCORE SYSTEM - XAU/USD Trading Bot")
    print("=" * 70)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("Point distribution:")
    print("  - EMAs (9, 21, 50): 25 pts (+5 crossover bonus)")
    print("  - RSI (14): 20 pts")
    print("  - MACD: 25 pts (+3 histogram bonus)")
    print("  - Bollinger Bands: 15 pts")
    print("  - Price vs EMA50: 15 pts")
    print("  - Total: 100 pts (normalized)")
    
    # Load data with indicators
    print("\n[1] Loading H1 data with indicators...")
    filepath = os.path.join(DATA_DIR, f"{SYMBOL}_H1_with_indicators.csv")
    
    if not os.path.exists(filepath):
        print(f"❌ File not found: {filepath}")
        return
    
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    print(f"    ✅ {len(df):,} bars loaded")
    
    # Calculate scores
    print("\n[2] Calculating technical scores...")
    df = apply_scores_to_dataframe(df)
    print(f"    ✅ Scores calculated")
    
    # Statistics
    print("\n[3] Calculating statistics...")
    stats = calculate_statistics(df)
    
    print(f"\n{'=' * 70}")
    print("SCORE STATISTICS")
    print(f"{'=' * 70}")
    print(f"Total bars analyzed: {stats['total_rows']:,}")
    print(f"Average score: {stats['mean']:.2f}")
    print(f"Median score: {stats['median']:.2f}")
    print(f"Standard deviation: {stats['std']:.2f}")
    print(f"Minimum score: {stats['min']:.2f}")
    print(f"Maximum score: {stats['max']:.2f}")
    
    print(f"\n{'─' * 70}")
    print("SCORE DISTRIBUTION:")
    print(f"{'─' * 70}")
    for faixa, pct in stats['distribution'].items():
        bar = "█" * int(pct / 2)
        print(f"  {faixa:25} {pct:5.1f}% {bar}")
    
    # Save file
    print(f"\n[4] Saving file...")
    output_file = os.path.join(DATA_DIR, f"{SYMBOL}_H1_with_scores.csv")
    df.to_csv(output_file, index=False)
    size_mb = os.path.getsize(output_file) / (1024 * 1024)
    print(f"    ✅ Saved: {output_file} ({size_mb:.2f} MB)")
    
    # Show examples
    show_examples(df)
    
    # Show last 10 rows
    print(f"\n{'=' * 70}")
    print("LAST 10 ROWS")
    print(f"{'=' * 70}")
    cols = ['datetime', 'close', 'technical_score']
    last_10 = df[cols].tail(10)
    for _, row in last_10.iterrows():
        interp = get_score_interpretation(row['technical_score'])
        print(f"{row['datetime']} | Close: {row['close']:.2f} | Score: {row['technical_score']:.1f} | {interp}")
    
    print(f"\n{'=' * 70}")
    print("SCORE CALCULATION COMPLETE!")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
