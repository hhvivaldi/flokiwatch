"""FLO-322 Phase 1 investigation — READ-ONLY SL frequency analysis.

Run: python scripts/_investigations/flo322_phase1_sl_analysis.py
Output: tables + CSV/JSON to data/_audits/flo322/
"""
import sqlite3, json, os, sys, statistics as stat
from datetime import datetime, timedelta, timezone

os.makedirs('data/_audits/flo322', exist_ok=True)
conn = sqlite3.connect('data/history.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

now = datetime.now(timezone.utc)
cut_90 = (now - timedelta(days=90)).isoformat()
print(f"Analysis window: {cut_90} -> {now.isoformat()}")

cur.execute("""SELECT ticket, direction, open_price, close_price, sl, tp, profit,
                      close_reason, open_time, close_time, mfe_points, mae_points,
                      decision_source, comment
               FROM trades
               WHERE open_time >= ? AND close_time IS NOT NULL
               ORDER BY open_time""", (cut_90,))
rows = [dict(r) for r in cur.fetchall()]
print(f"Closed trades (90d): {len(rows)}")

# Compute sl_pips per trade — XAU 1 pip = 0.1
for r in rows:
    if r['sl'] is None or r['open_price'] is None:
        r['sl_pips'] = None
        continue
    dist = abs(r['open_price'] - r['sl'])
    r['sl_pips'] = round(dist / 0.1, 1)

# tp_pips too
for r in rows:
    if r['tp'] is None or r['open_price'] is None:
        r['tp_pips'] = None
        continue
    dist = abs(r['tp'] - r['open_price'])
    r['tp_pips'] = round(dist / 0.1, 1)

# distribution of sl_pips
sl_vals = [r['sl_pips'] for r in rows if r['sl_pips'] is not None]
print()
print(f"SL pips stats (n={len(sl_vals)}):")
if sl_vals:
    print(f"  mean={stat.mean(sl_vals):.1f} median={stat.median(sl_vals):.1f} stdev={stat.stdev(sl_vals):.1f}")
    print(f"  min={min(sl_vals):.1f} max={max(sl_vals):.1f}")
    q = sorted(sl_vals)
    print(f"  p10={q[int(0.10*len(q))]:.1f} p25={q[int(0.25*len(q))]:.1f} p75={q[int(0.75*len(q))]:.1f} p90={q[int(0.90*len(q))]:.1f}")

# Bucket SL
buckets = [
    ("<=10",   lambda s: s is not None and s <= 10),
    ("11-20",  lambda s: s is not None and 10 < s <= 20),
    ("21-50",  lambda s: s is not None and 20 < s <= 50),
    ("51-100", lambda s: s is not None and 50 < s <= 100),
    (">100",   lambda s: s is not None and s > 100),
]

print()
print(f"{'Bucket':10s} {'n':>4s} {'wins':>5s} {'losses':>7s} {'WR%':>6s} {'avg_PnL$':>10s} {'avg_MFE_pts':>13s} {'stopout%':>10s} {'avg_tp_pips':>13s}")
print("-" * 90)
summary = []
for label, pred in buckets:
    bucket = [r for r in rows if pred(r['sl_pips'])]
    n = len(bucket)
    if n == 0:
        print(f"{label:10s} {n:>4d}")
        summary.append({'bucket':label,'n':0})
        continue
    wins = sum(1 for r in bucket if (r['profit'] or 0) > 0)
    losses = sum(1 for r in bucket if (r['profit'] or 0) < 0)
    wr = 100 * wins / n if n else 0
    avg_pnl = sum(r['profit'] or 0 for r in bucket) / n
    mfes = [r['mfe_points'] for r in bucket if r['mfe_points'] is not None]
    avg_mfe = sum(mfes)/len(mfes) if mfes else None
    stopouts = sum(1 for r in bucket if (r['close_reason'] or '').strip().lower() == 'stop loss')
    stopout_pct = 100 * stopouts / n if n else 0
    tps = [r['tp_pips'] for r in bucket if r['tp_pips'] is not None]
    avg_tp = sum(tps)/len(tps) if tps else None
    print(f"{label:10s} {n:>4d} {wins:>5d} {losses:>7d} {wr:>5.1f} {avg_pnl:>10.2f} {(avg_mfe or 0):>13.1f} {stopout_pct:>9.1f}% {(avg_tp or 0):>13.1f}")
    summary.append({
        'bucket':label, 'n':n, 'wins':wins, 'losses':losses, 'wr':round(wr,1),
        'avg_pnl':round(avg_pnl,2), 'avg_mfe_pts':round(avg_mfe,1) if avg_mfe else None,
        'stopout_pct':round(stopout_pct,1), 'avg_tp_pips':round(avg_tp,1) if avg_tp else None,
    })

# Save summary + raw rows
with open('data/_audits/flo322/sl_buckets_summary.json','w') as f:
    json.dump(summary, f, indent=2)

# MFE capture % — for winners only, how much of MFE was captured as profit
# capture_pct = (close P&L in pips) / MFE pips, only for winning trades
winners = [r for r in rows if (r['profit'] or 0) > 0 and r['mfe_points']]
print()
print(f"\nMFE capture analysis (winners only, n={len(winners)}):")
for label, pred in buckets:
    bw = [r for r in winners if pred(r['sl_pips'])]
    if not bw:
        continue
    # Close P&L in pips = (close - open)/0.1 for BUY, (open - close)/0.1 for SELL
    caps = []
    for r in bw:
        if r['close_price'] is None or r['open_price'] is None:
            continue
        if r['direction'] == 'BUY':
            close_pips = (r['close_price'] - r['open_price']) / 0.1
        else:
            close_pips = (r['open_price'] - r['close_price']) / 0.1
        if r['mfe_points']:
            caps.append(100 * close_pips / r['mfe_points'])
    if caps:
        print(f"  {label:10s} winners={len(bw)}  avg_capture_pct={sum(caps)/len(caps):.1f}%")

# Specifically tight SL (<=15 pips)
tight = [r for r in rows if r['sl_pips'] is not None and r['sl_pips'] <= 15]
print(f"\nTight SL count (<=15 pips, 90d): {len(tight)}")
tight_win = sum(1 for r in tight if (r['profit'] or 0) > 0)
print(f"  WR: {100*tight_win/len(tight) if tight else 0:.1f}%  wins={tight_win}/{len(tight)}")
tight_so = sum(1 for r in tight if (r['close_reason'] or '').lower() == 'stop loss')
print(f"  Stop-out rate: {100*tight_so/len(tight) if tight else 0:.1f}%")

# Temporal split — last 30 days vs 31-60 vs 61-90
cut_30 = (now - timedelta(days=30)).isoformat()
cut_60 = (now - timedelta(days=60)).isoformat()
print()
print("Temporal split (sl_pips<=15):")
for label, start, end in [('last 30d', cut_30, now.isoformat()),
                          ('30-60d', cut_60, cut_30),
                          ('60-90d', cut_90, cut_60)]:
    bucket = [r for r in rows if start <= r['open_time'] < end and r['sl_pips'] is not None and r['sl_pips'] <= 15]
    alltr = [r for r in rows if start <= r['open_time'] < end]
    print(f"  {label:10s}  tight={len(bucket)}/{len(alltr)}  ({100*len(bucket)/len(alltr) if alltr else 0:.1f}%)")

# Save row-level csv
import csv
with open('data/_audits/flo322/trades_90d_sl_analysis.csv','w',newline='',encoding='utf-8') as f:
    w = csv.writer(f)
    w.writerow(['ticket','open_time','direction','open_price','sl','sl_pips','tp','tp_pips','close_price','close_reason','profit','mfe_points','mae_points'])
    for r in rows:
        w.writerow([r['ticket'],r['open_time'],r['direction'],r['open_price'],r['sl'],r['sl_pips'],r['tp'],r['tp_pips'],r['close_price'],r['close_reason'],r['profit'],r['mfe_points'],r['mae_points']])
print(f"\nSaved {len(rows)} rows to data/_audits/flo322/trades_90d_sl_analysis.csv")
