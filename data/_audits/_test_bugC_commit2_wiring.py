"""Rule 20 validation for Bug C Commit 2 - wiring helper into _extract_price_from_cache."""
import sys
sys.path.insert(0, ".")
from types import SimpleNamespace

passes = 0
fails = 0
def check(label, cond, detail=""):
    global passes, fails
    status = "PASS" if cond else "FAIL"
    if cond: passes += 1
    else:    fails += 1
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))

from agent_tools import AgentTools

class _FakeExec:
    def __init__(self, ret=None, raise_on_call=False):
        self._ret = ret
        self._raise = raise_on_call
        self.call_count = 0
    def get_current_price(self):
        self.call_count += 1
        if self._raise:
            raise RuntimeError("simulated MT5 failure")
        return self._ret

_dummy = SimpleNamespace()

def _make_tools(executor):
    return AgentTools(
        bot=None,
        executor=executor,
        safety_checks_module=_dummy,
        risk_manager_module=_dummy,
    )

def _make_dp(bid, ask, spread):
    """Construct a fake agent data package with the given price fields."""
    return {
        "timestamp": "2026-04-20T20:30:00Z",
        "current_price": {
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "timestamp": "2026-04-20T20:30:00Z",
        },
    }

# ---------------------------------------------------------------
# Test A — Cache has bid == ask (the production bug state)
# ---------------------------------------------------------------
print("Test A - Cache bid==ask triggers live refresh")
exec_a = _FakeExec(ret=(4800.10, 4800.25))
tools = _make_tools(exec_a)
dp = _make_dp(bid=4800.0, ask=4800.0, spread=0.0)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid overwritten to 4800.10", r["bid"] == 4800.10, detail=f"got {r['bid']}")
    check("ask overwritten to 4800.25", r["ask"] == 4800.25, detail=f"got {r['ask']}")
    check("spread ~= 1.5 pips", abs(r["spread"] - 1.5) < 1e-6, detail=f"got {r['spread']}")
    check("bid != ask after refresh", r["bid"] != r["ask"])
check("executor called exactly once", exec_a.call_count == 1, detail=f"count={exec_a.call_count}")

# ---------------------------------------------------------------
# Test B — Healthy cache: no live call, values preserved
# ---------------------------------------------------------------
print("\nTest B - Healthy cache does NOT trigger live refresh")
exec_b = _FakeExec(ret=(9999.99, 9999.99))  # canary; should never be read
tools = _make_tools(exec_b)
dp = _make_dp(bid=4800.10, ask=4800.25, spread=1.5)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid unchanged = 4800.10", r["bid"] == 4800.10, detail=f"got {r['bid']}")
    check("ask unchanged = 4800.25", r["ask"] == 4800.25, detail=f"got {r['ask']}")
    check("spread unchanged = 1.5",  r["spread"] == 1.5, detail=f"got {r['spread']}")
check("executor NOT called (cache was healthy)",
      exec_b.call_count == 0, detail=f"count={exec_b.call_count}")

# ---------------------------------------------------------------
# Test C — Cache is bug state + executor returns None
# ---------------------------------------------------------------
print("\nTest C - Bug cache + executor None -> pass cache through")
exec_c = _FakeExec(ret=None)
tools = _make_tools(exec_c)
dp = _make_dp(bid=4800.0, ask=4800.0, spread=0.0)
try:
    r = tools._extract_price_from_cache(dp)
    check("no exception raised", True)
    check("returned dict", r is not None, detail=f"got {r}")
    if r:
        check("bid unchanged = 4800.0",  r["bid"] == 4800.0, detail=f"got {r['bid']}")
        check("ask unchanged = 4800.0",  r["ask"] == 4800.0, detail=f"got {r['ask']}")
        check("spread unchanged = 0.0",  r["spread"] == 0.0, detail=f"got {r['spread']}")
    check("executor called once (attempted refresh)",
          exec_c.call_count == 1, detail=f"count={exec_c.call_count}")
except Exception as e:
    check("no exception raised", False, detail=f"raised: {type(e).__name__}: {e}")

# ---------------------------------------------------------------
# Test D — Cache is bug state + executor raises exception
# ---------------------------------------------------------------
print("\nTest D - Bug cache + executor raises -> pass cache through")
exec_d = _FakeExec(raise_on_call=True)
tools = _make_tools(exec_d)
dp = _make_dp(bid=4800.0, ask=4800.0, spread=0.0)
try:
    r = tools._extract_price_from_cache(dp)
    check("no exception propagated", True)
    check("returned dict", r is not None, detail=f"got {r}")
    if r:
        check("bid unchanged = 4800.0",  r["bid"] == 4800.0, detail=f"got {r['bid']}")
        check("ask unchanged = 4800.0",  r["ask"] == 4800.0, detail=f"got {r['ask']}")
        check("spread unchanged = 0.0",  r["spread"] == 0.0, detail=f"got {r['spread']}")
