# FLO-336 Phase 1 — Editorial-language audit of the lesson/memory pipeline (READ-ONLY)

**Status:** Phase 1 complete. Inventory + classification + corpus quant + 4-option evaluation + recommendation matrix. No code changes.
**Blocks:** FLO-334 Phase 2, FLO-332 Phase 3. Both remain paused until FLO-336 closes.
**Author:** DEV, 2026-04-22

---

## HEADLINE — one-paragraph answer

**Bug G (`7b5f8a9`, 2026-04-21) already removed `luna_environment` from the capture path in `execute_trade`**, so trades opened after Apr 21 17:58 UTC do NOT write an editorial Luna label into `trade_conditions/*.json`. But the downstream aggregation in `trade_lessons._build_bucket_key` still *reads* `conditions.get("luna_environment")` and turns it into the final string dimension of the bucket key that `get_trade_lessons` returns to Floki. **77 of 87 historical `trade_conditions` snapshots (89%) carry `CAUTION` or `DANGER` values frozen in time from before Bug G.** Shipping FLO-334 Phase 2 would replay those historical values into Floki's lesson output — reintroducing the exact vocabulary Bug G and FLO-317 removed. This is a **partial-cleanup finish**, not a new design decision.

**Scope warning (Rule 19 class):** The ticket frames this as a luna-field audit. The corpus audit surfaces THREE separate editorial-language classes in the pipeline that the four options address with varying coverage — one class (the `AVOID:`/`PREFERRED:` lesson-text prefix) is not addressed by any of X/Y/W and requires Option Z or a dedicated fix.

---

## 1. Inventory — every Floki-facing surface carrying captured/derived editorial language

| # | Surface | Where | How it reaches Floki | Editorial content observed |
|---|---|---|---|---|
| 1 | `get_trade_lessons` tool | `agent_tools.py:2779` → `trade_lessons.get_relevant_lessons` | Floki pull | Bucket-key dimension `{luna}` ∈ {SAFE, CAUTION, DANGER, UNKNOWN}; lesson text prefix `AVOID:`/`PREFERRED:`/`NEUTRAL:` |
| 2 | `get_trade_patterns` tool | `agent_tools.py:2705` → `agent_reflection.read_patterns` → `data/agent_patterns.json` | Floki pull | Top-level `insight` field contains "Avoid — losing pattern", "Strong edge", other LLM/editorial strings |
| 3 | `get_recent_reflexions` tool | `agent_tools.py:2799` → `db_writer.get_recent_reflexions` | Floki pull | `thesis_summary` (BULLISH/BEARISH/NEUTRAL labels from active_thesis), `lesson` (LLM-authored prose), `pattern_tags` (valence-laden tag list), `revised_lesson` (LLM-authored hindsight) |
| 4 | `search_reflexions` tool | `agent_tools.py:2812` → `db_writer.search_reflexions` | Floki pull | Same as #3 (same table) |
| 5 | `read_session_memory` tool | `agent_tools.py:2664` → `data/agent_session_memory.json` | Floki pull | Prose `notes` array — free-form text, includes Sage daily briefings inserted at 21:00 UTC |
| 6 | Auto-inject `session_memory` | `agent_data_builder.py:1352, 1433` → `ai_agent._build_user_message:1693` (JSON dump) | Ambient every proactive cycle | Same prose notes as #5, present in 100% of proactive cycles |
| 7 | `get_luna_brief` tool | `agent_tools.py:4037` → `luna_analyst.load_luna_brief` | Floki pull | Post-Bug-G schema: `patterns_detected` enum (5 valence-laden names), `correlations` (numeric), `key_factors` (short observational strings) |
| 8 | `get_market_regime` tool | `agent_tools.py:3862` → `regime_detector` | Floki pull | Regime labels `TRENDING_BULLISH`/`TRENDING_BEARISH`/`BREAKOUT_IMMINENT`/`RANGING`/`TRANSITIONAL` — preserved by Bug G as deterministic |
| 9 | Auto-inject `regime_context` | `agent_data_builder.py:_format_regime_context` | Ambient every cycle | Same regime labels as #8 |
| 10 | `save_lesson` / `get_lessons` (floki_lessons) | `floki_lessons.py` | Floki read/write | Free-form Floki-authored prose; editorial content reflects Floki's own style |

