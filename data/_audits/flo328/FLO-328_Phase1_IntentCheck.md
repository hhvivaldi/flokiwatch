# FLO-328 Phase 1 — Intent Check (READ-ONLY)

**Status:** DRAFT for CTO review · **No code · No deployment**
**Date:** 2026-04-22
**Author:** DEV (Claude-Opus 4.7)
**Incident framing:** `data/safety_state.json` shows `daily_loss=$16.32`, `daily_trades=3` on 2026-04-22 while 5 trades (total abs-loss $27.42) executed between 2026-04-21 23:34 UTC and 2026-04-22 09:34 UTC. Hermano framed the $11.10 delta as "safety.daily_loss underreported, 6% daily cap fails silently."

**Phase 1 reframes this.** The observed number is not a data-loss bug. And the "6% daily cap" is not live code.

---

## HEADLINE — Read before anything else

**The ticket premise is inverted in two ways:**

1. **The 6% daily-loss cap is dead code.** FLO-118 (commit `c8f3bf7`, 2026-03-26) removed every live call site of `is_bot_paused()` from `main.py` (proactive cycle, Echo, Luna, Simba). No module reads `safety.daily_loss` for any enforcement. `config.MAX_DAILY_LOSS = 6.0` is a dead config. The startup log line `main.py:1144` ("Max daily loss: 6%") is misleading — it advertises a gate that does not exist. (Details §3a.)

2. **Today's $11.10 delta is not data loss.** It is a day-boundary mismatch between safety (UTC calendar, chosen deliberately in FLO-96 Phase 4) and main.py's `daily_stats` (broker-aligned, chosen deliberately in FLO-286 Phase 4). Safety's $16.32 is the EXACT correct sum for UTC day Apr 22; Hermano's $27.42 is the broker-day total; the two differ by the two trades that closed between 22:00 UTC Apr 21 and 00:00 UTC Apr 22. No record_trade_result call was missed. (Verification §5.)

**Severity recommendation: P2 defensive / cleanup. Not P1 safety-critical.** The real safety question — "should the 6% cap be live?" — is a separate ticket (proposed FLO-329) and is a deliberate-reversal-of-FLO-118 Rule-11 call.

Full reasoning below.

---

## 1. Location of `safety.daily_loss`

| Aspect | Site |
|---|---|
| Class field init | `safety_checks.py:25` — `self.daily_loss = 0.0` (inside `SafetyChecker.__init__`) |
| Persistence file | `data/safety_state.json` (atomic write via `.tmp` + `os.replace`, `safety_checks.py:35-60`) |
| Process-wide instance | `safety_checks.py:498` — `safety = SafetyChecker()` (module-level singleton) |

No in-memory-only usage outside the singleton; no alternative storage backend.

---

## 2. ALL write sites (audit complete)

There is **one and only one** production write path:

```
main.py:7534  record_trade_result(profit)            # FLO-118 kept this for "Sage data"
   ↓
safety_checks.py:527  module-level record_trade_result(profit)
   ↓
safety_checks.py:136  SafetyChecker.record_trade_result(profit)
   ↓
safety_checks.py:127  self.reset_daily_stats()       # checks UTC day boundary
safety_checks.py:139  self.daily_trades += 1          # unconditional
safety_checks.py:143  self.daily_loss += abs(profit)  # losses only (profit < 0)
safety_checks.py:147  self._save_state()              # persist
```

**Invocation site in main.py:7534:** inside `_monitor_cycle()`, runs for every `action['action']` in `['TIMEOUT_CLOSE', 'DRAWDOWN_CLOSE', 'BROKER_CLOSE']`, called unconditionally (even for pending P&L). No gating.

**Test-only writes** (not production): `safety_checks.py:579, 617-619` (test harness), `validate_all.py:221-223`. Ignored.

**Rate/deduplication:** none — every monitor-detected close event fires `record_trade_result` exactly once. If bot restarts and reconciliation does NOT re-invoke `record_trade_result` for already-closed trades, those closes are NEVER re-recorded into safety. This is the backbone of the observed behaviour.

---

## 3. ALL read sites (audit complete)

### Code-level reads of `safety.daily_loss`

| Site | Context | Status |
|---|---|---|
| `safety_checks.py:43` | `_save_state()` payload | Internal (persistence mirror) |
| `safety_checks.py:97` | `_load_state()` restore (same-day only) | Internal (persistence mirror) |
| `safety_checks.py:116` | `log.info(f"SAFETY \| Restored state: daily_loss=$...")` | Log line at startup |
| `safety_checks.py:131` | `reset_daily_stats()` zero out | Internal (reset) |
| `safety_checks.py:484-493` | `is_bot_paused()` — **6% daily-loss cap gate** | **DEAD CODE** (see §3a) |
| `safety_checks.py:555` | `get_safety_status()` dict output | **DEAD CODE** (see §3a) |

