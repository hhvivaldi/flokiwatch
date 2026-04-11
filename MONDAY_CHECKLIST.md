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
- `AI Agent initialized: model=qwen3.6-plus, mode=active`
- No import errors or tracebacks
- `Market open` detected (not "Weekend — market closed")

### 1.2 Start the dashboard
```powershell
python -m uvicorn dashboard.server:app --host 0.0.0.0 --port 8080
```
Access: http://localhost:8080/trade-room

### 1.3 First Brain cycle (within 60s)
- Brain analysis completes: Tech/News/ML/Momentum/Calendar scores in log
- Calendar events show UTC times (FLO-96 fix, commit 675e960)
- This populates the cache that Floki's tools read from

---

## Phase 2: Agent Verification (first 30 min)

### 2.1 Floki (Qwen 3.6-Plus)
- `FLOKI_SCHEDULE | Calling Floki now (timer due)` in log
- Floki calls `get_luna_brief` + `get_echo_alerts` at start
- Returns valid decision: WAIT/OPEN_BUY/OPEN_SELL/HOLD_TRADE/CLOSE_TRADE
- Trade Room card shows decision + confidence %
- **NEW**: Identity is neutral ("the XAU/USD trader" — no "intraday" label)
- **NEW**: Position phases show "OPEN/SL_ADJUSTED/TRAILING" (not BREAKEVEN)
- **NEW**: `get_calendar` works in position_mode (cfc2893)

### 2.2 Luna (MiMo-V2-Flash) — every 15 min
- `LUNA | MiMo analysis — SAFE/CAUTION/DANGER` in log
- `data/luna_brief.json` exists and is fresh (< 30 min)
- Trade Room Luna card shows environment badge + risk level
- **CRITICAL**: If Luna log says "AI unavailable — using local fallback", check LUNA_API_KEY

### 2.3 Rex (GPT-4o) — analyst with 11 tools + Bull/Bear debate
- Rex Bull/Bear debate injects into trigger_context before each Floki cycle
- Check log for `REX_DEBATE | INJECTED` or `REX_DEBATE | SKIPPED`
- Rex monitor runs every 30 min (get_rex_monitor tool)

### 2.3.1 Research Manager (Gemini 3 Flash) — verdict after debate
- Check log for `RESEARCH_MANAGER | OK | winner=BULL/BEAR | rec=...`
- If OK: `<verdict>` block injected. If FAIL: falls back to `<debate>` block

### 2.4 Simba (Python) — every 30s
- `SIMBA_CHECK` events in log
- Trade Room shows patrol reports
- Simba monitors wake/watch conditions (10 types)

### 2.5 Echo (MiMo-V2-Flash) — every 5 min
- `[ECHO] Direct RSS: N found` in log
- IMPORTANT/CRITICAL alerts in Trade Room NEWS tab
- **CRITICAL**: If Echo fails, check LUNA_API_KEY (shared key)

### 2.6 Session memory
```powershell
Get-Content .\data\agent_session_memory.json -Raw
```
- Sage briefing should show: "Best session: off_hours/ny. Worst: london"
- **NOT** "Best session: asian. Worst: ny" (old inverted data, fixed in 5b0eba3)

### 2.7 No errors
```powershell
Select-String -Path .\logs\trading_bot_*.log -Pattern "ERROR|CRITICAL|Traceback" | Select-Object -Last 10
```

---

## Phase 3: First Trade (when Floki decides OPEN)

### 3.1 Market order execution
- Log: `AGENT_TOOL | execute_trade | direction=BUY/SELL sl=X tp=Y`
- Safety checks pass
- Trade opens in MT5 with real ticket
- `record_trade_open` creates DB row with real ticket

### 3.2 Pending order execution
- Log: `AGENT_TOOL | place_pending_order | BUY_LIMIT/SELL_LIMIT @ X`
- **NEW**: `record_trade_open(ticket=0)` creates DB row at placement (FLO-269 fix ed1a768)
- When filled: `PENDING_FILL_DB | ticket=0 -> #REAL` updates the row
- Pending fill detection works (f9df751 fix)

### 3.3 Watch conditions + balance capture
- After trade: Floki calls `set_watch_conditions`
- Log: `BALANCE_CAPTURE | ticket=#X | balance=$Y`

### 3.4 SL/TP tracking (FLO-269)
- Every SL/TP modification logged to `trade_adjustments` table
- Sources: `floki_adjust`, `monitor_breakeven`, `monitor_trailing`
- MFE/MAE tracked in agent_monitor (written to DB on close)

---

## Phase 4: Ongoing Monitoring

### 4.1 Agent scheduling
- Floki self-schedules via `set_next_check` (5-120 min)

### 4.2 Post-trade report injection (FLO-269)
- After first trade closes, check: `data/post_trade_reports/{ticket}.json` exists
- Next Floki cycle should show `<last_trade_report>` in trigger_context
- Verify: `get_trade_journal` tool returns data (call manually if needed)

