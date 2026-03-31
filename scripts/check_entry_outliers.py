"""Check IDs 42-43 entry slippage outliers"""
import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timedelta

mt5.initialize()

# Get DB trades for 42-43
conn = sqlite3.connect('data/history.db')
cursor = conn.cursor()
cursor.execute('SELECT id, ticket, direction, open_price, open_time, comment FROM trades WHERE id IN (42, 43)')
db_trades = cursor.fetchall()
conn.close()

# Get MT5 entry deals
deals = mt5.history_deals_get(datetime(2026,1,1), datetime.now()+timedelta(days=1), group='*XAUUSD*')
entry_deals = {d.position_id: d for d in deals if d.entry == 0}

print("IDs 42-43 Entry Analysis:")
print("=" * 80)

for t in db_trades:
    db_id, ticket, direction, db_open, open_time, comment = t
    d = entry_deals.get(ticket)
    
    print(f"\nID {db_id} - Ticket {ticket}")
    print(f"  DB open_price:     {db_open:.2f}")
    print(f"  DB open_time:      {open_time}")
    print(f"  Comment:           {comment}")
    
    if d:
        mt5_time = datetime.fromtimestamp(d.time)
        slippage_pips = (d.price - db_open) / 0.1 if direction == "BUY" else (db_open - d.price) / 0.1
        print(f"  MT5 fill price:    {d.price:.2f}")
        print(f"  MT5 fill time:     {mt5_time}")
        print(f"  Entry slippage:    {slippage_pips:+.1f} pips")
        print(f"  Time diff:         {(mt5_time - datetime.fromisoformat(open_time)).total_seconds():.1f}s")
    else:
        print(f"  MT5 deal:          NOT FOUND")

mt5.shutdown()
