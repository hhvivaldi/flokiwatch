"""Rule 20 validation for FLO-316 — Rex Monitor prescriptive-label removal.

Tests A-F (pre-push). Test G is live post-restart and runs separately.

Target: Rex Monitor emits observational findings only. alert_level,
alert_context, alert_hint, per-finding severity/implication/source all gone.
Simba wake now gates on findings_count >= 2.
"""
import sys, os, json, re
sys.path.insert(0, ".")

passes = 0
fails = 0
def check(label, cond, detail=""):
    global passes, fails
    status = "PASS" if cond else "FAIL"
    if cond: passes += 1
    else:    fails += 1
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))

LEGACY_TOP_KEYS = ("alert_level", "alert_context", "alert_hint")
LEGACY_PER_FINDING_KEYS = ("severity", "implication", "source")

# =========================================================================
# Test A — rex_monitor.json does NOT contain alert_level/alert_context/alert_hint
# =========================================================================
print("Test A - rex_monitor.json has no alert_* top-level keys")
from rex_monitor import run_rex_monitor, MONITOR_FILE, _classify_findings, _classify_alert_level

class _StubTools:
    """Minimal stub — each rex_* returns a simple payload that triggers findings."""
    def rex_divergence_scan(self):
        return {"success": True, "divergences": {
            "H4": {"rsi": "bearish", "rsi_value": 55.0, "macd_divergence": "none"},
            "D1": {"rsi": "none", "rsi_value": 48.0, "macd_divergence": "none"},
        }}
    def rex_correlation_check(self):
        return {"success": True, "correlations": {
            "gold_dxy":    {"correlation":  0.24, "normal": (-0.6, -0.3), "status": "BROKEN"},
            "gold_silver": {"correlation":  0.85, "normal": (0.7, 0.95), "status": "NORMAL"},
            "gold_10y":    {"correlation": -0.55, "normal": (-0.7, -0.4), "status": "NORMAL"},
        }}
    def rex_regime_history(self):
        return {"success": True, "current_regime": "TRENDING_BEARISH", "duration_minutes": 5}
    def rex_session_performance(self):
        return {"success": True, "performance": {
            "asian":  {"buy": {"n": 8, "wr": 40.0, "pnl": 5.0}, "sell": {"n": 6, "wr": 50.0, "pnl": 2.0}},
            "london": {"buy": {"n": 17, "wr": 70.6, "pnl": 60.60}, "sell": {"n": 5, "wr": 0.0, "pnl": -30.0}},
            "ny":     {"buy": {"n": 4,  "wr": 50.0, "pnl": 1.0}, "sell": {"n": 3, "wr": 66.7, "pnl": 8.0}},
        }}

backup = MONITOR_FILE.read_text(encoding="utf-8") if MONITOR_FILE.exists() else None
try:
    scan_result = run_rex_monitor(_StubTools())
    check("run_rex_monitor returned dict", isinstance(scan_result, dict))
    check("rex_monitor.json file written", MONITOR_FILE.exists())
    if MONITOR_FILE.exists():
        loaded = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
        for k in LEGACY_TOP_KEYS:
            check(f"JSON has NO top-level '{k}'", k not in loaded,
                  detail=f"value={loaded.get(k)!r}" if k in loaded else "")
finally:
    if backup is not None:
        MONITOR_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test B — rex_monitor.json DOES contain findings_count + findings[].{type,observation,data}
# =========================================================================
print("\nTest B - JSON has observational-only finding schema")
backup = MONITOR_FILE.read_text(encoding="utf-8") if MONITOR_FILE.exists() else None
try:
    scan_result = run_rex_monitor(_StubTools())
    loaded = json.loads(MONITOR_FILE.read_text(encoding="utf-8"))
    check("JSON has 'findings_count'", "findings_count" in loaded,
          detail=f"keys={list(loaded.keys())[:8]}")
    check("JSON has 'findings' list", isinstance(loaded.get("findings"), list))
    # Each finding must have {type, observation, data}
    for i, f in enumerate(loaded.get("findings", [])):
        check(f"findings[{i}] has 'type'", "type" in f)
        check(f"findings[{i}] has 'observation'", "observation" in f)
        check(f"findings[{i}] has 'data'", "data" in f)
        for lk in LEGACY_PER_FINDING_KEYS:
            check(f"findings[{i}] has NO '{lk}'", lk not in f,
                  detail=f"value={f.get(lk)!r}" if lk in f else "")
finally:
    if backup is not None:
        MONITOR_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test C — get_rex_monitor tool response matches new shape
# =========================================================================
print("\nTest C - get_rex_monitor tool response clean")
backup = MONITOR_FILE.read_text(encoding="utf-8") if MONITOR_FILE.exists() else None
try:
    run_rex_monitor(_StubTools())
    from types import SimpleNamespace
    from agent_tools import AgentTools
    _dummy = SimpleNamespace()
    tools = AgentTools(bot=None, executor=_dummy, safety_checks_module=_dummy, risk_manager_module=_dummy)
    tool_result = tools.get_rex_monitor()
    check("tool returned success", tool_result.get("success") is True,
          detail=f"got {tool_result}")
    summary = tool_result.get("monitor") or {}
    for k in LEGACY_TOP_KEYS:
        check(f"tool summary has NO '{k}'", k not in summary)
    check("tool summary has 'findings_count'", "findings_count" in summary)
    check("tool summary has 'findings' list", isinstance(summary.get("findings"), list))
