"""Check M5 data alignment with trade entry prices."""
import sqlite3
import pandas as pd

# Load trades
conn = sqlite3.connect('data/history.db')
trades = pd.read_sql_query(
    "SELECT ticket, direction, open_price, open_time, close_time, profit FROM trades WHERE close_time IS NOT NULL AND open_time >= '2026-02-16' ORDER BY id", conn
)
conn.close()

# Load M5 data
m5 = pd.read_csv('data/XAUUSD_M5.csv')
m5['time'] = pd.to_datetime(m5['datetime'])

PIP = 0.10

print('Checking M5 data alignment for all Population B trades:')
print('=' * 90)
print(f"{'Ticket':<12} {'Dir':<5} {'Entry':<10} {'M5 Mid':<10} {'Diff':<8} {'Status':<10} {'P&L'}")
print('-' * 90)

mismatches = 0
for _, t in trades.iterrows():
    ticket = t['ticket']
    direction = t['direction']
    open_price = float(t['open_price'])
    open_time = pd.to_datetime(t['open_time'])
    profit = t['profit']
    
    # Find M5 candle at trade open time
    mask = (m5['time'] >= open_time - pd.Timedelta(minutes=5)) & (m5['time'] <= open_time + pd.Timedelta(minutes=5))
    candles = m5[mask]
    
    if len(candles) > 0:
        closest = candles.iloc[0]
        m5_price = (closest['high'] + closest['low']) / 2
        diff = abs(open_price - m5_price)
        status = 'OK' if diff < 20 else 'MISMATCH'
        if status == 'MISMATCH':
            mismatches += 1
        print(f"{ticket:<12} {direction:<5} {open_price:<10.2f} {m5_price:<10.2f} {diff:<8.1f} {status:<10} ${profit:+.2f}")
    else:
        print(f"{ticket:<12} {direction:<5} {open_price:<10.2f} {'N/A':<10} {'N/A':<8} {'NO DATA':<10} ${profit:+.2f}")
        mismatches += 1

print('-' * 90)
print(f"Total trades: {len(trades)} | Mismatches: {mismatches}")
if mismatches > 0:
    print("\nWARNING: M5 data has timezone or data source mismatch with trade timestamps.")
    print("MFE/MAE calculations in live_trade_analysis.txt may be unreliable.")
