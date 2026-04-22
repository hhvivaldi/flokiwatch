# FLO-322 Phase 3 — Backtest Report

**Status:** DRAFT for CTO + Hermano review · **No code · No deployment**
**Date:** 2026-04-22
**Author:** DEV (Claude-Opus 4.7)
**Scope:** H1-b counterfactual backtest for the `<sl_placement_mental_model>` block approved in Phase 3 Step 1.

**Artifacts:**
- Backtest script: `scripts/_investigations/flo322_backtest.py`
- Results JSON: `data/_audits/flo322/FLO-322_backtest_results.json`
- Prompt proposal (locked): `data/_audits/flo322/FLO-322_Phase3_Step1_PromptProposal.md`

---

## HEADLINE — honest verdict

**Backtest is INCONCLUSIVE at available data depth.** Direction of effect is positive on point estimates. Magnitude is small (~$34-$36 total P&L improvement over 30 days). Statistical confidence interval at N=43 INCLUDES "no effect". Only **4 trades out of 43** are counterfactually eligible (see §3 for why) — the claim of "36.5% of trades affected" from Phase 1 was measuring a different thing.

**DEV recommendation: ship on judgment, not on backtest signal.** The edit is zero-code-risk (prompt text only, trivially revertable) and directionally-positive even under the most conservative assumption. But the CTO should understand the backtest did NOT produce the "PF delta ≥ +0.20 with confidence" clearance the Phase 2 design doc imagined — it produced a point estimate meeting the threshold (+0.43 to +0.45) with a 95% CI [−0.65, +3.05] that includes zero.

**Key corrections applied during this Phase 3** (documented for transparency):
1. Design doc's 6-month window was infeasible — real available data is 30 days / N=43.
2. First-pass backtest had a methodological error: counted trailing-SL-in-profit stopouts as "rescueable by wider SL". They are not — H1-b affects only the ORIGINAL SL at open; trailing SLs fire regardless. Corrected. New N of eligible trades: 4.
3. MAE units are PIPS (not points) per `mfe_backfill.py:156`. Corrected.
4. Survivor model now uses M5+H1 ATR correctly.

---

## 1. Data window — reconciled with reality

| Source | Range | Usable rows |
|---|---|---|
| Design doc asked | 2025-10-22 → 2026-04-22 (6mo, 400-500 trades) | **infeasible** |
| `history.db` | 2026-04-07 → 2026-04-22 | 97 |
| `trade_conditions/*.json` | 2026-03-24 → 2026-04-22 | 85 |
| **Intersection** (sl>0 ∧ closed ∧ MFE/MAE ∧ atr_h1) | 2026-03-24 → 2026-04-22 (30 days) | **43** |
| **After eligibility filter** (see §3) | 4 |

**Bar-by-bar replay NOT performed.** MAE bounds were used as a proxy for "would wider SL have survived" — this is the dominant limit on signal strength.

---

## 2. Baseline

| Metric | Value |
|---|---|
| n | 43 |
| Trading days | 8 |
| Wins | 16 (WR 37.21%) |
| Losses | 20 |
| **Profit Factor** | **1.389** (95% CI [0.55, 3.07]) |
| Total P&L | +$56.33 |
| Max DD | $47.24 |
| Sharpe (daily) | 0.251 |
| Sub-0.5 × H1_ATR cohort | 16/43 = 37.2% |

Marginally-profitable micro-lot scalp account over 8 trading days. Matches Phase 1 findings.

---

## 3. CRITICAL — eligibility correction (first-pass bug, fixed)

### First-pass error
The initial backtest counted EVERY trade with `close_reason == "Stop Loss"` as a potential rescue candidate. Running that gave PF estimates of 2.0-12.2 across weightings — implausibly large.

### Root cause
H1-b widens the **ORIGINAL SL** set at trade open. But many (most) "Stop Loss" closes in the data are **trailing-SL exits**: Floki's position management tightens the SL as price moves in favor; when price retraces and hits that tightened SL, the broker reports close_reason = "Stop Loss" but the trigger was the TRAILING SL, not the original SL.

Verification: compare `sl` (original at open) vs `final_sl` (SL at close) in `history.db`:

| Pattern | Count (of 29 "Stop Loss" closes with MFE/MAE populated) | H1-b effect |
|---|---|---|
| `abs(sl − final_sl) < 0.5 pts` AND `profit < −0.5` — **genuine original-SL hit on loss** | **4** | Potentially rescueable |
| `abs(sl − final_sl) < 0.5 pts` AND `profit ≥ −0.5` — original SL set at/near entry, closed flat or small profit | 1 | No effect |
| `abs(sl − final_sl) ≥ 0.5 pts` AND `profit > +0.5` — **trailing-SL in profit zone** | **8** | No effect — trade was already winning |
| `abs(sl − final_sl) ≥ 0.5 pts` AND `−0.5 ≤ profit ≤ +0.5` — **BE-stop** | **8** | No effect — BE fired before original SL could |
| `abs(sl − final_sl) ≥ 0.5 pts` AND `profit < −0.5` — **trailing loss stop** (trailed tighter, small loss) | **8** | No effect — trailed SL fires first |

**Of 43 trades, only 4 are counterfactually eligible for H1-b to have any effect at all.** The other 39 would see identical outcomes whether original SL was at actual value or widened to floor — trailing/BE management takes over before the original SL can matter.

### Four eligible trades

From `data/_audits/flo322/FLO-322_backtest_results.json` diagnostics:

| Trade (approx identification) | sl_dist_pts | profit | MAE pts | atr_h1 pts |
|---|---|---|---|---|
| 1588562779 SELL | 9.99 | −$2.04 | 3.43 | (~18) |
| 1590450271 SELL | 6.70 | −$13.48 | 5.28 | (~18) |
| 1603833049 SELL | 4.46 | −$9.06 | 0.44 | (~17) |
| 1605124275 SELL | 6.32 | −$12.96 | 1.70 | (~16) |

All 4 are SELL. All stopped at original SL. MAE < sl_dist in some — likely an artifact of MAE backfill granularity (M1 candles miss the exact SL-hit tick). Treat as: trade actually reached original SL distance, MAE backfill underestimated.

---

## 4. Results at eligible N=4

### Per-weighting summary (adoption = obedience, i.e., SL = floor exactly)

| Weighting | Widened (of 43) | Eligible hits | Rescued | Still stops wider | PF (breakeven) | PF (small_win) | PF (mid_mfe) |
|---|---|---|---|---|---|---|---|
| 0.0 H1-only | 39 | 4 | **4** | 0 | 1.82 [0.73, 4.40] | 1.83 [0.74, 4.42] | 1.84 [0.76, 4.44] |
| 0.3 stability | 35 | 4 | 4 | 0 | 1.82 [0.73, 4.40] | 1.83 | 1.84 |
| 0.5 balanced | 31 | 4 | 4 | 0 | 1.82 | 1.83 | 1.84 |
| 0.7 reactive | 23 | 4 | 4 | 0 | 1.82 | 1.83 | 1.84 |
| 1.0 M5-only | 13 | 3 | 3 | 0 | 1.62 [0.66, 3.83] | 1.63 | 1.64 |
| **baseline** | — | — | — | — | **1.39 [0.55, 3.07]** | — | — |

### Observations

1. **Weightings 0.0–0.7 converge on identical PF.** All 4 eligible trades get rescued under any of these weightings because their actual sl_dist (4.46-9.99 pts) is comfortably below every proposed floor (~15-21 pts). Weighting choice doesn't differentiate in this sample.

2. **M5-only (w=1.0) loses 1 rescue.** With floor = spread + M5_ATR ≈ 3 + 4.7 ≈ 7.7 pts, one eligible trade (sl_dist 6.32) is ABOVE this floor for some M5 values and doesn't get rescued. PF delta smaller.

3. **Survivor-model sensitivity is narrow** (PF 1.82-1.84 across all three). Because rescued trades had small MFEs (most of the rescues were trades that would have turned breakeven, not big winners). The "mid_mfe turns loser into big winner" dynamic from the buggy first pass does NOT happen at the eligible set.

4. **Zero trades still stopped at wider SL** — but now that's a statement about only 4 eligible trades with small MAE values. Not the broad claim the first pass implied.

5. **Total P&L delta: +$33 to +$36 over 30 days.** Roughly $1 per trade averaged across the sample (or ~$8 per eligible rescue).

### 95% CI view

