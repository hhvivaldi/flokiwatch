# FLO-322 Phase 2 — Design Doc: Tight-SL Placement Fix Options

**Status:** DRAFT for CTO review · **No code · No deployment**
**Date:** 2026-04-22
**Author:** DEV (Claude-Opus 4.7)
**References:** FLO-322 Phase 1 investigation report · `data/_audits/flo322/trades_90d_sl_analysis.csv`

---

## 1. Problem Restatement

### 1.1 Evidence

- **Systemic frequency:** 23 of 63 trades (36.5%) in the conditions-tracked window had SL < 0.5 × H1-ATR.
- **WR penalty:** Sub-0.5 ATR bucket → 25-26% WR; 0.5-1.5 ATR bucket → 55-71% WR. **~3× win-rate gap.**
- **Stop-out rate penalty:** Sub-0.5 ATR → 57-75% stopout; 1.0-1.5 ATR → 34-50% stopout.
- **MFE round-trip fails:** 6 trades (90d) had MFE > 20 pips but closed at a loss — including Trade #2 (MFE 107 pips → −$3.80).
- **Temporal:** Pattern is stable across 7d (n=33) and 8-30d (n=30) windows. Not a recency artifact.

### 1.2 Current architecture (Rule 11 intent context)

- **Floki has full SL authority.** His decision JSON carries `trade_plan.stop_loss` with no schema constraint.
- **`agent_tools.execute_trade(sl, tp)`** passes values directly to MT5; the only derivation is `sl_pips = |entry − sl| / 0.1` fed to `calculate_position_size` for lot sizing.
- **`risk_manager.calculate_sl_tp()`** (the ATR-based 50-800-pip clamp) is **dead code on Floki's path** — only invoked in Phase-0 dead paths, dry-run tools, and tests.
- **Prompt guidance is advisory** (`agent_prompts.py` line ~194): *"The spread and ATR tell you the minimum market noise. A stop loss that doesn't cover at least spread + typical candle range will be hit by random movement…"*
- **This is intentional Escola 1 v2.0 design**, committed 2026-03-12 (`2edd74d`). The commit message itself reads *"lower MIN_SL safety net"*: Hermano deliberately widened agent latitude on SL choice.

### 1.3 Why a fix is warranted despite intentional design

- The informational guidance has **failed to change behavior** over the 40+ day window since `2edd74d` shipped.
- The WR penalty is **economically material**, not theoretical.
- The root design intent — *"give Floki authority to choose SL"* — is not being violated by corrective work as long as the fix **preserves agency** (Floki still decides) and adds either richer information or an economic cost to the pattern, rather than a hard block.

### 1.4 What must be preserved

| Principle | Load-bearing commit/file |
|---|---|
| Escola 1 v2.0: code informs, never prescribes | `2edd74d` (Mar 12 2026) |
| Floki is sole decisor (FLO-85+, CLAUDE.md) | Throughout `ai_agent.py`, `agent_tools.py` |
| No hard floor on Floki's SL value | `ai_agent.py:451` JSON schema, `agent_tools.py:2244` execute_trade |
| Observational-only agent ecosystem (Luna Bug G, Rex FLO-316) | `luna_analyst.py`, `rex_monitor.py`, FLO-317 |

---

## 2. Three Hypotheses — Full Exploration

### H1 — Structural-SL Cognition (information-layer fix)

**Premise:** Floki's SL selection is anchored to the *nearest structural level* (S/R zone, flip zone, confluence). His reasoning consistently cites phrases like *"SL above 4740 resistance"*, *"SL below 4704 support"*. The ATR context added in `2edd74d` lives in the SYSTEM prompt but is not surfaced *at the moment of SL selection*, nor is it attached to the specific structural level being chosen.

**Mechanism options:**

- **H1-a (new tool):** `get_sl_recommendation(proposed_sl: float, direction: str) -> dict`
  Returns: `{structural_sl, spread_pips, atr_m5_pips, atr_h1_pips, min_safe_sl, risk_zone}`. Floki calls it before `execute_trade` when he has a candidate SL. Pure tool — cannot block; cannot change outcome without Floki's next-turn reaction.
  
