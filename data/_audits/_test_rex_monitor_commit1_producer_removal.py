"""Rule 20 validation for Rex Monitor reduction Commit 1 — remove Bull/Bear
debate injection of Rex Monitor findings (producer in main.py + consumer in
rex_validator._build_debate_context).

After this commit:
- main.py no longer populates _debate_data["rex_monitor"]
- rex_validator._build_debate_context no longer reads data["rex_monitor"]
- Bull/Bear user message contains no "Rex monitor (scanned Xm ago):" section
- rex_monitor.json file is still written by rex_monitor.run_rex_monitor
- load_rex_monitor() remains importable and functional for other consumers
"""
import sys, os, json, time
sys.path.insert(0, ".")

passes = 0
fails = 0
def check(label, cond, detail=""):
    global passes, fails
    status = "PASS" if cond else "FAIL"
    if cond: passes += 1
    else:    fails += 1
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))

# =========================================================================
# Test A — _build_debate_context emits NO Rex monitor section, even if a
# rex_monitor key is present in the incoming data dict (belt-and-braces:
# proves the consumer is gone, not just that main.py stopped sending it).
# =========================================================================
print("Test A - _build_debate_context ignores any rex_monitor key")
from rex_validator import _build_debate_context

data_with_monitor = {
    "price": 4790.0,
    "regime": "RANGING",
    "rex_monitor": {
        "findings": [
            {"type": "CORRELATION_BREAK", "detail": "Gold-10Y correlation +0.28 (normal: -0.5) - BROKEN"},
            {"type": "SESSION_WARNING",    "detail": "London SELL: 0% WR over 5 trades"},
        ],
        "alert_level": "ELEVATED",
        "age_minutes": 5,
    },
}
msg_with = _build_debate_context(data_with_monitor)
check("returns a non-empty string", isinstance(msg_with, str) and len(msg_with) > 0,
      detail=f"len={len(msg_with) if isinstance(msg_with, str) else 'N/A'}")
check("NO 'Rex monitor' substring", "Rex monitor" not in msg_with)
check("NO 'scanned' substring (part of old '(scanned Xm ago)' line)", "scanned" not in msg_with)
check("NO 'CORRELATION_BREAK' finding leaked",
      "CORRELATION_BREAK" not in msg_with)
check("NO '[SESSION_WARNING]' bracket-type finding leaked",
      "[SESSION_WARNING]" not in msg_with)

# =========================================================================
# Test B — run_bull_bear_debate context-building survives a data dict with
# no rex_monitor key (simulating the post-commit main.py state). Also
# verifies that _build_debate_context returns the 'No data available.'
# sentinel string when parts is empty (preserving existing fallback).
# =========================================================================
print("\nTest B - Debate context-builder survives without rex_monitor key")
data_no_monitor = {
    "price": 4790.0,
    "regime": "RANGING",
    "indicators": {"rsi_14": 55.0, "adx_14": 18.0},
}
try:
    msg_no = _build_debate_context(data_no_monitor)
    check("no KeyError / AttributeError raised", True)
    check("msg is a string", isinstance(msg_no, str))
    check("NO 'Rex monitor' in output", "Rex monitor" not in msg_no)
except Exception as e:
    check("no exception raised", False, detail=f"raised: {type(e).__name__}: {e}")

# Empty data dict → sentinel
msg_empty = _build_debate_context({})
check("empty data returns 'No data available.' sentinel",
      msg_empty == "No data available.",
      detail=f"got: {msg_empty[:80]}")

# =========================================================================
# Test C — load_rex_monitor() is still importable and callable (other
# consumers: agent_tools.get_rex_monitor, agent_monitor Simba wake path,
# dashboard endpoint — these must all still work).
# =========================================================================
print("\nTest C - load_rex_monitor() still importable and callable")
from rex_monitor import load_rex_monitor  # import must succeed
result = load_rex_monitor()
check("load_rex_monitor imported and callable", True)
# Result is either None (file missing/malformed) or a dict with expected keys
if result is None:
    check("returns None gracefully when file missing/malformed", True,
          detail="current file absent or empty — acceptable")
else:
    check("returns dict with 'timestamp' key",
          isinstance(result, dict) and "timestamp" in result,
          detail=f"keys={list(result.keys())[:6]}")
    check("returns dict with 'findings' key",
          isinstance(result, dict) and "findings" in result)

