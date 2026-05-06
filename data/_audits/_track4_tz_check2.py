"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

Direct timezone offset measurement.

Compare MT5 server tick time vs actual UTC now. The difference = MT5 offset.
Then test which copy_rates_from input correctly returns the bar containing a
known-time trade entry.
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from mt5_safe import mt5, mt5_lock

with mt5_lock:
    if not mt5.initialize():
        sys.exit(1)
    mt5.symbol_select("XAUUSD", True)

    # 1. Direct offset measurement
    now_utc = datetime.now(timezone.utc)
    tick = mt5.symbol_info_tick("XAUUSD")
    if tick is not None:
        tick_dt = datetime.fromtimestamp(int(tick.time), tz=timezone.utc)
        offset_seconds = (tick_dt - now_utc).total_seconds()
        offset_hours = offset_seconds / 3600
        print(f"Now UTC: {now_utc.isoformat()}")
        print(f"MT5 tick.time as UTC-fromtimestamp: {tick_dt.isoformat()}")
        print(f"Offset (MT5 ahead of actual UTC): {offset_hours:+.2f} hours")
        print(f"  -> If +3h: MT5 returns timestamps that are broker-clock seconds (UTC+3), labeled as UTC.")
        print(f"  -> If +0h: MT5 returns proper UTC timestamps.")
        print()

    # 2. Find the M5 bar containing PLAN-009's UTC entry of 2026-05-06T13:11:27Z
    plan_entry_utc = datetime(2026, 5, 6, 13, 11, 27, tzinfo=timezone.utc)
    expected_open = 4678.85
    print(f"Target: M5 bar containing UTC {plan_entry_utc.isoformat()} (PLAN-009 entry, expected open ~{expected_open})")
    print()

    # Try four input strategies, see which returns the matching bar
    strategies = [
        ("naive UTC", plan_entry_utc.replace(tzinfo=None) + timedelta(minutes=10)),
        ("UTC + 3h (broker-time)", plan_entry_utc.replace(tzinfo=None) + timedelta(hours=3, minutes=10)),
        ("UTC + 2h", plan_entry_utc.replace(tzinfo=None) + timedelta(hours=2, minutes=10)),
        ("UTC + offset_hours", plan_entry_utc.replace(tzinfo=None) + timedelta(hours=int(offset_hours), minutes=10)),
    ]
    for label, ts in strategies:
        rates = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, ts, 5)
        print(f"=== Strategy: {label} (input ts: {ts}) ===")
        if rates is not None:
            for r in rates:
                t = datetime.fromtimestamp(int(r["time"]), tz=timezone.utc)
                near_target = abs(float(r["open"]) - expected_open) < 5
                marker = "  <-- MATCH" if near_target else ""
                print(f"  bar_open(label)={t.strftime('%H:%M')} O={float(r['open']):.2f}{marker}")
        print()

    mt5.shutdown()
