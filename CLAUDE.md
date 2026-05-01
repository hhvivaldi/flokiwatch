# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

FlokiWatch: autonomous 7-agent XAU/USD (Gold) trading system on MetaTrader 5. Runs 100% autonomously. See `README.md` for overview, `SYSTEM_DOCUMENTATION.md` for detailed behavior.

## Commands

```bash
python main.py              # Production
python main.py --test       # Single cycle then exit
python main.py --dry-run    # No MT5 execution
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080  # Dashboard
python test_central_brain.py   # Unit tests (standalone scripts, no pytest)
```

## Architecture — 8 Agents

| Agent | Model | File | Role |
|-------|-------|------|------|
| Floki | GPT-5.4 | `ai_agent.py` | Sole trading decisor. 30 tools. Self-schedules 5-30 min. |
| Rex | GPT-4o | `rex_validator.py` + `rex_monitor.py` | Analyst. 11 tools (6 standard + 5 unique). Bull/Bear debate (FLO-190). Proactive monitor every 30 min (FLO-211). |
| Research Mgr | Gemini 3 Flash | `research_manager.py` | Picks winner between Rex Bull and Rex Bear. Produces verdict with triggers (FLO-194). |
| Simba | Python | `agent_monitor.py` | Watchdog. 30s polling. Wakes Floki. |
| Sage | Gemini | `sage_auditor.py` | Daily auditor at 21:00 UTC. |
| Echo | MiMo-V2-Flash | `echo_sentinel.py` | News sentinel. 25 RSS feeds. PULL-only. |
| Luna | MiMo-V2-Flash | `luna_analyst.py` | Macro analyst. MT5+Yahoo+FRED. Observational output only (no env/risk/bias labels post-Bug G — Floki interprets). |
| Brain | Python | `central_brain.py` | Data pipeline. 5-pillar analysis. No decisions. |

**NOTE:** `simba_watcher.py` is dead code. Canonical Simba is `agent_monitor.py`.

## Model Independence

Each agent has its OWN model config variable — NEVER share between agents:
- `FLOKI_MODEL` → ai_agent.py | `REX_MODEL` → rex_validator.py
- `SAGE_MODEL` → sage_auditor.py (Gemini, NOT OpenAI)
- `ECHO_MODEL` → echo_sentinel.py | `LUNA_MODEL` → luna_analyst.py

## Data Flow

```
main.py (orchestrator)
  ├─ central_brain.py → BrainResult (score 0-100, direction, 5 pillars)
  │   ├─ technical_analyzer.py (45%) | ml_predictor.py (15%)
  │   ├─ news_score_hybrid.py (40%) | momentum_detector.py
  │   └─ economic_calendar.py | regime_detector.py (7 regimes, FLO-139)
  ├─ ai_agent.py (Floki) → WAIT / OPEN / CLOSE / ADJUST
  │   ├─ rex_validator.py (debates) | rex_monitor.py (30-min scan) | agent_tools.py (28 tools)
  ├─ executor.py → ea_bridge.py → FlokiBridge EA → MT5
  ├─ monitor.py → position management (BE, trailing, drawdown)
  ├─ state_writer.py → bot_state.json | db_writer.py → history.db
```

## Key Design Decisions

- **Floki is sole decisor.** Rex is advisory ("DISAGREE is feedback, not a veto").
- **Rex Bull/Bear debate (FLO-190/194):** Before each Floki cycle, Rex Bull argues gold goes UP (BUY) and Rex Bear argues gold goes DOWN (SELL) in parallel. Research Manager (Gemini) picks the winner → `<verdict>` block in trigger_context. If RM fails, falls back to `<debate>` block. Both Bull and Bear must succeed or neither is shown (Rule 1).
- **Rex defaults to DISAGREE on failure.** Truncation/parse error = no agreement.
- **EA is pure executor.** `FLOKI_MANAGES_POSITION = True`. 9999-pip triggers never fire.
- **Echo is pull-based.** Floki pulls alerts via tool, Echo does not push.
- **Rex monitor (FLO-211 / FLO-316):** Runs 4 tools every 30 min (divergence, correlation, regime, session). No LLM — deterministic classifier. Writes `data/rex_monitor.json`. Floki pulls via `get_rex_monitor`. FLO-316 removed prescriptive `alert_level` (QUIET/NORMAL/ELEVATED/CRITICAL) + `alert_context` + `alert_hint` + per-finding `severity` + `implication` fields. Output is now observational: each finding is `{type, observation, data}` (type ∈ DIVERGENCE/CORRELATION/REGIME/SESSION). Simba wake now gates on `findings_count >= 2` instead of `alert_level == CRITICAL` (2h debounce preserved). Bull/Bear debate injection of monitor findings already removed in Commit 1 (FLO-243 decoupling).
- **Session block removed (FLO-317, Fase 2 of FLO-314):** `<session>` XML block (`today_trades` / `today_wins` / `today_losses` / `today_pnl` / `last_5_results` / `consecutive_losses`) no longer injected into Floki's prompt. Rationale: running day-W/L produced a WAIT death spiral (6-day climb 45%→94%). Forced-injection caution vector per Escola 1 v2.0. `<open_positions count="..">` preserved (FLO-85 opposing-positions guard + max-positions risk manager). `get_session_context` tool untouched — Floki can still pull session data when he chooses (agency preserved). `_format_session_context()` helper deleted; `session_context: Dict` removed from `build_data_package` / `build_proactive_data_package` signatures.
- **Score system:** 0-100. 50=neutral. >65=BUY. <35=SELL. 45-55=HOLD.
- **Active thesis persistence:** `data/active_thesis.json` — carries between cycles.
- **Boss notes (FLO-303):** `data/boss_notes.json` — Hermano's directives to Floki. Active notes injected as `<boss_notes>` block at top of user message each cycle. Floki returns `acknowledged_boss_notes: [id, ...]` to stamp them as read. Default 24h auto-expiry, `expires_at:null` for permanent, `ack_dismisses:true` for one-shots. Add notes via `boss_notes.add_note(text, ...)` or edit the file directly. Non-blocking: missing/malformed file is a silent no-op.

