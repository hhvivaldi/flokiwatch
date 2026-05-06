"""[RESEARCH/REPRO ONLY — NOT IMPORTED BY PRODUCTION CODE]

Track 4 backfill — candle-based impulse-count vs snapshot-based proxy.
Read-only. Pulls M5 OHLC for the 17 historical breakout trades and recomputes
impulse_count_60m using actual candle bodies + M5 ATR. Compares to the
snapshot-based proxy used in the prior breakout study.
"""
from __future__ import annotations
import sys, os, pickle, statistics
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from datetime import datetime, timezone
from mt5_safe import mt5, mt5_lock

with open("data/_audits/_breakout_4h.pkl", "rb") as f:
    snapshot_results = {r["pid"]: r for r in pickle.load(f)}

fired = [
    ("PLAN-20260427-005", "SELL", "2026-04-27T14:27:33Z", 0),
    ("PLAN-20260427-007", "SELL", "2026-04-27T14:54:07Z", 78),
    ("PLAN-20260428-016", "SELL", "2026-04-28T23:05:57Z", 8),
    ("PLAN-20260430-009", "SELL", "2026-04-30T13:41:19Z", 0),
    ("PLAN-20260430-022", "BUY",  "2026-04-30T19:07:57Z", 15),
    ("PLAN-20260430-026", "BUY",  "2026-05-01T02:17:27Z", -62),
    ("PLAN-20260501-013", "SELL", "2026-05-01T06:29:11Z", -31),
    ("PLAN-20260501-019", "SELL", "2026-05-01T09:54:43Z", -6),
    ("PLAN-20260501-030", "BUY",  "2026-05-01T12:26:17Z", -72),
    ("PLAN-20260504-002", "SELL", "2026-05-04T01:05:16Z", 76),
    ("PLAN-20260504-007", "SELL", "2026-05-04T10:03:49Z", 208),
    ("PLAN-20260504-010", "SELL", "2026-05-04T11:39:18Z", -201),
    ("PLAN-20260504-012", "SELL", "2026-05-04T15:20:06Z", 209),
    ("PLAN-20260505-005", "BUY",  "2026-05-05T14:28:44Z", -75),
    ("PLAN-20260506-002", "BUY",  "2026-05-06T00:33:52Z", 78),
    ("PLAN-20260506-006", "BUY",  "2026-05-06T05:56:43Z", 25),
    ("PLAN-20260506-004", "BUY",  "2026-05-06T02:18:54Z", -88),
]

