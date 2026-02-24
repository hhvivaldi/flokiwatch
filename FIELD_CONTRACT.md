# FIELD CONTRACT — Bot ↔ Dashboard Data Interface

**Any change to the dashboard that touches data fields requires updating this contract and confirming both sides match before deployment.**

---

## Source of Truth

- **Bot writes**: `data/bot_state.json` via `state_writer.py`
- **Server reads**: `data/bot_state.json` via `dashboard/server.py` → serves at `/api/state`
- **Dashboard reads**: `/api/state` via `dashboard/static/app.js`

---

## Field Map

### Top-Level Fields

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `timestamp` | string (ISO 8601) | `state_writer.py` | `render()` → `#last-update` |
| `_expected_update_interval_seconds` | int | `state_writer.py` | `server.py` (staleness check) |
| `_meta.file_age_seconds` | float | `server.py` (injected) | `render()` → `setStaleUI()` |
| `_meta.reason` | string | `server.py` (injected) | not directly used |

### `bot` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `bot.status` | `"OPERATIONAL"` \| `"OFFLINE"` | `state_writer.py` | `render()` → `setStatusDot()`, `setStaleUI()` |
| `bot.mode` | string | `state_writer.py` | `render()` → `#mode` |
| `bot.running` | bool | `state_writer.py` | not directly used |
| `bot.session_start` | string (ISO) \| null | `state_writer.py` | not directly used |
| `bot.session_analyses` | int | `state_writer.py` | not directly used |
| `bot.uptime_seconds` | int \| null | `state_writer.py` | not directly used |

### `market` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `market.is_open` | bool | `state_writer.py` | `render()` → `#market`, market closed logic |
| `market.reason` | string | `state_writer.py` | `render()` → market closed display |
| `market.next_open` | string (ISO) \| null | `state_writer.py` | `render()` → reopens display |

### `account` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `account.balance` | float \| null | `state_writer.py` | `render()` → `#balance` |
| `account.equity` | float \| null | `state_writer.py` | `render()` → `#equity` |
| `account.margin` | float \| null | `state_writer.py` | not directly used |
| `account.free_margin` | float \| null | `state_writer.py` | not directly used |
| `account.profit` | float \| null | `state_writer.py` | not directly used |
| `account.leverage` | int \| null | `state_writer.py` | not directly used |
| `account.currency` | string \| null | `state_writer.py` | not directly used |

### `daily_stats` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `daily_stats.date` | string (YYYY-MM-DD) | `state_writer.py` | not directly used |
| `daily_stats.trades` | int | `state_writer.py` | not directly used |
| `daily_stats.wins` | int | `state_writer.py` | `renderTrades()` → `#trades-w` |
| `daily_stats.losses` | int | `state_writer.py` | `renderTrades()` → `#trades-l` |
| `daily_stats.breakevens` | int | `state_writer.py` | `renderTrades()` → `#trades-be` |
| `daily_stats.pnl` | float | `state_writer.py` | `render()` → `#pnl` |
| `daily_stats.pnl_percent` | float | `state_writer.py` | `render()` → `#pnl` |

### `last_analysis` Object (5 Pillars + Signal)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `last_analysis.decision` | string | `main.py` → `state_writer.py` | `render()` → `#goldcon-decision` |
| `last_analysis.final_score` | float | `main.py` → `state_writer.py` | `render()` → `#goldcon-score`, gauge |
| `last_analysis.confidence` | float | `main.py` → `state_writer.py` | `render()` → `#goldcon-conf` |
| `last_analysis.confidence_level` | string | `main.py` → `state_writer.py` | not directly used |
| `last_analysis.scenario` | string | `main.py` → `state_writer.py` | `render()` → scenario display |
| `last_analysis.scenario_description` | string | `main.py` → `state_writer.py` | `render()` → `#goldcon-scenario` |
| `last_analysis.tech_score` | float | `main.py` → `state_writer.py` | `renderPillar("p-tech")` |
| `last_analysis.ml_score` | float | `main.py` → `state_writer.py` | `renderPillar("p-ml")` |
| `last_analysis.momentum_score` | float | `main.py` → `state_writer.py` | `renderPillar("p-mom")` |
| `last_analysis.news_score` | float | `main.py` → `state_writer.py` | `renderPillar("p-news")` |
| `last_analysis.calendar_score` | float | `main.py` → `state_writer.py` | `renderPillar("p-cal")` |
| `last_analysis.current_price` | float | `main.py` → `state_writer.py` | `render()` → `#price` |
| `last_analysis.volatility_status` | string | `main.py` → `state_writer.py` | `render()` → vol banner |
| `last_analysis.volatility_description` | string | `main.py` → `state_writer.py` | `render()` → vol banner |
| `last_analysis.hold_forced` | bool | `main.py` → `state_writer.py` | `render()` → `#goldcon-blocked` |
| `last_analysis.original_decision` | string \| null | `main.py` → `state_writer.py` | `render()` → blocked display |
| `last_analysis.hold_reason` | string \| null | `main.py` → `state_writer.py` | `render()` → blocked display |

