# FLO-422 — Breakout Regime Observability

**Status:** Design frozen 2026-05-06. Implementation in progress.
**Owner:** Floki / Snow integration.
**Constraint:** Observability only. No validator gates, no hard rules. Behavioral nudges via prompt only.

---

## 1. Background

7-day plan-outcome review (Apr 30 – May 6, n=119 plans, 31 fired) surfaced
that `breakout_range` was 0/6 fired (-303p) and `structural_bounce` 0/3
(-81p), while `continuation_momentum` and `pullback_trend` were profitable.
Initial hypothesis: kill breakout setup-type entirely. CEO directive
2026-05-06: do not. Investigate whether the failure mode is "late/exhausted
continuation" rather than breakout-type-itself.

Follow-up 14-day breakout study (n=17 fired breakout-shape entries) appeared
to find a strong "compression + momentum building" signal: 5W/0L for plans
where `bb_width_4h_pct ≤ +15%` AND `impulse_count_60m ≥ 3`.

Track 4 backfill (read-only MT5 M5 OHLC pull, 17 trades) **falsified** the
"momentum-building" half of that signal. The snapshot-based impulse-count was
a sampling artifact of irregular brain-analysis ticks and only agreed with
real-candle measurements 41% of the time. Replicated with M5 OHLC, the
metric *reverses direction* for breakouts: more recent large bars = more
likely loser (exhaustion).

The corrected finding: healthy breakouts are preceded by *calm* recent
activity, not by stacking impulses. PLAN-504-007 (+208p) had a 60-min M5
pattern of small same-direction drift; PLAN-506-004 (-88p) had multiple
large-body bars already done before entry.

Critical caveat surfaced by checking PLAN-009 (a pullback_trend winner,
+281p): the same `impulse_total` metric that signals exhaustion for
breakouts can signal trend-health for pullbacks. PLAN-009 had
`impulse_total = 4` (would fail the breakout filter) and won big — the
impulses were trend-direction confirmation, not exhaustion.

**Conclusion:** the metric is meaningful but setup-type-dependent. Hard
universal rules would mis-classify across setup types. The right answer is
observability with context-aware Floki interpretation.

---

## 2. Signal evolution — what we tried, what failed, what we learned

This section is preserved deliberately so future readers (and future
sessions) can see the path and avoid re-running the same false leads.

### 2a. False signal: snapshot-based impulse-count

- **Hypothesis:** consecutive same-direction price changes between brain
  analysis snapshots in the last 60 min = momentum building into breakout.
  Threshold ≥3 = healthy.
- **Apparent result on 17 trades:** 5W/0L when paired with `bb_width_4h_pct ≤ +15%`.
- **Why it was wrong:** brain analyses fire at irregular intervals (cycle
  activity dependent), so "consecutive ticks" depended on sampling density,
  not market behavior. 41% agreement with real M5 candle truth.
- **Lesson:** any metric computed from irregular-interval data must be
  validated against fixed-interval truth before being treated as a signal.

### 2b. Real signal: candle-based impulse_total

- **Definition:** count of M5 bars in the last 60 min where `|body| ≥
  0.5×M5_ATR` AND body direction matches trade direction.
- **Result on 17 breakout trades:** `impulse_total < 2` → 4W/0L (+377p).
  `impulse_total ≥ 2` → 4W/7L (-215p).
- **Direction is REVERSED from the false signal.** Healthy breakout = few
  recent large bars. Exhausted breakout = many recent large bars.
- **Caveat:** sample N=17 still small; treat as directional evidence, not
  proven law.

### 2c. Setup-type dependency surfaced by PLAN-009

- PLAN-009 (pullback_trend BUY, +281p) had `impulse_total = 4` — would
  FAIL the breakout filter — yet was the canonical winner.
- The same metric measures different things in different setup types:
  - **Breakout:** "how many bars of the move have already happened" → high
    = late.
  - **Pullback:** "how strongly is the trend printing in our direction"
    → high = healthy continuation.
- This kills any universal hard threshold and motivates context-aware
  interpretation via Floki, not gating via validator.

### 2d. BB-width-Δ-4h survived but weakened

- Original claim: strongest single discriminator.
- Updated: still meaningful directionally (winners median +2%, losers
  median +29%), but `impulse_total < 2` is comparable in strength on this
  sample. Both worth logging.