## Critical Safety Rules

- No simultaneous BUY+SELL (FLO-85 hard gate, `is not None` check not truthiness)
- Max 3 positions. No trades 60 min before/after market open/close. Max 6% daily loss.
- Volatility guard: M5 >1.8% blocks trades (`volatility_guard.py`)
- adjust_trade: SL-widening guard + rate limit (max 3/hour/ticket, FLO-141)
- **Monotonic SL invariant (FLO-419, executor.py modify_position):** SL never loosens. BUY: new_sl >= current_sl. SELL: new_sl <= current_sl. Exception: first SL set when current is 0. Loosening attempts return `OrderResult(success=False, error_code=-5)` with a `SL_GUARD` warning log. Universal — covers Snow, Qwen TM, monitor.py, EA bridge, and any future caller.
- **Hybrid SL architecture (FLO-419):** Snow plan management = at most ONE `move_sl_to_breakeven` contingency at `mfe_reached >= 100` pips (validator rejects anything else — no `trail_sl`, no `adjust_sl`, no BE below 100p, no >1 contingencies). Tactical SL belongs to Qwen Trade Manager via `adjust_trade` on its 60s heartbeat. Snow is the safety net; TM is the brain. Supersedes FLO-416. See `data/_audits/gemini_era_trade_audit_2026-05-01.md` for the empirical motivation (PLAN-20260501-013 lost $6.16 to a non-monotonic trail; PLAN-20260430-020 forfeited ~$11 to a 15-pip BE).

## Code Review Rules

**Rule 11 — Intent Before Change.** NEVER assume a bug. Check `git show HEAD:filename` for original intent. 3 fixes were reverted in this codebase for changing intentional design.

**Rule 14 — Review Before Push.** For commits touching decision logic (`ai_agent.py`, `agent_tools.py`, `executor.py`, `safety_checks.py`, `monitor.py`, `rex_validator.py`, `floki_position_manager.py`, `risk_manager.py`): run code review with skill, show output, classify as BUG/DEFENSIVE/INTENTIONAL.

**Rule 15 — Complete File Before Push.** New files >100 lines: show complete file for audit.

**Rule 16 — Docs Updated in Same Commit.** `CLAUDE.md` if rules/conventions changed. `FIELD_CONTRACT.md` if bot_state.json changed. `README.md` if architecture changed. `SYSTEM_DOCUMENTATION.md` if behavior changed.

**Rule 17 — Push Immediately.** Every commit pushed to GitHub. No local-only commits.

**Rule 18 — Use Appropriate Skills Before Implementing.** Before implementing ANY change, read the relevant skill SKILL.md. This is NOT optional.
- **Frontend** (trade_room.html, app.js, style.css, index.html, ANY HTML/CSS/JS):
  Read: `engineering-skills/senior-frontend/SKILL.md` then invoke `/distinctive-frontend`
- **Backend** (main.py, server.py, ANY Python logic, API endpoints):
  Read: `engineering-skills/senior-backend/SKILL.md`
- **Architecture** (new agents, system redesign, data flow changes, new files):
  Read: `engineering-skills/senior-architect/SKILL.md`
- **Security** (API keys, validation, authentication, rate limiting):
  Read: `engineering-skills/senior-security/SKILL.md`
