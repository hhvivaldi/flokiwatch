"""
Historical data collection script - XAU/USD
Project: Trading Bot XAU/USD
Step 2: Collect data from 4 timeframes (M5, M15, H1, H4) - last 3 years
"""

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
import os

# Account credentials
ACCOUNT = 52704729
PASSWORD = "EnK2S8TUd&l$VG"
SERVER = "CapitalPointTrading-Demo"

# Configuration
SYMBOL = "XAUUSD"
DATA_DIR = "data"
YEARS_TO_COLLECT = 3

# Timeframe mapping
TIMEFRAMES = {
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
}


def connect_mt5():
    """Connect to MT5 and login"""
    if not mt5.initialize():
        print(f"❌ Failed to initialize MT5: {mt5.last_error()}")
        return False
    
    if not mt5.login(ACCOUNT, password=PASSWORD, server=SERVER):
        print(f"❌ Login failed: {mt5.last_error()}")
        mt5.shutdown()
        return False
    
    return True


def collect_data(symbol, timeframe_name, timeframe, start_date, end_date):
    """
    Collect historical data for a specific timeframe.
    For smaller timeframes (M5, M15), collects in 6-month chunks to avoid MT5 limit.
    """
    print(f"\n📊 Collecting {timeframe_name}...")
    print(f"   Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    # Ensure the symbol is selected
    if not mt5.symbol_select(symbol, True):
        print(f"❌ Error selecting symbol {symbol}")
        return None
    
    # For M5 and M15, collect in 6-month chunks
    if timeframe_name in ["M5", "M15"]:
        all_data = []
        chunk_months = 6
        current_start = start_date
        chunk_num = 0
        
        while current_start < end_date:
            chunk_num += 1
            current_end = min(current_start + timedelta(days=chunk_months * 30), end_date)
            
            print(f"   Chunk {chunk_num}: {current_start.strftime('%Y-%m-%d')} to {current_end.strftime('%Y-%m-%d')}...", end=" ")
            
            rates = mt5.copy_rates_range(symbol, timeframe, current_start, current_end)
            
            if rates is not None and len(rates) > 0:
                print(f"{len(rates):,} barras")
                all_data.append(pd.DataFrame(rates))
            else:
                print("no data")
            
            current_start = current_end
        
        if not all_data:
            print(f"❌ No data collected for {timeframe_name}")
            return None
        
        # Concatenate all chunks
        df = pd.concat(all_data, ignore_index=True)
        # Remove duplicates (overlaps between chunks)
        df = df.drop_duplicates(subset=['time'])
    else:
        # For H1 and H4, direct collection works
        rates = mt5.copy_rates_range(symbol, timeframe, start_date, end_date)
        
        if rates is None or len(rates) == 0:
            error = mt5.last_error()
            print(f"❌ Error collecting data: {error}")
            return None
        
        df = pd.DataFrame(rates)
    
    # Convert timestamp to datetime
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    # Rename columns to standard
    df = df.rename(columns={
        'time': 'datetime',
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'tick_volume': 'volume',
        'spread': 'spread',
        'real_volume': 'real_volume'
    })
    
    # Select only necessary columns
    df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']]
    
    # Sort by date
    df = df.sort_values('datetime').reset_index(drop=True)
    
    print(f"   ✅ {len(df):,} bars collected in total")
    
    return df


def validate_data(df, timeframe_name):
    """Validate collected data and identify possible issues"""
    issues = []
    
    if df is None or len(df) == 0:
        return ["No data collected"]
    
    # Check for null values
    null_count = df.isnull().sum().sum()
    if null_count > 0:
        issues.append(f"Null values found: {null_count}")
    
    # Check for negative or zero values in prices
    for col in ['open', 'high', 'low', 'close']:
        invalid = (df[col] <= 0).sum()
        if invalid > 0:
            issues.append(f"Invalid values in {col}: {invalid}")
    
    # Check if high >= low
    invalid_hl = (df['high'] < df['low']).sum()
    if invalid_hl > 0:
        issues.append(f"High < Low em {invalid_hl} barras")
    
    # Check for large gaps (more than 5 consecutive periods without data)
    df_sorted = df.sort_values('datetime')
    time_diff = df_sorted['datetime'].diff()
    
    # Define expected interval per timeframe
    expected_intervals = {
        "M5": timedelta(minutes=5),
        "M15": timedelta(minutes=15),
        "H1": timedelta(hours=1),
        "H4": timedelta(hours=4),
    }
    
    expected = expected_intervals.get(timeframe_name, timedelta(hours=1))
    max_gap = expected * 10  # Allow up to 10x the interval (weekends, holidays)
    
    large_gaps = (time_diff > max_gap).sum()
    if large_gaps > 0:
        issues.append(f"Large gaps detected: {large_gaps} (normal for weekends/holidays)")
    
    return issues


def save_to_csv(df, timeframe_name, data_dir):
    """Save DataFrame to CSV file"""
    if df is None or len(df) == 0:
        return None
    
    filename = f"{SYMBOL}_{timeframe_name}.csv"
    filepath = os.path.join(data_dir, filename)
    
    df.to_csv(filepath, index=False)
    
    return filepath


def main():
    print("=" * 60)
    print("HISTORICAL DATA COLLECTION - XAU/USD Trading Bot")
    print("=" * 60)
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Period: Last {YEARS_TO_COLLECT} years")
    print()

    # Create data directory if it doesn't exist
    os.makedirs(DATA_DIR, exist_ok=True)

    # Connect to MT5
    print("[1] Connecting to MT5...")
    if not connect_mt5():
        return
    print("✅ Connected!")

    # Define collection period
    end_date = datetime.now()
    start_date = end_date - timedelta(days=YEARS_TO_COLLECT * 365)
    
    print(f"\n[2] Collection period:")
    print(f"   Start: {start_date.strftime('%Y-%m-%d')}")
    print(f"   End: {end_date.strftime('%Y-%m-%d')}")

    # Collect data for each timeframe
    print("\n[3] Collecting historical data...")
    results = {}
    
    for tf_name, tf_value in TIMEFRAMES.items():
        df = collect_data(SYMBOL, tf_name, tf_value, start_date, end_date)
        
        if df is not None and len(df) > 0:
            # Validate data
            issues = validate_data(df, tf_name)
            
            # Save CSV
            filepath = save_to_csv(df, tf_name, DATA_DIR)
            
            results[tf_name] = {
                'bars': len(df),
                'start': df['datetime'].min(),
                'end': df['datetime'].max(),
                'filepath': filepath,
                'issues': issues
            }
        else:
            results[tf_name] = {
                'bars': 0,
                'start': None,
                'end': None,
                'filepath': None,
                'issues': ['Collection failed']
            }

    # Disconnect
    mt5.shutdown()
    print("\n✅ Disconnected from MT5")

    # Show summary
    print("\n" + "=" * 60)
    print("COLLECTION SUMMARY")
    print("=" * 60)
    
    total_bars = 0
    for tf_name, data in results.items():
        print(f"\n📈 {tf_name}:")
        if data['bars'] > 0:
            print(f"   Bars: {data['bars']:,}")
            print(f"   Period: {data['start'].strftime('%Y-%m-%d %H:%M')} to {data['end'].strftime('%Y-%m-%d %H:%M')}")
            print(f"   File: {data['filepath']}")
            total_bars += data['bars']
            
            if data['issues']:
                print(f"   ⚠️  Notes:")
                for issue in data['issues']:
                    print(f"      - {issue}")
        else:
            print(f"   ❌ Collection failed")
    
    print("\n" + "-" * 60)
    print(f"📊 TOTAL: {total_bars:,} bars collected")
    print("=" * 60)
    
    # Check created files
    print("\n[4] Created files:")
    for tf_name, data in results.items():
        if data['filepath'] and os.path.exists(data['filepath']):
            size_mb = os.path.getsize(data['filepath']) / (1024 * 1024)
            print(f"   ✅ {data['filepath']} ({size_mb:.2f} MB)")
        else:
            print(f"   ❌ {SYMBOL}_{tf_name}.csv - not created")
    
    print("\n" + "=" * 60)
    print("COLLECTION COMPLETED!")
    print("=" * 60)


if __name__ == "__main__":
    main()
