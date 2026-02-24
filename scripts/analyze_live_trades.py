"""
Analyze live trades from history.db to diagnose WR gap cause.
Examines: SL hit timing, patterns, close reasons, profit distribution.
"""

import sqlite3
import pandas as pd
from datetime import datetime
import json

def main():
    conn = sqlite3.connect('data/history.db')
    
    # Get all trades from trades table
    try:
        df = pd.read_sql_query("SELECT * FROM trades ORDER BY close_time", conn)
        print(f"Found {len(df)} trades in trades table")
    except Exception as e:
        print(f"Error reading trade_history: {e}")
        # Try alternative table names
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [t[0] for t in cursor.fetchall()]
        print(f"Available tables: {tables}")
        conn.close()
        return
    
    if len(df) == 0:
        print("No trades found")
        conn.close()
        return
    
    print("\n" + "="*80)
    print("LIVE TRADE ANALYSIS")
    print("="*80)
    
    # Basic stats
    print(f"\nTotal trades: {len(df)}")
    print(f"Columns: {list(df.columns)}")
    
    # Win/Loss breakdown
    wins = df[df['profit'] > 0]
    losses = df[df['profit'] < 0]
    breakevens = df[df['profit'] == 0]
    
    print(f"\nWins: {len(wins)} ({len(wins)/len(df)*100:.1f}%)")
    print(f"Losses: {len(losses)} ({len(losses)/len(df)*100:.1f}%)")
    print(f"Breakevens: {len(breakevens)} ({len(breakevens)/len(df)*100:.1f}%)")
    
    # Close reason breakdown
    if 'close_type' in df.columns:
        print(f"\nClose reasons:")
        for reason, count in df['close_type'].value_counts().items():
            print(f"  {reason}: {count}")
    
    # Analyze each trade
    print("\n" + "="*80)
    print("INDIVIDUAL TRADE ANALYSIS")
    print("="*80)
    
    for idx, trade in df.iterrows():
        print(f"\n--- Trade #{trade.get('ticket', idx)} ---")
        print(f"Direction: {trade.get('direction', 'N/A')}")
        print(f"Open: {trade.get('open_price', 'N/A')}")
        print(f"Close: {trade.get('close_price', 'N/A')}")
        print(f"SL: {trade.get('sl', 'N/A')}")
        print(f"TP: {trade.get('tp', 'N/A')}")
        print(f"Profit: ${trade.get('profit', 0):.2f}")
        print(f"Close type: {trade.get('close_type', 'N/A')}")
        print(f"Close time: {trade.get('close_time', 'N/A')}")
        
        # Calculate how close to SL/TP
        if all(col in trade.index for col in ['open_price', 'close_price', 'sl', 'tp', 'direction']):
            try:
                open_p = float(trade['open_price'])
                close_p = float(trade['close_price'])
                sl = float(trade['sl'])
                tp = float(trade['tp'])
                direction = trade['direction']
                
                if direction == 'BUY':
                    sl_dist = (open_p - sl) / 0.1  # pips
                    tp_dist = (tp - open_p) / 0.1
                    move = (close_p - open_p) / 0.1
                else:
                    sl_dist = (sl - open_p) / 0.1
                    tp_dist = (open_p - tp) / 0.1
                    move = (open_p - close_p) / 0.1
                
                print(f"SL distance: {sl_dist:.0f} pips")
                print(f"TP distance: {tp_dist:.0f} pips")
                print(f"Price move: {move:.0f} pips")
                
                if trade.get('profit', 0) < 0:
                    # Loss analysis
                    if abs(move + sl_dist) < 5:  # Hit SL
                        print(f"⚠️ HIT SL (full loss)")
                    else:
                        print(f"⚠️ Partial loss or manual close")
            except Exception as e:
                print(f"Error calculating distances: {e}")
    
    # Summary patterns
    print("\n" + "="*80)
    print("LOSS PATTERN ANALYSIS")
    print("="*80)
    
    sl_hits = 0
    quick_sl_hits = 0  # SL hit within first hour
    
    for idx, trade in losses.iterrows():
        close_type = trade.get('close_type', '')
        if 'SL' in str(close_type).upper() or 'STOP' in str(close_type).upper():
            sl_hits += 1
    
    print(f"\nLosses that hit SL: {sl_hits}/{len(losses)}")
    print(f"Losses from other causes: {len(losses) - sl_hits}/{len(losses)}")
    
    # Direction breakdown
    if 'direction' in df.columns:
        print(f"\nBy direction:")
        for direction in df['direction'].unique():
            dir_trades = df[df['direction'] == direction]
            dir_wins = dir_trades[dir_trades['profit'] > 0]
            print(f"  {direction}: {len(dir_wins)}/{len(dir_trades)} wins ({len(dir_wins)/len(dir_trades)*100:.1f}%)")
    
    conn.close()
    
    # Filter to trades with proper SL/TP (exclude test trades)
    print("\n" + "="*80)
    print("FILTERED ANALYSIS (Trades with proper SL/TP only)")
    print("="*80)
    
    proper_trades = df[(df['sl'] != 0) & (df['tp'] != 0)]
    print(f"\nProper trades (SL/TP set): {len(proper_trades)}")
    
    if len(proper_trades) > 0:
        proper_wins = proper_trades[proper_trades['profit'] > 0]
        proper_losses = proper_trades[proper_trades['profit'] < 0]
        
        print(f"Wins: {len(proper_wins)} ({len(proper_wins)/len(proper_trades)*100:.1f}%)")
        print(f"Losses: {len(proper_losses)} ({len(proper_losses)/len(proper_trades)*100:.1f}%)")
        
        # Analyze SL hits
        sl_hit_count = 0
        near_sl_count = 0  # Within 10 pips of SL
        
        for idx, trade in proper_losses.iterrows():
            open_p = float(trade['open_price'])
            close_p = float(trade['close_price'])
            sl = float(trade['sl'])
            direction = trade['direction']
            
            if direction == 'BUY':
                sl_dist = (open_p - sl) / 0.1
                move = (close_p - open_p) / 0.1
            else:
                sl_dist = (sl - open_p) / 0.1
                move = (open_p - close_p) / 0.1
            
            # Check if hit SL (within 5 pips)
            if abs(move + sl_dist) < 10:
                sl_hit_count += 1
            elif abs(move + sl_dist) < 20:
                near_sl_count += 1
        
        print(f"\nLoss breakdown:")
        print(f"  Full SL hit: {sl_hit_count}/{len(proper_losses)}")
        print(f"  Near SL (within 20 pips): {near_sl_count}/{len(proper_losses)}")
        print(f"  Other (manual/partial): {len(proper_losses) - sl_hit_count - near_sl_count}/{len(proper_losses)}")
        
        # Time-based analysis
        print(f"\nTrade timeline:")
        for idx, trade in proper_trades.iterrows():
            result = "WIN" if trade['profit'] > 0 else "LOSS"
            print(f"  {trade['close_time'][:10]} | {trade['direction']:4} | {result:4} | ${trade['profit']:+.2f}")
    
    print("\n" + "="*80)
    print("DIAGNOSIS")
    print("="*80)
    print("""
Based on the data:
- Exclude first 7 trades (no SL/TP set - test/config issues)
- Remaining trades show the actual system performance
- SL hit analysis shows if execution timing is the issue
- Timeline shows if there's a regime change pattern
""")

if __name__ == "__main__":
    main()