### 3a. CRITICAL FINDING — `safety.daily_loss` has ZERO functional consumers.

- **`is_bot_paused()` never called:** grep of entire codebase shows `is_bot_paused(` appears in exactly one location — the `def is_bot_paused()` definition at `safety_checks.py:478`. `main.py:73` imports it, never invokes it. No call site anywhere.
- **`get_safety_status()` never called:** same pattern — `main.py:73` and `dry_run_monitor.py:24` import it, neither invokes it. Only `safety_checks.py:649` inside the in-file test harness calls it.
- **No other module reads `safety.daily_loss` directly:** exhaustive grep across `*.py` (excluding `.venv`) returns zero hits outside `safety_checks.py` itself.
- **Sage does NOT read `safety.daily_loss` or `safety_state.json`:** the FLO-118 commit comment *"Keep record_trade_result/opened/close_type for Sage data"* is stale — Sage (`sage_auditor.py`) has no reference to either.

**Consequence:** `safety.daily_loss` is a **write-only counter**. It's computed, persisted, and then nothing in the live path reads it. The 6% daily-loss cap (`config.MAX_DAILY_LOSS = 6.0`) is **not enforced anywhere**.

**Even the diagnostic log line is silent.** `_load_state()` at `safety_checks.py:114-117` calls `log.info(f"SAFETY | Restored state: losses=X, daily_loss=$Y.YY, trades=Z")` on every startup with same-day state. A grep across today's + yesterday's logs returns **zero** `SAFETY | Restored` hits, despite 10+ restarts yesterday and 1 today. Cause: `safety = SafetyChecker()` at `safety_checks.py:498` is evaluated at module-import time, before `logger.py` configures handlers — the `log.info` call goes to an un-configured root logger and is dropped. The one piece of observability the author wrote for this counter is silent by architecture.

The only way a human ever sees the number is by opening `data/safety_state.json` manually — which is exactly how Hermano surfaced today's discrepancy.

**This directly contradicts the ticket premise** that *"6% daily loss cap fails silently"*. It doesn't fail — it doesn't exist as live code.

---

## 4. Git-blame findings / intent classification

### 4a. Rule-11 relevant commits

| Commit | Date | FLO | Intent (from message verbatim) |
|---|---|---|---|
| `a5a7a23` | 2026-02-20 | — | Initial commit. `daily_loss = 0.0`, `record_trade_result` with `daily_loss += abs(profit)`, `is_bot_paused` defined, `config.MAX_DAILY_LOSS = 6.0`. |
| `7ae3401` | 2026-03-24 | FLO-93 | *"persist SafetyChecker state to survive restarts."* Added `_save_state`, `_load_state`, same-day restore. |
| `c8f3bf7` | 2026-03-26 | **FLO-118** | *"remove SafetyChecker blocking, Floki manages own risk."* Removed `is_bot_paused()` gates from main.py (proactive cycle, Echo, Luna, Simba). Kept `record_trade_result` for "Sage data". |
| `96aaa1d` | 2026-04-09 | **FLO-96 Phase 4** | *"safety_checks daily counter uses UTC date boundary."* Changed `datetime.now().date()` (local) → `datetime.now(timezone.utc).date()` in init, state-restore, and reset. Explicit reasoning: *"Trades between 22:00-00:00 UTC would count on the wrong day."* |
| `6c16043` | 2026-04-13 | **FLO-286** | *"global timezone rule (Phase 3+4+5). Phase 4: day-boundary unification — main.py daily_stats reset uses trading_day_broker_aligned(), sage/echo/luna/news_score_hybrid use trading_day_utc(), alerts.py switched off datetime.utcnow()."* **`safety_checks.py` is NOT listed.** Not touched by FLO-286. |

### 4b. Intent classification

Three intents stacked chronologically:

1. **FLO-93 (Mar 24):** persist across restarts. Same-day restore gate implies author wanted within-day accumulation to survive reboots.
2. **FLO-118 (Mar 26):** remove `is_bot_paused()` gates. The 6% cap was consciously decommissioned. `record_trade_result` left in place "for Sage data" — but Sage never wired it up.
3. **FLO-96 Phase 4 (Apr 9):** explicit fix to use UTC day. The commit message explicitly justifies UTC: *"Trades between 22:00-00:00 UTC would count on the wrong day."* This was a **deliberate choice of UTC**, not an oversight.
4. **FLO-286 (Apr 13):** commit message literally says **"day-boundary unification"**. "Unification" is load-bearing — the stated goal was a single day definition across the codebase. Yet `safety_checks.py` is not in the enumerated module list and its day-math is unchanged. Skipping safety contradicts the commit's own stated intent. Best reading: **oversight, not intentional split.** If the author had meant to keep safety on UTC deliberately, the commit message would note the exception rather than silently exclude.

