"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

PLAN-009 manual reference: M5 candle pattern around the entry.

Goal: confirm the 60-min pre-entry candles produce a recognizable
'compression-with-fresh-expansion' pattern, validating that the candle
metric (operating on OHLC, not on time labels) is correctly aligned.
"""
from __future__ import annotations
import sys, os, statistics
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timezone
from mt5_safe import mt5, mt5_lock

with mt5_lock:
    if not mt5.initialize():
        sys.exit(1)
    mt5.symbol_select("XAUUSD", True)

    plan_entry_utc = datetime(2026, 5, 6, 13, 11, 27, tzinfo=timezone.utc)
    open_price = 4678.85
    direction = "BUY"
    rates = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, plan_entry_utc.replace(tzinfo=None), 30)
    mt5.shutdown()

bars = []
for r in rates:
    bars.append({
        "label_ts": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
        "body": float(r["close"]) - float(r["open"]),
    })

last12 = bars[-12:]
prior14 = bars[-26:-12] if len(bars) >= 26 else bars[:-12]

# M5 ATR (over prior 14)
trs = []
for i, b in enumerate(prior14):
    if i == 0:
        trs.append(b["high"] - b["low"])
    else:
        pc = prior14[i-1]["close"]
        trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
m5_atr = statistics.mean(trs)

print(f"PLAN-20260506-009 (BUY, entered UTC {plan_entry_utc.isoformat()}, open=4678.85, won +281p)")
print(f"M5 ATR (prior 14 bars): {m5_atr*10:.1f} pips")
print()
print("Pre-entry 60-min M5 bars (last 12):")
print(f'{"label":<8} {"open":>8} {"high":>8} {"low":>8} {"close":>8} {"body":>7} {"size_vs_ATR":>11} {"class":>10}')

target_pos = (direction == "BUY")
pattern = []
for b in last12:
    body = b["body"]
    same_dir = (body > 0) == target_pos
    magnitude_ok = abs(body) >= 0.5 * m5_atr
    if same_dir and magnitude_ok:
        cls = "+"
    elif same_dir:
        cls = "."
    elif body == 0:
        cls = "o"
    else:
        cls = "-"
    pattern.append(cls)
    body_size = abs(body) / m5_atr if m5_atr else 0
    print(f'{b["label_ts"].strftime("%H:%M"):<8} {b["open"]:>8.2f} {b["high"]:>8.2f} {b["low"]:>8.2f} {b["close"]:>8.2f} {body:>+7.2f} {body_size:>10.2f}× {cls:>10}')

print()
print(f"Pattern (60-min): [{''.join(pattern)}]")
print(f"  + = same-dir impulse (>=0.5x ATR)")
print(f"  . = same-dir small body")
print(f"  - = against-dir bar")
print()

# Compute the corrected metrics
impulse_trailing = 0
for ch in reversed(pattern):
    if ch == "+":
        impulse_trailing += 1
    else:
        break
impulse_total = sum(1 for ch in pattern if ch == "+")
drift_trailing = 0
for ch in reversed(pattern):
    if ch in ("+", "."):
        drift_trailing += 1
    else:
        break
same_dir_total = sum(1 for ch in pattern if ch in ("+", "."))

print(f"Trailing strict impulses (>=0.5x ATR same-dir): {impulse_trailing}")
print(f"Trailing same-direction bars (any size):        {drift_trailing}")
print(f"Total impulses anywhere in 12 bars:             {impulse_total}")
print(f"Total same-direction bars in 12 bars:           {same_dir_total}")
print()
print(f"Verification: last bar's open should be near 4678 (PLAN-009 entry price).")
print(f"  Last bar open: {last12[-1]['open']:.2f}")
print(f"  Match within 5p: {'YES' if abs(last12[-1]['open']-4678.85)<5 else 'NO — alignment may be off'}")
