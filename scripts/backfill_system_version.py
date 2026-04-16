"""FLO-328 one-time backfill: tag every existing trade_conditions/*.json
with system_version='pre_FLO-327' so they can be optionally included in
lessons by appending 'pre_FLO-327' to config.LESSONS_CURRENT_ERA_SHAS.

Default behavior after running: these trades are tagged but NOT in the
current era list — they won't appear in lessons unless Hermano opts in.
"""
import json, os, sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONDITIONS_DIR = "data/trade_conditions"
TAG = "pre_FLO-327"

if not os.path.isdir(CONDITIONS_DIR):
    print(f"ERROR: {CONDITIONS_DIR} not found")
    sys.exit(1)

tagged = skipped_existing = skipped_error = 0
for fn in sorted(os.listdir(CONDITIONS_DIR)):
    if not fn.endswith(".json"):
        continue
    path = os.path.join(CONDITIONS_DIR, fn)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        print(f"  SKIP {fn}: parse failed ({e})")
        skipped_error += 1
        continue
    if "system_version" in d:
        skipped_existing += 1
        continue
    d["system_version"] = TAG
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, default=str)
        os.replace(tmp, path)
        tagged += 1
    except Exception as e:
        print(f"  SKIP {fn}: write failed ({e})")
        skipped_error += 1

print(f"\nTagged {tagged} file(s) with system_version='{TAG}'")
print(f"Already-tagged (skipped): {skipped_existing}")
print(f"Errors: {skipped_error}")
print()
print(f"To optionally INCLUDE these legacy trades in lessons:")
print(f"  config.LESSONS_CURRENT_ERA_SHAS = ['1205fd4', '{TAG}']")
