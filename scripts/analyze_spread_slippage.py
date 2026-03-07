"""
SPREAD & SLIPPAGE ANALYSIS — Population B Trades
=================================================
Compares requested prices (history.db) vs actual fill prices (MT5 deal history)
to measure real spread and slippage costs.

Usage:
    python scripts/analyze_spread_slippage.py
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Add parent dir to path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import MetaTrader5 as mt5

# Constants
PIP_SIZE = 0.1  # XAU/USD: 1 pip = $0.1 price movement
PIP_VALUE_001_LOT = 0.10  # $0.10 per pip for 0.01 lot
POPULATION_B_START_ID = 11  # First Population B trade ID


def connect_mt5() -> bool:
    """Connect to MT5."""
    if not mt5.initialize():
        print(f"❌ MT5 init failed: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    if info:
        print(f"✅ MT5 connected: account {info.login} on {info.server}")
    return True


def get_db_trades() -> List[Dict]:
    """Get Population B trades from history.db."""
    db_path = os.path.join(ROOT_DIR, "data", "history.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, ticket, direction, volume, open_price, close_price, sl, tp, profit, 
               close_reason, open_time, close_time
        FROM trades 
        WHERE id >= ? AND ticket > 0
        ORDER BY id
    """, (POPULATION_B_START_ID,))
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(r) for r in rows]


def get_mt5_deals(ticket: int) -> Tuple[Optional[Dict], Optional[Dict]]:
    """
    Get entry and exit deals from MT5 for a given position ticket.
    Returns (entry_deal, exit_deal) or (None, None) if not found.
    """
    # Query deals for this position
    # MT5 deal history needs a time range - use a wide range
    from_date = datetime(2026, 1, 1)
    to_date = datetime.now() + timedelta(days=1)
    
    deals = mt5.history_deals_get(from_date, to_date, group="*XAUUSD*")
    
    if deals is None or len(deals) == 0:
        return None, None
    
    entry_deal = None
    exit_deal = None
    
    for deal in deals:
        if deal.position_id == ticket:
            # DEAL_ENTRY_IN = 0, DEAL_ENTRY_OUT = 1
            if deal.entry == 0:  # Entry deal
                entry_deal = {
                    "deal_id": deal.ticket,
                    "price": deal.price,
                    "volume": deal.volume,
                    "time": datetime.fromtimestamp(deal.time),
                    "commission": deal.commission,
                    "swap": deal.swap,
                    "profit": deal.profit,
                    "type": "BUY" if deal.type == 0 else "SELL",
                }
            elif deal.entry == 1:  # Exit deal
                exit_deal = {
                    "deal_id": deal.ticket,
                    "price": deal.price,
                    "volume": deal.volume,
                    "time": datetime.fromtimestamp(deal.time),
                    "commission": deal.commission,
                    "swap": deal.swap,
                    "profit": deal.profit,
                }
    
    return entry_deal, exit_deal


def analyze_trade(db_trade: Dict, entry_deal: Optional[Dict], exit_deal: Optional[Dict]) -> Dict:
    """Analyze slippage for a single trade."""
    result = {
        "id": db_trade["id"],
        "ticket": db_trade["ticket"],
        "direction": db_trade["direction"],
        "volume": db_trade["volume"],
        "db_open_price": db_trade["open_price"],
        "db_close_price": db_trade["close_price"],
        "db_profit": db_trade["profit"],
        "close_reason": db_trade["close_reason"],
        "mt5_entry_price": None,
        "mt5_exit_price": None,
        "entry_slippage_pips": None,
        "exit_slippage_pips": None,
        "total_slippage_pips": None,
        "slippage_cost_usd": None,
        "mt5_found": False,
    }
    
    if entry_deal:
        result["mt5_entry_price"] = entry_deal["price"]
        result["mt5_found"] = True
        
        # Entry slippage: difference between requested and actual fill
        # For BUY: positive slippage = paid more than expected (bad)
        # For SELL: positive slippage = sold at lower price than expected (bad)
        if db_trade["direction"] == "BUY":
            result["entry_slippage_pips"] = (entry_deal["price"] - db_trade["open_price"]) / PIP_SIZE
        else:
            result["entry_slippage_pips"] = (db_trade["open_price"] - entry_deal["price"]) / PIP_SIZE
    
    if exit_deal:
        result["mt5_exit_price"] = exit_deal["price"]
        
        # Exit slippage: difference between expected and actual close
        # For BUY: negative slippage = closed at lower price than expected (bad)
        # For SELL: negative slippage = closed at higher price than expected (bad)
        if db_trade["direction"] == "BUY":
            result["exit_slippage_pips"] = (db_trade["close_price"] - exit_deal["price"]) / PIP_SIZE
        else:
            result["exit_slippage_pips"] = (exit_deal["price"] - db_trade["close_price"]) / PIP_SIZE
    
    # Total slippage (entry + exit, both should be negative for unfavorable slippage)
    if result["entry_slippage_pips"] is not None:
        entry_slip = result["entry_slippage_pips"]
        exit_slip = result["exit_slippage_pips"] or 0
        result["total_slippage_pips"] = entry_slip + exit_slip
        
        # Cost in USD (negative = cost, positive = favorable)
        # Slippage cost = slippage_pips * pip_value * (volume / 0.01)
        lot_multiplier = (db_trade["volume"] or 0.01) / 0.01
        result["slippage_cost_usd"] = -result["total_slippage_pips"] * PIP_VALUE_001_LOT * lot_multiplier
    
    return result