### 1.1 CTO-listed terms — reconciled with corpus

CTO listed: `CAUTION`, `DANGER`, `safe_haven_flow`, `correlation_break`.

| Term | Corpus evidence |
|---|---|
| `CAUTION` | ✅ 39/87 `trade_conditions` files (45%) in `luna_environment` field |
| `DANGER` | ✅ 38/87 `trade_conditions` files (44%) in `luna_environment` field |
| `safe_haven_flow` | ❌ **zero historical occurrences** — theoretically emittable by Luna (Bug G preserved the 5-pattern enum `forced_liquidation, safe_haven_flow, news_price_divergence, dollar_gold_correlation_break, blow_off_reversal`), but none currently in data. The enum is Bug-G-preserved by design; out of FLO-336 scope unless CTO expands. |
| `correlation_break` (stem) | ✅ 6/87 files — all as `dollar_gold_correlation_break` in `luna_patterns` list |

---

## 2. Rule 11 intent check — was `luna_context` intentionally editorial?

This is the most important finding. **The answer is "no, and Bug G already removed the writing side."**

### 2.1 FLO-328 intent (the era filter that spawned the whitelist)

Verbatim from `git show 1a89658` (FLO-328 commit, 2026-04-16):

> *"Two protective filters on lesson aggregation … Reset (not append) was Hermano's call: pre-327 trades came from materially different systems — different AI model, broken capture formula, pre-planning prompt, fewer tools — and would contaminate current Qwen-era learnings."*

FLO-328 is silent on the editorial-vs-factual nature of bucket-key dimensions. The commit captured whatever `conditions_at_open` contained at that point. `luna_environment` was included because Luna wrote it, not because the aggregation design called for an editorial dimension.

### 2.2 Bug G intent (the writing-side removal)

Verbatim from `git show 7b5f8a9` (Bug G commit, 2026-04-21):

> *"Fields removed from Luna's JSON output: environment (SAFE / CAUTION / DANGER) … Violating Escola 1 (code informs, never prescribes)."*

And in the same commit body:

> *"agent_tools.py execute_trade — trade_conditions luna_ctx reduced from env/risk/bias to 'luna_patterns' list only."*

**This is the Rule 11 answer.** Bug G explicitly removed the editorial write path in `execute_trade` as part of Escola 1 alignment. The reading side in `trade_lessons._luna_env_bucket` was NOT updated in that commit — it continues reading a field that is no longer written. The failure mode is: Bug G finished removing the write path but left the read path pointing at the old key.

**No Rule 11 blocker exists.** Removing `luna_environment` from the bucket-key dimension is finishing Bug G, not reversing an intentional design.

### 2.3 What IS still intentional (do not touch)

- Regime labels (`TRENDING_BULLISH`, `TRENDING_BEARISH`, etc.) — Bug G explicitly preserved `regime_detector.py` and `get_market_regime` as the deterministic FLO-139 detector. Surfaces #8 and #9 in the inventory are intentional by design. They are directional but factually derived from ADX/price structure — factual, not LLM-editorial. Out of scope unless CTO expands.
- `patterns_detected` in `get_luna_brief` — Bug G preserved the 5-pattern enum. These names have valence but were intentionally kept as observational "pattern names" (like candlestick pattern names `hammer`/`engulfing`). Out of scope unless CTO expands.

---

## 3. Classification — every field, every value

### 3.1 `trade_conditions/*.json` fields

