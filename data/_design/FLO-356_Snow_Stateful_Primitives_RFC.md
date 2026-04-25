# FLO-356 — Snow Stateful Primitives RFC (Phase 8a)

**Status:** DRAFT — awaiting CEO + CTO peer review.
**Author:** generated 2026-04-25 in response to FLO-356 (Urgent priority).
**Implementation ticket:** FLO-XXX (Phase 8b, after this RFC is approved).

---

## 0. Why this RFC exists

Phase 7.3 (FLO-355) closed the easy gaps in Snow's primitive
vocabulary — Bollinger, Stochastic, Pivot proximity, MACD divergence
— all by wiring Brain-already-computed values into the schema.
What it could NOT close: the stateful primitive class explicitly
deferred by RFC v1 §6.6 to v2 schema. After Phase 7.3, the gap
analysis ranks the missing pieces as:

| Setup type | Today | Blocker |
|---|---|---|
| Mean-reversion BB+RSI+MACD CROSSOVER | PARTIAL | crossover semantics (state) |
| Fib pullback + RSI RECOVERING from oversold | PARTIAL | "X was below Y at some recent point" |
| Liquidity sweep + reversal | NO | session H/L (separate) + sweep latch (state) |

Three of the five most-cited professional setup shapes hinge on the
same architectural element: **per-condition memory across ticks**.
RFC v1 punted this as v2 work. FLO-356 designs it.

CEO strategic frame (verbatim, 2026-04-25):

> "É importante irmos o mais rápido possível para Phase 8. Se não
>  avançamos nunca saberemos que o Snow é bom mesmo."

Stateful primitives unlock the empirical validation question: can
Snow encode a professional trader's mental model well enough that
the plans Floki submits actually fire on the patterns he describes?
Without crossover / recent-history primitives, the answer is "only
crudely." Phase 8 finds out.

---

## 1. Goals + non-goals

### Goals

- **Crossover primitives** — `indicator_crossover` for RSI / MACD-line
  vs signal / price vs EMA. The single most-asked-for shape.
- **Recent-history primitives** — `indicator_was` (was X above/below
  threshold within last N bars) for "RSI recovering from oversold"
  type setups.
- **One stateful infrastructure that supports both** — single
  `state_cache_json` column, single in-memory state cache, single
  recovery model. Not two separate mechanisms.
- **Restart safety with documented false-negative window.** Not
  perfect persistence — restart-tolerant degradation per RFC v1
  §6.6 option (iii).
- **Backward-compatible schema migration.** Existing 18 primitives,
  existing plans, existing tests all keep passing. New primitives
  opt in.

### Non-goals

- Liquidity sweep semantics (Cat D — distinct future work).
- VWAP / OBV / Ichimoku (Cat B — separate computation pipeline).
- Price-tick-level crossover (only bar-level supported in v1; tick
  noise would dominate).
- Cross-plan state sharing (each plan's state is private; no shared
  "regime crossed bullish" global flags).

---

## 2. State model

### 2.1 Where state lives

Three layers, each with a clear contract:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Per-tick state (in-memory only)                                    │
│  ────────────────────────────────                                   │
│  snow.state.PerConditionStateCache                                  │
│    keyed by (plan_id, contingency_name, condition_index)            │
│    holds {prev_value, prev_bar_window_id, last_seen_at}             │
└─────────────────────────────────────────────────────────────────────┘
                            │ flush every N ticks
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Per-plan persistence (SQLite snow_plans column)                    │
│  ───────────────────────────────────────────────                    │
│  snow_plans.state_cache_json   TEXT NULL                            │
│    JSON-encoded snapshot of all condition states for that plan      │
└─────────────────────────────────────────────────────────────────────┘
                            │ rehydrated at process start
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Restart recovery                                                   │
│  ────────────────                                                   │
│  snow.recovery.rehydrate_state_cache()                              │
│    on process start, loads state_cache_json for every active plan,  │
│    populates PerConditionStateCache                                 │
│  Stale-state guard: if last_seen_at > N minutes old, drop the       │
│    state (treat as cold start; first eval re-seeds from current).   │
└─────────────────────────────────────────────────────────────────────┘
```

**Key invariant:** evaluators NEVER directly write to SQLite. They
read+update the in-memory cache. A flush job (every 60 ticks per
RFC v1 ORPHAN_SWEEP_INTERVAL_TICKS pattern, or on plan-state
transitions) serializes the cache to `state_cache_json`.

### 2.2 What each state row contains

```python
# snow/state.py (new module)

