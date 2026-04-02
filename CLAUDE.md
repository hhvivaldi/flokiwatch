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
| Rex | GPT-4o | `rex_validator.py` | Analyst. 11 tools (6 standard + 5 unique). Also runs Bull/Bear debate (FLO-190). |
| Research Mgr | Gemini 3 Flash | `research_manager.py` | Picks winner between Rex Bull and Rex Bear. Produces verdict with triggers (FLO-194). |
| Simba | Python | `agent_monitor.py` | Watchdog. 30s polling. Wakes Floki. |
| Sage | Gemini | `sage_auditor.py` | Daily auditor at 21:00 UTC. |
| Echo | MiMo-V2-Flash | `echo_sentinel.py` | News sentinel. 25 RSS feeds. PULL-only. |
| Luna | MiMo-V2-Flash | `luna_analyst.py` | Macro analyst. MT5+Yahoo+FRED. |
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
  │   ├─ rex_validator.py (debates) | agent_tools.py (28 tools)
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
- **Score system:** 0-100. 50=neutral. >65=BUY. <35=SELL. 45-55=HOLD.
- **Active thesis persistence:** `data/active_thesis.json` — carries between cycles.

## Critical Safety Rules

- No simultaneous BUY+SELL (FLO-85 hard gate, `is not None` check not truthiness)
- Max 3 positions. No trades 60 min before/after market open/close. Max 6% daily loss.
- Volatility guard: M5 >1.8% blocks trades (`volatility_guard.py`)
- adjust_trade: SL-widening guard + rate limit (max 3/hour/ticket, FLO-141)

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

## Ticket Convention

FLO-NNN format. Commits: `fix: FLO-XXX — description` or `feat: FLO-XXX — description`. Tracked in Linear (Floki Watch team). Known open issues: FLO-96 (timezone audit), FLO-140 (P1 backlog), FLO-146 (dead VIX feature).