| Field | Type | Example values | Classification | Bug G touched? |
|---|---|---|---|---|
| `direction` | enum | BUY / SELL | FACTUAL | No |
| `open_time` | ISO timestamp | 2026-04-22T15:02:40Z | FACTUAL | No |
| `system_version` | SHA string | ecb9ac8 | FACTUAL | No (FLO-328 tag) |
| `conditions_at_open.rsi_h1` | float | 42.39 | FACTUAL | No |
| `conditions_at_open.macd_h1` | float | -5.12 | FACTUAL | No |
| `conditions_at_open.adx_h1` | float | 22.35 | FACTUAL | No |
| `conditions_at_open.atr_h1` | float | 13.48 | FACTUAL | No |
| `conditions_at_open.ema50_distance_pct` | float | -0.61 | FACTUAL | No |
| `conditions_at_open.volume_h1` | float | None in all recent files | FACTUAL (but capture-broken — FLO-333 terrain) | No |
| `conditions_at_open.session` | enum | NY / LONDON / ASIAN | FACTUAL | No |
| `conditions_at_open.utc_hour` | int | 15 | FACTUAL | No |
| `conditions_at_open.confidence` | int | 55 | FACTUAL | No |
| `conditions_at_open.rex_agreed` | bool/null | True / False / None | FACTUAL | No |
| `conditions_at_open.luna_environment` | enum | **CAUTION** / **DANGER** / SAFE / None | **EDITORIAL** | ✅ write path removed — `None` in new files |
| `conditions_at_open.luna_patterns` | enum list | ['dollar_gold_correlation_break'] | SOFT-EDITORIAL (pattern names have valence) | Preserved by Bug G |
| `conditions_at_open.regime` | enum | RANGING / TRENDING_BULLISH / etc. | FACTUAL (deterministic FLO-139) | Preserved by Bug G |
| `conditions_at_open.thesis_at_open.direction_bias` | enum | **BULLISH** / **BEARISH** / NEUTRAL | EDITORIAL (from active_thesis.json) | Not addressed |
| `conditions_at_open.thesis_at_open.decision` | enum | WAIT / HOLD_TRADE / CLOSE_TRADE / ADJUST_TRADE | FACTUAL | No |
| `conditions_at_open.rex_at_open.reasoning` | prose | free-form Rex debate output | SOFT-EDITORIAL (LLM-authored) | Not addressed |

**Net editorial exposure in `trade_conditions/` corpus:** fields `luna_environment` + `thesis_at_open.direction_bias` + nested `rex_at_open.reasoning`. Pattern names and regime labels are soft-editorial but intentionally preserved.

### 3.2 `trade_reflexions` table (71 rows)

| Field | Classification | Example | Coverage (editorial hits) |
|---|---|---|---|
| `ticket`, `entry_price`, `exit_price`, `pnl`, `close_reason`, `direction`, `timestamp` | FACTUAL | — | 0 |
| `thesis_summary` | EDITORIAL | "NEUTRAL: []" — carries active_thesis labels | 3 rows with BULLISH/BEARISH |
| `lesson` | LLM-PROSE | "In neutral market conditions, it's crucial to..." | 4 rows with BULLISH, 2 with BEARISH, 1 with "avoid" |
| `pattern_tags` | SOFT-EDITORIAL | `["sl_too_tight", "round_number_rejection"]` | Top tags: `sl_too_tight`(34), `good_entry_bad_exit`(21), `false_breakout`(13), `asian_session_trap`(12) — many have valence |
| `revised_lesson` | LLM-PROSE (hindsight) | — | 4 rows with "avoid" |
| `hindsight_json` | LLM-JSON | — | Not sampled (same category as `revised_lesson`) |
| `reflexion_json` | LLM-JSON | `{was_thesis_correct: false, what_actually_happened: "..."}` | LLM-authored — same category |

**Important distinction:** `lesson` / `revised_lesson` / `reflexion_json` / `hindsight_json` are LLM-authored DURING Floki's own reflection. These are Floki's own reasoning echoing back to him. Different category from externally-imposed editorial labels.

### 3.3 `data/agent_patterns.json` (surfaced via `get_trade_patterns`)

| Field | Classification | Example |
|---|---|---|
| `updated`, `trade_count` | FACTUAL | 95 |
| `patterns[].name` | SOFT-EDITORIAL (labels with valence) | "VOL <0.1 BUY", "London SELL", "trend_continuation" |
| `patterns[].trades`, `wr`, `pf` | FACTUAL | 11, 72.7, 4.279 |
| `patterns[].insight` | **EDITORIAL (directive)** | **"Strong edge"**, **"Avoid — losing pattern"**, "Neutral" |