finally:
    if backup is not None:
        MONITOR_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test D — agent_prompts.py Rex Monitor section grep for forbidden labels
# =========================================================================
print("\nTest D - agent_prompts.py Rex section clean of CRITICAL/ELEVATED/etc. labels")
with open("agent_prompts.py", encoding="utf-8") as f:
    ap_src = f.read()
# Find the Rex-related paragraph(s)
rex_paragraph_match = re.search(r"Rex.*alert.*|get_rex_monitor.*", ap_src)
# Generic check: the specific phrases from the old prompt are gone.
for phrase in ("alert_level", "alert_hint", "alert_context",
               "QUIET/NORMAL/ELEVATED/CRITICAL"):
    check(f"agent_prompts.py does not contain '{phrase}'",
          phrase not in ap_src,
          detail=f"still present" if phrase in ap_src else "")

# ai_agent.py tool description
with open("ai_agent.py", encoding="utf-8") as f:
    ai_src = f.read()
check("ai_agent.py tool desc does not advertise 'alert_level (QUIET/NORMAL/ELEVATED/CRITICAL)'",
      "QUIET/NORMAL/ELEVATED/CRITICAL" not in ai_src)

# =========================================================================
# Test E — Simba wake gates on findings_count threshold, not alert_level
# =========================================================================
print("\nTest E - Simba wake gated on findings_count >= 2")
with open("agent_monitor.py", encoding="utf-8") as f:
    am_src = f.read()
# Old gate removed
check("agent_monitor.py no longer checks alert_level == 'CRITICAL'",
      'alert_level") == "CRITICAL"' not in am_src)
# New gate present
check("agent_monitor.py reads findings_count",
      "findings_count" in am_src)
check("agent_monitor.py uses _fc >= 2 threshold",
      "_fc >= 2" in am_src)

# =========================================================================
# Test F — Dashboard Rex card renders without crash on new schema
# =========================================================================
print("\nTest F - dashboard/server.py endpoint + trade_room.html null-safe on new schema")
with open("dashboard/server.py", encoding="utf-8") as f:
    srv_src = f.read()
check("dashboard/server.py no longer emits 'alert_level' in /api/rex-monitor",
      '"alert_level": monitor.get("alert_level"' not in srv_src)
check("dashboard/server.py emits 'findings_count'",
      '"findings_count":' in srv_src)
check("dashboard/server.py emits 'findings' list",
      '"findings": monitor.get("findings"' in srv_src)

with open("dashboard/static/trade_room.html", encoding="utf-8") as f:
    tr_src = f.read()
# The JS block that used to read m.alert_level was replaced with findings_count render.
# Verify the old colorMap prescription is gone inside the rex-monitor handler.
check("trade_room.html rex-monitor JS no longer uses (m.alert_level || 'QUIET')",
      "m.alert_level || 'QUIET'" not in tr_src,
      detail="Rex-card JS updated")

# =========================================================================
# Bonus — producer path: _classify_alert_level is gutted
# =========================================================================
print("\nBonus - _classify_alert_level is a no-op stub")
stub_out = _classify_alert_level([{"severity": "HIGH", "type": "CORRELATION_BREAK"}])
check("stub returns dict", isinstance(stub_out, dict))
check("stub level is empty",  stub_out.get("level") == "")
check("stub context is None", stub_out.get("context") is None)
check("stub hint is None",    stub_out.get("hint") is None)

# Bonus — _classify_findings doesn't emit legacy per-finding keys
print("\nBonus - _classify_findings emits observational-only shape")
fake_div = {"success": True, "divergences": {
    "H4": {"rsi": "bearish", "rsi_value": 55.0, "macd_divergence": "none"},
    "D1": {"rsi": "none", "rsi_value": 48.0, "macd_divergence": "none"},
}}
fake_corr = {"success": True, "correlations": {
    "gold_dxy": {"correlation": 0.24, "normal": (-0.6, -0.3), "status": "BROKEN"},
}}
fake_regime = {"success": True, "current_regime": "TRENDING_BEARISH", "duration_minutes": 5}
fake_perf = {"success": True, "performance": {}}
fs = _classify_findings(fake_div, fake_corr, fake_regime, fake_perf, prev_regime=None)
check(f"non-empty findings ({len(fs)})", len(fs) >= 2)
for i, f in enumerate(fs):
    for lk in LEGACY_PER_FINDING_KEYS:
        check(f"findings[{i}] has NO '{lk}' key", lk not in f)
    check(f"findings[{i}] type in new enum",
          f.get("type") in ("DIVERGENCE", "CORRELATION", "REGIME", "SESSION"),
          detail=f"got type={f.get('type')}")

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