### `last_analysis.intel_feed` Object (OSINT)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `intel_feed.headlines` | array | `main.py` | `renderIntelFeed()` → `#intel-headlines` |
| `intel_feed.macro` | object (dxy, yields, vix) | `main.py` | `renderIntelFeed()` → `#intel-macro` |
| `intel_feed.anomalies` | array | `main.py` | `renderIntelFeed()` → anomaly display |
| `intel_feed.analysis_method` | string | `main.py` | `renderIntelFeed()` → `#intel-method` |
| `intel_feed.news_score` | float | `main.py` | not directly used |
| `intel_feed.cache_age_minutes` | float | `main.py` | `renderIntelFeed()` → `#intel-cache-age` |
| `intel_feed.calendar` | object | `main.py` | `renderIntelFeed()` → `#intel-calendar` |
| `intel_feed.gpt_validator` | object | `main.py` | `renderIntelFeed()` → `#intel-gpt` |
| `intel_feed.confirmations` | array | `main.py` | `renderIntelFeed()` → `#intel-tags` |
| `intel_feed.alerts` | array | `main.py` | `renderIntelFeed()` → `#intel-tags` |
| `intel_feed.sr_zones` | array | `main.py` | `renderIntelFeed()` → `#intel-sr-zones` |
| `intel_feed.candlestick_patterns` | object \| null | `main.py` | `renderIntelFeed()` → `#intel-patterns` |

### `last_analysis.intel_feed.candlestick_patterns` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `candlestick_patterns.primary` | object | `main.py` | `renderIntelFeed()` → pattern display |
| `candlestick_patterns.primary.name` | string | `main.py` | pattern name (e.g., "Morning Star") |
| `candlestick_patterns.primary.direction` | `"bullish"` \| `"bearish"` | `main.py` | direction color |
| `candlestick_patterns.primary.base_score` | float | `main.py` | base score before S/R multiplier |
| `candlestick_patterns.primary.sr_multiplier` | float | `main.py` | S/R proximity multiplier (1.0-2.0) |
| `candlestick_patterns.primary.final_score` | float | `main.py` | final score after multiplier |
| `candlestick_patterns.primary.sr_context` | string | `main.py` | S/R zone context description |
| `candlestick_patterns.all_patterns` | array | `main.py` | list of all detected pattern names |

### `last_analysis.mtf_trend` Object (Multi-TF Trend Confirmation)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `mtf_trend.d1_direction` | `"bullish"` \| `"bearish"` \| `null` | `main.py` | `renderIntelFeed()` → `#intel-mtf` |
| `mtf_trend.h4_direction` | `"bullish"` \| `"bearish"` \| `null` | `main.py` | `renderIntelFeed()` → `#intel-mtf` |
| `mtf_trend.alignment` | `"aligned"` \| `"conflict"` \| `"mixed"` \| `"n/a"` | `main.py` | `renderIntelFeed()` → `#intel-mtf` |
| `mtf_trend.confidence_adjustment` | float | `main.py` | `renderIntelFeed()` → `#intel-mtf` |

### `last_analysis.volume_gate` Object (Volume Gate)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `volume_gate.volume_ratio` | float | `main.py` | `renderIntelFeed()` → `#intel-volume` |
| `volume_gate.status` | `"normal"` \| `"low"` \| `"very_low"` | `main.py` | `renderIntelFeed()` → `#intel-volume` |
| `volume_gate.confidence_adjustment` | float | `main.py` | `renderIntelFeed()` → `#intel-volume` |

### `positions` Array

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `positions[].ticket` | int | `state_writer.py` | `renderPositions()` |
| `positions[].direction` | string | `state_writer.py` | `renderPositions()` |
| `positions[].volume` | float | `state_writer.py` | `renderPositions()` |
| `positions[].open_price` | float | `state_writer.py` | `renderPositions()` |
| `positions[].current_price` | float | `state_writer.py` | `renderPositions()` |
| `positions[].sl` | float | `state_writer.py` | `renderPositions()` |
| `positions[].tp` | float | `state_writer.py` | `renderPositions()` |
| `positions[].profit` | float | `state_writer.py` | `renderPositions()` |
| `positions[].profit_pips` | float | `state_writer.py` | `renderPositions()` |

