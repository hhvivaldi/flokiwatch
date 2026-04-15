"""End-of-day report for Apr 15, 2026."""
import sqlite3, json, re, sys
from collections import Counter, defaultdict
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

DAY = "2026-04-15"
LOG = f"logs/trading_bot_{DAY}.log"

conn = sqlite3.connect('data/history.db')
conn.row_factory = sqlite3.Row

# -------------------------------------------------------------------
# SECTION 1-4: TRADES
# -------------------------------------------------------------------
print("=" * 72)
print(f"  END OF DAY — {DAY}")
print("=" * 72)
print("\n### TRADES ###\n")

# Pull all trades opened or closed today
trades = conn.execute(f"""
    SELECT ticket, direction, open_price, close_price, sl, tp, profit,
           open_time, close_time, comment, close_reason,
           mfe_points, mae_points, volume
    FROM trades
    WHERE open_time LIKE '{DAY}%' OR close_time LIKE '{DAY}%'
    ORDER BY open_time ASC
""").fetchall()

# For confidence@entry, find the OPEN_* cycle within 10 min of open
openings = conn.execute(f"""
    SELECT timestamp, agent_decision, agent_confidence
    FROM agent_proactive_analyses
    WHERE agent_decision IN ('OPEN_BUY','OPEN_SELL')
      AND timestamp LIKE '{DAY}%'
    ORDER BY timestamp ASC
""").fetchall()

def _parse(ts):
    if not ts: return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '').split('.')[0])
    except Exception:
        return None

def _conf_at_entry(direction, open_time):
    want = f"OPEN_{direction.upper()}"
    t_t = _parse(open_time)
    if not t_t: return None
    best = None; best_delta = None
    for o in openings:
        if o['agent_decision'] != want: continue
        t_o = _parse(o['timestamp'])
        if not t_o: continue
        delta = abs((t_t - t_o).total_seconds())
        if delta > 600: continue  # 10 min window
        if best_delta is None or delta < best_delta:
            best_delta = delta; best = o['agent_confidence']
    return best

total_pnl = 0.0; wins = 0; losses = 0
best_trade = None; worst_trade = None
rows_out = []
for t in trades:
    pnl = float(t['profit'] or 0)
    if t['close_price'] is None:
        continue  # still open
    total_pnl += pnl
    if pnl > 0: wins += 1
    elif pnl < 0: losses += 1
    if best_trade is None or pnl > best_trade['profit']: best_trade = dict(t)
    if worst_trade is None or pnl < worst_trade['profit']: worst_trade = dict(t)

    # Duration
    o_dt = _parse(t['open_time']); c_dt = _parse(t['close_time'])
    dur_min = round((c_dt - o_dt).total_seconds() / 60, 0) if (o_dt and c_dt) else None

    # Close reason classification
    cr = t['close_reason'] or ''
    comment = t['comment'] or ''
    if cr == 'TP': label = 'TP'
    elif cr == 'SL': label = 'SL'
    elif 'Bot-Close' in comment or cr == 'MANUAL_CLOSE' or 'close_trade' in comment.lower(): label = 'Floki'
    else: label = cr or 'EA'

    conf = _conf_at_entry(t['direction'], t['open_time'])
    rows_out.append({
        'ticket': t['ticket'], 'dir': t['direction'],
        'entry': t['open_price'], 'close': t['close_price'],
        'pnl': pnl, 'conf': conf, 'dur': dur_min, 'reason': label,
    })

print(f"{'#':<11} {'DIR':<4} {'ENTRY':>8} {'CLOSE':>8} {'P&L':>8} {'CONF':>5} {'DUR':>6} {'REASON':<10}")
for r in rows_out:
    c = f"{r['conf']}%" if r['conf'] is not None else "—"
    d = f"{int(r['dur'])}m" if r['dur'] is not None else "—"
    print(f"{r['ticket']:<11} {r['dir']:<4} {r['entry']:>8.2f} {r['close']:>8.2f} "
          f"{r['pnl']:>+7.2f}$ {c:>5} {d:>6} {r['reason']:<10}")

print(f"\n  Total P&L: ${total_pnl:+.2f}   Wins: {wins}  Losses: {losses}  WR: {wins/(wins+losses)*100:.1f}%" if (wins+losses) else f"\n  Total P&L: ${total_pnl:+.2f}")
if best_trade:
    print(f"  Best:  #{best_trade['ticket']} {best_trade['direction']} ${best_trade['profit']:+.2f}")
