# Monday Verification Checklist

Market opens Sunday 22:00 UTC. All agents should be running.

---

## Phase 1: Startup (Sunday ~21:50 UTC)

### 1.1 Start the bot
```powershell
cd C:\Users\Hermano\OneDrive\Desktop\XAUUSD
python main.py
```

Verify in logs:
- `AI Agent initialized: model=gemini-3-flash-preview, mode=active`
- No import errors or tracebacks
- `Market open` detected (not "Weekend — market closed")

### 1.2 Start the dashboard
```powershell
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
```
Access: http://localhost:8080/trade-room

### 1.3 First Brain cycle (within 60s)
- Brain analysis completes: Tech/News/ML/Momentum/Calendar scores in log
- This populates the cache that Floki's tools read from

---

## Phase 2: Agent Verification (first 30 min)

### 2.1 Floki (Gemini 3 Flash)
- `FLOKI_SCHEDULE | Calling Floki now (timer due)` in log
- Floki calls `get_luna_brief` + `get_echo_alerts` at start (NOT `get_macro`/`get_headlines` — Luna handles those)
- Returns valid decision: WAIT/OPEN_BUY/OPEN_SELL/HOLD_TRADE/CLOSE_TRADE
- Trade Room card shows decision + confidence %

### 2.2 Luna (MiMo-V2-Flash) — every 15 min
- `LUNA | MiMo analysis — SAFE/CAUTION/DANGER` in log
- `data/luna_brief.json` exists and is fresh (< 30 min)
- Trade Room Luna card shows environment badge + risk level
- MACRO tab shows Luna messages
- **CRITICAL**: If Luna log says "AI unavailable — using local fallback", check LUNA_API_KEY

### 2.3 Rex (GPT-4o) — on OPEN/CLOSE only
- Rex debates Floki's reasoning (AGREE/DISAGREE)
- HOLD/WAIT/ADJUST decisions **skip Rex** (cost optimization FLO-50)
- Trade Room shows Rex message with structured badge

### 2.4 Simba (Python) — every 30s
- `SIMBA_CHECK` events in log
- Trade Room shows patrol reports (price, conditions, trend, levels)
- Simba card expand panel shows condition details
- If Floki set wake conditions: Simba monitors 10 condition types

### 2.5 Echo (MiMo-V2-Flash) — every 5 min
- `[ECHO] Direct RSS: N found` in log (11/11 feeds ok)
- `[ECHO] N scanned, M fresh, K pass keyword filter`
- IMPORTANT/CRITICAL alerts appear in Trade Room NEWS tab
- Echo card shows: ACTIVE, last scan time, alert counts, feed health (25/25 healthy)
- **CRITICAL**: If Echo fails, check LUNA_API_KEY (shared key with Luna)

### 2.6 Session memory
```powershell
Get-Content .\data\agent_session_memory.json -Raw
```
Should contain Floki's session notes after first call.

### 2.6 No errors
```powershell
Select-String -Path .\logs\trading_bot_*.log -Pattern "ERROR|CRITICAL|Traceback" | Select-Object -Last 10
```

---

## Phase 3: First Trade (when Floki decides OPEN)

### 3.1 Trade execution
- Log: `AGENT_TOOL | execute_trade | direction=BUY/SELL sl=X tp=Y`
- Safety checks pass
- Trade opens in MT5
- Trade Room shows Floki decision with green/red badge

### 3.2 Watch conditions
- After trade: Floki calls `set_watch_conditions`
- Simba starts monitoring those conditions
- `data/agent_watch_conditions.json` contains the ticket

### 3.3 Balance capture
- Log: `BALANCE_CAPTURE | ticket=#X | balance=$Y`

---

## Phase 4: Ongoing Monitoring

### 4.1 Agent scheduling
- Floki self-schedules via `set_next_check` (5-120 min)
- Check: `data/agent_next_check.json` for next_check_at timestamp

### 4.2 Simba wake triggers
- If wake condition met: `SIMBA_WAKE | Floki called immediately`
- Floki reviews market and decides action

### 4.3 Echo news alerts
- CRITICAL → Simba wake → Floki called (max 2/hr) + Luna out-of-cycle trigger
- IMPORTANT → stored in `data/echo_alerts.json` → Luna reads on next cycle
- Floki gets CRITICAL via `get_echo_alerts`, routine news via `get_luna_brief`
- Trade Room NEWS tab shows Echo alerts, MACRO tab shows Luna briefs

