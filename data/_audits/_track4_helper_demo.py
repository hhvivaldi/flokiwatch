"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

FLO-422 helper integration demo — run breakout_regime.compute_regime_snapshot
against real PLAN-009 inputs (candles from MT5, analyses from history.db),
confirm the output matches the manual reference numbers.
"""
from __future__ import annotations
import sys, os, sqlite3
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
from mt5_safe import mt5, mt5_lock
from breakout_regime import compute_regime_snapshot

PLAN_ENTRY_UTC = datetime(2026, 5, 6, 13, 11, 27, tzinfo=timezone.utc)
PLAN_PRICE = 4678.85

# Pull M5 candles
with mt5_lock:
    if not mt5.initialize():
        print("MT5 init failed"); sys.exit(1)
    mt5.symbol_select("XAUUSD", True)
    rates = mt5.copy_rates_from("XAUUSD", mt5.TIMEFRAME_M5, PLAN_ENTRY_UTC.replace(tzinfo=None), 30)
    mt5.shutdown()

candles = []
for r in rates:
    candles.append({
        "open": float(r["open"]),
        "high": float(r["high"]),
        "low": float(r["low"]),
        "close": float(r["close"]),
    })

# Pull analyses for last 4h and last 24h
c = sqlite3.connect("data/history.db"); cur = c.cursor()
def fetch(mins_back: int):
    end = PLAN_ENTRY_UTC.replace(tzinfo=None)
    start = end - timedelta(minutes=mins_back)
    cur.execute(
        """SELECT timestamp, current_price, atr_14, rsi_14, ema_50,
                  bb_upper, bb_middle, bb_lower, adx_14
             FROM analyses
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp""",
        (start.isoformat()[:19], end.isoformat()[:19]),
    )
    cols = ["timestamp", "current_price", "atr_14", "rsi_14", "ema_50",
            "bb_upper", "bb_middle", "bb_lower", "adx_14"]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

a4 = fetch(240)
a24 = fetch(24 * 60)
print(f"Inputs: {len(candles)} M5 candles, {len(a4)} 4h analyses, {len(a24)} 24h analyses")

# Run the helper
snap = compute_regime_snapshot(
    ts=PLAN_ENTRY_UTC,
    direction="BUY",
    setup_type="pullback_trend",
    breakout_level=4678.0,  # PLAN-009 used a price_above 4678 reclaim
    current_price=PLAN_PRICE,
    candles_m5=candles,
    analyses_4h=a4,
    analyses_24h=a24,
    stage="author",
)

import json
print()
print("=== PLAN-009 helper output ===")
print(json.dumps(snap, indent=2))

# Cross-check against the prior manual reference numbers
print()
print("=== Cross-check against prior manual reference ===")
expected = {
    "m5_atr_pips": 52.9,
    "impulse_total_60m": 4,
    "candle_drift_trailing": 1,
    "m5_pattern": ".+.--++-+--.",
}
fails = []
for k, v in expected.items():
    actual = snap.get(k)
    if k == "m5_atr_pips":
        ok = actual is not None and abs(actual - v) < 1.0
    else:
        ok = actual == v
    sym = "OK" if ok else "FAIL"
    print(f"  [{sym}] {k}: expected {v!r}, got {actual!r}")
    if not ok: fails.append(k)

if fails:
    print(f"\nFAIL: {len(fails)} mismatches: {fails}")
    sys.exit(1)
print()
print("PLAN-009 reproduction confirmed — helper output matches prior manual analysis.")
