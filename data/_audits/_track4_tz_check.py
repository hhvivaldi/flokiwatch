"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

Verify MT5 broker-time alignment for the Track 4 candle backfill.

Hypothesis: copy_rates_from(symbol, tf, ts, n) interprets `ts` as broker time
(UTC+3 per FLO-96). When I passed naive UTC, the 60-min window I pulled was
actually 3 hours earlier in UTC than the trade entry.

Spot-check PLAN-009 (entered 2026-05-06T13:11:27Z UTC):
- Pull 12 M5 bars ending at naive-UTC 13:11 — what timestamps come back?
- Pull 12 M5 bars ending at broker-time (UTC+3) 16:11 — what timestamps come back?
- Compare: which set actually represents the 60 min before UTC 13:11?
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from mt5_safe import mt5, mt5_lock

with mt5_lock:
    if not mt5.initialize():
        print(f"MT5 init failed: {mt5.last_error()}")
        sys.exit(1)
    if not mt5.symbol_select("XAUUSD", True):
        print("symbol_select failed")
        sys.exit(1)

    plan_entry_utc = datetime(2026, 5, 6, 13, 11, 27, tzinfo=timezone.utc)
    print(f"Reference trade: PLAN-009 entered {plan_entry_utc.isoformat()} (UTC)")
    print(f"Expected price near entry: 4678.85 (from history.db trades.open_price)")
    print()

    # Approach 1: pass naive UTC datetime
    ts_naive_utc = plan_entry_utc.replace(tzinfo=None)
    rates_a = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, ts_naive_utc, 12)
    print(f"=== APPROACH A: pass naive UTC {ts_naive_utc} ===")
    if rates_a is not None and len(rates_a) > 0:
        for r in rates_a[-5:]:
            t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            print(f"  bar_open={t.isoformat()}  O={float(r['open']):.2f} H={float(r['high']):.2f} L={float(r['low']):.2f} C={float(r['close']):.2f}")
    else:
        print(f"  no data: {mt5.last_error()}")

    # Approach 2: pass broker time (UTC+3)
    ts_broker_naive = (plan_entry_utc + timedelta(hours=3)).replace(tzinfo=None)
    rates_b = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, ts_broker_naive, 12)
    print()
    print(f"=== APPROACH B: pass broker time (UTC+3) {ts_broker_naive} ===")
    if rates_b is not None and len(rates_b) > 0:
        for r in rates_b[-5:]:
            t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            print(f"  bar_open={t.isoformat()}  O={float(r['open']):.2f} H={float(r['high']):.2f} L={float(r['low']):.2f} C={float(r['close']):.2f}")
    else:
        print(f"  no data: {mt5.last_error()}")

    # Approach 3: copy_rates_range with explicit start/end as UTC (let MT5 handle interpretation)
    end = plan_entry_utc.replace(tzinfo=None)
    start = end - timedelta(hours=1)
    rates_c = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, start, end)
    print()
    print(f"=== APPROACH C: copy_rates_range with naive UTC ({start} → {end}) ===")
    if rates_c is not None and len(rates_c) > 0:
        for r in rates_c:
            t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            print(f"  bar_open={t.isoformat()}  O={float(r['open']):.2f} H={float(r['high']):.2f} L={float(r['low']):.2f} C={float(r['close']):.2f}")
    else:
        print(f"  no data: {mt5.last_error()}")

    # Approach 4: copy_rates_range with broker-time-converted bounds
    end_b = (plan_entry_utc + timedelta(hours=3)).replace(tzinfo=None)
    start_b = end_b - timedelta(hours=1)
    rates_d = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, start_b, end_b)
    print()
    print(f"=== APPROACH D: copy_rates_range with broker-time bounds ({start_b} → {end_b}) ===")
    if rates_d is not None and len(rates_d) > 0:
        for r in rates_d:
            t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
            print(f"  bar_open={t.isoformat()}  O={float(r['open']):.2f} H={float(r['high']):.2f} L={float(r['low']):.2f} C={float(r['close']):.2f}")
    else:
        print(f"  no data: {mt5.last_error()}")

    mt5.shutdown()

print()
print("=== ANALYSIS ===")
print("Trade open_price was 4678.85 at UTC 13:11. The approach whose bar prices")
print("are NEAREST to 4678 (within ~5p, given normal 5-min volatility) is the")
print("approach that's correctly aligned to the actual trade entry window.")
