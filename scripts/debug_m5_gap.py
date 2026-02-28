"""Check M5 data around trade #1495171143 for data issues."""
import pandas as pd

m5 = pd.read_csv('data/XAUUSD_M5.csv')
m5['time'] = pd.to_datetime(m5['datetime'])

# Check for gaps in M5 data around Feb 24
mask = (m5['time'] >= '2026-02-24 02:00') & (m5['time'] <= '2026-02-24 04:00')
candles = m5[mask][['time', 'open', 'high', 'low', 'close']]

print('M5 candles Feb 24 02:00-04:00:')
print('=' * 80)
for _, row in candles.iterrows():
    t = row['time']
    o = row['open']
    h = row['high']
    l = row['low']
    c = row['close']
    print(f"{t} | O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f}")

print()
print('=' * 80)
print('ANALYSIS:')
print('=' * 80)
print('Trade #1495171143:')
print('  Entry: 5176.54 at 02:51:42')
print('  Monitor logs show P&L: -2 to -100 pips (price was BELOW entry)')
print()
print('M5 data at 02:50-02:55 shows prices around 5227-5231')
print('This is ~50 points ABOVE the trade entry price!')
print()
print('CONCLUSION: M5 data collected today may differ from what was')
print('available during the live trade. The M5 data was just refreshed')
print('and may have different values than the original data source.')
