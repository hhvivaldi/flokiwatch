# FLO-332 Phase 1 — Session-Level Pattern Awareness Investigation (READ-ONLY)

**Status:** Phase 1 complete. Findings + option ranking + Phase 2 scope proposal.
**Generated:** 2026-04-22 UTC
**Evidence artifact:** `data/_audits/flo332/phase1_audit_results.json`
**Scope:** Read-only. No production code changed. No POC ran.

---

## HEADLINE — direct answer

Three findings that together determine the recommendation:

1. **The session-memory auto-inject mechanism already exists (Rule 11 win).**
   `agent_data_builder.build_data_package` / `build_proactive_data_package` attach the full `session_memory` dict (thesis + today-counters + prose notes) to the package (lines 1352, 1433). In the proactive path — which covers the overwhelming majority of Floki cycles — `ai_agent._build_user_message` (line 1693) renders the whole package via `json.dumps(data_package, indent=2, default=str)` into Floki's user message. **So session_memory is present in every proactive cycle as a JSON sub-object.** (The defined-but-dead `format_session_memory_xml` is a red herring — it exists at line 32 but has no production call-site. The fast path uses `format_fast_xml`, which is trigger-data only and does NOT include session_memory. FLO-332 targets the proactive path.) Bottom line: FLO-332 is not introducing injection — it is filling a gap in the existing injection shape.

2. **What is injected is prose, not structurally filtered.**
   The current block contains `<thesis>` + `<trades_today/wins_today/losses_today>` + up to 10 `<note time="HH:MM">free prose</note>`. Crucially, `write_session_memory` echoes the caller's prior notes back verbatim in its response. So before OPEN_BUY #7 at 13:45 UTC Floki's echoed notes included `"Price at 4735 confluent support. M5 bounce from 4741 promising..."` and before OPEN_BUY #9 at 15:03 UTC they included `"Price holding 4735 FLIP zone..."`. **The information Floki had was his own prior *interpretation* of 4735; what he did NOT see was a structurally-filtered list of outcomes from his recent attempts at ±10 pips of current price.**

3. **Active retrieval is inconsistent, but ambient context is always present.**
   Distinguishing *active retrieval* (calling `read_session_memory`/`get_trade_lessons`) from *ambient context* (auto-injected `<session_memory>` block):
   - Ambient: present in 100% of cycles.
   - Active: 5/16 OPEN cycles in 7d (31%), 21/41 in 30d (51%).
   - Of today's 3 losing OPEN_BUYs (#6, #7, #9), **zero** called `read_session_memory` pre-OPEN; `get_trade_lessons` was called for #6 and returned an empty list.

**Does he see the market correctly?** The ambient context surfaces *what* he thought about recent levels, not *what happened* to trades at those levels. Active retrieval of structural outcome data did not happen for the losing trades.

**Recommended path:** **Level 2 — extend the existing `<session_memory>` auto-inject with a new structurally-filtered sub-element `<recent_attempts_at_current_level>`.** This preserves Escola 1 v2.0 (observational, not prescriptive) and is zero-risk because the injection mechanism is already battle-tested in production.

A boundary finding (see §5) identifies a separate P2 bug that should not fold into FLO-332.

---

## 1. Session infrastructure audit (WS1)

### 1.1 Files, writers, readers

| File | Purpose | Writer(s) | Reader(s) |
|---|---|---|---|
| `data/agent_session_memory.json` | Current-session thesis + prose notes + today's counters | `agent_tools.write_session_memory` (Floki tool), `ai_agent._update_session_memory` (post-parse safety net) | `agent_tools.read_session_memory` (Floki tool), `agent_data_builder.load_session_memory` (auto-inject every cycle) |
| `data/agent_session_memory_YYYY-MM-DD.json` | Prior-day archives (auto-rotated) | `ai_agent._update_session_memory` when session_date changes | None in production reads — archival only |
| `data/agent_memory.json` | `AgentMemory` dataclass (active rejects, conditions, invalidation) — NOT session-level pattern memory | `agent_memory.write_memory` | `agent_memory.read_memory`, `agent_memory.get_memory_context_for_agent` |
| `data/agent_patterns.json` | Reflection engine's pattern memory — different layer | `agent_reflection` | `agent_tools.get_trade_patterns` |
| `data/trade_lessons.json` + `data/trade_conditions/*.json` | On-read aggregated lessons per era/commit SHA | `trade_lessons.extract_trade_lesson` at trade close | `trade_lessons.get_relevant_lessons` via `agent_tools.get_trade_lessons` |

