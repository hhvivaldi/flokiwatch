# FLO-332 Phase 2 — Design Doc: `recent_attempts_at_current_level`

**Status:** DESIGN (no code written, no POC run). Phase 3 implementation gated on CTO approval.
**Phase 1:** `data/_audits/flo332/FLO-332_Phase1_Investigation.md`
**Author:** DEV, 2026-04-22
**Owner:** CTO approval required before Phase 3.

---

## 0. Summary

Extend the existing proactive data-package dict (auto-rendered into Floki's user message via `json.dumps` in `ai_agent._build_user_message:1693`) with one new structured sub-object `recent_attempts_at_current_level`. Purely observational. Driven by a single SQL query against `history.db`. Feature-flagged OFF by default. ~30–50 LoC. No new tool. No prompt change.

The block answers exactly one question: *"within ±N pips of the current price, which trades has Floki already opened during this session and what were their outcomes?"*

---

## 1. Final shape

JSON sub-object emitted as a top-level key of the data package dict. Floki sees it serialized by `json.dumps(data_package, indent=2, default=str)`. XML below is illustrative only; the dead `format_session_memory_xml` path is not used.

**Populated example (what Floki would have seen at 15:03 UTC before OPEN_BUY #9, all three attempts already closed at snapshot time):**

```json
"recent_attempts_at_current_level": {
  "current_price": 4735.92,
  "radius_pips": 10,
  "session_start_utc": "2026-04-22T00:00:00Z",
  "attempts_in_window": 3,
  "attempts": [
    {"ticket": 1606526654, "time": "14:33", "direction": "BUY", "entry": 4735.98, "sl": 4732.98, "outcome": "SL", "pnl": -6.10, "duration_min": 10, "close_time": "14:43"},
    {"ticket": 1606383321, "time": "13:45", "direction": "BUY", "entry": 4735.78, "sl": 4732.78, "outcome": "EA", "pnl": -3.40, "duration_min": 2,  "close_time": "13:47"},
    {"ticket": 1605185504, "time": "00:41", "direction": "BUY", "entry": 4737.00, "sl": 4723.00, "outcome": "SL", "pnl": 14.72, "duration_min": 47, "close_time": "01:28"}
  ]
}
```

**Empty example (no prior attempts near current price):**

```json
"recent_attempts_at_current_level": {
  "current_price": 4735.92,
  "radius_pips": 10,
  "session_start_utc": "2026-04-22T00:00:00Z",
  "attempts_in_window": 0,
  "attempts": []
}
```

**Open-trade included example (position still open — per advisor, this is the loudest-possible prior):**

```json
"recent_attempts_at_current_level": {
  "current_price": 4735.92,
  "radius_pips": 10,
  "session_start_utc": "2026-04-22T00:00:00Z",
  "attempts_in_window": 2,
  "attempts": [
    {"ticket": 1606526654, "time": "14:33", "direction": "BUY", "entry": 4735.98, "sl": 4732.98, "outcome": "OPEN", "pnl_floating": -0.80, "age_min": 5},
    {"ticket": 1606383321, "time": "13:45", "direction": "BUY", "entry": 4735.78, "sl": 4732.78, "outcome": "SL",   "pnl": -3.40, "duration_min": 2, "close_time": "13:47"}
  ]
}
```

**Note on MFE/MAE for OPEN rows:** excluded from Phase 2. `trades.mfe_points / mae_points` are written at trade close (null for open positions), and pulling live trajectory from `trade_snapshots` adds a second query plus MT5 dependency. Keep the OPEN-row shape minimal: `pnl_floating + age_min` is sufficient for the "there's an open position here" signal. If Floki wants trajectory detail, `get_position_history(ticket)` is the existing answer.

### Field dictionary

| Field | Type | Always present? | Notes |
|---|---|:---:|---|
| `current_price` | float | yes | Filled bid. Used to render the filter context. |
| `radius_pips` | int | yes | From `config.SESSION_LEVEL_HISTORY_RADIUS_PIPS` (default 10). |
| `session_start_utc` | str (ISO-Z) | yes | UTC midnight of today. See §5 for boundary rationale. |
| `attempts_in_window` | int | yes | Number of attempts returned. Equals `len(attempts)`. If the cap is hit and more exist, the doc notes it (see §5.6). |
| `attempts` | list[dict] | yes | Empty list if none. Never omit the key. |
| `attempts[].ticket` | int | yes | MT5 deal ticket for traceability. |
| `attempts[].time` | str "HH:MM" | yes | UTC wall clock of `open_time`. |
| `attempts[].direction` | "BUY" \| "SELL" | yes | — |
| `attempts[].entry` | float | yes | `open_price` (filled price — not the requested price; see §4). |
| `attempts[].sl` | float | yes | The trade's SL at open. |
| `attempts[].outcome` | "SL" \| "TP" \| "EA" \| "OPEN" | yes | "EA" = Expert-Advisor managed close (trailing BE hit, Floki close, etc.). |
| `attempts[].pnl` | float | closed only | USD P&L at close. |
| `attempts[].duration_min` | int | closed only | Minutes open. |
| `attempts[].close_time` | str "HH:MM" | closed only | UTC wall clock. |
| `attempts[].pnl_floating` | float | open only | Floating USD P&L at snapshot time. |
| `attempts[].age_min` | int | open only | Minutes since open. |

### Name decision
Named `recent_attempts_at_current_level` per CTO-approved shape in the ticket. Not revisited.

---

## 2. Exact insertion point

Two insertion sites — both in `agent_data_builder.py`:

| Function | File:line | Current state | Edit |
|---|---|---|---|
| `build_data_package` | `agent_data_builder.py:1265` | Builds `package` dict ending at line 1352 with `"session_memory": session_memory,` | Add line 1353: `"recent_attempts_at_current_level": build_recent_attempts(current_price, cfg),` |
| `build_proactive_data_package` | `agent_data_builder.py:1362` | Builds `package` dict ending at line 1433 with `"session_memory": session_memory,` | Add line 1434: `"recent_attempts_at_current_level": build_recent_attempts(current_price, cfg),` |

The new builder function `build_recent_attempts` lives in the same module, adjacent to `load_session_memory` and `format_session_memory_xml` (approx. line 70–110). It takes `current_price` dict and a `config` reference; returns the dict shown in §1.

**Dead-code note:** `format_session_memory_xml` (line 32) is defined but never called in production. `format_proactive_xml` (line 550) is referenced only by a test file. FLO-332 does NOT touch either — everything ships via the JSON path through `ai_agent._build_user_message:1693`.

---

## 3. Query logic

Single SQL query against `data/history.db`. Two branches (closed + open) unioned in Python for clarity. Parameterized — no injection surface.

```python
# agent_data_builder.py (new function)
def build_recent_attempts(current_price: Dict[str, Any], radius_pips: int = 10, max_attempts: int = 5) -> Dict[str, Any]:
    """Return the structured list of same-session trades within radius_pips of current_price.

    Read-only. Degrades gracefully to empty on any error. Escola 1 compliant:
    factual fields only. Does not decide, does not warn, does not prescribe.
    """
    if not config.ENABLE_SESSION_LEVEL_HISTORY:
        return {"current_price": None, "radius_pips": radius_pips,
                "session_start_utc": None, "attempts_in_window": 0, "attempts": []}

    try:
        price_val = float((current_price or {}).get("bid")
                          or (current_price or {}).get("ask") or 0.0)
        if price_val <= 0:
            return _empty_attempts(radius_pips)  # current-price unavailable — §5

        pip_size = 0.1  # XAUUSD — consistent with executor.py:249 / agent_tools.py:470
        radius_price = radius_pips * pip_size

        from tz_utils import trading_day_utc, utc_iso
        today_str = trading_day_utc()
        session_start_iso = f"{today_str}T00:00:00Z"

        import sqlite3
        import config as cfg
        conn = sqlite3.connect(cfg.HISTORY_DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT ticket, direction, open_price, sl, profit, close_reason,
                       open_time, close_time, mfe_points, mae_points
                FROM trades
                WHERE open_time >= ?
                  AND ABS(open_price - ?) <= ?
                ORDER BY open_time DESC
                LIMIT ?
                """,
                (session_start_iso, price_val, radius_price, max_attempts * 2),
            ).fetchall()
        finally:
            conn.close()

        attempts = [_format_attempt(r, price_val) for r in rows]
        # Cap after formatting in case of near-duplicates / filtering noise
        return {
            "current_price": round(price_val, 2),
            "radius_pips": radius_pips,
            "session_start_utc": session_start_iso,
            "attempts_in_window": len(attempts),
            "attempts": attempts[:max_attempts],
        }
    except Exception as e:
        logger.warning(f"build_recent_attempts failed: {e}")
        return _empty_attempts(radius_pips)
```

### Outcome classification

- `close_reason == "Stop Loss"` → `"SL"`
- `close_reason == "Take Profit"` → `"TP"`
- `close_time IS NULL` → `"OPEN"`
- anything else (e.g. `"Expert Advisor"`, manual close, timeout) → `"EA"`

Rationale: Floki only needs the first-order classification. Fine-grained reasons (BE hit, trailing, monitor close) are already available via `get_position_history(ticket)` if he wants detail.

---

## 4. Radius unit — XAUUSD pip size

**Verified:** across the codebase 1 pip = 0.1 price units for XAUUSD (Capital Point broker).
- `alerts.py:539`, `agent_tools.py:470,503`, `executor.py:249,838`, `main.py:226` — all use `0.1`.
- Config constant: `MAX_SPREAD_PIPS = 5.0 # 1 pip = $0.10` (`config.py:339`).

Default `radius_pips=10` → `radius_price=1.00` USD move in either direction from current price. Configurable via `config.SESSION_LEVEL_HISTORY_RADIUS_PIPS`.

**`open_price` is the filled price, not the requested price** — correct for this purpose. Pending-order fills may drift a few cents from the limit price; the radius filter must apply to what actually happened (the fill), not the intention.

---

## 5. Edge cases

### 5.1 First trade of the session (empty list)
`attempts: []`, `attempts_found: 0`. Key is always present. Floki learns "no prior attempts today at this level" — useful positive information.

### 5.2 UTC day boundary rollover
Session boundary = `tz_utils.trading_day_utc()` → UTC midnight (YYYY-MM-DD 00:00:00Z).

**Verified alignment:** this matches `agent_session_memory.json`'s `session_date` field (written via `trading_day_utc()` in `agent_tools.py:3654` and `ai_agent.py`). `trades.open_time` is UTC ISO-Z (confirmed by sample query: `'2026-04-22T00:41:24.171517Z'`).

**Known inconsistency (out of scope):** `main.py` uses `trading_day_broker_aligned()` (22:00 UTC boundary) for daily_stats in some paths while Sage/reflexion/dashboard use UTC midnight. FLO-332 aligns with the session_memory boundary (UTC midnight) because the new block is a structural extension of session_memory. If the project migrates everything to broker-aligned in the future (tracked in `project_cross_module_day_boundary.md`), FLO-332 must migrate with it.

### 5.3 No trades in radius
Same as first-trade: `attempts: []`, `attempts_found: 0`. Key present.

### 5.4 Current price unavailable
`_empty_attempts()` returns a placeholder: `current_price: null`, `attempts: []`, `attempts_found: 0`. Never raises. Log at WARNING.

### 5.5 Open positions at the current level (advisor correction)
Included with `outcome: "OPEN"` plus `pnl_floating` and `age_min`. Rationale: the loudest prior at 4735 is the trade currently sitting at 4735 — `<positions>` shows it, but not filtered to the price-radius lens.

**Branch: open-trade data source.** Columns `ticket, direction, open_price, sl` come from `trades WHERE close_time IS NULL`. `pnl_floating` is computed from live MT5 price: `mt5.positions_get(ticket=...)` via the existing executor. If MT5 is unreachable, degrade to `pnl_floating: null` rather than drop the row — the structural fact that the trade is open at this level is still the valuable signal.

**MFE/MAE explicitly excluded from Phase 2.** `trades.mfe_points/mae_points` are closed-trade-only columns (null for open positions). Adding trajectory metrics would require a second query against `trade_snapshots` plus MT5 data plumbing — out of scope for a 30–50 LoC feature. Floki already has `get_position_history(ticket)` for trajectory detail on any open ticket.

### 5.6 Capped attempts
`max_attempts=5` — if more than 5 trades fall in the radius, keep the 5 most recent; `attempts_in_window` still reports the true count. Floki can query `get_trade_journal` for a fuller view if needed.

### 5.7 Feature flag disabled
Returns the skeleton with `current_price: null`, `attempts: []`. The key is still emitted so Floki's parser and downstream consumers see a consistent schema. Off by default (see §9).

### 5.8 Database lock / timeout
SQL timeout = 5 seconds. On timeout, log WARNING and return `_empty_attempts`. Never block the cycle.

---

## 6. Token budget verification

Per-attempt entry size (JSON-serialized):
```
{"ticket": 1606526654, "time": "14:33", "direction": "BUY", "entry": 4735.98,
 "sl": 4732.98, "outcome": "SL", "pnl": -6.10, "duration_min": 10,
 "close_time": "14:43"}
```
= ~140 characters. `json.dumps(..., indent=2)` adds ~20% indentation → ~170 chars per attempt.

| Scenario | Attempts | Chars (w/ indent) | Tokens est. (4 chars/tok) |
|---|---:|---:|---:|
| Empty | 0 | ~180 (wrapper only) | ~45 |
| Typical (1 attempt) | 1 | ~350 | ~90 |
| Busy cluster (3 attempts) | 3 | ~680 | ~170 |
| Cap reached (5 attempts) | 5 | ~1,020 | ~255 |

**Worst case (5 attempts + indent): ~255 tokens.** Below the §6 Phase-1 budget of <100 tokens *typical* (1–2 attempts is typical, matching the 11.2% repeat baseline). **Headroom is comfortable** against the Floki prompt's current input-token profile (typically 8k–15k per cycle).

If tokens become a concern post-deploy, trim `ticket` (20 chars) and `duration_min` (18 chars) — reduces each entry by ~25%.

---

## 7. FLO-334 precedence — DEV recommendation

**AGREE with CTO sequencing: ship FLO-334 before FLO-332 Phase 3.**

Stronger framing than "attribution corruption": **tool-ecosystem trust erosion.** Today Floki called `get_trade_lessons` 22 times and got empty 22 times. Over 7 days: 124/131 empty (94.7%). When a pull tool silently returns nothing in the steady state, the caller implicitly learns the tool has no signal — across-the-board memory-retrieval call rate is 31% of OPEN cycles in the last 7 days, and that number is not a deliberate choice, it's a learned degradation.

If FLO-332 Phase 3 ships while `get_trade_lessons` is still silently broken, `recent_attempts_at_current_level` appears in the data package alongside a long-discredited sibling. Floki's learned prior is "memory tools don't have anything useful" — there is a real risk the new block inherits that skepticism before it earns its own signal.

**Recommended sequence:**
1. FLO-332 Phase 2 design doc — *this document* (delivered).
2. FLO-334 Phase 1 investigation — root-cause the era-filter and field-capture interaction.
3. FLO-334 Phase 2 implementation — minimum viable fix restores non-empty payloads.
4. FLO-334 5-day observation — confirm `get_trade_lessons` non-empty rate climbs.
5. FLO-332 Phase 3 implementation — ship `recent_attempts_at_current_level` into a healthy memory ecosystem.
6. FLO-332 5-day observation.

**Not a lock**, per ticket. If the CTO prefers parallel delivery, the design doc is complete and Phase 3 is ready — FLO-332 is technically independent from FLO-334. The argument above is about signal quality during observation, not technical coupling.

---

## 8. Rule 20 test plan

Three classes of test. All must run green before any code is pushed. `pytest` not required — `python -c "..."` standalone invocation per CLAUDE.md convention.

### 8.1 Unit — `build_recent_attempts`

```python
# scripts/_investigations/flo332_phase3_test.py
from agent_data_builder import build_recent_attempts

# Case 1 — feature flag OFF returns skeleton
assert build_recent_attempts({"bid": 4735.0}) == {...empty skeleton...}

# Case 2 — feature flag ON, empty DB returns empty attempts
# (uses a tempfile DB with schema only)
result = build_recent_attempts({"bid": 4735.0})
assert result["attempts_in_window"] == 0 and result["attempts"] == []

# Case 3 — seeded DB with 2 closed + 1 open trade within radius
# Seed: closed at 4735.5 (SL), closed at 4736.1 (EA), open at 4734.9
# Expect: 3 attempts, outcomes [SL, EA, OPEN], ordered by open_time DESC
result = build_recent_attempts({"bid": 4735.0})
assert result["attempts_in_window"] == 3
assert [a["outcome"] for a in result["attempts"]] == ["OPEN", "EA", "SL"]

# Case 4 — trade outside radius not included
# Seed: closed at 4740.0 (SL), closed at 4735.0 (SL), radius=10 pips → 1.0 price
# Expect: only 4735.0 trade
...

# Case 5 — trade from yesterday not included
# Seed: close_time yesterday, open_time yesterday
# Expect: attempts_found == 0

# Case 6 — current_price missing → empty skeleton, no raise
result = build_recent_attempts({})
assert result["attempts_in_window"] == 0
```

### 8.2 Integration — live `build_data_package` run

```bash
python main.py --test
# Inspect logged prompt in data/_audits/ for the new key presence
```

Verify:
- `"recent_attempts_at_current_level"` key is present in the JSON dump.
- When flag is OFF: empty skeleton.
- When flag is ON: populated list matching a hand-verified SQL run.

### 8.3 Schema contract

Update `FIELD_CONTRACT.md` (Rule 16) — add entry for the new sub-object, listing every field and its type/nullability.

### 8.4 Replay verification (Rule 19 / adoption proxy)

For at least one of the 3 losing OPEN_BUYs of 2026-04-22, replay the builder against the historical DB state at the exact decision timestamp and verify the expected block against a hand-run SQL query. Concrete replay target:

- OPEN_BUY #9 at 15:03 UTC, current_price ≈ 4735.92. Expected: at least 2 prior attempts in-window (tickets 1606526654 at 4735.98 and 1606383321 at 4735.78). Depending on position-state at snapshot, one may be `outcome="OPEN"` with live `pnl_floating`, the other `outcome="SL"` with closed pnl.

Replay match → query logic correct. Mismatch → fix before push.

---

## 9. Feature flag

```python
# config.py (additions)
# FLO-332 — Session-level pattern awareness (Phase 2 design; Phase 3 implementation).
ENABLE_SESSION_LEVEL_HISTORY = False
SESSION_LEVEL_HISTORY_RADIUS_PIPS = 10
SESSION_LEVEL_HISTORY_MAX_ATTEMPTS = 5
```

- Default `False` per CTO directive. Pre-code-review safety.
- Flip to `True` only after Phase 3 code review passes.
- Runtime-toggleable via `.env` override if the env-loader path is extended (out of scope for Phase 2).

**Kill-switch behavior:** setting to `False` mid-run causes the next cycle to see the skeleton form of the key (no data). No restart needed. No prompt collision — Floki's parser treats empty `attempts` as absence.

---

## 10. 5-day observation success criteria

Honest on sample size — 5 days of 100% autonomous trading is approximately 25–50 trades depending on regime. This is a DIRECTIONAL target with caveat, not a binary gate.

### 10.1 Primary — behavioral

**Qualitative:** Floki's `agent_reasoning` text references the new block at least once in an OPEN decision cycle where `attempts_found > 0`. Validated by grep over `agent_proactive_analyses.agent_reasoning` for substrings like `"prior attempts"`, `"already entered"`, `"earlier at this level"`.

**Criterion:** ≥ 1 uncoerced reference in 5 days of live operation. Zero → indicates block is invisible to Floki → revisit format or surface as a named tool (Option A fallback).

### 10.2 Secondary — rate

**Repeat-rate of same-session 5-pip re-entries.** 30-day pre-deploy baseline: 11.2%. Today's post-FLO-331 outlier: 33% (3/9). Post-deploy target: DIRECTIONALLY lower than baseline, but n=25-50 is underpowered for a statistical claim.

**Criterion:** trend below baseline OR no trend worse than baseline. An increase would indicate the block is either being ignored or (worse) encouraging pattern-reinforcement. Abort criterion: repeat rate > 20% over 5 days → revisit.

### 10.3 Sanity — non-regression

- OPEN decision count / day: within ±30% of 30d baseline.
- Cycle latency p50: no regression > 10%.
- Input tokens / cycle: no regression > 5%.
- No new `ERROR` or `WARNING` level logs originating from `build_recent_attempts`.

### 10.4 Null-result handling

If 5-day observation shows no behavioral change and no rate improvement:
- DO NOT tune the block by adding prescriptive language (Escola 1 collision).
- DO NOT add prompt nudges telling Floki to look at it.
- DO consider a format change: collapse to a one-line summary string (e.g. `"session_level_summary": "3 attempts at 4735 today (2 SL, 1 closed +$14)"`) — still observational, higher signal density.
- DO consider surfacing the same data as a named tool (Option A fallback), letting adoption rate prove value.

---

## 11. Escola 1 v2.0 final compliance audit

Checked against non-negotiables from the ticket:

| Constraint | Block compliance |
|---|---|
| Only factual attributes | ✅ `time, direction, entry, sl, outcome, pnl, ticket, duration_min` — all measurable facts from MT5/history.db. |
| No prescriptive verbs | ✅ Zero `avoid`, `don't`, `should`, `must`, `warn`, `reconsider`, `caution` tokens in the block. |
| No `<warning>`, `<avoid>`, `<should>`, `<must>` elements | ✅ Not present. |
| Observational naming | ✅ `recent_attempts_at_current_level` — describes what it shows, not what to do about it. Parallel to `sr_zones`, `open_positions`, `trade_feedback`, `recent_decisions`. |
| Preserves Floki agency | ✅ Block reports. Nothing reads `attempts_found > 0` and gates a decision. |
| Does not block trades | ✅ No control-flow dependency exists or is proposed. |
| Does not adjust confidence | ✅ Confidence is Floki's output; this block is input only. |

**Verdict:** passes. Design ready for Phase 3 code, pending CTO approval and FLO-334 sequencing decision.

---

## 12. Rule 16 — docs that must update in Phase 3 commit

- `FIELD_CONTRACT.md` — add the sub-object schema (all fields, types, nullability).
- `SYSTEM_DOCUMENTATION.md` — add a paragraph under §11 (near FLO-322 SL mental model) documenting the new block as Phase 3's behavioral-observation layer.
- `CLAUDE.md` — no change required (no new rules/conventions).
- `README.md` — no change required (no architecture-level shift).

---

## 13. Open questions for CTO (non-blocking)

1. **Open-trade inclusion (§5.5).** Advisor-flagged. Design includes OPEN rows with floating pnl. Confirm.
2. **FLO-334 sequencing (§7).** DEV agrees with CTO recommendation to ship FLO-334 first. Confirm or override.
3. **Radius default.** 10 pips proposed. Smaller (5) is tighter but may miss clusters; larger (15-20) adds noise. Willing to ship 10 and tune post-observation.
4. **Max attempts cap.** 5 proposed. Historical maximum in 30-day corpus is 3 in any same-session 5-pip cluster — 5 is comfortable headroom.
5. **Broker-aligned vs UTC-midnight boundary.** §5.2 explicitly aligns with `agent_session_memory.json` (UTC midnight). If CTO prefers broker-aligned for consistency with `main.py` daily_stats, note that decision and FLO-332 migrates with it.

---

## 14. Phase 3 deliverables (on GO)

1. `agent_data_builder.py` — new `build_recent_attempts()` function + 2 call-site edits.
2. `config.py` — 3 new constants.
3. `scripts/_investigations/flo332_phase3_test.py` — 6 unit tests.
4. `FIELD_CONTRACT.md` — new schema entry.
5. `SYSTEM_DOCUMENTATION.md` — §11 paragraph.
6. One commit: `feat: FLO-332 — Session-level pattern awareness (recent_attempts_at_current_level)`.
7. 5-day observation window opens at bot restart.

**Estimated LoC:** 30 (production) + 40 (tests) + 20 (docs) ≈ **90 LoC total**.

**Ready to proceed on CTO approval and FLO-334 sequencing confirmation.**
