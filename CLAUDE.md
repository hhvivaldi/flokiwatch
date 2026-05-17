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

## Deprecated subsystems

- **Simba (`agent_monitor.py`, `simba_watcher.py`) — deprecated 2026-05-04 (CEO directive Option A).** Out-of-cycle wake/watch detection. The Simba process still runs in the bot for backward compatibility (no breaking changes), but its outputs land nowhere actionable:
  - `SIMBA_WAKE` / `SIMBA_WATCH` events route to the Trade Manager (currently disabled — `TRADE_MANAGER_ENABLED=False` gate at `trade_manager.run_cycle` makes them inert NO_OPs, no Qwen call).
  - The two Floki-side tools that wrote conditions for Simba to evaluate (`set_watch_conditions`, `set_wake_conditions`) have been removed from Floki's roster. Encode equivalent semantics as **Snow exit contingencies** instead — Snow's per-tick (~5s) evaluation against `price_above` / `price_below` / `mfe_reached` / indicator-side primitives covers the same wake-on-condition surface.
  - The agent_tools.py method bodies for the removed tools are RETAINED (no breaking changes for non-roster callers / tests).
  - Routing wires (`SIMBA_WAKE` in `main.py:tm_allowed`) intentionally preserved — re-enabling Simba is a config flag flip, not a code restoration.
  - Discord `#simba-watch` channel removed by operator. Trade Room UI Simba section will go quiet (no errors, just no events streaming).
  - Replacement: Snow contingency engine (`snow/snow_loop.py` + `snow/evaluators/`).
  - Removal target: future ticket once Snow's contingency surface is confirmed to cover all use cases (target window: after the next 3-5 trade reviews validate coverage).

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
- **Chart image pruning (FLO-420, CEO 2026-05-06).** When `get_chart_screenshots` is called, the 6 PNG screenshots ride history for exactly ONE reasoning turn: iter N (chart fetch) → iter N+1 API call sees images → at top of iter N+2, `_apply_chart_prunes` (`ai_agent.py`) replaces every `image_url` block with a text placeholder `[chart XAUUSD {tf} — shown at {iso_ts}, visual analysis incorporated]`. Saves ~20–25k tokens per subsequent iteration. System+tools cache breakpoint at messages[0] is unaffected; only the per-message prefix cache resets at the prune point (one-time miss ≈ recurring savings of 12+ iterations per cycle). `_chart_prunes_pending` list supports multiple pending image messages if Floki re-fetches charts mid-cycle. `FLOKI_CHART_PRUNE` log emits iter_appended/iter_pruned/msg_index/images_pruned/placeholder_est_tokens for cost forensics.
- **Score system:** 0-100. 50=neutral. >65=BUY. <35=SELL. 45-55=HOLD.
- **Active thesis persistence:** `data/active_thesis.json` — carries between cycles.
- **Boss notes (FLO-303):** `data/boss_notes.json` — Hermano's directives to Floki. Active notes injected as `<boss_notes>` block at top of user message each cycle. Floki returns `acknowledged_boss_notes: [id, ...]` to stamp them as read. Default 24h auto-expiry, `expires_at:null` for permanent, `ack_dismisses:true` for one-shots. Add notes via `boss_notes.add_note(text, ...)` or edit the file directly. Non-blocking: missing/malformed file is a silent no-op.

## Critical Safety Rules