**Rule 11 verdict:** Session memory infrastructure is already mature. Multiple layers exist: per-session thesis/notes (prose), reflection patterns (structured), era-aware lessons (structural). **None of them answer the question "what happened the last N times I opened near this price?".** That is the unfilled shape.

### 1.2 Today's agent_session_memory.json at time of OPENs

Snapshot at 17:07 UTC (current file) — 8 prose notes covering 16:29–17:07, plus 21:00 Sage briefing. Example notes visible at OPEN_BUY #9 (15:03):
- `"Price holding 4735 FLIP zone after M15 stochastic turned up from oversold."`
- `"SL tightened to breakeven on BUY #1606526654. Watching 4736 support test."`
- `"M15/M5 show price testing 4736 confluence support with small bounce forming."`

These notes contain Floki's **prior thesis at 4735**, not a structured history of *outcomes* at 4735. The most recent closed-trade outcome at that level is mentioned in *prose* (`"SL tightened to breakeven on #..."`) but is not surfaced as a machine-parseable record keyed to the current price.

---

## 2. Tool-call frequency audit (WS2)

Source: `agent_proactive_analyses.tool_trace` (per-cycle JSON array of tool calls).

### 2.1 7-day and 30-day OPEN-cycle memory-tool usage

| Metric | 7 days | 30 days |
|---|---:|---:|
| Total Floki cycles | 796 | 1,950 |
| OPEN decisions | 16 | 41 |
| WAIT decisions | 610 | ~1,550 |
| OPEN cycles with any memory read | **5 (31%)** | **21 (51%)** |
| OPEN cycles WITHOUT any memory read | 11 (69%) | 20 (49%) |

Memory-read means any of: `read_session_memory`, `get_trade_lessons`, `get_position_history`, `get_trade_patterns`, `get_recent_reflexions`, `search_reflexions`, `search_memory`, `get_trade_journal`.

### 2.2 Today's 3 OPEN_BUYs (the losing cluster)

| Decision | ID | UTC | read_session_memory? | get_trade_lessons? | Other memory tools? |
|---|---:|---|:---:|:---:|:---:|
| OPEN_BUY #6 | 4700 | 12:43 | ❌ | ✅ (returned empty) | write_session_memory |
| OPEN_BUY #7 | 4709 | 13:45 | ❌ | ❌ | write_session_memory only |
| OPEN_BUY #9 | 4721 | 15:03 | ❌ | ❌ | write_session_memory only |

Note: `write_session_memory` is a write, not a read — but its tool response echoes the caller's last 3 notes back. So Floki was REMINDED of his prior notes (prose), just without having actively requested them.

**Observation:** On the cycle that produced the one BUY that won (#4 at 00:41 UTC, decision id=4630), Floki DID call `get_trade_lessons`. It also returned empty. So the presence of a memory call is not outcome-predictive in a meaningful way — call rate is low across the board and the tools don't all carry usable payloads.

### 2.3 Ambient vs active retrieval distinction

Active retrieval (tool call): inconsistent — 31–51% of OPEN cycles.
Ambient context (auto-injected `session_memory` JSON sub-object via `json.dumps` in the proactive path): **present in 100% of proactive cycles.** (Fast-path trigger cycles do not carry it; they are a minority and are out of scope here.)

The meaningful question is not "does Floki read session memory?" — he sees it ambient. The meaningful question is **"does the format of the ambient sub-object answer the structural question 'have I attempted this level recently and what happened?'" — and the answer is no.**

---

## 3. Pattern rigidity prevalence — 30-day baseline (WS3)

Definition: a "repeat" is a trade whose entry price is within 5 pips ($0.50) of another trade already executed *earlier the same UTC day*.

| Radius | Total trades (30d) | Repeats | % Repeats | WR on repeats | WR on fresh |
|---|---:|---:|---:|---:|---:|
| 5 pips | 98 | 11 | 11.2% | 36.4% (4/11) | 40% (35/87) |
| 10 pips | 98 | 15 | 15.3% | 40.0% (6/15) | 39% (34/83) |

**Interpretation:** same-session re-entry at the same narrow level happens in ~11% of trades at 5-pip radius and ~15% at 10-pip radius. Today's 3-trade cluster at 4735 (3/9 = 33% of today's trades are repeats) is well above baseline frequency. **But the WR difference between repeat-entries and fresh-entries is not statistically meaningful at n=11 — I will not claim outcome degradation.**