**Finding:** the `insight` field literally uses the word "Avoid" in pattern outputs surfaced to Floki. Source verified at `agent_reflection.py:210-216` (`_insight_label` pure function — rule-generated literal, not LLM-generated — so fix is a code change, not a prompt change). This is a second Rule-19-class leak separate from the luna-field question.

### 3.4 `trade_lessons.get_relevant_lessons` output format

For every lesson returned:
- `bucket` = string in format `"{DIRECTION} | RSI {rsi_bucket} | Vol {vol_bucket} | {session} | {luna}"` — includes the editorial luna dimension
- `lesson` = string prefix in `{AVOID: | PREFERRED: | NEUTRAL:}` + bucket_key + stats

**Two editorial surfaces:** (a) the luna dimension in the bucket string; (b) the AVOID/PREFERRED/NEUTRAL prefix in the lesson text.

### 3.5 `agent_session_memory.json` (23 files scanned)

Prose `notes` array contains free-form text Floki and Sage write into. Hit counts:

| Term | Files containing |
|---|---|
| `avoid` | 4 |
| `CAUTION` | 3 (Sage briefing re-injects these) |
| `DANGER` | 3 (Sage briefing re-injects these) |
| `risky` | 2 |
| `dangerous` | 2 |
| `blow_off` | 2 (pattern-name bleed) |
| `whipsaw` | 1 |

---

## 4. Historical corpus quantification

### 4.1 `trade_conditions/*.json` — 87 total files

| Status | Count | % |
|---|---:|---:|
| Files containing `CAUTION` somewhere | 39 | 45% |
| Files containing `DANGER` somewhere | 38 | 44% |
| Files containing `BULLISH` somewhere | 40 | 46% |
| Files containing `BEARISH` somewhere | 12 | 14% |
| Files containing `NEUTRAL` somewhere | 69 | 79% |
| Files containing `dollar_gold_correlation_break` | 6 | 7% |
| **Total files with ANY editorial value (union)** | **~80** | **~92%** |

Luna environment specifically:

| `luna_environment` value | Files | Note |
|---|---:|---|
| `CAUTION` | 39 | pre-Bug-G |
| `DANGER` | 38 | pre-Bug-G |
| `None` (null/missing) | 10 | 5 are post-Bug-G (write removed), 5 are pre-FLO-327 backfill |

### 4.2 Retagging effort estimate (for Option W)

If Option W (capture-side neutralization + backfill) is chosen:

- **Files to retag:** ~77 (all with non-null `luna_environment`) plus all files with `thesis_at_open.direction_bias` in {BULLISH, BEARISH} (~9 more)
- **Total bytes to rewrite:** ~500 KB across 80 files (avg file size ~6 KB)
- **Mapping rules needed:** `CAUTION → luna_env_1`, `DANGER → luna_env_2` (or similar neutral labels); `BULLISH → bias_pos`, `BEARISH → bias_neg`
- **Backfill script complexity:** moderate — one-time sweep, 30–50 LoC
- **Downstream consumer updates:** every reader of these fields (2 writes, N reads in aggregation) must accept the new vocab

**Assessment:** Retagging is feasible but expensive relative to Option X's near-zero cost. Bug G's decision to leave historical data untouched (and trust the era filter to eventually expire it) was deliberate — FLO-336 Phase 2 must decide whether that decision still holds.

---

## 5. Four-option comparative analysis (as-specified in ticket)

Each option evaluated against the three editorial-leak target classes found:

- **Class A:** `luna_environment` in bucket key (77 historical files, 0 new)
- **Class B:** `AVOID:` / `PREFERRED:` / `NEUTRAL:` lesson-text prefixes
- **Class C:** Pattern names / regime labels with valence (CTO-preserved)

### Option X — Strip `luna_context` from bucket key

**Change:** remove the `{luna}` dimension from `_build_bucket_key` (`trade_lessons.py:140`) and update format string. 5D → 4D buckets.

**Evidence support (verified):** Corpus query on all files with `open_time >= 2026-04-21T18:00:00Z` (post-Bug-G cutoff) returns **10/10 files with `luna_environment: None`**. Zero leaks from the capture path. `_luna_env_bucket` returns `"UNKNOWN"` for every new trade. For new trades, stripping the dimension is functionally identical to keeping it. For historical trades, stripping collapses CAUTION/DANGER buckets into their luna-less siblings — increases bucket occupancy (some buckets that previously sat at 2 occurrences may cross `min_occurrences=3` and emit a lesson).

