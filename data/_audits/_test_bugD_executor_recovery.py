"""Rule 20 validation for Bug D — executor stale-read recovery.

Tests executor.MT5Executor retry + reconnect + cooldown behavior by
monkey-patching the MetaTrader5 module inside executor's namespace.
"""
import sys, io, types
sys.path.insert(0, ".")

passes = 0
fails = 0
def check(label, cond, detail=""):
    global passes, fails
    status = "PASS" if cond else "FAIL"
    if cond: passes += 1
    else:    fails += 1
    print(f"  [{status}] {label}" + (f" -- {detail}" if detail else ""))

import executor as ex_mod
from executor import MT5Executor

# --- Fake AccountInfo object (duck-types mt5.account_info result) ------
class _FakeAccount:
    def __init__(self, balance=2188.04, login=52704729):
        self.login = login
        self.balance = balance
        self.equity = balance
        self.margin = 0.0
        self.margin_free = balance
        self.profit = 0.0
        self.leverage = 500
        self.currency = "USD"

# --- Fake mt5 module -----------------------------------------------------
class _FakeMT5:
    """Replaces the `mt5` reference inside executor module."""
    def __init__(self):
        self.account_info_queue = []       # list of return values to pop
        self.account_info_calls = 0
        self.initialize_return = True
        self.initialize_calls = 0
        self.last_error_return = (0, "OK")
        # terminal_info stub (used inside connect())
        class _TermInfo:
            path = "C:\\FakeTerminal"
            data_path = "C:\\FakeTerminal\\data"
        self._term = _TermInfo()

    def account_info(self):
        self.account_info_calls += 1
        if self.account_info_queue:
            return self.account_info_queue.pop(0)
        return _FakeAccount()  # healthy default

    def initialize(self, path=None):
        self.initialize_calls += 1
        return self.initialize_return

    def terminal_info(self):
        return self._term

    def last_error(self):
        return self.last_error_return

    def login(self, *a, **kw):
        return True

    def shutdown(self):
        pass

# --- Capture log messages --------------------------------------------------
class _LogCapture:
    def __init__(self):
        self.warnings = []
        self.infos = []
        self.errors = []
        self.debugs = []
        self.mt5_status_calls = []
    def warning(self, msg): self.warnings.append(str(msg))
    def info(self, msg):    self.infos.append(str(msg))
    def error(self, msg):   self.errors.append(str(msg))
    def debug(self, msg):   self.debugs.append(str(msg))
    def mt5_status(self, connected, msg=""):
        self.mt5_status_calls.append((bool(connected), str(msg)))
    def all_lines(self):
        return self.warnings + self.infos + self.errors + self.debugs

# --- Helper: build isolated executor with fakes ---------------------------
import time as _time
_sleep_calls = []
_real_sleep = _time.sleep
_real_time = _time.time
_time_now = [1_000_000.0]  # controllable monotonic source

def _fake_sleep(seconds):
    _sleep_calls.append(seconds)
    _time_now[0] += seconds  # advance virtual time

def _fake_time():
    return _time_now[0]

def _make_executor(fake_mt5, fake_log, fake_time=True):
    """Build executor with mt5/log replaced. Pre-sets connected=True to
    simulate post-startup state. Returns executor + cleanup fn."""
    orig_mt5 = ex_mod.mt5
    orig_log = ex_mod.log
    orig_time_module = ex_mod.time
    ex_mod.mt5 = fake_mt5
    ex_mod.log = fake_log
    if fake_time:
        # Replace time.sleep and time.time inside executor's module reference
        class _TimeStub:
            sleep = staticmethod(_fake_sleep)
            time = staticmethod(_fake_time)
        ex_mod.time = _TimeStub
    e = MT5Executor()
    e.connected = True  # simulate already-connected state
    e._last_reconnect_attempt = 0.0  # long-ago; cooldown expired
    def cleanup():
        ex_mod.mt5 = orig_mt5
        ex_mod.log = orig_log
        ex_mod.time = orig_time_module
    return e, cleanup

def _reset_clock():
    _sleep_calls.clear()
    _time_now[0] = 1_000_000.0

# =========================================================================
# Test A — transient None then success (1 retry)
# =========================================================================
print("Test A - transient None, resolves after 1 retry")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
fm.account_info_queue = [None, _FakeAccount(balance=2188.04)]
e, cleanup = _make_executor(fm, fl)
r = e.get_account_info()
check("returned dict (not None)", r is not None, detail=f"got {r}")
if r:
    check("balance=2188.04", r["balance"] == 2188.04, detail=f"got {r['balance']}")
check("mt5.account_info called 2x (original + 1 retry)",
      fm.account_info_calls == 2, detail=f"got {fm.account_info_calls}")
check("1 STALE_READ warning logged",
      sum(1 for m in fl.warnings if "STALE_READ" in m and "retry 1/3" in m) == 1,
      detail=f"warnings={fl.warnings}")
