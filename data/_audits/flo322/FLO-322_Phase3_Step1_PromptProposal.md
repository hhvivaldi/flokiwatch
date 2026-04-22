# FLO-322 Phase 3 — Step 1: H1-b Prompt Text Proposal

**Status:** DRAFT for CTO approval · **No code · No deployment**
**Date:** 2026-04-22
**Author:** DEV (Claude-Opus 4.7)
**Scope:** Propose exact replacement text for the SL advisory at `agent_prompts.py:193-197`. CTO approval required BEFORE backtest.

---

## 1. Current text — exact location and block

File: `agent_prompts.py`
Block: `FAST_DECISION_PROMPT` (NOT `SYSTEM_PROMPT`)
Lines 186-197 for context:

```
<task>
You have exactly 3 options:
1) ACT — take an action now (open/close/adjust)
2) HOLD — do nothing (thesis intact)
3) DISMISS — trigger is noise; ignore it
</task>

<context>
The spread and ATR tell you the minimum market noise. A stop loss that doesn't cover at least spread + typical candle range will be hit by random movement, not by thesis invalidation. Factor this into your SL placement.

If <active_trade_context> includes phase and current_sl, you can override at any time by choosing ADJUST or CLOSE.
</context>
```

Line 194 is the single-paragraph advisory to be replaced. The `<active_trade_context>` paragraph (line 196) stays untouched.

---

## 2. CRITICAL — design-doc evidence base is misscoped

**This is the highest-leverage finding in the doc. Read carefully before approving.**

### Phase 2 design doc's central claim
*"The informational guidance has failed to change behavior over the 40+ day window since 2edd74d shipped."* — FLO-322 Phase 2 design doc §1.3.

### What Phase 1 actually measured
"23 of 63 trades (36.5%) in the conditions-tracked window had SL < 0.5 × H1-ATR." These numbers come from `data/trade_conditions/*.json`, which is written **at OPEN time**, not at ADJUST time.

### What the existing advisory actually reaches

| Prompt | File location | Used by | Can issue OPEN? |
|---|---|---|---|
| `FAST_DECISION_PROMPT` (where the advisory lives today, line 194) | `agent_prompts.py` | `main.py:3326` `agent_fast_decide()` — Simba-triggered fast cycles only | **NO** — `main.py:3415` blocks OPEN: *"AGENT_FAST \| ACT blocked (OPEN disabled)"* |
| `SYSTEM_PROMPT` (no SL guidance) | `agent_prompts.py` | `ai_agent.agent_decide()` at `ai_agent.py:1276, 2006` — the main Floki cycle that issues OPEN | YES — this is where the measured 36.5% sub-0.5-ATR cohort was decided |

Grep proof:
```
grep FAST_DECISION_PROMPT ai_agent.py  →  0 matches
grep SYSTEM_PROMPT ai_agent.py         →  used at lines 1276, 2006
```

### The inversion

**The advisory hasn't "failed on OPEN" — it was never visible at OPEN.** OPEN decisions route through `SYSTEM_PROMPT`, which has zero SL/ATR guidance. The sub-0.5-ATR cohort Phase 1 measured was decided without ever seeing the advisory that Phase 2 proposes to rewrite.

### Implication for the CTO decision

The Phase 2 "Caminho B — staged minimal" approval was predicated on the framing that editing `agent_prompts.py:194` would affect OPEN-time SL placement. It will not, because that line is in the wrong prompt for the measured problem.

You now have two different decisions to make, not one:

- **(a) If you want the backtest to test ADJUST-distribution shifts on the FAST path:** keep the edit at line 194 (FAST), accept that the effect size on the sub-0.5-ATR cohort will be near-zero because FAST can't OPEN trades, and reinterpret the backtest as a narrower test.
- **(b) If you want the backtest to address the measured OPEN-time problem:** the edit must reach `SYSTEM_PROMPT`, not just FAST.

This is NOT a scope nuance — it changes what Caminho B is actually shipping. Flagging explicitly so you are approving the edit you intend, not the edit you thought the design doc implied.

### Scope options

| Option | Reach | Matches measured problem? | Risk |
|---|---|---|---|
| **S1 — strict** (FAST only, line 194) | ADJUST decisions via Simba fast path | ❌ does not touch OPEN SL | Minimal. Cheap revert. Effective backtest signal = near-zero. |
| **S2 — OPEN-focused** (add analogous block to SYSTEM_PROMPT; leave FAST untouched) | OPEN decisions, ADJUST via main cycle | ✅ touches OPEN-time SL | Moderate — new block in a large prompt, some tokens added. Testable. |
| **S3 — both** (edit FAST + add to SYSTEM_PROMPT) | OPEN + ADJUST | ✅ both paths | Moderate — two surfaces changed; two tests needed. |