@dataclass
class ConditionStateRow:
    """In-memory per-condition state. Serialised to JSON for persistence."""
    plan_id: str
    contingency_name: str  # "_entry" for entry conditions
    condition_index: int   # 0-based position in the conditions list
    cond_type: str         # discriminator string for staleness check
    prev_value: Optional[float] = None   # last observed indicator value
    prev_above_threshold: Optional[bool] = None  # for crossover detection
    bar_history: list[float] = field(default_factory=list)  # last N bar values
    bar_history_max_n: int = 0       # capacity (set per primitive)
    last_seen_at: str = ""           # utc_iso of last update
```

**Storage shape on disk:**

```json
// snow_plans.state_cache_json
[
  {
    "contingency_name": "_entry",
    "condition_index": 1,
    "cond_type": "indicator_crossover",
    "prev_value": 49.6,
    "prev_above_threshold": false,
    "bar_history": [],
    "last_seen_at": "2026-04-25T14:32:11Z"
  },
  {
    "contingency_name": "exit_rsi_recovery",
    "condition_index": 0,
    "cond_type": "indicator_was",
    "prev_value": null,
    "bar_history": [22.0, 19.5, 26.1, 31.4],
    "bar_history_max_n": 6,
    "last_seen_at": "2026-04-25T14:32:11Z"
  }
]
```

`bar_history` is a sliding window. Capped at `bar_history_max_n`
(per-primitive — 4 for "within last 4 candles", etc). Updated on each
NEW BAR closing on the relevant TF, NOT every tick (prevents noise
saturation).

### 2.3 New SQLite migration

Migration file: `snow/migrations/002_state_cache_json.sql`.

```sql
-- Up migration
ALTER TABLE snow_plans ADD COLUMN state_cache_json TEXT;

-- No down migration; state_cache_json is additive and NULL-safe
-- for plans created pre-v2 schema (their conditions are all
-- stateless and never touch the column).
```

Application logic:
- `snow.db.init_snow_tables()` runs the migration idempotently
  (`ALTER TABLE` wrapped in `try / except OperationalError(duplicate
  column)`).
- Existing plans get `NULL`; rehydration treats NULL as "no state",
  cold-starts on first eval.
- `schema_version` on snow_plans bumps from 1 → 2. Old plans (v1) are
  read with `schema_version=1` and never reach a stateful primitive
  (validator rejects stateful primitives in v1 plans).

---

## 3. Primitive specifications

### 3.1 indicator_crossover

```python
class IndicatorCrossover(_Cond):
    """Fires the FIRST tick after the indicator crosses the threshold
    in `direction`. State: prev_above_threshold (bool).

    Detection rule:
      prev_above = (prev_value > threshold)
      curr_above = (curr_value > threshold)
      direction == "above": fires iff (not prev_above) AND curr_above
      direction == "below": fires iff prev_above AND (not curr_above)

    Edge case — equality:
      prev_value == threshold treated as NEITHER above nor below
      (i.e. ambiguous; use last definite state). Rare in practice
      with floats; documented to remove footguns.

    Cold-start (no prev_value):
      RFC v1 §6.6 option (iii): seed prev = current; first tick
      evaluates as 'no crossing'. False-negative on the cold-start
      tick only.
    """
    type: Literal["indicator_crossover"] = "indicator_crossover"
    indicator: Literal["rsi", "macd_histogram", "stochastic"]
    tf: Timeframe
    direction: Literal["above", "below"]
    threshold: float
```

State carried: `prev_above_threshold: bool`. One slot per condition.
~16 bytes serialised. Per-tick update O(1).

### 3.2 indicator_was

```python
class IndicatorWas(_Cond):
    """Was the indicator value `op` `threshold` within the last
    `bars` bars on `tf`? Updated on bar-close, not every tick.

    Use case: 'RSI was below 30 within last 4 H1 bars' → true even
    if RSI is now back above 40. Combined via AND with other
    primitives, expresses 'recovering from oversold'.

    Memory: bar_history list, capped at `bars`. Bar value captured
    once per bar close on `tf`. last-bar-id tracked to avoid
    appending the same bar twice.
    """
    type: Literal["indicator_was"] = "indicator_was"
    indicator: Literal["rsi", "macd_histogram", "stochastic"]
    tf: Timeframe
    op: ComparisonOp
    threshold: float
    bars: int = Field(ge=1, le=20)  # capped at 20 to bound memory
