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
| `floki_lessons.json` | file (not in bot_state) | `data/floki_lessons.json` | FLO-325: Floki's permanent process-memory layer. Separate from `session_memory` (daily reset), `trade_lessons.json` (bucket × outcome auto-populated on trade close), and `reflexions` (ChromaDB per-trade rich analyses). Shape: `{next_id: int, lessons: [{id, timestamp, lesson, context: {regime?, session?, related_ticket?}}, ...]}`. FIFO cap 50 — on add, oldest auto-drops. Duplicate text (case-insensitive) bumps the existing entry to newest and reuses its id. Floki-managed via `save_lesson(text, context?)` and `forget_lesson(lesson_id)` tools. Rendered as a `<lessons_learned>` block at the top of Floki's user message (after boss_notes, before pre_decision_plan). Per-lesson text cap 400 chars. Non-blocking: missing/malformed file is a silent no-op.
| `last_analysis.data_needs` | object \| null | `ai_agent.py` → `main.py` → `state_writer.py` | FLO-302 / FLO-306 / FLO-310 / FLO-315: Floki's structured self-assessment. Shape: `{followed_plan: "yes"\|"yes_with_changes"\|"no"\|"", not_called: string[], unavailable: string[], timeframes_skipped: string[] (D1\|H4\|H1\|M15\|M5\|M1), biggest_obstacle: string, self_critique: string (process reflection — how Floki used existing tools), feature_requests: string[] (capabilities that DON'T exist yet, max 2), tool_errors: string[], assessment: string}`. **`not_called`** = tools Floki had access to but skipped (chronic skips → blind spots). **`unavailable`** = genuinely missing/errored/stale data (chronic → broken infra). **`self_critique`** = one-sentence reflection on THIS cycle's process, does NOT trigger Discord dispatch alone (every-cycle noise). **`feature_requests`** = genuine asks for new capabilities, DOES trigger dispatch + drift tracking. Back-compat: legacy `missing_data` → `not_called`; legacy `suggestions` → `feature_requests`; legacy plain-string payload → `{assessment: <str>, others empty}`. Mirrored to Discord `#data_needs` when any of not_called/unavailable/biggest_obstacle/feature_requests/tool_errors is non-empty; drift detection runs independently on `not_called`, `unavailable`, and `feature_requests` (3+ consecutive cycles → labeled drift warning). Never affects decisions. |

### `last_analysis.simba` Object (Simba Watcher)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `last_analysis.simba.decision` | string (`"SLEEP"` \| `"WAKE"`) | `main.py` | `dashboard/static/trade_room.html` → Simba card |
| `last_analysis.simba.triggered` | array | `main.py` | Trade Room Simba card |
| `last_analysis.simba.checked_count` | int | `main.py` | Trade Room Simba card |
| `last_analysis.simba.met_count` | int | `main.py` | Trade Room Simba card |
| `last_analysis.simba.summary` | string | `main.py` | Trade Room Simba card |
| `last_analysis.simba.timestamp` | string (ISO) | `main.py` | Trade Room Simba card |

### `last_analysis.debate` Object (FLO-190 Rex Bull/Bear)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `last_analysis.debate.status` | string (`"INJECTED"` \| `"SKIPPED"` \| `"DISABLED"`) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.skip_reason` | string \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.timestamp` | string (ISO) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.direction` | string (`"BUY"` \| `"SELL"` \| `"NONE"`) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.case` | string | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.conviction` | int (1-10) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.entry` | float \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.sl` | float \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bull.target` | float \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.direction` | string (`"SELL"`) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.case` | string | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.conviction` | int (1-10) | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.entry` | float \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.sl` | float \| null | `state_writer.py` | Dashboard debate card |
| `last_analysis.debate.rex_bear.target` | float \| null | `state_writer.py` | Dashboard debate card |

### `last_analysis.verdict` Object (FLO-194 Research Manager)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `last_analysis.verdict.status` | string (`"OK"` \| `"FAILED"` \| `"DISABLED"`) | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.winner` | string (`"BULL"` \| `"BEAR"` \| `"NEUTRAL"`) | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.reasoning` | string | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.recommendation` | string (`"ENTER_BUY"` \| `"ENTER_SELL"` \| `"NEUTRAL"`) | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.entry` | float \| null | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.sl` | float \| null | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.target` | float \| null | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.trigger_buy` | string \| null | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.trigger_sell` | string \| null | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.conviction` | int (1-10) | `state_writer.py` | Dashboard verdict card |
| `last_analysis.verdict.timestamp` | string (ISO) | `state_writer.py` | Dashboard verdict card |

### `pending_orders` Array (FLO-263 Pending Orders)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `pending_orders` | array of objects | `executor.py` | Dashboard |
| `pending_orders[].ticket` | int | `executor.py` | Dashboard |
| `pending_orders[].type` | string (`"BUY_LIMIT"` \| `"SELL_LIMIT"` \| `"BUY_STOP"` \| `"SELL_STOP"`) | `executor.py` | Dashboard |
| `pending_orders[].price` | float | `executor.py` | Dashboard |
| `pending_orders[].sl` | float | `executor.py` | Dashboard |
| `pending_orders[].tp` | float | `executor.py` | Dashboard |
| `pending_orders[].volume` | float | `executor.py` | Dashboard |

### `data/agent_watch_conditions.json` (FLO-301 Simba Compound Conditions)

Simba monitors these conditions every 30s for open positions. Written by `agent_tools.set_watch_conditions`; read and evaluated by `agent_monitor._evaluate_watch_conditions_for_position`.

**File shape:** `{ "<ticket>": { ...entry... }, ... }`

| Field | Type | Writer | Reader | Notes |
|-------|------|--------|--------|-------|
| `<ticket>.updated_at` | string (ISO UTC) | `agent_tools.py` | `agent_monitor.py` | Last Floki call to set_watch_conditions. |
| `<ticket>.conditions` | array | `agent_tools.py` | `agent_monitor.py` | List of condition entries (legacy single OR compound). |
| `<ticket>.mfe_pnl` | float \| null | `agent_monitor.py` | `agent_monitor.py` | Max profit seen (dollars). Updated every eval tick when pnl grows. Preserved across re-sets by Floki. Feeds `mfe_drawdown` condition type. |