except Exception as e:
    check("no exception propagated", False, detail=f"raised: {type(e).__name__}: {e}")

# ---------------------------------------------------------------
# Test E — Cache has spread is None explicitly -> trigger fires
# ---------------------------------------------------------------
print("\nTest E - Cache spread=None triggers live refresh")
exec_e = _FakeExec(ret=(4800.10, 4800.30))
tools = _make_tools(exec_e)
dp = _make_dp(bid=4800.10, ask=4800.25, spread=None)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid overwritten to 4800.10", r["bid"] == 4800.10, detail=f"got {r['bid']}")
    check("ask overwritten to 4800.30", r["ask"] == 4800.30, detail=f"got {r['ask']}")
    check("spread ~= 2.0 pips",          abs(r["spread"] - 2.0) < 1e-6, detail=f"got {r['spread']}")
check("executor called exactly once", exec_e.call_count == 1, detail=f"count={exec_e.call_count}")

# ---------------------------------------------------------------
# Test F — Cache has negative spread -> trigger fires (spread <= 0.0)
# ---------------------------------------------------------------
print("\nTest F - Cache spread<0 triggers live refresh (defensive)")
exec_f = _FakeExec(ret=(4800.10, 4800.30))
tools = _make_tools(exec_f)
dp = _make_dp(bid=4800.10, ask=4800.25, spread=-0.5)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid overwritten to 4800.10", r["bid"] == 4800.10, detail=f"got {r['bid']}")
    check("ask overwritten to 4800.30", r["ask"] == 4800.30, detail=f"got {r['ask']}")
    check("spread ~= 2.0 pips (negative replaced)",
          abs(r["spread"] - 2.0) < 1e-6, detail=f"got {r['spread']}")
check("executor called exactly once", exec_f.call_count == 1, detail=f"count={exec_f.call_count}")

# ---------------------------------------------------------------
# Bonus — Cache bid==ask + live also returns bid==ask (MT5 glitch) -> pass through
# ---------------------------------------------------------------
print("\nBonus - Live refresh also returns bid==ask -> pass cache through")
exec_g = _FakeExec(ret=(4800.50, 4800.50))  # live also collapsed (rare)
tools = _make_tools(exec_g)
dp = _make_dp(bid=4800.0, ask=4800.0, spread=0.0)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid unchanged = 4800.0 (live rejected)",
          r["bid"] == 4800.0, detail=f"got {r['bid']}")
    check("ask unchanged = 4800.0 (live rejected)",
          r["ask"] == 4800.0, detail=f"got {r['ask']}")
    check("spread unchanged = 0.0 (live rejected)",
          r["spread"] == 0.0, detail=f"got {r['spread']}")

# ---------------------------------------------------------------
# Bonus — Missing bid/ask still returns None (unchanged behavior)
# ---------------------------------------------------------------
print("\nBonus - Missing bid/ask still returns None (regression guard)")
exec_h = _FakeExec(ret=(4800.10, 4800.25))
tools = _make_tools(exec_h)
dp = {"current_price": {"bid": None, "ask": None, "spread": 0.0}}
r = tools._extract_price_from_cache(dp)
check("returns None when cache bid/ask missing",
      r is None, detail=f"got {r}")
check("executor NOT called (early return before trigger)",
      exec_h.call_count == 0, detail=f"count={exec_h.call_count}")

# ---------------------------------------------------------------
# Bonus — spread is None + executor None -> original recompute fires
# ---------------------------------------------------------------
print("\nBonus - spread=None + executor unavailable -> original recompute")
exec_i = _FakeExec(ret=None)
tools = _make_tools(exec_i)
dp = _make_dp(bid=4800.10, ask=4800.25, spread=None)
r = tools._extract_price_from_cache(dp)
check("returned dict", r is not None, detail=f"got {r}")
if r:
    check("bid unchanged", r["bid"] == 4800.10, detail=f"got {r['bid']}")
    check("ask unchanged", r["ask"] == 4800.25, detail=f"got {r['ask']}")
    check("spread recomputed ~= 1.5 pips ((ask-bid)/0.1)",
          abs(r["spread"] - 1.5) < 1e-6, detail=f"got {r['spread']}")

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