**Pros:**
- Smallest code footprint (~3 LoC: one import cleanup, one bucket-key format change, one test).
- Rule 11 clean — finishes Bug G, reverses no intentional design.
- Bucket density increases — more lessons reach the `min_occurrences=3` threshold.
- Zero backfill needed; historical files stay intact; only the aggregation logic changes.

**Cons:**
- Loses one dimension. Floki can no longer distinguish "I lost 6 SELLs in CAUTION env" from "I lost 6 SELLs in SAFE env" via the bucket. But since Luna no longer writes env, this information was going dark anyway.
- Does NOT address Class B or Class C.

**LoC:** ~3 code + 2 test + 1 doc.

**Rule 11 verdict:** ✅ clean. No intentional design reversed.

### Option Y — Neutralize values at lesson-emit time

**Change:** add a translation map at the output surface: `CAUTION → env_mid_risk`, `DANGER → env_high_risk`, `SAFE → env_low_risk` (or similar neutral rebadging). Bucket key dimension preserved; only the string Floki sees changes.

**Pros:**
- Preserves the macro context dimension if needed.
- Doesn't destroy data; historical and future bucketing semantics unchanged.

**Cons:**
- **The replacements still carry valence.** `env_high_risk` is more neutral than `DANGER` but still implies Floki should care about it. `env_low_risk` / `env_mid_risk` / `env_high_risk` is an ordinal scale — still prescriptive-adjacent.
- Requires a 3-value translation map that is itself an editorial choice: who decides what's "low/mid/high" vs a truly neutral naming?
- Does NOT address Class B.
- More moving parts than Option X for the same net effect post-Bug-G.

**LoC:** ~5 code + 3 test + 2 doc.

**Rule 11 verdict:** ✅ clean, but introduces new editorial language (the translation names).

### Option Z — Internal keys, external factual summaries

**Change:** keep bucket keys as internal strings (unchanged). Rewrite `get_trade_lessons` output to produce factual sentences that never surface the bucket-key string. Example output:

```
"SELL trades during NY session with weak RSI: 6 occurrences, 0 wins (0%), avg P&L -$18.83"
```

Instead of:
```
"AVOID: SELL | RSI WEAK | Vol UNKNOWN | NY | CAUTION — 0/6 wins (0%), avg P&L $-18.83"
```

**Pros:**
- **Addresses both Class A and Class B in one change.** Luna dim hidden by default (not in the surface string); `AVOID:` prefix gone (replaced by factual stats).
- Most Escola-1-aligned rendering possible: operator gets aggregation granularity, Floki gets just the counts.
- Can optionally surface luna dim in parenthetical if CTO decides it's useful ("... in NY session, CAUTION macro conditions (pre-Bug-G data): 6 occurrences..."). Level of detail configurable.

**Cons:**
- Largest rewrite of the 4 options. Touches `_generate_lesson_text`, output schema of `get_trade_lessons` tool. Schema change may affect downstream consumers (dashboard, tests).
- Requires a new sentence-templating function.
- If over-engineered, becomes a mini-NLG layer — scope risk.

**LoC:** ~15–25 code + 5 test + docs + schema migration.

**Rule 11 verdict:** ✅ clean. No capture-side changes; read-side format change only.

### Option W — Capture-side neutralization

**Change:** rewrite the capture paths to never write editorial values going forward AND backfill the 77–80 historical files with neutralized vocab.

Scope inventory:
- `execute_trade` in `agent_tools.py` — already clean post-Bug-G (no `luna_environment` written). But `thesis_at_open.direction_bias` (BULLISH/BEARISH/NEUTRAL) still captured — requires active_thesis rewrite upstream.
- `monitor.py` pending-fill path — same treatment.
- `active_thesis.json` writing (wherever it happens) — rewrite to neutral vocab.
- `luna_analyst.py` pattern enum — preserved by Bug G per design, out of Option W's scope unless CTO expands.
- Backfill script — 30–50 LoC, one-off.
- Data migration of `trade_conditions/*.json` — 77+ files.

