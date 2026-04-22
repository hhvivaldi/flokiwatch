# FLO-334 Phase 1 — `get_trade_lessons` silent-failure investigation (READ-ONLY)

**Status:** Phase 1 complete. Root cause confirmed. Fix options + detection design proposed.
**Generated:** 2026-04-22 UTC
**Context:** Surfaced by FLO-332 Phase 1 boundary-finding: `get_trade_lessons` returns empty 124/131 times in 7d (94.7%).
**Rule 11 posture:** Era filter was intentional design per FLO-328. Investigation confirmed intent and does not propose to remove it; proposes to simplify its implementation.

---

## HEADLINE — direct answer

The era filter works exactly as designed. **Failure mode is a process gap, not a code bug.**
The `LESSONS_CURRENT_ERA_SHAS` whitelist requires an operator to append every behavior-affecting commit SHA to the list (per `CLAUDE.md` "Trade Lessons Era Management" checklist). This checklist ran for ~2 days (Apr 16–17) during the initial FLO-327/298/299 landing, and then **stopped**. Across 71+ commits since Apr 17 (including major behavioral commits FLO-317, FLO-322, and all Bug A–G fixes), **zero new SHAs were appended**.

Result: today's trade snapshots are tagged with SHAs like `ecb9ac8` (FLO-317), `56fd4eb` (Rex reduction), and others — **none match the 6-SHA whitelist** — so every snapshot is filtered out before bucket aggregation.

Counterfactual check (simulating `get_relevant_lessons` against the full snapshot corpus):

| Filter variant | Trades qualifying | Unique buckets | Lessons (≥3 occurrences) |
|---|---:|---:|---:|
| **Current (6 whitelisted SHAs)** | **0** | **0** | **0** |
| Widened to all post-FLO-327 SHAs | 32 | 21 | 3 |

The numbers do the talking. The current filter excludes everything.

---

## 1. Rule 11 — intent reconstruction (critical check)

### 1.1 Era filter genesis (FLO-328, commit `1a89658`, 2026-04-16)

Per the commit message (verbatim from `git show 1a89658`):

> *"Follow-up to FLO-327 per Hermano's approval. Two protective filters on lesson aggregation, replacing the old persistent-aggregation model:*
>   - *age ≤ LESSONS_WINDOW_DAYS (default 30)*
>   - *era snapshot.system_version ∈ LESSONS_CURRENT_ERA_SHAS*
>
> *Reset (not append) was Hermano's call: pre-327 trades came from materially different systems — different AI model, broken capture formula, pre-planning prompt, fewer tools — and would contaminate current Qwen-era learnings."*

**Intent**: exclude pre-FLO-327 trades from bucket aggregation. The reason was *materially different system*, not *each commit creates a new era*.

**Implementation**: whitelist of SHAs that the operator appends to as behavioral commits land. `CLAUDE.md` captures this as "Trade Lessons Era Management" with an append-vs-reset checklist.

**Expected cold-start gap**: 3–5 days until first bucket reaches `min_occurrences=3` in the new era. Documented in the commit message.

### 1.2 The 6 SHAs currently in the whitelist (verified)

All 6 were curated deliberately, **not** just "the first N commits":

| SHA | Date | Description |
|---|---|---|
| `1205fd4` | 2026-04-16 | **FLO-327** — dedup guard on trade lesson extraction (era start) |
| `4f0981c` | 2026-04-17 | **FLO-298 fix 4** — data staleness awareness lesson in system prompt |
| `68fa3d3` | 2026-04-17 | **FLO-299 1/5** — main.py trigger_context autonomy cleanup |
| `6844820` | 2026-04-17 | **FLO-299 2/5** — agent_prompts.py neutralization |
| `4a271e0` | 2026-04-17 | **FLO-299 3/5** — truncation markers + suggestion removal |
| `6458e5a` | 2026-04-17 | **FLO-299 4/5** — SL auto-clamp visibility (risk_manager.py) |

Notably **missing from the list**: `e6a3a30` (FLO-299 5/5, 2026-04-17) — the closure commit that "*feat: FLO-299 commit 5/5 — era SHAs + Luna age verification*." **The commit whose own title documents appending era SHAs never got its own SHA appended.** That's the first symptom of the maintenance failure.