**DEV recommendation: S2 (add to SYSTEM_PROMPT).** Rationale:
- The measured problem is OPEN-time; the fix must reach OPEN-time.
- FAST already carries the current text — leaving it avoids a regression-shaped change on a path where no new problem exists.
- Single edit surface. Single revert. Single backtest attribution surface.

If CTO insists on **strict-to-design-doc literal wording** ("replace line ~194"), **S1 is acceptable only if CTO is OK that the backtest effect-size will be near-zero** (measurable on ADJUST behavior only, not on OPEN distribution).

**I need this decision before the backtest runs.** Backtest design differs between S1 and S2/S3 (what trades count, what counterfactual applies).

---

## 3. Proposed replacement text (under S2 — recommended)

### 3a. New block to INSERT into `SYSTEM_PROMPT`

**Exact insertion site: between `</decisions>` (line 106) and `<output>` (line 108).** Rationale: decision types are enumerated in `<decisions>`, SL mental model is decision guidance (not output schema), then `<output>` covers JSON format. Semantically this is the clean place.

Before/after:

```
...                                             # line 105 (last decisions example)
</decisions>                                     # line 106
                                                 # line 107 (blank)
<output>                                         # line 108
FLO-295 PRIMARY CHANNEL: end every cycle...
```

↓ becomes ↓

```
</decisions>                                     # line 106
                                                 # blank
<sl_placement_mental_model>                      # NEW — inserted here
...  (block contents below)
</sl_placement_mental_model>
                                                 # blank
<output>
FLO-295 PRIMARY CHANNEL: end every cycle...
```

**Block contents (verbatim):**

```
<sl_placement_mental_model>
Noise floor (rule of thumb for XAU/USD):

- weighted_ATR = 0.5 × M5_ATR + 0.5 × H1_ATR
  (H1_ATR is in your data package as <atr value=... description="Average True Range H1"/>. M5_ATR is available via get_indicators(timeframe='M5') — call it when you're about to set or adjust an SL.)

- Noise floor: SL distance ≥ spread + 1.0 × weighted_ATR.
  Tighter than this puts the stop inside normal market noise — more likely hit by random movement than by thesis invalidation.

- Preferred placement: put the SL one weighted_ATR past the nearest structural level you'd use to invalidate your thesis, not on it. A stop at the level gets swept by wick; a stop beyond it requires a real break.

Guideline, not a gate. You own the SL choice. The numbers just tell you when you're inside the noise band.
</sl_placement_mental_model>
```

### 3b. Replacement text for FAST_DECISION_PROMPT `<context>` block (under S1 or S3)

If CTO picks S1 (FAST only) or S3 (both), the same content compressed for the fast-decision context:

```
<context>
Noise floor for SL placement (rule of thumb):
- weighted_ATR = 0.5 × M5_ATR + 0.5 × H1_ATR (H1 in data, M5 via get_indicators(timeframe='M5'))
- SL distance ≥ spread + 1.0 × weighted_ATR — tighter sits inside normal noise
- Preferred: SL one weighted_ATR past the structural invalidation level, not on it

Guideline, not a gate. You own the SL choice.

If <active_trade_context> includes phase and current_sl, you can override at any time by choosing ADJUST or CLOSE.
</context>
```

Line count: original 1 sentence → new 5 lines. Token cost: ~40 extra tokens per fast-decision call (negligible given 25k-30k cached prefix per cycle).

---

## 4. Weighting formula — 0.5 × M5 + 0.5 × H1

### Why 50/50

**Factors considered:**

| Weight split | Behaviour | Notes |
|---|---|---|
| 100/0 (M5 only) | Pure short-horizon noise | Too tight when H1 is trending/volatile — misses the broader context that dominates after ~20min of exposure |
| 70/30 (M5-heavy) | Reactive to short-term | Over-weights intra-candle noise; SL can be whipped before structural move plays out |
| **50/50** | **Balance** | **Each timeframe contributes equally; simple to compute mentally (just average); matches Floki's typical hold of minutes-to-hours where both scales matter** |
| 30/70 (H1-heavy) | Conservative, wide SL | Produces unnecessarily wide stops in quiet markets; over-insures |
| 0/100 (H1 only) | Pure medium-horizon | Too wide for scalp setups; over-sizes risk for quick trades |

**Why 50/50 is defensible under backtest:**
- Symmetric — no bias toward short- or medium-term
- Arithmetic mean is the single clearest aggregation; Floki can compute it mentally from two numbers visible on screen
- Backtest sensitivity (±20% on effect size) will tell us if the split is meaningfully wrong; we can re-tune to 40/60 or 60/40 in Phase 4 if data supports it

