"""
POPULATION B TRADE ANALYSIS
============================
Analyzes all 33 Population B trades (IDs 11-43) with:
1. Trade direction (BUY/SELL)
2. Actual entry fill price
3. Outcome (WIN/LOSS)
4. Duration in hours
5. Max Adverse Excursion (MAE) in pips
6. SL Type for losing trades (SPIKE/SUSTAINED/NO DATA)
7. Brain confidence at entry

SPIKE vs SUSTAINED definition:
- SPIKE: price reversed ≥50% of adverse move within 30 minutes after SL hit
- SUSTAINED: price continued or reversed <50%

Usage:
    python scripts/analyze_population_b.py
"""

import os
import sys
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

import MetaTrader5 as mt5

PIP_SIZE = 0.1
POPULATION_B_START_ID = 11
POST_SL_ANALYSIS_MINUTES = 30


def connect_mt5() -> bool:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        return False
    info = mt5.account_info()
    if info:
        print(f"MT5 connected: {info.server}")
    return True


def get_trades_with_confidence() -> List[Dict]:
    """Get Population B trades joined with confidence from analyses."""
    db_path = os.path.join(ROOT_DIR, "data", "history.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT t.id, t.ticket, t.direction, t.open_price, t.close_price, 
               t.sl, t.tp, t.profit, t.open_time, t.close_time,
               (SELECT confidence FROM analyses a 
                WHERE a.timestamp <= t.open_time 
                ORDER BY a.timestamp DESC LIMIT 1) as confidence
        FROM trades t
        WHERE t.id >= ?
        ORDER BY t.id
    """, (POPULATION_B_START_ID,))
    
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string from DB."""
    if "T" in dt_str:
        if "." in dt_str:
            return datetime.fromisoformat(dt_str)
        return datetime.fromisoformat(dt_str)
    return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")


def get_m1_bars(start: datetime, end: datetime) -> Optional[list]:
    """Get M1 bars from MT5 for a time range."""
    rates = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M1, start, end)
    if rates is None or len(rates) == 0:
        return None
    return rates


def calculate_mae(direction: str, entry_price: float, bars: list) -> float:
    """Calculate Max Adverse Excursion in pips."""
    if direction == "BUY":
        worst_price = min(bar['low'] for bar in bars)
        mae_pips = (entry_price - worst_price) / PIP_SIZE
    else:
        worst_price = max(bar['high'] for bar in bars)
        mae_pips = (worst_price - entry_price) / PIP_SIZE
    return max(0, mae_pips)


def classify_sl_hit(direction: str, sl_price: float, close_time: datetime) -> str:
    """
    Classify SL hit as SPIKE or SUSTAINED.
    SPIKE: price reversed ≥50% of adverse move within 30 minutes after SL hit
    SUSTAINED: price continued or reversed <50%
    """
    post_sl_end = close_time + timedelta(minutes=POST_SL_ANALYSIS_MINUTES)
    bars = get_m1_bars(close_time, post_sl_end)
    
    if bars is None or len(bars) == 0:
        return "NO DATA"
    
    if direction == "BUY":
        worst_after_sl = min(bar['low'] for bar in bars)
        best_after_sl = max(bar['high'] for bar in bars)
        adverse_continuation = sl_price - worst_after_sl
        reversal = best_after_sl - sl_price
    else:
        worst_after_sl = max(bar['high'] for bar in bars)
        best_after_sl = min(bar['low'] for bar in bars)
        adverse_continuation = worst_after_sl - sl_price
        reversal = sl_price - best_after_sl
    
    total_move = adverse_continuation + reversal
    if total_move <= 0:
        return "SUSTAINED"
    
    reversal_pct = reversal / total_move if total_move > 0 else 0
    
    if reversal_pct >= 0.5:
        return "SPIKE"
    return "SUSTAINED"


def analyze_trade(trade: Dict) -> Dict:
    """Analyze a single trade."""
    open_time = parse_datetime(trade["open_time"])
    close_time = parse_datetime(trade["close_time"])
    
    duration_hours = (close_time - open_time).total_seconds() / 3600
    outcome = "WIN" if (trade["profit"] or 0) > 0 else "LOSS"
    
    bars = get_m1_bars(open_time, close_time)
    
    if bars is not None and len(bars) > 0:
        mae = calculate_mae(trade["direction"], trade["open_price"], bars)
    else:
        mae = None
    
    sl_type = "-"
    if outcome == "LOSS":
        sl_type = classify_sl_hit(trade["direction"], trade["sl"], close_time)
    
    return {
        "id": trade["id"],
        "direction": trade["direction"],
        "entry_price": trade["open_price"],
        "outcome": outcome,
        "duration_hours": duration_hours,
        "mae_pips": mae,
        "sl_type": sl_type,
        "confidence": trade["confidence"],
        "profit": trade["profit"],
    }


def main():
    print("=" * 90)
    print("POPULATION B TRADE ANALYSIS")
    print("=" * 90)
    print()
    
    if not connect_mt5():
        return
    
    trades = get_trades_with_confidence()
    print(f"Analyzing {len(trades)} Population B trades (IDs {POPULATION_B_START_ID}+)")
    print()
    
    results = []
    for trade in trades:
        result = analyze_trade(trade)
        results.append(result)
    
    print("=" * 90)
    print(f"{'ID':>3} | {'Dir':>4} | {'Entry':>9} | {'Out':>4} | {'Dur(h)':>6} | {'MAE':>6} | {'SL Type':>9} | {'Conf':>5} | {'P&L':>8}")
    print("-" * 90)
    
    wins = 0
    losses = 0
    total_mae = 0
    mae_count = 0
    spike_count = 0
    sustained_count = 0
    no_data_count = 0
    
    for r in results:
        mae_str = f"{r['mae_pips']:.1f}" if r['mae_pips'] is not None else "N/A"
        conf_str = f"{r['confidence']:.0f}%" if r['confidence'] else "N/A"
        
        print(f"{r['id']:>3} | {r['direction']:>4} | {r['entry_price']:>9.2f} | {r['outcome']:>4} | {r['duration_hours']:>6.1f} | {mae_str:>6} | {r['sl_type']:>9} | {conf_str:>5} | ${r['profit']:>+7.2f}")
        
        if r['outcome'] == "WIN":
            wins += 1
        else:
            losses += 1
            if r['sl_type'] == "SPIKE":
                spike_count += 1
            elif r['sl_type'] == "SUSTAINED":
                sustained_count += 1
            else:
                no_data_count += 1
        
        if r['mae_pips'] is not None:
            total_mae += r['mae_pips']
            mae_count += 1
    
    print("-" * 90)
    print()
    print("SUMMARY (data only, no conclusions)")
    print("-" * 40)
    print(f"Total trades:        {len(results)}")
    print(f"Wins:                {wins}")
    print(f"Losses:              {losses}")
    print(f"Win rate:            {wins/len(results)*100:.1f}%")
    print(f"Avg MAE:             {total_mae/mae_count:.1f} pips" if mae_count > 0 else "Avg MAE: N/A")
    print()
    print(f"Loss breakdown:")
    print(f"  SPIKE:             {spike_count}")
    print(f"  SUSTAINED:         {sustained_count}")
    print(f"  NO DATA:           {no_data_count}")
    
    mt5.shutdown()


if __name__ == "__main__":
    main()
