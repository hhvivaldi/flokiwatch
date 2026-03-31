"""Quick check: DB close_price vs MT5 exit deal price"""
import sqlite3
import MetaTrader5 as mt5
from datetime import datetime, timedelta

mt5.initialize()

# Get DB trades
conn = sqlite3.connect('data/history.db')
cursor = conn.cursor()
cursor.execute('SELECT id, ticket, close_price FROM trades WHERE id >= 11 ORDER BY id')
db_trades = cursor.fetchall()
conn.close()

# Get MT5 exit deals
deals = mt5.history_deals_get(datetime(2026,1,1), datetime.now()+timedelta(days=1), group='*XAUUSD*')
exit_deals = {d.position_id: d.price for d in deals if d.entry == 1}

print("DB close_price vs MT5 exit price:")
print("-" * 60)
print(f"{'ID':>3} | {'Ticket':>10} | {'DB Close':>10} | {'MT5 Exit':>10} | Match")
print("-" * 60)

for t in db_trades:
    db_id, ticket, db_close = t
    mt5_exit = exit_deals.get(ticket)
    match = "YES" if mt5_exit and abs(db_close - mt5_exit) < 0.01 else "NO"
    mt5_str = f"{mt5_exit:.2f}" if mt5_exit else "N/A"
    print(f"{db_id:>3} | {ticket:>10} | {db_close:>10.2f} | {mt5_str:>10} | {match}")

mt5.shutdown()
