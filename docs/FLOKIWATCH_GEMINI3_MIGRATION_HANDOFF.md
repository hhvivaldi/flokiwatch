# FLOKIWATCH — Gemini 3 Flash Migration Handoff
## Full System Audit + Migration Plan
### Date: March 18, 2026

---

## 1. DECISION

Migrate Floki decision engine from local model (Qwen/Ollama inline) to **Gemini 3 Flash API** with native tool/function calling. This returns to the original Claude architecture (iterative tool loop) but on a different provider.

**Why Gemini 3 Flash:**
- Native tool/function calling (eliminates all inline data problems)
- GPQA Diamond: 90.4%
- Cost: $0.50/1M input + $3/1M output (~6x cheaper than Claude)
- Designed for agentic workflows
- google-genai SDK already installed

**Call frequency:** Every 5 minutes

---

## 2. SYSTEM COMPONENTS — CURRENT STATE + MIGRATION ACTION

### KEEP UNCHANGED (no modifications needed)

| Component | File | Status | Notes |
|---|---|---|---|
| 19 Agent Tools | agent_tools.py | ✅ WORKING | get_price, get_indicators, get_sr_zones, get_candles, get_fibonacci, get_macro, get_headlines, get_ml_prediction, get_open_positions, get_position_events, get_calendar, read_session_memory, write_session_memory, set_watch_conditions, get_watch_conditions, get_trade_patterns, execute_trade, close_position, debate_with_rex |
| Floki Personality Prompt | agent_prompts.py | ✅ INTACT | System prompt, guidelines, personality |
| Rex Validator | rex_validator.py | ✅ CONFIRMED GPT-4o | Debate partner, no Gemini residue |
| Simba Watcher | simba_watcher.py | ✅ WORKING (Python) | 5-min summaries, wake/watch conditions |
| EA Bridge | ea_bridge.py + FlokiBridge.mq5 | ✅ WORKING | JSON protocol, BE/trailing |
| Executor | executor.py | ✅ WORKING | MT5 order execution |
| Risk Manager | risk_manager.py | ✅ WORKING | Position sizing, SL/TP |
| Safety Checks | safety_checks.py | ✅ WORKING | 10 safety validations |
| DB Writer | db_writer.py | ✅ WORKING | SQLite persistence |
| State Writer | state_writer.py | ✅ WORKING | Dashboard state JSON |
| Trade Room | dashboard/static/trade_room.html | ✅ WORKING | Decisions feed |
| Dashboard | dashboard/ | ✅ WORKING | Main monitoring UI |
| Alerts | alerts.py | ✅ WORKING | Discord notifications |

### MODIFY (adapt for Gemini)

| Component | File | Action |
|---|---|---|
| AI Agent | ai_agent.py | Change Anthropic SDK → Google Gemini SDK. Keep tool loop architecture. Keep AgentResult output format |
| Main Orchestrator | main.py | Remove local Floki code path. Reactivate Agent call path. Change interval to 5 min. Fix phantom position bug |
| Agent Monitor | agent_monitor.py | Fix phantom position: use MT5 as source of truth |
| Monitor | monitor.py | Fix phantom position cache |
| Config | config.py | Remove shadow/local flags, add Gemini config |

### REMOVE COMPLETELY

| Component | File | Reason |
|---|---|---|
| Shadow Model | shadow_model.py | Replaced by Gemini with tools |
| Local Floki entrypoint | _call_floki_local_and_execute() in main.py | Replaced by Agent path |
| Shadow worker thread | _shadow_worker() in main.py | Not needed |
| Local prompt builder | build_shadow_prompt() in shadow_model.py | Not needed — Gemini uses tools |
| All FLOKI_LOCAL_* logs | main.py | Replaced by Agent logging |
| All SHADOW_* logs | main.py | Not needed |
| All FLOKI_SR/NEWS/MEMORY diagnostic logs | main.py | Temporary, already served purpose |

---

## 3. FEATURES CONFIRMED — MUST BE PRESERVED

These features were verified with runtime evidence during the March 17-18 session. They must all work after the Gemini migration.