**Legacy single condition entry (action = wake, implicit):**
| Field | Type | Values |
|-------|------|--------|
| `type` | string | `price_touch`, `pnl_threshold`, `pnl_below`, `pnl_above`, `indicator_threshold`, `bb_position`, `mfe_drawdown` |
| `level` \| `value` \| `pct` | float | Type-specific threshold |
| `description` | string | Floki-authored note |

**Compound entry (FLO-301, `all_of` wrapper with explicit action):**
| Field | Type | Values | Notes |
|-------|------|--------|-------|
| `all_of` | array of leaf conditions | | All must be true for action to fire |
| `action` | string | `"wake"` \| `"close"` \| `"adjust_sl"` | Simba behavior when all_of met |
| `description` | string | Floki's rule description | |
| `sl_value` | float | Required when `action="adjust_sl"` | Target SL price. SL-widening guard applies. |
| `fired_at` | string (ISO UTC) \| null | Set by agent_monitor on execution | Prevents re-fire. Persisted. |
| `fired_result` | dict \| absent | `{success, reason}` | Written alongside `fired_at` on execution |

**Condition types (leaves, used in both legacy and all_of):**
| Type | Fields | Fires when |
|------|--------|------------|
| `price_touch` | `level` (+ optional `tolerance`) | \|price − level\| ≤ tolerance (default 5 pips) |
| `pnl_threshold` | `value` ($) | pnl ≥ value (if positive) or pnl ≤ value (if negative) |
| `pnl_below` | `value` ($) | pnl < value |
| `pnl_above` | `level` ($) | pnl ≥ level |
| `indicator_threshold` | `indicator` (rsi\|macd_histogram\|adx\|vix), `direction` (above\|below), `level` | Current indicator ≷ level |
| `bb_position` | `value` (above_upper\|below_lower\|upper_band\|lower_band\|middle) | Indicator `bb_position` matches (**Phase 2 plumbing pending — see FLO-302**) |
| `mfe_drawdown` | `pct` (0-100) | (mfe_pnl − pnl) / mfe_pnl × 100 ≥ pct. Requires mfe_pnl > 0. |

**Trigger registration:** when a compound `action=close` or `action=adjust_sl` fires, `agent_monitor` calls `executor.close_position` / `executor.modify_position`, then `bot.agent_proactive_out_of_cycle(trigger_type="SIMBA_EXIT_EXECUTED", trigger_data=...)`. `main.py:337` accepts this trigger; `main.py:3618` renders a `<simba_execution>` block in trigger_context so Floki sees the execution report and decides next steps.

### `/api/rex-monitor` Endpoint (FLO-214 + FLO-316 Rex Proactive Monitor)

**FLO-316 (2026-04-21)** removed prescriptive labels from Rex Monitor schema
to reduce Floki's compounding caution. The endpoint now surfaces only
observational findings.

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `monitor.findings_count` | int (0+) | `rex_monitor.py` | Rex card pill `#rex-monitor-pill` |
| `monitor.findings` | list of `{type, observation, data}` | `rex_monitor.py` | get_rex_monitor tool response |
| `monitor.timestamp` | string (ISO) | `rex_monitor.py` | Rex card pill (age calc) |
| `monitor.age_minutes` | float \| null | `server.py` | Rex card pill ("Xm ago") |
| `stale` | boolean | `server.py` | Rex card pill ("stale" suffix) |

**Removed fields (FLO-316):**
- `monitor.alert_level` (QUIET/NORMAL/ELEVATED/CRITICAL) — prescriptive label
- `monitor.alert_context` (decorrelation / directional_risk / transition / mixed) — narrative interpretation
- `monitor.alert_hint` (guidance text) — parroted back as blanket "caution"
- Per-finding `severity` (HIGH/MEDIUM/LOW), `implication` (bullish / bearish / avoid_X), and `source` tag

**Finding shape:** each finding in `findings[]` is `{type, observation, data}`:
- `type` ∈ `DIVERGENCE` | `CORRELATION` | `REGIME` | `SESSION`
- `observation` — human-readable sentence with numeric values
- `data` — dict of numeric fields (specific to type)

**Simba wake threshold:** `findings_count >= 2` (was `alert_level == "CRITICAL"`). 2h debounce preserved.

### FLO-215 Phase 3: Adaptive Main Area IDs

| ID | Purpose | Writer | Notes |
|----|---------|--------|-------|
| `main-has-position` | Container visible when position(s) open | JS toggle | `display:none` when 0 positions |
| `main-no-position` | Container visible when no positions | JS toggle | `display:none` when positions > 0 |
| `position-bar` | Position rows container | `_renderPositionBar()` | innerHTML rebuilt each poll |
| `position-direction` | Direction pill (first position only) | `_renderPositionBar()` | BUY green / SELL red |
| `position-entry` | Entry price (first position only) | `_renderPositionBar()` | — |
| `position-pnl` | Live P&L (first position only) | `_renderPositionBar()` | Green if +, red if - |
| `position-sl` | Stop loss (first position only) | `_renderPositionBar()` | — |
| `position-tp` | Take profit (first position only) | `_renderPositionBar()` | — |
| `position-duration` | Time since open (first position only) | `_renderPositionBar()` | Computed client-side |
| `position-phase` | Phase pill (first position only) | `_renderPositionBar()` | OPEN/BE/TRAILING |
| `position-hold-reasoning` | Floki's last reasoning text | `updatePanels()` | From `proactive_analysis.reasoning` |
| `position-watch-conditions` | Simba watch conditions compact | `_renderPositionBar()` | From `wake_conditions.conditions[]` |

**Note:** Static IDs (`position-direction`, etc.) reference the **first position only**. Additional positions (max 3) are rendered as anonymous rows without unique IDs.