The rate evidence supports "this is a recurring behavioral pattern" but not "repeats are categorically worse." The behavioral evidence in §2 (pre-OPEN memory-retrieval absent) is the stronger case.

### 3.1 Top cluster days, 30d, 5-pip radius

| Day | Total trades | Clusters | Max cluster size |
|---|---:|---:|---:|
| 2026-04-08 | 12 | 1 | 2 |
| 2026-04-09 | 12 | 2 | 2 |
| 2026-04-13 | 12 | 2 | 2 |
| 2026-04-14 | 9 | 1 | 2 |
| 2026-04-16 | 8 | 1 | 2 |

Today (2026-04-22, not yet in 30d aggregate because the script ran mid-day) — max_cluster_size=3 at 4735-4736 based on the FLO-331 analysis. **Today's 3-trade cluster exceeds every other day in the 30-day window.** Either today is anomalous, or the baseline isn't fully capturing clusters because of the narrow window boundary.

---

## 4. Option evaluation (WS4)

Ranked by evidence, not speculation.

### Option A — new observational tool `get_session_trade_history(level, radius)`

- Pros: tool-based, pure pull, preserves Floki agency, fits the "tools not rules" pattern.
- Cons: **adoption risk.** Active memory-tool calls happen in 31–51% of OPEN cycles; `get_trade_lessons` call rate is reasonable (~10% of cycles) but **94.7% of calls return empty** in the last 7 days — signal that the existing pull-based lesson layer is silently broken, and a new pull tool could die the same way. Adoption would have to be taught via prompt, and every injection of "you should call get_session_trade_history when near a repeated level" is a Level-3 prescriptive nudge (Escola 1 collision).
- **Adoption data:** neither of today's losing OPEN_BUYs (#7, #9) called any memory-read tool. A new tool would have zero calls in those slots.
- Verdict: **weak.** Does not match evidence pattern.

### Option B — auto-inject a structured `<recent_attempts_at_current_level>` sub-block inside the existing `<session_memory>` (Level 2)

- Pros: Rule 11 win — uses the **existing auto-inject pipeline** already validated over N months. Information is ambient, not pulled. Purely observational (entry/SL/outcome/pnl). The gap it fills is precisely the one the evidence identifies: structural filter by current price, outcome column, keyed to this session. Doesn't touch prompt rules. Small LoC surface.
- Cons: adds ~100–300 tokens per cycle depending on cluster density (bounded because filtered to ±N pips of current price). Introduces a new data shape — requires a loader and XML formatter.
- **Evidence match:** hits the single measured gap (structural filter absent). Auto-injection is how `<current_price>`, `<indicators>`, `<regime>`, `<open_positions>`, `<agent_memory>`, `<session_memory>` thesis already work. Adding a structural sub-element is an extension, not a new pattern.
- Verdict: **strong.** Primary recommendation.

### Option C — enhance existing tools (e.g. make `get_trade_lessons` work again)

- Pros: smallest surface if the issue is era-filter config.
- Cons: `get_trade_lessons` empty-rate is 94.7% over 7 days (124/131 calls). That is a **separate bug** — likely FLO-327 `LESSONS_CURRENT_ERA_SHAS` is narrower than intended, so the on-read filter excludes everything. Fixing it restores lesson availability but does NOT surface structural "attempts at current level" — lessons are aggregated by *bucket* (direction+RSI+session+volume+luna_env), not by *price*. Fixing C is valuable independently but does not address H3/H4.
- Verdict: **adjacent fix, not primary.** Recommend as a separate P2 ticket.