---

## 3. Architecture — dual author/trigger snapshots

Floki authors forward-conditional plans. A plan good at author-time can
become bad by trigger-time if the regime drifts. We need both stages.

```
   ┌─────────────────────────────────────────────────────┐
   │  AUTHOR-TIME (in ai_agent.py)                       │
   │  - Floki considering a breakout/pullback plan       │
   │  - Calls get_breakout_regime_metrics tool           │
   │  - Reads numbers, decides whether/how to author     │
   │  - On submit_plan_to_snow, auto-snapshot persists   │
   │    the author-time regime regardless of tool call   │
   └─────────────────┬───────────────────────────────────┘
                     │
                     │ snow_plans.author_regime_snapshot_json
                     │
                     ▼  (plan sits as conditional, may wait minutes-hours)
                     │
   ┌─────────────────┴───────────────────────────────────┐
   │  TRIGGER-TIME (in snow/snow_loop.py)                │
   │  - Entry conditions transition un-met → met         │
   │  - Recompute identical snapshot at firing moment    │
   │  - Compute drift vs author-time                     │
   │  - Persist trigger snapshot + drift                 │
   │  - Fire order normally (no behavioral gating)       │
   └─────────────────────────────────────────────────────┘
                     │
                     │ snow_plans.trigger_regime_snapshot_json
                     │ snow_plans.regime_drift_json (computed delta)
                     │
                     ▼
   ┌─────────────────────────────────────────────────────┐
   │  POST-MORTEM (after trade closes)                   │
   │  - Three-way join: author + trigger + outcome       │
   │  - Distinguish: bad-at-author vs drifted-bad-       │
   │    between-author-and-trigger                       │
   │  - Drift between author and trigger may be the most │
   │    important explanatory variable for losses        │
   │    (CEO directive 2026-05-06)                       │
   └─────────────────────────────────────────────────────┘
```

### Key decisions

- **Same JSON schema** at both stages → direct comparability without
  field-mapping logic.
- **Two columns on `snow_plans`** (`author_regime_snapshot_json`,
  `trigger_regime_snapshot_json`) for easy SQL.
- **No regime_classification label in v1.** Raw numbers only. Setup-type-
  aware interpretation lives in prompt + Floki reasoning, not in a label
  enum that could mis-classify across setup types.
- **Drift JSON computed at trigger time, persisted alongside snapshots**
  so post-mortem queries don't recompute.

---

## 4. JSON schema (both stages, identical fields)

```json
{
  "stage": "author" | "trigger",
  "ts": "2026-05-06T13:11:27Z",            // moment of snapshot, real UTC
  "current_price": 4678.85,
  "direction": "BUY" | "SELL",
  "setup_type": "breakout_range",          // copied from plan for convenience
  "breakout_level": 4636.0,                // null if no fixed entry level
  "breakout_distance_pips": 21.0,          // signed by direction; null if no level
  "breakout_age_bars": 4,                  // M5 bars since first cross of breakout_level; null if no level

  "impulse_total_60m": 4,                  // count of >=0.5xATR same-dir M5 bodies in last 12 bars
  "candle_drift_trailing": 1,              // consecutive trailing same-dir bars (any size)
  "m5_pattern": ".+.--++-+--.",            // 12-char visualization: + . o -
  "m5_atr_pips": 52.9,                     // M5 ATR over prior 14 bars

  "bb_width_4h_pct": -3.2,                 // BB width % change over prior 4h
  "atr_4h_pct": -8.8,                      // H1 ATR % change over prior 4h
  "pre_range_4h_pips": 313,                // (max-min) close over prior 4h, in pips
  "pre_range_24h_pips": 624,
  "range_ratio_4h_24h": 0.50,

  "rsi_now": 64.4,
  "adx_now": 44.1,
  "bb_position_now": 0.34,
  "ema50_distance_atr": 3.02,

  "computation_warnings": []               // e.g. ["insufficient_4h_history"], [] if clean
}
```

### Drift JSON (computed at trigger time only)

```json
{
  "delta_seconds_author_to_trigger": 4382,
  "price_change_pips": +12.4,
  "impulse_total_delta": +2,
  "bb_width_4h_pct_delta": +18.5,
  "atr_4h_pct_delta": +4.1,
  "breakout_age_bars_at_trigger": 7,
  "drift_assessment": "regime_expanded"    // qualitative tag from delta thresholds
}
```