def main():
    print("=" * 70)
    print("SPREAD & SLIPPAGE ANALYSIS — Population B Trades")
    print("=" * 70)
    print()
    
    # Connect to MT5
    if not connect_mt5():
        return
    
    # Get trades from DB
    db_trades = get_db_trades()
    print(f"\n📊 Found {len(db_trades)} Population B trades (IDs {POPULATION_B_START_ID}+)")
    print()
    
    # Analyze each trade
    results = []
    mt5_found_count = 0
    
    print("Querying MT5 deal history...")
    print("-" * 70)
    
    for trade in db_trades:
        entry_deal, exit_deal = get_mt5_deals(trade["ticket"])
        result = analyze_trade(trade, entry_deal, exit_deal)
        results.append(result)
        
        if result["mt5_found"]:
            mt5_found_count += 1
    
    print(f"MT5 deals found for {mt5_found_count}/{len(db_trades)} trades")
    print()
    
    # Print detailed results
    print("=" * 70)
    print("PER-TRADE ANALYSIS")
    print("=" * 70)
    print(f"{'ID':>3} | {'Ticket':>10} | {'Dir':>4} | {'DB Open':>9} | {'MT5 Open':>9} | {'Entry Slip':>10} | {'Exit Slip':>10} | {'Cost $':>8}")
    print("-" * 90)
    
    total_entry_slippage = 0.0
    total_exit_slippage = 0.0
    total_slippage_cost = 0.0
    slippage_count = 0
    
    for r in results:
        entry_slip_str = f"{r['entry_slippage_pips']:+.2f}" if r['entry_slippage_pips'] is not None else "N/A"
        exit_slip_str = f"{r['exit_slippage_pips']:+.2f}" if r['exit_slippage_pips'] is not None else "N/A"
        cost_str = f"{r['slippage_cost_usd']:+.2f}" if r['slippage_cost_usd'] is not None else "N/A"
        mt5_open_str = f"{r['mt5_entry_price']:.2f}" if r['mt5_entry_price'] else "N/A"
        
        print(f"{r['id']:>3} | {r['ticket']:>10} | {r['direction']:>4} | {r['db_open_price']:>9.2f} | {mt5_open_str:>9} | {entry_slip_str:>10} | {exit_slip_str:>10} | {cost_str:>8}")
        
        if r['entry_slippage_pips'] is not None:
            total_entry_slippage += r['entry_slippage_pips']
            slippage_count += 1
        if r['exit_slippage_pips'] is not None:
            total_exit_slippage += r['exit_slippage_pips']
        if r['slippage_cost_usd'] is not None:
            total_slippage_cost += r['slippage_cost_usd']
    
    # Summary statistics
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    avg_entry_slip = total_entry_slippage / slippage_count if slippage_count > 0 else 0
    avg_exit_slip = total_exit_slippage / slippage_count if slippage_count > 0 else 0
    avg_total_slip = (total_entry_slippage + total_exit_slippage) / slippage_count if slippage_count > 0 else 0
    
    print(f"Trades analyzed:           {len(results)}")
    print(f"MT5 deals found:           {mt5_found_count}")
    print()
    print(f"Total entry slippage:      {total_entry_slippage:+.2f} pips")
    print(f"Total exit slippage:       {total_exit_slippage:+.2f} pips")
    print(f"Average entry slippage:    {avg_entry_slip:+.2f} pips/trade")
    print(f"Average exit slippage:     {avg_exit_slip:+.2f} pips/trade")
    print(f"Average total slippage:    {avg_total_slip:+.2f} pips/trade")
    print()
    print(f"TOTAL SLIPPAGE COST:       ${total_slippage_cost:+.2f}")
    print()
    
    # Calculate P&L impact
    total_db_profit = sum(r['db_profit'] or 0 for r in results)
    gross_wins = sum(r['db_profit'] for r in results if (r['db_profit'] or 0) > 0)
    gross_losses = abs(sum(r['db_profit'] for r in results if (r['db_profit'] or 0) < 0))
    
    print("=" * 70)
    print("P&L IMPACT ANALYSIS")
    print("=" * 70)
    print(f"Total P&L (from DB):       ${total_db_profit:+.2f}")
    print(f"Gross wins:                ${gross_wins:+.2f}")
    print(f"Gross losses:              ${gross_losses:.2f}")
    print(f"Current PF:                {gross_wins/gross_losses:.2f}" if gross_losses > 0 else "N/A")
    print()
    
    # Estimate spread cost (typical ICMarkets spread = 2-3 pips)
    typical_spread_pips = 2.5
    avg_volume = sum(r['volume'] or 0.01 for r in results) / len(results)
    spread_cost_per_trade = typical_spread_pips * PIP_VALUE_001_LOT * (avg_volume / 0.01)
    total_spread_cost = spread_cost_per_trade * len(results)
    
    print(f"Estimated spread cost:     ${total_spread_cost:.2f} (assuming {typical_spread_pips} pips avg spread)")
    print(f"Combined spread+slippage:  ${total_spread_cost + total_slippage_cost:.2f}")
    print()
    
    # Backtest PF adjustment
    print("=" * 70)
    print("BACKTEST PF ADJUSTMENT")
    print("=" * 70)
    print("Python backtest uses ZERO spread. If we add realistic costs:")
    print()
    
    # Backtest had PF 2.25 with ~225 trades over 6 months
    # Estimate spread impact on backtest
    backtest_trades = 225
    backtest_gross_wins = 2323  # From Project State doc
    backtest_gross_losses = 1033  # Estimated from PF 2.25
    backtest_pf = 2.25
    
    # Add spread cost to losses
    backtest_spread_cost = typical_spread_pips * PIP_VALUE_001_LOT * backtest_trades
    adjusted_losses = backtest_gross_losses + backtest_spread_cost
    adjusted_pf = backtest_gross_wins / adjusted_losses if adjusted_losses > 0 else 0
    
    print(f"Backtest PF (no spread):   {backtest_pf:.2f}")
    print(f"Backtest trades:           {backtest_trades}")
    print(f"Estimated spread cost:     ${backtest_spread_cost:.2f} ({typical_spread_pips} pips × {backtest_trades} trades)")
    print(f"Adjusted PF (with spread): {adjusted_pf:.2f}")
    print()
    
    # Does this explain the gap?
    live_pf = gross_wins / gross_losses if gross_losses > 0 else 0
    print("=" * 70)
    print("CONCLUSION")
    print("=" * 70)
    print(f"Backtest PF:               {backtest_pf:.2f}")
    print(f"Adjusted backtest PF:      {adjusted_pf:.2f}")
    print(f"Live PF:                   {live_pf:.2f}")
    print()
    
    gap_explained = adjusted_pf - live_pf
    original_gap = backtest_pf - live_pf
    explanation_pct = (1 - gap_explained / original_gap) * 100 if original_gap > 0 else 0
    
    print(f"Original gap:              {original_gap:.2f} (backtest {backtest_pf:.2f} vs live {live_pf:.2f})")
    print(f"Gap after spread adj:      {gap_explained:.2f}")
    print(f"Spread explains:           {explanation_pct:.1f}% of the gap")
    print()
    
    if explanation_pct < 50:
        print("⚠️  Spread alone does NOT fully explain the PF gap.")
        print("   Other factors: sample size, market regime, execution timing, etc.")
    else:
        print("✅ Spread is a MAJOR contributor to the PF gap.")
    
    mt5.shutdown()


if __name__ == "__main__":
    main()