**Numerical illustration** (typical XAU/USD values — all in POINTS, matching what Floki sees on-screen via the `<atr value="X" unit="points"/>` data-package block):

- Quiet market: M5_ATR=3 pts, H1_ATR=15 pts → weighted = 9 pts. Spread ~3 pts. Floor = 3 + 9 = **12 pts minimum SL (= 120 pips)**.
- Normal market: M5_ATR=5 pts, H1_ATR=30 pts → weighted = 17.5 pts. Floor = 3 + 17.5 = **20.5 pts minimum SL (= 205 pips)**.
- Volatile (FOMC): M5_ATR=10 pts, H1_ATR=60 pts → weighted = 35 pts. Floor = 3 + 35 = **38 pts minimum SL (= 380 pips)**.

### Sanity check: does the formula actually shift behavior past current?

Cross-referenced against `data/_audits/flo322/trades_90d_sl_analysis.csv` (filtered to n=74 trades with non-zero agent-chosen SL, sl_pips sane range):

| Metric | Historical (90d, agent-chosen SL) | Proposed Normal-market floor |
|---|---|---|
| Median SL | 12.4 pts (= 123.9 pips) | 20.5 pts (= 205 pips) |
| p25 | 8.3 pts (= 83 pips) | 20.5 pts |
| p75 | 19.3 pts (= 193 pips) | 20.5 pts |
| Trades below 100 pips (= 10 pts) | **27 / 74 = 36.5%** | 0 (formula forbids) |
| Trades below 50 pips (= 5 pts) | **4 / 74 = 5.4%** | 0 |

Interpretation:
- The proposed Normal floor (20.5 pts) is **above the historical p75**. In other words, under typical volatility conditions, the rule would push Floki's SL wider than ~75% of his current placements.
- The formula demonstrably reaches past current behavior — it is not a no-op. Backtest effect size should be meaningful (non-zero), which is a prerequisite for the ±20% sensitivity test to produce signal.
- For the sub-0.5-ATR cohort specifically (the 27 trades with SL<100 pips), every one of them would have been widened to at least the floor. If Floki obeys the guideline.
- **If Floki does not obey the guideline** (common trader behavior when rule conflicts with structural-SL anchoring), the effect size collapses. The backtest must model both "obey" and "partial-obey" scenarios.

For reference: current `MIN_SL_PIPS = 50` in `config.py` (commit 2edd74d, April — lowered from 150). The proposed mental floor is DYNAMIC and tracks volatility; the config floor is a safety net at 5 pts. They don't conflict; they complement at different scales.

### Alternative considered: `max(M5_ATR, 0.3 × H1_ATR)`

Simpler — takes the larger of the two. But:
- Less smooth (step function)
- CTO parameters said "weighted average" not "max of weighted" — I honored that literal framing
- If the weighted-average backtest performs poorly, `max()` becomes a v2 candidate

---

## 5. Checklist vs required properties

| Requirement | Draft status |
|---|---|
| Structured mental model (not loose advisory) | ✅ 3 bullets with explicit formula |
| References H1 + M5 weighted ATR explicitly | ✅ formula `0.5 × M5_ATR + 0.5 × H1_ATR` named in the text |
| Preserves Escola 1 v2.0 (guideline, not mandate) | ✅ "Guideline, not a gate. You own the SL choice." — no "MUST" / "SHOULD" / prescriptive verbs |
| Does NOT reverse commit 2edd74d intent (floor is mental, not code) | ✅ no code change in this step; the "floor" in the text is mental (rule-of-thumb language) |
| M5 availability honest | ✅ explicitly notes M5 requires `get_indicators(timeframe='M5')` tool call |
| Escola 1 v2.0 compliance spot-check | ✅ advisor noted earlier draft satisfied this; same structure preserved |

---

## 6. Escola 1 v2.0 self-review (Rule 18 — senior-prompt-engineer)

Pattern-match against Escola 1 v2.0 markers (from `CLAUDE.md`, Luna Bug G, Rex FLO-316, FLO-317 session block removal):

- **"Inform, never prescribe":** the text states facts (what noise is, what the floor means) and hands the decision to Floki. Passes.
- **No "you MUST" / "you SHOULD":** verified. "Guideline, not a gate. You own the SL choice." is the explicit agency marker.
- **No Boolean flags / alert levels:** no categorical labels (QUIET/NORMAL/ELEVATED/CRITICAL style that FLO-316 removed). Only numerical guidelines.
- **Preserves data-first agent-first principle** (per memory `feedback_agent_first_prompts.md`): the text informs about data (spread, ATR), doesn't prescribe workflow.
- **2edd74d intent preserved:** original commit widened Floki's SL latitude by lowering `MIN_SL_PIPS`. The proposed text stays mental — no code enforcement. A Floki decision of SL=15 points is still legal; the text simply says "that's inside the noise band, think about it."