**Rule-11 verdict:** None of the "current behaviour" is a bug by original design.
- The UTC boundary at `safety_checks.py:87, 91, 129` was chosen deliberately (FLO-96 Phase 4).
- The lack of 6% cap enforcement was a deliberate removal (FLO-118).
- The divergence from main.py's broker-day is a side effect of FLO-286 not cascading. Reasonable reading: oversight, not intentional split. But not a bug in safety — safety is behaving EXACTLY as FLO-96 Phase 4 specified.

A Phase-2 fix that **aligns safety to broker-day** touches FLO-96 Phase 4's explicit choice → needs CTO sign-off on the philosophical change. A fix that **re-wires the 6% cap** reverses FLO-118 → much bigger Rule-11 call (reversing Escola 1 principle that code informs, never prescribes).

---

## 5. Reproducing today's $11.10 discrepancy (the bug that isn't)

### Critical timezone disambiguation (directly verified)

MT5 `deal.time` is **broker-time (UTC+3) interpreted as Unix-epoch seconds**. Evidence (directly measured, not inferred):

1. Live tick comparison (run during Phase 1):
   - `mt5.symbol_info_tick('XAUUSD').time = 1776863868` → `fromtimestamp(UTC) = 2026-04-22T13:17:48`
   - `time.time() = 1776853068` (Python local clock = UTC epoch)
   - **Diff = +10800s = +3h.** MT5 tick reports 3h ahead of real UTC → broker is UTC+3, `tick.time` is broker-time-as-epoch.

2. Deal-time vs monitor-log cross-check (5 trades today, each independently validated):
   - Ticket 1605185504: MT5 `deal.time=1776832086` → `fromtimestamp(UTC)=2026-04-22T04:28:06`. Actual UTC = broker − 3h = **01:28:06 UTC**. Monitor log `2026-04-22 03:29:03` local (UTC+2) = **01:29:03 UTC**. Bot detected 57s after actual close. ✓
   - Ticket 1605837214: MT5 `deal.time=1776861274` → `fromtimestamp(UTC)=2026-04-22T12:34:34`. Actual UTC = **09:34:34 UTC**. Monitor log `2026-04-22 11:34:34` local = **09:34:34 UTC**. Bot detected instantly. ✓
   - Same pattern confirmed for 1605010600, 1605103684, 1605124275.

3. Bot-written `close_time` in `bot_state.json` = "2026-04-22T09:34:34" for 1605837214. Matches actual UTC. Executor code at `executor.py:1526` stores deals in naive-UTC after its own broker-offset conversion — already handles this.

This is a codebase-level gotcha already partly-acknowledged in memory `project_mt5_timezone_bug.md`. Noted here because the entire §5 reasoning downstream depends on it.

### True UTC close times for today's trades

| Ticket | Raw `deal.time` interpretation | **Actual UTC close** | UTC calendar day | Broker day (broker midnight = 22:00 UTC) |
|---|---|---|---|---|
| 1604966610 | 2026-04-21 23:40:01 (broker) | **2026-04-21 20:40 UTC** | Apr 21 | Apr 22 |
| 1605010600 | 2026-04-22 02:13:55 (broker) | **2026-04-21 23:13 UTC** | Apr 21 | Apr 22 |
| 1605103684 | 2026-04-22 02:45:53 (broker) | **2026-04-21 23:45 UTC** | Apr 21 | Apr 22 |
| 1605124275 | 2026-04-22 03:02:17 (broker) | **2026-04-22 00:02 UTC** | Apr 22 | Apr 22 |
| 1605185504 | 2026-04-22 04:28:06 (broker) | **2026-04-22 01:28 UTC** | Apr 22 | Apr 22 |
| 1605837214 | 2026-04-22 09:34:34 (broker) | **2026-04-22 06:34 UTC** | Apr 22 | Apr 22 |

### Safety's UTC-calendar view of "today"

- UTC day Apr 22 trades = **3** → {1605124275, 1605185504, 1605837214}
- UTC day Apr 22 losses (abs): **12.96 + 3.36 = $16.32**
- UTC day Apr 22 wins: +14.72 (ignored for `daily_loss`, counted in `daily_trades`)
- Expected persisted state: `daily_trades=3, daily_loss=16.32` → **MATCHES `safety_state.json` exactly.**