| Feature | Verified By | Commit(s) |
|---|---|---|
| EA BE/trailing at 50% of SL | Log: BE=154 pips for SL=307 | ddf059c |
| Rex on GPT-4o (no restrictions) | Log: REX model=gpt-4o provider=openai | c3aa41b |
| Simba Python 5-min summaries | Log: SIMBA_5MIN_SUMMARY with price/conditions | 2877ef5, 905fb98 |
| Simba watch+wake dual monitoring | Log: 0/1 conditions met | 797d05a, be6b2d0 |
| Simba smart cooldown (30 min) | Log: fingerprint-based dedupe | 9247c62 |
| WATCH_REMAP ticket 0→real | Code present, not tested with new trade | e63b2cf |
| Startup skip if analysis <30min | Log: age_minutes=X threshold=30 skip=True/False | 6af06dd |
| Floki decisions in Trade Room feed | Log: record_agent_event FLOKI_DECISION | 897739b |
| Rex debate in Trade Room feed | Log: DEBATE turn=1/5 | 897739b |
| Trade Room avatar GIFs (60s rotation) | Screenshot confirmed | a324ec4 |
| Simba price display in Trade Room | Log: Simba price shows correctly | 897739b |

---

## 4. BUGS TO FIX IN THIS MIGRATION

### Bug 1: Phantom Position (CRITICAL)
**Description:** AGENT_MONITOR reports `active_trade=SELL@4993.6` at 09:26 when the position was closed at 01:07 (8 hours earlier). Safety check then blocks valid OPEN_BUY decisions.

**Root Cause:** `AgentMonitor._get_active_trade()` reads from DB (`get_active_trade_from_proactive()`) which only clears when a CLOSE_TRADE event is recorded. EA-closed positions don't record CLOSE_TRADE events.

**Fix:** MT5 as source of truth:
- `_get_active_trade()` must call `executor.get_open_positions()` (which uses `mt5.positions_get`)
- If MT5 returns no positions → return None regardless of DB
- Clean per-ticket caches when tickets disappear from MT5
- Rebuild state on startup from MT5 current positions
- Pre-flight MT5 check before any OPEN order

### Bug 2: Claude Error Spam
**Description:** System attempts Claude API calls even after credit exhaustion, generating repeated error logs.

**Fix:** When FLOKI_MODEL_SOURCE="gemini", skip ALL Anthropic client initialization and calls. No more credit error spam.

### Bug 3: ML in Prompt (Already addressed)
**Description:** ML ensemble data was removed from local Floki prompt. For Gemini, the `get_ml_prediction` tool still exists but the model can choose not to use it.

**Decision:** Keep the tool available. If Gemini doesn't find it useful, it won't call it. No need to remove.

---

## 5. CONFIG CHANGES

### ADD:
```
FLOKI_MODEL = "gemini-3-flash"          # Exact model string from Google
FLOKI_MODEL_SOURCE = "gemini"            # Options: "gemini" | "claude" (for rollback)
FLOKI_CALL_INTERVAL = 300                # 5 minutes (seconds)
GEMINI_API_KEY = <already in .env>
```

### REMOVE:
```
SHADOW_MODEL_ENABLED                     # No longer needed
SHADOW_MODEL_URL                         # No longer needed
SHADOW_MODEL_NAME                        # No longer needed
SHADOW_MODEL_TIMEOUT                     # No longer needed
FLOKI_LOCAL_CALL_INTERVAL                # No longer needed
FLOKI_LOCAL_TIMEOUT                      # No longer needed
```

### KEEP (for rollback to Claude):
```
ANTHROPIC_API_KEY                        # Keep in .env
USE_AI_AGENT                             # Keep (True)
AI_AGENT_MODEL                           # Keep (claude-sonnet-4-20250514)
```

---

## 6. AI_AGENT.PY MIGRATION — KEY CHANGES

### Current (Anthropic):
```
client = anthropic.Client(api_key=ANTHROPIC_API_KEY)
response = client.messages.create(
    model="claude-sonnet-4-20250514",
    system=system_prompt,
    messages=conversation,
    tools=tool_schemas,
    max_tokens=4096
)
# Tool loop: parse tool_use blocks → execute → send results back
```

### Target (Gemini):
```
from google import genai
client = genai.Client(api_key=GEMINI_API_KEY)
response = client.models.generate_content(
    model="gemini-3-flash",
    contents=conversation,
    config=genai.types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[tool_declarations],
        temperature=0.3
    )
)
# Tool loop: parse function_call parts → execute → send results back
```