check("STALE_READ resolved info logged",
      sum(1 for m in fl.infos if "STALE_READ | resolved" in m) == 1,
      detail=f"infos={[m for m in fl.infos if 'STALE' in m]}")
check("self.connected stayed True", e.connected is True)
check("no reconnect attempted", fm.initialize_calls == 0)
cleanup()

# =========================================================================
# Test B — transient None x2 then success
# =========================================================================
print("\nTest B - transient None x2, resolves after 2 retries")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
fm.account_info_queue = [None, None, _FakeAccount()]
e, cleanup = _make_executor(fm, fl)
r = e.get_account_info()
check("returned dict", r is not None)
check("mt5.account_info called 3x", fm.account_info_calls == 3, detail=f"got {fm.account_info_calls}")
check("2 retry warnings logged",
      sum(1 for m in fl.warnings if "STALE_READ" in m and "retry" in m) == 2,
      detail=f"warnings={fl.warnings}")
check("resolved after retry 2",
      any("STALE_READ | resolved after retry 2" in m for m in fl.infos))
check("self.connected stayed True", e.connected is True)
check("no reconnect attempted", fm.initialize_calls == 0)
check("slept with backoff 100ms + 300ms",
      _sleep_calls == [0.1, 0.3], detail=f"got {_sleep_calls}")
cleanup()

# =========================================================================
# Test C — all retries fail, reconnect succeeds
# =========================================================================
print("\nTest C - 3 retries fail, reconnect succeeds, recovered")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
# 3 initial + 3 retries = 4 None returns, then reconnect's connect() does
# its own account_info call (returns healthy by default), then post-reconnect
# verify call returns healthy.
fm.account_info_queue = [None, None, None, None]  # original + 3 retries
fm.initialize_return = True
e, cleanup = _make_executor(fm, fl)
r = e.get_account_info()
check("returned dict after reconnect", r is not None, detail=f"got {r}")
check("mt5.initialize called exactly 1x (single reconnect)",
      fm.initialize_calls == 1, detail=f"got {fm.initialize_calls}")
check("RECONNECT attempt warning logged",
      any("RECONNECT | attempt" in m for m in fl.warnings))
check("RECONNECT success info logged",
      any("RECONNECT | success" in m for m in fl.infos))
check("mt5_status(True, 'recovered after reconnect') called",
      any(c[0] is True and "recovered" in c[1] for c in fl.mt5_status_calls),
      detail=f"calls={fl.mt5_status_calls}")
check("self.connected True", e.connected is True)
check("slept 100+300+900ms before reconnect",
      _sleep_calls[:3] == [0.1, 0.3, 0.9], detail=f"got {_sleep_calls}")
cleanup()

# =========================================================================
# Test D — all retries fail, reconnect also fails
# =========================================================================
print("\nTest D - retries fail, reconnect fails, latched with visible log")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
# Queue: 4 Nones for (original + 3 retries). After that, account_info_queue
# is empty so fake returns healthy default — BUT initialize_return=False
# forces connect() to return False, so we never reach the post-reconnect read.
fm.account_info_queue = [None, None, None, None]
fm.initialize_return = False
fm.last_error_return = (-10008, "NO_CONNECTION")
e, cleanup = _make_executor(fm, fl)
r = e.get_account_info()
check("returns None when reconnect fails", r is None, detail=f"got {r}")
check("mt5.initialize attempted once", fm.initialize_calls == 1)
check("RECONNECT failed error logged",
      any("RECONNECT | failed" in m for m in fl.errors),
      detail=f"errors={fl.errors}")
check("mt5_status(False, 'latched ...') called",
      any(c[0] is False and "latched" in c[1] for c in fl.mt5_status_calls),
      detail=f"calls={fl.mt5_status_calls}")
check("self.connected flipped to False", e.connected is False)
cleanup()

# =========================================================================
# Test E — reconnect cooldown prevents second attempt within 60s
# =========================================================================
print("\nTest E - cooldown prevents second reconnect within 60s window")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
# First call: all retries fail, reconnect fails too (sets cooldown timestamp
# via connect() inside _try_reconnect_once which calls self.connect() which
# stamps _last_reconnect_attempt).
# Queue is refilled per call.
e, cleanup = _make_executor(fm, fl)

# First call — retries all None + reconnect fails
fm.account_info_queue = [None, None, None, None]
fm.initialize_return = False
r1 = e.get_account_info()
check("first call returns None", r1 is None)
initial_initialize_calls = fm.initialize_calls
check("first call attempted initialize once", initial_initialize_calls == 1)

# Advance virtual time by 30s (within cooldown)
_time_now[0] += 30.0
# Reset connected for second call (first call flipped it False)
e.connected = True
# Second call: retries again, would normally reconnect — but cooldown blocks
fm.account_info_queue = [None, None, None, None]
r2 = e.get_account_info()
check("second call within 30s also returns None", r2 is None)
check("second call did NOT call mt5.initialize again (cooldown)",
      fm.initialize_calls == initial_initialize_calls,
      detail=f"initialize_calls={fm.initialize_calls}")
