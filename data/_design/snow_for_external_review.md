# Snow — Technical Architecture for External Review

**Audience:** professional trader reviewing whether Snow's
primitive vocabulary can express the patterns you actually trade.
**Assumptions:** trading domain expertise (RSI, MACD, Bollinger,
divergence, liquidity sweeps); no Pydantic / Python knowledge
required.
**Scope:** Snow's plan model, the 21 condition primitives, the
plan lifecycle, three worked examples covering different
complexity levels, and an honest list of what Snow cannot express.
**Out of scope:** source code walkthrough, backtesting
methodology, MT5 integration details, roadmap beyond Phase 8b.

---

## Table of Contents

1. [System overview](#1-system-overview)
2. [Plan structure — the form Floki fills](#2-plan-structure)
3. [The 21 primitives catalog](#3-the-21-primitives-catalog)
4. [Plan lifecycle](#4-plan-lifecycle)
5. [Stateful primitives deep dive](#5-stateful-primitives-deep-dive)
6. [Three worked examples](#6-three-worked-examples)
7. [Honest limitations](#7-honest-limitations)
8. [Glossary](#8-glossary)

---

## 1. System overview

### 1.1 Why Snow exists — projective vs reactive

The system has two collaborating agents on XAU/USD:

- **Floki** — the LLM-driven decision-maker. One cycle every
  5–30 minutes (Floki self-schedules). On each cycle, Floki sees
  the world (charts on 6 timeframes, indicators, regime, news
  sentiment, macro, open positions, active plans) and decides
  what to do next. Floki has **agency** — every tool call is its
  own choice.
- **Snow** — the deterministic event-driven executor. One tick
  every 5 seconds. On each tick, Snow checks every active plan's
  conditions against the current market state. When a plan's
  conditions become all-true, Snow fires the corresponding
  action — opens a position, moves stop-loss to break-even,
  trails a stop, closes partially or fully.

The split is **deliberate**. An LLM reasoning every 5 seconds
would be expensive, slow, and prone to flip-flopping. A
deterministic loop reasoning every cycle would have no judgment.
The combination — Floki at human-like cadence, Snow at
machine-like cadence — gets both.

In paradigm terms: Floki is **projective** (writes the future as
a contingent plan), Snow is **reactive** (executes the plan when
conditions become true). The contract between them is the plan
itself, which is a JSON document with a fixed schema.

### 1.2 The Floki → Snow handshake

```
                      Floki cycle (5–30 min)                    Snow tick (5 s)
   ┌──────────────────────────────────┐         ┌────────────────────────────────────┐
   │ 1. receive data package          │         │ for every plan in PENDING/ACTIVE:  │
   │ 2. form thesis                   │         │   ── re-read current market data   │
   │ 3. draft plan(s)                 │         │   ── evaluate every condition      │
   │ 4. submit_plan_to_snow(plan)     │ ───→    │   ── if all-true: fire action      │
   │ 5. record plan_id in notes       │         │   ── persist state cache (60-tick) │
   │ 6. decision = WAIT (Snow watches)│         │ ────────────────────────────────── │
   └──────────────────────────────────┘         └────────────────────────────────────┘
                                                                  │
                                                                  ▼
                                                       MT5 executor (live trades)
```

Floki cannot interfere with a plan in flight without explicitly
calling `cancel_plan(plan_id, reason)`. Once submitted, a plan is
contractual — Snow honors it until terminal state (closed,
expired, cancelled, or failed). This prevents the "manager who
changes their mind every candle" anti-pattern that plagues most
naive bot designs.

### 1.3 Architectural principles

- **Plans are immutable once submitted.** Floki cannot mutate a
  plan's `entry`, `management`, `exit`, or `emergency` blocks
  after submit. To change strategy, Floki cancels and submits a
  new plan.
- **AND within a block; OR via paired plans.** Conditions inside
  a single block are joined with logical AND. To express an OR,
  Floki submits two parallel plans (one per branch). One of them
  fires, the other expires.
- **Floki is the sole decision-maker.** Snow has zero discretion.
  Snow does not "improve" a plan's SL or "interpret" a fuzzy
  condition. Snow does what the plan says, fail-safe to no-op
  on missing data.
- **Snow is restart-safe.** Crash, restart, or planned outage:
  Snow reconciles every active plan against MT5 reality on
  startup, recovers cached condition state from disk, and
  resumes. A documented one-tick false-negative window applies
  to stateful conditions across long outages (>15 min).

---

## 2. Plan structure

A plan is a JSON document with **6 blocks**. Floki fills all of
them; Snow validates and persists.

### 2.1 The 6 blocks

| Block | Purpose | Cardinality |
|---|---|---|
| `analysis` | thesis + key levels + assumed regime + confidence | 1 (mandatory) |
| `entry` | direction + volume + conditions + initial SL/TP | 1 (mandatory) |
| `management` | rules to move SL, BE-lock, partial profits, trail | 0..N |
| `exit` | invalidation conditions that close the trade | 0..N |
| `emergency` | max loss pips, max duration, broker-error fallback | 1 (mandatory) |
| `expires_at` | auto-cancel if entry never fires by this time | optional |

Plus `id`, `created_at`, `created_by` (always `"floki"`), and
`schema_version` (currently `2`).

### 2.2 The entry block (where the trade comes from)

```json
"entry": {
  "direction": "BUY",
  "volume": 0.02,
  "conditions": [
    { "type": "price_above", "level": 4730.0 },
    { "type": "rsi", "tf": "M15", "op": "below", "threshold": 70 }
  ],
  "initial_sl": 4720.0,
  "initial_tp": 4750.0
}
```

- `direction` — `BUY` or `SELL`
- `volume` — lots (broker units)
- `conditions` — list of primitives joined with AND
- `initial_sl` / `initial_tp` — hard prices
- Validator enforces SL < entry < TP for BUY, mirror for SELL

### 2.3 The management block (where stops move)

Each management contingency is a named rule with its own
conditions and action.

```json
"management": [
  {
    "name": "lock_be_at_10",
    "priority": 7,
    "conditions": [{"type": "profit_pips", "op": "above", "threshold": 10}],
    "action": {"type": "move_sl_to_breakeven", "offset_pips": 0},
    "fires": "once",
    "guards": {"only_if_tighter_sl": true, "cooldown_seconds": 60}
  }
]
```

- `priority` — 1..10, higher wins when multiple contingencies
  fire on the same tick. Action category dominates: a
  `close_full` always beats an `adjust_sl` regardless.
- `fires` — `"once"` (latch) or `"every_tick"`.
- `guards` — optional guards to prevent loosening SL or rapid
  re-firing.

### 2.4 The exit block (where the thesis is invalidated)

Same structure as management but the action is typically
`close_full`. Exits exist to close a trade when the original
thesis is no longer supported, even before TP/SL are hit.

### 2.5 The emergency block (the floor)

```json
"emergency": {
  "max_loss_pips": 150,
  "max_duration_minutes": 480,
  "on_broker_error": "alert_floki"
}
```

This is the only block evaluated **every tick** without regard to
contingency state. It's the hard floor: if loss exceeds
`max_loss_pips`, position is closed. If duration exceeds
`max_duration_minutes`, position is closed.

### 2.6 Action types

| Action | Where | Effect |
|---|---|---|
| `execute_market` | entry only | open position at market |
| `adjust_sl` | management | set new SL price |
| `adjust_tp` | management | set new TP price |
| `move_sl_to_breakeven` | management | SL = open price + `offset_pips` |
| `move_sl_to_price` | management | SL = explicit price |
| `trail_sl` | management | trailing stop with `distance_pips` |
| `close_full` | exit | close entire position |
| `close_partial` | management | close `percent` of remaining volume |

---

## 3. The 21 primitives catalog

Primitives are the building blocks of a plan's `conditions` lists.
Every primitive has a `type` discriminator string + parameters.

### 3.1 Price (point-in-time, stateless) — 2 primitives

#### `price_above`
```json
{"type": "price_above", "level": 4730.0}
```
True iff current mid-price > `level`. Strict.

#### `price_below`
```json
{"type": "price_below", "level": 4720.0}
```
True iff current mid-price < `level`. Strict.

### 3.2 Indicator (point-in-time, stateless) — 7 primitives

#### `rsi`
```json
{"type": "rsi", "tf": "H1", "op": "above", "threshold": 70}
```
- `tf` ∈ {M1, M5, M15, H1, H4, D1}
- `op` ∈ {above, below}, strict
- `threshold` ∈ [0, 100]

#### `macd_histogram`
```json
{"type": "macd_histogram", "tf": "H1", "op": "above", "threshold": 0.0}
```
MACD histogram value vs threshold. Standard 12/26/9.

#### `ema_relation`
```json
{"type": "ema_relation", "tf": "H1", "period": 50, "relation": "price_above"}
```
- `period` ∈ {9, 21, 50, 200}
- `relation` ∈ {price_above, price_below, aligned_bull, aligned_bear}
- `aligned_bull` = EMA9 > EMA21 > EMA50 > EMA200 (and inverse for bear)

#### `atr`
```json
{"type": "atr", "tf": "H1", "op": "above", "threshold": 5.0}
```
ATR(14) value (in price units, not pips) vs threshold. Used for
volatility gates ("don't enter if M5 ATR > 1.8%").

#### `stochastic`
```json
{"type": "stochastic", "tf": "H1", "op": "below", "threshold": 20}
```
Stochastic %K (0–100) vs threshold. Currently published only on H1.

#### `bollinger_position`
```json
{"type": "bollinger_position", "tf": "H1", "relation": "below_lower"}
```
- `relation` ∈ {above_upper, below_lower, above_middle, below_middle, in_squeeze}
- `in_squeeze` is bandwidth < threshold (Brain pre-computes)

#### `indicator_divergence`
```json
{"type": "indicator_divergence", "indicator": "macd", "direction": "bullish"}
```
- `indicator` ∈ {macd} (rsi divergence requires Brain extension; deferred)
- `direction` ∈ {bullish, bearish}
- True iff Brain's divergence detector reports `{detected: true, type: <direction>}`

### 3.3 Structural / level proximity (point-in-time, stateless) — 3 primitives

#### `price_at_sr_zone`
```json
{"type": "price_at_sr_zone", "tf": "H1", "side": "above_support",
 "tolerance_pips": 5}
```
- `side` ∈ {above_support, below_resistance, in_zone}
- Brain computes S/R zones per timeframe; this primitive checks
  proximity within `tolerance_pips`.

#### `price_at_fibonacci`
```json
{"type": "price_at_fibonacci", "level": 0.618, "tolerance_pips": 5}
```
- `level` ∈ {0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618}
- Brain computes Fib retracement on the dominant swing.

#### `price_at_pivot`
```json
{"type": "price_at_pivot", "set": "classic", "level": "S1",
 "tolerance_pips": 5}
```
- `set` ∈ {classic, fibonacci}
- `level` ∈ {PP, R1, R2, R3, S1, S2, S3}
- `tolerance_pips` is mandatory (no default — Floki must commit)

### 3.4 Position-state (require ACTIVE plan) — 4 primitives

These read from the per-plan tracker (in-memory state seeded on
first ACTIVE observation, refreshed every tick). They short-circuit
to False if the plan is not ACTIVE (i.e., position not yet open).

#### `profit_pips`
```json
{"type": "profit_pips", "op": "above", "threshold": 10}
```
Current profit in pips vs threshold. Used for BE-lock, partial-profit.

#### `mfe_reached`
```json
{"type": "mfe_reached", "op": "above", "threshold": 30}
```
Maximum Favorable Excursion (peak profit) reached vs threshold.
Used for trailing-stop activation ("only trail after we've seen
30 pips profit at some point").

#### `mae_reached`
```json
{"type": "mae_reached", "op": "below", "threshold": -20}
```
Maximum Adverse Excursion (worst drawdown) reached vs threshold.

#### `profit_retraced_from_peak`
```json
{"type": "profit_retraced_from_peak", "percent": 50}
```
True iff current profit has retraced ≥ `percent`% from peak MFE.
Used for "give back half of profits → close" exit.

### 3.5 Time / clock — 2 primitives

#### `duration_exceeds`
```json
{"type": "duration_exceeds", "minutes": 240}
```
True iff plan has been ACTIVE for ≥ `minutes` minutes. Used for
time-stop exits.

#### `time_between`
```json
{"type": "time_between", "start_utc": "08:00", "end_utc": "16:00"}
```
True iff current UTC time is in [start, end]. Cross-midnight
windows allowed (e.g., `"22:00"` → `"06:00"`).

### 3.6 Stateful (require schema_version ≥ 2) — 3 primitives

These carry memory across ticks. See [Section 5](#5-stateful-primitives-deep-dive)
for the state model and restart semantics.

#### `indicator_crossover`
```json
{"type": "indicator_crossover", "indicator": "rsi", "tf": "H1",
 "direction": "below", "threshold": 30}
```
- Fires on the FIRST tick where `indicator` crosses `threshold` in
  `direction`. Continuation does NOT re-fire.
- `indicator` ∈ {rsi, macd_histogram, stochastic}
- Cold-start (no prev): seed prev=current, return False on tick 1.
- Equality at threshold: ambiguous; preserves last definite state.

#### `indicator_was`
```json
{"type": "indicator_was", "indicator": "rsi", "tf": "H1",
 "op": "below", "threshold": 30, "within_bars": 4}
```
- True iff `indicator` was `op threshold` in any of the last
  `within_bars` closed bars on `tf`.
- `within_bars` ∈ [1, 20]
- Sliding window updated on bar boundaries (deduped via close
  timestamp); 5 ticks within one bar produce 1 append.
- Useful for "RSI was oversold within last 4 H1 bars" recovery
  setups, where the qualifying event has already passed by the
  time you want to act.

#### `price_crossed_level`
```json
{"type": "price_crossed_level", "direction": "below", "level": 4720}
```
- One-shot latch. Fires when mid-price crosses `level` in
  `direction`; stays True for the rest of the plan's lifetime.
- Detection: `prev (strict) level AND curr (inclusive) level`
  — a tick landing exactly on the level still counts as a
  successful tag from the previous side.
- No mid-plan reset (per CEO Q3 decision). Use paired plans for
  "fire on each cross."
- Useful for sweep/tag detection: AND with `price_above` for
  "tagged 4720 from above and is now back above 4725."

---

## 4. Plan lifecycle

### 4.1 State diagram

```
                        Floki cancels
                  ┌────────────────────────────┐
                  │                             ▼
   submit_plan ─→ PENDING ──[entry conds met]──→ TRIGGERED ──→ CANCELLED
                  │                              │
                  │                              │ executor returns OK
                  │ expires_at past              ▼
                  ▼                            ACTIVE ←─────┐
                EXPIRED                          │           │
                                                 │ exit/em.  │ closing
                                                 │ fired     │ failed
                                                 ▼           │ → revert
                                              CLOSING ───────┘
                                                 │
                                                 │ executor close OK
                                                 ▼
                                              CLOSED  (terminal)

   At any point: irrecoverable broker error → FAILED  (terminal)
```

### 4.2 State semantics

| State | Meaning | Snow's behavior |
|---|---|---|
| `PENDING` | plan submitted, entry not yet fired | evaluates entry conditions every tick |
| `TRIGGERED` | entry conds satisfied, executor call in flight | transient ≤60s |
| `ACTIVE` | position open, managed by Snow | evaluates management + exit + emergency every tick |
| `CLOSING` | exit fired or emergency hit, close call in flight | transient ≤60s |
| `CLOSED` | position fully closed; outcome recorded | terminal |
| `EXPIRED` | `expires_at` passed without entry firing | terminal |
| `CANCELLED` | Floki called `cancel_plan(plan_id, reason)` | terminal |
| `FAILED` | unrecoverable error (broker reject, position vanished) | terminal |

### 4.3 Cadences

- **Snow tick:** 5 seconds. Every active plan re-evaluates every
  condition.
- **State cache flush:** 60 ticks (5 minutes). Per-condition
  state for stateful primitives is serialized to disk.
- **Floki cycle:** 5–30 minutes. Self-scheduled by Floki based
  on context — tight schedule for active trades, loose for
  ranging markets.

### 4.4 Startup recovery (FLO-354, shipped 2026-04-26)

On every bot startup, before the Snow tick loop spawns:

1. Read every plan in a non-terminal state.
2. Single batch query to MT5 for open positions (filter by magic
   number).
3. Per plan, reconcile the recorded state against MT5 reality:
   - PENDING + `expires_at` past → mark EXPIRED.
   - TRIGGERED + position exists → ACTIVE + tracker seed.
   - TRIGGERED + no position → FAILED (`crash_during_trigger`).
   - CLOSING + position exists → revert to ACTIVE (close retries).
   - CLOSING + no position → CLOSED + outcome backfill.
   - ACTIVE + no position + deal history non-empty → CLOSED + outcome backfill.
   - ACTIVE + no position + deal history empty → FAILED (`position_vanished`).
   - ACTIVE + position exists → tracker reseed only.
4. If MT5 disconnect during the batch query, abort and refuse to
   spawn the loop. Operator handles it.

### 4.5 Outcome backfill (FLO-353, shipped 2026-04-26)

After every CLOSED transition (whether via Snow's close action or
recovery's reconciliation), Snow queries MT5 deal history for
the position ticket and computes:
- `outcome_usd = sum(deal.profit for deal in close_deals)`
- `outcome_pips = ((vw_close_price - open_price) * direction_sign) / pip_size`
  where `vw_close_price` is the volume-weighted average across
  partial closes.

Backfill is **best-effort**: if MT5 deal history is unavailable,
the outcome columns stay NULL with an audit row recorded. The
trade itself closed successfully; outcomes are observability,
not correctness.

---

## 5. Stateful primitives deep dive

### 5.1 Why stateful is hard

Most condition primitives are **point-in-time**: read one value
now, compare to a threshold, return bool. No memory needed.

Stateful primitives observe **transitions** (crossover) or
**recent history** (sliding window) or **one-shot events**
(latch). They need memory across ticks. Memory means state.
State means: where does it live, when does it persist, what
happens on restart, can it drift, can it leak.

### 5.2 The state model

Three layers:

```
Per-tick (in-memory):
   PerConditionStateCache  keyed by (plan_id, contingency_name, condition_index)
       holds {prev_value, prev_above_threshold, bar_history, latched, last_seen_at}

Per-plan (on disk):
   snow_plans.state_cache_json  TEXT NULL
       JSON list, one row per stateful condition for this plan

Restart recovery:
   rehydrate_state_cache()  on bot start, reads state_cache_json
       drops rows older than STALE_STATE_THRESHOLD_MINUTES (15)
```

### 5.3 The flush cycle

The cache is flushed to disk every **60 ticks (5 minutes)** OR on
plan terminal transition. This is a deliberate trade-off:

- More frequent → more DB writes, less restart-recovery loss.
- Less frequent → less DB churn, larger window of state lost on
  crash.
- 5 minutes balances DB load vs. observable false-negative on
  restart.

Performance budget (RFC §8.2): full flush of 100 plans × 8
conditions = ~50 ms; well under the per-tick 200 ms warning
threshold.

### 5.4 The 15-minute stale threshold

On rehydrate, any row whose `last_seen_at` is older than 15
minutes is dropped. The condition cold-starts on its next tick
— prev_value is seeded from current, the first tick reports no
crossing.

This means: a bot down for ≤15 min recovers stateful conditions
losslessly. A bot down >15 min may miss one crossing event per
condition. This is documented and operator-visible; Floki's
prompt explicitly mentions this caveat.

### 5.5 The false-negative window

The first tick after a cold-start (fresh allocation OR
post-rehydrate stale-drop) cannot detect a crossing — there's no
`prev` to compare against. From the second tick onward, detection
works.

Worst case for a 5-second tick: 5 seconds of detection latency on
a single plan, once. For hour-scale conditions like RSI on H1,
this is negligible. For tick-sensitive scalping conditions (M1
crossings), this is closer to the noise floor — operators
choosing tight crossover plans should know.

---

## 6. Three worked examples

Each example shows: market scenario, Floki's thesis in plain
language, the full plan JSON, how Snow evaluates it tick-by-tick,
and possible outcomes.

### 6.1 Example 1 — Simple breakout BUY (point-in-time only)

**Market scenario.** XAU/USD at 14:30 UTC, in a tight 4720–4730
H1 range for 6 hours. M15 chart shows higher lows. Floki sees
RSI H1 at 55 (neutral with bullish bias), MACD histogram
positive but flat. ATR M5 normal (~3.5 pips).

**Floki's thesis.** "If price breaks 4730 with RSI confirming
above 60, the breakout is real and I want to ride it. SL goes
back inside the range; TP at the next H1 resistance at 4750.
After 10 pips profit, lock break-even."

**The plan.**

```json
{
  "schema_version": 2,
  "id": "PLAN-20260426-001",
  "created_at": "2026-04-26T14:30:00Z",
  "expires_at": "2026-04-26T18:00:00Z",
  "analysis": {
    "thesis": "M15 higher-lows + tight H1 range → breakout above 4730",
    "key_levels": [4730.0, 4720.0, 4750.0],
    "confidence": 65,
    "regime_assumed": "BREAKOUT_PENDING"
  },
  "entry": {
    "direction": "BUY",
    "volume": 0.02,
    "conditions": [
      { "type": "price_above", "level": 4730.0 },
      { "type": "rsi", "tf": "H1", "op": "above", "threshold": 60 }
    ],
    "initial_sl": 4720.0,
    "initial_tp": 4750.0
  },
  "management": [
    {
      "name": "lock_be_at_10",
      "priority": 7,
      "conditions": [{"type": "profit_pips", "op": "above", "threshold": 10}],
      "action": {"type": "move_sl_to_breakeven", "offset_pips": 0},
      "fires": "once"
    }
  ],
  "exit": [
    {
      "name": "rsi_invalidation",
      "priority": 9,
      "conditions": [{"type": "rsi", "tf": "H1", "op": "below", "threshold": 50}],
      "action": {"type": "close_full"},
      "fires": "once"
    }
  ],
  "emergency": {
    "max_loss_pips": 100,
    "max_duration_minutes": 240,
    "on_broker_error": "alert_floki"
  }
}
```

**How Snow evaluates.**

- Tick 1 (14:30:05). Price=4727, RSI H1=55. Entry conds:
  price_above=False, rsi=False. Plan stays PENDING.
- Tick 60 (14:35:05). Price=4729, RSI H1=58. Same.
- Tick 240 (15:00:05). Price=4731, RSI H1=62. Both true → fires.
  Snow places BUY @ 4731 with SL=4720, TP=4750. Plan transitions
  PENDING → TRIGGERED → ACTIVE.
- Tick 300 (15:05:05). Price=4733, profit=2 pips. management
  conds: profit>10 = False. exit conds: rsi<50 = False.
- Tick 600 (15:25:05). Price=4742, profit=11 pips. management
  fires: SL moves to 4731 (entry). Latched (`fires: once`).
- Tick 900–...  Price=4748, profit=17 pips. SL still 4731.
- Tick X. Price reaches 4750.0 → TP hit at broker. Position
  closes externally. Snow's next tick sees position gone →
  CLOSING → CLOSED. Outcome backfill: outcome_pips=190
  (4750-4731 = 19 *10 pips), outcome_usd=$38.

**Possible outcomes.**

- TP hit (profit). 4750 reached → CLOSED, outcome positive.
- SL hit before BE-lock. Price drops to 4720 first → CLOSED at SL,
  outcome ~−110 pips.
- BE-lock then SL drift back. Profit reaches 11 pips, BE locks,
  then price drops back to 4731 → CLOSED at break-even, outcome
  ~0.
- RSI exit. RSI H1 drops below 50 mid-trade → exit fires →
  close_full. Outcome whatever the position was worth at that
  moment.
- Expiration. 14:30→18:00 passes without entry firing → EXPIRED,
  no position ever opened.

### 6.2 Example 2 — Mean-reversion with divergence (Phase 7.3 primitives)

**Market scenario.** XAU/USD on H1 at 21:15 UTC London close.
Price has been pushed to 4760 in a parabolic 3-hour move; RSI H1
at 76 (overbought). Brain detects bearish MACD divergence
(price higher highs, MACD lower highs) on H1. Bollinger upper
band at 4762 — price tagging it. ATR H1 elevated.

**Floki's thesis.** "Exhaustion-pop signature. RSI overbought,
MACD divergence, BB upper tag, post-London-close liquidity
thinning. I want a SELL targeting the 20-period Bollinger middle
(daily mean) at ~4742. SL above the parabolic high. Use the
divergence as a confirming condition so I don't fade strength
without it."

**The plan.**

```json
{
  "schema_version": 2,
  "id": "PLAN-20260426-002",
  "created_at": "2026-04-26T21:15:00Z",
  "expires_at": "2026-04-26T23:30:00Z",
  "analysis": {
    "thesis": "Parabolic exhaustion + bearish MACD divergence + BB upper tag",
    "key_levels": [4760.0, 4742.0, 4765.0],
    "confidence": 72,
    "regime_assumed": "TRENDING_BULLISH_EXTENDED"
  },
  "entry": {
    "direction": "SELL",
    "volume": 0.03,
    "conditions": [
      { "type": "bollinger_position", "tf": "H1", "relation": "above_upper" },
      { "type": "rsi", "tf": "H1", "op": "above", "threshold": 70 },
      { "type": "indicator_divergence", "indicator": "macd",
        "direction": "bearish" }
    ],
    "initial_sl": 4765.0,
    "initial_tp": 4742.0
  },
  "management": [
    {
      "name": "lock_be_at_8",
      "priority": 7,
      "conditions": [{"type": "profit_pips", "op": "above", "threshold": 8}],
      "action": {"type": "move_sl_to_breakeven", "offset_pips": 0},
      "fires": "once"
    },
    {
      "name": "trail_after_15",
      "priority": 6,
      "conditions": [{"type": "mfe_reached", "op": "above", "threshold": 15}],
      "action": {"type": "trail_sl", "distance_pips": 8},
      "fires": "every_tick"
    }
  ],
  "exit": [
    {
      "name": "thesis_invalidation",
      "priority": 9,
      "conditions": [
        { "type": "price_above", "level": 4763.0 },
        { "type": "rsi", "tf": "H1", "op": "above", "threshold": 80 }
      ],
      "action": {"type": "close_full"},
      "fires": "once"
    },
    {
      "name": "give_back_half",
      "priority": 5,
      "conditions": [
        { "type": "mfe_reached", "op": "above", "threshold": 12 },
        { "type": "profit_retraced_from_peak", "percent": 50 }
      ],
      "action": {"type": "close_full"},
      "fires": "once"
    }
  ],
  "emergency": {
    "max_loss_pips": 80,
    "max_duration_minutes": 180,
    "on_broker_error": "alert_floki"
  }
}
```

**How Snow evaluates (notable transitions).**

- Tick 1 (21:15). All 3 entry conds true (price above BB upper,
  RSI 76, divergence detected). Snow fires SELL @ 4760.5 with
  SL=4765, TP=4742. ACTIVE.
- Tick ~30 (21:17:30). Price=4759, profit=1.5 pips. Tracker
  records MFE=1.5.
- Tick ~360 (21:45). Price=4752, profit=8.5 pips. management
  `lock_be_at_8` fires once: SL → 4760.5.
- Tick ~720 (22:15). Price=4747, profit=13.5 pips, MFE=14. trail
  not yet active (MFE < 15).
- Tick ~750 (22:17:30). Price=4744.5, profit=16, MFE=16. trail
  fires: SL = 4744.5 + 8 = 4752.5.
- Tick ~840 (22:22:30). Price=4742.0 → TP hit. CLOSED. outcome
  ≈ +185 pips ($55.50).

**Possible outcomes.**

- TP hit (clean): standard win, ~185 pips.
- Trailing stop hit: price retraces from 4744 → 4752 → SL hit at
  trail level, outcome ~+80 pips.
- BE-lock then revert: profit reaches 8 pips, BE locks, price
  comes back → close at break-even.
- Exit thesis_invalidation: rare — needs both price>4763 AND
  RSI>80 simultaneously, would mean the parabolic continues
  hard. Close at SL or there.
- Exit give_back_half: MFE reached 12+ then retraced 50% →
  partial-profit-protection close.
- Emergency: 80-pip loss or 180-min duration → forced close.

### 6.3 Example 3 — Tag-and-bounce with stateful latch (Phase 8b primitives)

**Market scenario.** XAU/USD at 09:00 UTC London open. Price
sitting at 4738. There's a clearly visible **liquidity pool**
just below 4720 (multiple H1 swing lows clustered there). The
expectation among order-flow traders is that a sweep below 4720
flushes the stop-loss orders, then institutional buyers absorb
and push price back up.

**Floki's thesis.** "I want to BUY the sweep — but only after
price has actually swept below 4720 (proves the pool was tested)
AND has reclaimed back above 4725 (proves absorption). Without
the sweep, this is just a level break. With both, it's a textbook
tag-and-bounce."

This is **only expressible** with stateful primitives:
`price_crossed_level(direction='below', level=4720)` latches
True the moment the sweep happens (and stays True even when
price comes back up). Combined via AND with `price_above(4725)`,
the entry only fires AFTER the sweep AND when price has
reclaimed.

**The plan.**

```json
{
  "schema_version": 2,
  "id": "PLAN-20260426-003",
  "created_at": "2026-04-26T09:00:00Z",
  "expires_at": "2026-04-26T12:00:00Z",
  "analysis": {
    "thesis": "Liquidity sweep below 4720 then reclaim above 4725 → BUY",
    "key_levels": [4720.0, 4725.0, 4740.0, 4750.0],
    "confidence": 68,
    "regime_assumed": "RANGING_PRE_BREAKOUT"
  },
  "entry": {
    "direction": "BUY",
    "volume": 0.02,
    "conditions": [
      { "type": "price_crossed_level", "direction": "below", "level": 4720.0 },
      { "type": "price_above", "level": 4725.0 }
    ],
    "initial_sl": 4717.0,
    "initial_tp": 4750.0
  },
  "management": [
    {
      "name": "lock_be_at_8",
      "priority": 7,
      "conditions": [{"type": "profit_pips", "op": "above", "threshold": 8}],
      "action": {"type": "move_sl_to_breakeven", "offset_pips": 0},
      "fires": "once"
    },
    {
      "name": "scale_out_half_at_15",
      "priority": 6,
      "conditions": [{"type": "profit_pips", "op": "above", "threshold": 15}],
      "action": {"type": "close_partial", "percent": 50},
      "fires": "once"
    }
  ],
  "exit": [
    {
      "name": "rsi_recovery_complete",
      "priority": 5,
      "conditions": [
        { "type": "indicator_was", "indicator": "rsi", "tf": "H1",
          "op": "below", "threshold": 35, "within_bars": 3 },
        { "type": "rsi", "tf": "H1", "op": "above", "threshold": 60 }
      ],
      "action": {"type": "close_partial", "percent": 30},
      "fires": "once"
    }
  ],
  "emergency": {
    "max_loss_pips": 100,
    "max_duration_minutes": 180,
    "on_broker_error": "alert_floki"
  }
}
```

**How Snow evaluates.**

The interesting tick-by-tick:

- Tick 1 (09:00:05). Price=4738.0. Entry:
  - `price_crossed_level`: cold-start, prev=4738, latched=None.
    Returns False.
  - `price_above 4725`: True.
  - AND → False. PENDING.
- Tick 200 (09:16:45). Price=4732.5. Both still on the same side
  of 4720. price_crossed_level prev updated, still no latch.
  AND → False.
- Tick 600 (09:50:05). Price=4719.5. **Sweep happens.**
  - price_crossed_level: prev=4720.5, curr=4719.5. prev>level
    AND curr<=level → fires. **state.latched = True.** Returns
    True.
  - price_above 4725: 4719.5 > 4725 → False.
  - AND → False. Plan still PENDING but latch is set.
- Tick 720 (10:00:05). Price=4722. price_crossed_level returns
  True (latched). price_above=False. AND → False.
- Tick 880 (10:13:25). Price=4725.5. **Reclaim.**
  - price_crossed_level: latched. True.
  - price_above 4725: 4725.5 > 4725 → True.
  - AND → True. Snow fires BUY @ 4725.5 with SL=4717, TP=4750.

The latch is the key: once price swept 4720, that fact persists
forever (until plan is terminal). The combination (sweep happened
+ now back above 4725) cannot be expressed without state. A naive
plan using only `price_above` + `price_below` would either fire
on every tick price was above 4725 (no sweep verification) or
miss the entry entirely (price_below 4720 only true during the
sweep itself).

**The exit logic uses `indicator_was`.** Once the BUY is open,
the exit `rsi_recovery_complete` fires when RSI was oversold in
the last 3 H1 bars AND is now above 60 — i.e., the recovery has
played out. Partial scale-out at 30% protects the win. The plan
keeps the remaining 70% to ride further if momentum continues.

**Possible outcomes.**

- Sweep + reclaim fires → ride to TP at 4750 (~245 pips).
- Sweep happens but price doesn't reclaim → entry never fires →
  plan EXPIRED at 12:00.
- Sweep + reclaim fires, but the move fails immediately — SL at
  4717 takes ~85-pip loss.
- Sweep + reclaim fires, BE-lock kicks in, price grinds higher,
  scale-out half at 15 pips, ride remainder to TP.
- Sweep + reclaim fires, RSI recovery exit triggers a partial
  close at the 30% level — protects against a full reversal.

---

## 7. Honest limitations

What Snow **cannot** express today, with the why:

### 7.1 Sustained crossover

"MACD crossed positive AND has stayed positive for 3 bars."
Approximation: `indicator_crossover(direction='above', threshold=0)`
+ `indicator_was(op='above', threshold=0, within_bars=3)`. Two
primitives ANDed. Works for most cases but doesn't precisely
distinguish "just crossed" from "crossed 3 bars ago and held."

### 7.2 Multi-event sequential patterns

"Did X, then did Y, then did X again." No native primitive. The
v2 schema allows expressing this via **plan chaining** (a plan's
exit fires `submit_plan` for the next leg), but it's verbose.
Not a Phase 8b shipped feature.

### 7.3 Geometric pattern recognition

Triangles, flags, head-and-shoulders, double-tops. No primitives
for shape detection. Floki can detect these in chart screenshots
and describe them in the analysis block, but Snow does not
monitor pattern integrity over time. If the pattern breaks
mid-trade, the exit conditions need to express the breakage in
primitive terms (e.g., `price_below specific_level`).

### 7.4 Order-flow / volume profile / VWAP

These belong to Cat B in the FLO-347 RFC — separate computation
pipeline, not yet built. Snow's data layer (LiveData + Brain)
does not expose VWAP, order-flow imbalance, or volume profile
nodes. Adding them would require new Brain primitives.

### 7.5 Liquidity-sweep semantic primitive

`price_crossed_level` is the building block; combined with
`price_above`/`price_below`, you can express "tag and reclaim."
A formal `liquidity_swept` primitive (with swing-high/low
identification + sweep latch + reversal detection in one) is
deferred to Cat D, future RFC.

### 7.6 Cross-plan coordination

Each plan's state is private. There is no shared "regime is
bullish" flag visible to all plans. Floki recomposes the regime
view at every cycle. If the trader uses multi-plan portfolios
where one plan's behavior depends on another's state, that
coordination has to flow through Floki's prompt context.

### 7.7 Logical operators inside a block

A block's `conditions` are joined with **AND only**. There is
no `OR`, `NOT`, or arbitrary boolean expression at the block
level. Equivalent expressivity is achieved via:
- **OR** → submit two parallel plans (PAIRED PLANS pattern).
- **NOT** → invert the operator (`above` ↔ `below`) where
  applicable; for non-numeric primitives (`bollinger_position
  in_squeeze`), there's no universal NOT.
- Complex boolean → decompose into multiple management/exit
  contingencies, each with its own AND-joined conditions.

This is a deliberate simplicity choice; full boolean
expressivity would require a parser and dramatically expand the
test surface. Current evidence (3 weeks of Floki using the
schema in DRY_RUN) suggests AND + paired plans covers the
patterns Floki actually wants to express.

### 7.8 Restart false-negative window

A bot restart that exceeds 15 minutes invalidates per-condition
state for stateful primitives. Affected conditions cold-start;
the first tick after rehydrate cannot detect a crossing event
that occurred during the outage. For hour-scale conditions
(RSI on H1), this is negligible. For tick-sensitive (M1
crossover) plans, operators should be aware.

### 7.9 No tick-level granularity

Snow operates at 5-second ticks. Sub-tick events (e.g., a flash
spike that bounces within one tick) are not observable. For
slow timeframes (M5 and above), this is below the noise floor.
For scalp-level strategies, a faster cadence would be needed
(not in scope for v1).

---

## 8. Glossary

- **Active plan** — plan whose position is open and being managed.
- **Brain** — data pipeline that computes indicators and structural
  levels each Floki cycle.
- **Contingency** — a named rule with conditions + an action,
  inside the management or exit blocks.
- **DRY_RUN** — config flag; when True, Snow logs `*_would_fire`
  events instead of dispatching to MT5. Default until 2026-04-26.
- **Evaluator** — the function that takes a condition + context
  and returns True/False.
- **Floki** — the LLM-driven decision-maker (currently GPT-5.4).
- **LiveData** — per-tick MT5 data (price, ticks, recently-cached
  indicators).
- **Magic number** — MT5's per-EA position tag; used to filter
  bot-owned positions from manual trades.
- **MFE / MAE** — Maximum Favorable / Adverse Excursion. The
  best / worst the trade has been since open.
- **Pip size** — XAU/USD: 0.1 USD per pip per 1 lot.
- **Plan** — the JSON contract Floki submits to Snow.
- **Plan state** — one of {PENDING, TRIGGERED, ACTIVE, CLOSING,
  CLOSED, EXPIRED, CANCELLED, FAILED}.
- **Primitive** — an atomic condition type (one of 21).
- **Snow** — the deterministic event-driven executor.
- **State cache** — in-memory + on-disk store of per-condition
  state for stateful primitives.
- **Tick** — one Snow loop iteration (5 seconds).

---

**Document version.** 2026-04-26, post-Phase-8b (FLO-359 commits
1–6) + post-FLO-354 + post-FLO-353. Reflects schema_version=2
and prompt v3.7.

**Companion documents.**
- `snow_resumo_para_revisao_externa.md` — Portuguese ~3-page
  executive summary for the CEO.
- `FLO-347_Snow_RFC_v1.md` — original architecture RFC.
- `FLO-356_Snow_Stateful_Primitives_RFC.md` — Phase 8b stateful
  vocabulary RFC.

**Source-code references.** Available on request; this document
is intentionally code-free so the trader can review without
needing repo access.
