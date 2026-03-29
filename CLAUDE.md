# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

FlokiWatch: autonomous 7-agent XAU/USD (Gold) trading system on MetaTrader 5. Runs 100% autonomously — no manual intervention. DEMO mode on Capital Point broker. Balance: $813.76, 101 trades (Population B).

## Commands

```bash
# Run the bot (production)
python main.py

# Single analysis cycle (test mode — exits after one loop)
python main.py --test

# Dry run (no MT5 execution)
python main.py --dry-run

# Dashboard
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080

# Unit tests
python test_central_brain.py
python sanity_test.py
python validate_all.py
```

No formal test framework (no pytest.ini). Test files are standalone scripts.

## Architecture — 7 Agents

| Agent | Model | File | Role | Tools |
|-------|-------|------|------|-------|
| **Floki** | GPT-5.4 (temp 1.0) | `ai_agent.py` | Sole trading decisor. Self-schedules 5-30 min. | 28 |
| **Rex** | GPT-5 mini (temp 1.0) | `rex_validator.py` | Co-pilot. Debates trades. Advisory only (not a veto). | 9 |
| **Simba** | Python (no AI) | `agent_monitor.py` | Watchdog. 30s polling. Wakes Floki on conditions. | — |
| **Sage** | Gemini (SAGE_MODEL) | `sage_auditor.py` | Daily auditor. Runs 21:00 UTC. Reviews trade history. | — |
| **Echo** | MiMo-V2-Flash | `echo_sentinel.py` | News sentinel. 25 RSS feeds every 5 min. PULL-only. | — |
| **Luna** | MiMo-V2-Flash | `luna_analyst.py` | Macro analyst. MT5 enriched (20 instruments) + Yahoo + FRED every 15 min. | — |
| **Brain** | Python (no AI) | `central_brain.py` | Data pipeline. 5-pillar analysis every 60s. No decisions. | — |

**NOTE:** `simba_watcher.py` is dead/shadow code. The canonical Simba evaluator is in `agent_monitor.py`.

## Model Independence

Each agent has its OWN model config variable. They are INDEPENDENT:
- `FLOKI_MODEL` → ai_agent.py only
- `REX_MODEL` → rex_validator.py only
- `SAGE_MODEL` → sage_auditor.py only (Gemini, NOT OpenAI)
- `ECHO_MODEL` → echo_sentinel.py only (MiMo via Xiaomi API)
- `LUNA_MODEL` → luna_analyst.py only (MiMo via Xiaomi API, hardcoded)

NEVER share model variables between agents. The Sage model contamination bug (used FLOKI_MODEL instead of SAGE_MODEL) taught this lesson.

## Data Flow

```
main.py (orchestrator)
  ├─ central_brain.py → BrainResult (score 0-100, direction, 5 pillars)
  │   ├─ technical_analyzer.py: RSI, MACD, BB, EMAs, ATR, S/R, Fibonacci
  │   ├─ ml_predictor.py: 6 models (XGB+LGB+CAT × H1+H4)
  │   ├─ news_score_hybrid.py: RSS headlines + DXY + VIX + yields
  │   ├─ momentum_detector.py: ADX, volume ratio, breakout detection
  │   └─ economic_calendar.py: USD events, PRE/DURING/POST phases
  ├─ regime_detector.py → 7 regimes with temporal context (FLO-139)
  ├─ ai_agent.py (Floki) → WAIT / OPEN / CLOSE / ADJUST decision
  │   ├─ rex_validator.py (Rex debates on OPEN/CLOSE)
  │   └─ agent_tools.py (28 tools: market data, trading, memory)
  ├─ executor.py → MT5 order execution
  │   └─ ea_bridge.py → JSON signal file → FlokiBridge EA
  ├─ monitor.py → position management (breakeven, trailing, drawdown)
  ├─ state_writer.py → bot_state.json (dashboard) + market_context.json
  └─ db_writer.py → data/history.db (SQLite)
```

## Regime Detection (FLO-139)