**Honest flag — draft is more prescriptive than the current advisory by design.** The current text at line 194 is purely descriptive ("The spread and ATR tell you the minimum market noise...") with no numbers. The proposed text gives a specific formula and inequality. That is deliberate: H1-b's whole premise is that pure description failed to shift behavior, so a structured mental model is needed. But the tradeoff is that we are taking one step closer to "mandate" territory than the current block sits.

Mitigations:
- Label as "rule of thumb" (standard trader language, not a rule).
- Explicit "Guideline, not a gate. You own the SL choice." disclaimer immediately after.
- No enforcement code — pure prompt text. Floki can still override with thesis-driven reasoning.
- Compared to H2 (hard floor), this remains firmly on the informational side of the Escola 1 line.

**If CTO judges this still too prescriptive for Escola 1 v2.0, H1-b is not viable and the ticket should return to design.** Do not approve if the prescriptive shift is the concern — a rewritten purely-descriptive version would not reach past current behavior (see §4 sanity check) and would fail backtest for mechanical reasons.

---

## 7. Rule checkpoints

- **Rule 11 (intent):** No intent reversed. 2edd74d's mental-floor design is preserved (still mental). Escola 1 v2.0 preserved (still informational).
- **Rule 14 (decision-logic file):** `agent_prompts.py` touches decision logic. Phase 4 commit must go through code-review skill and be classified (likely REFACTOR for S1, FEATURE for S2/S3 adding a new block).
- **Rule 15 (complete file):** insertion of ~10 lines — under the 100-line threshold. No pre-push full-file audit required.
- **Rule 16 (docs updated in same commit):** Phase 4 commit should add a one-line note to `SYSTEM_DOCUMENTATION.md` describing the SL mental-model block (under "Floki's SL authority" section or similar).
- **Rule 18:** senior-prompt-engineer consulted (this doc). Phase 4 commit runs code-reviewer skill.
- **Rule 20 (test new tools):** no new tool; no Rule 20 trigger.

---

## 8. What Phase 3 Step 2 (backtest) needs from this decision

Pending CTO answers:
1. **Scope S1 / S2 / S3.** Backtest data filter depends on this.
2. **Weighting formula approved (50/50) OR request alternative.**
3. **Approve exact text above, verbatim, OR request edits.**

Once approved, Step 2 (backtest) proceeds with:
- Counterfactual: assume prompt change shifts Floki's SL distribution by some effect size (design doc: ±20% sensitivity).
- Apply shifted distribution to 6-month historical trades (2025-10-22 → 2026-04-22).
- Recompute P&L under new SLs, compare WR / PF / Max DD / Sharpe / % sub-0.5 ATR.
- Edge cases: FOMC days (high vol), Asian low-vol, post-news whipsaw.
- Go/no-go: PF delta ≥ +0.20 AND max-DD not worse by >10%.

**Standing by for CTO approval on §2 scope, §3a/§3b text, and §4 weighting.**

---

## 9. Dependent cleanup — M5 pre-injection (sub-ticket candidate)

Under the proposed rule, Floki must know the M5_ATR at SL-decision time. Today M5_ATR is only reachable via `get_indicators(timeframe='M5')` — a tool call. Design-doc §2 already flagged tool-adoption latency as a risk for H1-a (new tool): *"tool adoption has latency — Floki would need multiple cycles to incorporate calling `get_sl_recommendation` into his routine."* The same latency risk applies to `get_indicators(M5)` called only when placing an SL.

**Proposal:** open a sub-ticket (FLO-322-helper or a new FLO number) to **pre-inject `M5_ATR` into the Floki data package** alongside the existing H1 `<atr>` block. Zero prompt change beyond exposing the value — Floki reads it from the data package instead of tool-calling for it.

Scope:
- `agent_data_builder.py`: compute M5_ATR once per cycle, emit as e.g. `<atr_m5 value="X" unit="points"/>` (or extend the existing `<atr>` block to carry both).
- Cost: ~1-2ms extra compute; ~15 extra tokens per cycle.
- Revert: trivial.

**Not in FLO-322 H1-b scope.** Flagging because if H1-b is approved and the backtest shows weak adoption, this is the first lever to pull in a v2 before considering H1-a or H3. Cheap, bounded, removes tool-call friction.

CTO call: authorize as parallel sub-ticket OR defer to Phase 4 retrospective.
