# Monday Monitoring Checklist (March 10, 2026)

Market opens Sunday 22:00 UTC. First trades expected Monday morning.

---

## 1. First New Trade — Validate open_price Recording

**What to check:** After first trade opens, confirm `open_price` in `history.db` matches actual MT5 fill price.

**How to verify:**
```sql
SELECT ticket, open_price FROM trades ORDER BY open_time DESC LIMIT 1;
```
Compare with MT5 terminal actual fill price.

**Validates:** Commit 1478825 (record actual MT5 fill price)

---

## 2. Agent v1.2 First 3 Decisions — Validate MTF Data

**What to check:**
- `d1_direction` and `h4_direction` are NOT "?" (must be "bullish" or "bearish")
- Strong REJECTs show confidence 70-90, not 25
- Calendar score mentioned in reasoning when relevant

**How to verify:** Check Discord Agent messages or `data/bot_state.json` → `last_analysis.agent_decision`

**Validates:** Agent v1.2 prompt with MTF awareness

---

## 3. EA Bridge — Confirm No FALLBACK Events

**What to check:** No FALLBACK events during normal operation.

**Context:** Threshold now 120s, recompile gap ~45s should no longer trigger.

**How to verify:** Check logs for "FALLBACK" keyword.

---

## 4. Agent v1.3 Deployment

**Prerequisite:** Items 1 and 2 above MUST be confirmed first.

**Action:** After v1.2 first 3 live decisions are confirmed with correct MTF data, deploy v1.3.

**What to verify after deployment:**
- First REJECT after v1.3 deployment should show:
  - Market View (direction + description)
  - Conditions to Approve (2-4 specific conditions)
  - Invalidation Timeframe (e.g., "3 H1 candles")
- All three fields visible in both Discord and dashboard Agent Memory section

**Commits ready (not yet deployed):**
- 105e263: agent_memory.py module
- 5fb4e36: v1.3 REJECT requirements in prompt
- 6363181: memory integration in ai_agent.py
- 357a024: agent_memory in state_writer
- d3d70f0: FIELD_CONTRACT.md updates
- f095787: renderAgentMemory in app.js
- 2ecf030: Agent Memory section in index.html

---

## Weekend Summary (March 7-8, 2026)

**Commits delivered:**
- 84e6210: Startup log shows dynamic breakeven (50% of SL)
- 1478825: Record actual MT5 fill price in history.db
- 273e520: Population B analysis script
- 105e263 → 2ecf030: Agent v1.3 memory system (7 commits)

**Analysis completed:**
- Spread/slippage: $11.81 total cost, explains 9.2% of PF gap
- 33-trade Population B: 9 SUSTAINED losses, 3 SPIKE losses, 2 BE saves
- Trade 29 gap-through: 45 pips below SL, early Monday Asian session
- Brain confidence calibration issue logged: 5 trades with 100% confidence had 382-601 pips MAE

**System state:**
- No changes to trading logic, Brain, or parameters
- Agent v1.2 in shadow mode
- EA Bridge operational, FALLBACK threshold 120s