### Option D — prompt engineering (Level 3)

- Pros: lowest LoC.
- Cons: Escola 1 v2.0 violation risk. Any phrasing like "before a BUY, call read_session_memory" is prescriptive workflow. Already-tried pattern: the FLO-317 session-block removal *explicitly* moved the opposite direction because forced prompt caution produced death spirals. Phase 1 of FLO-322 spent a week demonstrating the CTO-preferred path is informational, not instructional.
- Verdict: **last resort. Reject unless B is blocked.**

### 4.1 Ranking

1. **Option B** — evidence-strongest, Rule-11-compliant, Escola-1-compliant.
2. **Option A** — tool-based fallback if B is blocked on token budget.
3. **Option C (separately)** — fix the 94.7% empty rate, track as own ticket.
4. **Option D** — not pursued.

---

## 5. Boundary finding — `get_trade_lessons` 94.7% empty (separate P2)

Over the last 7 days Floki called `get_trade_lessons` 131 times and received an empty list in 124 of them (94.7%). Today: 22/22 empty. Root cause not diagnosed in Phase 1 — suspected era filter (`LESSONS_CURRENT_ERA_SHAS`) too narrow post-FLO-327.

**Recommendation:** separate ticket (proposed FLO-333 — data-quality). Do NOT fold into FLO-332. They address different surfaces.

