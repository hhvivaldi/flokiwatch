# FLO-327 Phase 1 — Intent Check (READ-ONLY)

**Status:** DRAFT for CTO review · **No code · No deployment**
**Date:** 2026-04-22
**Author:** DEV (Claude-Opus 4.7)
**Incident:** Bot restart 2026-04-22 ~10:53 UTC (Windows PC update, external — confirmed by Hermano). 5 pre-restart trades (net -$9.34) dropped from session counter. Dashboard displayed -$3.36 vs actual -$12.70.

**Restart cause verified not a crash:** `grep Traceback|FATAL|CRITICAL` on log lines before 2026-04-22 10:53 returned zero bot-internal errors — the only CRITICAL hits are news-alert and Rex-monitor log events. No Python traceback, no fatal, no exception preceding the restart. This is an OS-initiated restart, not a bot failure.

---

## 1. Location of daily_stats initialization

There are **two independent "daily" trackers** in the codebase — this is the most important finding for scoping.

### A) `TradingBot.daily_stats` (dashboard / agent context)

| Step | Site | Purpose |
|---|---|---|
| Constructor init | `main.py:155-162` | Fresh zeros: `{trades:0, wins:0, losses:0, breakevens:0, pnl:0.0, date:trading_day_broker_aligned()}` |
| Persisted-state load | `main.py:282-323` `_load_persisted_state()` | Reads `data/bot_state.json` → loads `daily_stats` dict + `trade_history` → `closed_trades_today` |
| MT5 reconciliation | `main.py:381-788` `_reconcile_with_mt5()` | Pass 1 rebuilds `closed_trades_today` from `get_recent_closed_deals(hours=168)`, then rebuilds `daily_stats` from it |
| Per-trade increment | `main.py:7366-7373` | On trade close: `trades += 1`, `wins/losses/breakevens`, `pnl += profit` |
| Daily rollover | `main.py:7548` | `if today != daily_stats['date']: reset` |

**Startup sequence** (`main.py:1020-1195`):
```
start()
  → session_start_time = datetime.now()
  → _load_persisted_state()                    [line 1025]
  → init_db()
  → ... (MT5 connect, ea_bridge, other setup)
  → _reconcile_with_mt5()                      [line 1178]
  → _launch_dashboard_server()
  → write_state(self)                          [line 1195]
```

### B) `SafetyChecks.daily_loss` (6% daily-cap gate)

| Step | Site | Purpose |
|---|---|---|
| Constructor init | `safety_checks.py:25` | `daily_loss = 0.0`, `last_reset_date = None` |
| Persisted-state load | `safety_checks.py:85-119` | Reads `data/safety_state.json`, restores `daily_loss` **only if `last_reset_date == today UTC`** |
| Per-trade increment | `safety_checks.py:143` `record_trade_result(profit)` | `daily_loss += abs(profit)` for losses only (net loss magnitude tracker) |
| Daily reset | `safety_checks.py:131` | UTC-date-boundary based |

**These two trackers are wired independently.** They do not cross-validate, share a source, or rehydrate from each other.

---

## 2. Git blame findings

| Line | Commit | Date | Author | Intent |
|---|---|---|---|---|
| `main.py:155-162` (daily_stats init) | `a5a7a23` | 2026-02-20 | beckviva | Initial commit — zeros-on-boot |
| `main.py:161` (date field broker-aligned) | `6c16043` | 2026-04-13 | beckviva | FLO-286 TZ normalization |
| `main.py:381` (`_reconcile_with_mt5`) | `a5a7a23` | 2026-02-20 | beckviva | Initial commit — doc comment: *"MT5 is the source of truth — replaces what was in bot_state"* |
| `executor.py:1570` (`get_recent_closed_deals`) | `a5a7a23` | 2026-02-20 | beckviva | Initial commit |
| `executor.py:1596-1608` (two-call today+long workaround) | `eda278a` | ~Apr 2026 | beckviva | FLO-292 — long-range MT5 bug workaround |
| `safety_checks.py:85+` (load_state, same-day check) | initial + later | — | beckviva | Same-day-only restore intent is explicit |