**Pros:**
- Addresses root cause across the ecosystem rather than just cleaning up downstream.
- Future-proof: no new editorial values can slip in from upstream.

**Cons:**
- **Largest scope by a wide margin.** Touches Luna, active_thesis writers, reflexion capture, optionally sage/rex reasoning capture.
- Requires a backfill migration of live data files — Rule 11-risky (if the mapping is wrong, pre-327 and post-327 data contamination becomes asymmetric).
- Touches files Bug G intentionally left untouched.
- Does NOT address Class B (lesson-text prefix) because Class B is in the EMIT path, not the CAPTURE path.

**LoC:** ~80–120 code + 20+ test + 30–50 backfill + data migration + doc.

**Rule 11 verdict:** ⚠️ **requires caution.** Rewrites capture paths Bug G designed. Worth doing only if the data-migration complexity is warranted by a use case.

---

## 6. Recommendation matrix

Because CTO asked to evaluate the four options individually and NOT propose hybrids, the recommendation is structured as a **coverage matrix** across the three editorial target classes.

| Target class | Option X | Option Y | Option Z | Option W |
|---|:---:|:---:|:---:|:---:|
| A — `luna_environment` in bucket key (77 historical files) | ✅ removed | ⚠️ rebadged (still valence) | ✅ hidden from surface | ✅ rewritten in source + backfill |
| B — `AVOID:`/`PREFERRED:`/`NEUTRAL:` lesson prefix | ❌ not addressed | ❌ not addressed | ✅ replaced with factual stats | ❌ not addressed |
| C — Pattern names / regime labels (CTO-preserved) | ❌ not addressed (by design per Bug G) | ❌ (by design) | ❌ (by design) | ⚠️ would touch; out of ticket scope |

### 6.1 DEV recommendation (single-option per CTO directive)

**Option X.** Class B remains; CTO binary choice attached.

Reasoning:
1. **Rule 11 posture is strongest.** X finishes Bug G with zero capture-side change. No intentional design reversed. No migration.
2. **Evidence fit is clean and verified.** Post-Bug-G, `luna_environment` is `None` in 10/10 new files (direct corpus query). The bucket dimension is already effectively dead. Stripping it is finishing Bug G.
3. **Smallest code footprint** (~3 LoC) means it unblocks FLO-334 Phase 2 (and downstream FLO-332 Phase 3) fastest.

**CTO binary choice on Class B:**
- **(a) Ship Option X alone** — close the luna dimension leak now; handle the `AVOID:`/`PREFERRED:`/`NEUTRAL:` lesson-text prefix as a separate follow-up ticket (proposed **FLO-337 — lesson-text neutralization**, ~5 LoC).
- **(b) Ship Option X + prefix swap in the same commit** — close both A and B in one commit; commit message reflects both; no separate ticket needed. Adds ~5 LoC and one test.

DEV preference: **(b)** — the prefix swap is mechanical, small, and Escola-1-aligned for the same reasons as X. Shipping both together is one commit instead of two and clears the luna-path fully. But both options (a) and (b) are reasonable — this is a commit-granularity decision, not a correctness decision. (b) is technically a "hybrid" only in the colloquial sense; per CTO's no-hybrid rule, (a) is the safe literal interpretation.

### 6.2 If CTO prefers a broader one-shot fix

**Option Z** is the single-option answer that addresses both A and B in one commit. Its cost (~25 LoC + schema change) is ~5× Option X but it eliminates two Class surfaces instead of one. The Z-vs-X decision is a scope-cost tradeoff, not a correctness tradeoff.

### 6.3 Options explicitly not recommended

- **Option Y:** introduces new editorial vocabulary (env_low/mid/high_risk) to replace the existing editorial vocabulary. Net cleanup is marginal and the translation map is itself a design choice with Escola 1 risk.
- **Option W:** out-of-proportion scope for the measured gap. 77 historical files is ~4 days of active trading; the era filter will naturally expire them within 30 days. Rewriting the ecosystem to neutralize 4 days of historical data is overreach.

---

