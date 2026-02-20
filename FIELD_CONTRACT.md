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
`status-dot`, `status-label`, `mode`, `last-update`, `market`

### Signal Core
`goldcon`, `goldcon-decision`, `goldcon-score`, `goldcon-conf`, `goldcon-scenario`, `goldcon-blocked`, `gauge-needle`, `gauge-arc`

### Account & Stats
`balance`, `equity`, `pnl`, `price`, `price-label`

### Pillars
`p-tech-bar`, `p-tech-val`, `p-ml-bar`, `p-ml-val`, `p-mom-bar`, `p-mom-val`, `p-news-bar`, `p-news-val`, `p-cal-bar`, `p-cal-val`

### Trades & Positions
`positions`, `positions-empty`, `positions-count`, `last-trade-evidence`, `lte-dir`, `lte-pnl`, `lte-reason`, `lte-time`, `trades-count`, `trades-w`, `trades-l`, `trades-be`, `trades-wr`

### OSINT Feed
`intel-feed-section`, `intel-method`, `intel-cache-age`, `intel-headlines`, `intel-macro`, `intel-calendar`, `intel-sr`, `intel-sr-zones`, `intel-gpt`, `intel-tags`, `intel-bottom`, `intel-cached-badge`, `intel-mtf`, `intel-volume`

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

*Last updated: 2026-02-20*