### 4.3 Trade Journal tool
- Floki can call `get_trade_journal` to review past 20 trades
- Returns MFE, capture rate, SL adjustments, counterfactual verdicts
- Filter by session or direction

### 4.4 Sage daily audit (21:00 UTC)
- Runs at 21:00 UTC (Mon-Fri)
- **NEW**: Session assignments use corrected timezone (5b0eba3)
- After Sage: `run_eod_counterfactuals()` runs automatically
- After counterfactuals: ChromaDB reflexions enriched with real data
- Report in `data/sage_report.json`

---

## Phase 5: Trade Close

### 5.1 Position closes (SL/TP or Floki CLOSE_TRADE)
- `Position #X disappeared` in log
- `BALANCE_DIFF | ticket=#X | diff=$W`
- MFE/MAE written to trades table
- Post-trade report generated: `data/post_trade_reports/{ticket}.json`
- Reflexion generated (GPT-5.4 daemon thread)
- Hindsight scheduled (1h delayed)

### 5.2 Post-close verification
- `POST_TRADE_REPORT | #{ticket} | P&L=... | MFE=... | capture=...` in log
- Watch conditions cleared for that ticket
- Next Floki cycle sees `<last_trade_report>` in context

---

## Trade Room Verification

### New sections (FLO-269)
- **TRADE JOURNAL** tier (collapsed, bottom of page) — expand to see table
- Journal shows: Ticket, Dir, Session, P&L, MFE, Capture%, #Adj, Verdict
- Verdict badges: green SAVED, red COST, gray NEUTRAL
- Stats bar: Avg Capture Rate, Adj Helped %, Adj Hurt %
- API: `/api/journal` returns data

---

## Diagnostic Commands

```powershell
# Recent Floki tool calls
Select-String -Path .\logs\trading_bot_*.log -Pattern "AGENT_TOOL" | Select-Object -Last 20

# Trade adjustments for a ticket
python -c "from db_writer import get_trade_adjustments; print(get_trade_adjustments(TICKET))"

# Post-trade report
Get-Content .\data\post_trade_reports\TICKET.json -Raw

# Trade journal (manual test)
python -c "from agent_tools import AgentTools; # ... see Rule 20"

# Session memory
Get-Content .\data\agent_session_memory.json -Raw

# Sage report
Get-Content .\data\sage_report.json -Raw

# Errors only
Select-String -Path .\logs\trading_bot_*.log -Pattern "ERROR|Traceback" | Select-Object -Last 10
```

---

## Key Config Values

| Setting | Value | File |
|---------|-------|------|
| Floki model | qwen3.6-plus | config.py (FLOKI_MODEL) |
| Rex model | gpt-5-mini | rex_validator.py |
| Echo model | mimo-v2-flash (Xiaomi API) | config.py |
| Luna model | mimo-v2-flash (Xiaomi API) | config.py |
| Sage model | gemini-3-flash-preview | sage_auditor.py |
| Brain cycle | 60s | config.py (ANALYSIS_INTERVAL_SECONDS) |
| Monitor cycle | 10s | monitor.py |
| Simba cycle | 30s | agent_monitor.py |
| Echo scan | 300s (5 min) | config.py |
| Luna scan (open) | 900s (15 min) | config.py |
| Floki default interval | 300s (5 min) | config.py |
| MT5 server UTC offset | +2h | config.py (MT5_SERVER_UTC_OFFSET) |
| Dashboard port | 8080 | uvicorn command |

---

## Trade Room Market Hours

| Period | Floki/Rex/Simba/Sage | Echo | Luna |
|--------|---------------------|------|------|
| Market open (Mon 22:00 → Fri 21:00 UTC) | Normal | ACTIVE | ACTIVE (15 min) |
| Daily pause (21:00-22:00 UTC Mon-Thu) | COFFEE BREAK | ON WATCH | COFFEE BREAK (30 min) |
| Weekend (Fri 21:00 → Sun 21:00 UTC) | REST DAY | ON WATCH | REST DAY (sleeps) |
| Pre-market (Sun 21:00, 1h before open) | REST DAY | ON WATCH | Wakes — 1 fresh brief |

---

## Post-Deploy Checks (FLO-269 Pipeline)

- [ ] First trade close: `POST_TRADE_REPORT` log line appears
- [ ] `data/post_trade_reports/{ticket}.json` created with MFE/MAE
- [ ] Next Floki cycle: `<last_trade_report>` visible in trigger_context
- [ ] `get_trade_journal` tool returns trades with capture rate
- [ ] Trade Room TRADE JOURNAL section visible and populated
- [ ] At 21:00 UTC: `EOD_COUNTERFACTUAL` log lines appear
- [ ] After EOD: `RICH_REFLEXION` log lines confirm ChromaDB enrichment
- [ ] Pending orders: `record_trade_open(ticket=0)` at placement, updated on fill