Baseline PF 95% CI: [0.55, 3.07].
Best H1-b weighting (w=0.0 through 0.7, mid_mfe): PF 95% CI: [0.76, 4.44].

**Lower CI bound of H1-b (0.76) is BELOW baseline point estimate (1.39).** At N=43 we cannot reject "no effect" even under optimistic assumptions. The point-estimate improvement is real but the sample is too small for statistical certainty.

---

## 5. Go/no-go verdict per threshold

CTO-locked thresholds (Phase 2 design doc):
- **Required:** PF delta ≥ +0.20 AND max-DD not worse by > 10%
- **Preferred:** WR delta ≥ +5 pp

Applied (best weighting, obedience adoption, all 3 survivor models):

| Threshold | Point estimate | Lower CI bound | Verdict |
|---|---|---|---|
| PF delta ≥ +0.20 | +0.43 to +0.45 (✓) | −0.64 (✗ crosses zero) | **Point passes, CI inconclusive** |
| Max DD delta ≤ +10% | −$11.54 (improves by 24%) | — | **PASS** |
| WR delta ≥ +5 pp (preferred) | −2.3 to +2.3 pp depending on survivor model | — | **FAILS preferred** |

### Honest read

Point-estimates on required thresholds pass. CIs at N=43 preclude statistical certainty.

The Phase 2 design doc's implicit framing — "run a 6-month backtest, get clear signal, ship if signal passes threshold" — cannot be executed at available data depth. This is the #1 Phase 3 finding.

---

## 6. Edge-case subsets

**Skipped as formal analysis.** At eligible N=4, splitting into sub-buckets (FOMC, low-vol, Asian session) gives sub-N of 0-2. No meaningful statistics possible. Raw profile: all 4 eligible trades are SELLs, all during LONDON/NY session per trade_conditions, not FOMC-dated. High-vol, low-vol, and post-news edge cases are not represented in the eligible cohort.

---

## 7. Sensitivity to prompt-shift adoption rate

Design doc specified ±20% prompt-shift sensitivity. Implemented as adoption rate: obedience (SL exactly to floor) vs partial-50 (SL midway between actual and floor).

At N=4 eligible, both adoption models rescue the same 4 trades (because the midpoint is still comfortably above MAE). Adoption-rate sensitivity is effectively zero in this sample. Direction-of-effect insensitive to adoption assumption.

---

## 8. Weighting recommendation (conditional on GO)

If CTO + Hermano approve GO despite the inconclusive statistical signal, the recommended weighting is:

### **w_m5 = 0.0 (H1-only)** — same as first-pass recommendation

Rationale:
- Backtest data doesn't differentiate between w = 0.0 and w = 0.7 (all 4 trades rescued under each). So the decision defaults to qualitative factors.
- **Zero tool-adoption latency:** H1_ATR is already pre-injected in Floki's data package. He never needs to call `get_indicators(timeframe='M5')`. This was the #2 adoption risk flagged in Phase 2.
- **Simplest mental model:** `floor = spread + H1_ATR`. One number + one small constant. Easy to internalize.
- **Widest effect surface** (91% of all trades would be widened, vs 81% at w=0.3, 71% at w=0.5, 52% at w=0.7). For trades that aren't eligible-to-rescue, the widening has no effect (no downside) — but for future trades in unseen distribution, wider cover = more potential rescue.

Second choice: **w_m5 = 0.3** — satisfies the CTO-spec "weighted average of H1 and M5" while retaining most of H1-only's advantages.

Not recommended: w_m5 = 0.5, 0.7, 1.0.

### Corresponding prompt text (if w = 0.0 approved)

```
<sl_placement_mental_model>
Noise floor (rule of thumb for XAU/USD):

- Floor: SL distance ≥ spread + 1.0 × H1_ATR.
  (H1_ATR is in your data package as <atr value=... description="Average True Range H1"/>.)
  Tighter than this puts the stop inside normal market noise — more likely hit by random
  movement than by thesis invalidation.

- Preferred placement: put the SL one H1_ATR past the nearest structural level you'd use
  to invalidate your thesis, not on it. A stop at the level gets swept by wick; a stop
  beyond it requires a real break.

Guideline, not a gate. You own the SL choice. The number just tells you when you're
inside the noise band.

(For finer-scale setups, get_indicators(timeframe='M5') gives M5_ATR; use it if your
thesis is M5-horizon and H1_ATR feels over-wide.)
</sl_placement_mental_model>
```