if worst_trade:
    print(f"  Worst: #{worst_trade['ticket']} {worst_trade['direction']} ${worst_trade['profit']:+.2f}")

# FLO-326: count ACTUAL entries by direction × entry-type from the trades
# table. Replaces the old "OPEN_BUY/OPEN_SELL decision count" approach,
# which undercounted pending-order fills (those cycles carry a WAIT
# decision because place_pending_order was called, not execute_trade).
# Source of truth is the trade row itself — direction + comment field.
_dir_counts = {"BUY": {"market": 0, "pending": 0}, "SELL": {"market": 0, "pending": 0}}
_entries = conn.execute(f"""
    SELECT ticket, direction, comment FROM trades
    WHERE open_time LIKE '{DAY}%' AND ticket != 0
""").fetchall()
for t in _entries:
    d = (t["direction"] or "").upper()
    if d not in _dir_counts: continue
    cmt = (t["comment"] or "").lower()
    et = "pending" if "pending" in cmt else "market"
    _dir_counts[d][et] += 1

print(f"\n  Entries by direction (from trades table, not decision cycles):")
for d in ("BUY", "SELL"):
    total = _dir_counts[d]["market"] + _dir_counts[d]["pending"]
    print(f"    {d}: {total} (market: {_dir_counts[d]['market']}, pending: {_dir_counts[d]['pending']})")

# -------------------------------------------------------------------
# SECTION 5-8: DECISIONS
# -------------------------------------------------------------------
print("\n### DECISIONS ###\n")

cycles = conn.execute(f"""
    SELECT timestamp, agent_decision, agent_confidence, raw_response, tool_trace, input_tokens, output_tokens
    FROM agent_proactive_analyses
    WHERE timestamp LIKE '{DAY}%'
    ORDER BY timestamp ASC
""").fetchall()

dec_counter = Counter()
dec_conf_sum = defaultdict(list)
for c in cycles:
    d = c['agent_decision']
    dec_counter[d] += 1
    if c['agent_confidence'] is not None:
        dec_conf_sum[d].append(c['agent_confidence'])

print(f"  Total cycles today: {len(cycles)}")
print(f"  Decision distribution + avg confidence:")
for d, n in dec_counter.most_common():
    confs = dec_conf_sum[d]
    avg_c = f"{sum(confs)/len(confs):.1f}%" if confs else "—"
    print(f"    {d:<15}  {n:>4}  avg_conf={avg_c}")

# Pending orders — from log
import subprocess
def _grep(pattern):
    try:
        r = subprocess.run(['grep', '-cE', pattern, LOG], capture_output=True, text=True)
        return int(r.stdout.strip() or '0')
    except Exception: return 0

po_placed = _grep('PENDING_ORDER \| PLACED')
po_filled = _grep('PENDING_ORDER \| FILL_DETECTED')
po_cancel_single = _grep('PENDING_ORDER \| CANCELLED ticket=')
po_cancel_all = _grep('PENDING_ORDER \| CANCEL_ALL')
po_expired = _grep('PENDING_ORDER \| EXPIRED|pending.*expired')

print(f"\n  Pending orders: placed={po_placed}  filled={po_filled}  "
      f"cancelled_single={po_cancel_single}  cancel_all_batches={po_cancel_all}  expired={po_expired}")

# -------------------------------------------------------------------
# SECTION 9-13: TOOL USAGE
# -------------------------------------------------------------------
print("\n### TOOL USAGE ###\n")

tool_counter = Counter()
tf_counter = Counter()
plan_followed = Counter()

for c in cycles:
    tt = c['tool_trace'] or ''
    try:
        trace = json.loads(tt) if tt else []
    except Exception:
        trace = []
    for e in trace if isinstance(trace, list) else []:
        if isinstance(e, dict):
            nm = e.get('tool') or e.get('name')
            if nm:
                tool_counter[nm] += 1
                # Chart TFs
                if 'chart_screenshot' in nm:
                    args = e.get('args') or e.get('arguments') or {}
                    if isinstance(args, str):
                        try: args = json.loads(args)
                        except Exception: args = {}
                    if isinstance(args, dict):
                        for tf in (args.get('timeframes') or []):
                            tf_counter[str(tf).upper()] += 1
    # followed_plan
    try:
        rj = json.loads(c['raw_response'] or '{}')
        fp = (rj.get('data_needs') or {}).get('followed_plan') or ''
        plan_followed[fp or '(empty)'] += 1
    except Exception:
        pass