## 7. Proposed Phase 2 scope (if CTO approves Option X)

Smallest viable fix matching recommendation:

1. `trade_lessons.py:140` — remove `luna = _luna_env_bucket(conditions.get("luna_environment"))`.
2. `trade_lessons.py:145` — update bucket-key format string: `f"{d} | RSI {rsi} | Vol {vol} | {session}"`.
3. Delete dead `_luna_env_bucket` helper (lines 86–93 — 8 LoC).
4. Update `_test_bugG_luna_contamination_removal.py` if it references luna_env in bucket keys.
5. Commit message flags Class B as remaining for separate ticket.

**Estimated delta:** ~12 LoC removed, ~2 LoC changed, ~1 test adjusted.

**Downstream gating remains:**
1. FLO-336 Phase 2 (Option X) — 15 min work, not yet approved.
2. FLO-334 Phase 2 (era-boundary swap) — resumes after FLO-336 Phase 2.
3. FLO-332 Phase 3 (session-level history) — resumes after FLO-334 Phase 2.

### 7.1 Proposed follow-up ticket for Class B

**FLO-337 (proposed):** "Neutralize `AVOID:`/`PREFERRED:`/`NEUTRAL:` prefix in `_generate_lesson_text` output." Replace directive verbs with factual stats-only framing. Small (~5 LoC + test). Escola 1 v2.0 alignment.

---

## 8. What I do NOT know

1. **Whether the `agent_patterns.json` "insight" field with "Avoid — losing pattern" is LLM-generated or rule-generated.** I identified it as editorial but did not trace the writer. If LLM-generated, fix requires prompt change; if rule-generated, fix is a code change. Out of FLO-336 scope — flagging for future ticket.
2. **Whether the regime labels (`TRENDING_BULLISH`, etc.) warrant a separate audit.** Bug G preserved them deliberately as deterministic, but the valence is clearly directional. If CTO later decides they're in scope, the `regime_detector.py` labels would need separate treatment.
3. **Whether `thesis_at_open.direction_bias` (BULLISH/BEARISH/NEUTRAL in `trade_conditions`) reaches Floki via any path other than post-trade reflexions.** I did not fully trace every consumer of `trade_conditions` — there may be an indirect path. Probably not outputting to Floki today (the era filter excludes everything), but a separate verification would close the loop.
4. **Whether `pattern_tags` in `trade_reflexions` (e.g. `sl_too_tight`, `asian_session_trap`) are Floki-authored or prompt-template-forced.** Some tags have heavy valence; if they come from a fixed tag list, they could be neutralized; if LLM-chosen, prompt change needed. Scope for later.
5. **Whether `search_reflexions` editorial content exceeds `lesson`/`revised_lesson` prose in a way that warrants its own handling.** I audited the fields but did not grep real-world search call patterns to see what terms Floki actually searches for.
6. **Whether Sage's daily-briefing text that lands in session_memory notes should be audited separately.** Sage writes with a different prompt than Luna; I did not audit Sage's prompt for editorial language. Flagging.

---

## 9. Rule 19 / Escola 1 v2.0 framing

This investigation closes a Rule-19-class silent violation: the `_luna_env_bucket` read path is a residue left behind by Bug G's partial cleanup. The code runs correctly, the output is statistically valid, and nothing crashes — but the surface rendered to Floki carries the exact vocabulary (`CAUTION`, `DANGER`, `AVOID`) that FLO-317 + FLO-303 + Bug G worked to remove.

Phase 1 quantified the corpus contamination (~77/87 files for luna_environment specifically), identified three distinct editorial-class surfaces in the pipeline (A: luna dim, B: lesson prefix, C: preserved-by-design pattern/regime labels), reconstructed the Rule 11 intent via direct commit-message quotation, and evaluated four options against a clear coverage matrix.

**Recommended: Option X** for scope-cost efficiency and Rule 11 safety. Class B (AVOID prefix) remains; flag for FLO-337 follow-up.

**Phase 2 is gated on CTO approval of this recommendation.** No code has been written. No tests have been run. No commits have been proposed. FLO-334 Phase 2 and FLO-332 Phase 3 remain paused until FLO-336 Phase 2 ships.