### `last_analysis.intel_feed` Object (OSINT)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `intel_feed.headlines` | array | `main.py` | `renderIntelFeed()` → `#intel-headlines` |
| `intel_feed.macro` | object (yields_10y, gld, real_yields, gld_flows) | `main.py` | `renderIntelFeed()` → `#macro-grid`, `#macro-yahoo` |
| `market_context` | object (metals, forex, indices, energy, crypto, futures, session) | `state_writer.py` (from `market_context_cache.json`) | `renderIntelFeed()` → `#macro-metals`, `#macro-forex`, `#macro-futures`, `#macro-grid` |
| `intel_feed.anomalies` | array | `main.py` | `renderIntelFeed()` → anomaly display |
| `intel_feed.analysis_method` | string | `main.py` | `renderIntelFeed()` → `#intel-method` |
| `intel_feed.news_score` | float | `main.py` | not directly used |
| `intel_feed.cache_age_minutes` | float | `main.py` | `renderIntelFeed()` → `#intel-cache-age` |
| `intel_feed.calendar` | object | `main.py` | `renderIntelFeed()` → `#intel-calendar` |
| `intel_feed.gpt_validator` | object | `main.py` | `renderIntelFeed()` → `#intel-gpt` |
| `intel_feed.confirmations` | array | `main.py` | `renderIntelFeed()` → `#intel-tags` |
| `intel_feed.alerts` | array | `main.py` | `renderIntelFeed()` → `#intel-tags` |
| `intel_feed.sr_zones` | array | `main.py` | `renderIntelFeed()` → `#intel-sr-zones` |
| `intel_feed.sr_zones[*].volume` | int | `main.py` (FLO-312) | aggregate `tick_volume` across candles overlapping the zone |
| `intel_feed.sr_zones[*].volume_bucket` | string (`HIGH`/`MEDIUM`/`LOW`/`—`) | `main.py` (FLO-312) | percentile classification vs other zones in same pool |
| `intel_feed.candlestick_patterns` | object \| null | `main.py` | `renderIntelFeed()` → `#intel-patterns` |

### `market_context` Object (FLO-122)

Data source: `data/market_context_cache.json` (written by `agent_tools.get_market_context()`, read by `state_writer.py`).

**Section container IDs:**

| Container ID | Section | Data Source |
|-------------|---------|-------------|
| `#macro-metals` | Metals table (Silver, Platinum, Palladium) | `market_context.metals` |
| `#macro-forex` | Forex table (6 pairs + dollar strength) | `market_context.forex` |
| `#macro-futures` | Futures table (DXY, VIX, 10Y Bond) | `market_context.futures` |
| `#macro-grid` | 2x2 grid (S&P, BTC, Oil, Yields) | `market_context.indices/crypto/energy` + `macro.yields_10y` |
| `#macro-yahoo` | Yahoo data (GLD Vol, Flows, Real Yields) | `intel_feed.macro` |

**Instrument field IDs** (each is a `<tr>` element):

| Field ID | Symbol | Category |
|----------|--------|----------|
| `#mc-XAGUSD` | Silver | metals |
| `#mc-XPTUSD` | Platinum | metals |
| `#mc-XPDUSD` | Palladium | metals |
| `#mc-EURUSD` | EUR/USD | forex |
| `#mc-USDJPY` | USD/JPY | forex |
| `#mc-USDCHF` | USD/CHF | forex |
| `#mc-AUDUSD` | AUD/USD | forex |
| `#mc-USDCNH` | USD/CNH | forex |
| `#mc-GBPUSD` | GBP/USD | forex |
| `#mc-DXY_M6` | Dollar Index | futures |
| `#mc-VIX_J6` | VIX | futures |
| `#mc-UST10Y_M6` | 10Y Bond | futures |
| `#mc-grid-sp500` | S&P 500 | grid |
| `#mc-grid-btc` | Bitcoin | grid |
| `#mc-grid-oil` | Oil WTI | grid |
| `#mc-grid-yields` | 10Y Yield | grid |
| `#mc-gld` | GLD Volume | yahoo |
| `#mc-gld-flows` | GLD Flows | yahoo |
| `#mc-real-yields` | Real Yields | yahoo |

**Per-instrument fields** (in `market_context.{category}.{SYMBOL}`):

| Field | Type | Description |
|-------|------|-------------|
| `bid` | float | Current bid price |
| `change_pct` | float \| null | Change % from session close |
| `day_high` | float \| null | Session high (bidhigh) |
| `day_low` | float \| null | Session low (bidlow) |
| `position_in_range` | float \| null | 0.0 (day low) to 1.0 (day high) |
| `label` | string \| null | Human-readable label (futures only) |

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

### `last_analysis.fast_decisions` Array (Real-time Triggers)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `fast_decisions[]` | array of objects | `main.py` | `renderFastTriggers()` → `#fast-triggers-chips` |
| `fast_decisions[].action` | `"ACT"` \| `"HOLD"` \| `"DISMISS"` | `main.py` | action label/color |
| `fast_decisions[].reason` | string | `main.py` | trigger reason |
| `fast_decisions[].execution.type` | `"OPEN"` \| `"CLOSE"` \| `"ADJUST"` | `main.py` | execution differentiation |
| `fast_decisions[].execution.direction` | `"BUY"` \| `"SELL"` | `main.py` | color-coding (Green/Red) |
| `fast_decisions[].timestamp` | string (ISO) | `main.py` | age calculation |