### `trade_history` Array

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `trade_history[].ticket` | int | `state_writer.py` | `renderPositions()`, `renderTrades()` |
| `trade_history[].direction` | string | `state_writer.py` | `renderPositions()` |
| `trade_history[].profit` | float | `state_writer.py` | `renderPositions()`, `renderTrades()` |
| `trade_history[].close_time` | string (ISO) | `state_writer.py` | `renderTrades()`, `renderPositions()` |
| `trade_history[].close_type` | string | `state_writer.py` | `renderPositions()` |
| `trade_history[].reason` | string | `state_writer.py` | `renderPositions()` |

---

## HTML Element IDs Required by app.js

Any dashboard HTML redesign **must** preserve these element IDs or update `app.js` simultaneously:

### Header
`status-dot`, `status-label`, `mode`, `last-update`, `market`, `ea-bridge-status`, `ea-spread`

### Signal Core
`goldcon`, `goldcon-decision`, `goldcon-score`, `goldcon-conf`, `goldcon-scenario`, `goldcon-blocked`, `signal-segments`

### Account & Stats
`balance`, `equity`, `pnl`, `price`, `price-label`

### Pillars
`p-tech-bar`, `p-tech-val`, `p-ml-bar`, `p-ml-val`, `p-mom-bar`, `p-mom-val`, `p-news-bar`, `p-news-val`, `p-cal-bar`, `p-cal-val`

### Trades & Positions
`positions`, `positions-empty`, `positions-count`, `last-trade-evidence`, `lte-dir`, `lte-pnl`, `lte-reason`, `lte-time`, `trades-count`, `trades-w`, `trades-l`, `trades-be`, `trades-wr`

### OSINT Feed
`intel-feed-section`, `intel-method`, `intel-cache-age`, `intel-headlines`, `intel-macro`, `intel-calendar`, `intel-sr`, `intel-sr-zones`, `intel-gpt`, `intel-tags`, `intel-bottom`, `intel-cached-badge`, `intel-mtf`, `intel-volume`, `intel-patterns`

### Banners & Misc
`offline-banner`, `offline-last-update`, `pillars-cached-badge`, `vol-banner`, `news-marquee`, `recent-decisions`

---

## Change Process

1. **Before** any dashboard change touching data fields or element IDs:
   - Update this contract with proposed changes
   - Verify every ID in the "HTML Element IDs" section still exists
   - Run the dashboard with live bot data and confirm all components render

2. **Never** rename or remove an HTML element ID without updating `app.js` in the same commit.

3. **Never** change a field name in `state_writer.py` without updating `app.js` in the same commit.

---

*Last updated: 2026-02-24*

---

## EA Bridge (Optional Execution Mode)

### Overview

The EA Bridge is an **optional** execution mode that separates the Python Brain (analysis) from MT5 execution. When enabled, Python writes signals to a JSON file, and the FlokiBridge EA reads and executes them with tick-by-tick position management.

**Status:** `USE_EA_BRIDGE = False` (disabled until full integration testing)

### Architecture

```
USE_EA_BRIDGE = True AND ea_status.json < 60s old:
┌─────────────┐    brain_signal.json    ┌─────────────┐
│ Python Brain│ ──────────────────────► │ FlokiBridge │
│  (main.py)  │                         │    (EA)     │
│             │ ◄────────────────────── │             │
└─────────────┘    ea_status.json       └─────────────┘

USE_EA_BRIDGE = False OR ea_status.json > 60s old:
┌─────────────┐    MT5 API (direct)     ┌─────────────┐
│ Python Brain│ ──────────────────────► │    MT5      │
│  (main.py)  │ ◄────────────────────── │  Terminal   │
└─────────────┘                         └─────────────┘
```

### Files

