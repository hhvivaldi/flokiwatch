"""Rule 20 validation for Bug C Commit 1 - _fetch_live_price_from_executor helper."""
import sys, os
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

# ---------------------------------------------------------------
# Test A - executor returns valid tuple (real market bid != ask)
# ---------------------------------------------------------------
print("Test A - executor returns valid tuple")
exec_a = _FakeExec(ret=(4800.10, 4800.25))
tools = _make_tools(exec_a)
r = tools._fetch_live_price_from_executor()
check("returned dict (not None)", r is not None, detail=f"got {r}")
if r:
    check("bid == 4800.10", r["bid"] == 4800.10, detail=f"got bid={r['bid']}")
    check("ask == 4800.25", r["ask"] == 4800.25, detail=f"got ask={r['ask']}")
    check("spread == 1.5 pips ((ask-bid)/0.1)",
          abs(r["spread"] - 1.5) < 1e-9, detail=f"got spread={r['spread']}")
    check("bid != ask in result", r["bid"] != r["ask"])
check("executor called exactly once", exec_a.call_count == 1, detail=f"count={exec_a.call_count}")

# ---------------------------------------------------------------
# Test B - executor returns None (MT5 unavailable / symbol invalid)
# ---------------------------------------------------------------
print("\nTest B - executor returns None (MT5 unavailable)")
exec_b = _FakeExec(ret=None)
tools = _make_tools(exec_b)
try:
    r = tools._fetch_live_price_from_executor()
    check("returns None", r is None, detail=f"got {r}")
    check("no exception raised", True)
except Exception as e:
    check("no exception raised", False, detail=f"raised: {type(e).__name__}: {e}")

# ---------------------------------------------------------------
# Test C - executor raises exception
# ---------------------------------------------------------------
print("\nTest C - executor raises exception")
exec_c = _FakeExec(raise_on_call=True)
tools = _make_tools(exec_c)
try:
    r = tools._fetch_live_price_from_executor()
    check("returns None on exception", r is None, detail=f"got {r}")
    check("exception swallowed (not propagated)", True)
except Exception as e:
    check("exception swallowed", False, detail=f"raised: {type(e).__name__}: {e}")

# ---------------------------------------------------------------
# Test D - executor returns bid == ask (helper does not second-guess MT5)
# ---------------------------------------------------------------
print("\nTest D - executor returns bid==ask (pure passthrough)")
exec_d = _FakeExec(ret=(4800.10, 4800.10))
tools = _make_tools(exec_d)
r = tools._fetch_live_price_from_executor()
check("returns dict (no second-guess)", r is not None, detail=f"got {r}")
if r:
    check("bid == ask preserved verbatim", r["bid"] == 4800.10 and r["ask"] == 4800.10)
    check("spread == 0.0 (direct formula, not fabricated)",
          r["spread"] == 0.0, detail=f"got spread={r['spread']}")

# ---------------------------------------------------------------
# Test E - helper is pure: does not touch cache
# ---------------------------------------------------------------
print("\nTest E - helper does not touch any cache path")
exec_e = _FakeExec(ret=(4801.11, 4801.23))
tools = _make_tools(exec_e)

_cache_calls = [0]
_extract_calls = [0]
_orig_cache = tools._last_agent_data
_orig_extract = tools._extract_price_from_cache

def _spy_cache(*a, **kw):
    _cache_calls[0] += 1
    return _orig_cache(*a, **kw)
def _spy_extract(*a, **kw):
    _extract_calls[0] += 1
    return _orig_extract(*a, **kw)

tools._last_agent_data = _spy_cache
tools._extract_price_from_cache = _spy_extract

r = tools._fetch_live_price_from_executor()
check("executor.get_current_price was called", exec_e.call_count == 1)
check("_last_agent_data was NOT called", _cache_calls[0] == 0, detail=f"count={_cache_calls[0]}")
check("_extract_price_from_cache was NOT called",
      _extract_calls[0] == 0, detail=f"count={_extract_calls[0]}")
check("helper returned a valid live dict",
      r is not None and r["bid"] != r["ask"], detail=f"got {r}")

# ---------------------------------------------------------------
# Bonus — executor attribute is None (bot init incomplete)
# ---------------------------------------------------------------
print("\nBonus - executor is None")
tools_none = _make_tools(None)
r = tools_none._fetch_live_price_from_executor()
check("returns None when self._executor is None", r is None, detail=f"got {r}")

# ---------------------------------------------------------------
# Bonus — executor returns malformed tuple (length != 2)
# ---------------------------------------------------------------
print("\nBonus - executor returns malformed tuple")
exec_mal = _FakeExec(ret=(4800.10,))  # single-element
tools = _make_tools(exec_mal)
r = tools._fetch_live_price_from_executor()
check("returns None on malformed tuple (len=1)", r is None, detail=f"got {r}")

exec_mal3 = _FakeExec(ret=(4800.10, 4800.25, 4800.17))  # three-element
tools = _make_tools(exec_mal3)
r = tools._fetch_live_price_from_executor()
check("returns None on malformed tuple (len=3)", r is None, detail=f"got {r}")

# ---------------------------------------------------------------
# Bonus — executor returns non-numeric values
# ---------------------------------------------------------------
print("\nBonus - executor returns non-numeric tuple")
exec_nan = _FakeExec(ret=("not-a-number", 4800.25))
tools = _make_tools(exec_nan)
r = tools._fetch_live_price_from_executor()
check("returns None on non-numeric bid", r is None, detail=f"got {r}")

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