### Hermano's broker-day view

- Broker day Apr 22 trades = **5** (all of the above except 1604966610 which closed 20:40 UTC = broker day Apr 21)
- Broker day Apr 22 abs-losses: 3.80 + 7.28 + 12.96 + 3.36 = **$27.40** (matches the observed "$27.42" within rounding noise in deal profit vs commission/swap breakout)
- Expected under broker: `daily_trades=5, daily_loss=27.40`
- Actual delta vs Hermano's expectation: $27.40 − $16.32 = **$11.08** → matches reported "$11.10 underreported".

### Conclusion

**The missing $11.08 is not lost data.** It is trades 1605010600 (-$3.80, closed 23:13 UTC Apr 21) and 1605103684 (-$7.28, closed 23:45 UTC Apr 21) — both fell in UTC day Apr 21 (safety's view) but broker day Apr 22 (Hermano's view). Safety's UTC calendar zeroed them at UTC midnight. No write was lost; no record_trade_result call was missed.

This is exactly the behaviour FLO-96 Phase 4 specified.

---

## 6. Day-boundary logic map (cross-module)

| Module | Day function | Calendar |
|---|---|---|
| `main.py` — `daily_stats.date` | `trading_day_broker_aligned()` | Broker midnight (22:00 UTC) |
| `safety_checks.py` — `daily_loss` reset | `datetime.now(timezone.utc).date()` | UTC calendar |
| `sage_auditor.py`, `echo_sentinel.py`, `luna_analyst.py`, `news_score_hybrid.py` | `trading_day_utc()` | UTC calendar |
| `trade_reflexion.py` EOD counterfactual | `trading_day_utc()` | UTC calendar |

Observations:
- **Three day definitions active in the codebase.** main.py is broker-aligned (singular). Everything else is UTC-calendar.
- **`safety_checks.py` is aligned with Sage/Echo/Luna (UTC)** — not with `main.py daily_stats` (broker).
- If the CTO decides FLO-328 should align safety to broker-day, that change would also pull `safety_checks.py` away from Sage/Echo/Luna's UTC alignment. A full unification would require touching more modules than just safety.
- Known backlog item (`project_cross_module_day_boundary.md`): ~8 trades/month land in different buckets between the two regimes. Matches today's observation exactly.

---

## 7. Decision points for CTO

Given §3a (6% cap is dead code) and §4b (UTC-in-safety was deliberate), Phase 1 puts **three independent questions** on the CTO desk, not one:

### Q1 — Is `safety.daily_loss` still needed at all?

As of today, it has **zero functional consumers**. It is a log line at startup and an entry in `safety_state.json`. Options:
- **Keep + re-wire:** re-instate the 6% cap as a gate (reverses FLO-118 — major Rule-11 call, needs its own ticket).
- **Keep as observational:** acknowledge it's for audit/human-eye inspection only; document in `SYSTEM_DOCUMENTATION.md`; no code change required.
- **Delete:** remove write path + persistence + dead `is_bot_paused`/`get_safety_status` branches; clean commit, ~60-80 LoC removed.

### Q2 — If kept, align to broker-day or leave UTC?

- **Align to broker-day:** matches Hermano's expectation; matches main.py's daily_stats; reverses FLO-96 Phase 4's explicit choice → needs CTO sign-off.
- **Leave UTC:** matches Sage/Echo/Luna group alignment; current behaviour is correct per FLO-96 Phase 4; document the difference in `FIELD_CONTRACT.md` and `safety_state.json` keys.

### Q3 — Is the 6% daily-loss cap a real safety requirement or deprecated?

- `config.MAX_DAILY_LOSS = 6.0` still exists but reaches zero enforcement paths.
- FLO-118 removed the gate in March 2026 (*"Floki manages own risk"*). Zero enforcement since then.
- Re-wiring the cap would directly reverse FLO-118's explicit intent → Rule-11 requires new justification, not a trivial Phase-2 call.
- Deprecation (remove dead code + config key) is a cleanup, no intent reversal.

This is the real safety question underneath Hermano's original concern. DEV recommends **split into new ticket FLO-329: "6% daily-loss cap — re-enable or formally deprecate"**. That ticket owns the policy decision and reversal-of-FLO-118 Rule-11 analysis.

### Q3-adjacent — Misleading startup log line (`main.py:1144`)

The startup log *"Risk/trade: {X}% | Max daily loss: {Y}%"* advertises a gate that doesn't exist. This is a **UX/documentation defect**, not a policy question. Fix follows whichever way FLO-329 resolves:

- If FLO-329 re-enables the cap → log line is correct, no change.
- If FLO-329 deprecates the cap → remove or reword the log line.