### `computation_warnings`

Returned non-empty when inputs are incomplete:
- `insufficient_4h_history`: fewer than ~6 analyses in the prior 4h window.
- `insufficient_m5_history`: fewer than 26 M5 bars (cannot compute ATR baseline + 12-bar window).
- `mt5_unavailable`: candle pull failed; M5 metrics will be null.
- `weekend_gap`: gap of >2h in the analyses series, suggesting weekend or downtime.

Snapshot is always returned; null fields are explicit. Never blocks plan
submission.

---

## 5. Component breakdown

### 5.1 `breakout_regime.py` — helper module (pure compute)

- No MT5 calls, no DB reads, no logging. Pure function.
- Caller fetches `analyses` rows and M5 candles, passes them in.
- Returns the JSON dict above.
- Trivially unit-testable.

### 5.2 DB schema

```sql
ALTER TABLE snow_plans ADD COLUMN author_regime_snapshot_json TEXT;
ALTER TABLE snow_plans ADD COLUMN trigger_regime_snapshot_json TEXT;
ALTER TABLE snow_plans ADD COLUMN regime_drift_json TEXT;
```

Forward-only migration. Pre-existing rows have NULL — no rewrite needed.

### 5.3 Author-time auto-snapshot (in `ai_agent.py`)

- Hooks after `submit_plan_to_snow` succeeds.
- Filters to setup_type ∈ {`breakout_range`, `continuation_momentum`,
  `pullback_trend`, `structural_bounce`} (the lifecycle-sensitive types).
- Pulls inputs (analyses for last 4h, MT5 M5 for last ~30 bars), calls
  helper, persists to `author_regime_snapshot_json`.
- Floki may not have called the tool — this captures author-time state
  regardless.

### 5.4 `get_breakout_regime_metrics` tool (in `agent_tools.py`)

- Tool surfaced to Floki for active reasoning.
- Same compute as auto-snapshot. Floki can call when authoring.
- Returns the JSON dict + a one-line natural-language summary for prompt
  efficiency: e.g. `"author: bb_width_4h=-3% (compressed), impulse_total=4
  in trade direction (active uptrend), 313p range over 4h."`
- Does NOT return a regime_classification label — leaves interpretation to
  Floki.

### 5.5 Trigger-time snapshot (in `snow/snow_loop.py`)

- When entry conditions transition un-met → met (just before order fire).
- Identical compute to author-time snapshot.
- Persisted to `trigger_regime_snapshot_json`.
- Drift computed vs `author_regime_snapshot_json` (joins on plan_id),
  persisted to `regime_drift_json`.
- Emits log line: `BREAKOUT_REGIME_DRIFT | plan_id=... | drift_assessment=...`
- Does NOT block order fire.

### 5.6 Soft prompt guidance (in `agent_prompts.py`)

Single insertion (~150 words) describing volatility lifecycle as
context-dependent evidence Floki should weigh, with explicit per-setup-type
framing. No threshold rules. References the tool. See section 7 for draft.

---

## 6. Drift tracking — explicit instrumentation

CEO directive 2026-05-06: drift between author and trigger may be the most
important explanatory variable in future post-mortems. Treat as
first-class observability.

`drift_assessment` qualitative tag (computed from numeric deltas):

- `regime_stable`: both snapshots in similar lifecycle state (e.g., both
  show `bb_width_4h_pct ≤ +15%` and `impulse_total ≤ 2`).
- `regime_expanded`: trigger shows materially more expansion than author
  (e.g., `bb_width_4h_pct_delta > +20pp` or `impulse_total_delta > +2`).
  This is the "bad drift" — author thought it was fresh, trigger fires
  into late-stage.
- `regime_compressed`: rare reverse case — trigger shows less expansion
  than author. Sometimes happens when author was late in a move that
  subsequently retraced before triggering.
- `setup_invalidated`: e.g., breakout level no longer in front of price,
  or ATR collapsed (volatility-event passed).

Thresholds for each tag are tentative. Log raw numerics so we can refine
the categorical mapping after 30 days of data without re-running anything.

---

## 7. Soft prompt guidance — proposed wording

Single insertion in `agent_prompts.py` near line 320, after the eight
worked examples:

