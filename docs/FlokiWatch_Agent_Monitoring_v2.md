# FlokiWatch Agent Monitoring Architecture v2.0

## Overview
Replace the current H1-only Proactive snapshot with a 3-level monitoring system:
- Level 1: Agent Full Analysis every 30 minutes
- Level 2: Python Monitor every 1 minute (zero cost)
- Level 3: Agent Fast Decision when triggers fire

## Architecture

```
Every 30 min → Agent Full Analysis (structure, macro, indicators, everything)
                 ↓
              Output: OPEN / WAIT+conditions / HOLD / CLOSE / ADJUST
                 ↓
Python Monitor (every 1 min, no Claude, zero cost)
  ├── Check entry conditions (from Agent's WAIT output)
  ├── Check SL/TP proximity (active trade)
  ├── Check calendar events (HIGH impact approaching)
  ├── Check breakout (large price move in short time)
  └── Check session change (London/NY open)
                 ↓ (trigger fires)
Agent Fast Decision (minimal data, quick response)
  → Confirm entry / Manage trade / React to event
```

## Level 1 — Agent Full Analysis (every 30 minutes)

### What changes from current H1:
- Frequency: H1 (60 min) → 30 minutes
- Everything else stays the same: XML format, all data sources, memory, trade continuity

### New output field for WAIT decisions: `entry_conditions`
When the Agent says WAIT, it must provide concrete conditions:

```json
{
  "decision": "WAIT",
  "confidence": 70,
  "reasoning": "...",
  "entry_conditions": {
    "direction": "SELL",
    "conditions": [
      {"type": "price_touch", "level": 5197.0, "description": "Price touches Fib 23.6% resistance"},
      {"type": "price_break", "level": 5172.0, "direction": "below", "description": "Price breaks below support with volume"}
    ],
    "validity_minutes": 180,
    "preferred_entry": 5197.0,
    "sl": 5210.0,
    "tp": 5152.0
  }
}
```

If the Agent says WAIT without conditions, that's fine — it means "nothing interesting, check back in 30 min."

### Implementation:
- In main.py: change proactive trigger from H1 close to every 30 minutes
- In agent_prompts.py: add entry_conditions to the output format for WAIT decisions
- In ai_agent.py: parse entry_conditions from Agent response
- In db_writer.py: persist entry_conditions

## Level 2 — Python Monitor (every 1 minute)

### Zero Claude cost — pure Python logic

Create a new file: `agent_monitor.py`

This runs every 1 minute (or every analysis cycle, whichever is more frequent) and checks:

### Trigger 1: Entry Condition Met
- Read the latest entry_conditions from the Agent's last WAIT decision
- Check if current price meets any condition:
  - price_touch: abs(current_price - level) < threshold (e.g., 2 points)
  - price_break: price crossed level in the specified direction
- If met → set trigger flag

### Trigger 2: Trade at Risk
- If active trade exists (from Agent's OPEN decision):
  - Distance to SL < 5 points → trigger
  - Distance to TP < 5 points → trigger (consider taking profit)
  - Trade P&L went from positive to negative → trigger

### Trigger 3: Calendar Event
- If HIGH impact event in upcoming_events with time_until < 15 minutes → trigger (pre-event)
- If HIGH impact event just passed (time_until was <5 min in previous check, now >5 min ago) → trigger (post-event)

### Trigger 4: Breakout Detection
- If price moved > 15 points in the last 5 minutes → trigger
- Calculate from M5 candles or from price history

### Trigger 5: Session Change
- London open (08:00 UTC) → trigger
- NY open (13:00 UTC) → trigger
- Only trigger once per session

### When trigger fires:
- Call the Agent with trigger_type="CONDITION_MET" or "TRADE_AT_RISK" or "EVENT_APPROACHING" etc.
- Pass minimal data: current price, M5 candles, the specific trigger info, active trade context
- Agent responds quickly with a decision

### Implementation:
- New file: agent_monitor.py with class AgentMonitor
- Stores: current entry_conditions, active_trade_context, last_trigger_times (to prevent spam)
- Anti-spam: minimum 5 minutes between triggers of the same type
- In main.py: call agent_monitor.check() every cycle (5 min) or create a separate 1-minute timer

## Level 3 — Agent Fast Decision

### Lighter than full analysis
When a trigger fires, the Agent receives a SMALLER data package:
- Current price (bid/ask)
- Last 10 M5 candles (micro-structure)
- The trigger details (which condition met, what level, what event)
- Active trade context (if applicable)
- Calendar upcoming (if relevant)

NOT included (saves tokens and time):
- Full 50 H1 candles (already analyzed 30 min ago)
- Full H4/D1 candles
- Full indicators
- Full S/R zones
- Headlines

### Expected response time: 3-5 seconds (smaller payload = faster)

### Valid decisions for Fast calls:
- OPEN_BUY / OPEN_SELL (confirm entry)
- HOLD_TRADE / ADJUST_TRADE / CLOSE_TRADE (manage position)
- CANCEL_CONDITION (condition met but context changed, don't enter)
- WAIT (not interested, go back to monitoring)

### Implementation:
- New function in ai_agent.py: agent_fast_decide(trigger_data)
- New XML format: format_fast_xml(trigger_data) — compact, ~30% of full XML size
- New prompt section or separate prompt for fast decisions

## Implementation Order (phases)

### Phase 1: 30-minute analysis + entry_conditions (can code now)
1. Change proactive frequency from H1 to 30 minutes
2. Add entry_conditions to WAIT output format in prompt
3. Parse and persist entry_conditions
4. Test: Agent produces conditions, stored in DB

### Phase 2: Python Monitor (can code now)
1. Create agent_monitor.py with all 5 triggers
2. Integrate into main.py loop
3. Test: monitor detects conditions, logs trigger events

### Phase 3: Agent Fast Decision (after Phase 1+2 working)
1. Create fast decision prompt and XML format
2. Create agent_fast_decide() function
3. Wire: monitor trigger → fast decision → execution
4. Test: full cycle from condition → trigger → confirm → trade

### Phase 4: Discord Integration
1. Alerts for all trigger events and fast decisions
2. Compact format for monitor events, full format for trades

## Cost Estimate
- Level 1 (30 min): ~48 calls/day × $0.015 = ~$0.72/day
- Level 3 (fast): ~5-10 calls/day × $0.005 = ~$0.05/day
- Total: ~$0.77/day (~$23/month)
- Current (H1 only): ~$0.36/day (~$11/month)
- Increase: ~$12/month for significantly better entry detection and trade management

## Files to Create/Modify
- NEW: agent_monitor.py (Python monitor with triggers)
- MODIFY: main.py (30-min frequency, monitor integration, fast calls)
- MODIFY: agent_prompts.py (entry_conditions in output, fast decision prompt)
- MODIFY: ai_agent.py (parse entry_conditions, agent_fast_decide)
- MODIFY: agent_data_builder.py (format_fast_xml)
- MODIFY: db_writer.py (persist entry_conditions)
- MODIFY: alerts.py (trigger alerts)
- MODIFY: FIELD_CONTRACT.md (new fields)

## Non-Negotiable Rules
- Python Monitor NEVER executes trades — only triggers Agent calls
- Agent Fast Decision follows same risk rules as full analysis
- Anti-spam: minimum 5 minutes between triggers of same type
- All triggers and decisions logged and visible on dashboard
- Entry conditions have expiry (validity_minutes) — expired conditions are ignored
- GitHub commits for every change
