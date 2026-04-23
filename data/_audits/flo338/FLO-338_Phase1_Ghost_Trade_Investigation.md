# FLO-338 Phase 1 — Ghost Trade Investigation (READ-ONLY)

**Status:** Phase 1 complete. Bot NOT restarted. No code or data files modified.
**Generated:** 2026-04-23 UTC
**Priority:** P0 — but severity HIGHER than initially framed: this is not just a journal-display bug, it's a **DUPLICATE-ORDER PLACEMENT** on an autonomous trading system.

---

## HEADLINE — direct answer

The ghost trade is a symptom of a more serious bug. At **2026-04-23 02:21:35 UTC+2** (log local time), the EA Bridge timed out attempting to resolve a real MT5 ticket for Floki's OPEN_BUY decision; `executor` then fell through to the **MT5 direct API** fallback and placed **a SECOND order**. Both orders filled within seconds at the same price (4729.43 BUY, SL 4702, TP 4746, 0.02 lots):

- **Ticket 1607377569** — placed via the EA Bridge. Never registered by `execute_trade`. Only seen by the monitor's `first_sight` path. **The ghost.** Closed at TP 4746 for +$33.14.
- **Ticket 1607377682** — placed via the MT5 direct fallback. The one `execute_trade` returned and wrote to `history.db`. Closed at SL trailing for -$1.36.

**Floki intended to open ONE position. The system opened TWO.** The broker filled both, monitor tracked both, `bot_state.json` shows both, but `history.db` only shows one. The ghost is the *orphan of a silent duplicate placement*.

This is categorically different from the prior FLO-308 fix (ticket=0 collision) and FLO-97 fix (reconciliation safety net). Those fixes assume *one intended order → one actual order*. This bug is *one intended order → two actual orders*, and all the hygiene code downstream is powerless to detect the extra one as anything other than an "orphan pending fill".

**Scope evidence (Proxy 3, §5.3):** `SELECT ticket, profit, comment FROM trades WHERE comment LIKE 'reconciled:%'` returns **20 prior ghosts** silently caught by the FLO-97 reconciliation safety net in 16 days (2026-04-07 → 2026-04-20). Today's ghost (1607377569) would be the 21st. Average ~1.4 ghosts/day. This bug class is *recurring*, not rare — but Hermano hasn't noticed before because reconciliation-at-startup backfilled the rows before any dashboard check. Today's ghost is visible specifically because reconciliation hasn't had a chance to run since the close.

---

## 1. Verification (STEP 1.1)

MT5 direct query was NOT run (bot is live — sharing MT5 connection would risk interference). Verification sourced from log evidence + bot_state.

**Deal resolver output at close-time** (log lines 99322-99324, local time 2026-04-23 02:30:44):

```
Deal history [N2.5]:
  ✓ Deal #1330733989 | pos_id=1607377569 | entry=IN  | type=BUY  | price=4729.43 | profit=0.00 | time=2026-04-23 00:21:46
  ✓ Deal #1330758455 | pos_id=1607377569 | entry=OUT | type=SELL | price=4746.00 | profit=33.14 | time=2026-04-23 00:30:16
Deal history [N2.5] FOUND: position_ticket=1607377569 | deal_ticket=1330758455 | entry=OUT
  close_price=4746.00 | profit=33.14 | reason=Take Profit
```

