"""
Migration script to fix trade history in SQLite database.
Fetches correct SL, TP, and profit values from MT5 deal history.
"""

import sqlite3
import MetaTrader5 as mt5
from datetime import datetime
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_mt5_order_data(position_id: int) -> dict:
    """Get SL/TP from MT5 order history for a position."""
    orders = mt5.history_orders_get(position=position_id)
    if not orders:
        return None
    
    # Find the opening order (type 0 = BUY, type 1 = SELL market orders)
    for order in orders:
        if order.type in (0, 1) and order.sl != 0:
            return {
                "sl": order.sl,
                "tp": order.tp,
            }
    return None


def get_mt5_deal_data(position_id: int) -> dict:
    """Get close price and profit from MT5 deal history."""
    deals = mt5.history_deals_get(position=position_id)
    if not deals:
        return None
    
    # Find the closing deal (entry type 1 = OUT)
    for deal in deals:
        if deal.entry == 1:  # DEAL_ENTRY_OUT
            return {
                "close_price": deal.price,
                "profit": deal.profit,
                "close_time": datetime.fromtimestamp(deal.time).isoformat(),
            }
    return None


def migrate_trades():
    """Migrate all trades in the database with correct MT5 data."""
    
    # Initialize MT5
    if not mt5.initialize():
        print("ERROR: Failed to initialize MT5")
        return False
    
    print("MT5 initialized successfully")
    
    # Connect to database
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "history.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Get all trades
    cursor.execute("SELECT ticket, direction, sl, tp, profit, close_price FROM trades ORDER BY ticket")
    trades = cursor.fetchall()
    
    print(f"\nFound {len(trades)} trades in database")
    print("=" * 80)
    
    updated = 0
    errors = 0
    
    for ticket, direction, db_sl, db_tp, db_profit, db_close_price in trades:
        print(f"\nTicket {ticket}:")
        print(f"  DB: SL={db_sl}, TP={db_tp}, Profit={db_profit}, Close={db_close_price}")
        
        # Get MT5 data
        order_data = get_mt5_order_data(ticket)
        deal_data = get_mt5_deal_data(ticket)
        
        if not order_data and not deal_data:
            print(f"  WARNING: No MT5 data found for ticket {ticket}")
            errors += 1
            continue
        
        # Prepare update values
        new_sl = order_data["sl"] if order_data else db_sl
        new_tp = order_data["tp"] if order_data else db_tp
        new_close_price = deal_data["close_price"] if deal_data else db_close_price
        new_profit = deal_data["profit"] if deal_data else db_profit
        new_close_time = deal_data["close_time"] if deal_data else None
        
        print(f"  MT5: SL={new_sl}, TP={new_tp}, Profit={new_profit}, Close={new_close_price}")
        
        # Check if update needed
        needs_update = (
            (db_sl == 0 and new_sl != 0) or
            (db_tp == 0 and new_tp != 0) or
            (db_profit is None and new_profit is not None) or
            (db_close_price is None and new_close_price is not None)
        )
        
        if needs_update:
            cursor.execute("""
                UPDATE trades 
                SET sl = ?, tp = ?, profit = ?, close_price = ?, close_time = COALESCE(?, close_time)
                WHERE ticket = ?
            """, (new_sl, new_tp, new_profit, new_close_price, new_close_time, ticket))
            print(f"  UPDATED")
            updated += 1
        else:
            print(f"  OK (no update needed)")
    
    conn.commit()
    conn.close()
    mt5.shutdown()
    
    print("\n" + "=" * 80)
    print(f"Migration complete: {updated} trades updated, {errors} errors")
    
    return True


def verify_totals():
    """Verify the totals match MT5 report."""
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "history.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    cursor.execute("SELECT profit FROM trades WHERE profit IS NOT NULL")
    profits = [row[0] for row in cursor.fetchall()]
    
    gross_profit = sum(p for p in profits if p > 0)
    gross_loss = sum(p for p in profits if p < 0)
    net_pnl = sum(profits)
    pf = abs(gross_profit / gross_loss) if gross_loss != 0 else 0
    
    wins = len([p for p in profits if p > 0])
    losses = len([p for p in profits if p < 0])
    total = len(profits)
    wr = (wins / total * 100) if total > 0 else 0
    
    conn.close()
    
    print("\n" + "=" * 80)
    print("VERIFICATION")
    print("=" * 80)
    print(f"Total trades: {total}")
    print(f"Wins: {wins}, Losses: {losses}")
    print(f"Win Rate: {wr:.2f}%")
    print(f"Gross Profit: ${gross_profit:.2f}")
    print(f"Gross Loss: ${gross_loss:.2f}")
    print(f"Net P&L: ${net_pnl:.2f}")
    print(f"Profit Factor: {pf:.2f}")
    print("\nExpected (from MT5 report):")
    print(f"Gross Profit: $229.25")
    print(f"Gross Loss: $-260.75")
    print(f"Net P&L: $-31.50")
    print(f"Profit Factor: 0.88")
    
    # Check if values match
    match = (
        abs(gross_profit - 229.25) < 0.01 and
        abs(gross_loss - (-260.75)) < 0.01 and
        abs(net_pnl - (-31.50)) < 0.01
    )
    
    if match:
        print("\n✅ VALUES MATCH MT5 REPORT")
    else:
        print("\n❌ VALUES DO NOT MATCH - manual review required")
    
    return match


if __name__ == "__main__":
    print("=" * 80)
    print("TRADE HISTORY MIGRATION")
    print("=" * 80)
    
    if migrate_trades():
        verify_totals()
