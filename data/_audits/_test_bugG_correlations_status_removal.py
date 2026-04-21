"""Rule 20 validation for Bug G follow-up — remove per-pair status labels
from Luna correlations.

Tests J / K / L per Hermano's spec.
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

# =========================================================================
# Test J — Luna output correlations sub-objects have NO per-pair "status" key
# =========================================================================
print("Test J - per-pair correlations have no 'status' key")
from luna_analyst import _build_correlations, _classify_correlation, _build_correlation_context

# Stub the history loader so _build_correlations has enough data. Monkey-patch
# _load_macro_history to return 5 synthetic days.
import luna_analyst as _la
_orig_loader = _la._load_macro_history
def _fake_history():
    return {
        "2026-04-17": {"gold": 4800.0, "dxy": 98.0, "yields": 4.25, "sp500": 5800.0},
        "2026-04-18": {"gold": 4820.0, "dxy": 97.8, "yields": 4.27, "sp500": 5830.0},
        "2026-04-19": {"gold": 4850.0, "dxy": 97.5, "yields": 4.20, "sp500": 5850.0},
        "2026-04-20": {"gold": 4830.0, "dxy": 98.1, "yields": 4.30, "sp500": 5820.0},
        "2026-04-21": {"gold": 4785.0, "dxy": 98.2, "yields": 4.30, "sp500": 5790.0},
    }
_la._load_macro_history = _fake_history
try:
    corrs = _build_correlations({})
    check("top-level 'status' is 'ok'", corrs.get("status") == "ok",
          detail=f"got {corrs.get('status')}")
    # Per-pair: verify no 'status' key
    for name in ("gold_dxy", "gold_yields", "gold_sp500"):
        pair = corrs.get(name)
        check(f"{name} is a dict", isinstance(pair, dict))
        if isinstance(pair, dict):
            check(f"{name} has NO 'status' key", "status" not in pair,
                  detail=f"keys={list(pair.keys())}")
            check(f"{name} HAS 'value' key", "value" in pair)
            check(f"{name} HAS 'normal_range' key", "normal_range" in pair)
finally:
    _la._load_macro_history = _orig_loader

# _classify_correlation should still be callable but return empty
val_out = _classify_correlation(0.5, -0.9, -0.3)
check("_classify_correlation returns empty string (gutted)",
      val_out == "",
      detail=f"got {val_out!r}")

# =========================================================================
# Test K — correlation_context prompt builder no longer emits status labels
# =========================================================================
print("\nTest K - _build_correlation_context has no NORMAL/WEAK/BROKEN labels")
sample_correlations = {
    "status": "ok", "days": 5,
    "gold_dxy":    {"value": -0.28, "normal_range": [-0.9, -0.3]},
    "gold_yields": {"value":  0.24, "normal_range": [-0.8, -0.2]},
    "gold_sp500":  {"value":  0.58, "normal_range": [-0.5,  0.5]},
}
context = _build_correlation_context(sample_correlations)
check("context string non-empty", isinstance(context, str) and len(context) > 0,
      detail=f"got {context[:80]!r}")
for label in ("NORMAL", "WEAK", "BROKEN", "N/A"):
    check(f"context has NO '{label}' label", label not in context,
          detail=f"context preview: {context[:200]!r}")
# Must still report numbers
check("context contains raw values (-0.28)", "-0.28" in context)
check("context contains typical range tokens",
      "typical:" in context or "typical" in context)

# =========================================================================
# Test L — Consumers survive missing per-pair status field
# =========================================================================
print("\nTest L - consumers survive missing per-pair status")

# Source-level regression: no Python code reads correlations[x]["status"]
# as a per-pair field. (dashboard top-level 'status' is a different object.)
with open("luna_analyst.py", encoding="utf-8") as f:
    la_src = f.read()
# The only places that assign per-pair "status" were _build_correlations
# (just removed) and _build_correlation_context's reader (just removed).
# Verify no remaining line sets 'status' inside a correlation sub-dict.
pair_status_assigns = re.findall(r'"status"\s*:\s*"(NORMAL|WEAK|BROKEN|N/A)"', la_src)
check("no literal 'NORMAL'/'WEAK'/'BROKEN'/'N/A' status assignments remain",
      len(pair_status_assigns) == 0,
      detail=f"found: {pair_status_assigns}")

# Dashboard app.js — only reads top-level correlations.status (== "ok"); not
# per-pair. Verify.
with open("dashboard/static/app.js", encoding="utf-8") as f:
    js_src = f.read()
# Check that any reference to correlations.*.status (per-pair) doesn't exist
per_pair_status_reads = re.findall(r'correlations\.\w+\.status|correlations\[".*"\]\["status"\]', js_src)
# (allow top-level: correlations.status === "ok")
non_top_level = [m for m in per_pair_status_reads
                 if not m.startswith("correlations.status")]
check("dashboard/app.js does not read per-pair .status",
      len(non_top_level) == 0,
      detail=f"found: {non_top_level}")

# Roundtrip test: write a brief with new correlations shape, load, confirm
from luna_analyst import _save_brief, load_luna_brief, BRIEF_FILE, _parse_mimo_response
fake_mimo_parsed = {
    "patterns_detected": [],
    "key_factors": ["test"],
    "next_events": [],
    "data_snapshot": {},
}
fake_macro = {}
_la._load_macro_history = _fake_history
try:
    result = _parse_mimo_response(fake_mimo_parsed, fake_macro, [])
finally:
    _la._load_macro_history = _orig_loader

backup = BRIEF_FILE.read_text(encoding="utf-8") if BRIEF_FILE.exists() else None
try:
    _save_brief(result)
    loaded = json.loads(BRIEF_FILE.read_text(encoding="utf-8"))
    loaded_corrs = loaded.get("correlations", {})
    check("roundtrip: correlations top-level has 'status'", "status" in loaded_corrs)
    for name in ("gold_dxy", "gold_yields", "gold_sp500"):
        sub = loaded_corrs.get(name)
        if isinstance(sub, dict):
            check(f"roundtrip: {name} has NO 'status' key", "status" not in sub)
finally:
    if backup is not None:
        BRIEF_FILE.write_text(backup, encoding="utf-8")

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
