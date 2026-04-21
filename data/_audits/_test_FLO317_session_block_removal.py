"""Rule 20 validation for FLO-317 — Session block removal from Floki prompt.

Tests J-M (pre-push). Test N is live post-restart and runs separately.

Target: agent_data_builder.py no longer emits <session> XML block, the
_format_session_context helper is gone, and the Floki prompt has no
references to today's W/L / session performance stats as forced injection.
<open_positions count=".."/> preserved (FLO-85 safety guard).
"""
import sys, os, re, inspect
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
# Test J - trigger_context build has NO session block and NO today_* fields
# =========================================================================
print("Test J - trigger_context output has no session data block")

from agent_data_builder import format_proactive_xml

# Fake data package that preserves shape
dp = {
    "timestamp": "2026-04-21T20:00:00Z",
    "current_price": {"bid": 4720.5, "ask": 4720.8, "spread_pips": 3.0},
    "indicators": {},
    "brain_analysis": {},
    "ml_predictions": {},
    "macro": {},
    "positions": [],
    "volatility": {"status": "NORMAL"},
    "sr_zones": [],
    "nearest_support": None,
    "nearest_resistance": None,
    "candlestick_patterns": {},
    "sr_proximity": {},
    "agent_memory": [],
    "trade_feedback": {},
    "delta_context": {},
    "portfolio": {},
    "regime_context": {},
    "session_memory": "",
}

xml_out = format_proactive_xml(dp)

for forbidden in ("<session ", "<today ", "today_trades", "today_wins",
                  "today_losses", "today_pnl", "last_5_results",
                  "consecutive_losses", "<session>", "</session>"):
    check(f"trigger_context has NO '{forbidden}'", forbidden not in xml_out,
          detail=f"still present" if forbidden in xml_out else "")

# =========================================================================
# Test K - <open_positions count=..> still rendered
# =========================================================================
print("\nTest K - open_positions count preserved (FLO-85 guard)")
check("trigger_context contains '<open_positions count='",
      "<open_positions count=" in xml_out,
      detail="missing!" if "<open_positions count=" not in xml_out else "")

# =========================================================================
# Test L - agent_prompts.py SYSTEM_PROMPT clean of caution language
# =========================================================================
print("\nTest L - agent_prompts.py free of forced-session caution language")
with open("agent_prompts.py", encoding="utf-8") as f:
    ap_src = f.read()

forbidden_phrases = (
    "session WR",
    "today's win rate",
    "today's performance",
    "wins today",
    "losses today",
    "session stats",
    "today's WR",
    "session so far",
)
for phrase in forbidden_phrases:
    check(f"agent_prompts.py has NO '{phrase}'",
          phrase not in ap_src,
          detail="still present" if phrase in ap_src else "")

# =========================================================================
# Test M - _format_session_context fully removed, signatures cleaned
# =========================================================================
print("\nTest M - producer-side cleanup")
with open("agent_data_builder.py", encoding="utf-8") as f:
    adb_src = f.read()

check("_format_session_context function removed",
      "def _format_session_context" not in adb_src)
check("no 'session_context: Dict' parameter in signatures",
      "session_context: Dict" not in adb_src)
check("no 'session = dp.get(\"session\"' lookup in render path",
      'session = dp.get("session"' not in adb_src)
check("no '_format_session_context(session_context)' call site",
      "_format_session_context(session_context)" not in adb_src)

# Signature-level inspection: build_data_package should no longer take session_context
from agent_data_builder import build_data_package, build_proactive_data_package
sig1 = inspect.signature(build_data_package)
sig2 = inspect.signature(build_proactive_data_package)
check("build_data_package signature free of 'session_context'",
      "session_context" not in sig1.parameters,
      detail=f"params={list(sig1.parameters.keys())}" if "session_context" in sig1.parameters else "")
check("build_proactive_data_package signature free of 'session_context'",
      "session_context" not in sig2.parameters,
      detail=f"params={list(sig2.parameters.keys())}" if "session_context" in sig2.parameters else "")

# =========================================================================
# Bonus - ensure unrelated price-period "session" string literal is still there
# =========================================================================
print("\nBonus - price-period 'session' string literal preserved (different concept)")
check("'period\": \"session\"' price-change label still exists",
      '"period": "session"' in adb_src)
check("'if period == \"session\"' branch still exists",
      'if period == "session"' in adb_src)

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