```

State carried: `bar_history: list[float]` capped at `bars`,
`prev_bar_id: int` (or timestamp) to dedupe same-bar ticks.
~`bars * 8` bytes plus overhead. Per-tick update O(1) (only
appends on bar close).

### 3.3 price_crossed_level

```python
class PriceCrossedLevel(_Cond):
    """Has price crossed `level` in `direction` since plan-active?
    One-shot latch — once True, stays True until plan is reseeded.

    Use case: liquidity-sweep building block. Combine with
    `price_above` or `price_below` for "price tagged 4720 then
    bounced above": price_crossed_level(level=4720, direction='below')
    AND price_above(level=4725).
    """
    type: Literal["price_crossed_level"] = "price_crossed_level"
    level: float
    direction: Literal["above", "below"]
```

State: `latched: bool`. ~16 bytes. Per-tick update O(1). Reset on
plan re-seed (entry fire); preserved across restart while plan is
active.

### 3.4 What's NOT in scope

- **price_crosses_above / price_crosses_below** as INSTANT crossover
  primitives (the v1 RFC §6.6 names) — these would behave
  identically to `indicator_crossover` but on price; merge into
  `price_crossed_level` with a "fires once" semantic vs "instant
  fire" semantic. **Decision:** ship `price_crossed_level` as a
  latch (one-shot True), NOT an instant fire. Floki gets crossover-
  AND-current-state in two ANDed primitives instead.
- **divergence detection by Snow** — Brain detects, Snow reads.
  Already covered in Phase 7.3.

---

## 4. Architecture flow per tick

```
┌─────────────────────────────────────────────────────────────────┐
│ SnowLoop._tick()                                                │
│   for each active plan:                                         │
│     for each condition in plan.entry/management/exit:           │
│       state = state_cache.get_or_create(plan_id, c_name, idx)   │
│       result = evaluate_condition(cond, ctx, state)             │
│       state.last_seen_at = utc_iso()                            │
│       state.update_from_eval(cond, ctx, result)                 │
│       (state in-memory ONLY at this point)                      │
│   if tick_count % 60 == 0:                                      │
│     state_cache.flush_to_db()  # bulk UPSERT into state_cache_json│
└─────────────────────────────────────────────────────────────────┘
```

Stateless primitives (the existing 18) ignore the `state` argument
— they're shape-compatible with the new evaluator signature
`(cond, ctx, state)` but don't mutate `state`. Adapter pattern: a
thin shim wraps the existing 2-arg evaluators so dispatch can call
all evaluators uniformly.

Hot path cost: state cache lookup is dict-O(1); update is O(1) for
crossover/latch, O(1) for `indicator_was` (deque append + pop). Per-
tick budget per condition: <100 µs even worst case. Phase 4
TIMING_WARN_THRESHOLD_MS=200 still covers ~2000 stateful conditions
per tick — far above realistic plan counts.

---

## 5. Restart behaviour

### 5.1 Recovery sequence

On `XAUUSDBot.start()` after `init_snow_tables()`:

```python
snow.recovery.rehydrate_state_cache()
# 1. SELECT state_cache_json FROM snow_plans WHERE status IN active_statuses
# 2. for each row: parse JSON, rebuild ConditionStateRow objects
# 3. drop rows where (now - last_seen_at) > STALE_STATE_THRESHOLD_MINUTES
#    (default 15 min — covers a typical restart window)
# 4. populate snow.state.PerConditionStateCache
```

Stale rows are NOT errors — they just trigger cold-start on first
eval. The `STALE_STATE_THRESHOLD_MINUTES` constant gates how long a
restart can take before state is considered untrustworthy.

### 5.2 False-negative window

Per RFC v1 §6.6 option (iii):

| State at restart | First-tick behaviour |
|---|---|
| `state_cache_json` valid, last_seen_at recent | Restored seamlessly; crossover detection works first tick |
| `state_cache_json` stale (>15 min) | Treated as cold start. `prev` seeded to current. First tick reports NO crossing even if a real crossing happened during outage. |
| `state_cache_json` missing / null / corrupt | Cold start. Same false-negative semantics. |

Documented user-facing impact: **a restart during an active plan
can lose at most one tick of crossover detection per condition**.
For most plans (5-second cadence, hour-scale conditions) this is
imperceptible. For plans relying on a single bar's crossover (rare),
operators should know.

### 5.3 Atomicity

State persistence is best-effort, not transactional with executor
calls:
- Crossover fires → `record_trigger_and_transition` runs in its
  own SQLite transaction (existing path)
- State cache flush is ASYNC w.r.t. fire, runs every 60 ticks
- Net effect: a fire that happens in the 1-tick window between flush
  and crash is replayable from MT5 state; the corresponding state
  update is lost (next restart cold-starts)

This is the same trade-off Phase 5b accepted for executor calls vs
DB writes (RFC v1 §7.6 acknowledged "no distributed transaction;
recovery.reconcile() catches divergence").

---

## 6. Validator changes

```python
# snow/validator.py — new check

