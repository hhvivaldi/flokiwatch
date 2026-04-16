"""FLO-327 rebuild — wipe trade_lessons.json and replay every real trade
through the now-deduped extract_trade_lesson(). Before/after diff.

Does NOT modify trade_conditions/ or history.db — read-only source of truth.
"""
import json, os, shutil, sqlite3, sys
from datetime import datetime
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trade_lessons import extract_trade_lesson, LESSONS_FILE, CONDITIONS_DIR

# Snapshot current file
before_path = LESSONS_FILE
backup_path = LESSONS_FILE + ".pre_flo327.bak"
if os.path.exists(before_path):
    shutil.copy2(before_path, backup_path)
    with open(before_path, 'r', encoding='utf-8') as f:
        before = {l['bucket']: l for l in json.load(f)}
    print(f"Backed up current lessons → {backup_path}")
else:
    before = {}

# Wipe
with open(LESSONS_FILE, 'w', encoding='utf-8') as f:
    f.write('[]')
print("Wiped trade_lessons.json")

# Replay every trade that has both a trade_conditions file AND a DB row
conn = sqlite3.connect('data/history.db')
conn.row_factory = sqlite3.Row

processed = 0
skipped_no_cond = 0
skipped_no_profit = 0
for row in conn.execute(
    "SELECT ticket, direction, profit FROM trades WHERE profit IS NOT NULL ORDER BY open_time ASC"
).fetchall():
    t = row['ticket']
    if t == 0:
        continue
    cond_path = os.path.join(CONDITIONS_DIR, f"{t}.json")
    if not os.path.exists(cond_path):
        skipped_no_cond += 1
        continue
    result = extract_trade_lesson(t)
    if result is None:
        skipped_no_profit += 1
        continue
    processed += 1

conn.close()

print(f"\nReplayed: {processed}  skipped(no_cond_file): {skipped_no_cond}  skipped(no_profit): {skipped_no_profit}")

# Diff
with open(LESSONS_FILE, 'r', encoding='utf-8') as f:
    after = {l['bucket']: l for l in json.load(f)}

print("\n" + "=" * 110)
print("BEFORE (corrupted) vs AFTER (rebuilt)")
print("=" * 110)
all_buckets = sorted(set(before.keys()) | set(after.keys()))
print(f"Total buckets: before={len(before)} after={len(after)}")
print()
print(f"{'BUCKET':<55} {'BEFORE (o/w/l/avg/label)':<35} {'AFTER (o/w/l/avg/label)':<35}")
print("-" * 125)
for bk in all_buckets:
    b = before.get(bk, {})
    a = after.get(bk, {})
    def _fmt(d):
        if not d: return "— (gone)"
        label = (d.get('lesson','') or '').split(':')[0].strip()
        return f"{d.get('occurrences',0)}/{d.get('wins',0)}/{d.get('losses',0)}/{d.get('avg_pnl','?'):+.2f}/{label}"
    print(f"{bk[:55]:<55} {_fmt(b):<35} {_fmt(a):<35}")

# Specifically call out AVOID → NEUTRAL/gone transitions
print("\n--- Classification changes ---")
for bk in all_buckets:
    b_label = (before.get(bk, {}).get('lesson','') or '').split(':')[0].strip()
    a_label = (after.get(bk, {}).get('lesson','') or '').split(':')[0].strip() if bk in after else "(gone)"
    if b_label != a_label:
        print(f"  {bk[:60]:<60}  {b_label or '(new)':<11} → {a_label}")
