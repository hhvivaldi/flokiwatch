"""Quick check for M1 data availability"""
import MetaTrader5 as mt5
from datetime import datetime, timedelta

mt5.initialize()

dates_to_check = [
    datetime(2026, 2, 16, 12, 0),
    datetime(2026, 2, 28, 10, 0),
    datetime(2026, 3, 1, 10, 0),
    datetime(2026, 3, 2, 10, 0),
    datetime(2026, 3, 3, 10, 0),
    datetime(2026, 3, 4, 10, 0),
    datetime(2026, 3, 5, 10, 0),
    datetime(2026, 3, 6, 10, 0),
]

print("M1 data availability check:")
for d in dates_to_check:
    rates = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M1, d, d + timedelta(hours=12))
    count = len(rates) if rates is not None else 0
    print(f"  {d.strftime('%Y-%m-%d %H:%M')}: {count} bars")

mt5.shutdown()