### `last_analysis.proactive_analysis` Object (AI Agent - Proactive M30 Snapshot)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `proactive_analysis.trigger` | string | `main.py` | `renderProactiveAnalysis()` |
| `proactive_analysis.h1_close_time` | string (ISO) | `main.py` | `renderProactiveAnalysis()` → `#proactive-h1-close` |
| `proactive_analysis.timestamp` | string (ISO) | `main.py` | not directly used |
| `proactive_analysis.decision` | string | `main.py` | `renderProactiveAnalysis()` → `#proactive-decision` |
| `proactive_analysis.confidence` | int (0-100) | `main.py` | `renderProactiveAnalysis()` → `#proactive-confidence` |
| `proactive_analysis.reasoning` | string | `main.py` | `renderProactiveAnalysis()` → `#proactive-reasoning` |
| `proactive_analysis.key_factors` | array of strings | `main.py` | `renderProactiveAnalysis()` → `#proactive-factors` |
| `proactive_analysis.concerns` | array of strings | `main.py` | `renderProactiveAnalysis()` → `#proactive-concerns` |
| `proactive_analysis.latency_ms` | int | `main.py` | `renderProactiveAnalysis()` → `#proactive-latency` |
| `proactive_analysis.tokens_used` | int | `main.py` | `renderProactiveAnalysis()` → `#proactive-tokens` |
| `proactive_analysis.entry_conditions` | object \| null | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` + lifecycle bar |
| `proactive_analysis.trade_plan` | object \| null | `main.py` | `renderProactiveAnalysis()` → trade plan block |
| `proactive_analysis.adjustment` | object \| null | `main.py` | not directly used |
| `proactive_analysis.close_reason` | string \| null | `main.py` | not directly used |

### `last_analysis.proactive_analysis.entry_conditions` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `entry_conditions.direction` | string | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |
| `entry_conditions.conditions` | array | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |
| `entry_conditions.validity_minutes` | int | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |
| `entry_conditions.preferred_entry` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |
| `entry_conditions.sl` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |
| `entry_conditions.tp` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-entry-conditions` |

### `last_analysis.proactive_analysis.trade_plan` Object

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `trade_plan.entry_strategy` | string | `main.py` | `renderProactiveAnalysis()` → `#proactive-tp-entry` |
| `trade_plan.entry_price` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-tp-entry` |
| `trade_plan.entry_rationale` | string | `main.py` | not directly used |
| `trade_plan.stop_loss` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-tp-sl` |
| `trade_plan.stop_loss_rationale` | string | `main.py` | not directly used |
| `trade_plan.take_profit` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-tp-tp` |
| `trade_plan.take_profit_rationale` | string | `main.py` | not directly used |
| `trade_plan.risk_reward_ratio` | float | `main.py` | `renderProactiveAnalysis()` → `#proactive-tp-rr` |
| `trade_plan.timing` | string | `main.py` | not directly used |
| `trade_plan.moment_assessment` | string | `main.py` | not directly used |

### `agent_proactive_analyses` Table (SQLite)

| Column | Type | Writer | Note |
|--------|------|--------|------|
| `id` | INTEGER | `db_writer.py` | Primary key |
| `timestamp` | TEXT | `db_writer.py` | ISO timestamp |
| `h1_close_time` | TEXT | `db_writer.py` | ISO timestamp of M30 close |
| `agent_decision` | TEXT | `db_writer.py` | Decision (OPEN_BUY, OPEN_SELL, WAIT, REJECT, HOLD_TRADE, ADJUST_TRADE, CLOSE_TRADE) |
| `agent_confidence` | INTEGER | `db_writer.py` | 0-100 |
| `agent_reasoning` | TEXT | `db_writer.py` | Reasoning text |
| `agent_key_factors` | TEXT | `db_writer.py` | JSON array of key factors |
| `agent_concerns` | TEXT | `db_writer.py` | JSON array of concerns |
| `raw_response` | TEXT | `db_writer.py` | Full response string |
| `tp_entry_strategy` | TEXT | `db_writer.py` | Entry strategy |
| `tp_entry_price` | REAL | `db_writer.py` | Entry price |
| `tp_entry_rationale` | TEXT | `db_writer.py` | Entry rationale |
| `tp_stop_loss` | REAL | `db_writer.py` | Stop loss price |
| `tp_stop_loss_rationale` | TEXT | `db_writer.py` | Stop loss rationale |
| `tp_take_profit` | REAL | `db_writer.py` | Take profit price |
| `tp_take_profit_rationale` | TEXT | `db_writer.py` | Take profit rationale |
| `tp_risk_reward_ratio` | REAL | `db_writer.py` | Risk/reward ratio |
| `tp_timing` | TEXT | `db_writer.py` | Timing constraint |
| `tp_moment_assessment` | TEXT | `db_writer.py` | Moment assessment |
| `prompt_version` | TEXT | `db_writer.py` | Prompt version |
| `prompt_hash` | TEXT | `db_writer.py` | Prompt hash |
| `model` | TEXT | `db_writer.py` | Model used |
| `input_tokens` | INTEGER | `db_writer.py` | Input tokens |
| `output_tokens` | INTEGER | `db_writer.py` | Output tokens |
| `latency_ms` | INTEGER | `db_writer.py` | Latency in milliseconds |
| `adjustment_new_sl` | REAL | `db_writer.py` | New SL for ADJUST_TRADE |
| `adjustment_new_tp` | REAL | `db_writer.py` | New TP for ADJUST_TRADE |
| `adjustment_reason` | TEXT | `db_writer.py` | Reason for ADJUST_TRADE |
| `close_reason` | TEXT | `db_writer.py` | Reason for CLOSE_TRADE |

### `agent_memory` Object (AI Agent Memory - v1.3)

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `agent_memory` | object \| null | `state_writer.py` via `agent_memory.get_memory_for_dashboard()` | `renderAgentMemory()` |
| `agent_memory.timestamp` | string (ISO) | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-timestamp` |
| `agent_memory.brain_signal` | string | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-brain-signal` |
| `agent_memory.brain_score` | float | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-brain-score` |
| `agent_memory.market_view.direction` | `"BUY"` \| `"SELL"` \| `"HOLD"` | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-view-direction` |
| `agent_memory.market_view.description` | string | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-view-description` |
| `agent_memory.conditions` | array of objects | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-conditions` |
| `agent_memory.conditions[].description` | string | `agent_memory.py` | condition text |
| `agent_memory.conditions[].met` | bool | `agent_memory.py` | condition status (✅/❌) |
| `agent_memory.conditions[].current_value` | float \| null | `agent_memory.py` | current indicator value |
| `agent_memory.invalidation.timeframe` | string | `agent_memory.py` | `renderAgentMemory()` → `#agent-memory-expiry` |
| `agent_memory.invalidation.candles_total` | int | `agent_memory.py` | total candles for invalidation |
| `agent_memory.invalidation.candles_remaining` | int | `agent_memory.py` | candles remaining |
| `agent_memory.invalidation.expires_at` | string (ISO) | `agent_memory.py` | expiration timestamp |
| `agent_memory.status` | `"active"` \| `"conditions_met"` \| `"invalidated"` | `agent_memory.py` | memory status |
| `agent_memory.all_conditions_met` | bool | `agent_memory.py` | whether all conditions are met |

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
| `positions[].phase` | `"OPEN"` \| `"SL_ADJUSTED"` \| `"TRAILING"` | `state_writer.py` via `monitor.get_position_phase()` | `renderPositions()` |
| `positions[].be_trigger_pips` | float \| null | `state_writer.py` via `monitor.get_be_info()` | `renderPositions()` → Protection column |
| `positions[].be_remaining_pips` | float \| null | `state_writer.py` via `monitor.get_be_info()` | `renderPositions()` → Protection column |