| File | Location | Purpose |
|------|----------|---------|
| `FlokiBridge.mq5` | `mql5/` | MT5 EA that executes signals |
| `ea_bridge.py` | project root | Python JSON I/O module |
| `brain_signal.json` | `MQL5\Files\` | Python → EA signals |
| `ea_status.json` | `MQL5\Files\` | EA → Python status |

### Config Options (`config.py`)

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `USE_EA_BRIDGE` | bool | `False` | Enable EA execution mode |
| `EA_STALE_THRESHOLD_SECONDS` | int | `60` | Fallback to direct API if status older than this |
| `BRAIN_SIGNAL_JSON_PATH` | string | `MQL5\Files\brain_signal.json` | Signal file path |
| `EA_STATUS_JSON_PATH` | string | `MQL5\Files\ea_status.json` | Status file path |

### `brain_signal.json` Schema (Python → EA)

| Field | Type | Description |
|-------|------|-------------|
| `version` | int | Schema version (1) |
| `timestamp` | string (ISO) | Signal generation time |
| `signal_id` | string | Unique ID (YYYYMMDDHHmmss) |
| `signal` | `"BUY"` \| `"SELL"` \| `"HOLD"` \| `"CLOSE"` | Trade action |
| `sl` | float | Stop loss price |
| `tp` | float | Take profit price |
| `lot_size` | float | Position size |
| `confidence` | float | Brain confidence (0-100) |
| `magic` | int | Magic number (234000) |
| `comment` | string | Order comment |
| `breakeven_trigger_pips` | float | Pips profit to move SL to entry |
| `trailing_trigger_pips` | float | Pips profit to activate trailing |
| `trailing_distance_pips` | float | Trailing distance behind price |
| `max_drawdown_pips` | float | Emergency close threshold |

### `ea_status.json` Schema (EA → Python)

| Field | Type | Description |
|-------|------|-------------|
| `version` | int | Schema version (1) |
| `timestamp` | string | Status update time |
| `last_signal_id` | string | Last processed signal ID |
| `last_signal_result` | string | `"OK"` or error message |
| `account.balance` | float | Account balance |
| `account.equity` | float | Account equity |
| `account.margin` | float | Used margin |
| `account.free_margin` | float | Free margin |
| `positions[]` | array | Open positions (see below) |
| `closed_today[]` | array | Trades closed today |
| `spread_pips` | float | Current spread in pips |
| `last_error` | string \| null | Last error message |

### `ea_status.json` Position Fields

| Field | Type | Description |
|-------|------|-------------|
| `positions[].ticket` | int | Position ticket |
| `positions[].direction` | `"BUY"` \| `"SELL"` | Direction |
| `positions[].volume` | float | Lot size |
| `positions[].open_price` | float | Entry price |
| `positions[].current_price` | float | Current price |
| `positions[].sl` | float | Current stop loss |
| `positions[].tp` | float | Take profit |
| `positions[].profit` | float | P&L in account currency |
| `positions[].profit_pips` | float | P&L in pips |
| `positions[].open_time` | string | Open time |
| `positions[].phase` | `"OPEN"` \| `"BREAKEVEN"` \| `"TRAILING"` | Position phase |
| `positions[].breakeven_hit` | bool | Breakeven triggered |
| `positions[].trailing_active` | bool | Trailing active |
| `positions[].max_profit_pips` | float | Max profit reached |

### Dashboard Integration (IMPLEMENTED)

The following elements have been added to the dashboard:

| Element ID | Data Source | Description |
|------------|-------------|-------------|
| `ea-bridge-status` | `state.ea_bridge.enabled/online` | Shows "OFF", "ONLINE", or "FALLBACK" |
| `ea-spread` | `state.ea_bridge.spread_pips` | Real-time spread from EA (e.g., "3.2p") |

**Note:** `position-phase` (OPEN/BREAKEVEN/TRAILING) is deferred until EA Bridge is enabled for live trading.

### `ea_bridge` Object in `bot_state.json`

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `ea_bridge.enabled` | bool | `state_writer.py` | `render()` → `#ea-bridge-status` |
| `ea_bridge.online` | bool | `state_writer.py` | `render()` → `#ea-bridge-status` |
| `ea_bridge.spread_pips` | float \| null | `state_writer.py` | `render()` → `#ea-spread` |

### Fallback Behavior

When `USE_EA_BRIDGE = True` but EA is offline (status file > 60s old):
1. Python logs: `"EA Bridge: OFFLINE — falling back to direct MT5 API"`
2. Trade execution uses `executor.py` (direct MT5 API)
3. Position monitoring uses `monitor.py` (30s intervals)
4. Dashboard should show: `"EA: OFFLINE (fallback)"`

### Testing Checklist (Before Enabling)

- [ ] Python writes `brain_signal.json` correctly
- [ ] EA reads signal and opens position
- [ ] EA writes `ea_status.json` with position data
- [ ] Python reads status and updates dashboard
- [ ] Fallback works when EA is offline
- [ ] Breakeven triggers at correct pip level
- [ ] Trailing activates and follows price
- [ ] No conflict with existing positions (same magic number)
- [ ] Dashboard shows EA status and position phase