total_tool_calls = sum(tool_counter.values())
print(f"  Total tool calls: {total_tool_calls}")
print(f"  Top 10 tools:")
for t, n in tool_counter.most_common(10):
    print(f"    {t:<28} {n}")

# New tools specifically
vp = tool_counter.get('get_volume_profile', 0)
tp = tool_counter.get('get_tick_pressure', 0)
cs = tool_counter.get('get_chart_screenshots', 0)
print(f"\n  New tools usage: get_volume_profile={vp}  get_tick_pressure={tp}")
print(f"  get_chart_screenshots: {cs} calls")
if tf_counter:
    print(f"  Chart timeframes invoked: {dict(tf_counter)}")
else:
    print(f"  Chart TFs: (args not captured in tool_trace for older entries)")

print(f"\n  followed_plan distribution:")
for k, v in plan_followed.most_common():
    print(f"    {k:<20} {v}")

# -------------------------------------------------------------------
# SECTION 14-15: COSTS
# -------------------------------------------------------------------
print("\n### COSTS ###\n")

total_cost = 0.0; cost_n = 0
try:
    with open(LOG, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if 'FLOKI |' in line and 'cost=$' in line:
                m = re.search(r'cost=\$([\d.]+)', line)
                if m:
                    total_cost += float(m.group(1))
                    cost_n += 1
except Exception as e:
    print(f"  (log read failed: {e})")
avg_cost = (total_cost / cost_n) if cost_n else 0
print(f"  Total Qwen cost: ${total_cost:.4f}  ({cost_n} cycles)")
print(f"  Avg cost / cycle: ${avg_cost:.4f}")

# Total input/output tokens
in_tokens = sum((c['input_tokens'] or 0) for c in cycles)
out_tokens = sum((c['output_tokens'] or 0) for c in cycles)
print(f"  Total tokens: in={in_tokens:,}  out={out_tokens:,}")

# -------------------------------------------------------------------
# SECTION 16: OVERNIGHT SELL
# -------------------------------------------------------------------
print("\n### NOTABLE EVENTS ###\n")

# Trade #1592474728 — the +$56.26 SELL
r = conn.execute("""
    SELECT ticket, direction, open_price, close_price, profit, mfe_points, mae_points, open_time, close_time
    FROM trades WHERE ticket = 1592474728
""").fetchone()
if r:
    pnl_pips = (float(r['open_price']) - float(r['close_price'])) / 0.1
    cap = round(pnl_pips / float(r['mfe_points']) * 100, 1) if r['mfe_points'] else None
    print(f"  #1592474728 (overnight SELL): {r['open_price']} → {r['close_price']} = ${r['profit']:+.2f}")
    print(f"    MFE={r['mfe_points']}p MAE={r['mae_points']}p  capture={cap}%")
    print(f"    open={r['open_time']}  close={r['close_time']}")

# Errors / fallbacks
print(f"\n  Errors / fallbacks today:")
print(f"    JSON parse failures:        {_grep('Failed to parse Agent response as JSON')}")
print(f"    FLOKI timeouts:             {_grep('FLOKI.*timeout')}")
print(f"    OpenRouter fallbacks fired: {_grep('OPENROUTER.*fallback|Alibaba.*cooldown')}")
print(f"    Luna 451 blocks:            {_grep('LUNA.*451.*Liga')}")
print(f"    Luna Gemini JSON invalid:   {_grep('LUNA.*Gemini returned invalid JSON')}")
print(f"    Luna local_fallback fires:  {_grep('LUNA.*local_fallback|LUNA: local fallback')}")
print(f"    Luna MiMo success cycles:   {_grep('LUNA: MiMo response in')}")
print(f"    Luna MiMo recovered:        {_grep('LUNA . MiMo recovered')}")
print(f"    Echo Gemini fallback OK:    {_grep('ECHO . Gemini fallback OK')}")
print(f"    Agent checklist MISSING:    {_grep('AGENT_CHECKLIST . MISSING')}")

# Approximate Luna on-MiMo fraction
mimo_ok = _grep('LUNA: MiMo response in')
local_fb = _grep('LUNA: local fallback')
total_luna = mimo_ok + local_fb
if total_luna:
    print(f"\n  Luna uptime: MiMo {mimo_ok}/{total_luna} = {mimo_ok/total_luna*100:.0f}%  "
          f"local_fallback {local_fb}/{total_luna} = {local_fb/total_luna*100:.0f}%")