with mt5_lock:
    if not mt5.initialize():
        print(f"MT5 initialize failed: {mt5.last_error()}")
        sys.exit(1)

    sym = "XAUUSD"
    if not mt5.symbol_select(sym, True):
        print(f"symbol_select({sym}) failed: {mt5.last_error()}")
        sys.exit(1)
    info = mt5.symbol_info(sym)
    print(f"symbol={sym}, MT5 connected, digits={info.digits if info else '?'}")
    print()
    print(f'{"plan":<22} {"dir":<5} {"pnl":>5}  {"snap_imp":>9}  {"candle_imp":>11}  {"M5_ATR":>8}  {"agree?":>7}  pattern (last 12 M5)')
    print("=" * 170)

    results = []
    for pid, direction, ts_str, pnl in fired:
        ts_utc = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        ts_naive = ts_utc.replace(tzinfo=None)
        rates = mt5.copy_rates_from(sym, mt5.TIMEFRAME_M5, ts_naive, 30)
        if rates is None or len(rates) < 14:
            print(f"{pid}: insufficient M5 history (got {0 if rates is None else len(rates)})")
            continue

        bars = []
        for r in rates:
            bars.append({
                "time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            })

        last12 = bars[-12:]
        prior14 = bars[-26:-12] if len(bars) >= 26 else bars[:-12]

        if len(prior14) >= 2:
            trs = []
            for i, b in enumerate(prior14):
                if i == 0:
                    trs.append(b["high"] - b["low"])
                else:
                    pc = prior14[i - 1]["close"]
                    trs.append(max(b["high"] - b["low"], abs(b["high"] - pc), abs(b["low"] - pc)))
            m5_atr = statistics.mean(trs)
        else:
            m5_atr = statistics.mean([b["high"] - b["low"] for b in last12])

        target_pos = (direction == "BUY")
        pattern_chars = []
        for b in last12:
            body = b["close"] - b["open"]
            same_dir = (body > 0) == target_pos
            magnitude_ok = abs(body) >= 0.5 * m5_atr
            if same_dir and magnitude_ok:
                pattern_chars.append("+")
            elif same_dir:
                pattern_chars.append(".")
            elif body == 0:
                pattern_chars.append("o")
            else:
                pattern_chars.append("-")

        # Metric A: trailing strict impulses (>=0.5*ATR)
        candle_impulse = 0
        for ch in reversed(pattern_chars):
            if ch == "+":
                candle_impulse += 1
            else:
                break
        # Metric B: trailing same-direction bars (any magnitude — includes ".")
        candle_drift = 0
        for ch in reversed(pattern_chars):
            if ch in ("+", "."):
                candle_drift += 1
            else:
                break
        # Metric C: count of impulses anywhere in the 12-bar window (not just trailing)
        impulse_total = sum(1 for ch in pattern_chars if ch == "+")
        same_dir_total = sum(1 for ch in pattern_chars if ch in ("+", "."))

        snap = snapshot_results.get(pid, {})
        snap_imp = snap.get("impulse", None)
        if snap_imp is None:
            agree = "?"
        elif abs(candle_impulse - snap_imp) <= 1:
            agree = "YES"
        else:
            agree = "no"

        cls = "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "FLAT")
        results.append({
            "pid": pid, "dir": direction, "pnl": pnl, "cls": cls,
            "snap_imp": snap_imp, "candle_impulse": candle_impulse,
            "candle_drift": candle_drift,
            "impulse_total": impulse_total, "same_dir_total": same_dir_total,
            "m5_atr_pips": m5_atr * 10,
            "agree": agree, "pattern": "".join(pattern_chars),
        })
        print(f'{pid:<22} {direction:<5} {pnl:>+5} {cls:<5}  snap={str(snap_imp):>3}  impulse={candle_impulse:>2}  drift={candle_drift:>2}  imp_tot={impulse_total:>2}  same_tot={same_dir_total:>2}  ATR={m5_atr*10:>5.1f}p  [{"".join(pattern_chars)}]')

    mt5.shutdown()

with open("data/_audits/_breakout_track4_candles.pkl", "wb") as f:
    pickle.dump(results, f)

print()
print("=== AGREEMENT ANALYSIS (snapshot vs candle, ±1 tolerance) ===")
agreed = [r for r in results if r["agree"] == "YES"]
disagreed = [r for r in results if r["agree"] == "no"]
print(f"agreed: {len(agreed)}/{len(results)} ({100*len(agreed)/len(results):.0f}%)")
print(f"disagreed: {len(disagreed)}")
for r in disagreed:
    print(f"  {r['pid']}: snap={r['snap_imp']} candle_impulse={r['candle_impulse']}  pattern={r['pattern']}")

print()
print("=== DISCRIMINATOR: each candle metric, winners vs losers ===")
for metric in ["candle_impulse", "candle_drift", "impulse_total", "same_dir_total"]:
    print(f"\n--- {metric} ---")
    for label, group in [
        ("WIN", [r for r in results if r["cls"] == "WIN"]),
        ("LOSS", [r for r in results if r["cls"] == "LOSS"]),
        ("FLAT", [r for r in results if r["cls"] == "FLAT"]),
    ]:
        if not group:
            continue
        vals = [r[metric] for r in group]
        print(f"  {label} (n={len(group)}): median={statistics.median(vals):.1f}  mean={statistics.mean(vals):.2f}  range=[{min(vals)}, {max(vals)}]")

print()
print("=== Filter test: same_dir_total (drift count over full 12 M5 bars) ===")
for thr in [4, 5, 6, 7, 8]:
    pf = [r for r in results if r["same_dir_total"] >= thr]
    ff = [r for r in results if r["same_dir_total"] < thr]
    pw = sum(1 for r in pf if r["cls"] == "WIN"); pl = sum(1 for r in pf if r["cls"] == "LOSS")
    fw = sum(1 for r in ff if r["cls"] == "WIN"); fl = sum(1 for r in ff if r["cls"] == "LOSS")
    print(f"  same_dir>={thr}: PASS n={len(pf)}, {pw}W/{pl}L, pips={sum(r['pnl'] for r in pf):+d}  |  FAIL n={len(ff)}, {fw}W/{fl}L, pips={sum(r['pnl'] for r in ff):+d}")

print()
print("=== Filter test: impulse_total (count of >=0.5*ATR impulses anywhere in 12 bars) ===")
for thr in [1, 2, 3, 4]:
    pf = [r for r in results if r["impulse_total"] >= thr]
    ff = [r for r in results if r["impulse_total"] < thr]
    pw = sum(1 for r in pf if r["cls"] == "WIN"); pl = sum(1 for r in pf if r["cls"] == "LOSS")
    fw = sum(1 for r in ff if r["cls"] == "WIN"); fl = sum(1 for r in ff if r["cls"] == "LOSS")
    print(f"  impulse_total>={thr}: PASS n={len(pf)}, {pw}W/{pl}L, pips={sum(r['pnl'] for r in pf):+d}  |  FAIL n={len(ff)}, {fw}W/{fl}L, pips={sum(r['pnl'] for r in ff):+d}")