```
BREAKOUT/PULLBACK LIFECYCLE — TRAJECTORY OVER SNAPSHOT. For setup_types
breakout_range / continuation_momentum / pullback_trend / structural_bounce,
call `get_breakout_regime_metrics(direction, breakout_level)` before authoring.
The tool returns numbers describing volatility lifecycle — context-dependent,
not threshold-rules:

For BREAKOUT entries (price_above/below + momentum confirmation, no latch):
  - Healthy: small `impulse_total_60m` (1-2 large bars), modest BB width
    expansion (`bb_width_4h_pct ≤ +15%`), recent activity has been compression.
    The breakout is fresh — entering near the start of a new expansion regime.
  - Late/exhausted: high `impulse_total_60m` (3+ large bars already done),
    `bb_width_4h_pct ≥ +25%`, `atr_4h_pct ≥ +10%`. Expansion is several bars
    in — chasing.

For PULLBACK entries (latch + reclaim + bounce-direction confirmation):
  - Healthy: trend-direction `impulse_total_60m` ≥ 2 — strong trend printing
    impulses confirms continuation thesis. Recent retracement to the level
    is the entry trigger.
  - Unhealthy: trend has stalled (no impulses in trend direction for several
    bars), or the pullback is into an HTF that's already exhausted.

The numbers are evidence to weigh, not gates. Cite them in `confidence_reason`
when they support or contradict your thesis. Empirical (May 2026):
PLAN-20260506-007 (-70p) and PLAN-20260506-010 (-58p) had high
`impulse_total_60m` for BUY breakouts at exhausted levels; PLAN-20260506-009
(+281p) had a more compressed pre-state into a clean reclaim. Same zone,
different trajectory.
```

This is shorter than prior drafts, avoids prescriptive thresholds, and
respects the setup-type dependency.

---

## 8. Implementation order (CEO-approved 2026-05-06)

1. `breakout_regime.py` helper module — pure compute, unit-testable.
2. DB schema migration — three new columns on `snow_plans`.
3. Author-time auto-snapshot in `ai_agent.py` — passive, no Floki interaction.
4. `get_breakout_regime_metrics` tool exposure to Floki.
5. Trigger-time snapshot + drift tracking in `snow/snow_loop.py`.
6. Soft prompt guidance — single insertion in `agent_prompts.py`.
7. Bot restart + observation window. No validator rules. ~30 in-scope
   plans needed before considering any hard behavioral constraints.

Each step gets a separate review-and-approve gate before implementation
moves to the next. No bundling.

---

## 9. Anti-anti-breakout commitment

CEO directive 2026-05-06: the goal is distinguishing fresh expansion from
exhausted continuation, not suppressing breakout setups.

Concretely this means:
- No hard validator gate on `impulse_total`, `bb_width_4h_pct`, or any
  related metric in v1.
- Soft prompt guidance frames lifecycle as evidence to weigh, with
  explicit "numbers are evidence to weigh, not gates" language.
- Floki retains agency to author against the lifecycle signal if his
  read of the chart says otherwise — required to cite the override in
  `confidence_reason` per existing rules.
- Validation gate before any future hard rule: 30 in-scope plans
  through the observability pipe, both stages snapshotted, drift
  computed for each fired trade. Then revisit.

---

## 10. Open risks

- **N=17 sample is small.** The 4-for-0 result on `impulse_total < 2` for
  breakouts is encouraging but not statistically robust. Need 30+ for
  confidence at p<0.05.
- **Setup-type-aware interpretation depends on Floki understanding the
  prompt.** If Floki applies breakout-mode reasoning to a pullback (or
  vice versa), the metric meaning is inverted. Mitigation: explicit
  per-setup-type framing in the prompt, plus tool docstring.
- **Drift detection thresholds are tentative.** May need to refine
  `regime_expanded` cutoffs after seeing real drift cases.
- **Compute cost.** Author-time tool call adds ~150-200ms per breakout
  authoring cycle. Trigger-time snapshot adds ~100ms in a hot loop. If
  profiling shows budget overrun, in-memory cache analyses for 30s.

---

## 11. Frozen for reference — change log

- **2026-05-06**: Initial design (sections 1-10). Replaces prior
  "compression + momentum building" framing falsified in Track 4 backfill.
  Setup-type-aware interpretation. No hard rules. Dual snapshot
  architecture with drift tracking.