- **ML / Data** (ml_predictor.py, training scripts, SQLite queries, data pipelines):
  Read: `engineering-skills/senior-ml-engineer/SKILL.md` + `engineering-skills/senior-data-engineer/SKILL.md`
- **Prompts** (agent_prompts.py, system prompts):
  Read: `engineering-skills/senior-prompt-engineer/SKILL.md`
- **Full-stack** (when a change touches BOTH frontend and backend):
  Read: `engineering-skills/senior-fullstack/SKILL.md`
- **Agent design** (new agent architecture, multi-agent patterns):
  Read: `engineering-advanced-skills/agent-designer/SKILL.md`
- **Database** (schema changes, migrations, query optimization):
  Read: `engineering-advanced-skills/database-designer/SKILL.md`
- **Code review** (pre-push review, quality gates):
  Read: `engineering-skills/code-reviewer/SKILL.md`
Plugin skills are at: `~/.claude/plugins/cache/claude-code-skills/{plugin}/{version}/{skill}/SKILL.md`
User skills are at: `~/.claude/skills/{skill}/SKILL.md` — invoke directly as `/{skill-name}`

**Rule 19 — Verify Tools Before Blaming Agent.** When an agent appears to ignore a tool, verify the tool works first. Run it manually or check logs for error responses. A broken tool looks identical to an adoption problem — but the fix is completely different. (Learned: Floki "ignored" pending orders for a full day; root cause was `_get_balance` AttributeError making the tool fail silently.)

**Rule 20 — Test New Tools Before Push.** Every new agent tool must be called successfully at least once before pushing. Run `python -c "from agent_tools import AgentTools; ..."` or equivalent and verify a valid response. A tool that has never been called successfully in any environment is not deployable. (Learned: `place_pending_order` shipped with a nonexistent method call that would have been caught by a single test invocation.)

**Rule 21 — Never Speculate on Numbers.** NEVER use "I think", "maybe", "probably", "estimated", "likely", "may have" when discussing prices, P&L, trade outcomes, metrics, or any numerical claim. Either query the actual data (DB, MT5 candles, logs, JSON files) and show the result, or say "I don't know, need to verify." No speculation, no guessing, no rounding from memory. (Learned: speculated "SL would survive, TP hit = +$44" without checking candles. Automated counterfactual proved the opposite — SL was hit, trade SAVED $25. One wrong speculation set the wrong direction for an entire day.)