- **H1-b (prompt re-framing):** Re-word the SL guidance in `agent_prompts.py` from advisory to a structured mental model. Example: replace the single sentence with a short list — *"Rule of thumb for XAU: SL ≥ max(spread + 1.0 × M5_ATR, structural_SL + 0.3 × M5_ATR buffer). Tighter than this puts you inside market noise."*
  
- **H1-c (surface in trade_feedback):** Inject a one-line observational into the post-trade reflexion block: *"Last trade SL/ATR ratio was 0.32 — historical cohort had 25% WR at that ratio vs 65% at 0.5-1.5."* Floki sees it next cycle.

**Preserves:** Full Escola 1 agency. Floki retains final SL choice.

**Risks:**
- Current advisory guidance has already failed over 40+ days. Adding more advisory text may not shift behavior.
- Tool-based (H1-a) may be ignored if Floki doesn't adopt the habit of calling it.
- H1-b prompt re-framing risks shading into prescriptive territory if the language is strong enough to actually change behavior.

**Expected impact (estimated):**
- H1-a alone: 10-20% reduction in sub-0.5 ATR rate (conservative — tool adoption is a prerequisite).
- H1-b alone: 20-40% reduction (prompt changes have historically shifted behavior in this codebase).
- H1-c alone: 15-25% reduction (post-trade feedback loops tend to stick with Floki — evidenced by Lesson #2 self-citation in Trade #3 managed exit).

### H2 — Hard Enforcement Layer (code-level floor)

**Premise:** Advisory guidance has failed empirically. Only code-level enforcement prevents the pattern with certainty.

**Mechanism options:**

- **H2-a (re-enable risk_manager clamp):** Re-wire `risk_manager.calculate_sl_tp()` into the `execute_trade` path; reject trades with `sl_pips < MIN_SL_PIPS` (raise floor from 50 to ~0.5 × H1-ATR ≈ 130-150 pips dynamic).
  
- **H2-b (execute_trade wrapper):** Add a `min_sl_check` gate in `agent_tools.execute_trade` that returns `{success: False, reason: "sl_too_tight (X pips < 0.5 ATR floor of Y pips)"}` if ratio < 0.5.

**Preserves:** Nothing of Escola 1 on the SL dimension. Floki's SL becomes conditional on code approval.

**Risks:**
- **Rule 11 RED FLAG:** Directly reverses intentional design of commit `2edd74d`. Without new information invalidating Hermano's original rationale, reversing intent is forbidden under Rule 11.
- Creates new failure mode: valid tight-SL setups (e.g., extreme-volatility M1 scalps where 30-pip SL is legitimate) become impossible.
- Breaks the precedent established by Bug G / FLO-316 / FLO-317 of *unwinding* hard-coded caution vectors. Re-introducing one contradicts the broader FLO-314 umbrella direction.
- No override mechanism means Hermano loses the ability to run experiments on specific setups.

**CTO pre-stated position:** REJECTED as primary option. Documented here for completeness and as a strawman for contrast.

### H3 — Position Sizing Penalty (economic interaction fix)

**Premise:** The problem is not purely SL placement — it's `risk_per_trade × tight_SL = large_lot_size × tight_stop`, which maximizes the dollar loss when (historically frequent) the tight SL fires. A trade at 0.21 ATR with 2% account risk has ~3× the lot size of a trade at 0.63 ATR, but the tighter SL fires 2-3× more often. Result: aggregate loss.

**Mechanism:** Tier `RISK_PER_TRADE` by the SL/ATR ratio at entry:

| SL/ATR | Effective risk % | Position-size effect |
|---|---|---|
| < 0.25 | 0.25% (−87%) | ~1/8 of nominal lot |
| 0.25 — 0.5 | 0.5% (−75%) | ~1/4 of nominal lot |
| 0.5 — 1.5 | 2.0% (nominal) | Standard |
| 1.5 — 3.0 | 2.0% (nominal) | Standard (no penalty on wide SL) |
| ≥ 3.0 | 1.0% (−50%) | Cap large-SL over-sizing too |

Implementation site: `agent_tools.execute_trade` — after `sl_pips = ...`, compute `sl_over_atr` using most-recent M5 or H1 ATR from the cached data package, then pass the tiered `risk_pct` into `risk_manager.calculate_position_size`.

**Preserves:** Floki's SL authority. He still chooses SL. The system *sizes* his conviction — narrower SL = smaller bet, because historical data says narrow SLs are worse bets.

**Risks:**
- Doesn't fix the *frequency* of losses — only the magnitude. Sub-0.5 ATR trades still stop out 57-75% of the time, just at smaller $ loss each.
- Requires surfacing the effective risk % in the execute_trade response so Floki can see it; without transparency, he may be confused about position sizing.
- Introduces a new config dimension (tier table) that needs maintenance / tuning.
- Edge case: Floki's RR calculations (RR ≈ tp_pips / sl_pips) stay the same, but **$-denominated RR** changes due to smaller lot → could confuse his setup-quality assessment.

**Expected impact:**
- 60-70% reduction in aggregate loss from sub-0.5 ATR bucket (multiplicative: smaller lot × same stopout rate = smaller $ loss).
- Zero impact on loss frequency.
- Some offsetting win reduction on the 25% of sub-0.5 ATR trades that DO win — smaller wins.
- Net effect likely positive because loss/win ratio is asymmetric in that bucket.

---

## 3. Combo Analysis: H1 + H3

### Rationale

H1 informs the decision; H3 sizes the execution. They operate on different layers of the pipeline:

```
Floki LLM
   │
   │  SL choice (cognition)  ←  H1 acts here (new info)
   ▼
agent_tools.execute_trade
   │
   │  sl_pips → risk %   ←  H3 acts here (economic penalty)
   ▼
risk_manager.calculate_position_size
   │
   ▼
executor.execute_trade
```

### Why H1 + H3 is the best alignment with Escola 1

- **H1 alone** risks being ignored like current advisory (evidence: 40+ days of no behavior change).
- **H3 alone** treats the symptom ($ loss size) without attempting to improve decision quality.
- **H1 + H3 together** — Floki sees better info *at decision time* (H1) AND the system economically penalizes tight SLs via lot sizing (H3). The combination creates both a cognitive-path improvement (learned over cycles) and an immediate damage-control layer (automatic).
- Neither component is prescriptive. Floki can still place any SL. If he chooses a tight SL despite H1's info, H3 reduces the blast radius.

### Complexity

- H1-a (new tool) + H3 (sizing tier) = 2 surfaces changed, both in `agent_tools.py`, ~80-120 LoC, feature-flaggable.
- H1-b (prompt edit) + H3 = ~40-60 LoC (prompt edit is trivial; H3 is the bulk).
- Recommended: **H1-b + H1-c + H3** — prompt edit (cheap) + trade_feedback surfacing (cheap) + sizing tier (moderate). Skip H1-a for v1 to minimize moving parts.

### Risks of combo

- Two variables changing simultaneously makes backtest attribution harder (which piece caused which effect?). Mitigation: backtest components independently then together.
- H3 sizing penalty could mask H1's cognitive impact (smaller lots → smaller signal in the noise). Mitigation: track SL/ATR ratio distribution over time as primary behavioral metric, P&L as secondary.
- Combo increases surface area for bugs. Mitigation: two commits (H1 first, stabilize, then H3).

---

## 4. Backtest Plan (Phase 3 — after design approval)

### Data

- **6-month historical window** (2025-10-22 → 2026-04-22).
- Source: `data/history.db` `trades` table (close_time as anchor) joined with `data/trade_conditions/*.json` for atr_h1 where available, plus reconstructed M5 ATR from `data/history_*.db` or MT5 historical candle replay.
- Expected usable rows: ~400-500 trades (current 90d = 93; extending 6mo multiplies).
- Filter out `sl=0` and pending-order fills lacking entry conditions.

### Simulation approach

- **Counterfactual per trade:** For each historical trade, recompute what would have happened under each proposed mechanism:
  - **H1-b/c simulation**: assume a percentage shift in Floki's SL choice distribution based on prior prompt-change observed effect sizes. Sensitivity: ±20%.
  - **H3 simulation**: re-price the historical P&L assuming tiered risk %. Formula: `sim_profit = historical_profit × (new_risk_pct / 2.0)` for trades that closed via SL or managed exit (P&L scales linearly with lot size). Winners on TP/trailing: same formula.
  - **Combo simulation**: apply both.
- **Null model:** Baseline = historical untouched.

### Metrics

| Metric | Definition |
|---|---|
| WR | wins / total |
| PF (Profit Factor) | gross_wins / gross_losses |
| Max DD | largest peak-to-trough equity drop |
| Sharpe (daily) | mean daily return / stdev daily return |
| Avg P&L | sum(profit) / n |
| Avg lot size | mean(lot) — captures H3 behavior |
| Total risk exposure | sum(sl_pips × lot × pip_value) — pre-stop-out risk taken |

### Edge-case windows

- **FOMC days** (high volatility, wide ATR) — H3 should NOT starve the bot of size here.
- **Low-volatility Asian sessions** (narrow ATR) — H3 might over-shrink. Need to check ratio normalization holds.
- **Post-news whipsaw** (ATR computed pre-news, actual vol post-news diverges) — H3 may misprice.

### Go/No-go thresholds

| Condition | Threshold |
|---|---|
| Required for approval | PF delta ≥ +0.20 AND max-DD not worse by > 10% |
| Preferred | WR delta ≥ +5 pp (percentage points) |
| Red-flag (back to drawing board) | PF worse OR max-DD > 15% worse than baseline |

### Duration

- Backtest runtime: ~2-4h compute (simulation is lightweight — row-by-row arithmetic, no live-market simulation required).
- Report + analysis: +4-6h.
- Total Phase 3: ~8-10h.

---

## 5. Implementation Complexity (Phase 4 — after backtest approval)

### Per-option complexity

| Option | Files touched | LoC delta | Rule 14 class | Feature flag? | Rollback |
|---|---|---|---|---|---|
| **H1-b** (prompt edit) | `agent_prompts.py` | ~10-15 | REFACTOR/config | No — trivial revert | Single-file revert |
| **H1-c** (trade_feedback injection) | `agent_data_builder.py` | ~30-50 | FEATURE | Yes (config flag recommended) | Flag off |
| **H1-a** (new tool) | `agent_tools.py`, `ai_agent.py` (schema) | ~80-120 | FEATURE | Yes (tool registration flag) | Unregister tool |
| **H3** (sizing tier) | `agent_tools.py`, `config.py`, `risk_manager.py` | ~60-100 | FEATURE | **Yes (critical — this alters sizing)** | Flag off → revert to fixed 2% |
| **H2-a/b** (hard floor) | `agent_tools.py`, `risk_manager.py`, `config.py` | ~40-80 | REFACTOR (reverses `2edd74d`) | Required | Flag off |

### Rule checkpoints per option

- **Rule 11:** H2 fails (reverses intent). H1, H3 pass (add to intent, don't reverse).
- **Rule 14:** H1-b = config-like; H1-a/c, H3 = new capability. H2 = reversal-class refactor.
- **Rule 15:** All options >40 LoC need full-file pre-push audit. H3 requires it (alters sizing path).
- **Rule 18:** senior-architect + senior-backend + senior-ml-engineer (for backtest) — already acknowledged.
- **Rule 20:** Test coverage required per option:
  - H1-b: 2 tests (prompt text diff + render simulation verifying old vs new wording not regressed in tests)
  - H1-c: 4-5 tests (trade_feedback payload shape, edge cases: no prior trade, no ATR available)
  - H1-a: 6-8 tests (tool registered, returns expected keys, error cases, edge ratios)
  - H3: 10-15 tests (tier boundaries, edge cases at boundaries, ATR fallback when cache missing, lot-size math, configurability)

### Rollout plan (recommended: H1-b + H1-c + H3)

- **Commit 1 (H1-b prompt edit):** Ship alone. Observe 5 days. No flag needed; easy revert.
- **Commit 2 (H1-c trade_feedback line):** Ship with feature flag `SURFACE_SL_ATR_FEEDBACK`. Default ON. 5-day observation.
- **Commit 3 (H3 sizing tier):** Ship with feature flag `SL_ATR_SIZING_TIER_ENABLED`. Default OFF for 48h canary. Turn ON after verification.
- **Total: 3 commits over ~10-14 days.** Each independently revertable.

### Rollback plan (all options)

- **H1-b:** `git revert <commit>` — trivial.
- **H1-c:** Flip `SURFACE_SL_ATR_FEEDBACK=false` in `.env` → restart bot.
- **H3:** Flip `SL_ATR_SIZING_TIER_ENABLED=false` → restart. All new sizing reverts to fixed 2%.
- **Trigger for rollback:** any of — WR drops vs baseline over 5-day rolling window; max DD > 15% worse; Floki data_needs increase in `tool_errors` or `obstacle` citing sizing confusion.

---

## 6. DEV Recommendation

### Primary recommendation: **H1-b + H1-c + H3 (staged rollout)**

### Reasoning

1. **Philosophy alignment:** Of the three hypotheses, only H1 + H3 preserve Escola 1 v2.0. H2 directly reverses `2edd74d` and fails Rule 11. Rejecting H2 is already CTO-stated.
2. **Empirical weight on advisory-only:** H1-b alone has a ~40-day counterexample showing informational guidance can be insufficient to change behavior in isolation. H1-c adds a post-trade feedback loop proven effective (Lesson #2 self-citation in Trade #3 managed exit — Floki *does* internalize surfaced lessons). H3 adds economic teeth without removing agency.
3. **Risk asymmetry:** H3 alone treats magnitude not frequency, meaning the bot would still stop out as often but lose less each time. Not a full fix. H1+H3 attacks both dimensions.
4. **Reversibility:** Staged rollout with feature flags on each piece minimizes blast radius. Each piece can be rolled back independently.
5. **Evidence-grading:**
   - Historical WR ~3× penalty → strong evidence for intervention
   - MFE capture 65% → moderate evidence (some giveback, not critical — CTO already downgraded FLO-323 urgency)
   - Recent 7d tightening marginal → weak evidence for urgent action, strong evidence for gradual staged fix
6. **Testability:** Each component has independent backtest signal. H1-b easiest to measure (prompt distribution shift). H3 cleanest economic math. H1-c moderate.

### Caveats

- **Recommended v1 EXCLUDES H1-a (new tool).** Rationale: tool adoption has latency — Floki would need multiple cycles to incorporate calling `get_sl_recommendation` into his routine. H1-b + H1-c touch Floki's passive input stream, which updates immediately on next cycle. H1-a is a candidate for v2 after backtest signal is established.
- **H3 tier thresholds (0.25 / 0.5 / 1.5 / 3.0 ATR)** are drawn from Phase 1 bucket analysis. These need revalidation in the 6-month backtest — the 90-day sample may over-represent recent market regimes.
- **If backtest for H3 shows no improvement or DD regression**, fallback is H1-b + H1-c only, accepting a smaller effect size for a cleaner philosophical fit.

### What DEV does NOT recommend

- **H2 (hard floor):** Rule 11 violation. Would contradict Bug G / FLO-316 / FLO-317 precedent. CTO already has this position.
- **Shipping all 3 in a single commit:** Blast radius, attribution difficulty, and rollback complexity all increase.
- **H1-a alone without H3:** Tool-adoption latency + 40-day evidence against advisory-only = low expected effect size.

### Decision gate for CTO

| Go/no-go signal | Threshold | If met |
|---|---|---|
| Approve H1-b + H1-c + H3 combo with staged rollout | CTO accepts philosophy alignment + staging plan | Proceed to Phase 3 backtest |
| Approve H1-b + H1-c only (skip H3) | CTO prefers information-layer-only | Backtest H1-b + H1-c combo only |
| Request alternative hypothesis | H4/H5 needed | Return to Phase 1 investigation |
| Reject all and close FLO-322 | Observed pattern deemed acceptable given Escola 1 priority | Close ticket, document as WONTFIX |

---

## 7. Open Questions for CTO

1. Is the 2% risk-per-trade a hard constraint or tunable? H3 tier table is sensitive to this.
2. Should M5 ATR or H1 ATR be the denominator for SL/ATR ratio? Phase 1 used H1 (available). M5 is more responsive but requires fresh data point retrieval at execution.
3. If H3 reduces lot size, does that interact with the `MAX_LOT_SIZE` / `MIN_LOT_SIZE` config limits in unanticipated ways? (Phase 3 backtest will validate.)
4. Should Hermano retain a per-trade override (boss-note or config) for explicit tight-SL experiments? If yes, what's the interface?
5. Priority vs FLO-323 (MFE capture) + FLO-324 (round-trip failures) — do we sequence these or batch?

---

## 8. Deliverables Summary

- This design doc (`data/_audits/flo322/FLO-322_Phase2_DesignDoc.md`)
- Phase 1 artifacts referenced (`data/_audits/flo322/`)
- No code changes
- No deployment
- 72h FLO-315 observation window untouched

**Standing by for CTO review.**
