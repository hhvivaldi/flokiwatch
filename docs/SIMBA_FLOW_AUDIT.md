# Simba Flow Audit (Pre-Restart)

This document is a **full audit** of the Simba (watchdog) integration: exact call flow, data paths, fallbacks, prompts, and Trade Room rendering.

All references below are grounded in the current repo code.

---

## 1) FLOW DIAGRAM (Startup → Simba decision → Floki gating → Dashboard)

### 1.1 High-level trigger

- **Entry point (proactive cadence):** `TradingBot._call_agent_proactive_h1_snapshot(...)`
  - **File:** `main.py`
  - **Purpose:** runs once per H1 candle close (diagnostic/proactive snapshot).

### 1.2 Decision: call Simba vs call Floki

Inside `TradingBot._call_agent_proactive_h1_snapshot(...)` (main.py):

1. Acquire non-blocking lock:
   - `self._proactive_lock.acquire(blocking=False)`
   - If not acquired: logs and returns.

2. Determine whether Simba can be used (`use_simba`):

   1) **Check open positions** (only if `self.executes_trades`):
   - Calls: `executor.get_open_positions()`
   - If any positions exist: `use_simba = False` (Simba is bypassed)

   2) **If no open positions**:
   - Create an `AgentTools` object (used only to locate the wake-conditions path)
   - Resolve wake-conditions file path:
     - `tools_obj._wake_conditions_path()`
   - If file exists: load JSON
     - `wake_conditions = json.load(f)`

   3) Validate wake conditions + sleep window:
   - Require: `isinstance(wake_conditions, dict)` and `wake_conditions.get("conditions")`
   - Read:
     - `max_sleep_minutes`
     - `sleep_started_at`
   - Compute elapsed minutes and mark sleep window **expired** if `elapsed_min >= max_sleep_minutes`

   4) Simba is enabled iff:
   - no open positions
   - wake_conditions file exists and has `conditions`
   - `max_sleep_minutes > 0`
   - sleep window **not expired**

### 1.3 Exact function that builds `scanner_data`

- **Function:** `TradingBot._call_agent_proactive_h1_snapshot(...)`
- **File:** `main.py`
- **Code block:** inside `if use_simba:`

`scanner_data` is built from `dp = agent_data if isinstance(agent_data, dict) else {}`.

### 1.4 Exact fields in `scanner_data` (ALL)

In `main.py`, within `_call_agent_proactive_h1_snapshot`, Simba receives:

- `scanner_data["current_price"] = dp.get("current_price")`
- `scanner_data["indicators"] = dp.get("indicators")`
- `scanner_data["patterns"] = dp.get("patterns")`
- `scanner_data["macro"] = dp.get("macro")`
- `scanner_data["volume"] = dp.get("volume") or dp.get("tick_volume") or dp.get("last_h1_volume")`
- `scanner_data["candlestick_patterns"] = dp.get("candlestick_patterns")`
- `scanner_data["last_h1_tick_volume"]` extracted from cached candles:
  - Reads `candles = dp.get("candles", {})`
  - Reads `h1_candles = candles.get("H1")`
  - Uses last candle `last_h1 = h1_candles[-1]`
  - If `last_h1` is dict: `int(last_h1.get("volume", last_h1.get("tick_volume", 0)) or 0)`
- `scanner_data["timestamp"] = h1_close_time_iso`

If building fails, fallback is:
- `scanner_data = {"timestamp": h1_close_time_iso}`

### 1.5 How `wake_conditions.json` gets read

In `main.py`, within `_call_agent_proactive_h1_snapshot`:

- Resolve path: `tools_obj._wake_conditions_path()`
- Read JSON:
  - `with open(wake_path, "r", encoding="utf-8") as f: wake_conditions = json.load(f)`

This happens:
- once during the **use_simba** decision (existence/expiry check)
- again inside `if use_simba:` before calling Simba

### 1.6 How `SimbaWatcher.check_conditions()` is called

In `main.py`, within `_call_agent_proactive_h1_snapshot`:

- `from simba_watcher import SimbaWatcher`
- `simba = SimbaWatcher(timeout_seconds=10)`
- `simba_result = simba.check_conditions(scanner_data, wake_conditions)`