**Rule 22 — Timestamps: UTC In, Local Out (FLO-286).** ALL timestamps stored to disk (DB, JSON, logs) MUST be UTC, ISO-8601 format, with explicit "Z" suffix. ALL API responses serve UTC. ONLY the frontend converts to user-local time, via `window.displayTime()` / `displayHHMM()` / `displayAge()` from `dashboard/static/tz.js`. Backend writers MUST use `tz_utils.utc_iso()` instead of `datetime.now().isoformat()` or `datetime.utcnow().isoformat()`. "Today" boundary uses `tz_utils.trading_day_utc()` (UTC calendar) or `trading_day_broker_aligned()` (broker midnight = 22:00 UTC). NEVER call `datetime.now()` for storage — it captures local time and breaks cross-timezone consistency. NEVER inline `new Date(x).toLocaleTimeString()` in JS — use the `displayTime()` helper which is defensive against missing Z suffix. (Learned: timezone bugs reappeared in every new component — daily_stats reset at wrong hour, calendar showed yesterday's events as today, MT5 broker time was logged as UTC. Single source of truth eliminates the recurring class.)

**Rule 23 — MT5 Access via Thread-Safe Proxy Only (FLO-348).** ALL production MT5 access MUST go through `from mt5_safe import mt5` (or aliased, e.g. `from mt5_safe import mt5 as _mt5_m`). Direct `import MetaTrader5` is **FORBIDDEN** in production code (`*.py` at repo root, plus anything main.py can import). The proxy wraps every callable attribute in a shared `threading.RLock` (`mt5_lock`), preventing races between concurrent callers (Floki main loop, monitor.py, agent_monitor.py Simba, Snow FLO-347). Additionally, `executor.execute_trade` / `modify_position` / `close_position` are serialised by a module-level `executor_lock` (RLock) via the `@_with_executor_lock` decorator in `executor.py`. NEVER add a bare `import MetaTrader5` inside a function — it shadows the module-level proxy and silently bypasses both locks. For multi-call atomic operations (e.g. "read tick AND get positions in one critical section") import the lock directly: `from mt5_safe import mt5, mt5_lock; with mt5_lock: ...`. Exceptions: `scripts/` and `data/_audits/` are allowed to use raw `import MetaTrader5 as mt5` because they run as separate one-shot processes with no shared state. (Learned: Snow's 5s daemon polling + Floki's multi-minute cycles both driving `executor` required explicit locking; `_mt5_offset_cache_ex` TTL hack in executor.py:15 was already evidence of prior concern.)

**Bug Classification:** P0=crash/corruption → fix now. P1=logic error → with approval. P2=smell → deferred.

## File Conventions

- **Config:** `config.py` + `.env`. Loaded at import time.
- **Logging:** `from logger import log`. Trade-critical=`warning`/`error`. Pipeline=`debug`/`info`.
- **Database:** SQLite `data/history.db`. Parameterized queries. New connection per call with try/finally.
- **State:** JSON in `data/`. Atomic writes via temp + `os.replace()`.
- **Dashboard contract:** `FIELD_CONTRACT.md` is LAW.

## Environment

- Windows 11, Python 3.12+, MetaTrader 5 must be running
- Home: `C:\Users\Hermano\OneDrive\Desktop\XAUUSD` | Remote: `C:\Users\hvivaldi\Desktop\DevOPS\flokiwatch`
- Keys: `OPENAI_API_KEY` (Floki/Rex), `LUNA_API_KEY` (Echo/Luna), `GEMINI_API_KEY` (Sage), `FCS_API_KEY`
- **Floki provider switch (FLO-384 / FLO-389):** `LLM_PROVIDER` env (`qwen` | `kimi` | `gemini`, default `qwen`). When `LLM_PROVIDER=kimi`, config resolves `FLOKI_API_BASE`/`FLOKI_API_KEY`/`FLOKI_MODEL` from `KIMI_BASE_URL`/`KIMI_API_KEY`/`KIMI_MODEL` at config-load time (defaults: `https://api.moonshot.ai/v1` / required / `kimi-k2.5`). When `LLM_PROVIDER=gemini`, config resolves the same triple from `GEMINI_BASE_URL`/`GEMINI_API_KEY`/`GEMINI_MODEL` (defaults: `https://generativelanguage.googleapis.com/v1beta/openai/` / required / `gemini-3.1-pro-preview`). Existing OpenAI client init path consumes the resolved triple unchanged. Fallback (FLO-299) stays pointed at Qwen/OpenRouter regardless of primary — cross-provider fallback is intentional v1 safety net. Bot restart required after flipping.

## Trade Lessons Era Management (FLO-334, supersedes FLO-328)

`data/trade_conditions/*.json` files each carry a `system_version` field
(short git SHA, set automatically at trade open). `get_relevant_lessons()`
computes lessons on-read with two filters applied to every trade:
`open_time` within `config.LESSONS_WINDOW_DAYS` (default 30), AND
`system_version` must NOT equal `config.LESSONS_ERA_BOUNDARY` (default
`"pre_FLO-327"`) and must be non-empty. Both filters must pass.

**Operator maintenance: none required for typical commits.** Any snapshot
tagged with the boundary sentinel is excluded; everything else qualifies
— no per-commit append step. The FLO-328 SHA whitelist required
prospective operator discipline that did not scale (see FLO-334 Phase 1
audit for evidence); the time-boundary sentinel replaces it.

**When to reset the boundary** — rare. Only for major system-level
inflections that invalidate prior learnings wholesale (AI-model swap,
fundamental decision-schema change, pillar-weight rewrite). Procedure:

1. Tag the legacy snapshots with the new boundary value (one-time backfill
   script pattern — see `scripts/backfill_system_version.py` for reference).
2. Update `config.LESSONS_ERA_BOUNDARY` to the new sentinel.
3. Lessons rebuild over 3–5 days as new post-boundary trades accumulate.

To re-include legacy trades for one-off analysis, set
`LESSONS_ERA_BOUNDARY` to a sentinel value that no snapshot carries
(e.g. `"__disabled__"`) — this effectively disables the era filter.

**Silent-failure detection (FLO-334):** if `get_relevant_lessons()`
aggregates zero snapshots because all were excluded by the era filter,
it logs a single `LESSONS_ERA_FILTER_DEGRADED | ... era boundary may be
stale` WARN per process lifetime. If you see this, verify the boundary
value matches what snapshots actually carry.

`trade_lessons.json` is retained as an audit log (written by
`extract_trade_lesson` on every trade close) but is no longer the source
of truth for Floki's lessons — the on-read aggregation from
`trade_conditions/` + `history.db` is canonical.

## Ticket Convention

FLO-NNN format. Commits: `fix: FLO-XXX — description` or `feat: FLO-XXX — description`. Tracked in Linear (Floki Watch team). Known open issues: FLO-96 (timezone audit — mostly done: calendar/executor/sage fixed, remaining: verify all DB timestamps), FLO-140 (P1 backlog), FLO-146 (dead VIX feature — 16 files still reference VIX).

## General Coding Principles

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