# =========================================================================
# Test D — rex_monitor.json writer path preserved. We verify that
# run_rex_monitor(tools) writes the file with the expected core schema.
# Use a stub AgentTools that returns successful dummy outputs for the
# four unique rex tools so the writer reaches the file-write step.
# =========================================================================
print("\nTest D - rex_monitor.json writer path preserved (Commit 3 will change schema, not this commit)")

class _StubTools:
    """Minimal stub for rex_monitor.run_rex_monitor - returns success stubs."""
    def rex_divergence_scan(self):
        return {"success": True, "divergences": {
            "H4": {"rsi": "none", "rsi_value": 50.0, "macd_divergence": "none", "bars_analyzed": 20},
            "D1": {"rsi": "none", "rsi_value": 55.0, "macd_divergence": "none", "bars_analyzed": 20},
        }}
    def rex_correlation_check(self):
        return {"success": True, "correlations": {
            "gold_dxy":    {"correlation": -0.45, "normal": -0.6, "status": "NORMAL"},
            "gold_silver": {"correlation":  0.85, "normal":  0.85, "status": "NORMAL"},
            "gold_10y":    {"correlation":  0.28, "normal": -0.5, "status": "BROKEN"},
        }}
    def rex_regime_history(self):
        return {"success": True, "current_regime": "RANGING", "duration_minutes": 45}
    def rex_session_performance(self):
        return {"success": True, "performance": {
            "asian":  {"buy": {"n": 8, "wr": 40.0, "pnl": 5.0}, "sell": {"n": 6, "wr": 50.0, "pnl": 2.0}},
            "london": {"buy": {"n": 17, "wr": 70.6, "pnl": 60.60}, "sell": {"n": 5, "wr": 0.0, "pnl": -30.0}},
            "ny":     {"buy": {"n": 4,  "wr": 50.0, "pnl": 1.0}, "sell": {"n": 3, "wr": 66.7, "pnl": 8.0}},
        }}

from rex_monitor import run_rex_monitor, MONITOR_FILE
mtime_before = os.path.getmtime(MONITOR_FILE) if os.path.exists(MONITOR_FILE) else 0
# Preserve real file (so live bot's state isn't clobbered by test)
backup = None
if os.path.exists(MONITOR_FILE):
    backup = MONITOR_FILE.read_text(encoding="utf-8")
try:
    scan_result = run_rex_monitor(_StubTools())
    check("run_rex_monitor returned a dict", isinstance(scan_result, dict),
          detail=f"type={type(scan_result).__name__}")
    check("rex_monitor.json written (file exists after call)",
          os.path.exists(MONITOR_FILE))
    if os.path.exists(MONITOR_FILE):
        mtime_after = os.path.getmtime(MONITOR_FILE)
        check("file mtime advanced", mtime_after > mtime_before,
              detail=f"before={mtime_before} after={mtime_after}")
        # Parse and check schema still has the core fields (alert_level etc.
        # are kept in this commit — they're removed in Commit 3)
        parsed = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
        for k in ("timestamp", "findings", "finding_count"):
            check(f"schema key '{k}' present", k in parsed)
finally:
    # Restore original file so tests don't disturb production state
    if backup is not None:
        MONITOR_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Bonus — static check: the two removed code paths no longer contain the
# offending block. Belt-and-braces regression guard against accidental
# revert.
# =========================================================================
print("\nBonus - source-level verification that removed blocks are gone")
with open("main.py", encoding="utf-8") as f:
    main_src = f.read()
with open("rex_validator.py", encoding="utf-8") as f:
    val_src = f.read()

check("main.py no longer calls load_rex_monitor in debate path",
      "_debate_data[\"rex_monitor\"]" not in main_src,
      detail="[producer removed]")
check("rex_validator.py no longer reads data.get('rex_monitor')",
      'data.get("rex_monitor")' not in val_src,
      detail="[consumer removed]")
check("rex_validator.py no longer emits 'Rex monitor (scanned'",
      "Rex monitor (scanned" not in val_src)

# Non-regression: load_rex_monitor is still defined
check("rex_monitor.load_rex_monitor remains defined (other consumers)",
      "def load_rex_monitor" in open("rex_monitor.py", encoding="utf-8").read())

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