---

## 3. Intent classification — **PARTIAL**

**Design intent: rehydrate state from persisted + MT5 on every restart.**

Evidence (code + comments):
- `_load_persisted_state` exists and runs at startup.
- `_reconcile_with_mt5` exists with explicit Pass 1 doc: *"MT5 is the source of truth — replaces what was in bot_state"*.
- `safety_checks.load_state` has explicit `same_day` gate and restores `daily_loss` on same-day restart.
- `main.py:410` loads `saved_pnl` into a local — **but never reads it** (see §7 — looks like an unfinished fallback the original author considered and abandoned mid-wire).

**Bug class: implementation-incomplete, not intent-reversal.** Rule 11 verdict: any Phase 2 fix that hardens rehydration ALIGNS WITH the original intent — no intent reversal, Rule 11 passes.

**The bug is not "daily_stats was designed to reset on every boot."** That framing would justify WONTFIX. It is not the correct framing.

---

## 4. Consumer audit

### `bot.daily_stats.pnl` consumers

| Site | Purpose | Severity if wrong |
|---|---|---|
| `main.py:6050` | Dashboard `today_pnl` field | Cosmetic |
| `main.py:7385` | Trade close logging (`day_pnl=...`) | Log noise |
| `main.py:7582-7590` | `pnl_percent` calc in hourly log | Log noise |
| `main.py:6238` | Hourly self-report | Log noise |
| `state_writer.py:123` | Dashboard state JSON | Cosmetic |
| `dashboard/server.py` | `/api/state` → browser | Cosmetic |
| **`agent_tools.get_session_context`** | **Floki-callable tool** | **DECISION-ADJACENT** |

**Important nuance on decision-adjacency** (FLO-317 context): the `<session>` block was removed from the forced-injection Floki prompt in FLO-317, but `get_session_context` remains as a Floki-callable tool. **Concrete failure mode today:** if Floki calls `get_session_context`, he sees `trades=1, pnl=-$3.36` — he would perceive a fresh session only ~0.16% into the day, not a session already -$12.70 / -0.59% into drawdown after 6 trades. More aggressive sizing follows (tighter SLs, larger lots) because nothing in his signal says "you've already been losing today." Not cosmetic — this is stale context that distorts risk posture.

### `safety.daily_loss` consumers

| Site | Purpose | Severity if wrong |
|---|---|---|
| `safety_checks.py:484-493` (`check_all`) | **THE 6% DAILY-CAP GATE** | **SAFETY** — could permit trades that should be blocked |

**Critical consumer distinction:** the 6% gate uses `safety.daily_loss`, NOT `bot.daily_stats.pnl`. Today's observed divergence: dashboard shows `pnl = -$3.36` (bot.daily_stats), while safety shows `daily_loss = $16.32`. Different numbers because they track different things (net vs abs-of-losses) AND because they have different bugs.

---

## 5. Related-code survey

- **No `reconciliation.py` module exists.** Reconciliation is a method on `TradingBot` (`_reconcile_with_mt5` at `main.py:381`).
- **Startup warm-up hook EXISTS already** at `main.py:1178` — `_reconcile_with_mt5()` runs after `_load_persisted_state()` and after MT5 connect. The rehydration surface is correctly located; the issue is WHAT it does when MT5 returns empty.
- **No retry/wait logic on `get_recent_closed_deals`** — `executor.py:1570-1676` returns whatever MT5 gives on the first try.
- `get_trade_history` agent-tool (`agent_tools.py:2208`) also calls `get_recent_closed_deals` — any warm-up race hits both paths.

---

## 6. Evidence for root cause (warmup race hypothesis)

**Log evidence** (`logs/trading_bot_2026-04-22.log`):