check("RECONNECT | skipped (cooldown) logged",
      any("RECONNECT | skipped (cooldown" in m for m in fl.infos),
      detail=f"infos with cooldown={[m for m in fl.infos if 'cooldown' in m]}")

# Advance past cooldown (total 65s from first reconnect attempt)
_time_now[0] += 35.0  # total 65s
e.connected = True
fm.account_info_queue = [None, None, None, None]
fm.initialize_return = True  # now succeeds
r3 = e.get_account_info()
check("third call after cooldown expires DOES reconnect",
      fm.initialize_calls == initial_initialize_calls + 1,
      detail=f"initialize_calls={fm.initialize_calls}")
check("third call returns dict after successful reconnect", r3 is not None)
cleanup()

# =========================================================================
# Test F — latch-then-recovery: simulate stuck state, next call recovers
# =========================================================================
print("\nTest F - latched flag False then recovered on next call")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
e, cleanup = _make_executor(fm, fl)
# Simulate today's bug stuck state
e.connected = False  # latched
# Next call — connected is False, returns None immediately (no recovery path
# from this state — that's expected; is_connected needs external reset via
# monitor's DEAL_REFRESH or a restart to flip back).
r_stuck = e.get_account_info()
check("get_account_info returns None when flag is False (pre-existing contract)",
      r_stuck is None, detail=f"got {r_stuck}")
check("no mt5.account_info calls while flag is False",
      fm.account_info_calls == 0, detail=f"calls={fm.account_info_calls}")
# Simulate external connect() success (e.g., monitor DEAL_REFRESH reset)
e.connected = True
fm.account_info_queue = [None, _FakeAccount()]  # transient None then OK
r_recovered = e.get_account_info()
check("after external connect, next call recovers via retry",
      r_recovered is not None)
check("STALE_READ | resolved logged on recovery",
      any("STALE_READ | resolved" in m for m in fl.infos))
check("self.connected True after recovery", e.connected is True)
cleanup()

# =========================================================================
# Test G — happy path zero overhead (Refinement 2)
# =========================================================================
print("\nTest G - happy path: zero retries, zero logs, zero sleep")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
# Default healthy behavior — account_info_queue empty → returns _FakeAccount()
e, cleanup = _make_executor(fm, fl)
r = e.get_account_info()
check("returned dict immediately", r is not None, detail=f"got {r}")
if r:
    check("balance=2188.04 (default)", r["balance"] == 2188.04)
check("mt5.account_info called EXACTLY once",
      fm.account_info_calls == 1, detail=f"got {fm.account_info_calls}")
check("mt5.initialize NOT called (no reconnect)",
      fm.initialize_calls == 0, detail=f"got {fm.initialize_calls}")
check("zero STALE_READ log lines",
      not any("STALE_READ" in m for m in fl.all_lines()),
      detail=f"all_lines={fl.all_lines()}")
check("zero RECONNECT log lines",
      not any("RECONNECT" in m for m in fl.all_lines()),
      detail=f"all_lines={fl.all_lines()}")
check("zero mt5_status calls (no state change)",
      fl.mt5_status_calls == [], detail=f"calls={fl.mt5_status_calls}")
check("zero time.sleep calls",
      _sleep_calls == [], detail=f"sleeps={_sleep_calls}")
check("self.connected stayed True", e.connected is True)
cleanup()

# =========================================================================
# Bonus — is_connected is flag-only (no MT5 probe, no side effects)
# =========================================================================
print("\nBonus - is_connected() is flag-only, no MT5 probe")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
e, cleanup = _make_executor(fm, fl)
e.connected = True
r = e.is_connected()
check("is_connected returns True when flag is True", r is True)
check("is_connected did NOT call mt5.account_info",
      fm.account_info_calls == 0, detail=f"calls={fm.account_info_calls}")
e.connected = False
r2 = e.is_connected()
check("is_connected returns False when flag is False", r2 is False)
check("still zero mt5 calls", fm.account_info_calls == 0)
cleanup()

# =========================================================================
# Bonus — connect() stamps _last_reconnect_attempt (enables cooldown sharing)
# =========================================================================
print("\nBonus - connect() updates _last_reconnect_attempt timestamp")
_reset_clock()
fm = _FakeMT5(); fl = _LogCapture()
e, cleanup = _make_executor(fm, fl)
e._last_reconnect_attempt = 0.0
_time_now[0] = 2_000_000.0
try:
    e.connect()  # succeeds since fake returns True and account_info=healthy
except Exception:
    pass
check("connect() stamped _last_reconnect_attempt to fake-now",
      e._last_reconnect_attempt == 2_000_000.0,
      detail=f"got {e._last_reconnect_attempt}")
cleanup()

print(f"\n{'='*60}")
print(f"RESULTS: {passes} passed, {fails} failed")
print(f"{'='*60}")
sys.exit(0 if fails == 0 else 1)