### 1.7 What happens on SLEEP vs WAKE

After `simba_result` is obtained:

1) Bot state is updated for dashboard:
- `self.last_analysis["simba"] = {...}` (see section 5)

2) If Simba does **not** say WAKE:
- Condition:
  - `if str(simba_result.get("decision") or "").upper() != "WAKE":`
- Behavior:
  - log: `"PROACTIVE_H1 | Simba says SLEEP — skipping Floki"`
  - **return** (Floki proactive call is skipped)

3) If Simba says **WAKE**:
- log: `"PROACTIVE_H1 | Simba says WAKE — calling Floki | <summary>"`
- continues to:
  - `self._call_agent_proactive_snapshot(trigger_type="PROACTIVE_H1", ...)`

### 1.8 What gets written to dashboard

- `self.last_analysis["simba"]` is updated in `main.py` (details in section 5)
- The dashboard reads bot state via `/api/state` (served by dashboard server), and Trade Room JS uses `state.last_analysis.simba`.

---

## 2) DATA AUDIT — Wake condition TYPE → exact `scanner_data` field path

This section maps each typed wake condition to **where Simba can find the value** in `scanner_data`.

> Note: Simba is instructed to verify strictly based on provided scanner data, but `simba_watcher.py` does **not** implement local evaluation. The evaluation is performed by the LLM. Therefore this mapping is about **data availability** and **canonical field paths** to include in conditions.

### 2.1 `price_above` / `price_below`

- **Recommended scanner path:**
  - `scanner_data.current_price.bid`
  - `scanner_data.current_price.ask`

- **Where it comes from:**
  - `scanner_data["current_price"] = dp.get("current_price")`

- **Audit status:**
  - **Present** if `agent_data["current_price"]` exists.
  - If missing/None: Simba cannot verify price levels and may respond inconsistently; main.py fallbacks force WAKE on failure.

### 2.2 `h1_volume_above`

- **Primary absolute field:**
  - `scanner_data.last_h1_tick_volume` (integer)

- **Where it comes from:**
  - extracted from `agent_data["candles"]["H1"][-1]["volume"]` (fallback to `tick_volume`)

- **Secondary (less ideal) fields that may exist:**
  - `scanner_data.volume` (unknown structure; may be None)
  - `scanner_data.indicators.volume.tick_volume_ratio` (ratio, not absolute)

- **Audit status:**
  - `last_h1_tick_volume` is now explicitly included.
  - **Potential gap:** if `dp["candles"]["H1"]` list is empty or missing, `last_h1_tick_volume` will be absent.

### 2.3 `indicator_above` / `indicator_below`

- **Canonical scanner path:**
  - `scanner_data.indicators.<indicator_key>...`

Examples based on the agent data package builder:
- RSI value:
  - `scanner_data.indicators.rsi.value`
- MACD histogram:
  - `scanner_data.indicators.macd.histogram`
- ATR value:
  - `scanner_data.indicators.atr.value`
- ADX value:
  - `scanner_data.indicators.adx.value`
- EMA values:
  - `scanner_data.indicators.emas.ema9 / ema21 / ema50 / ema200 (if present)`
- Volume ratio (NOT absolute volume):
  - `scanner_data.indicators.volume.tick_volume_ratio`

- **Where it comes from:**
  - `scanner_data["indicators"] = dp.get("indicators")`

- **Audit status:**
  - Present if `agent_data["indicators"]` exists.
  - If indicators missing/None: Simba cannot verify indicator conditions.

### 2.4 `scanner_pattern`

There are **two** pattern-like containers currently passed:

1) `scanner_data.candlestick_patterns`
- **Where it comes from:** `dp.get("candlestick_patterns")`
- **Likely field paths (from agent_data_builder formatting):**
  - Primary name:
    - `scanner_data.candlestick_patterns.primary_pattern.name`
  - List of detected patterns:
    - `scanner_data.candlestick_patterns.patterns[i].name`

2) `scanner_data.patterns`
- **Where it comes from:** `dp.get("patterns")`
- **Audit status:**
  - This depends on runtime structure of `agent_data["patterns"]` (not audited here).

