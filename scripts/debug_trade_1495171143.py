"""Investigate trade #1495171143 - why BE didn't trigger despite 507 pip MFE."""
import sqlite3
import pandas as pd
from datetime import datetime, timedelta

# Get trade details
conn = sqlite3.connect('data/history.db')
cur = conn.cursor()
cur.execute('SELECT * FROM trades WHERE ticket = 1495171143')
row = cur.fetchone()
cols = [d[0] for d in cur.description]
trade = dict(zip(cols, row))
conn.close()

print("=" * 80)
print("TRADE #1495171143 INVESTIGATION")
print("=" * 80)
print()
print("TRADE DETAILS:")
print(f"  Direction: {trade['direction']}")
print(f"  Open Time: {trade['open_time']}")
print(f"  Close Time: {trade['close_time']}")
print(f"  Entry: {trade['open_price']}")
print(f"  Exit: {trade['close_price']}")
print(f"  SL: {trade['sl']}")
print(f"  TP: {trade['tp']}")
print(f"  Profit: {trade['profit']}")
print(f"  Close Reason: {trade['close_reason']}")

# Calculate SL distance and BE threshold
PIP = 0.10
open_price = float(trade['open_price'])
sl = float(trade['sl'])
direction = trade['direction']

if direction == "BUY":
    sl_dist = (open_price - sl) / PIP
else:
    sl_dist = (sl - open_price) / PIP

be_threshold = sl_dist * 0.70

print()
print(f"  SL Distance: {sl_dist:.1f} pips")
print(f"  BE Threshold (70%): {be_threshold:.1f} pips")

# Load M5 data and find MFE
m5_df = pd.read_csv('data/XAUUSD_M5.csv')
m5_df['time'] = pd.to_datetime(m5_df['datetime'])

open_time = pd.to_datetime(trade['open_time'])
close_time = pd.to_datetime(trade['close_time'])

mask = (m5_df['time'] >= open_time) & (m5_df['time'] <= close_time)
candles = m5_df.loc[mask].copy()

print()
print(f"M5 CANDLES DURING TRADE: {len(candles)}")
print(f"  Trade duration: {close_time - open_time}")

if direction == "BUY":
    candles['profit_pips'] = (candles['high'] - open_price) / PIP
    max_idx = candles['profit_pips'].idxmax()
    mfe_candle = candles.loc[max_idx]
    mfe = mfe_candle['profit_pips']
else:
    candles['profit_pips'] = (open_price - candles['low']) / PIP
    max_idx = candles['profit_pips'].idxmax()
    mfe_candle = candles.loc[max_idx]
    mfe = mfe_candle['profit_pips']

print()
print("MFE ANALYSIS:")
print(f"  Max Favorable Excursion: {mfe:.1f} pips")
print(f"  MFE Candle Time: {mfe_candle['time']}")
print(f"  MFE Candle: O={mfe_candle['open']:.2f} H={mfe_candle['high']:.2f} L={mfe_candle['low']:.2f} C={mfe_candle['close']:.2f}")
print(f"  Gap above BE threshold: {mfe - be_threshold:.1f} pips")

# Find all candles where profit exceeded BE threshold
above_be = candles[candles['profit_pips'] >= be_threshold]
print()
print(f"CANDLES ABOVE BE THRESHOLD ({be_threshold:.0f} pips): {len(above_be)}")
if len(above_be) > 0:
    print("  First candle above BE:")
    first_above = above_be.iloc[0]
    print(f"    Time: {first_above['time']}")
    print(f"    Profit: {first_above['profit_pips']:.1f} pips")
    print()
    print("  All candles above BE:")
    for _, row in above_be.iterrows():
        print(f"    {row['time']} | {row['profit_pips']:.1f} pips | H={row['high']:.2f} L={row['low']:.2f}")

print()
print("=" * 80)
print("CONCLUSION")
print("=" * 80)
if len(above_be) > 0:
    duration_above = (above_be['time'].max() - above_be['time'].min())
    print(f"Trade was above BE threshold for {len(above_be)} M5 candles ({len(above_be) * 5} minutes)")
    print(f"Monitor should have triggered during this window.")
    print()
    print("Possible issues:")
    print("  1. Monitor was not running (check logs)")
    print("  2. Python exception skipped monitor cycle")
    print("  3. profit_pips calculation differs from M5 high")
else:
    print("Trade never reached BE threshold based on M5 data.")
