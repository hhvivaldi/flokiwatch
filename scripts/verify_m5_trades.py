"""
Verify M5 data at the moments of today's trades (16 Feb 2026).
Trade #1: SELL at ~07:10 UTC
Trade #2: SELL at ~08:00 UTC

For each moment, fetches the last 6 M5 candles and calculates:
- move_pct (% change from open of 1st candle to close of last)
- green_count / red_count
- Whether the M5 filter would have blocked the SELL (move > +0.20% with green majority)
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta

# MT5 server offset (ICMarkets-Demo = UTC+2)
MT5_UTC_OFFSET_HOURS = 2

# Thresholds from config.py
M5_MODERATE_THRESHOLD = 0.20  # %
M5_STRONG_THRESHOLD = 0.40    # %

# Trade times (UTC) — adjust if needed
TRADE_TIMES_UTC = [
    {"label": "SELL #1", "utc_time": datetime(2026, 2, 16, 7, 10)},
    {"label": "SELL #2", "utc_time": datetime(2026, 2, 16, 8, 0)},
]

# How many M5 candles to analyze before the trade moment
N_CANDLES = 6


def analyze_m5_at_time(utc_time: datetime, label: str):
    """
    Fetches the last N_CANDLES completed M5 candles BEFORE utc_time.
    """
    # Convert UTC to server time (UTC+2)
    server_time = utc_time + timedelta(hours=MT5_UTC_OFFSET_HOURS)
    
    # Fetch M5 candles that end before or at the trade moment
    # Request N+5 extra candles to ensure we have enough
    rates = mt5.copy_rates_range(
        "XAUUSD", mt5.TIMEFRAME_M5,
        server_time - timedelta(minutes=(N_CANDLES + 5) * 5),
        server_time
    )
    
    if rates is None or len(rates) == 0:
        print(f"\n{'='*60}")
        print(f"  {label} — {utc_time.strftime('%H:%M UTC')}")
        print(f"  ❌ No M5 data available")
        return
    
    df = pd.DataFrame(rates)
    df['datetime_server'] = pd.to_datetime(df['time'], unit='s')
    df['datetime_utc'] = df['datetime_server'] - pd.Timedelta(hours=MT5_UTC_OFFSET_HOURS)
    
    # Filter candles that opened BEFORE the trade moment (completed)
    df = df[df['datetime_utc'] < utc_time].tail(N_CANDLES)
    
    if len(df) < N_CANDLES:
        print(f"\n{'='*60}")
        print(f"  {label} — {utc_time.strftime('%H:%M UTC')}")
        print(f"  ⚠️ Only {len(df)} M5 candles available (need {N_CANDLES})")
    
    # Calculate move_pct
    open_price = float(df.iloc[0]['open'])
    close_price = float(df.iloc[-1]['close'])
    move_pct = ((close_price - open_price) / open_price) * 100
    
    # Count green/red
    green_count = 0
    red_count = 0
    for _, row in df.iterrows():
        if row['close'] > row['open']:
            green_count += 1
        elif row['close'] < row['open']:
            red_count += 1
    
    # Check if filter would have blocked
    would_block_strong = move_pct > M5_STRONG_THRESHOLD and green_count > red_count
    would_block_moderate = move_pct > M5_MODERATE_THRESHOLD and green_count > red_count
    would_penalize = move_pct > M5_MODERATE_THRESHOLD  # Even without green majority
    
    # Output
    print(f"\n{'='*60}")
    print(f"  {label} — {utc_time.strftime('%H:%M UTC')}")
    print(f"{'='*60}")
    print(f"  M5 candles analyzed: {len(df)} (last {N_CANDLES} before the trade)")
    print(f"  Period: {df.iloc[0]['datetime_utc'].strftime('%H:%M')} → {df.iloc[-1]['datetime_utc'].strftime('%H:%M')} UTC")
    print(f"")
    
    # Detail of each candle
    print(f"  {'#':<3} {'Time UTC':<12} {'Open':>10} {'Close':>10} {'High':>10} {'Low':>10} {'Color':<8} {'Body%':>7}")
    print(f"  {'─'*75}")
    for i, (_, row) in enumerate(df.iterrows()):
        cor = "🟢 GREEN" if row['close'] > row['open'] else ("🔴 RED" if row['close'] < row['open'] else "⚪ DOJI")
        body_pct = abs(row['close'] - row['open']) / row['open'] * 100
        print(f"  {i+1:<3} {row['datetime_utc'].strftime('%H:%M'):<12} {row['open']:>10.2f} {row['close']:>10.2f} {row['high']:>10.2f} {row['low']:>10.2f} {cor:<8} {body_pct:>6.3f}%")
    
    print(f"")
    print(f"  SUMMARY:")
    print(f"  ├─ Total move: {move_pct:+.4f}% ({open_price:.2f} → {close_price:.2f})")
    print(f"  ├─ Green: {green_count} | Red: {red_count} | Doji: {N_CANDLES - green_count - red_count}")
    print(f"  ├─ Green majority: {'YES' if green_count > red_count else 'NO'}")
    print(f"  │")
    print(f"  ├─ Moderate threshold (>{M5_MODERATE_THRESHOLD}%): {'EXCEEDED' if abs(move_pct) > M5_MODERATE_THRESHOLD else 'NO'}")
    print(f"  ├─ Strong threshold (>{M5_STRONG_THRESHOLD}%): {'EXCEEDED' if abs(move_pct) > M5_STRONG_THRESHOLD else 'NO'}")
    print(f"  │")
    
    if would_block_strong:
        print(f"  └─ 🚫 M5 FILTER: STRONG BLOCK — move +{move_pct:.2f}% with {green_count}G/{red_count}R → SELL would have been BLOCKED")
    elif would_block_moderate:
        print(f"  └─ ⚠️ M5 FILTER: PENALTY — move +{move_pct:.2f}% with {green_count}G/{red_count}R → confidence -15, possible block")
    elif would_penalize:
        print(f"  └─ ⚠️ M5 FILTER: move +{move_pct:.2f}% against SELL but no green majority → no current block")
    else:
        print(f"  └─ ✅ M5 FILTER: move {move_pct:+.2f}% — compatible with SELL or below threshold")
    
    return {
        "label": label,
        "utc_time": utc_time,
        "move_pct": move_pct,
        "green_count": green_count,
        "red_count": red_count,
        "would_block_strong": would_block_strong,
        "would_block_moderate": would_block_moderate,
    }


def main():
    print("=" * 60)
    print("🔍 M5 VERIFICATION — TODAY'S TRADES (16 Feb 2026)")
    print("=" * 60)
    print(f"Current time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"M5 candles per analysis: {N_CANDLES} (~{N_CANDLES * 5} min)")
    print(f"Thresholds: moderate={M5_MODERATE_THRESHOLD}% | strong={M5_STRONG_THRESHOLD}%")
    
    # Connect MT5
    if not mt5.initialize():
        print(f"❌ MT5 init failed: {mt5.last_error()}")
        return
    
    print(f"✅ MT5 connected")
    
    results = []
    for trade in TRADE_TIMES_UTC:
        result = analyze_m5_at_time(trade["utc_time"], trade["label"])
        if result:
            results.append(result)
    
    mt5.shutdown()
    
    # Final summary
    print(f"\n{'='*60}")
    print(f"📊 FINAL SUMMARY")
    print(f"{'='*60}")
    for r in results:
        block_status = "🚫 BLOCKED" if r["would_block_strong"] else ("⚠️ PENALIZED" if r["would_block_moderate"] else "✅ WOULD PASS")
        print(f"  {r['label']} ({r['utc_time'].strftime('%H:%M')}): move={r['move_pct']:+.3f}% | {r['green_count']}G/{r['red_count']}R | {block_status}")
    
    print(f"\n  Conclusion: {'Both SELLs would have been filtered' if all(r.get('would_block_moderate') or r.get('would_block_strong') for r in results) else 'Not all would have been filtered — see details above'}")


if __name__ == "__main__":
    main()