`regime_detector.py` detects 7 market regimes using ALL available indicators. Priority order:
1. VOLATILE (vol_guard EXTREME/COOLING, ATR >2x avg)
2. QUIET (ATR <0.5x avg, volume <0.5x, ADX <15)
3. BREAKOUT_IMMINENT (BB squeeze + volume rising + ADX rising)
4. TRENDING_BULLISH (ADX >=25, EMAs aligned, price >EMA50, MTF bullish)
5. TRENDING_BEARISH (mirror of bullish)
6. RANGING (ADX <20, price between S/R, low volume)
7. TRANSITIONAL (fallback)

Includes temporal tracking: duration, previous_regime, regime_changes_24h, stability, transition narrative. Injected into Floki's trigger_context as `<market_regime>` block.

## FOLLOWUP Mechanism

When Floki decides OPEN_BUY/OPEN_SELL but forgets to call execute_trade, or decides CLOSE_TRADE without calling close_trade, or decides ADJUST_TRADE without calling adjust_trade — the FOLLOWUP mechanism injects a reminder turn. Uses JSON-parsed decision field (not substring match) to avoid false positives.

## Active Thesis Persistence

Floki saves reasoning to `data/active_thesis.json` between cycles: direction_bias, key_levels, conditions, decision. Next cycle shows what changed. Anti-repetition triggers after 3 unchanged cycles ("Focus on what's NEW"). Both proactive AND reactive paths receive `<previous_thesis>` block.

## Key Design Decisions

**Floki is the sole decisor.** No other agent can execute trades. Rex's AGREE/DISAGREE is advisory — "DISAGREE is feedback, not a veto." Safety checks (market hours, opposing positions) are hard gates, but SafetyChecker blocking was removed (FLO-118) — Floki manages own risk.

**Rex defaults to DISAGREE on failure.** If Rex can't clearly say AGREE/DISAGREE (truncation, parse error), default is DISAGREE. This is truthful — "Rex didn't clearly agree" != "Rex agreed."

**EA is a pure executor.** `FLOKI_MANAGES_POSITION = True`. EA gets 9999-pip BE/trailing triggers (never fire). Floki manages positions via `adjust_trade`.

**Echo is pull-based, not push.** Floki decides when to check news via `get_echo_alerts` tool.

**Score system:** 0-100 scale. 50 = neutral. >65 = BUY signal. <35 = SELL signal. 45-55 = HOLD.

## Critical Safety Rules

- No simultaneous BUY + SELL (FLO-85 hard gate in `safety_checks.py`)
  - Uses `is not None` check, not truthiness — empty list [] = no positions (safe), None = fetch failed (BLOCK)
- Max 3 open positions (`config.MAX_POSITIONS`)
- No trades 60 min before/after market open/close
- Max 6% daily loss → pause
- Volatility guard: M5 >1.8% movement blocks trades (`volatility_guard.py`)
- adjust_trade has NO SL-widening guard currently (FLO-141 — planned)
- adjust_trade has NO rate limiting currently (FLO-141 — planned)

## File Conventions

**Config:** All settings in `config.py`, secrets in `.env`. Config values loaded at import time. Market hours configurable via env var (`MARKET_DAILY_CLOSE_HOUR`, `MARKET_DAILY_OPEN_HOUR`).

**Logging:** `from logger import log`. Daily rotation. Trade-critical functions use `log.warning`/`log.error`. Data pipeline uses `log.debug`/`log.info`.

**Database:** SQLite at `data/history.db`. All queries parameterized. Every function creates a new connection via `_get_connection()` with try/finally conn.close().

**State files:** JSON files in `data/`. Atomic writes via temp file + `os.replace()`.

**Dashboard data contract:** `FIELD_CONTRACT.md` is LAW. Changes to `bot_state.json` require updating both `state_writer.py` and `FIELD_CONTRACT.md`. HTML element IDs cannot change without updating `app.js` in the same commit.

## CRITICAL CODE REVIEW RULES

**These rules are MANDATORY for every commit. No exceptions.**

### Rule 11 — Intent Before Change

NEVER assume something is a bug — it may be intentional design. Always check git history (`git show HEAD:filename`) and ask about design intent BEFORE changing behavior. The codebase had 3 "fixes" reverted because they changed intentional design (S/R touch accumulation, calendar API cache, COOLING_DOWN override).

### Rule 14 — Skill Review Before Push