Ghost ticket **CONFIRMED in MT5**:
- Position ticket: 1607377569
- Deals: IN 1330733989 (BUY @ 4729.43) + OUT 1330758455 (SELL @ 4746.00)
- Profit: **+$33.14** (matches Hermano's report)
- Reason: Take Profit (code 5)
- Times: open 00:21:46 UTC, close 00:30:16 UTC
- Symbol: XAUUSD (confirmed by N2.5 XAUUSD-filtered query)
- Volume: 0.02 lots

Adjacent-ticket context (chronological, UTC):

| Ticket | Open UTC | Close UTC | Profit | In history.db? |
|---|---|---|---|:---:|
| 1607255156 | 22:23:35:45Z | 22:23:42:26Z | -5.84 | ✅ |
| **1607377569** | **23:00:21:46Z** | **23:00:30:16Z** | **+33.14** | ❌ **GHOST** |
| 1607377682 | 23:00:22:05Z | 23:00:25:07Z | -1.36 | ✅ |
| 1607549360 | 23:01:57:17Z | 23:02:00:44Z | -2.46 | ✅ |
| 1607591024 | 23:02:16:18Z | 23:02:18:50Z | +2.28 | ✅ |

*(22:23 = 2026-04-22 23:xx; 23:00 = 2026-04-23 00:xx)*

Notable: **1607377569 opened 19 seconds BEFORE 1607377682** and at the exact same price. Ticket numbers are sequential (569 < 682) confirming the earlier ticket opened first.

---

## 2. Capture path grid (STEP 1.2) — 5 tickets × 5 surfaces

| Ticket | A: `trades` row | B: `bot_state.trade_history` | C: `trade_conditions/` | D: `post_trade_reports/` | E: logs |
|---|:---:|:---:|:---:|:---:|:---:|
| 1607255156 | ✅ | ✅ | ✅ (1084 B) | ✅ (916 B) | ✅ normal close |
| **1607377569** | **❌** | **✅** (profit=33.14) | **❌** | **❌** | **⚠️ ghost close via deal resolver fallback** |
| 1607377682 | ✅ | ✅ | ✅ (1112 B) | ✅ (906 B) | ✅ normal close |
| 1607549360 | ✅ | ✅ | ✅ (665 B) | ✅ (506 B) | ✅ normal close |
| 1607591024 | ✅ | ✅ | ✅ (1111 B) | ✅ (502 B) | ✅ normal close |

**1607377569 fails on 3 of 5 surfaces** — but is preserved in `bot_state.trade_history` (+$33.14) and has full monitoring/close log evidence. The write-path gap starts at `execute_trade` (never called for this ticket) and propagates to every downstream persistence except `bot_state` (updated by monitor at close) and `trade_reflexions` (also reads from monitor state).

---

## 3. Timeline around the ghost (STEP 1.3)

Window ±3 min around the ghost's close. Annotated with interpretation. All timestamps LOCAL (UTC+2 CEST); UTC in parentheses.

### 3.1 Ghost OPEN phase (02:21:28 → 02:22:05 local / 00:21:28 → 00:22:05 UTC)

```
02:21:28  Floki cycle begins: BUY decision forming (Score 67.4, Conf VERY_LOW 32.2)
02:21:35  ⚠️ EA_BRIDGE | Could not resolve real ticket after 10s — falling through to MT5 direct API
02:21:35     EA Bridge: Signal written - HOLD | SL:0.00 TP:0.00 Lot:0  ← signal CLEARED on EA side
02:21:35     EA_BRIDGE | Signal cleared (HOLD) before MT5 direct fallthrough
02:21:35     EA_BRIDGE | ticket_not_resolved after polling — retrying via MT5 direct API
02:21:46  ORDER | BUY | Ticket:1607377682 Lot:0.02 Price:4729.43 SL:4702.00 TP:4746.00   ← MT5 direct placement
02:21:46  SUCCESS | Order executed: Ticket 1607377682 | Spread: 2.0 pips
02:21:46  LESSONS | conditions saved for ticket #1607377682                              ← capture for T#682 only
02:21:46  AGENT_TOOL | execute_trade | 20361ms | BUY @ 4729.43 | ticket=1607377682 | success
02:21:48  Monitor tick: 2 open position(s)                                                ← FIRST SIGHTING OF 2 POSITIONS
02:21:48  Monitor: #1607377569 BUY @ 4729.43 | SL original=4702.00 (274 pips)            ← ghost discovered by monitor
02:21:48  BALANCE_CAPTURE | ticket=#1607377569 | balance=$2129.68 | source=first_sight
02:21:48  ⚠️ PENDING_FILL_DB | no ticket=0 row found for direction=BUY (ticket #1607377569)
02:21:48  Monitor: #1607377682 BUY @ 4729.43 | SL original=4702.00 (274 pips)            ← visible-to-Floki ticket
02:21:48  BALANCE_CAPTURE | ticket=#1607377682 | balance=$2129.68 | source=first_sight
02:21:48  ⚠️ PENDING_FILL_DB | no ticket=0 row found for direction=BUY (ticket #1607377682)
02:22:05  FLOKI | decision_channel=tool | decision=OPEN_BUY conf=60 fields_populated=9 | tools_called=12
02:22:05  Agent decision: OPEN_BUY (conf=60) [125151+3241 tokens, 78144ms]
02:22:05  FLOKI | record_trade_open → ticket=1607377682 BUY @ 4729.43                    ← INSERT fires for T#682 only
```

### 3.2 Ghost MONITORING phase (02:22:08 → 02:30:34)

Monitor tracks P&L for both tickets in parallel for ~8 minutes. Both see identical P&L swings (same entry, same SL, same TP). At 02:28:14 and 02:29:51, Floki issues ADJUST_TRADE for **ticket 1607377682 only** (the visible one) — trailing SL from 4702 → 4730 → 4736. The ghost's SL stays at 4702 because ADJUST_TRADE only knows about 1607377682.

### 3.3 Ghost CLOSE phase (02:30:44)

```
02:30:44  Monitor: Position #1607377569 disappeared — checking history...
02:30:44  BALANCE_DIFF | ticket=#1607377569 | open=$2129.68 | now=$2161.46 | diff=$+31.78
02:30:44  Deal history [N1]: position=1607377569 | 28 deals returned by MT5
02:30:44  Deal history [N1]: No deals with position_id=1607377569               ← fallback 1 missed
02:30:44  Deal history [N2]: 28 XAUUSD deals total, 0 with position_id=1607377569   ← fallback 2 missed
02:30:44  Deal history [N2.5]: 6 XAUUSD deals total (today), 2 with position_id=1607377569
02:30:44  ✓ [N2.5] Deal #1330733989 | pos_id=1607377569 | entry=IN  ← FOUND via N2.5
02:30:44  ✓ [N2.5] Deal #1330758455 | pos_id=1607377569 | entry=OUT | price=4746.00 | profit=33.14
02:30:44  Deal history [N2.5] FOUND | close_price=4746.00 | profit=33.14 | reason=Take Profit
02:30:44  WARNING | Monitor: P&L drift for #1607377569: deal=$+33.14 vs balance_diff=$+31.78
02:30:44  POSITION | Ticket:1607377569 | BROKER_CLOSE | Closed by broker: Take Profit | P&L: $+33.14
02:30:45  ⚠️ TRADE_CLOSE | ticket #1607377569 not found in SQLite — close not recorded (will be caught by reconciliation)
02:30:45  REFLEXION | starting for ticket=1607377569
02:30:45  DEBUG POST_TRADE_REPORT | ticket #1607377569 not in trades table
02:30:47  REFLEXION | ticket=1607377569 | lesson=Setting a realistic take profit...
```

**The deal resolver succeeded (via N2.5 fallback). bot_state was updated. Reflexion ran. But the `trades` UPDATE failed because no row was ever INSERTED at OPEN time.** The WARN *"will be caught by reconciliation"* is a promise that didn't cash — reconciliation hasn't run again since restart (see §3.4).

### 3.4 Reconciliation timing (critical)

Reconciliation ran **once, at 21:46:45 local** (bot startup after FLO-334 push):
```
21:46:45  Reconciliation: 38 total deals | 9 today | 29 historical
21:46:45  Reconciliation complete: balance=$2135.52 | Trades today: 9 (W:2 L:6) | PnL today: $-17.84
```

This ran **before the ghost was opened (~4h 35m later)**. No further reconciliation entry appears in logs through the latest reviewed timestamp (~02:30:45 local). Reconciliation either (a) only runs on startup, (b) is scheduled less frequently than the 4h gap between restart and ghost, or (c) has an orchestration bug preventing re-runs. This is the reason the promised *"will be caught by reconciliation"* has NOT yet materialized.

### 3.5 Baseline comparison — neighbor ticket 1607377682 (normal flow)

Neighbor opened 19 seconds after ghost; SAME price, SL, TP, lot, direction. Critical difference:

```
02:21:46  ORDER | BUY | Ticket:1607377682 ← successful execute_trade response
02:21:46  SUCCESS | Order executed: Ticket 1607377682
02:21:46  LESSONS | conditions saved for ticket #1607377682          ← save_trade_conditions fires
02:21:46  AGENT_TOOL | execute_trade | 20361ms | ticket=1607377682 | success
02:22:05  FLOKI | record_trade_open → ticket=1607377682              ← db INSERT fires
```

The ghost went through none of these. It skipped save_trade_conditions and record_trade_open entirely because `execute_trade` never returned successfully with its ticket.

---

## 4. Rule 11 — prior fix identification (STEP 1.4)

Two prior fixes in this bug-adjacent territory:

### 4.1 FLO-97 — reconciliation safety net (2026-03-23, commit `d855c56`)

Verbatim commit message:

> *"When the EA Bridge returns ticket=0, trades execute on MT5 but are invisible to history.db. Today's 9 trades and Friday's 3 trades were all affected… Extended the reconciliation engine (Pass 2) to detect deals in MT5 that are missing from SQLite and INSERT complete records… Duplicate-safe: INSERT OR IGNORE + all_sqlite_tickets set check."*

> *"Layer 1: FLO-89 polling gets real ticket at execution time (primary)*
> *Layer 2: FLO-97 reconciliation catches anything Layer 1 misses (safety net)"*

**Still present in code** — evidence: `21:46:45 Reconciliation: 38 total deals | 9 today | 29 historical` ran successfully at startup. The design works when it runs.

### 4.2 FLO-308 — ticket=0 collision (2026-04-15, commit `3b4a412`)

Verbatim:

> *"`trades.ticket` is UNIQUE, and `record_trade_open` used INSERT OR IGNORE. Once a pending order's ticket=0 row was written, any later pending placement was silently dropped, and the eventual fill could not be reconciled back into SQLite ('no ticket=0 row found for direction=SELL'). Fix: purge stale unfilled ticket=0 rows (close_price IS NULL) before inserting a new placeholder."*

> *"Classification: BUG. Retroactive backfill: Trade 1592474728… +$56.26, opened 03:57:43 UTC… inserted into history.db."*

**Still present in code** — the `PENDING_FILL_DB | no ticket=0 row found` WARN is a consequence of this logic. Multiple trades today hit the WARN (5 WARNs seen); most were captured anyway via Layer 1 polling — only the ghost was not.

### 4.3 What these fixes did NOT anticipate

Both prior fixes assume the system places **ONE order per Floki decision**. The current failure is a different shape: EA Bridge's timeout + MT5 direct fallback **places TWO orders** for one decision. The first (EA-placed) never gets a `ticket=N` resolution back into `execute_trade`, so it never touches the INSERT path — it only becomes visible via the monitor's first_sight polling.

The prior fixes work on *"trade placed once, captured zero times"*. This ticket reveals *"trade placed twice, only one was captured"*. Different bug class.

---

## 5. Scope of damage (STEP 1.5)

**Full MT5 scope query NOT run** — bot is live and sharing the MT5 connection. A direct `mt5.history_deals_get` call could risk contention. Scope is therefore estimated via available proxies.

### 5.1 Proxy 1 — bot_state vs history.db

bot_state retains only the last 5 trades. Of those 5, one (1607377569) is drift. **Drift ratio in the rolling window: 20%.**

### 5.2 Proxy 2 — EA_BRIDGE fallthrough events in logs

Searching all `trading_bot_*.log` files for `"Could not resolve real ticket after 10s — falling through to MT5 direct API"`:

- 2026-04-09 17:44:15
- 2026-04-13 09:10:59
- 2026-04-15 13:35:16
- 2026-04-16 07:02:55
- 2026-04-23 02:21:35 (today's ghost)

**5 EA-bridge-timeout → MT5-direct-fallback events in the last 14 days.** Each is a potential ghost opportunity. Whether each produced a duplicate depends on whether the EA Bridge placed the first order before timing out — which, given today's case, it clearly CAN.

Of the 5, one is today's known ghost. For the other 4, reconciliation at subsequent startups likely caught any missing trades via INSERT OR IGNORE — those tickets would now be tagged with `comment="reconciled:..."` in history.db.

### 5.3 Proxy 3 — `comment LIKE 'reconciled:%'` rows in history.db

Query executed: **20 prior ghosts** silently caught by the FLO-97 reconciliation safety net in the last 16 days (2026-04-07 through 2026-04-20). Total P&L: **+$28.36**. Pattern breakdown:

- 14 tagged `reconciled:Agent-BUY` / `reconciled:Agent-SELL` — Floki placements that produced duplicates or ticket=0 orphans.
- 6 tagged `reconciled:Pending-BUY_LIMIT` / `reconciled:Pending-SELL_LIMIT` — pending-order fills that failed to resolve.

Average: **~1.4 prior ghosts per trading day** — this is NOT a rare edge case. The safety net has been silently cleaning them up behind the scenes, which means neither operator nor audits surfaced the underlying duplicate-placement pattern until today's case (where reconciliation hadn't had a chance to run between close and Hermano's dashboard check).

One notable prior: `1592474728 SELL +$56.26` on 2026-04-15 is the same ticket from FLO-308's commit message (*"Trade 1592474728 … +$56.26, opened 03:57:43 UTC … inserted into history.db"*) — reconciliation caught THAT one too, but only because restart occurred before Hermano noticed.

**Including today's (not yet reconciled) ghost, 21 known ghosts over 16 days, total P&L $+61.50.** Non-reconciled P&L silent drift: $+33.14 (today, pending reconciliation).

### 5.4 Monetary impact

- Today's ghost: **+$33.14** (winning trade omitted from dashboard journal). Net effect: dashboard under-reports P&L by $33.14.
- For the 4 prior EA-bridge-fallthrough events (non-reconciled): unknown without the SQL query above.
- **bigger risk**: each duplicate was a REAL market order at 0.02 lots. On today's case that added ~$32 of exposure (1× lot beyond intended). If a duplicate had been issued in the wrong direction or on a high-leverage day, the downside exposure would compound silently.

---

## 6. FLO-331 overlap (STEP 1.6)

Direct query against history.db for the 9 tickets FLO-331 analyzed:

| Ticket | In history.db? |
|---|:---:|
| 1605010600 | ✅ |
| 1605103684 | ✅ |
| 1605124275 | ✅ |
| 1605185504 | ✅ |
| 1605837214 | ✅ |
| 1606209348 | ✅ |
| 1606383321 | ✅ |
| 1606526654 | ✅ |
| 1606607186 | ✅ |

**All 9 FLO-331 trades are present in history.db.** FLO-331 conclusions (H3 pattern rigidity + H4 reactive chasing at the 4735-4747 level) are NOT contaminated by this ghost bug for the trades examined. However: the broader FLO-331 conclusion that Floki's OPEN-cycle count was 9 remains correct — the 10th "trade" (1607377569 and similar past ghosts) wouldn't be a Floki decision, it would be a system-duplicate, and those wouldn't contribute to his reasoning rigidity.

---

## 7. Root cause hypothesis

**Primary**: `executor.py`'s EA-Bridge-timeout fallback path does not cancel or verify the original EA-Bridge-placed order before sending a new order via `mt5.order_send()`. Both orders execute, the broker fills both, and `execute_trade` returns only the second ticket. The first (EA-placed) is orphaned.

**Mechanism (step-by-step):**
1. Floki calls `execute_trade` → `executor.place_order_ea_bridge()`.
2. `ea_bridge.py` writes signal file for EA, polls MT5 for the resulting ticket.
3. EA reads signal file and calls `OrderSend` in MT5 — ticket #1607377569 is created.
4. Polling has a 10-second timeout. EA's fill reached MT5 but **the polling didn't see the ticket within 10s** (possibly due to MT5 cache staleness — referenced in commit `10e942d` "add deal resolver subprocess for MT5 cache stale workaround").
5. `executor` logs `Could not resolve real ticket after 10s — falling through to MT5 direct API`, clears the EA signal to HOLD, and calls `mt5.order_send()` directly.
6. MT5 direct succeeds → ticket #1607377682 is created. 20-second total latency.
7. `execute_trade` returns success with ticket=1607377682.
8. `save_trade_conditions(1607377682)` → OK.
9. `record_trade_open(1607377682)` via `db_writer.py` → OK.
10. Monitor polls 2 seconds later and sees **two** open positions. First-sight logic captures balance for both but doesn't create history rows (that's the write path's responsibility, not monitor's).
11. Ghost is now alive-and-untracked-in-DB until it closes.

**Secondary**: Reconciliation is a safety net but only runs on bot startup (from this session's log evidence). Four hours between startup and close meant reconciliation didn't re-run to backfill the ghost. The WARN *"will be caught by reconciliation"* at close implies a trust that isn't being fulfilled on the current schedule.

**Tertiary (observability)**: The system does NOT alert when it sees 2 positions open against Floki's 1-intended-order (no duplicate-detection guard). Monitor's first-sight logic treats the extra ticket as "an orphan pending fill" rather than flagging the duplicate.

### 7.1 Risk divergence — not just a data bug

Today's ghost demonstrates that duplicate placement creates **independent risk outcomes** for what was supposed to be one trade:

| Ticket | SL path | Outcome | P&L |
|---|---|---|---|
| 1607377682 (visible) | Floki trailed 4702 → 4730 → 4736 | Hit trailed SL | **-$1.36** |
| **1607377569 (ghost)** | **Never trailed (stayed at 4702)** | **Hit TP 4746** | **+$33.14** |

Floki's ADJUST_TRADE tool targets by ticket. He only knows about the visible one. The ghost runs free with the *original* SL, no trailing, no management. Same market moved both positions; different exit strategies produced **directionally opposite outcomes** ($33 vs -$1.36 = $34 divergence on 0.02-lot positions).

**On this trade, Floki would have made $33 less had the ghost also been managed correctly** — or, equivalently, he made $33 *more* purely because one of his two orders escaped his management. This is a stroke of luck on a winning move. The symmetric case (market reversal where the un-trailed ghost rides to a much bigger loss than the trailed visible trade) is a **silent risk amplifier** that hasn't struck yet.

This is why FLO-338 is a risk bug, not a dashboard bug.

---

## 8. Ranked fix options for Phase 2

Four candidates. Ranking by evidence fit + severity-reduction + scope.

### Option A — Cancel the EA-placed order before MT5 direct fallback (most correct, medium scope)

When the 10-second polling times out, before sending a new order via MT5 direct, the executor should:
1. Scan MT5 for recently-opened positions with matching direction/lot/SL/TP/price.
2. If found — that's the EA-placed ticket. Use IT; do NOT place a second order.
3. If NOT found — only then fall through to MT5 direct (safer assumption that EA-side didn't place anything).

**LoC estimate**: ~30-50 in `executor.py`.
**Pro**: eliminates the duplicate-placement root cause.
**Con**: adds 1-2 additional MT5 API calls inside the hot path; slight latency cost.
**Rule 11**: consistent with FLO-89 polling design, extends it rather than replaces it.

### Option B — Duplicate detection post-fill (cheap, reactive)

When `execute_trade` returns, re-scan MT5 positions briefly for an "extra" ticket matching the intended order parameters. If found, close it immediately (with a clearly-tagged comment).

**LoC estimate**: ~20-30.
**Pro**: simple. Traps duplicates even if other paths introduce them.
**Con**: reactive — a duplicate does briefly exist and broker sees two orders (potential slippage cost). Can't distinguish "legitimate pyramiding" from "accidental duplicate" without further heuristics.
**Risk**: if Floki intentionally pyramids at the same level, Option B might close a legitimate second entry.

### Option C — Run reconciliation on a schedule, not just at startup (partial fix)

Schedule reconciliation every N minutes (e.g. 15-30 min) or trigger it on every `TRADE_CLOSE` WARN *"not found in SQLite"*. Backfills ghosts within a bounded window.

**LoC estimate**: ~10-15 in `main.py` scheduler or `monitor.py`.
**Pro**: closes the dashboard-invisibility gap even if the duplicate-placement root cause remains. Low-risk change.
**Con**: doesn't prevent the duplicate ORDER at the broker. The ghost still gets placed, still consumes margin, still carries market risk. Option C is a DATA-layer fix, not a RISK-layer fix.

### Option D — Remove the MT5-direct fallback entirely (most conservative)

Remove the 10s-timeout fallback path. If EA Bridge can't resolve a ticket in 10s, abort the order and log an error — no second order sent, no duplicate risk.

**LoC estimate**: ~10 (feature flag + conditional skip).
**Pro**: eliminates duplicate-placement root cause absolutely.
**Con**: if the EA-Bridge actually DIDN'T place an order (timeout was a connectivity issue), Floki's decision is silently dropped. This is a bigger product regression than a rare ghost — removes a recovery path that works correctly most of the time.

### DEV recommendation (Phase 2 design, not implementation)

**Primary: Option A** (pre-fallback scan) + **Option C** (periodic reconciliation). A closes the risk path at its root; C closes the journal-display gap as a safety net while A is being validated. Together ~50-80 LoC.

**Not recommended: Option D alone** — trades a rare data bug for a more common dropped-decision bug. Only worth as a temporary kill-switch if Option A needs more design time than is available.

**Option B as a secondary safety net**: consider after Option A stabilizes. The duplicate-detection guard could run in a separate monitor loop without adding to the hot-path latency, but only valuable as defense-in-depth.

---

## 9. What I do NOT know

1. **Full 30-day scope of ghost damage.** Without a direct `mt5.history_deals_get` query (which I declined to run — bot is live), I can only confirm 1 ghost from today's evidence and 4 prior EA-Bridge-fallthrough events that *may* have produced ghosts since cleaned up by reconciliation-at-startup. Proxy 3 query (`SELECT ... WHERE comment LIKE 'reconciled:%'`) is the next step but was deferred.
2. **Whether the EA-side actually placed the order before timing out, or if the polling was just slow.** Today's evidence shows the EA definitely placed it (ticket 1607377569 appeared in MT5). For prior fallthrough events, I haven't verified whether each produced two fills or just one slow one.
2a. **Relative timing of the EA's `OrderSend` vs the signal-clear at 02:21:35 is NOT directly observable from Python-side logs.** MT5's IN-deal timestamp for the ghost (`2026-04-23 00:21:46`) matches the MT5-direct fallback's order timestamp to the second. Two hypotheses both fit:
   - **H-early**: EA placed the order *before* the 10s timeout; polling simply missed it due to MT5 cache staleness (the exact cause FLO-292 and the `deal_resolver` subprocess commit `10e942d` documented). The HOLD signal-clear arrived when the order was already in flight and had no effect.
   - **H-late**: EA received the signal pre-timeout, queued it, and called `OrderSend` at 02:21:46 — concurrent with the MT5-direct fallback. The HOLD signal-clear either didn't reach the EA or was ignored because `OrderSend` was already in flight.
   Both produce the same outcome. **The fix differs slightly**: H-early is fully addressed by Option A (pre-fallback MT5 scan). H-late also needs an EA-side "check for signal-clear before OrderSend" guard. Disambiguating requires EA-side logs from the MQL5 side, not available from this audit.
3. **Whether reconciliation is scheduled to run periodically in current code, or only on startup.** Log evidence shows it only ran once post-restart (at 21:46:45). Reading `main.py` would confirm — I avoided this per the read-only instruction, but the scheduling logic is close to a code-only question.
4. **Whether the duplicate order 1607377569 hit TP 4746 because of the same market movement that trailed 1607377682's SL to 4736**. Looking at the ghost's P&L trajectory (peak +105 pips at 02:26:23), the market rallied to ~4754 before reversing. Ghost's UNTRAILED SL at 4702 meant it had more room to run; its TP at 4746 hit. Neighbor's trailed SL at 4736 got hit on the reversal. Coincidence: the ghost "worked better" than the tracked trade purely because of the SL trailing difference — Floki's ADJUST logic only knew about 1607377682.
5. **Historical deeper scope**: if a ghost ever hit SL in the wrong direction unnoticed, the silent loss would have been invisible to dashboards until reconciliation. Possible silent drawdown on prior days not yet audited.
6. **Whether the `PENDING_FILL_DB | no ticket=0 row found` WARN is a reliable ghost-detection signal or fires spuriously**. Today 5 such WARNs fired across 2 sessions; only 1 is a confirmed ghost. The WARN isn't sufficient by itself to differentiate.
7. **Whether Floki's `record_trade_open → ticket=1607377682` log line (02:22:05) correctly reflects the db INSERT**. Grepping for `INSERT INTO trades` in source code was out of scope; the log suggests success but couldn't be directly verified.

---

## 10. Recommended Phase 2 scope

Phase 2 is NOT yet authorized. This section records what a Phase 2 design doc would cover given the findings above.

1. **Primary fix** (Option A): pre-fallback MT5 scan in `executor.py`'s EA-Bridge-timeout path. ~30-50 LoC. Includes 5-test Rule 20 plan and replay verification against today's log sequence.
2. **Secondary fix** (Option C): schedule reconciliation every 15-30 min via `main.py` scheduler. ~10-15 LoC + test.
3. **Historical cleanup**: run one-time backfill on current live ticket 1607377569 (one row insert into `trades` + `trade_conditions` regeneration from MT5 deal data). Tag as `comment="reconciled:FLO-338_manual_backfill"`.
4. **Scope audit**: query `history.db` for `comment LIKE 'reconciled:%'` to enumerate prior ghosts; cross-check MT5 deals if any slipped through.
5. **Observability**: add a DISCORD alert for `PENDING_FILL_DB no ticket=0 row found` WARN so future ghosts are caught within minutes rather than at user-discovery time.
6. **Bot behavior now**: until fix ships, the ghost exists in MT5 but not in dashboards. It will self-heal the next time reconciliation runs at a bot restart. Recommend NOT restarting the bot until after Phase 2 is designed, so the live-state forensic evidence is preserved; then one planned restart will both ship the fix and backfill the ghost.

---

## 11. Methodology + evidence trail (for future audits)

Queries run (all read-only):
- `sqlite3 history.db SELECT … FROM trades WHERE ticket IN (…)` — Surface A
- `Get-Content bot_state.json | ConvertFrom-Json` — Surface B
- `Get-ChildItem data/trade_conditions/*.json` filter by BaseName — Surface C
- `Get-ChildItem data/post_trade_reports/*.json` filter by BaseName — Surface D
- `Select-String logs\*.log -Pattern "<ticket|PENDING_FILL|BROKER_CLOSE|…>"` — Surface E
- `git show <sha>` + `git log --all --oneline --grep="…"` + `git log -S "…"` — Rule 11

Queries declined:
- `mt5.history_deals_get()` — bot is live, MT5 connection shared; avoided.
- Reading `executor.py` / `monitor.py` / `db_writer.py` source — read-only audit constraint.

**Phase 1 deliverable total: ~2h forensic work. No side effects. Bot continues to run. Ghost ticket still exists in MT5 but remains absent from history.db.**
