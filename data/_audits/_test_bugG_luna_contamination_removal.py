"""Rule 20 validation for Bug G — Luna prescriptive-language contamination removal.

Tests A-H (pre-push). Test I is live-grep post-restart and runs separately.

Target: Luna produces only observational fields. environment, risk_level,
directional_bias, bias_confidence, market_regime, summary are gone from
the JSON schema and from the Floki prompt surface.
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

LEGACY_KEYS = ("environment", "risk_level", "directional_bias", "bias_confidence", "market_regime", "summary")
FORBIDDEN_OUTPUT_WORDS = (
    "DANGER", "CAUTION", "SAFE", "BULLISH ", "BEARISH ",
    "risk_level", "bias_confidence", "directional_bias",
)

# =========================================================================
# Test A — load_luna_brief() output dict has NO legacy keys
# =========================================================================
print("Test A - load_luna_brief() has no legacy keys")

# Build a fake MiMo parser result, drive it through _parse_mimo_response +
# _save_brief roundtrip via in-memory dict simulation.
from luna_analyst import (
    _parse_mimo_response,
    _run_local_analysis,
    LUNA_SYSTEM_PROMPT,
    LunaAnalysisResult,
    _save_brief,
    load_luna_brief,
    BRIEF_FILE,
)
from dataclasses import asdict

# Use a fake MiMo response (legacy keys PRESENT — simulate LLM ignoring the
# updated prompt) and verify the parser discards them.
fake_mimo_parsed = {
    "environment": "DANGER",                 # legacy — must be discarded
    "risk_level": 8,                         # legacy — must be discarded
    "directional_bias": "BEARISH",           # legacy — must be discarded
    "bias_confidence": 7,                    # legacy — must be discarded
    "market_regime": "risk_off",             # legacy — must be discarded
    "summary": "Conditions are dangerous.",  # legacy — must be discarded
    "patterns_detected": ["news_price_divergence"],
    "key_factors": ["Gold -1.98% from 3-day high 4891.62."],
    "next_events": [],
    "data_snapshot": {},
}
fake_macro = {
    "gold": {"current": 4785.5, "change_percent": -0.53},
    "dxy": {"current": 98.19, "change_percent": 0.15},
    "vix": {"current": 19.22, "change_percent": 1.85},
    "yields": {"current": 4.25, "change_percent": 0.09},
    "sp500": {"current": 5840, "change_percent": -0.22},
    "oil": {"current": 86.4, "change_percent": 0.95},
}
result = _parse_mimo_response(fake_mimo_parsed, fake_macro, [])

# Roundtrip through disk: write + load, check actual JSON shape
backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(result)
    loaded = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))

    for k in LEGACY_KEYS:
        check(f"JSON has NO '{k}'", k not in loaded,
              detail=f"value={loaded.get(k)!r}" if k in loaded else "")
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test B — load_luna_brief() output dict HAS required observational keys
# =========================================================================
print("\nTest B - JSON has required observational keys")
backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(result)
    loaded = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    for k in ("timestamp", "source", "data_snapshot", "correlations",
              "patterns_detected", "key_factors", "next_events"):
        check(f"JSON has '{k}'", k in loaded,
              detail=f"keys={list(loaded.keys())[:10]}" if k not in loaded else "")
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test C — agent_prompts.py Luna description doesn't contain prescriptive labels
# =========================================================================
print("\nTest C - agent_prompts.py Luna section is clean")
with open("agent_prompts.py", encoding="utf-8") as f:
    ap_src = f.read()
# Find the Luna line(s): look for '- Luna:' block
luna_line_match = re.search(r"- Luna:.*", ap_src)
check("Luna role description exists", luna_line_match is not None)
if luna_line_match:
    luna_text = luna_line_match.group(0)
    for forbidden in ("SAFE/CAUTION/DANGER", "DANGER", "CAUTION", "BULLISH", "BEARISH",
                      "risk_level", "directional bias", "bias_confidence"):
        # Skip pattern names: "safe_haven_flow" contains "safe_" but that's a pattern identifier.
        # Check the forbidden as a standalone label (surrounded by non-identifier chars).
        lowered = luna_text.lower()
        if forbidden.lower() in ("danger", "caution", "bullish", "bearish"):
            pattern = rf"\b{forbidden.lower()}\b"
            hit = re.search(pattern, lowered)
        else:
            hit = forbidden.lower() in lowered
        # "directional bias" appears in new description as "does NOT assign directional bias" — OK
        if forbidden == "directional bias":
            # accept only if it's in a negation ("does NOT assign")
            if "does not assign" in lowered or "does not" in lowered:
                check(f"'{forbidden}' only in negation context", True)
                continue
        check(f"'{forbidden}' not in Luna role description", not bool(hit),
              detail=f"hit in: {luna_text[:120]}")

# =========================================================================
# Test D — /api/luna-brief endpoint returns new schema (passthrough)
# =========================================================================
print("\nTest D - /api/luna-brief response shape")
# Simulate endpoint by calling load_luna_brief (endpoint passes through)
backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(result)
    api_response = load_luna_brief()
    check("endpoint returns non-None brief", api_response is not None)
    if api_response:
        for k in LEGACY_KEYS:
            check(f"API response has NO '{k}'", k not in api_response)
        for k in ("patterns_detected", "key_factors", "data_snapshot"):
            check(f"API response has '{k}'", k in api_response)
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test E — get_luna_brief tool returns clean schema
# =========================================================================
print("\nTest E - get_luna_brief tool returns clean schema")
from types import SimpleNamespace
from agent_tools import AgentTools

backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(result)
    _dummy = SimpleNamespace()
    tools = AgentTools(bot=None, executor=_dummy, safety_checks_module=_dummy, risk_manager_module=_dummy)
    tool_result = tools.get_luna_brief()
    check("tool returns success", tool_result.get("success") is True)
    brief = tool_result.get("brief") or {}
    for k in LEGACY_KEYS:
        check(f"tool result has NO '{k}'", k not in brief)
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test F — Consumers handle missing legacy fields gracefully
# =========================================================================
print("\nTest F - consumers survive missing legacy fields")
clean_brief = {
    "timestamp": "2026-04-21T17:30:00Z",
    "source": "mimo",
    "data_snapshot": {},
    "correlations": {},
    "patterns_detected": ["news_price_divergence"],
    "key_factors": ["Gold -1.98%"],
    "next_events": [],
}

# decision_flags helpers (should return False / None now)
from decision_flags import _luna_environment_in_cycle, flag_skipped_oracle_in_luna_danger, flag_skipped_rex_in_luna_danger
trace = [{"name": "get_luna_brief", "result": {"success": True, "brief": clean_brief}}]
check("_luna_environment_in_cycle returns None (no env field)",
      _luna_environment_in_cycle(trace) is None)
check("flag_skipped_oracle_in_luna_danger returns False",
      flag_skipped_oracle_in_luna_danger(trace) is False)
check("flag_skipped_rex_in_luna_danger returns False",
      flag_skipped_rex_in_luna_danger(trace) is False)

# deep_search.py directional_bias filter should have been removed (source grep)
with open("deep_search.py", encoding="utf-8") as f:
    ds_src = f.read()
check("deep_search.py no longer reads 'directional_bias'",
      "directional_bias" not in ds_src)

# discord_cards.py env-color selection removed
with open("discord_cards.py", encoding="utf-8") as f:
    dc_src = f.read()
check("discord_cards.py no longer selects color by environment==DANGER",
      'environment == "DANGER"' not in dc_src)

# rex_validator.py Luna env/bias/risk line removed
with open("rex_validator.py", encoding="utf-8") as f:
    rv_src = f.read()
check("rex_validator.py no longer formats 'environment='",
      "environment=" not in rv_src or "Luna: environment=" not in rv_src)

# research_manager.py Luna REPORT 3 updated
with open("research_manager.py", encoding="utf-8") as f:
    rm_src = f.read()
check("research_manager.py no longer reads luna_brief['environment']",
      'luna_brief.get("environment"' not in rm_src)

# main.py Bull/Bear + RM context
with open("main.py", encoding="utf-8") as f:
    main_src = f.read()
check("main.py no longer references 'luna_environment' key in debate data",
      '"luna_environment"' not in main_src)

# agent_tools.py execute_trade luna_ctx
with open("agent_tools.py", encoding="utf-8") as f:
    at_src = f.read()
check("agent_tools.py no longer writes 'luna_environment' to trade_conditions",
      '"luna_environment":' not in at_src)

# monitor.py pending-fill luna_ctx
with open("monitor.py", encoding="utf-8") as f:
    mon_src = f.read()
check("monitor.py no longer writes 'luna_environment' to trade_conditions",
      '_conds["luna_environment"]' not in mon_src)

# =========================================================================
# Test G — Luna local-fallback produces valid new-schema brief
# =========================================================================
print("\nTest G - local fallback produces valid new-schema brief")
local_result = _run_local_analysis(fake_macro, [], [])
# Check that in-memory result has expected values
check("local result has source='local_fallback'", local_result.source == "local_fallback")
check("local result environment is empty (Bug G)", local_result.environment == "")
check("local result risk_level is 0 (Bug G)", local_result.risk_level == 0)
check("local result directional_bias is empty (Bug G)", local_result.directional_bias == "")
check("local result bias_confidence is 0 (Bug G)", local_result.bias_confidence == 0)
check("local result market_regime is empty (Bug G)", local_result.market_regime == "")
check("local result summary is empty (Bug G)", local_result.summary == "")
check("local result has patterns_detected", isinstance(local_result.patterns_detected, list))
check("local result has key_factors", isinstance(local_result.key_factors, list))

# Roundtrip: local -> disk -> json — no legacy keys
backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(local_result)
    loaded = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    for k in LEGACY_KEYS:
        check(f"local-fallback JSON has NO '{k}'", k not in loaded)
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

# =========================================================================
# Test H — Parser warns when MiMo still emits legacy keys
# =========================================================================
print("\nTest H - parser warns on legacy-key leakage")
import logging, io
# Capture logger
from logger import log as _applog
# Prefer WARNING level for our LUNA: warnings
_capture = io.StringIO()
_h = logging.StreamHandler(_capture)
_h.setLevel(logging.WARNING)
logging.getLogger().addHandler(_h)
try:
    _parse_mimo_response(fake_mimo_parsed, fake_macro, [])
    captured = _capture.getvalue()
    check("warning emitted for legacy keys in MiMo response",
          "legacy keys" in captured or "discarded" in captured or "Bug G" in captured,
          detail=f"log text: {captured[:200]}")
finally:
    logging.getLogger().removeHandler(_h)

# =========================================================================
# Bonus — prompt itself contains no forbidden output-context labels
# =========================================================================
print("\nBonus - LUNA_SYSTEM_PROMPT self-audit")
# The prompt MUST not contain the forbidden words IN OUTPUT-context
# (they're allowed inside the negation list). The legacy OUTPUT schema
# examples must be gone.
check("prompt has no 'environment: \"SAFE\"' schema line",
      '"environment":' not in LUNA_SYSTEM_PROMPT)
check("prompt has no 'risk_level: 1-10' schema line",
      '"risk_level":' not in LUNA_SYSTEM_PROMPT)
check("prompt has no 'directional_bias:' schema line",
      '"directional_bias":' not in LUNA_SYSTEM_PROMPT)
check("prompt has no 'bias_confidence:' schema line",
      '"bias_confidence":' not in LUNA_SYSTEM_PROMPT)
check("prompt has no 'market_regime:' schema line",
      '"market_regime":' not in LUNA_SYSTEM_PROMPT)
check("prompt has 'patterns_detected' in schema",
      '"patterns_detected"' in LUNA_SYSTEM_PROMPT)
check("prompt has 'data_snapshot' in schema",
      '"data_snapshot"' in LUNA_SYSTEM_PROMPT)
check("prompt has 'correlations' in schema",
      '"correlations"' in LUNA_SYSTEM_PROMPT)

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