### 1.3 Maintenance gap (the process failure)

`git log --oneline --since="2026-04-17" --until="2026-04-22" -- config.py` shows **zero changes to `LESSONS_CURRENT_ERA_SHAS`** for 5+ days. Meanwhile **71 total commits shipped to the repo** since the last era-SHA append:

| Period | Commits to repo | SHAs added to whitelist |
|---|---:|---:|
| 2026-04-16 → 2026-04-17 | ~10 | 6 (whitelist was being maintained) |
| 2026-04-18 → 2026-04-22 | 71 | **0** |

The per-commit checklist failed the moment active attention on FLO-298/299/328 ended. The *intent* of the filter is still valid (exclude pre-327); the *mechanism* relied on prospective human discipline that didn't scale past the initial ticket focus.

**Rule 11 verdict:** do NOT remove the era concept. DO simplify the mechanism so it doesn't require per-commit maintenance.

---

## 2. Audit results

### 2.1 Call-rate audit (FLO-332 Phase 1, reconfirmed)

| Window | `get_trade_lessons` calls | Returned empty | Empty rate |
|---|---:|---:|---:|
| Today (2026-04-22) | 22 | 22 | **100%** |
| Last 7 days | 131 | 125 | **95.4%** |
| Last 7 days (POST-FLO-328 only) | 125 | 125 | **100%** |

The 6 non-empty responses all predate FLO-328 shipping — see §2.4. **Post-era-filter shipping: 0/125 non-empty.**

### 2.2 Snapshot corpus health

87 trade_conditions JSON files exist in `data/trade_conditions/`. Distribution by `system_version`:

| `system_version` | Count | Filter outcome |
|---|---:|---|
| `pre_FLO-327` (legacy tagged by backfill script) | 44 | ❌ excluded (by design) |
| `<MISSING>` (capture regression — FLO-333 terrain) | 11 | ❌ excluded (silently — no log) |
| `ecb9ac8` (FLO-317 — today's post-restart SHA) | 10 | ❌ not whitelisted |
| `dea48bd` | 3 | ❌ not whitelisted |
| `390a03a` | 3 | ❌ not whitelisted |
| `cea2447` | 3 | ❌ not whitelisted |
| `a866c7b` | 3 | ❌ not whitelisted |
| `2982d24` | 2 | ❌ not whitelisted |
| `56fd4eb` (Rex reduction) | 2 | ❌ not whitelisted |
| 6 other singleton SHAs | 6 | ❌ not whitelisted |

**Total post-FLO-327 trades NOT excluded by era filter: 0.**
**Total post-FLO-327 trades that WOULD qualify if whitelist were correct: 32.**

### 2.3 Bucket aggregation simulation

Using `get_relevant_lessons`'s actual bucket-key algorithm (`direction × RSI bucket × volume bucket × session × luna context`), applied to the 30-day corpus:

| Filter | Trades | Buckets | Lessons (≥3 occ) |
|---|---:|---:|---:|
| Current whitelist (6 SHAs) | 0 | 0 | 0 |
| Widened to all post-FLO-327 | 32 | 21 | 3 |

The 3 lessons that a widened filter would produce (illustrative — current bucket diversity is low):

- `('BUY', 'neutral_low', 'unknown', 'ASIAN', 'CAUTION'): 1W/2L  avg=-$2.54`
- `('BUY', 'neutral_low', 'unknown', 'LONDON', 'DANGER'): 1W/2L  avg=-$5.11`
- `('BUY', 'neutral_low', 'unknown', 'NY', 'dollar_gold_correlation_break'): 1W/2L  avg=+$0.40`

Modest but non-zero. Bucket diversity is low partly because `volume_h1` is captured as `None` in all recent snapshots (an adjacent capture gap, likely FLO-333 territory — flagged but out of scope).

### 2.4 The 7 non-empty responses in the 7-day window — verified

Previous 7-day audit count: 7/131 non-empty. Direct inspection finds **6 non-empty, all dated 2026-04-15 18:23 UTC through 2026-04-16 06:29 UTC — i.e. BEFORE commit `1a89658` (FLO-328) landed at 09:41 UTC on Apr 16**. Those responses came from the pre-FLO-328 implementation of `get_relevant_lessons()` which read from the persistent `trade_lessons.json` aggregation. Same 6 lessons returned in every call (identical bucket list).

**Implication:** *after* FLO-328 shipped, `get_relevant_lessons()` has returned non-empty exactly **zero** times in the remaining 5+ days. The era filter has been silently excluding every trade since the day it shipped — the cold-start gap documented in the commit message (3–5 days) never ended, because the whitelist never grew.

The "7" in the original summary was a miscount — the real number of post-FLO-328 non-empty responses is 0/131. This tightens Option 1's argument: there is no evidence the SHA-whitelist mechanism EVER worked in production as designed. The whitelist didn't "go stale" — the whitelist never caught up with the cold-start gap in the first place.

---

## 3. Root cause — summary

**Not a bug.** The code in `trade_lessons.get_relevant_lessons` is correct and implements the intended era+age filter faithfully.

**Is a design failure.** The mechanism requires prospective per-commit operator discipline that did not scale past the initial ticket. Symptoms:

1. Silent failure — `get_trade_lessons` returns `[]` indistinguishably whether (a) no trades have occurred in the new era yet, (b) all trades are in the era but no bucket has ≥3 occurrences yet, or (c) the whitelist has simply gone stale. Floki sees the same empty list in every case.
2. No operator observability — nothing surfaces "the era whitelist is stale" to dashboard, logs, or Floki.
3. No graceful degradation — the filter is binary (match/exclude), so a single missed SHA append excludes that trade forever (or until manually backfilled).

---

## 4. Phase 2 fix options

Four design options, ranked by evidence fit:

### Option 1 — Replace SHA whitelist with a time-boundary filter (RECOMMENDED)

Swap `system_version ∈ LESSONS_CURRENT_ERA_SHAS` for `system_version != "pre_FLO-327" AND system_version is not null AND system_version != ""`.

**Rationale:** this is the *literal* intent of FLO-328 per the commit message — exclude pre-327 contamination. The SHA whitelist was a conservative implementation that assumed every post-327 commit might be a behavioral inflection; in practice, most commits aren't, and the enumeration cost exceeds its value.

**Pros:**
- Zero operator maintenance (no checklist, no per-commit step).
- Preserves the original intent (exclude legacy contamination).
- `<MISSING>` snapshots (11 files) are excluded too — the filter is more robust.
- Operator can still RESET via one flag (e.g. `LESSONS_ERA_BOUNDARY = "FLO-327"`) for future major inflections.

**Cons:**
- Removes the per-commit granular control. If a specific post-327 commit ships a regression, its trades still land in the bucket.
- Gives up the "bucket this behavioral commit's lessons separately" idea that CLAUDE.md envisioned but never materialized in practice.

**CLAUDE.md rewrite needed:** Yes — the append-vs-reset checklist becomes "reset only, very rarely, for major inflections."

### Option 2 — Keep the whitelist but auto-extend it

Add a boot-time helper that auto-appends the current HEAD SHA to the whitelist if it's missing. Writes to a separate runtime file (`data/era_shas_runtime.json`), preserving config.py as the "baseline" era start.

**Pros:**
- Preserves the per-SHA granular model.
- Eliminates human-discipline requirement.
- Non-invasive to config.py semantics.

**Cons:**
- Adds complexity (runtime file, merge logic).
- Still depends on `_current_sha()` at trade-open time matching HEAD at read time.
- Mixes config-as-code with runtime state — breaks the "config.py is the source of truth" pattern.

### Option 3 — Fallback: if current whitelist yields 0 trades, widen to post-327

Try strict filter first; if `processed == 0 && skipped_era > 0`, re-run with widened filter and log a WARN.

**Pros:**
- Self-healing — filter degrades gracefully when whitelist goes stale.
- Surfaces the degradation via WARN log.

**Cons:**
- Non-deterministic behavior (depends on current snapshot state).
- Two passes over the snapshot directory when stale.
- Hides the underlying maintenance gap — operator may never notice if the fallback silently works.

### Option 4 — Remove the era filter entirely

Drop the `system_version` check; rely only on the 30-day age window.

**Pros:**
- Simplest possible implementation.
- No maintenance burden.

**Cons:**
- **Violates Rule 11.** FLO-328 commit explicitly called out pre-327 contamination as a risk. 44 pre-327 snapshots would start leaking into buckets.
- Undoes the intentional FLO-328 design decision.

**Not recommended.**

### 4.1 Recommendation

**Option 1** (time-boundary filter). Evidence fit: the FLO-328 commit language is *"pre-327 contamination"*, which is a time boundary, not an SHA whitelist. Implementation cost: ~5 LoC. Maintenance cost afterward: zero. Preserves intent, removes discipline requirement.

**Pseudocode:**

```python
# trade_lessons.py — in get_relevant_lessons
BOUNDARY_SHA = getattr(_cfg, "LESSONS_ERA_BOUNDARY", "pre_FLO-327")
# Filter:
if sv == BOUNDARY_SHA or not sv:
    skipped_era += 1
    continue
```

**Config change:**
```python
# config.py — LESSONS_CURRENT_ERA_SHAS removed (or kept as comment for historical record)
LESSONS_ERA_BOUNDARY = "pre_FLO-327"  # Snapshot system_version values matching this are excluded
```

**CLAUDE.md revision:** the "Trade Lessons Era Management" section shortens dramatically — append-vs-reset checklist is replaced by "reset the boundary only for major system-level inflections (AI-model swap, fundamental schema change). Most commits require no change."

---

## 5. Silent-failure detection design

Per ticket requirement: `>20% empty rate → WARN log`.

Simpler variant that captures the same signal without tracking a rolling rate: **log a WARN line once per process lifetime when `get_relevant_lessons` returns `[]` with `skipped_era > 0 AND processed == 0`**. That condition means "the era filter excluded everything" — the exact degraded state this investigation found.

Implementation draft (~3 LoC in `trade_lessons.py`):

```python
_LESSONS_EMPTY_WARNED = False

def get_relevant_lessons(...):
    ...
    # Existing log.debug line
    log.debug(f"LESSONS_AGG | era={era_list} ... processed={processed} skip_era={skipped_era}")
    global _LESSONS_EMPTY_WARNED
    if processed == 0 and skipped_era > 0 and not _LESSONS_EMPTY_WARNED:
        log.warning(
            f"LESSONS_ERA_FILTER_DEGRADED | "
            f"skipped_era={skipped_era} processed=0 | "
            f"era boundary may be stale — check config.LESSONS_ERA_BOUNDARY"
        )
        _LESSONS_EMPTY_WARNED = True
    ...
```

**Why once-per-process and not rolling-rate:**
- Simpler (no state-machine, no persistence).
- Actionable signal without noise.
- Operator either sees the WARN in `grep -i LESSONS_ERA_FILTER_DEGRADED logs/*` or doesn't.
- Resets naturally on bot restart — after a config fix, the next startup verifies it.

**Not building:** dashboard widgets, counts-per-hour metrics, stateful rolling windows. Out of scope. The ticket's *">20% empty rate"* criterion is captured by "did the filter exclude everything" — which is the same binary state in practice.

---

## 6. Boundaries — what this ticket does NOT fix

FLO-334 is scoped to the era-filter failure. Adjacent issues observed during investigation but **out of scope**:

| Issue | Suggested ticket | Why out of scope here |
|---|---|---|
| 11 trade_conditions files have `<MISSING>` `system_version` | **FLO-333** (already opened for `trade_conditions` capture regression) | Separate capture-path bug |
| `volume_h1` captured as `None` in all recent snapshots | FLO-333 or new P2 | Upstream indicator-capture gap |
| `rex_agreed` populated only ~12% of the time | FLO-333 | Same capture-path family |
| Bucket diversity is low even with widened era filter (21 buckets over 32 trades) | future observational | Depends on above capture fixes; independent of era filter |
| `trade_lessons.json` is now an audit log but still accumulates | unknown | Dead-data-storage question, not decision-impacting |

**After FLO-334 ships, lessons will flow from 0 → ~3 (verified by simulation).** After FLO-333 fixes the capture gap, lesson count and bucket diversity will climb further — that's the expected compound improvement.

---

## 7. Phase 2 scope proposal

Deliverable: small, focused fix implementing Option 1.

### 7.1 Design work (0.5h)

1. Choose the boundary sentinel. Proposal: keep `"pre_FLO-327"` (backward-compatible — matches existing tagged files).
2. Decide whether to KEEP `LESSONS_CURRENT_ERA_SHAS` as historical record (comment-out) or remove entirely. Proposal: keep commented-out for audit trail.
3. Confirm the `<MISSING>`/`None` `system_version` handling — exclude (current behavior) or include (change). Proposal: exclude (don't trust untagged rows).

### 7.2 Implementation work (~15 LoC)

- `trade_lessons.py` — `get_relevant_lessons()` filter swap + 3-line WARN detector.
- `config.py` — add `LESSONS_ERA_BOUNDARY = "pre_FLO-327"`; comment out `LESSONS_CURRENT_ERA_SHAS`.
- `CLAUDE.md` — shorten the "Trade Lessons Era Management" section.

### 7.3 Rule 20 test plan

1. Empty snapshot directory → returns `[]`, no WARN.
2. All-pre-327 snapshots → returns `[]`, WARN fires (skipped_era > 0, processed = 0).
3. Mixed pre-327 + post-327 snapshots → returns lessons from post-327 only, no WARN.
4. All post-327 snapshots but < 3 occurrences per bucket → returns `[]`, NO WARN (processed > 0, buckets just too small).
5. Re-run within same process → WARN does not re-fire (module-scope `_LESSONS_EMPTY_WARNED` flag).

### 7.4 Verification post-deploy

- Restart bot, monitor logs.
- Next cycle: `get_trade_lessons` call should return 3 lessons (or more as new trades land).
- Grep `LESSONS_ERA_FILTER_DEGRADED` — should be absent after the fix.
- Floki's reasoning may or may not reference lessons initially; that's fine, the tool simply works again.

### 7.5 5-day observation

- Success: `get_trade_lessons` non-empty rate climbs above baseline (>10%), no regression in OPEN decision volume, no new WARN lines.
- Null result: lesson tool still returns non-empty but Floki ignores. Separate from FLO-334 — could be format issue, surfaces a downstream question for later.
- Abort: if WARN line re-fires post-fix (logic regression).

---

## 8. What I do NOT know

1. **Whether the 3 lessons a widened filter would surface are useful to Floki.** My simulation shows the bucket shape; whether those lessons influence decisions is a post-deploy question.
2. **Whether there are other downstream consumers of `LESSONS_CURRENT_ERA_SHAS`.** I grepped and found only `trade_lessons.py` using it, plus `scripts/backfill_system_version.py` as a documentation reference. But Rule 11: verify before removing. Proposal: keep commented-out in config.py to avoid hard-breaking any external reader.
3. **Whether the `<MISSING>` tag is a capture-path bug (FLO-333) or something else.** 11 untagged files exist; I did not trace their provenance. My recommendation excludes them conservatively.
4. **Whether operators ever manually edit `LESSONS_CURRENT_ERA_SHAS` for ad-hoc analysis.** If so, the new `LESSONS_ERA_BOUNDARY` flag supports the same use case (set to a pre-era SHA temporarily, then reset), but mechanically different.

---

## 9. Summary

- **Rule 11 confirmed.** Era filter was intentional (FLO-328); investigation preserves the intent, changes the mechanism.
- **Root cause**: process gap — SHA whitelist requires prospective per-commit operator discipline; discipline held for ~2 days then collapsed over 71 subsequent commits.
- **Evidence**: 0 qualifying trades today under current filter vs 32 under widened filter (simulation).
- **Fix (Option 1)**: swap the whitelist for a time-boundary sentinel (`LESSONS_ERA_BOUNDARY = "pre_FLO-327"`). ~15 LoC. Zero maintenance afterward.
- **Detection**: 3-line once-per-process WARN on `processed == 0 && skipped_era > 0`.
- **Phase 2 gating**: FLO-332 Phase 3 implementation is BLOCKED on FLO-334 Phase 2 shipping (per CTO sequencing and "tool-ecosystem trust erosion" framing).

**Ready to proceed on CTO approval of Option 1.**

**TL;DR for CTO:** the era filter is intentional and remains intentional. The SHA whitelist needs to become a time boundary. One flag, one filter-line swap, one WARN detector — 15 LoC. After ship: `get_trade_lessons` flows again immediately with ~3 lessons from today's corpus, grows as new trades accumulate.
