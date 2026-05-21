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

## Architecture — 6 Agents (FLO-434 reduced from 8)

| Agent | Model | File | Role |
|-------|-------|------|------|
| Floki | Claude Opus 4.6 | `ai_agent.py` | Sole trading decisor. Self-schedules 5-30 min. |
| Rex Monitor | Python (deterministic) | `rex_monitor.py` | Background scan every 30 min — divergence / correlation / regime / session findings. Floki pulls via get_rex_monitor. |
| Simba | Python | `agent_monitor.py` | Watchdog. 30s polling. Wakes Floki. (Deprecated — see Deprecated subsystems.) |
| Sage | Gemini | `sage_auditor.py` | Daily auditor at 21:00 UTC. |
| Echo | MiMo-V2-Flash | `echo_sentinel.py` | News sentinel. 25 RSS feeds. PULL-only. |
| Luna | MiMo-V2-Flash | `luna_analyst.py` | Macro analyst. MT5+Yahoo+FRED. Observational output only (no env/risk/bias labels post-Bug G — Floki interprets). |
| Brain | Python | `central_brain.py` | Data pipeline. 5-pillar analysis. No decisions. |

**FLO-434 removed from the cycle (2026-05-17):** Rex Bull, Rex Bear, Research Manager. All three used weaker models than Floki (Claude Opus 4.6) and produced advisory output Floki either ignored or contradicted. The conversational Rex debate is gone (`debate_with_rex` tool deleted); the verdict-file machinery is gone (`get_oracle_verdict` tool deleted, `data/oracle_verdict.json` no longer written, `RM_VERDICT` events no longer recorded). `rex_validator.py` and `research_manager.py` are retained on disk for potential future re-enable but no live call path reaches them. The Rex monitor (deterministic 30-min scan, no LLM) stays — it's a useful observational source Floki polls when he wants it.

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

- **Floki is sole decisor.** Brain → Screenshots → Floki (Claude Opus 4.6) → Plans. No intermediaries (FLO-434, 2026-05-17).
- **EA is pure executor.** `FLOKI_MANAGES_POSITION = True`. 9999-pip triggers never fire.
- **Echo is pull-based.** Floki pulls alerts via tool, Echo does not push.
- **Rex monitor (FLO-211 / FLO-316):** Runs 4 tools every 30 min (divergence, correlation, regime, session). No LLM — deterministic classifier. Writes `data/rex_monitor.json`. Floki pulls via `get_rex_monitor`. FLO-316 removed prescriptive `alert_level` (QUIET/NORMAL/ELEVATED/CRITICAL) + `alert_context` + `alert_hint` + per-finding `severity` + `implication` fields. Output is now observational: each finding is `{type, observation, data}` (type ∈ DIVERGENCE/CORRELATION/REGIME/SESSION). Simba wake now gates on `findings_count >= 2` instead of `alert_level == CRITICAL` (2h debounce preserved). Bull/Bear debate injection of monitor findings already removed in Commit 1 (FLO-243 decoupling).
- **Session block removed (FLO-317, Fase 2 of FLO-314):** `<session>` XML block (`today_trades` / `today_wins` / `today_losses` / `today_pnl` / `last_5_results` / `consecutive_losses`) no longer injected into Floki's prompt. Rationale: running day-W/L produced a WAIT death spiral (6-day climb 45%→94%). Forced-injection caution vector per Escola 1 v2.0. `<open_positions count="..">` preserved (FLO-85 opposing-positions guard + max-positions risk manager). `get_session_context` tool untouched — Floki can still pull session data when he chooses (agency preserved). `_format_session_context()` helper deleted; `session_context: Dict` removed from `build_data_package` / `build_proactive_data_package` signatures.
- **Chart image pruning (FLO-420, CEO 2026-05-06).** When `get_chart_screenshots` is called, the 6 PNG screenshots ride history for exactly ONE reasoning turn: iter N (chart fetch) → iter N+1 API call sees images → at top of iter N+2, `_apply_chart_prunes` (`ai_agent.py`) replaces every `image_url` block with a text placeholder `[chart XAUUSD {tf} — shown at {iso_ts}, visual analysis incorporated]`. Saves ~20–25k tokens per subsequent iteration. System+tools cache breakpoint at messages[0] is unaffected; only the per-message prefix cache resets at the prune point (one-time miss ≈ recurring savings of 12+ iterations per cycle). `_chart_prunes_pending` list supports multiple pending image messages if Floki re-fetches charts mid-cycle. `FLOKI_CHART_PRUNE` log emits iter_appended/iter_pruned/msg_index/images_pruned/placeholder_est_tokens for cost forensics.
- **Score system:** 0-100. 50=neutral. >65=BUY. <35=SELL. 45-55=HOLD.
- **Active thesis persistence:** `data/active_thesis.json` — carries between cycles.
- **Boss notes (FLO-303):** `data/boss_notes.json` — Hermano's directives to Floki. Active notes injected as `<boss_notes>` block at top of user message each cycle. Floki returns `acknowledged_boss_notes: [id, ...]` to stamp them as read. Default 24h auto-expiry, `expires_at:null` for permanent, `ack_dismisses:true` for one-shots. Add notes via `boss_notes.add_note(text, ...)` or edit the file directly. Non-blocking: missing/malformed file is a silent no-op.

## D1 Bearish Trend Score & counter-trend gate (FLO-452, CEO 2026-05-21)

Multi-factor structural-trend gate to stop Floki chasing M15 reversal narratives (double bottoms, RSI divergence) against a bearish daily structure — 3 counter-HTF BUYs (PLAN-006/021/005) lost a net -$39 while price sat ~4.5% below the D1 EMA50, and the FLO-427/430 regime gate missed them (it only fires in CONFIRMED-TRENDING; these were RANGING/TRANSITIONAL-labelled).