_STATEFUL_PRIMITIVES = {
    "indicator_crossover",
    "indicator_was",
    "price_crossed_level",
}

def _check_stateful_in_v1(plan: Plan) -> list[str]:
    """schema_version=1 plans must NOT contain stateful primitives.
    Stateful was introduced in schema_version=2; v1 plans bypass the
    state_cache_json column entirely."""
    if plan.schema_version >= 2:
        return []
    errors = []
    for block_name, conditions in (
        ("entry", plan.entry.conditions),
        *((f"management[{i}]", c.conditions) for i, c in enumerate(plan.management)),
        *((f"exit[{i}]", c.conditions) for i, c in enumerate(plan.exit)),
    ):
        for ci, c in enumerate(conditions):
            if c.type in _STATEFUL_PRIMITIVES:
                errors.append(
                    f"{block_name} conditions[{ci}]: {c.type!r} requires "
                    f"schema_version >= 2; got {plan.schema_version}"
                )
    return errors
```

**Migration path for Floki:** the SnowLoop's plan submission tool
auto-stamps `schema_version=2` (the new default). Plans created
post-FLO-356 deploy use stateful primitives freely. Plans frozen
in `plan_json` at v1 keep working — they have no stateful
primitives by validator construction.

---

## 7. Test strategy

Five test classes for the new module surface:

### 7.1 TestStateCache (~12 tests)

In-memory cache lifecycle:
- get_or_create allocates fresh
- subsequent get returns same object
- flush_to_db serialises every dirty row, clears dirty flag
- rehydrate from JSON repopulates cache exactly
- corrupt JSON in DB → row dropped + WARN logged, no crash
- stale rows (>STALE_STATE_THRESHOLD_MINUTES) dropped on rehydrate
- last_seen_at stamped on every update
- forget_plan clears all rows for that plan
- thread-safety: concurrent get_or_create yields one shared row

### 7.2 TestIndicatorCrossover (~10 tests)

- "above" direction: prev=49 curr=51 fires; prev=51 curr=49 doesn't
- "below" direction: mirror
- two consecutive ticks above threshold: fires once (first tick),
  not the second
- equality at threshold: documented behaviour
- cold start (no prev): no crossing detected first tick
- post-restart with valid state: detection works first tick
- post-restart with stale state: cold start; first tick no fire
- non-numeric current value: returns False, doesn't update state
- threshold inversion: `direction="above", threshold=70` fires the
  tick RSI moves from 69.x → 70.x

### 7.3 TestIndicatorWas (~8 tests)

- bars=4 window: condition true after value goes below 30 once
  in last 4 closes
- window slides: 5th bar pushes oldest out
- multiple thresholds in same plan: independent histories
- bar-id dedupe: 5 ticks within one M1 bar append once
- cold start with no history: condition False (no satisfaction yet)
- post-restart history restored: condition fires immediately if
  satisfied within recent bars
- bars=1 corner case: equivalent to current-tick check
- bars=20 (max): no overflow

### 7.4 TestPriceCrossedLevel (~6 tests)

- price moves through level "above" direction: latched True
- subsequent ticks below level: still True (latched)
- never crossed: False
- post-restart with latch True: stays True
- new plan instance: latch starts False
- combination: AND with `price_above` to express "tagged-then-bounced"

### 7.5 TestRestartSimulation (~10 tests)

End-to-end resilience:
- write state, "kill" cache (clear in-memory), reload from DB,
  state preserved
- concurrent fire + flush: serialisation correctness
- corrupt JSON in one row doesn't break sibling rows
- migration: existing v1 DB upgrades cleanly to v2
- v1 plans persist post-migration: read fine, evaluate fine
- v2 plan with stateful primitive submitted to v1 schema: validator
  rejects with clear error
- mixing stateful + stateless conditions in one contingency: dispatch
  routes correctly, state only allocated for stateful
- 100-plan stress: 100 ticks, 5 stateful conditions each, no leaks
  (memory growth bounded by capacity)
- false-negative window measured: cold-start tick reports None
- 15-minute stale threshold respected: state at exactly 14 min
  retained; at 16 min dropped

### 7.6 TestValidatorStatefulGate (~5 tests)

- v1 plan with stateful primitive: rejected with clear error path
- v2 plan with stateful primitive: accepted
- v2 plan with only stateless primitives: accepted (backward compat)
- error messages name the field path

**Total: ~51 new tests.** Plus regression: existing 534 tests must
all keep passing.

---

## 8. Risk analysis

### 8.1 Corruption scenarios

| Scenario | Detection | Recovery |
|---|---|---|
| state_cache_json invalid JSON | `json.loads` exception caught; row dropped; WARN logged | Cold start that condition |
| state_cache_json schema mismatch (e.g. v3 fields in v2 reader) | Pydantic deserialise raises; caught; row dropped | Cold start |
| In-memory cache desync from DB after partial flush | Detected on next flush by comparing `last_seen_at` | Re-flush all dirty rows; old DB row overwritten |
| `prev_value=None` after evaluator bug | Crossover evaluates as cold start (no prev) | First tick is no-fire; subsequent ticks fine |
| DB row exists but in-memory cache lost (e.g. process restart with no rehydrate) | Detected on first eval (cache miss → rehydrate) | Lazy-load that plan's row from DB |

### 8.2 Performance scenarios

Worst case: 100 active plans × 8 conditions/plan × 5 stateful = 4000
state rows. Per-tick:
- 4000 dict lookups: ~0.4 ms
- 4000 evaluator calls (mix stateful + stateless): ~10 ms
- 4000 last_seen_at stamps: ~0.4 ms
Per 60-tick flush:
- 4000 JSON serialisations: ~5 ms
- ~4000-row UPSERT batch: ~50 ms (single SQL, bulk via executemany)

Total per-tick load <15 ms; per-flush load ~55 ms. Phase 4 timing
WARN threshold is 200 ms. Comfortably under.

**Memory:** 4000 rows × ~100 bytes = ~400 KB. Negligible.

### 8.3 Specific failure modes

- **Plan deletion mid-tick** — flush sees orphan rows; dropped on
  flush via FK behaviour (state_cache_json is a column on snow_plans,
  so it's already gone with the plan).
- **Two threads racing on same condition state** — Snow tick is
  single-threaded per loop instance; not a real concern. The
  PerConditionStateCache uses a single RLock for defence in depth.
- **Clock skew** — `last_seen_at` uses `tz_utils.utc_iso()`; same
  source as the rest of Snow; no skew between writers.

---

## 9. Implementation estimate

| Item | LoC | Effort |
|---|---|---|
| `snow/state.py` (PerConditionStateCache + ConditionStateRow) | ~250 | 0.5 day |
| `snow/migrations/002_state_cache_json.sql` + `init_snow_tables` upgrade | ~30 | 0.25 day |
| `snow.recovery.rehydrate_state_cache` | ~120 | 0.5 day |
| 3 new condition classes in `snow/schema.py` | ~70 | 0.25 day |
| 3 new evaluators (crossover / was / crossed_level) | ~180 | 0.5 day |
| Dispatch wiring (extend evaluator signature to optional state) | ~50 | 0.25 day |
| `snow.snow_loop._tick` integration (state cache lookup + flush) | ~60 | 0.5 day |
| Validator stateful-in-v1 rejection | ~40 | 0.25 day |
| Tests (~51 new) | ~600 | 1.5 days |
| Prompt v3.6 update (new primitives + scope-limiter to mandatory workflow) | ~+200 tokens | 0.25 day |

**Total: ~1,400 LoC + ~600 LoC tests. Estimate 4-5 working days for
implementation + 1 day for prompt + observation gate. Realistic
ship window: end of next week.**

---

## 10. Backward compatibility checklist

- [x] Existing 18 primitives unchanged
- [x] Existing 534 tests still pass (would be checked at impl time)
- [x] v1 plans (schema_version=1) keep working — validator gates
      stateful-in-v1 rejection
- [x] v2 plans (schema_version=2) unlock stateful primitives
- [x] `state_cache_json` column NULL-safe for v1 plans
- [x] Phase 6 tools (`submit_plan_to_snow`, etc.) auto-stamp v2 on
      plans submitted post-deploy — no caller change needed
- [x] Floki prompt: v3.6 adds stateful primitive descriptions,
      keeps v3.5 list intact
- [x] Reverting v3.6 prompt → v3.5 prompt is a single-commit revert
      and stateful primitives just go unused

---

## 11. Open questions for CEO + CTO review

1. **STALE_STATE_THRESHOLD_MINUTES default = 15.** Bot restart
   typically takes <2 min; 15 min covers planned maintenance + crash
   restart. Tighter (5 min) is safer but creates more cold-starts
   on incidental delays. Looser (60 min) is risky on long outages.
   Defaulting 15.
2. **Bar-history capacity (`bars` field on `indicator_was`).** Cap
   at 20 to bound memory. Higher values (50+) requested by traders
   sometimes; can be relaxed if N=20 proves limiting.
3. **`price_crossed_level` reset semantics.** Latch resets when:
   plan goes ACTIVE (entry fires)? plan transitions to terminal? On
   demand via a `reset_after_fire: bool` field? Proposing: latched
   for plan lifetime; no reset within a single plan's PENDING period.
   Operator can express "fire each cross" via two paired plans.
4. **State migration on schema bump.** v2 → v3 future schema
   versions: same pattern (additive columns, validator gating).
   Worth explicit doc.
5. **Test budget.** ~51 new tests on top of 534 existing brings the
   total to ~585. Suite runtime currently 40s; adding state +
   restart simulation tests likely pushes to 50-60s. Acceptable.
6. **Prompt tightening discipline for v3.6.** Per Phase 7.x policy:
   limit prompt overhead, scope new mandates to mandatory-workflow
   only, regression-test the new section. Same forcing functions.

---

## 12. Phase 8b ship plan (after this RFC is approved)

Sequence:

1. **Branch:** `feat/flo356-stateful-primitives`
2. **Migration ships first** (additive column, idempotent). Single
   commit. Verifiable on a tmp DB clone.
3. **State module + tests.** ~12 tests for cache lifecycle alone.
4. **Three new primitives + evaluators + tests.** Crossover first
   (highest value), `indicator_was` second, `price_crossed_level`
   third. Each its own commit.
5. **Validator gating** (stateful-in-v1 rejection). One commit.
6. **Loop integration** + restart-simulation tests. One commit.
7. **Prompt v3.6** + regression tests. One commit.
8. **Final regression sweep.** All commits squashed into a feature
   branch; merge to main when CEO + CTO sign off.

Each commit independently revertable. Schema migration is the only
non-reversible step; it's NULL-safe so reverting downgrades to v1
plan submissions but keeps the column.

---

## 13. Success criteria for Phase 8b

After ship:
- 585+/585+ snow suite green
- Existing 18 primitives still work; existing plans still validate
- Restart simulation test passes (state preserved across simulated
  process restart)
- Floki submits at least one plan using `indicator_crossover` within
  72h of v3.6 deploy (evidence-window observation)
- Setup B (mean-reversion BB+RSI+MACD crossover) in the gap-analysis
  walkthrough reaches YES, not PARTIAL

After 7-day window:
- Crossover false-negative rate (cold-start ticks / total ticks):
  measurable, target <0.1%
- State cache memory growth bounded (no leak)
- No state-cache corruption WARN logs

If observation reveals issues: state-cache code is isolated, can be
disabled with one feature flag while the schema column stays
backward-compatible.

---

## 14. Out-of-scope follow-ups

- **Liquidity sweep semantics** — needs swing-high/low identification
  + sweep latch + reversal detection. Cat D, distinct RFC.
- **Cross-plan state sharing** — "regime is bullish" shared signal
  across all plans. Not a Phase 8 goal; deferred to a hypothetical
  Phase 10 (multi-plan coordination).
- **VWAP / OBV / Ichimoku** — Cat B, Brain pipeline work; lower
  utility-per-cost per CEO decision.
- **Schema v3** — when stateful primitives mature, future expansions
  (e.g. cross-indicator divergence detection in Snow itself, multi-
  bar pattern matchers) would bump schema_version again.

---

## End of RFC — awaiting CEO + CTO review.

**Recommended review focus areas (in priority order):**

1. State-cache restart semantics (§5) — false-negative window
   acceptable?
2. STALE_STATE_THRESHOLD_MINUTES default (§11.1) — 15 min OK?
3. `price_crossed_level` latch semantics (§11.3) — single-plan
   lifetime acceptable?
4. Implementation estimate (§9) — 4-5 days realistic?
5. Anything missing from the risk analysis (§8)?