### `trade_history` Array

| Field | Type | Writer | Reader (app.js) |
|-------|------|--------|-----------------|
| `trade_history[].ticket` | int | `state_writer.py` | `renderPositions()`, `renderTrades()` |
| `trade_history[].direction` | string | `state_writer.py` | `renderPositions()` |
| `trade_history[].profit` | float | `state_writer.py` | `renderPositions()`, `renderTrades()` |
| `trade_history[].close_time` | string (ISO) | `state_writer.py` | `renderTrades()`, `renderPositions()` |
| `trade_history[].close_type` | string | `state_writer.py` | `renderPositions()` |
| `trade_history[].reason` | string | `state_writer.py` | `renderPositions()` |
| `trade_history[].breakeven_activated` | bool | `state_writer.py` | `renderTrades()` |

### `/api/live-readiness` Endpoint (FLO-272 Live Readiness Panel)

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `status` | string | computed | `NOT_READY` / `MINIMUM_READY` / `READY_FOR_LIVE` |
| `criteria_met` | int | computed | 0-6 metrics at or above minimum |
| `criteria_total` | int | const 6 | Total metrics evaluated |
| `metrics.profit_factor` | object | `trades` table | `{value, min: 1.2, ideal: 1.5, level, trend}` |
| `metrics.win_rate` | object | `trades` table | `{value (%), min, ideal, level, trend}`. FLO-318: `min` and `ideal` are **dynamic** — computed from `avg_win_loss` ratio. Formula: `breakeven_wr = 1 / (1 + ratio) × 100`; `min = breakeven_wr + 5%`; `ideal = breakeven_wr + 12%`. Fallback to fixed 50/55 when ratio is degenerate (≤0 or ≥10). Rationale: a fixed 50% floor misclassifies a profitable R/R=1.8 system as below-minimum (actual math breakeven ≈36%). The computed thresholds are rendered on the card so the value is auditable — frontend already reads `min` and `ideal` from the response. |
| `metrics.avg_win_loss` | object | `trades` table | `{value (x), min: 1.5, ideal: 2.0, level, trend}` |
| `metrics.trades` | object | `trades` table | `{value (count), min: 50, ideal: 100, level, trend}` |
| `metrics.max_drawdown` | object | computed from trades | `{value (%), min: 10, ideal: 5, level, trend, higher_is_better: false}` |
| `metrics.days_without_p0` | object | `data/last_p0_incident.json` | `{value (days), min: 14, ideal: 30, level, trend}` |
| `metrics.*.level` | string | computed | `below` / `min` / `ideal` |
| `metrics.*.trend` | string | 7d vs prev 7d | `improving` / `declining` / `stable` |
| `last_updated` | string (ISO) | server time | UTC timestamp |

**Reader:** `history.html` → `#live-readiness-panel`, `#rp-metrics`

Status thresholds: `criteria_met == 6` → READY_FOR_LIVE, `criteria_met >= 3` → MINIMUM_READY, else NOT_READY.

### `/api/journal` Endpoint (FLO-269 Trade Journal)

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `trades[]` | array | `trades` + `trade_adjustments` + report JSONs | Last 30 closed trades with full detail |
| `trades[].ticket` | int | `trades.ticket` | MT5 ticket |
| `trades[].direction` | string | `trades.direction` | BUY/SELL |
| `trades[].session_open` | string | Derived from `open_time` | Asian/London/NY/OffHours |
| `trades[].session_close` | string | Derived from `close_time` | Asian/London/NY/OffHours |
| `trades[].pnl` | float | `trades.profit` | Realized P&L |
| `trades[].mfe` | float\|null | `trades.mfe_points` | Max Favorable Excursion |
| `trades[].mae` | float\|null | `trades.mae_points` | Max Adverse Excursion |
| `trades[].capture_pct` | float\|null | Computed: pnl/mfe | Capture rate percentage |
| `trades[].adj_count` | int | `trade_adjustments` count | Number of SL/TP adjustments |
| `trades[].adjustments` | array | `trade_adjustments` | SL/TP adjustment history |
| `trades[].verdict` | string\|null | Counterfactual analysis | "SAVED $X" or "COST $X" |
| `trades[].verdict_type` | string\|null | Derived | "helped" / "hurt" / "neutral" |
| `trades[].counterfactual` | object\|null | `post_trade_reports/{ticket}.json` | EOD counterfactual data |
| `stats.avg_capture` | float\|null | Aggregate | Average capture rate % |
| `stats.adj_helped_pct` | int\|null | Aggregate | % of trades where outcome beat original plan |
| `stats.adj_hurt_pct` | int\|null | Aggregate | % of trades where original plan was better |

**Reader:** `trade_room.html` → `#journal-table`, `#journal-stats`

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

### AI Agent Card
`agent-card`, `agent-decision`, `agent-confidence`, `agent-reasoning`, `agent-factors`, `agent-concerns`, `agent-agreement`, `agent-executed`, `agent-latency`