For ANY commit that touches trading decision logic in these files, run an explicit code review using Claude Code skills BEFORE pushing:
- `ai_agent.py`
- `regime_detector.py`
- `agent_prompts.py`
- `agent_tools.py`
- `risk_manager.py`
- `executor.py`
- `rex_validator.py`
- `safety_checks.py`
- `monitor.py`
- `floki_position_manager.py`

Example: "Using code-reviewer skill, review this file — focus on [specific areas]"
Show the review output as evidence. Developer approves all changes.

### Rule 15 — Complete File Before Push

For new files over 100 lines, show the complete file for audit BEFORE pushing. Commit hash of unseen code = NOT CONFIRMED.

### Bug Classification

- **P0:** Crash, data corruption, wrong trade execution, silent failure in trading path → Fix immediately
- **P1:** Logic error, wrong calculation, missing validation → Fix with developer approval
- **P2:** Code smell, deprecated API, minor improvement → Deferred

### Commit Process

1. Create plan file before implementing
2. Implement changes
3. Run code review with skill (Rule 14) on affected files
4. Show review output — classify findings as BUG / DEFENSIVE / INTENTIONAL
5. For anything that might be INTENTIONAL: check `git show HEAD:filename` and explain original intent
6. Fix BUGs and DEFENSIVE items. Do NOT change INTENTIONAL design without explicit approval.
7. Commit with descriptive message: `fix: FLO-XXX — description` or `feat: FLO-XXX — description`
8. Push to GitHub after each commit

### Rule 16 — Documentation Always Updated

After ANY change to the system, update ALL relevant documentation in the SAME commit:
- `CLAUDE.md` — if architecture, agents, models, tools, rules, or conventions changed
- `README.md` — if architecture, setup, or overview changed
- `SYSTEM_DOCUMENTATION.md` — if detailed system behavior changed
- `MONDAY_CHECKLIST.md` — if startup/verification procedures changed
- `FIELD_CONTRACT.md` — if bot_state.json fields changed
- `docs/MONDAY_GPT54_TEST.md` — if test procedures changed

Documentation updates are NOT optional. They go in the SAME commit as the code change. If you add a new field to bot_state.json, FIELD_CONTRACT.md is updated in that commit. If you change a model, CLAUDE.md and README.md are updated in that commit. If you add a new agent feature, SYSTEM_DOCUMENTATION.md is updated in that commit.

Outdated documentation is worse than no documentation — it causes wrong assumptions and wasted time.

### Rule 17 — Push Immediately

Every commit must be pushed to GitHub immediately after committing. No local-only commits. The repo on GitHub must ALWAYS reflect the latest state. If Hermano needs to pull from another machine, or if we need to verify code, GitHub must be current.

### What NOT to Do

- Do NOT push 50+ fixes in parallel without individual review
- Do NOT read working copy to determine original intent — use `git show HEAD:filename`
- Do NOT classify something as P0 without verifying it actually crashes/corrupts in production
- Do NOT remove safety features (rate limiting, SL guards, API caches) without confirming they're not intentional

## Environment

- Windows 11, Python 3.12+, MetaTrader 5 terminal must be running
- Home: `C:\Users\Hermano\OneDrive\Desktop\XAUUSD`
- Remote: `C:\Users\hvivaldi\Desktop\DevOPS\flokiwatch`
- API keys: `OPENAI_API_KEY` (Floki/Rex), `LUNA_API_KEY` (Luna/Echo via Xiaomi), `GEMINI_API_KEY`/`SAGE_API_KEY` (Sage)
- Discord webhooks for 12+ channels (currently disabled)
- FCS API key for economic calendar (`FCS_API_KEY`) — free tier 500 req/day, 5-min error cache intentional

## Ticket Convention

Tickets use FLO-NNN format (e.g., FLO-143). Commit messages: `fix: FLO-143 — description` or `feat: FLO-XXX — description`. Track in Linear (Floki Watch team).

## Known Issues / Backlog

- FLO-96: Timezone audit (3 sources: local CET, UTC, MT5 EET) — DO NOT fix without full audit
- FLO-140: 26 P1 bugs logged from full codebase review — post-Monday cleanup
- FLO-141: adjust_trade needs SL-widening guard + rate limiting
- vix_change always 0 in ML features — VIX removed from pipeline in FLO-121, feature is dead