- **`regime_detector.compute_d1_trend_score(d1)`** — PURE 8-factor score (each weighted): close<EMA50 (.10), <EMA50 3+ bars (.10), EMA50 slope<0 (.15), close<EMA200 (.15), death cross (.10), (EMA50−close)>0.5×ATR (.10), ADX>25 & −DI>+DI (.15), swing LH+LL (.15). All true → 100. Returns `{direction, score, bearish_score, bullish_score, factors, bullish_factors}`. Symmetric bullish set. `build_d1_trend_score()` assembles inputs from live D1 candles (mt5_safe; ATR/EMA via pandas, Wilder ADX, fractal swings); fail-soft None.
- **Layer 1 (inject):** `detect_market_regime()` adds `d1_trend_score` to its return → `_last_regime_context` → `state_writer` writes `bot_state.json:d1_trend_score`.
- **Layer 2 (prompt):** `agent_prompts.py` "STEP 0 — HTF STRUCTURE CHECK" — mandatory pre-authoring; if bearish_score≥70 candidate actions are SELL/HOLD only unless 3+ exceptions cited.
- **Layer 3 (gate):** `snow/validator._check_d1_trend_gate` — BUY + opposing bearish_score ≥ `_D1_GATE_THRESHOLD` + `<3` cited `analysis.counter_trend_exceptions` → REJECT (`D1_TREND_GATE` log). Wired via `validate_plan(author_d1_trend=...)` ← `agent_tools` reads `_last_regime_context['d1_trend_score']`. Symmetric for SELL.
- **⚠️ COUNTERFACTUAL FINDING (must tune before it does anything):** with the spec threshold **70 the gate is INERT** on the recent counter-HTF BUYs — gold is ABOVE its rising 200-day EMA (no close<EMA200, no death cross) and ADX<25, so 3 factors (0.40) are FALSE and the score caps at **~60**. At **threshold 55** the 3 losing BUYs REJECT and the 2 SELL winners ALLOW (matches reality: saves -$62, forgoes +$23 = +$39 net). **`_D1_GATE_THRESHOLD` is set to 55 (CEO-calibrated 2026-05-21)** for this medium-term-pullback-within-long-term-uptrend structure. It self-tightens: if gold enters a full bear market (below EMA200 + death cross + ADX≥25) the score rises >70 naturally. Re-evaluate the constant if the macro structure flips.
- **Wiring fix (2026-05-21):** the gate DEGRADED on every real plan (002, 003) — `author_d1_trend` was None at validation because `_last_regime_context` intermittently lacked `d1_trend_score` (a cycle's `build_d1_trend_score()` hit an MT5 None). Two fixes: (a) `regime_detector._LAST_D1_TREND_SCORE` caches the last good score so an intermittent MT5 None returns the cache, not None; (b) `agent_tools.submit_plan_to_snow` falls back to reading `bot_state.json:d1_trend_score` when `_last_regime_context` lacks it (same cheap-read as `_build_specialist_context`). Tests in `test_flo452_wiring.py`.
- **⚠️ GAMEABILITY:** exceptions are Floki-CITED and counted, not deterministically verified — a biased planner can list 3 to bypass. Layers 1+2 add friction; deterministic verification of the 5 exception conditions is the follow-up. Tests: `test_flo452_d1_trend.py` (6 score + 5 gate + counterfactual table). One pre-existing unrelated validator test fails (`get_snow_primitives_reference` not in SYSTEM_PROMPT — strings live in a non-SYSTEM_PROMPT var at agent_prompts.py:707/769; predates FLO-452).

## Setup-regime matrix + mandatory thesis-break exit (FLO-453, CEO 2026-05-21)

Solves "right mechanics, wrong setup": PLAN-003 (SELL `continuation_momentum` in a RANGING ADX-20 tape) lost -$48 — no momentum to continue, and no exit when the breakdown level was reclaimed, so it bled to SL. Three coupled fixes:

- **`SETUP_REGIME_MATRIX` + `_check_setup_regime_gate` (snow/validator.py):** maps each `setup_type` to an H1-ADX window `{min_adx, max_adx, require_adx_rising}` and REJECTs mismatches. `continuation_momentum`/`breakout_range`/`session_open_break` need ADX≥22 AND **rising**; `pullback_trend` 18-60; `structural_bounce` ≤25; `mean_reversion_extreme` ≤20; ranges ≤22. Wired via `validate_plan(author_setup_ctx={adx, adx_rising})` ← `agent_tools` reads H1 ADX + `adx_change_4bars` sign from `bot_state.json:multi_tf_indicators.H1`. `SETUP_REGIME_GATE` log; fail-open on missing ADX. **⚠️ Calibration: pullback `max_adx` raised 40→60 from the counterfactual** — the two winning pullbacks (PLAN-004 ADX 43, PLAN-019 ADX 42) sit above 40; strong trends are where pullbacks WIN, so 40 false-rejected the best trades. Counterfactual now: PLAN-003 (continuation@20)→REJECT, all winners→ALLOW.
- **`_check_thesis_break_exit` (snow/validator.py):** `continuation_momentum`/`breakout_range`/`session_open_break` plans MUST carry a structural-invalidation exit — `price_above` (SELL) or `price_below` (BUY) somewhere in management/exit — else REJECT (`THESIS_BREAK_MISSING` log). PLAN-003 had none; a `price_above ~4515` exit would have closed it ~-$17 instead of -$48. (Placement still governed by FLO-419 geometry: SELL price_above < SL.)
- **Regime-first prompt cascade (agent_prompts.py):** STAGE 1 classify H1 regime → STAGE 2 constrained SETUP MENU (only setups whose ADX window fits) → STAGE 3 author (+ mandatory thesis_break exit for momentum setups). Constrains the menu BEFORE Floki pattern-matches. Plus an informational gold-session-character note (Asian=range, overlap=expansion).
- **⚠️ GAMEABILITY:** `setup_type` is self-reported — Floki could relabel `continuation_momentum`→`pullback_trend` to dodge the matrix. The Stage-2 prompt menu is the upstream mitigation; a deterministic setup-classifier is a future ticket. Tests: `test_flo453_setup_regime.py` (4 matrix + 3 thesis-break + 6-plan counterfactual). One PRE-EXISTING unrelated validator test fails (`get_snow_primitives_reference`, predates FLO-452/453).

## Philosophy pivot: discretionary chart reading + soft gates (FLO-454, CEO 2026-05-21)

Reframes Floki from gate-heavy indicator-checking to **professional discretionary chart reading**, on the thesis (research-backed: FinAgent, Chroma Context-Rot, CFA/FTMO risk frameworks) that LLMs read slope/structure well and indicator-panel walls poorly, and that hard-blocking judgment calls is the wrong tool. Three changes:

- **Prompt — 6-step READING ORDER (agent_prompts.py).** Replaced the planning blocks (FLO-435 top-down, FLO-452 STEP 0, FLO-453 STAGE 1/2/3 cascade, FLO-427 regime-alignment) with: 1. CONTEXT (H4) → 2. STRUCTURE (H1) → 3. LOCATION (M15) → 4. NARRATIVE → 5. TRIGGER (M15) → 6. CONFLUENCE COUNT, plus a describe-before-decide CHART READING template and TWO IRON RULES. Emphasis on BOS/CHOCH/Wyckoff structure + a one-sentence narrative over indicator readouts. **SYSTEM_PROMPT shrank 90,671→~84,700 chars.**
- **Charts cut 6→3 TFs: H4 (bias) / H1 (setup) / M15 (entry).** `_CHART_TFS` (agent_tools.py) and `_CHART_TF_MAP` (main.py) drop D1/M5/M1 — only 3 PNGs requested from the EA and sent to Floki. **Indicator-panel cleanup (MACD/BB/Stoch/ADX off; keep EMA50/200, RSI-14 on M15 only) is a MANUAL MT5 chart-template change** — `FlokiBridge.mq5` does `ChartScreenShot()` of whatever the terminal template displays; it is NOT code-settable. Operator must apply clean templates in MT5.
- **Gate reclassification — 5 HARD + 4 SOFT.** HARD (still block, in `validate_plan` `errors +=`): FLO-439 daily loss, FLO-428 plan cap, FLO-445 SL buffer, FLO-436 news blackout, FLO-452 D1 trend gate (the hard counter-trend backstop) — plus FLO-453 `thesis_break` (deterministic plan completeness). SOFT (run + log `*_SOFT_WARNING`, do NOT block): FLO-427 regime + FLO-430 ADX-override (both inside `_check_regime_counter_trend_gate`), FLO-453 `setup_regime` matrix, FLO-429 give-back. Softening is at the `validate_plan` call site (gate detector functions unchanged — they still compute + log; the call site demotes their objection to an advisory warning). Rationale: regime/setup-fit/give-back are judgment inputs Floki weighs in the CONFLUENCE COUNT, not enforcement. **Net effect: counter-trend trades are no longer hard-blocked except by the FLO-452 D1 gate** — watch the `*_SOFT_WARNING` adherence data before deciding whether any soft gate should return to hard. Tests: `test_flo454_soft_gates.py` (hard-still-block / soft-demoted / detectors-still-run / hard-d1-rejects). Two chart-suite tests updated from the 6-TF to the 3-TF contract.

## Critical Safety Rules

- No simultaneous BUY+SELL (FLO-85 hard gate, `is not None` check not truthiness)
- Max 3 positions. No trades 60 min before/after market open/close. Max 6% daily loss.
- Volatility guard: M5 >1.8% blocks trades (`volatility_guard.py`)
- adjust_trade: SL-widening guard + rate limit (max 3/hour/ticket, FLO-141)
- **Monotonic SL invariant (FLO-419, executor.py modify_position):** SL never loosens. BUY: new_sl >= current_sl. SELL: new_sl <= current_sl. Exception: first SL set when current is 0. Loosening attempts return `OrderResult(success=False, error_code=-5)` with a `SL_GUARD` warning log. Universal — covers Snow, Qwen TM, monitor.py, EA bridge, and any future caller.
- **Escola 2 SL architecture (FLO-419 Phase 3, CEO 2026-05-01 evening, supersedes the FLO-419 hybrid).** Claude authors the full SL policy in each plan (BE trigger + optional trail). Snow executes mechanically. The Qwen Trade Manager is OFF (`TRADE_MANAGER_ENABLED=False` gates the heartbeat thread spawn at `main.py` — the receiver-side handler stays in place for future re-enable). Validator (`snow/validator.py:_check_management_hybrid_constraints`) permits up to TWO management contingencies: `move_sl_to_breakeven` and/or `trail_sl`, each requiring `mfe_reached.pips > 0`. `adjust_sl` and `move_sl_to_price` remain rejected. Empty `management` is rejected unless TP-distance-from-entry < 100 pips (the PLAN-036/037 opt-out pattern this rule exists to prevent). Claude picks one of two rules per plan: Option A — BE when MFE reaches 60% of TP distance; Option B — BE when MFE reaches 1R (= SL distance). After BE, optionally trail at a fixed distance behind price (typ. 100-150p). The monotonic SL guard at `executor.modify_position` (commits a9a8f4a + 7a1a1c9) blocks trail_sl from walking SL backward — the failure mode that motivated banning trail in the previous iteration is now caught at the bottleneck. Empirical motivation for the Escola 2 pivot: PLAN-042 (Gemini-era SELL) made +125p MFE then closed at +11p when Qwen TM fired CLOSE_TRADE on a regime flip; one regime-driven close erased a winning trade.
- **Exit-vs-SL geometry rule (FLO-419, CEO 2026-05-04).** Exit contingencies that use a price-side trigger MUST be positioned to fire BEFORE the broker SL: BUY plan exits with `price_below level` require `level > initial_sl`; SELL plan exits with `price_above level` require `level < initial_sl`. Boundary case `level == initial_sl` is also rejected (provides no earlier capture). Enforced by `snow/validator.py:_check_exit_geometry_vs_sl`. Empirical motivation: PLAN-20260504-009 (BUY entry 4574, SL 4543, exit price_below 4525 = 18 USD past SL) lost -$65 with the thesis_invalidation exit never armed; audit of last 10 closed plans showed 4 broken plans + 2 boundary cases. The opposite shapes (BUY+price_above, SELL+price_below) are TP-side triggers and have no SL-ordering constraint. For "trade reversed after going favorable" semantics, use `profit_retraced_from_peak` or the `mfe_reached + profit_pips below 0` AND combination — see agent_prompts.py FAILED-RECOVERY EXITS section.
- **Executor pre-flight dedup against double-spawn race (FLO-446 follow-up, FLO-448 commit, CEO 2026-05-19).** Empirical motivation: PLAN-20260519-001 triggered at 07:36:49 UTC and the executor spawned TWO market positions within 100ms (tickets 1655939235 and 1655939305). `GHOST_GUARD_B` in `executor.py` caught it 2 seconds later and closed the duplicate at -$0.34, but only after MT5 had filled both. Historical count of `GHOST_GUARD_B` fires: **15 across ~25 trading days** (~0.6/day). This is the EA-late-arrival race: executor tries the EA bridge, times out, falls back to direct MT5 placement, succeeds — and then the EA actually delivers the original request late. **New pre-flight guard** in `snow/actions.SnowActions._dispatch_execute_market`: before transitioning PENDING→TRIGGERED, two checks reject any duplicate attempt: (a) is there already an MT5 position with `comment == 'snow:{plan_id}'`? (b) was `execute_market` for this `plan_id` attempted within `_DEDUP_COOLDOWN_SECS=5.0` of now (process-local `_recent_executor_calls` dict)? Rejection emits `EXECUTOR_DEDUP | plan_id=X | rejected duplicate execute_market | existing_ticket=Y | …` WARN, records a SKIPPED_GUARD trigger row, and returns ActionResult without transitioning plan status (stays PENDING so a future legitimate fire can still take it). `GHOST_GUARD_B` remains the post-flight safety net for cases that slip past the pre-flight. **Fail-soft:** any internal error in the dedup check falls back to the existing GHOST_GUARD_B path. 7 unit tests in `snow/tests/flo446_executor_dedup_test.py`. Full validator-gate suite (83 tests) unaffected.
- **Trade Room P&L reconciliation — daily_stats.pnl + trade_history from authoritative DB (FLO-447, CEO 2026-05-19).** Empirical motivation: PLAN-20260518-004 closed for actual realized **+$66.51** (partial $40.93 at 05:22 + runner $25.58 at 08:04) but Trade Room showed **$40.59** for `daily_stats.pnl`. Two bugs compounded: (a) Snow's `close_partial` action fires the executor directly and never emits a monitor action, so the bot's `_monitor_cycle` daily-stats counter never sees Snow partials; (b) when a ticket has multiple OUT deals (partial + runner-close), `db_writer.record_trade_close`'s `UPDATE trades SET profit=?` overwrites profit on each call rather than accumulating, so `trades.profit` holds only one fragment. `snow_plans.outcome_usd` correctly captured the aggregate via the runtime_reconcile path (value-weighted close-price sum across all OUT deals) but state_writer was reading the bot's counter. **Fix:** `state_writer._recompute_today_pnl_from_db()` and `state_writer._build_trade_history()` recompute `daily_stats.pnl` and the `trade_history` array at every state write from authoritative DB sources — `snow_plans.outcome_usd` for plan-linked tickets, `trades.profit` for non-plan closes (ghost guards). When a ticket has a snow_plans aggregate, the in-memory `closed_trades_today` fragments for that ticket are suppressed to avoid double-counting. Live verification at commit time: all three sources (recompute helper, trade_history sum, MT5 ground truth) agree at $+66.17 for today. The bot's in-memory counter remains unchanged (used as fallback if the DB lookup fails).
- **regime_detector NameError fix — `mtf_d1`/`mtf_h4` threaded through `_build_result` (FLO-446, CEO 2026-05-19).** Root cause of the 533 `REGIME | detection error: name 'mtf_d1' is not defined` WARN lines today and the empty `market_regime` in `bot_state.json` since 2026-05-17: my FLO-430 commit added `"d1_direction": mtf_d1` and `"h4_direction": mtf_h4` to `_build_result()`'s return dict, but `mtf_d1`/`mtf_h4` are local to the *caller* (`detect_market_regime`), not in scope inside `_build_result`. Every Brain cycle since FLO-430 raised `NameError` inside the regime path, the caller swallowed it as `REGIME | detection error: ...`, and `self._last_regime_context` stayed at its prior value (or None on first call after restart). Downstream effect: `state_writer` skipped writing `market_regime` to `bot_state.json` (guard at line 316 requires `_regime.get("regime")`), and `agent_tools.submit_plan_to_snow` built `_author_regime=None`, so FLO-427/430 regime gates ran DEGRADED 8× today (allowing every plan through unconditionally). All plans have been trend-aligned in the meantime so no counter-trend trade slipped through — the bug was inert but live. **Fix:** added `mtf_d1=None, mtf_h4=None` kwargs to `_build_result()` signature; updated all 10 call sites inside `detect_market_regime` to pass `mtf_d1=mtf_d1, mtf_h4=mtf_h4`. Live smoke test confirms the detector now returns `d1_direction`/`h4_direction` populated. 17 FLO-427/430 tests pass — no regression.
- **SL buffer from sweep envelope + sweep-vs-break prompt + partial-at-first-structure + BE sanity (FLO-445, CEO 2026-05-18).** Empirically motivated by PLAN-20260518-001 stop-hunt post-mortem (SL at 4582 = cluster top, two M5 wicks at 4584.24 fired the stop, price then fell 52 USD per thesis — counterfactual at duration cap was +$34 vs actual -$58). Four coupled changes shipped as one feature:
  - **New validator gate `_check_sl_buffer_from_structure`** in `snow/validator.py`. Reads `plan.analysis.key_levels` and the live M15 ATR (new helper `_fetch_m15_atr_pips` mirroring FLO-429's M5 version). For SELL plans, finds the highest key_level between entry and SL; for BUY plans, the lowest. Requires SL to sit at least `max(20p, 1.0 × M15_ATR)` beyond that level on the SL side. Rejection message names the offending level and the suggested SL price. Fail-soft on missing key_levels, missing M15 ATR, or no levels in the SL direction. Wired into `validate_plan` between FLO-429 and FLO-436. Sample rejection wording on PLAN-001 reproduction: *"sl_buffer: SL=4582 sits 0 pips from the nearest structural level on the SL side (4582). Required buffer at current M15 ATR is max(20p, M15 ATR) = 50 pips. Move SL to ≥ 4587."* Suggested-SL math is `struct_level ± required_buffer/10` (XAUUSD: 10 pips = 1 USD).
  - **STOP PLACEMENT prompt block** (`agent_prompts.py`) inserted above the existing STRUCTURAL STOPS block. Teaches the sweep envelope rule (`SL = level + max(20p, 1×M15_ATR)`) and adds the SWEEP-VS-BREAK distinction (M5 wick that closes back inside = sweep, hold; M5 close beyond with body + follow-through = real break, close). Notes that Snow's broker-side SL cannot make this judgment, which is why the FLO-445 buffer pushes the SL beyond the sweep envelope at plan-author time.
  - **PARTIAL CLOSE AT FIRST STRUCTURAL TARGET** prompt block expanded inside LADDERED TARGETS (FLO-437). Combines with FLO-442's `close_partial` management primitive. Canonical 2-contingency pattern: `close_partial 50%` at MFE = first-structural-target distance, paired with `move_sl_to_breakeven` at the same MFE. PLAN-001 counterfactual: would have closed at +$22 instead of -$58.
  - **BE TRIGGER SANITY rule** in the same block. If structural reward distance (entry → first major S/R on TP side) is less than SL distance, set BE at the structural distance, not at 1R. PLAN-001 had BE at MFE=280p but realistic MFE for that regime was 212p — BE was decoration. Correct setting was BE at MFE=110p (next H4 support at 4543, 110p below entry 4554).
  - **9 regression tests** in `snow/tests/flo445_sl_buffer_test.py` lock the gate: PLAN-001 verbatim reproduction (rejected with suggested SL=4587), buy-side mirror, exactly-at-threshold accepted, just-under-threshold rejected, 20p floor in low-vol regime, fail-open on missing M15 ATR, fail-open on absent SL-side key_levels. Live MT5 cross-check during commit: `_fetch_m15_atr_pips()` returned a sensible 30-80p range on this broker.
- **Self-consistency voter on plan submission (FLO-443, CEO 2026-05-18 — env-gated OFF by default).** Post-Pydantic-parse, pre-business-validator hook in `agent_tools.submit_plan_to_snow` that runs 5 parallel Anthropic API calls (Sonnet 4.6, temperature 0.7, ~200 token output each) on a compact summary of Floki's plan analysis, tallies votes, and: (a) **rejects** the plan with `self_consistency: ...` if no direction wins > 50% of valid votes (consensus = `DISAGREE`); (b) **rejects** the plan if the winning direction contradicts the plan's stated direction; (c) **mutates** `analysis.confidence` to the winning side's vote-share percent (e.g. 4/5 votes = 80) if consensus agrees. **Fail-soft** on SDK unavailability or majority-error votes — logs `SELF_CONSISTENCY_DEGRADED` and lets the plan through with original confidence. **Env-gated:** `FLO443_SELF_CONSISTENCY=on` enables; default is `off` (entire branch is a no-op without the flag). Inversion note (advisor-flagged): Sonnet voting on Opus-produced analysis is variance reduction (Wang et al. self-consistency), not a weaker-model override — same Opus output goes to all 5 votes; the ensemble narrows classification variance. New module `self_consistency.py`. Wiring in `agent_tools.py` is ~70 lines inside the `submit_plan_to_snow` try-block, between the `_author_account` build and the `_validate(...)` call.
  - **FLO-449 (CEO 2026-05-20): votes route through the Agent SDK, not the Anthropic API.** The original `anthropic.Anthropic(api_key=...)` path degraded on EVERY production call with `reason=no_anthropic_api_key` — the bot runs on subscription auth (FLO-426) with no `ANTHROPIC_API_KEY` in env, so the voter never cast a single real vote (35 `SELF_CONSISTENCY_DEGRADED` lines, 0 votes). The voter now uses `claude-agent-sdk` + the bundled Claude Code CLI (model `claude-sonnet-4-6`, `env={"ANTHROPIC_API_KEY": ""}`, `setting_sources=[]`) — the SAME subscription auth Floki's Opus planner uses, no key needed. `vote_on_plan` runs inside the SDK event-loop thread (tools dispatch there per `floki_agent_sdk_path._wrapped`), so the 5 concurrent SDK sessions are offloaded to ONE worker thread (`_run_votes`) with its own event loop via `ThreadPoolExecutor` + `loop.run_until_complete(asyncio.gather(...))` — calling `run_until_complete` on the planner's own loop thread would raise. Live smoke at commit: 1 real vote returned `SELL/conf 8` in ~5.2s, `degraded=False`. **Behavioural diff:** the SDK exposes no temperature knob, so vote variance now comes from Sonnet's default sampling rather than the prior `temperature=0.7`.
  - **FLO-450 (CEO 2026-05-21): confidence cap — the voter confirms or lowers, never inflates.** Once the SDK voter went live it stamped `confidence=100` on 4 consecutive plans (two SELL, two BUY, different regimes) because every ensemble voted unanimously and the mutation set `analysis.confidence = vote_share_pct` (100 for 5/5). That destroyed the signal's discriminating value. New helper `self_consistency._cap_confidence(plan_conf, vote_share_pct)` returns `min(plan_conf, vote_share_pct)` (falls back to vote share when planner conf ≤ 0). `ConsensusResult` gains a `vote_share_pct` field (raw winner share, pre-cap) so logs show both; `confidence_pct` is now the **applied** (capped) value. The `SELF_CONSISTENCY` log line in `agent_tools.py` now prints `vote_share=… applied_confidence=… plan_conf=…`. Net: a unanimous vote can at most *confirm* the planner's own number (76 stays 76, not 100); a split vote *lowers* it (3/5 → 60). 3 regression tests in `test_flo450_confidence_cap.py`.
  - **FLO-451 (CEO 2026-05-21): multi-specialist voter replaces the uniform ensemble.** The FLO-443/450 voter sent 5 identical Sonnets the same summary → 100% unanimity on every plan (the FLO-450 cap blunted the damage but the votes still carried no independent signal). FLO-451 swaps them for **5 specialists, each with a distinct system prompt + data lens**: NEWS (web search, today's events), MACRO (DXY/yields/VIX), HTF TECHNICAL (D1/H4 EMA50/EMA200 stack — no web search, gets `multi_tf_indicators` injected), SENTIMENT (COT/ETF/positioning, web search), and DEVIL'S ADVOCATE (runs *after*, sees the other 4 outputs, names the plan's weakest assumptions). Voters 1-4 run in parallel via `asyncio.gather`; the Devil runs sequentially. New public entry `self_consistency.run_specialist_vote(plan, context=, mode=)`. **Mode switch `FLO451_VOTER_MODE=shadow|confidence|block` (default shadow):** shadow = voters run + log `SPECIALIST_VOTE_SHADOW` (with `would_block`), plan proceeds unchanged; confidence = mutate `analysis.confidence` to `min(plan_conf, voter_avg)`; block = 3+ REJECT rejects the submission. **Aggregation (pure code):** 3+ APPROVE → proceed; 3+ REJECT → block/would_block; no clear majority → proceed with `min(plan_conf, avg×0.8)` (tie goes to Opus, lowered confidence); **3+ TIMEOUTS → SKIPPED** (original confidence kept). Per-voter timeout → ABSTAIN. A freshness/`NO_DATA` ABSTAIN is a legitimate "no opinion" and does NOT count toward the skip threshold — only true timeouts do (the active voters carry the verdict). Live-run tuning (2026-05-21): per-voter timeouts raised to 75s (web) / 60s (devil), orchestration 180s, after the first run timed out web voters at 45/30s; multi-TF context is serialized HTF-first (`_compact_htf`) so the Technical voter always sees D1/H4 (a flat truncation had starved it). **WebSearch caps are PROMPT-enforced, not SDK-enforced** — the subscription Agent SDK (0.1.81) has no Messages-API `web_search` `max_uses`/`allowed_domains` knobs, so each voter's system prompt states its search budget (news/sentiment 3, macro 2, devil 1) and domain allowlist; the technical voter gets `allowed_tools=[]` (hard 0). Hard enforcement via a `can_use_tool` callback is a follow-up if shadow data shows prompt drift. **FLO-434 reconciliation:** FLO-434 removed weaker-model advisors that *debated*; these specialists each bring *independent data* and aggregate deterministically (no conversational override), and ship gated OFF-of-blocking (shadow default) until validated. Context for the voters is read cheaply from `bot_state.json` (`multi_tf_indicators`, `market_regime`, `last_known_price`, `market_context`) — no live Luna/Echo LLM re-runs during submission. Wiring in `agent_tools.submit_plan_to_snow` (`_build_specialist_context` helper) replaces the FLO-443 call; `vote_on_plan` is retained for the FLO-443/450 regression tests. Cost ≈ $0.40-0.50/cycle (5-10 plans/day). New tests in `test_flo451_specialist_voter.py` (aggregation, timeout→ABSTAIN, shadow log). **NOTE:** with `FLO443_SELF_CONSISTENCY=on` (master switch) the specialists run in shadow on every plan after restart — observe `SPECIALIST_VOTE_SHADOW` for a few days (target: specialists disagree on 10-15% of plans) before flipping `FLO451_VOTER_MODE=confidence` or `block`.
- **Dashboard cleanup — Rex/Oracle empty panels removed (FLO-444, CEO 2026-05-18).** `dashboard/static/trade_room.html` had two `<article class="agent-card">` blocks that rendered empty after the FLO-434 cycle removal (state_writer no longer populates `last_analysis.debate` or `last_analysis.verdict`): the `rex-card` Bull/Bear debate panel and the `verdict-card` Oracle/Research Manager panel. Both removed; a single HTML comment in their place documents the FLO-444 / FLO-434 history. CSS rules and JS update-paths for those IDs are left in place (no-op since the elements no longer exist) — cleanup of orphan selectors is a separate ticket. Rex Monitor (deterministic background scan) is surfaced via the existing `get_rex_monitor` tool inside Floki's roster, not via a panel.
- **close_partial allowed in Escola 2 management (FLO-442, CEO 2026-05-18).** Snow's `_check_management_hybrid_constraints` allowlist expanded from `{move_sl_to_breakeven, trail_sl}` to `{move_sl_to_breakeven, trail_sl, close_partial}`. Enables the TP1-partial-close pattern: close 50% of position at 1R MFE and pair with a `move_sl_to_breakeven` on the runner. Both contingencies still subject to: max 2 contingencies per plan, each requires an `mfe_reached.pips > 0` trigger. `close_partial.percent` is bounded `(0, 100)` at the schema layer. The empty-management carve-out is unchanged — a non-empty `close_partial` is, by construction, not "empty" management. Prompt block "PARTIAL CLOSE AT TP1" added to `agent_prompts.py` showing the canonical 2-contingency pattern. The full ladder (partial + BE + trail = 3 contingencies) is intentionally NOT supported in this commit; expanding `_MAX` from 2 to 3 is a separate design call.
- **list_active_plans WAL read-side hardening (FLO-441, CEO 2026-05-18).** Defensive fix for the production bug where `list_active_plans` returned `count=0` despite pending plans in the DB (memory 9854, overnight 2026-05-14/15). The bug was **not reproducible in-process** during the FLO-441 investigation (`data/_audits/_wal_reproducer.py` — in-process two-connection visibility works fine), so this is hardening rather than a confirmed root-cause fix. New helper `_connect_read_only()` in `snow/db.py` wraps `_connect()` and sets `conn.isolation_level = None` (autocommit) so Python sqlite3's implicit-read-txn cannot pin a stale snapshot across consecutive SELECTs. `list_plans_by_status` (the function `get_active_plans` and the FLO-428 cap query through) switched to the read-only helper. **Writer path `_connect()` is intentionally unchanged** — the bot has a live trade while this fix ships and altering writer isolation mid-trade is too risky. The new helper routes through `_connect()` so existing test fixtures that monkeypatch `_connect` automatically affect read-only callers. 6 regression tests lock the autocommit behavior.
  - **KNOWN LIMITATION (FLO-449 investigation, 2026-05-20): `list_active_plans` still returns stale data under `LLM_PROVIDER=agent_sdk`.** FLO-441 hardened the wrong code path. Forensics: `list_active_plans` returned `count=0` in 16 of 17 recent cycles while the DB held pending plans AND the validator's own `get_active_plans()` simultaneously logged `live_count=2/2` (FLO-428 `ACTIVE_PLAN_CAP`). Empirically: `db_writer._get_connection()` opens a FRESH connection every call (no cache), the read path ALREADY routes through `_connect_read_only()`, and `get_active_plans()` returns the correct rows **in-process** — so the prescribed "route through `_connect_read_only`" fix changes nothing. The bug only manifests under the FLO-426 Agent SDK subprocess runtime and is NOT reproducible in-process (same conclusion FLO-441 reached). **Masked by FLO-428 plan cap + Floki's session-memory fallback — no plan-stacking has occurred since FLO-428 shipped.** Root cause requires Agent SDK subprocess boundary analysis — separate deep-dive ticket. Do NOT ship another "fix" for this without instrumentation or a repro.
- **Killzone gate removed; session data made informational; FLO-429 fall-back fix (FLO-440, CEO 2026-05-18).** Four coupled changes after empirical session-by-session P&L review against 210 closed trades:
  - **Killzone gate removed.** `_check_killzone_gate` deleted from `snow/validator.py`, wiring line removed from `validate_plan`, companion tests file `snow/tests/flo436_killzone_news_gate_test.py` replaced with `snow/tests/flo436_news_blackout_test.py` (news_blackout gate retained — Tier-1 macro releases are non-discretionary and still hard-gated). The constant previously named `_KILLZONE_TIER1_EVENTS` was renamed to `_TIER1_NEWS_EVENTS`. Empirical motivation: the killzone allowlists inverted this bot's actual P&L — `structural_bounce` (allowed in Asian) was the worst-performing Asian setup (0% WR, -$76 across 4 trades), while `pullback_trend` and `continuation_momentum` (banned in Asian) were the two profitable Asian setups (+$74, +$37). Asian session overall has PF 1.36 (best of the gated sessions) and the highest plan trigger rate (39.1%). London open, which the gate favoured, has the WORST PF (0.67). 4-trade per-setup buckets are too small to support an inverted hard-gate, so the gate is removed entirely and the per-session signal moves to the prompt.
  - **`SESSION CONTEXT` prompt block replaced with `SESSION DATA`.** New block in `agent_prompts.py` contains this bot's actual per-session PF / total P&L / setup-type winners and losers, plus best/worst hours by P&L. Floki weights it as confluence, no hard gate. Replaces the FLO-431 generic-retail-CFD framing (London open = breakouts, Asian = low volume) which the data does not support for this bot.
  - **WAIT 40% floor removed.** FLO-435's SCORING block previously said "You should output WAIT on at least 40% of cycles." Replaced with "quality in the conditions, not abstention quotas — there is no minimum WAIT percentage to hit and no maximum trade-frequency to stay under." The asymmetric +1/-3/0 math is unchanged; the explicit abstention quota is the part removed (it was a heuristic, not a derived target from the asymmetry).
  - **FLO-429 fall-back to `plan.analysis.regime_assumed`.** Previously the give-back trending-ban (Rule a) silently no-op'd when `author_regime` was DEGRADED, regardless of what Floki claimed in his own thesis. PLAN-20260517-001 exposed this: plan claimed `TRENDING_BEARISH`, live snapshot was DEGRADED, give-back exit at 150p slipped through the ATR floor instead of being banned outright. `_check_give_back_calibration` now reads `plan.analysis.regime_assumed` as a fall-back when the live snapshot is missing. Live snapshot still wins when present.
- **Daily loss limit gate (FLO-439, CEO 2026-05-17 → updated 2026-05-18: fixed-dollar threshold).** Validator rejects new plans when today's realized P&L is at or below **-$200.00 fixed daily loss limit**. Threshold `LIMIT_USD = 200.00` is hardcoded in `_check_daily_loss_limit` (snow/validator.py). Account snapshot supplied via the `author_account` kwarg on `validate_plan`: `{"balance": float, "today_pnl_usd": float}`. `agent_tools.submit_plan_to_snow` pulls balance from `executor.get_account_info()` (now informational only — used in the log line for context, not in the threshold calc) and today's realized P&L from history.db (sum of `profit` where `close_time >= today_00:00_UTC`) via helper `_today_realized_pnl_usd`. Fail-soft on missing account info / parse errors (logs `DAILY_LOSS_LIMIT_DEGRADED` WARN). Why fixed-dollar: with the account at ~$2200, the prior percentage-based rule triggered at -$44 — a single 0.02-lot SL hit could lock out plan creation for the entire UTC day. -$200 is the real catastrophe threshold, only fired when 3-4 trades all hit SL the same day. `balance=0` no longer forces fail-open because the threshold is now absolute.
- **ICT flow tools — FVG + liquidity sweep detection (FLO-438, CEO 2026-05-17).** Two new read-only AgentTools methods in `agent_tools.py`, registered in `ai_agent.py` schema + `_PARALLEL_SAFE_TOOLS`:
  - `get_fair_value_gaps` — scans H4 and H1 over last 100 candles, returns up to 10 unfilled FVGs per timeframe (newest first). 3-candle rule: bullish FVG when `candle[i].high < candle[i+2].low`; bearish when `candle[i].low > candle[i+2].high`. "Unfilled" = no subsequent candle has retraced ≥ 50% of the gap. Output per FVG: direction, top, bottom, midpoint, size_pips, age_candles, filled_pct, formed_at_iso. Module helpers: `_scan_fvgs`, `_mt5_tf`.
  - `get_liquidity_sweeps` — scans H4 and H1, returns up to 10 sweeps per timeframe. Fractal swing detection (3-candle window) + breach-and-recover check. Output per sweep: level, direction (BSL/SSL), sweep_candle_time_iso, wick_size_pips, recovered_pct, age_candles. Module helper: `_scan_sweeps`.
  - Live smoke at commit time produced 5 H1 FVGs + 6 H4 FVGs + 10 H1 sweeps from production candle data. Both tools route through `mt5_safe` under `mt5_lock` (Rule 23). Read-only, fail-soft (None on MT5 failure → empty list). Prompt block (`agent_prompts.py` "ICT FLOW TOOLS") describes both and notes the textbook ICT entry pattern (against the sweep direction on M5/M15 MSS confirmation).
- **Structural stops + RR-aware BE / laddered targets (FLO-437, CEO 2026-05-17).** Prompt-only additions to `agent_prompts.py`:
  - **STRUCTURAL STOPS** — SL must be placed beyond a structural invalidation level (swing high/low that, if broken, kills the thesis), not a fixed pip count. Position sizing flexes around the stop, never the reverse. Gold's typical structural-stop distance is 50-150 pips (ATR 200-500+ pips daily vs 80-120 for EURUSD).
  - **LADDERED TARGETS / RR-AWARE BE** — TP set at TP2 or TP3 level (minimum 1:2 R:R, target 1:3 to 1:5). BE management contingency anchored at the 1:1 R:R MFE distance (i.e. `move_sl_to_breakeven` with `mfe_reached.pips = SL_distance_pips`) rather than a fixed pip floor. Why not a literal three-contingency partial ladder: Snow has the `close_partial` primitive, but the FLO-419 Escola 2 management-block validator restricts to `move_sl_to_breakeven` + `trail_sl` only. Expanding management to permit `close_partial` is a separate ticket; until then, "TP at the bigger level + BE locked at 1:1 R:R" is the structural equivalent (runner protected, bigger target).
- **Session killzone + news blackout gates (FLO-436, CEO 2026-05-17).** Two new validator gates in `snow/validator.py`:
  - **Killzone gate** maps the plan's `created_at` UTC hour to a session and rejects `setup_type` values that historically convert poorly there. Mapping: LONDON_OPEN 07-09 (only `breakout_range`, `session_open_break`); NY_OVERLAP 09-12 + 12-16 + LATE_NY 20-21 (all allowed); NY_PM 16-20 (only `pullback_trend`, `continuation_momentum`); ASIAN 21-06 (only `mean_reversion_extreme`, `structural_bounce`). Empirical motivation: the Asian session is the worst-performing window for breakouts on gold (largest false-signal rate per research doc); the London open is the most reliable for breakouts. The setup_type allowlists per session are explicit and documented in the rejection message so Floki can refine. Fail-soft on missing/unparseable `created_at`.
  - **News blackout gate** rejects new plans within ±30 minutes of any HIGH-importance Tier-1 macro release (NFP, CPI, FOMC/Fed Rate, PPI, GDP). Reads `self._bot._last_calendar_data` via `agent_tools.submit_plan_to_snow` → `_author_calendar` parameter. Detection: HIGH importance + `|minutes_until| ≤ 30` + name contains a Tier-1 keyword. Fail-soft on missing calendar (logs `NEWS_BLACKOUT_DEGRADED` WARN). Rejection tells Floki to wait for the release to print and the first 30 minutes of price action to clear, then reauthor.
  - Wiring: `validate_plan` gained an `author_calendar: Optional[list]` keyword arg. `agent_tools.submit_plan_to_snow` now passes both `_author_regime` (existing) and `_author_calendar` (new).
- **Top-down workflow + asymmetric scoring + indicator priority (FLO-435, CEO 2026-05-17).** Three prompt-only additions to `agent_prompts.py:SYSTEM_PROMPT`:
  1. **TOP-DOWN ANALYSIS CHECKLIST** — strict 5-step sequential workflow (D1 bias → H4 confirmation → H1 zone → M15/M5 trigger → PLAN/WAIT). Explicitly prescriptive ("Follow these steps IN ORDER. Do NOT skip to Step 4 before completing Step 1"). Overrides project memory `feedback_no_prescriptive_rules.md` — CEO directive 2026-05-17 backed by the professional XAUUSD methodology research doc establishes that *the discipline itself is the edge being encoded*, qualitatively different from earlier add-on data blocks where prescriptiveness was the wrong shape. Memory entry has been updated to record this exception. Failure mode the rule targets: starting analysis on the entry timeframe (M5) creates confirmation bias — "see what you want to see."
  2. **SCORING (asymmetric)** — added to the identity section directly under the WAIT-is-fine line (FLO-434): correct trade = +1, wrong trade = -3, NO_TRADE/WAIT = 0. Target WAIT rate ≥ 40%. The math: a 3× asymmetric loss means at 50% win rate the trader is net negative, so the bar for OPEN must be a setup the operator would be embarrassed to defend at the level. No validator code; this is a self-evaluation lens.
  3. **INDICATOR PRIORITY** — guidance block (not constraint) ranking indicators by signal-additivity: PRIMARY (EMA alignment, ADX, ATR), SECONDARY (RSI for extremes/divergences only, MACD), REDUNDANT (Stochastic ≈ RSI; Bollinger position ≈ EMA-distance — cite at most one of each pair, not both). All tools remain available; the prompt just stops Floki from inflating apparent confidence by stacking near-duplicate indicators.
- **Volume primitive (FLO-433, CEO 2026-05-17).** New Snow condition type `volume_above` for entry/exit/management blocks. Pydantic class in `snow.schema.VolumeAbove` (Literal `"volume_above"`, fields `tf: Timeframe`, `period: int [5..200] default 20`, `ratio: float [0..10] default 0.5`). Evaluator `evaluate_volume_above` in `snow/evaluators/indicator.py`: True iff `live_data.volume_ratio(tf, period) >= ratio`. `LiveData.volume_ratio()` added to `snow/live_data.py` — fetches `period+1` bars on the requested TF via `mt5_safe.copy_rates_from_pos` (Rule 23) under `mt5_lock`, divides latest tick_volume by mean of the prior `period` tick_volumes. Per-tick cached via the existing `_tick_cached` mechanism. None on MT5 failure or insufficient history → evaluator returns False (fail-safe). XAUUSD has `real_volume=0` (broker doesn't publish); tick_volume mean ~35k with range 11k-70k (production sample). Wired into `snow/evaluators/dispatch.py` and added to the `Condition` Union in `snow.schema`. Prompt block (`agent_prompts.py` "VOLUME GATE") describes the primitive and suggests ratio floors per setup_type but does NOT mandate it — optional confluence input.
- **DXY direct to Floki (FLO-432, CEO 2026-05-17).** New tool `get_dxy_status` in `agent_tools.py` (registered in `ai_agent.py` schema + `_PARALLEL_SAFE_TOOLS`). Returns DXY current price, 1-day return %, 5-day return %, 30-day correlation with gold (XAUUSD), and a coarse signal label (`DXY_RISING` if 5d > +0.75%, `DXY_FALLING` if < -0.75%, else `DXY_NEUTRAL`; `DXY_UNKNOWN` on network/sparse-history failure). 5-minute in-memory cache via module-level `_DXY_CACHE`. Uses yfinance with multi-symbol fallback (`DX-Y.NYB` → `DX=F` → `UUP`). Architectural reason: CEO directive 2026-05-17 — DXY is the primary inverse correlate for gold and the trade decisor (Floki) should see it directly rather than have it filtered through Luna's macro brief; matches the same agent-first principle that drives `get_market_regime` over auto-injected regime context. Prompt block (`agent_prompts.py` "DXY CONFIRMATION") describes the data and the empirical asymmetry (BUY-gold + DXY_RISING is fighting macro; SELL-gold + DXY_RISING is confirmed) but leaves the conviction adjustment to Floki — no prescriptive "WAIT" rule.
- **Session awareness (FLO-431, CEO 2026-05-17).** Prompt-only change — added `SESSION CONTEXT` block to `agent_prompts.py` directly after `REGIME ALIGNMENT`. Lists the four UTC session windows (London open 07-09, London/NY overlap 13-17, late NY 17-21, Asian 21-06) with the empirical liquidity / breakout-reliability characterization of each. Framed as data, not rule — same-setup-at-different-hour is not equivalently strong; Floki should factor this into confidence and slot allocation. The block does NOT prescribe WAIT during Asian hours (per project memory `feedback_no_prescriptive_rules.md`); A+ setups at major D1/weekly levels remain valid in any session.
- **ADX-override (FLO-430, CEO 2026-05-17).** Path B of the regime gate. Independent of regime label and confidence tier — rejects counter-trend plans when `adx >= 30` AND `d1_direction == h4_direction` (both bullish or both bearish per `_get_mtf_trend_direction` price-vs-EMA50 check) AND plan direction opposes the stack. Catches the case FLO-427 misses: regime_detector returns confidence="moderate" 64% of the time (memory 9801), so the FLO-427 confidence floor silently allowed PLAN-20260514-009 — a BUY with ADX 46.87 and full bearish multi-TF EMA stack labelled `regime=TRANSITIONAL`. The fix wires `d1_direction`/`h4_direction` through `regime_detector.detect_market_regime()` return dict → `main.py:_last_regime_context` → `agent_tools._author_regime` → `_check_adx_override` helper. Path B is checked in two places inside `_check_regime_counter_trend_gate`: when regime is NOT trending (catches mislabelled-regime cases) and when regime IS trending but confidence is below "high" (catches low-confidence-tier cases). Fail-soft when `d1`/`h4` are absent (legacy snapshots or transient brain failures). Rejection emits `regime_gate:` prefix marked `FLO-430` and includes ADX + D1/H4 alignment in the message so Floki can reorient.
- **Give-back exit calibration (FLO-429, CEO 2026-05-17).** Snow's validator gates `profit_retraced_from_peak` exits two ways: (a) in `TRENDING_BULLISH`/`TRENDING_BEARISH` regimes the contingency is rejected entirely (let SL + duration_cap + optional trail_sl handle exits — trends are M5-noisy and give-back fires on routine pullbacks), and (b) in all other regimes the threshold must be ≥ 3.0 × live M5 ATR(14) pips. M5 ATR is fetched live via `mt5_safe` at validation time; one `copy_rates_from_pos` per validate call. Fail-open on missing author_regime or MT5 hiccup (logs `GIVE_BACK_CAL_DEGRADED` WARN). Rejection emits `give_back_calibration:` prefix with the offending contingency name(s) and the ATR/min-required numbers. Empirical motivation: PLAN-20260515-010 (SELL, TRENDING_BEARISH, ADX 47) reached +104p MFE then a 2-minute reversal spike retraced 150p from peak and fired give_back at -55p; gold subsequently fell another 215p favorable. Same trade with no give_back would have closed via SL/duration at deep profit. **FLO-449 prompt reinforcement (CEO 2026-05-20):** the FAILED-RECOVERY EXITS block in `agent_prompts.py` now explicitly tells Floki NOT to author `profit_retraced_from_peak` (or any give-back) exits in TRENDING regimes because the validator strips them. Motivation: the FLO-449 review found `GIVE_BACK_CAL` was the busiest gate — 12 distinct plans rejected May 18-20 — because Floki kept authoring give-back exits in trending regimes despite FLO-429. The gate was working; the prompt was the gap.
- **Active-plan cap (FLO-428, CEO 2026-05-15).** Snow's validator hard-rejects any plan submission when the in-flight count (status ∈ {pending, active, triggered, closing}) is already at `MAX_ACTIVE_PLANS=2`. Belt-and-suspenders against the `list_active_plans` WAL-visibility bug — overnight 2026-05-14/15, Floki's tool returned `count=0` for two consecutive cycles despite pending plans living in the DB, and Floki stacked 4 SELL plans (entries at 4581/4607/4635/4645) on the assumption that prior plans had expired. The validator does its own fresh `snow.db.get_active_plans()` count at validation time, excludes the row being submitted (self-id), and rejects with `active_plan_cap:` prefix that names the live plan IDs. Fail-open on DB error (emits `ACTIVE_PLAN_CAP_DEGRADED` WARN). Rejection tells Floki to `cancel_plan(<id>)` on a live plan first or wait for fire/expire/close before authoring more. Separate ticket pending for the underlying WAL-visibility bug — until then, this cap is the live safety.
- **Counter-trend regime gate (FLO-427, CEO 2026-05-14).** Snow's validator hard-rejects counter-trend plans in confirmed trending markets. Gate fires when ALL of: `regime ∈ {TRENDING_BULLISH, TRENDING_BEARISH}`, `confidence ∈ {high, strong}`, `adx >= 25`, and plan direction opposes regime (BULLISH→SELL or BEARISH→BUY). In RANGING / BREAKOUT_IMMINENT / VOLATILE / QUIET / TRANSITIONAL, both directions are permitted. The regime snapshot is captured at plan-author time from `self._bot._last_regime_context` (set by `main.py` from `regime_detector.detect_market_regime()`) and passed to `validate_plan(plan_dict, author_regime=...)` in `agent_tools.submit_plan_to_snow`. Fail-open: missing/UNKNOWN snapshot logs `REGIME_GATE_DEGRADED` WARN and allows the plan through (paralysis risk on transient MT5/Brain hiccups outweighs fail-closed value). Rejection emits `regime_gate:` prefix with regime/ADX/confidence/timestamp so Floki can reorient on the next iteration. Empirical motivation: May 1-4 forensics — 45% of losses were counter-trend SELL plans on bullish days (4 SELLs lost on May 1, a TRENDING_BULLISH day). Companion change: `paired_hedge` removed from `snow.schema.SetupType` Literal; `agent_prompts.py` rewritten to drop "scenario map" framing, cap concurrent plans at 2 SAME-direction, and promote anti-hedge guardrail to top-level rule.
- **Entry conditions must use fixed prices (FLO-419, CEO 2026-05-04).** `price_at_sr_zone`, `price_at_pivot`, and `price_at_fibonacci` are REJECTED in `entry.conditions` by `snow/validator.py:_check_no_dynamic_level_in_entry`. They resolve their target price from the live SemanticCache at trigger time — the trigger silently shifts whenever Brain re-ranks the nearest zone / pivot / fib level, firing at a price the plan's thesis never analyzed. Plans must commit to the literal number using `price_above {level: N}` or `price_below {level: N}`. These primitives remain permitted in `exit` and `management` blocks where live-structure semantics are intentional. Empirical motivation: PLAN-20260503-001 authored with the 4605-4612 support cluster in mind but used `price_at_sr_zone tolerance_pips=8` — would have fired at any drifted "nearest support" Brain surfaced later, regardless of the multi-confluence thesis. **FLO-450 prompt rule (CEO 2026-05-21):** pullback entries (`pullback_trend` / `structural_bounce`) MUST use a two-step latch — `price_crossed_level below <target>` (wait for the pullback to arrive) + `price_above <reclaim>` (enter on the reclaim) — never a bare `price_above` floor. A floor is true at any price above the level, so it fills at the TOP of the move before the pullback prints: PLAN-20260520-006 filled +6 USD above zone, PLAN-20260521-001 filled +10 USD above zone, both opened underwater. The latch persists once price dips through the level, so latch+reclaim go all-true together only after the pullback. Block added to `agent_prompts.py` ("PULLBACK ENTRY GEOMETRY (FLO-450)") with GOOD/BAD examples.

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

### 5. Use /goal for Every Task

**Set a completion condition. Let Claude work autonomously until done.**

Every task from the CEO/CTO must be executed via `/goal`, not as plain instructions.

Format:
```
/goal [verifiable condition 1] AND [verifiable condition 2] ... or stop after N turns
```

Rules:
- Conditions must be VERIFIABLE from the transcript (test output, file exists, grep result)
- Always include a turn cap: "or stop after N turns" (default: 25)
- Run in auto mode (no permission prompts)
- One goal per session; set a new one after completing the previous
- `/goal clear` to cancel if stuck

Example:
```
/goal FLO-445: _check_sl_buffer_from_structure rejects plans with buffer < 1.0×M15_ATR. All validator tests pass (pytest tests/flo445). Prompt updated in agent_prompts.py with sweep education block. CLAUDE.md has FLO-445 entry. Or stop after 25 turns.
```

Bad conditions (not verifiable):
- "make the code better" — what does "better" mean?
- "fix all bugs" — how does the evaluator know all bugs are fixed?
- "production-ready" — unverifiable from transcript

Good conditions (verifiable):
- "pytest exits 0" — test output appears in transcript
- "grep -r 'FLO-445' CLAUDE.md returns a match" — grep output in transcript
- "python -c 'import agent_prompts' exits 0" — import check in transcript

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
