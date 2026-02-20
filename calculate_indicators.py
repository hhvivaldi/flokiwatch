"""
Technical indicators calculation script - XAU/USD
Project: Trading Bot XAU/USD
Step 3: Calculate EMAs, RSI, MACD, Bollinger Bands
"""

import pandas as pd
import pandas_ta as ta
import os
from datetime import datetime

# Configuration
DATA_DIR = "data"
SYMBOL = "XAUUSD"

# Available timeframes
TIMEFRAMES = ["M5", "M15", "H1", "H4"]


def load_data(timeframe):
    """Load historical data for a timeframe"""
    filepath = os.path.join(DATA_DIR, f"{SYMBOL}_{timeframe}.csv")
    
    if not os.path.exists(filepath):
        print(f"❌ File not found: {filepath}")
        return None
    
    df = pd.read_csv(filepath, parse_dates=['datetime'])
    return df


def calculate_emas(df):
    """Calculate EMAs of 9, 21 and 50 periods"""
    df['ema_9'] = ta.ema(df['close'], length=9)
    df['ema_21'] = ta.ema(df['close'], length=21)
    df['ema_50'] = ta.ema(df['close'], length=50)
    return df


def calculate_rsi(df, period=14):
    """Calculate 14-period RSI"""
    df['rsi_14'] = ta.rsi(df['close'], length=period)
    return df


def calculate_macd(df):
    """Calculate MACD (12, 26, 9)"""
    macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
    df['macd'] = macd['MACD_12_26_9']
    df['macd_signal'] = macd['MACDs_12_26_9']
    df['macd_hist'] = macd['MACDh_12_26_9']
    return df


def calculate_bollinger(df, period=20, std=2):
    """Calculate Bollinger Bands (20, 2)"""
    bbands = ta.bbands(df['close'], length=period, std=std)
    df['bb_upper'] = bbands[f'BBU_{period}_{std}.0']
    df['bb_middle'] = bbands[f'BBM_{period}_{std}.0']
    df['bb_lower'] = bbands[f'BBL_{period}_{std}.0']
    return df


def calculate_all_indicators(df):
    """Calculate all technical indicators"""
    df = calculate_emas(df)
    df = calculate_rsi(df)
    df = calculate_macd(df)
    df = calculate_bollinger(df)
    return df


def validate_indicators(df):
    """Validate if indicators were calculated correctly"""
    issues = []
    
    # Check RSI (must be between 0 and 100)
    rsi_valid = df['rsi_14'].dropna()
    if len(rsi_valid) > 0:
        if rsi_valid.min() < 0 or rsi_valid.max() > 100:
            issues.append(f"RSI out of range: min={rsi_valid.min():.2f}, max={rsi_valid.max():.2f}")
    
    # Check EMAs (must be positive)
    for ema in ['ema_9', 'ema_21', 'ema_50']:
        ema_valid = df[ema].dropna()
        if len(ema_valid) > 0 and ema_valid.min() <= 0:
            issues.append(f"{ema} with negative or zero values")
    
    # Check Bollinger (upper > middle > lower)
    bb_valid = df[['bb_upper', 'bb_middle', 'bb_lower']].dropna()
    if len(bb_valid) > 0:
        invalid_bb = ((bb_valid['bb_upper'] < bb_valid['bb_middle']) | 
                      (bb_valid['bb_middle'] < bb_valid['bb_lower'])).sum()
        if invalid_bb > 0:
            issues.append(f"Bollinger Bands inverted in {invalid_bb} rows")
    
    return issues


def process_timeframe(timeframe):
    """Process a timeframe: load data, calculate indicators, save"""
    print(f"\n{'='*50}")
    print(f"📊 Processing {timeframe}")
    print('='*50)
    
    # Load data
    print(f"[1] Loading data...")
    df = load_data(timeframe)
    if df is None:
        return None
    print(f"    ✅ {len(df):,} bars loaded")
    
    # Calculate indicators
    print(f"[2] Calculating indicators...")
    df = calculate_all_indicators(df)
    print(f"    ✅ Indicators calculated")
    
    # Validate
    print(f"[3] Validating indicators...")
    issues = validate_indicators(df)
    if issues:
        for issue in issues:
            print(f"    ⚠️  {issue}")
    else:
        print(f"    ✅ All indicators valid")
    
    # Count NaN per indicator column
    indicator_cols = ['ema_9', 'ema_21', 'ema_50', 'rsi_14', 'macd', 'macd_signal', 'macd_hist', 'bb_upper', 'bb_middle', 'bb_lower']
    print(f"[4] NaN values (normal in first rows):")
    for col in indicator_cols:
        nan_count = df[col].isna().sum()
        print(f"    {col}: {nan_count} NaN")
    
    # Save
    output_file = os.path.join(DATA_DIR, f"{SYMBOL}_{timeframe}_with_indicators.csv")
    df.to_csv(output_file, index=False)
    print(f"[5] File saved: {output_file}")
    
    return df


def show_sample(df, timeframe, n=10):
    """Show sample of last n rows"""
    print(f"\n📋 Last {n} rows of {timeframe}:")
    print("-" * 100)
    
    # Select relevant columns
    cols = ['datetime', 'close', 'ema_9', 'ema_21', 'ema_50', 'rsi_14', 'macd', 'macd_signal', 'bb_upper', 'bb_lower']
    sample = df[cols].tail(n)
    
    # Format for display
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', None)
    pd.set_option('display.float_format', '{:.2f}'.format)
    
    print(sample.to_string(index=False))
    print("-" * 100)


def main():
    print("=" * 60)
    print("TECHNICAL INDICATORS CALCULATION - XAU/USD Trading Bot")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    print("Indicators to calculate:")
    print("  • EMA 9, 21, 50")
    print("  • RSI 14")
    print("  • MACD (12, 26, 9)")
    print("  • Bollinger Bands (20, 2)")
    
    results = {}
    
    # Process all timeframes
    for tf in TIMEFRAMES:
        df = process_timeframe(tf)
        if df is not None:
            results[tf] = df
    
    # Show H1 sample
    if 'H1' in results:
        show_sample(results['H1'], 'H1')
    
    # Final summary
    print("\n" + "=" * 60)
    print("FINAL SUMMARY")
    print("=" * 60)
    
    for tf in TIMEFRAMES:
        filepath = os.path.join(DATA_DIR, f"{SYMBOL}_{tf}_with_indicators.csv")
        if os.path.exists(filepath):
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            print(f"✅ {SYMBOL}_{tf}_with_indicators.csv ({size_mb:.2f} MB)")
        else:
            print(f"❌ {SYMBOL}_{tf}_with_indicators.csv - not created")
    
    print("\n" + "=" * 60)
    print("CALCULATION COMPLETED!")
    print("=" * 60)


if __name__ == "__main__":
    main()