### Trade Room Agent Grid (FLO-33)
`sage-card`, `sage-status`, `sage-summary`, `sage-last-run`, `sage-report-period`, `sage-report-trades`, `sage-report-win-rate`, `sage-report-profit-factor`, `sage-report-recommendations`
`echo-card`, `echo-status-pill`, `echo-alert-pill`, `echo-last-scan`, `echo-scan-count`, `echo-critical-count`, `echo-important-count`
`luna-card`, `atlas-card`
`luna-headlines` (FLO-238): Headlines consumed panel. `luna-headlines-list` renders echo alerts Luna read.
`luna-deep-research` (FLO-236): Deep Search panel. `luna-deep-consensus`, `luna-deep-insight`, `luna-deep-sources`, `luna-deep-time`.

### Luna Brief API (`/api/luna-brief`)

**Bug G schema (observational-only, Escola 1 alignment):**

Response `brief` contains:
- `timestamp` (ISO 8601 UTC, Z suffix)
- `source` (`mimo` | `gemini_fallback` | `local_fallback`)
- `data_snapshot`: per-instrument numeric values + changes:
  - `dxy`, `vix`, `yields_10y`: `{value, change_pct_24h, trend_3d}` where `trend_3d` ∈ `rising`/`falling`/`flat`
  - `oil_wti`, `sp500`, `usdcny`: `{value, change_pct_24h}`
  - `gold`: `{value, change_pct_24h, dist_from_3d_high_pct, 3d_high}`
  - `gld_volume`: `{avg_5d_vs_baseline, rising_price, status}` where status ∈ `accumulation`/`distribution`/`quiet_bid`/`quiet`
  - `real_yields`, `fed_funds`, `breakeven`, `cpi`: `{value, change}`
- `correlations`: top-level `{status: "ok"|"insufficient_data", days, gold_dxy, gold_yields, gold_sp500}`. Per-pair values are `{value, normal_range}` (raw current + typical only — no `NORMAL`/`WEAK`/`BROKEN` status labels; Bug G follow-up 2026-04-21 removed prescriptive classification).
- `patterns_detected` (list of Python-validated pattern names): `forced_liquidation`, `safe_haven_flow`, `news_price_divergence`, `dollar_gold_correlation_break`, `blow_off_reversal`
- `key_factors` (3–5 short observational statements, each anchored to a number)
- `next_events` (list of `{event, time, impact}`)
- `brief.headlines_consumed` (FLO-238): Array of `{title, severity}` — Echo alerts Luna consumed on her last cycle. Severity: CRITICAL/IMPORTANT/ROUTINE.

