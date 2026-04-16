"""FLO-333 audit: compare UTC-day vs broker-aligned daily P&L bucketing
across last 7 days. Shows which days' P&L numbers were wrong under the
UTC strftime bug."""
import sys
import os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timedelta
from collections import defaultdict
import time as _t
import MetaTrader5 as mt5

mt5.initialize()
from tz_utils import trading_day_broker_aligned

now = datetime.now()
deals = mt5.history_deals_get(now - timedelta(days=8), now + timedelta(hours=1), group="*XAUUSD*")
tick = mt5.symbol_info_tick("XAUUSD")
offset_s = int(tick.time) - int(_t.time()) if tick and tick.time else 0

closes = []
for d in deals:
    if d.entry != 1:
        continue
    ct_utc = datetime.utcfromtimestamp(int(d.time) - offset_s)
    closes.append({"pos": d.position_id, "profit": d.profit, "close_utc": ct_utc})

cross = []
for c in closes:
    buggy = c["close_utc"].strftime("%Y-%m-%d")
    correct = trading_day_broker_aligned(now=c["close_utc"])
    if buggy != correct:
        cross.append({**c, "utc_day": buggy, "broker_day": correct})

print(f"Total close deals 7d: {len(closes)}")
print(f"Cross-day boundary trades (utc_day != broker_day): {len(cross)}")
for c in cross:
    print(f"  #{c['pos']} profit={c['profit']:+.2f} close_utc={c['close_utc']} utc_day={c['utc_day']} broker_day={c['broker_day']}")

utc_pnl = defaultdict(float)
broker_pnl = defaultdict(float)
utc_count = defaultdict(int)
broker_count = defaultdict(int)
for c in closes:
    utc_pnl[c["close_utc"].strftime("%Y-%m-%d")] += c["profit"]
    utc_count[c["close_utc"].strftime("%Y-%m-%d")] += 1
    bd = trading_day_broker_aligned(now=c["close_utc"])
    broker_pnl[bd] += c["profit"]
    broker_count[bd] += 1

print("\nDaily P&L comparison (utc-bucket = current buggy, broker-bucket = correct):")
all_days = sorted(set(list(utc_pnl.keys()) + list(broker_pnl.keys())))
for d in all_days:
    u = utc_pnl[d]
    b = broker_pnl[d]
    uc = utc_count[d]
    bc = broker_count[d]
    flag = "  <-- DIFFER" if abs(u - b) > 0.01 or uc != bc else ""
    print(f"  {d}  utc=${u:+8.2f} ({uc}t)  broker=${b:+8.2f} ({bc}t)  diff=${b - u:+.2f}{flag}")

mt5.shutdown()