Connected to the `trade_conditions` NULL field-capture regression surfaced by FLO-331 (`rex_agreed` populated ~12% of the time, `luna_environment` NULL for today's 9 trades). These two issues degrade the lesson corpus together.

---

## 6. Escola 1 v2.0 compliance check (WS6)

For the recommended **Option B** block, here is the exact proposed XML shape (for Phase 2 design — NOT yet implemented):

```xml
<recent_attempts_at_current_level current_price="4735.92" radius_pips="10">
  <attempt time="15:03" direction="BUY"  entry="4735.98" sl="4732.98" outcome="SL"    pnl="-5.20"/>
  <attempt time="13:45" direction="BUY"  entry="4735.78" sl="4732.78" outcome="SL"    pnl="-6.10"/>
  <attempt time="12:43" direction="BUY"  entry="4747.20" sl="4744.00" outcome="SL"    pnl="-3.40"/>
  <attempt time="23:51" direction="SELL" entry="4738.18" sl="4741.18" outcome="SL"    pnl="-12.96"/>
</recent_attempts_at_current_level>
```

**Language audit:**
- `time`, `direction`, `entry`, `sl`, `outcome`, `pnl` — all **factual attributes**.
- `outcome="SL"` — descriptive (stop loss hit); not `"avoid"`, `"don't"`, `"warning"`.
- No `<recommendation>`, `<action>`, `<should>`, `<must>`, `<note>` elements.
- No language directing Floki what to do with the information.
- Parallel construction with existing observational blocks (`<sr_zones>`, `<open_positions>`, `<trade_feedback>`).

**Verdict:** passes Escola 1 v2.0. Information, not instruction. Floki sees it. Floki decides.

Rendered reality: the proactive path JSON-dumps the whole `data_package`, so this block will ship as a new sub-object `"recent_attempts_at_current_level"` (or sibling key) inside the dict. Floki sees it as JSON, not XML, unless Phase 2 also adds an XML rendering path. XML shape above is for readability in this doc.

### 6.1 What this block does NOT do (explicit non-goals)

- Does not block trades.
- Does not adjust confidence.
- Does not inject caution text.
- Does not prescribe a cool-down period.
- Does not hide when the data is "inconvenient."

It only reports. Escola 1 v2.0 axiom: *"The system informs; Floki decides."*

---

## 7. Phase 2 scope proposal

### 7.1 Design work (1–2h)

1. Decide the radius parameter. Defaults: 10 pips for the "visible" set, keep 5 pips structural. Make configurable via `config.py`.
2. Decide the lookback window. Candidates: current session only, or last 24h. **Recommend: current UTC day only** — matches `agent_session_memory.json` boundary semantics.
3. Decide max attempts displayed. Cap at 5 most recent to bound token growth.
4. Decide source: `history.db` `trades` table — filter by `open_time` within today's boundary and `abs(open_price - current_price) <= radius * 0.10`.
5. Decide insertion point in the XML: nested inside existing `<session_memory>` OR standalone. **Recommend: standalone** sibling of `<session_memory>` — avoids coupling to session notes parser.
6. Token budget estimate: 4 attempts × ~80 chars = ~320 chars, well under 100 tokens.

### 7.2 Implementation work (est. 30–50 LoC)

- `agent_data_builder.py` — new loader function reading from `history.db` keyed to current price + today boundary, new formatter `format_recent_attempts_xml()`, call from `build_data_package` / `build_proactive_data_package` between `<session_memory>` and `<sr_zones>`.
- `config.py` — constants for radius and cap.
- One SQL unit test.
- Verification: run `python main.py --test` once, confirm XML block appears in the constructed prompt package, confirm no regressions in parsing.

### 7.3 Validation plan

- Replay today's 9 trades with the new block in a test harness — show what Floki *would* have seen before each OPEN_BUY.
- No counterfactual claim about outcomes (too noisy with n=3), only a visibility claim: "this block would have put `outcome="SL" pnl=...` on his screen before OPEN #7 and #9."
- 5-day observation window post-deploy, similar pattern to FLO-322.

### 7.4 Phase 2 deliverable

`data/_audits/flo332/FLO-332_Phase2_Design.md` — full design doc with:
- SQL query drafts
- XML schema
- Insertion point diff
- Token-budget math
- Test plan
- Rollout checklist

---

## 8. What I do NOT know

1. **Whether the same block would have changed the trades.** The block makes the information visible. Whether Floki weights it correctly is a separate question answerable only post-deploy. Phase 1 does not assert a counterfactual.
2. **Whether the radius / lookback defaults are right.** Phase 2 design must propose values and justify; current 10-pip / today-only is a starting guess grounded in today's cluster geometry.
3. **Whether existing `<session_memory>` prose notes should also be restructured** (e.g., `<note time=... outcome="SL" ticket=...>`) — Phase 2 may opt for one mechanism or both. Recommend: start with the new block; don't restructure the narrative notes yet.
4. **Whether `get_trade_lessons` fixing (Option C / FLO-333) would reduce FLO-332's value** if lessons contained level-rigidity buckets. Separate ticket, separate analysis.
5. **30-day pattern-rigidity WR degradation is not statistically established at n=11.** I flagged this as a frequency finding only.

---

## 9. Summary

- **Infrastructure already exists.** `<session_memory>` is auto-injected every cycle.
- **The gap is structural, not mechanical.** Prose notes answer "what did I think?", not "what happened when I tried this level?"
- **Active memory retrieval is sparse pre-OPEN.** 0/3 of today's losing OPEN_BUYs called `read_session_memory`, though write-echoes gave them prose reminders.
- **Pattern rigidity frequency = 11–15% of trades over 30 days.** WR degradation cannot be claimed at this sample size.
- **`get_trade_lessons` returns empty 94.7% of the time.** Separate P2 ticket (proposed FLO-333). Weakens Option C.
- **Recommended: Option B — extend the existing auto-inject with `<recent_attempts_at_current_level>`.** Escola 1 v2.0 compliant; Rule 11 compliant; evidence-matched.
- **Phase 2 scope:** 1–2h design doc, 30–50 LoC implementation, 5-day observation window.

**TL;DR for CTO:** the mechanism is there — we're adding one structured sub-element, not a new pattern. Evidence supports Level 2. No prescriptive language. Phase 2 design doc on request.

**Ticket linkage (from FLO-331 Phase 1):** FLO-322 closed H5 (tight SL). FLO-330 addresses H1 (macro narrative). **FLO-332 addresses H3/H4 (pattern rigidity / reactive chasing) — different shape.** Each ticket is orthogonal and composes cleanly.