**Recommendation for condition definition:**
- Treat `scanner_pattern` as matching against:
  - `scanner_data.candlestick_patterns.primary_pattern.name`, OR
  - any `scanner_data.candlestick_patterns.patterns[].name`

**Audit status:**
- `candlestick_patterns` is passed explicitly, so candlestick names can be verified.
- If `candlestick_patterns` is None/missing, `scanner_pattern` checks may fail.

### 2.5 Missing/None flag summary

- `scanner_data.current_price`: may be None if agent_data missing it
- `scanner_data.indicators`: may be None if agent_data missing it
- `scanner_data.candlestick_patterns`: may be None if agent_data missing it
- `scanner_data.last_h1_tick_volume`: present only if `agent_data.candles.H1` is a non-empty list

---

## 3) FALLBACK AUDIT (Safety paths)

### 3.1 Wake file missing (`wake_conditions.json` not present)

- In `main.py`, `use_simba` stays False unless:
  - wake_path exists AND `os.path.exists(wake_path)` AND JSON loads AND `wake_conditions.get("conditions")` AND sleep window not expired.

**Result:**
- No wake file (or empty conditions) → **Simba is bypassed** → Floki is called normally.

### 3.2 `scanner_data` build fails

- `try/except` around building scanner fields.
- On exception: `scanner_data = {"timestamp": h1_close_time_iso}`

**Result:**
- Simba still gets called (if `use_simba` True) but with minimal data; that increases chance Simba will say WAKE or return an unhelpful decision.

### 3.3 Simba API fails (OpenAI request error)

In `main.py`:
- Any exception around calling SimbaWatcher produces:
  - `simba_result = {"decision": "WAKE", ... "summary": "fallback — simba_failed"}`

In `simba_watcher.py`:
- Missing API key → `_wake_fallback(..., "OPENAI_API_KEY not set")`
- OpenAI client import/init fails → `_wake_fallback(..., "openai_client_unavailable: ...")`
- Request fails/timeout → `_wake_fallback(..., "openai_request_failed: ...")`

**Result:**
- Any failure → **WAKE** (safe default) → Floki proceeds.

### 3.4 Simba returns invalid JSON

In `simba_watcher.py`:
- If `json.loads(content)` fails → `_wake_fallback(..., "invalid_json: ...")`

**Result:**
- Invalid JSON → **WAKE**.

### 3.5 Simba returns invalid schema

In `simba_watcher.py`:
- `_normalize_result(parsed)` enforces:
  - decision ∈ {SLEEP, WAKE}
  - triggered: list of strings
  - checked_count, met_count: integers
  - summary non-empty

If invalid: `_wake_fallback(..., "invalid_schema")`

**Result:**
- Invalid schema → **WAKE**.

### 3.6 `max_sleep_minutes` expires

In `main.py`:
- `expired = elapsed_min >= max_sleep_minutes`
- If expired OR `max_sleep_minutes <= 0`:
  - `use_simba` remains False

**Result:**
- Sleep expired → Simba bypassed → Floki called normally on proactive cadence.

### 3.7 Position opens while Simba is active

In `main.py`:
- Before enabling Simba, bot checks:
  - `positions = executor.get_open_positions()`
  - if `positions`: `use_simba = False`

**Result:**
- Any open position → Simba bypassed → Floki called normally (and trade management/watch conditions apply).

---

## 4) PROMPT AUDIT (Exact texts)

### 4.1 Simba system prompt (exact)

**File:** `simba_watcher.py`
**Function:** `SimbaWatcher._system_prompt()`

```text
You are Simba, a low-cost checklist verifier for a trading system. You do NOT analyze markets, do NOT give trade advice, and do NOT invent data. You ONLY verify whether ANY provided wake condition is met given the scanner data.

Wake-condition types are typed and structured. Evaluate each one strictly using the provided fields.

Return ONLY strict JSON with keys: decision, triggered, checked_count, met_count, summary.
decision must be either SLEEP or WAKE.
triggered must be an array of condition ids that are met (can be empty).
checked_count and met_count must be integers.
summary must be a short human-readable string explaining why you chose SLEEP/WAKE.
```