**Removed fields (Bug G, 2026-04-21)** — no longer present in the JSON:
- `environment` (SAFE/CAUTION/DANGER)
- `risk_level` (1-10)
- `directional_bias` (BULLISH/BEARISH/NEUTRAL)
- `bias_confidence` (1-10)
- `market_regime` (Luna's LLM interpretation; FLO-139 Market Regime Detector is unchanged and continues to produce RANGING/TRENDING_*/etc. via `regime_detector.py`)
- `summary` (interpretive prose)

Rationale: Luna reports observational data; Floki forms his own view. Prescriptive labels violate Escola 1.

`deep_research` (FLO-236): Deep Search cache if fresh (<3h). Contains `analyst_consensus`, `key_insight`, `price_targets`, `risks_this_week`, `sources`.

### AI Agent Memory (v1.3)
`agent-memory-section`, `agent-memory-timestamp`, `agent-memory-brain-signal`, `agent-memory-view-direction`, `agent-memory-view-description`, `agent-memory-conditions`, `agent-memory-expiry`

### Proactive Analysis (M30 Snapshot)
`proactive-section`, `proactive-h1-close`, `proactive-countdown`, `proactive-decision`, `proactive-confidence`, `proactive-reasoning`, `proactive-factors`, `proactive-concerns`, `proactive-latency`, `proactive-tokens`, `proactive-reasoning-toggle`, `proactive-entry-conditions`

Required for proactive sentiment + thesis lifecycle bars:
`sentiment-bar`, `sentiment-indicator`, `sentiment-label`, `lifecycle-bar`, `lifecycle-indicator`, `lifecycle-label`

Proactive HOLD display (live positions-based):
`proactive-hold-block`, `proactive-hold-summary`, `proactive-hold-pnl`

Required for proactive OPEN trade plan display:
`proactive-tp-block`, `proactive-tp-entry`, `proactive-tp-sl`, `proactive-tp-tp`, `proactive-tp-rr`

Required for proactive CLOSE/ADJUST details display:
`proactive-close-reason-block`, `proactive-close-reason`, `proactive-adjust-block`, `proactive-adjust-sl`, `proactive-adjust-tp`, `proactive-adjust-reason`

### Banners & Misc
`offline-banner`, `offline-last-update`, `pillars-cached-badge`, `vol-banner`, `news-marquee`, `recent-decisions`, `brain-toggle`, `brain-reference-panel`, `fast-triggers-chips`

---

## Change Process

1. **Before** any dashboard change touching data fields or element IDs:
   - Update this contract with proposed changes
   - Verify every ID in the "HTML Element IDs" section still exists
   - Run the dashboard with live bot data and confirm all components render

2. **Never** rename or remove an HTML element ID without updating `app.js` in the same commit.

3. **Never** change a field name in `state_writer.py` without updating `app.js` in the same commit.

---

## Trade History (History Dashboard)

### Outcome Classification

Trade outcomes are classified in `dashboard/server.py` from `trades.profit`:

- **Win**: `profit > 0`
- **Loss**: `profit < 0`
- **Breakeven**: `profit == 0` (exact zero only)

Win Rate uses `wins / (wins + losses)` and excludes breakevens.

### Live vs Backtest Population

The Live column in the History comparison table is filtered to **current system trades** only:

- `open_time >= 2026-02-16`
- Label: “Trades #8-22 (current system only)”

---

/* ================================================================
   FIELD CONTRACT UPDATED: 2026-03-12
   ================================================================ */

---

## EA Bridge (Optional Execution Mode)

### Overview

The EA Bridge is an execution mode that separates Python (analysis/agent orchestration) from MT5 execution. When enabled, Python writes signals to a JSON file, and the FlokiBridge EA reads and executes them with tick-by-tick position management.

**Status:** `USE_EA_BRIDGE = True` (active)

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
| `heartbeat_count` | int | Increments every OnTimer() call (diagnostic) |
| `last_heartbeat_time` | string | Timestamp of last OnTimer() execution |
| `consecutive_write_failures` | int | Count of consecutive file write failures |
| `last_write_error` | string \| null | Last write error message (diagnostic) |
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
| `positions[].phase` | `"OPEN"` \| `"SL_ADJUSTED"` \| `"TRAILING"` | Position phase |
| `positions[].breakeven_hit` | bool | SL adjusted to entry |
| `positions[].trailing_active` | bool | Trailing active |
| `positions[].max_profit_pips` | float | Max profit reached |

### Dashboard Integration (IMPLEMENTED)

The following elements have been added to the dashboard:

| Element ID | Data Source | Description |
|------------|-------------|-------------|
| `ea-bridge-status` | `state.ea_bridge.enabled/online` | Shows "OFF", "ONLINE", or "FALLBACK" |
| `ea-spread` | `state.ea_bridge.spread_pips` | Real-time spread from EA (e.g., "3.2p") |

### Trade Room (dashboard/static/trade_room.html)

| Element ID | Data Source | Description |
|------------|-------------|-------------|
| `simba-card` | `state.last_analysis.simba` | Container card for Simba |
| `simba-status-pill` | `state.last_analysis.simba.decision` | Shows OFF/MONITOR/ALERT |
| `simba-conditions-text` | `state.last_analysis.simba.met_count/checked_count` | Displays conditions met vs checked |
| `simba-conditions-bar` | derived from met/checked | Progress bar for met ratio |
| `simba-mode-tag` | derived from decision | OFF/MONITORING/ALERT tag |
| `simba-summary-tag` | `state.last_analysis.simba.summary` | Human summary of why Simba chose sleep/wake |

**Note:** `position-phase` (OPEN/SL_ADJUSTED/TRAILING) is deferred until EA Bridge is enabled for live trading.

### `floki_next_check_at` (FLO-143)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `floki_next_check_at` | string (ISO timestamp, e.g., `"2026-03-30T09:56:18Z"`) | `state_writer.py` (reads `data/agent_next_check.json`) | `#proactive-countdown` + `#cc-countdown` (Trade Room) |

### Command Center Elements (FLO-145 Proposal 1)

| Element ID | Reads from | Description |
|------------|-----------|-------------|
| `#cc-price` | `last_analysis.current_price` / `last_known_price` | XAU/USD price (28px) |
| `#cc-change` | `price_daily_change_pct` | Daily change % with color |
| `#cc-regime` | `market_regime.regime` | Regime name, color-coded |
| `#cc-regime-detail` | `market_regime.confidence` + `duration` | Sub-label |
| `#cc-floki-decision` | `proactive_analysis.decision` | Floki decision, color-coded |
| `#cc-floki-conf` | `proactive_analysis.confidence` | Confidence % |
| `#cc-rex-verdict` | Latest REX insights message | Insights count / FLAGS / CLEAR (FLO-158) |
| `#cc-countdown` | `floki_next_check_at` | Live countdown (1s ticker) |
| `#cc-pnl` | `daily_stats.pnl` | Session P&L |

### `active_thesis` Object (FLO-146)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `active_thesis.direction_bias` | string (`NEUTRAL` \| `BULLISH` \| `BEARISH`) | `state_writer.py` (reads `data/active_thesis.json`) | `#floki-summary` (Trade Room) |
| `active_thesis.key_levels` | array of numbers | `state_writer.py` | `#floki-summary` (Trade Room) |
| `active_thesis.conditions` | array of strings | `state_writer.py` | `#floki-summary` (Trade Room) |
| `active_thesis.decision` | string | `state_writer.py` | `#floki-summary` (Trade Room) |
| `active_thesis.confidence` | number | `state_writer.py` | `#floki-summary` (Trade Room) |
| `active_thesis.timestamp` | string (ISO) | `state_writer.py` | `#floki-summary` (Trade Room) |
| `active_thesis.price_at_decision` | number | `state_writer.py` | `#floki-summary` (Trade Room) |

### `wake_conditions` Object (FLO-146)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `wake_conditions.count` | integer | `state_writer.py` (reads `data/agent_wake_conditions.json`) | `#ctx-watch-conditions` (Trade Room) |
| `wake_conditions.conditions` | array of condition objects | `state_writer.py` | `#ctx-watch-conditions` (Trade Room) |
| `wake_conditions.max_sleep_minutes` | integer | `state_writer.py` | Trade Room (future) |
| `wake_conditions.last_wake_at` | string (ISO) | `state_writer.py` | Trade Room (future) |

### `market_regime` Object (FLO-139)

Data source: `regime_detector.py` via `state_writer.py`. Computed every Brain cycle (~60s) from all available indicators.

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `market_regime.regime` | string (`VOLATILE` \| `QUIET` \| `BREAKOUT_IMMINENT` \| `TRENDING_BULLISH` \| `TRENDING_BEARISH` \| `RANGING` \| `TRANSITIONAL`) | `state_writer.py` via `regime_detector.py` | `#ctx-regime-pill` + `#regime-card-pill` (Trade Room) |
| `market_regime.confidence` | string (`high` \| `moderate` \| `low`) | `state_writer.py` | `#ctx-regime-detail` + `#regime-card-conf` (Trade Room) |
| `market_regime.duration` | string (e.g., "4h 23m") | `state_writer.py` | `#ctx-regime-detail` + `#regime-card-duration` (Trade Room) |
| `market_regime.stability` | string (`stable` \| `moderate` \| `unstable`) | `state_writer.py` | `#ctx-regime-detail` + `#regime-card-stability` (Trade Room) |
| `market_regime.adx` | float | `state_writer.py` | `#regime-card-adx` (Trade Room) |
| `market_regime.atr_ratio` | float | `state_writer.py` | `#regime-card-atr` (Trade Room) |
| `market_regime.transition` | string | `state_writer.py` | `#regime-card-transition` (Trade Room) |
| `market_regime.src` | string (`fast` \| `ADX`) | `state_writer.py` | `#ctx-regime-detail` + `#cc-regime-detail` (Trade Room) |
| `market_regime.h4_volume_bias` | object or null `{bias: "BULLISH"\|"BEARISH"\|"NEUTRAL", age_min, confidence}` | `regime_detector.py` | `get_market_regime` tool (agent-facing) |
| `market_regime.macro_divergence` | object or null `{signal, bias, confidence, age_min, detail}` | `macro_divergence_detector.py` via `main.py._compute_macro_divergence` | `get_market_regime` tool (agent-facing) |
| `market_regime.m15_explosive` | object or null `{direction: "bull"\|"bear", age_min}` | `regime_detector.py` | `get_market_regime` tool (agent-facing) + `agent_monitor.py` (Simba wake) |
| `market_regime.regime_price_divergence` | object or null `{detected: true, price_direction, regime_label, conflicting_bars, detail}` | `regime_detector.py` | `get_market_regime` tool (agent-facing). Fires when last 3 H1 closes disagree with TRENDING regime label — regime classifier can lag reversals 25-60min. |

### `multi_tf_indicators` Object in `bot_state.json` (FLO-221)

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `multi_tf_indicators` | object | `state_writer.py` via `technical_analyzer.py` | `index.html` (`#mtf-grid-dashboard`), `trade_room.html` (`#ctx-mtf-*`) |
| `multi_tf_indicators.{M15,H1,H4,D1}` | object | `compute_indicators_from_candles()` | Dashboard + Trade Room multi-TF grid |
| `multi_tf_indicators.{TF}.rsi` | float (0-100) | RSI(14) calculation | Color-coded: <30 green, 30-70 white, >70 red |
| `multi_tf_indicators.{TF}.macd` | object `{value, signal, histogram}` | MACD(12,26,9) | Arrow: ▲ green (bullish) / ▼ red (bearish) |
| `multi_tf_indicators.{TF}.adx` | object `{value, plus_di, minus_di}` | ADX(14) | Brightness: <20 dim, 20-30 normal, >30 bright |
| `multi_tf_indicators.{TF}.ema50` | float | EMA(50) price | — |
| `multi_tf_indicators.{TF}.ema200` | float | EMA(200) price | — |
| `multi_tf_indicators.{TF}.atr` | float | ATR(14) | — |
| `multi_tf_indicators.{TF}.ema9` | float | EMA(9) price | — |
| `multi_tf_indicators.{TF}.ema21` | float | EMA(21) price | — |
| `multi_tf_indicators.{TF}.price_vs_ema9` | string (`above`/`below`) | Price vs EMA9 | — |
| `multi_tf_indicators.{TF}.price_vs_ema21` | string (`above`/`below`) | Price vs EMA21 | — |
| `multi_tf_indicators.{TF}.price_vs_ema50` | string (`above`/`below`) | Price vs EMA50 | — |
| `multi_tf_indicators.{TF}.price_vs_ema200` | string (`above`/`below`) | Price vs EMA200 | — |
| `multi_tf_indicators.{TF}.ema_alignment` | string (`full_bullish`/`full_bearish`/`mixed`) | EMA9>21>50>200 order | BULL/BEAR/MIX in grid |
| `multi_tf_indicators.{TF}.ema9_ema21_distance` | float | EMA9 - EMA21 (positive = above) | — |
| `multi_tf_indicators.{TF}.ema9_ema21_direction` | string (`widening`/`narrowing`/`flat`) | Distance trend vs 4 bars ago | — |
| `multi_tf_indicators.{TF}.ema9_cross_ema21` | string (`golden_cross`/`death_cross`/`none`) | EMA9 x EMA21 crossover in last 4 bars | Crossover icon in grid |
| `multi_tf_indicators.{TF}.ema50_cross_ema200` | string (`golden_cross`/`death_cross`/`none`) | Classic golden/death cross in last 4 bars | Crossover icon in grid |
| `multi_tf_indicators.{TF}.rsi_direction` | string (`rising`/`falling`/`flat`) | RSI 4-bar trend | ↑↓ arrow next to RSI value |
| `multi_tf_indicators.{TF}.rsi_change_4bars` | float | RSI delta vs 4 candles ago | — |
| `multi_tf_indicators.{TF}.macd_direction` | string (`bullish_strengthening`/`bullish_weakening`/`bearish_strengthening`/`bearish_weakening`) | MACD histogram trend | ↑↓ arrow next to MACD arrow |
| `multi_tf_indicators.{TF}.adx_direction` | string (`rising`/`falling`/`flat`) | ADX 4-bar trend | ↑↓ arrow next to ADX value |
| `multi_tf_indicators.{TF}.adx_change_4bars` | float | ADX delta vs 4 candles ago | — |

### `pivot_points` Object in `bot_state.json` (FLO-223)

3-layer structure: daily (D1), weekly (W1), monthly (MN1).

| Field | Type | Writer | Reader |
|-------|------|--------|--------|
| `pivot_points` | object | `state_writer.py` via `main.py` | `index.html`, `trade_room.html` |
| `pivot_points.{daily,weekly,monthly}` | object | Computed from prev candle H/L/C | Dashboard + Trade Room |
| `pivot_points.{layer}.classic.{R3,R2,R1,PP,S1,S2,S3}` | float | Classic pivot formulas | `#ctx-pivot-points`, `#pivot-grid-dashboard` |
| `pivot_points.{layer}.fibonacci.{R3,R2,R1,PP,S1,S2,S3}` | float | Fibonacci pivot formulas (0.382, 0.618) | — |
| `pivot_points.{layer}.source.date` | string (ISO) | Previous candle timestamp | — |
| `pivot_points.{layer}.source.{high,low,close}` | float | Previous candle OHLC | — |

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

---

## Monitor Notes (Live Tracking)

- `balance_at_open` is tracked in the monitor layer to enable profit drawdown / balance diff calculations even when trade history details are delayed.

## Deal Resolver Notes

- `deal_resolver.py` runs as a subprocess to resolve closed trade details via MT5 reconnect and fill in secondary metadata when the primary balance diff/P&L source is not enough for classification.

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