Should NOT be scoped under FLO-328 or FLO-329 directly — track as a dependent cleanup that branches off FLO-329's resolution (or a ~5-min chore ticket tied to the FLO-329 commit).

**DEV recommendation:**
- **Q1: Keep as observational** (safer than deletion; cheap; preserves audit trail for Sage if it ever gets wired).
- **Q2: Align to broker-day** — matches Hermano's mental model, matches main.py, and the FLO-96 Phase 4 justification ("trades between 22:00-00:00 UTC on wrong day") was specifically about local-vs-UTC, not UTC-vs-broker; the same reasoning arguably supports broker-aligned even more strongly (broker = market session).
- **Q3: Split into a NEW FLO-329.** "Dead 6% cap — decide wire-up vs deprecation." Not a Phase 2 for FLO-328.

---

## 8. What Phase 2 should do (pending CTO Q1/Q2 answer)

If CTO picks **Q1=keep, Q2=broker-aligned**:
1. Change `safety_checks.py:87, 91, 129` from `datetime.now(timezone.utc).date()` to the broker-aligned helper.
2. Migrate in-flight `safety_state.json` — no schema change, but the first post-deploy `reset_daily_stats()` call on a broker-day boundary may double-reset transiently. Design the migration to tolerate.
3. Tests: simulate close events at 21:30 UTC, 22:30 UTC, 23:30 UTC, 00:30 UTC — verify same broker day bucketing.
4. Update `FIELD_CONTRACT.md` if `safety_state.json` keys are contract-exposed (they aren't currently — internal file).

If CTO picks **Q1=delete**: ~60-80 LoC removal, single refactor commit, straightforward.

If CTO picks **Q1=observational, Q2=leave UTC**: **no code change.** Document in `SYSTEM_DOCUMENTATION.md` that `safety_state.json` uses UTC calendar deliberately, and that its numbers will differ from `bot_state.json daily_stats` by up to 2 hours of closes.

---

## 9. Rule checkpoints

- **Rule 11:** FLO-96 Phase 4 was deliberate. FLO-118 was deliberate. Any Phase 2 touching these needs explicit intent-reversal reasoning. Deletion (Q1=delete) is NOT intent-reversal — it's cleanup of code made dead by FLO-118. Re-wiring the cap IS intent-reversal — needs a new FLO and Rule-11 justification.
- **Rule 14:** `safety_checks.py` is decision-logic-file. Any Phase 2 commit runs through code-review skill.
- **Rule 15:** File is 655 LoC — any new functional code triggers full-file audit.
- **Rule 18 (skills):** senior-backend + senior-security acknowledged for Phase 1. Same two + senior-data-engineer for Phase 2 (persistence touched).
- **Rule 22:** Timestamps. `safety_checks.py` currently uses `utc_iso()` for `saved_at` (FLO-309 sweep) and `datetime.now(timezone.utc)` for day math (FLO-96 Phase 4). No regression vectors here.

---

## 10. Severity reclassification (recommended)

Original ticket frame: **P1 URGENT** — "Feeds the 6% daily loss cap gate (safety layer). Silent failure mode — gate fails without alarm."

**Phase 1 finding inverts this:** the 6% cap gate is dead code (since FLO-118, March 26). The "silent failure" is the absence of the gate altogether, which is a decision Hermano made three weeks ago. The daily_loss number Hermano compared against Sage's counts is correct per its own design (UTC-day), not a data-loss bug.

DEV recommends reclassifying FLO-328 to **P2 defensive / cleanup** and opening **FLO-329** (P1 or P0, TBD by CTO) for the dead-6%-cap question, which is the real safety question underneath Hermano's original concern.

---

## 11. Artifacts

- This report: `data/_audits/flo328/FLO-328_Phase1_IntentCheck.md`
- Evidence files:
  - `data/safety_state.json` — `daily_loss=16.32, daily_trades=3, last_reset_date=2026-04-22`
  - `data/bot_state.json` — `daily_stats.trades=1, daily_stats.pnl=-3.36` (separate issue, tracked in FLO-327)
  - Today's log (`logs/trading_bot_2026-04-22.log`) — zero `SAFETY` events (record_trade_result does not log)
  - Yesterday's log (`logs/trading_bot_2026-04-21.log`) — contains all pre-restart trade closes (1605010600, 1605103684, 1605124275, 1605185504) with BROKER_CLOSE events
- Referenced commits: `a5a7a23`, `7ae3401`, `c8f3bf7`, `96aaa1d`, `6c16043`

**Standing by for CTO decisions on Q1 / Q2 / Q3 before any code.**