### 4.2 Floki WAIT wake-conditions instruction (exact)

**File:** `agent_prompts.py`
**Section:** `<position_management_tools>`

```text
MANDATORY: When you decide WAIT and there are no open positions, you MUST call set_wake_conditions before finishing. Define the specific conditions that would make you reconsider:

1. At least one PRICE condition (price_above or price_below) — the key level that would change your thesis
2. At least one supporting condition (indicator_above, indicator_below, h1_volume_above, or scanner_pattern) — confirmation you'd want to see
3. Set max_sleep_minutes (default 120 — never sleep more than 2 hours)

Example: If you decide WAIT because price is ranging between 5002-5022 with low volume:
- price_above: 5022 (breakout above range)
- price_below: 5002 (breakdown below range)  
- h1_volume_above: 8000 (volume returns)
- max_sleep_minutes: 120

These conditions tell Simba (your watchdog) when to wake you up. Without wake conditions, you will be called every 30 minutes regardless — wasting resources.
```

### 4.3 `set_wake_conditions` tool schema (exact)

**File:** `ai_agent.py`
**Location:** tool schema list (`_tool_schemas`)

```json
{
  "name": "set_wake_conditions",
  "description": "When deciding WAIT with no open position, set wake conditions for Simba to monitor and wake you only when conditions are met.",
  "input_schema": {
    "type": "object",
    "properties": {
      "max_sleep_minutes": {"type": "integer"},
      "conditions": {
        "type": "array",
        "items": {"type": "object"}
      }
    },
    "required": ["max_sleep_minutes", "conditions"],
    "additionalProperties": false
  }
}
```

---

## 5) TRADE ROOM AUDIT (Simba card population)

### 5.1 What `main.py` writes to `last_analysis["simba"]`

**File:** `main.py`
**Function:** `TradingBot._call_agent_proactive_h1_snapshot(...)`

```python
self.last_analysis["simba"] = {
  "decision": simba_result.get("decision"),
  "triggered": simba_result.get("triggered") if isinstance(simba_result.get("triggered"), list) else [],
  "checked_count": simba_result.get("checked_count"),
  "met_count": simba_result.get("met_count"),
  "summary": simba_result.get("summary"),
  "timestamp": datetime.utcnow().isoformat(),
}
```

So the Trade Room has access to:
- `decision`
- `triggered`
- `checked_count`
- `met_count`
- `summary`
- `timestamp`

### 5.2 What Trade Room JS reads

**File:** `dashboard/static/trade_room.html`
**Function:** `renderSimbaFromState(state)`

It reads:
- `var last = (state && state.last_analysis) ? state.last_analysis : {};`
- `var simba = last.simba || null;`

Expected fields:
- `simba.decision`
- `simba.checked_count`
- `simba.met_count`
- `simba.summary`

### 5.3 What Trade Room shows on SLEEP vs WAKE

In `renderSimbaFromState(state)`:

- If no simba data (`!simba || !simba.decision`):
  - pill: `OFF`
  - mode: `OFF`
  - conditions: `—`

- Else, for `decision === 'WAKE'`:
  - pill text: `ALERT`
  - pill class: `pill-hold`
  - mode tag: `ALERT` (green dot)

- Else (implicitly `SLEEP`):
  - pill text: `MONITOR`
  - pill class: `pill-wait`
  - mode tag: `MONITORING` (gray dot)

The conditions bar is computed from:
- `met/checked` → percent width
- `condText = met + '/' + checked + ' met'`

---

## Appendix A — Current authoritative code locations

- Simba gating decision + scanner_data build:
  - `main.py` → `TradingBot._call_agent_proactive_h1_snapshot`

- Simba OpenAI call + schema normalization:
  - `simba_watcher.py` → `SimbaWatcher.check_conditions`, `_normalize_result`, `_wake_fallback`

- Floki wake-condition instruction:
  - `agent_prompts.py` → `<position_management_tools>`

- Tool schema:
  - `ai_agent.py` → tool schemas list includes `set_wake_conditions`

- Trade Room rendering:
  - `dashboard/static/trade_room.html` → `renderSimbaFromState(state)`