### 4.4 Luna macro briefs
- Runs every 15 min (market open), 30 min (daily pause), sleeps during weekend
- When Luna brief is fresh: Floki's `get_macro` and `get_headlines` tools are REMOVED
- When Luna brief is stale (>30 min): fallback tools auto-restore with log:
  `LUNA | Brief stale — Floki using raw macro tools (fallback)`
- Check feed health: `Get-Content .\data\echo_feed_health.json -Raw`

### 4.5 Sage daily audit
- Runs at 21:00 UTC (Mon-Fri)
- Report in `data/sage_report.json`
- Trade Room Sage card shows win rate + profit factor

---

## Phase 5: Trade Close

### 5.1 Position closes (EA trailing/SL/TP or Floki CLOSE_TRADE)
- `Position #X disappeared` in log
- `BALANCE_DIFF | ticket=#X | diff=$W`
- Deal resolver finds matching MT5 deal

### 5.2 Post-close
- Watch conditions cleared for that ticket
- Floki re-evaluates market on next scheduled call

---

## Diagnostic Commands

```powershell
# Recent Floki tool calls
Select-String -Path .\logs\trading_bot_*.log -Pattern "AGENT_TOOL" | Select-Object -Last 20

# Echo scan results
Select-String -Path .\logs\trading_bot_*.log -Pattern "\[ECHO\]" | Select-Object -Last 10

# Simba status
Get-Content .\data\agent_wake_conditions.json -Raw

# Session memory
Get-Content .\data\agent_session_memory.json -Raw

# Next Floki check
Get-Content .\data\agent_next_check.json -Raw

# Echo alerts
Get-Content .\data\echo_alerts.json -Raw

# Luna brief
Get-Content .\data\luna_brief.json -Raw

# Feed health
Get-Content .\data\echo_feed_health.json -Raw

# Luna cost
Get-Content .\data\luna_daily_cost.json -Raw

# Errors only
Select-String -Path .\logs\trading_bot_*.log -Pattern "ERROR|Traceback" | Select-Object -Last 10
```

---

## Key Config Values

| Setting | Value | File |
|---------|-------|------|
| Floki model | gemini-3-flash-preview | config.py |
| Rex model | gpt-4o | rex_validator.py |
| Echo model | mimo-v2-flash (Xiaomi API) | config.py |
| Luna model | mimo-v2-flash (Xiaomi API) | config.py |
| Brain cycle | 60s | config.py (ANALYSIS_INTERVAL_SECONDS) |
| Monitor cycle | 10s | monitor.py |
| Simba cycle | 30s | simba_watcher.py |
| Echo scan | 300s (5 min) | config.py (ECHO_SCAN_INTERVAL_SECONDS) |
| Luna scan (open) | 900s (15 min) | config.py (LUNA_SCAN_INTERVAL_SECONDS) |
| Luna scan (closed) | 1800s (30 min) | config.py (LUNA_SCAN_INTERVAL_CLOSED) |
| Floki default interval | 300s (5 min) | config.py (FLOKI_CALL_INTERVAL) |
| Echo max wakes/hr | 2 | config.py (ECHO_MAX_WAKES_PER_HOUR) |
| Echo daily cost cap | $1.00 | config.py (ECHO_DAILY_COST_CAP) |
| Luna daily cost cap | $1.00 | config.py (LUNA_DAILY_COST_CAP) |
| Dashboard port | 8080 | uvicorn command |

---

## Trade Room Market Hours

| Period | Floki/Rex/Simba/Sage | Echo | Luna |
|--------|---------------------|------|------|
| Market open (Mon 22:00 → Fri 21:00 UTC) | Normal status | ACTIVE | ACTIVE (15 min) |
| Daily pause (21:00-22:00 UTC Mon-Thu) | COFFEE BREAK | ON WATCH | COFFEE BREAK (30 min) |
| Weekend (Fri 21:00 → Sun 21:00 UTC) | REST DAY | ON WATCH | REST DAY (sleeps) |
| Pre-market (Sun 21:00, 1h before open) | REST DAY | ON WATCH | Wakes — 1 fresh brief |