Insertion site: between `</decisions>` (line 106) and `<output>` (line 108) of `agent_prompts.py`.

---

## 9. DEV recommendation

### Ship H1-b (w_m5=0.0) on judgment, not on backtest signal

Reasons:
- **Zero code risk.** Prompt text only. Trivial revert via `git revert <sha>`.
- **Directional signal positive** across every assumption tested.
- **Magnitude is modest but nonzero** (+$1 per trade avg; ~$34/month at current pace).
- **Observational gain is valuable** regardless of effect size — 5-day live observation will tell us if Floki actually changes SL distribution under the new block, which is the H1-b question that the backtest fundamentally can't answer.
- **Backtest will never be conclusive at current data depth.** Demanding it become so means either (a) wait 3+ months for data accumulation, or (b) reconstruct bar-by-bar price action to pin survival precisely — both disproportionate for a 20-LoC prompt edit.

### Alternative: NO-GO, wait

Only justified if CTO's policy is "code/text changes ship only on statistically-significant backtest evidence". If that's the rule here, FLO-322 should close or be deferred pending 3+ more months of data.

### NOT recommended: reconstruct bar-by-bar data

Would take 3-5h, tripling compute and storage, to tighten the CI on a 4-trade eligible cohort. ROI negative for a zero-risk prompt edit.

---

## 10. What Phase 4 (if GO) should do

1. Single commit: insert block §8 above into `agent_prompts.py` at line 107.
2. Rule 14 classification: **REFACTOR** (text-only, no logic).
3. Era management per CLAUDE.md: **APPEND** new SHA to `LESSONS_CURRENT_ERA_SHAS` (prompt edit is additive context, not era reset).
4. Update `SYSTEM_DOCUMENTATION.md` with a short "SL mental model" note.
5. Observe 5 days (Caminho B gate). Measure:
   - Fraction of OPEN trades with SL distance ≥ spread + H1_ATR (baseline: 61.9%, target: ≥ 80%).
   - Fraction of trades in sub-0.5-H1_ATR cohort (baseline: 37.2%, target: < 20%).
   - Dollar P&L over 5-day observation window vs 5-day-prior baseline.
6. Rollback trigger: WR drops > 10 pp vs baseline, OR Floki data_needs cites confusion about the new block.

---

## 11. Phase 3 limitations acknowledged

| Limitation | Impact |
|---|---|
| Data window 30 days, not 6 months | CIs wide |
| Eligibility n=4 after correct filtering | Effect-size inference unreliable |
| MAE from M1 backfill misses exact SL-hit tick | "Rescued" classification may over-include |
| No bar-by-bar replay of post-original-SL price action | Cannot verify "would wider SL have been hit later" |
| Spread fixed at 3.0 pts | Reality varies 2-8 pts; small effect on floor |
| No lot-size re-sizing under wider SL | Per CTO-locked H1-b scope |
| Model B (partial-50) and Model A (obedience) converge at N=4 | Adoption-rate sensitivity not measurable |

**Direction of effect is suggestive-positive, not robust.** At N_eligible=4, any one of {MAE-underestimation, post-SL price continuation, wrong survivor-model assumption} could plausibly flip the +$34 observed improvement into −$5 or zero. Magnitude is likewise fragile — modest uplift on 4 small losses is the full finding; no structural evidence for a large effect. The live 5-day observation is a better signal than attempting to tighten this dataset.

---

## 12. Summary table

| Item | Status |
|---|---|
| Backtest script written + ran | ✅ |
| 6-month window attempted | ❌ (infeasible) |
| 30-day window achieved | ✅ (N=43) |
| Eligibility filter applied | ✅ (N_eligible = 4) |
| 5-weighting grid tested | ✅ |
| Survivor-model sensitivity | ✅ (breakeven / small_win / mid_mfe) |
| Adoption-model sensitivity | ✅ (obedience / partial_50) |
| Bootstrap 95% CIs | ✅ |
| Edge-case analysis | ⚠️ skipped due to N |
| Go/no-go thresholds | ⚠️ point estimate passes required, CI inconclusive, preferred fails |
| DEV recommendation | GO on judgment, ship w_m5=0.0 H1-only |

**Standing by for CTO + Hermano decision.**