- No simultaneous BUY+SELL (FLO-85 hard gate, `is not None` check not truthiness)
- Max 3 positions. No trades 60 min before/after market open/close. Max 6% daily loss.
- Volatility guard: M5 >1.8% blocks trades (`volatility_guard.py`)
- adjust_trade: SL-widening guard + rate limit (max 3/hour/ticket, FLO-141)
- **Monotonic SL invariant (FLO-419, executor.py modify_position):** SL never loosens. BUY: new_sl >= current_sl. SELL: new_sl <= current_sl. Exception: first SL set when current is 0. Loosening attempts return `OrderResult(success=False, error_code=-5)` with a `SL_GUARD` warning log. Universal — covers Snow, Qwen TM, monitor.py, EA bridge, and any future caller.
- **Escola 2 SL architecture (FLO-419 Phase 3, CEO 2026-05-01 evening, supersedes the FLO-419 hybrid).** Claude authors the full SL policy in each plan (BE trigger + optional trail). Snow executes mechanically. The Qwen Trade Manager is OFF (`TRADE_MANAGER_ENABLED=False` gates the heartbeat thread spawn at `main.py` — the receiver-side handler stays in place for future re-enable). Validator (`snow/validator.py:_check_management_hybrid_constraints`) permits up to TWO management contingencies: `move_sl_to_breakeven` and/or `trail_sl`, each requiring `mfe_reached.pips > 0`. `adjust_sl` and `move_sl_to_price` remain rejected. Empty `management` is rejected unless TP-distance-from-entry < 100 pips (the PLAN-036/037 opt-out pattern this rule exists to prevent). Claude picks one of two rules per plan: Option A — BE when MFE reaches 60% of TP distance; Option B — BE when MFE reaches 1R (= SL distance). After BE, optionally trail at a fixed distance behind price (typ. 100-150p). The monotonic SL guard at `executor.modify_position` (commits a9a8f4a + 7a1a1c9) blocks trail_sl from walking SL backward — the failure mode that motivated banning trail in the previous iteration is now caught at the bottleneck. Empirical motivation for the Escola 2 pivot: PLAN-042 (Gemini-era SELL) made +125p MFE then closed at +11p when Qwen TM fired CLOSE_TRADE on a regime flip; one regime-driven close erased a winning trade.
- **Exit-vs-SL geometry rule (FLO-419, CEO 2026-05-04).** Exit contingencies that use a price-side trigger MUST be positioned to fire BEFORE the broker SL: BUY plan exits with `price_below level` require `level > initial_sl`; SELL plan exits with `price_above level` require `level < initial_sl`. Boundary case `level == initial_sl` is also rejected (provides no earlier capture). Enforced by `snow/validator.py:_check_exit_geometry_vs_sl`. Empirical motivation: PLAN-20260504-009 (BUY entry 4574, SL 4543, exit price_below 4525 = 18 USD past SL) lost -$65 with the thesis_invalidation exit never armed; audit of last 10 closed plans showed 4 broken plans + 2 boundary cases. The opposite shapes (BUY+price_above, SELL+price_below) are TP-side triggers and have no SL-ordering constraint. For "trade reversed after going favorable" semantics, use `profit_retraced_from_peak` or the `mfe_reached + profit_pips below 0` AND combination — see agent_prompts.py FAILED-RECOVERY EXITS section.
- **ADX-override (FLO-430, CEO 2026-05-17).** Path B of the regime gate. Independent of regime label and confidence tier — rejects counter-trend plans when `adx >= 30` AND `d1_direction == h4_direction` (both bullish or both bearish per `_get_mtf_trend_direction` price-vs-EMA50 check) AND plan direction opposes the stack. Catches the case FLO-427 misses: regime_detector returns confidence="moderate" 64% of the time (memory 9801), so the FLO-427 confidence floor silently allowed PLAN-20260514-009 — a BUY with ADX 46.87 and full bearish multi-TF EMA stack labelled `regime=TRANSITIONAL`. The fix wires `d1_direction`/`h4_direction` through `regime_detector.detect_market_regime()` return dict → `main.py:_last_regime_context` → `agent_tools._author_regime` → `_check_adx_override` helper. Path B is checked in two places inside `_check_regime_counter_trend_gate`: when regime is NOT trending (catches mislabelled-regime cases) and when regime IS trending but confidence is below "high" (catches low-confidence-tier cases). Fail-soft when `d1`/`h4` are absent (legacy snapshots or transient brain failures). Rejection emits `regime_gate:` prefix marked `FLO-430` and includes ADX + D1/H4 alignment in the message so Floki can reorient.
- **Give-back exit calibration (FLO-429, CEO 2026-05-17).** Snow's validator gates `profit_retraced_from_peak` exits two ways: (a) in `TRENDING_BULLISH`/`TRENDING_BEARISH` regimes the contingency is rejected entirely (let SL + duration_cap + optional trail_sl handle exits — trends are M5-noisy and give-back fires on routine pullbacks), and (b) in all other regimes the threshold must be ≥ 3.0 × live M5 ATR(14) pips. M5 ATR is fetched live via `mt5_safe` at validation time; one `copy_rates_from_pos` per validate call. Fail-open on missing author_regime or MT5 hiccup (logs `GIVE_BACK_CAL_DEGRADED` WARN). Rejection emits `give_back_calibration:` prefix with the offending contingency name(s) and the ATR/min-required numbers. Empirical motivation: PLAN-20260515-010 (SELL, TRENDING_BEARISH, ADX 47) reached +104p MFE then a 2-minute reversal spike retraced 150p from peak and fired give_back at -55p; gold subsequently fell another 215p favorable. Same trade with no give_back would have closed via SL/duration at deep profit.
- **Active-plan cap (FLO-428, CEO 2026-05-15).** Snow's validator hard-rejects any plan submission when the in-flight count (status ∈ {pending, active, triggered, closing}) is already at `MAX_ACTIVE_PLANS=2`. Belt-and-suspenders against the `list_active_plans` WAL-visibility bug — overnight 2026-05-14/15, Floki's tool returned `count=0` for two consecutive cycles despite pending plans living in the DB, and Floki stacked 4 SELL plans (entries at 4581/4607/4635/4645) on the assumption that prior plans had expired. The validator does its own fresh `snow.db.get_active_plans()` count at validation time, excludes the row being submitted (self-id), and rejects with `active_plan_cap:` prefix that names the live plan IDs. Fail-open on DB error (emits `ACTIVE_PLAN_CAP_DEGRADED` WARN). Rejection tells Floki to `cancel_plan(<id>)` on a live plan first or wait for fire/expire/close before authoring more. Separate ticket pending for the underlying WAL-visibility bug — until then, this cap is the live safety.
- **Counter-trend regime gate (FLO-427, CEO 2026-05-14).** Snow's validator hard-rejects counter-trend plans in confirmed trending markets. Gate fires when ALL of: `regime ∈ {TRENDING_BULLISH, TRENDING_BEARISH}`, `confidence ∈ {high, strong}`, `adx >= 25`, and plan direction opposes regime (BULLISH→SELL or BEARISH→BUY). In RANGING / BREAKOUT_IMMINENT / VOLATILE / QUIET / TRANSITIONAL, both directions are permitted. The regime snapshot is captured at plan-author time from `self._bot._last_regime_context` (set by `main.py` from `regime_detector.detect_market_regime()`) and passed to `validate_plan(plan_dict, author_regime=...)` in `agent_tools.submit_plan_to_snow`. Fail-open: missing/UNKNOWN snapshot logs `REGIME_GATE_DEGRADED` WARN and allows the plan through (paralysis risk on transient MT5/Brain hiccups outweighs fail-closed value). Rejection emits `regime_gate:` prefix with regime/ADX/confidence/timestamp so Floki can reorient on the next iteration. Empirical motivation: May 1-4 forensics — 45% of losses were counter-trend SELL plans on bullish days (4 SELLs lost on May 1, a TRENDING_BULLISH day). Companion change: `paired_hedge` removed from `snow.schema.SetupType` Literal; `agent_prompts.py` rewritten to drop "scenario map" framing, cap concurrent plans at 2 SAME-direction, and promote anti-hedge guardrail to top-level rule.
- **Entry conditions must use fixed prices (FLO-419, CEO 2026-05-04).** `price_at_sr_zone`, `price_at_pivot`, and `price_at_fibonacci` are REJECTED in `entry.conditions` by `snow/validator.py:_check_no_dynamic_level_in_entry`. They resolve their target price from the live SemanticCache at trigger time — the trigger silently shifts whenever Brain re-ranks the nearest zone / pivot / fib level, firing at a price the plan's thesis never analyzed. Plans must commit to the literal number using `price_above {level: N}` or `price_below {level: N}`. These primitives remain permitted in `exit` and `management` blocks where live-structure semantics are intentional. Empirical motivation: PLAN-20260503-001 authored with the 4605-4612 support cluster in mind but used `price_at_sr_zone tolerance_pips=8` — would have fired at any drifted "nearest support" Brain surfaced later, regardless of the multi-confluence thesis.

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
- **Floki provider switch (FLO-384 / FLO-389 / FLO-419 Phase 2 / FLO-426):** `LLM_PROVIDER` env (`qwen` | `kimi` | `gemini` | `openai` | `anthropic` | `agent_sdk`, default `qwen`). For `agent_sdk` (FLO-426): Floki routes through `claude-agent-sdk` + the bundled Claude Code CLI, billing against the Max subscription pool instead of API credits. Entry point is `floki_agent_sdk_path.decide_via_agent_sdk` invoked from a branch at the top of `ai_agent.AIAgent.decide()`. The path masks `ANTHROPIC_API_KEY` from the subprocess (required — SDK otherwise inherits and routes to API-credit billing), passes Floki's 70k-char system prompt via `system_prompt={"type":"file","path":...}` (Windows argv limit workaround), uses `setting_sources=[]` to strip Claude Code's default scaffolding, and wraps `_tool_schemas()` + `SUBMIT_DECISION_TOOL` as `@tool`-decorated MCP closures via `make_sdk_tools()`. `get_chart_screenshots` emits MCP `image` content blocks from `tools._chart_images` so Floki keeps chart vision. Loop terminates on `submit_decision`; the tool's args become the `content` field of the response dict so `_parse_response_with_retry` consumes it unchanged. Any SDK exception falls through to the existing direct-API path (`floki_anthropic_adapter` via `_call_openai_with_tools`). Known behavioural diffs vs the direct path: 5-min cache TTL (no public knob), no FLO-420 chart pruning (charts ride history; ~+250k cache_read/cycle), no FLO-409 action-tool priority reordering inside batches. Pre-flight findings in `memory/project_agent_sdk_migration_preflight.md`. For `kimi` / `gemini` / `openai`, config resolves the FLOKI_API_BASE/KEY/MODEL triple from provider-specific env vars and the existing OpenAI-compat client path consumes it unchanged. For `anthropic` (FLO-419 Phase 2): config resolves to `claude-opus-4-6` via `ANTHROPIC_MODEL`/`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`, and `ai_agent.py` initialises the native Anthropic SDK (`anthropic.Anthropic(...)`) instead of `OpenAI(...)`. Calls route through `floki_anthropic_adapter.call_anthropic_with_oai_kwargs` which converts OpenAI-format kwargs ↔ Anthropic API at the wire boundary — the agentic loop, message list, tool schemas, and OpenRouter fallback all stay OpenAI-shaped. Prompt caching (1h ephemeral TTL) is wired automatically on system + tools — empirical 98% cache hit on second call within the window. Fallback (FLO-299) stays on OpenAI-compat (Qwen/OpenRouter) regardless of primary — cross-provider fallback is intentional. Bot restart required after flipping.

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
