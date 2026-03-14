# Monday Verification Checklist (March 16, 2026)

Market opens Sunday 22:00 UTC. Bot should be running on commit 57dfd87.

---

## Phase 1: Startup (Sunday 22:00 UTC)

### 1.1 Clean startup
- Restart bot: python main.py
- Verify: "AI Agent initialized: model=claude-sonnet-4-20250514, mode=active, timeout=60s"
- Verify: No import errors
- Verify: DB migrations applied (tool_trace columns)
- Verify: "Market open" detected (not "Weekend — market closed")

### 1.2 First Brain cycle
- Wait 60 seconds
- Verify: Brain analysis completes (Tech/News/ML/Momentum/Calendar scores in log)
- This populates the cache that tools read from

---

## Phase 2: First Agent Call (within first 30 min)

### 2.1 Tool use works
- When PROACTIVE_H1 fires, verify log shows: "AGENT_TOOL | get_current_price | Xms"
- Verify: Agent calls MULTIPLE tools (not just one)
- Verify: Agent returns a valid JSON decision (WAIT, OPEN, etc.)

### 2.2 Tool trace logged
- Search log: Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "AGENT_TOOL" | Select-Object -Last 20
- Should show sequence of tool calls with latency

### 2.3 Session memory
- After first Agent call, check: Get-Content -Path .\data\agent_session_memory.json -Raw
- Should exist and contain session_notes from the Agent

### 2.4 No errors
- Search: Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "ERROR|CRITICAL|Traceback" | Select-Object -Last 10
- Should be empty or only non-critical warnings

---

## Phase 3: First Trade (when Agent decides OPEN)

### 3.1 execute_trade tool fires
- Log shows: "AGENT_TOOL | execute_trade | direction=SELL sl=X tp=Y"
- Safety checks pass (or fail with clear reason)
- Trade opens in MT5

### 3.2 BE/trailing parameters
- If Agent specified custom BE/trailing, verify in brain_signal.json
- If not specified, verify defaults calculated from SL distance

### 3.3 Watch conditions set
- After trade opens, check if Agent called set_watch_conditions
- Verify: data/agent_watch_conditions.json contains the ticket

### 3.4 Balance capture
- Log shows: "BALANCE_CAPTURE | ticket=#X | balance=$Y"

---

## Phase 4: Position Management

### 4.1 Watch condition evaluation
- Monitor checks conditions every 1 minute (when market open)
- If condition triggers: Agent called with WATCH_CONDITION trigger type

### 4.2 Scheduled re-evaluation
- Every 30 minutes: Agent called with PROACTIVE trigger
- Agent should call get_open_positions() to check trade status

---

## Phase 5: Trade Close

### 5.1 Position closes (EA trailing/SL/TP)
- "Position #X disappeared" in log
- "BALANCE_DIFF | ticket=#X | open=$Y | now=$Z | diff=$W"

### 5.2 Deal resolution
- "DEAL_REFRESH | Forcing MT5 reconnect for ticket #X"
- N2.5 today-only search attempts to find deal
- Deal resolver subprocess if needed

### 5.3 Watch conditions cleared
- data/agent_watch_conditions.json: ticket removed after close

---

## Diagnostic Commands (run without restarting)

# Tool use evidence
Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "AGENT_TOOL" | Select-Object -Last 30

# Session memory
Get-Content -Path .\data\agent_session_memory.json -Raw

# Watch conditions
Get-Content -Path .\data\agent_watch_conditions.json -Raw

# Errors
Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "ERROR|Traceback" | Select-Object -Last 10

# Agent decisions
Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "Agent decision:" | Select-Object -Last 10

# Balance capture
Select-String -Path .\logs\trading_bot_2026-03-16.log -Pattern "BALANCE_CAPTURE|BALANCE_DIFF" | Select-Object -Last 10

---

## Rollback Plan
If critical failures occur:
- Last stable commit before tool-use: 24c4241 (prompt refinement, old XML architecture)
- git checkout 24c4241 -- ai_agent.py main.py agent_prompts.py
- Restart bot
- This restores the old single-call XML flow