```
2026-04-22 06:30:22 | INFO | get_recent_closed_deals: range=36 deals, today=10 deals, merged=36 unique
                           ↑ Floki tool call mid-session, MT5 warm → 36 deals ✓
2026-04-22 10:53:51 | INFO | get_recent_closed_deals: range=0 deals, today=0 deals, merged=0 unique
                           ↑ Reconciliation at startup, ~18s after process start → 0 deals ✗
2026-04-22 10:53:51 | INFO | Reconciliation: 0 total deals | 0 today | 0 historical
2026-04-22 10:53:51 | INFO | Reconciliation complete: balance=$2144.04 | Trades today: 0 (W:0 L:0) | PnL today: $+0.00
2026-04-22 11:35:03 | DEBUG | Deal history [N1]: position=1605837214 | 30 deals returned by MT5
                           ↑ 42 min after restart, MT5 warm again → 30 deals ✓
```

**Hypothesis:** `mt5.history_deals_get()` returns `None`/empty list when called within the first ~20-30s after `mt5.initialize()` — even when positions_get() already works. This is a known MT5 Python API behavior (warm-up lag on history cache). Reconciliation at line 1178 runs too early.

**Confidence:** HIGH on the symptom (log shows 0 deals returned unambiguously). MEDIUM on the precise mechanism (warm-up race is most likely but has not been reproduced in a controlled test — that's a Phase 2 validation step).

**Honesty note on the 11:35 evidence:** the 11:35 success is from `history_deals_get` called inside deal_resolver (a different call path), not a retry of `get_recent_closed_deals` itself. The specific failed call was never retried in-session, so "MT5 eventually became warm" is inferred from a neighboring-API success, not directly measured on the same function. This is precisely why Phase 2 step 1 is a controlled reproduction — to measure the exact `get_recent_closed_deals` timing curve after a fresh `mt5.initialize()`.

**Alternative causes considered and ranked lower:**
- `executor.is_connected()` returning False: would have short-circuited with empty list and no log line. Log line DID print → connection was up.
- Symbol filter mismatch (`*{symbol}*`): Floki's 06:30 call used the same code → worked. Rules this out.
- Naive-datetime timezone confusion: could skew the date window but wouldn't produce a universal empty result with 7-day `hours=168` range.

---

## 7. Secondary issues surfaced during investigation

### 7a. Dead `saved_pnl` local at `main.py:410`

```python
saved_pnl = float(self.daily_stats.get('pnl', 0.0) or 0.0)
saved_date = self.daily_stats.get('date')

# If saved state is from another day, clear (daily reset will handle)
if saved_date and str(saved_date) != today:
    ...
    return
```

`saved_pnl` is loaded into a local then **never read anywhere after this point**. Reads like the original author considered an "if MT5 returns empty but we have non-zero saved pnl, keep it" guard and abandoned it mid-wire. Worth surfacing — Phase 2 may want to finish wiring this (or remove it if another approach wins).

### 7b. Pass 1 destructive-by-design at `main.py:452`

```python
self.closed_trades_today = []  # unconditionally wipes first

for deal in today_deals:
    self.closed_trades_today.append(...)
```

If `today_deals` is empty (for ANY reason — warmup race, MT5 disconnect, symbol rename, API hiccup), `closed_trades_today` becomes `[]` and the rebuild at line 746 sets `daily_stats.pnl = 0`. The destructive wipe happens BEFORE we know if the MT5 data is valid. This is the "multiplier" on top of the MT5-warm-up trigger.

### 7c. `safety.daily_loss` has its own divergence

Today's state (`data/safety_state.json`):
```
daily_loss = 16.32    (actual abs-of-losses today: 27.42)
daily_trades = 3      (actual trade count today: 5-6)
last_reset_date = 2026-04-22 (correct)
```

Math: 16.32 = 12.96 (1605124275) + 3.36 (1605837214). Safety captured only the last pre-restart loss and the post-restart loss. Restart wipe + partial re-record.

**Different code path, different persistence file, different restore logic** (`safety_checks.py:85+`). The 6% cap gate is partially insulated from the main.py bug but has its own data-loss mode. Today the gap is small (16.32 / 2140.68 = 0.76%, far below 6% cap) — but the bug is systemic. On a heavier-loss day that restarts mid-session, the gate could be materially wrong.

### 7d. Cross-module day-boundary inconsistency (already known backlog)

- `main.py` daily_stats uses `trading_day_broker_aligned()` (broker midnight = 22:00 UTC)
- `safety_checks.py:91` uses `datetime.now(timezone.utc).date()` (UTC calendar)

These disagree by up to 2 hours, so ~8 trades/month land in different buckets between the two trackers. Pre-existing issue tracked in memory (`project_cross_module_day_boundary.md`). Worth noting here because any FLO-327 fix that touches either tracker should at least not widen the gap.

---

## 8. Scope decision for CTO

Phase 2 surfaces fall into three plausible groupings. DEV flags this so the CTO decides scope, rather than DEV silently folding or splitting.

### Option A — FLO-327 narrow: only `bot.daily_stats`
- Fix Pass 1 destructive wipe + add MT5 warmup retry
- Leaves `safety.daily_loss` bug for a separate ticket (FLO-327b? Or existing safety ticket?)
- Smaller blast radius, cleaner attribution

### Option B — FLO-327 broad: both trackers in one pass
- Fix both in coordinated manner (potentially unify the trackers, or at least align the day-boundary)
- Bigger commit, Rule 14 REFACTOR-class, more test surface
- Addresses the 6% gate data-loss at the same time

### Option C — FLO-327a (daily_stats) + FLO-327b (safety) split
- Two independent tickets, sequenced
- Treats each tracker's bug at its layer
- DEV recommendation IF the two bugs have different fix shapes

**DEV recommendation: Option C.** They have different fix shapes:
- `bot.daily_stats` → fix the unconditional wipe and add warmup guard in reconciliation
- `safety.daily_loss` → likely needs a rebuild-from-MT5-on-restart path it currently lacks entirely (it only restores from its own persisted file, not from source of truth)

---

## 9. What Phase 2 should investigate (pending CTO approval)

If CTO approves Phase 2 on the daily_stats side (FLO-327 or FLO-327a):

1. **Reproduce the warmup race in a controlled script** — call `mt5.history_deals_get` 1, 5, 10, 30 seconds after fresh `mt5.initialize()`. Confirm the timing profile.
2. **Design the Pass 1 guard.** Candidates:
   - Keep persisted state if `get_recent_closed_deals` returns empty AND persisted has non-zero data for today's broker date.
   - Or retry `get_recent_closed_deals` with backoff until non-empty OR N seconds exceeded, then fall through to "keep persisted".
3. **Decide fate of `saved_pnl` at line 410.** Wire it into the guard, or delete it.
4. **Write tests:** simulate MT5 returning `[]`, verify `daily_stats` survives with persisted values.

If CTO approves safety side (FLO-327b):
1. Design MT5-source-of-truth rehydration path for safety.
2. Align day-boundary with broker alignment (OR explicitly document why it stays UTC).

---

## 10. Rule checkpoints

- **Rule 11 (intent):** Original author's intent was to rehydrate. Both fixes ALIGN with intent. No reversal.
- **Rule 14 (decision-logic file):** `safety_checks.py` is decision-logic. `main.py` contains decision flow. Any Phase 2 commit must go through code-review with skill and be classified.
- **Rule 18 (skills):** senior-backend + senior-data-engineer consulted for Phase 1 investigation. Same two + senior-security for Phase 2 (safety path touches risk gates).
- **Rule 22 (timestamps):** Both modules touched. Phase 2 fix must not reintroduce `datetime.now()` for storage.

---

## 11. Artifacts

- This report: `data/_audits/flo327/FLO-327_Phase1_IntentCheck.md`
- Incident log excerpt: `logs/trading_bot_2026-04-22.log` lines 41395-41399 (reconciliation failure), 25930 (prior success), 44061+ (post-restart MT5 warm)
- Live-state evidence:
  - `data/bot_state.json` — `daily_stats.pnl = -3.36`, `trade_history` length 1
  - `data/safety_state.json` — `daily_loss = 16.32`, `daily_trades = 3`

**Standing by for CTO Phase 2 scope decision (Option A / B / C) before any code.**