### Tool Schema Conversion:
- Anthropic format: `{"name": "get_price", "description": "...", "input_schema": {...}}`
- Gemini format: `{"name": "get_price", "description": "...", "parameters": {...}}`

The parameter schemas are nearly identical (JSON Schema). Minimal conversion needed.

### Response Parsing:
- Anthropic: `response.content` contains `text` or `tool_use` blocks
- Gemini: `response.candidates[0].content.parts` contains `text` or `function_call` parts

### Tool Result Format:
- Anthropic: `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "...", "content": "..."}]}`
- Gemini: `{"role": "user", "parts": [{"function_response": {"name": "...", "response": {...}}}]}`

### AgentResult Output:
MUST remain identical so main.py doesn't need changes:
```python
AgentResult(
    decision="WAIT",
    confidence=72,
    reasoning="...",
    trade_plan=None,
    key_factors=[...],
    concerns=[...],
    tokens_used=...,
    latency_ms=...
)
```

---

## 7. MAIN.PY CHANGES

### Remove:
- `_call_floki_local_and_execute()` method entirely
- `_shadow_worker()` method entirely
- `_floki_local_lock` threading lock
- `_floki_local_raw_log_remaining` counter
- `_is_floki_local_enabled()` check
- All `FLOKI_LOCAL | ...` log lines
- All `SHADOW | ...` log lines
- All diagnostic log lines (FLOKI_SR, FLOKI_NEWS, FLOKI_MEMORY, FLOKI_LOCAL_DATA, etc.)
- The import of `shadow_model`

### Modify:
- Agent call in `_analysis_cycle()`: instead of calling local model every minute, call Agent (Gemini) every 5 minutes
- Keep the existing `_call_agent_proactive_snapshot()` flow but ensure it uses Gemini client
- Simba triggers (SIMBA_WAKE, SIMBA_WATCH) can still trigger out-of-cycle Gemini calls for urgent situations

### Keep:
- Brain/Scanner cycle (1 minute) — data collection unchanged
- Monitor cycle — position monitoring unchanged
- Simba 5-min summaries
- Trade Room feed writes
- Discord alerts
- All safety checks
- All execution logic (EA bridge, executor)

---

## 8. CALL FREQUENCY ARCHITECTURE

```
Every 1 minute:
  - Scanner collects data (indicators, news, calendar, S/R)
  - Monitor checks positions (BE, trailing, safety)
  - Simba evaluates conditions (wake/watch)

Every 5 minutes:
  - Floki (Gemini 3 Flash) called with full tool access
  - Can call 15-25 tools iteratively
  - Makes decision: WAIT / OPEN / HOLD / CLOSE / ADJUST
  - Debates with Rex (GPT-4o) on OPEN/CLOSE decisions

On Simba trigger (out of cycle):
  - Simba detects critical condition
  - Floki called immediately (doesn't wait for 5-min boundary)
```

---

## 9. COST ESTIMATE

| Scenario | Calls/Day | Est. Tokens/Call | Daily Cost |
|---|---|---|---|
| 5-min interval (no positions) | 288 | ~80K input + 3K output | ~$17/day |
| 5-min interval (with positions, more tools) | 288 | ~120K input + 5K output | ~$25/day |
| 15-min interval alternative | 96 | ~80K input + 3K output | ~$5.76/day |

---

## 10. VERIFICATION CHECKLIST (after migration)

After restart on Gemini, verify ALL of these:

- [ ] `FLOKI | model=gemini-3-flash` in startup log
- [ ] Floki makes first decision within 5 minutes
- [ ] Floki calls multiple tools (AGENT_TOOL log lines)
- [ ] Rex debate works on OPEN/CLOSE decisions
- [ ] Session memory read/write works
- [ ] Watch conditions set correctly (real ticket, not 0)
- [ ] Simba 5-min summaries continue
- [ ] Simba wake/watch triggers work
- [ ] No phantom position in AGENT_MONITOR
- [ ] Safety checks use MT5 positions (not cache)
- [ ] OPEN_BUY/OPEN_SELL executes when valid
- [ ] Trade Room shows Floki decisions
- [ ] Discord alerts work
- [ ] Dashboard shows all data correctly
- [ ] No Claude API error spam in logs
- [ ] No SHADOW_* or FLOKI_LOCAL_* lines in logs
- [ ] Zero import errors on startup

---

*FlokiWatch — Gemini 3 Flash Migration Handoff — March 18, 2026*
