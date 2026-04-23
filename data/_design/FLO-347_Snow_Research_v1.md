# FLO-347 — Snow Research & Schema Reference v1

**Status:** Research complete. Read before implementing.
**Author:** CTO (Claude)
**Date:** 2026-04-23
**Purpose:** Provide DEV with complete trading-concepts reference and schema foundation for Snow agent.
**Paradigm:** Event-Driven Trade Management (ETM) — used by institutional OEMS for decades.

---

## 0 — Executive Summary

Snow is an event-driven execution agent. Floki writes **contingency plans** for trades (entry conditions + management scenarios + exit scenarios). Snow monitors markets every 5 seconds, evaluates conditions, and executes when they fire.

**The paradigm shift:** Floki moves from "decide now, execute now" to "decide once, pre-commit plans for scenarios, let Snow execute."

**Why this resolves observed problems:**

1. **Entry quality:** Forcing Floki to write conditions upfront removes impulse firing on market noise
2. **SL churn:** No "re-evaluate every 5 min" pressure — plans are committed
3. **Consistency:** Same analysis time, same quality regardless of mental load

**Data support:**
- 62% of SELLs had entry-direction wrong (v2 MFE re-run)
- 5× pending-vs-market $ edge (H-$ analysis)
- Ticket 1605010600 lost +$14 due to 9 SL adjustments in 70 min (B-1 forensic)

---

## 1 — Historical Context: What Professional Systems Do

### 1.1 OEMS (Order Execution Management Systems)

Institutional trading firms have used event-driven OEMS since the 1990s. Key concepts:

- **Conditional orders** — orders dormant until trigger fires
- **Bracket orders (OCO / OTO / OTOCO)** — multiple linked orders that self-manage
- **Contingent orders** — orders tied to external events (price, volume, time, other order fills)

Key insight from institutional world: *"The primary function of contingent orders is TIME SAVING and EMOTIONAL CONTROL. Contingent Orders help you save time from having to monitor your trades tediously and help you prevent trading mistakes caused by breaking trading rules due to emotions."* (OptionTradingpedia)

**Snow applies this to an LLM-driven system.** Instead of a human trader writing contingent orders, Floki writes them. Same principle, same benefits.

### 1.2 Fidelity's 4 Conditional Order Types (reference)

Institutional standard names we'll reuse where appropriate:

| Type | Meaning |
|------|---------|
| **Contingent** | Order triggers based on market event (price, volume, time) |
| **OTO** (One-Triggers-Other) | Order A fills → Order B activates |
| **OCO** (One-Cancels-Other) | Two orders live, one fills → other cancels |
| **OTOCO** (One-Triggers-OCO) | Order A fills → Orders B+C become OCO pair |

**Example:** A bracket order is an OTOCO:
- Entry (limit buy)
- → triggers → TP (limit sell) + SL (stop sell) as OCO pair
- If TP fills, SL cancels. If SL fires, TP cancels.

**Snow generalizes this pattern.** Instead of 1 entry + 2 exits, Snow supports N entry conditions + M management contingencies + K exit contingencies.

### 1.3 NautilusTrader Architecture (open-source reference)

NautilusTrader is an open-source institutional-grade event-driven trading engine. Key architectural decisions we should emulate:

- **Deterministic event-driven core** with nanosecond resolution (Snow uses second resolution — adequate for XAU/USD)
- **Advanced order types**: post-only, reduce-only, OCO, OTO, contingencies
- **Message bus pattern** for agent coordination
- **Research-to-live parity** — backtest code IS live code

---

## 2 — Trading Concepts Catalog

This section catalogs every trading concept Snow needs to support. Each concept becomes a condition or action type in the schema.

### 2.1 Price-Based Conditions

| Condition | Example | Notes |
|-----------|---------|-------|
| `price_above(level)` | price > 4735.00 | Ask price for SELL, Bid for BUY entry |
| `price_below(level)` | price < 4720.00 | Inverse |
| `price_touches(level, tolerance)` | price within ±0.2 of 4730 | Wick-friendly |
| `price_in_range(low, high)` | 4720 ≤ price ≤ 4730 | Channel condition |
| `price_out_of_range(low, high)` | price < 4720 OR > 4730 | Breakout condition |
| `price_crosses_above(level)` | Was below, now above | Directional |
| `price_crosses_below(level)` | Was above, now below | Directional |

**TradingView reference:** These map to TV's "Crossing Up", "Crossing Down", "Greater Than", "Less Than", "Inside Channel", "Outside Channel", "Entering Channel", "Exiting Channel" operators.

### 2.2 Indicator-Based Conditions (Multi-Timeframe)

All indicators Floki currently has:

#### 2.2.1 Momentum / Oscillators

| Condition | Parameters |
|-----------|------------|
| `rsi_above(timeframe, threshold)` | TF ∈ {M1, M5, M15, H1, H4, D1}, threshold 0-100 |
| `rsi_below(timeframe, threshold)` | Same |
| `rsi_crosses_above(timeframe, threshold)` | Directional |
| `rsi_crosses_below(timeframe, threshold)` | Directional |
| `rsi_divergence(timeframe, direction)` | Bullish or bearish divergence |
| `macd_histogram_above(timeframe, threshold)` | Typical threshold 0 |
| `macd_histogram_below(timeframe, threshold)` | Same |
| `macd_cross_bullish(timeframe)` | Signal line cross |
| `macd_cross_bearish(timeframe)` | Same |
| `stochastic_above(timeframe, threshold)` | %K or %D |
| `stochastic_below(timeframe, threshold)` | Same |

#### 2.2.2 Trend / Moving Averages

| Condition | Parameters |
|-----------|------------|
| `price_above_ema(timeframe, period)` | period ∈ {9, 21, 50, 200} |
| `price_below_ema(timeframe, period)` | Same |
| `ema_aligned(timeframe, direction)` | 9>21>50>200 (bull) or reverse |
| `ema_crosses_up(tf, fast, slow)` | e.g., EMA50 crosses above EMA200 (golden cross) |
| `ema_crosses_down(tf, fast, slow)` | Death cross |
| `price_pullback_to_ema(tf, period, tolerance)` | Price touches EMA during trend |

#### 2.2.3 Volatility / Range

| Condition | Parameters |
|-----------|------------|
| `atr_above(timeframe, multiplier, baseline)` | ATR > X × baseline ATR |
| `atr_below(timeframe, multiplier, baseline)` | Same |
| `bollinger_touch_upper(timeframe)` | Price touches BB upper |
| `bollinger_touch_lower(timeframe)` | Price touches BB lower |
| `bollinger_breakout_up(timeframe)` | Close above BB upper |
| `bollinger_breakout_down(timeframe)` | Close below BB lower |
| `bollinger_squeeze(timeframe, width_threshold)` | BB width < threshold (consolidation) |

#### 2.2.4 Trend Strength

| Condition | Parameters |
|-----------|------------|
| `adx_above(timeframe, threshold)` | ADX > 25 = strong trend (typical) |
| `adx_rising(timeframe, bars_back)` | ADX has risen over N bars |
| `adx_falling(timeframe, bars_back)` | ADX has fallen over N bars |
| `di_plus_above_minus(timeframe)` | +DI > -DI (bullish) |
| `di_minus_above_plus(timeframe)` | -DI > +DI (bearish) |

### 2.3 Volume Conditions

| Condition | Parameters |
|-----------|------------|
| `volume_above(timeframe, multiplier, baseline)` | Current vol > X × avg |
| `volume_below(timeframe, multiplier, baseline)` | Same |
| `volume_spike(timeframe, threshold)` | Vol > threshold × 20-bar avg |

### 2.4 Structural Conditions (S/R / Levels)

| Condition | Parameters |
|-----------|------------|
| `price_at_sr_zone(zone_type, tolerance)` | Within X pips of S/R zone (calculated by Python) |
| `price_rejects_sr_zone(zone_type, confirm_candles)` | Touches + N candles reverse |
| `price_breaks_sr_zone(zone_type, confirm_candles)` | Closes through + N candles hold |
| `price_at_fibonacci(level_name)` | At 0.382, 0.5, 0.618, 0.786 level |
| `price_at_pivot(pivot_type, layer)` | pivot_type ∈ {R1-R3, PP, S1-S3}, layer ∈ {daily, weekly, monthly} |
| `price_at_prev_day_high()` | Tests PDH |
| `price_at_prev_day_low()` | Tests PDL |

### 2.5 Multi-Timeframe Confluence Conditions

These are composite conditions — Snow evaluates the underlying conditions on multiple TFs:

| Condition | Parameters |
|-----------|------------|
| `mtf_trend_aligned(direction, timeframes)` | All TFs agree on direction |
| `mtf_rsi_agreement(operator, threshold, timeframes)` | RSI condition on N of M TFs |
| `regime_matches(regime)` | Current regime matches expected (from regime_detector) |

### 2.6 Candlestick Pattern Conditions

Already computed by Python:

| Condition | Parameters |
|-----------|------------|
| `candlestick_pattern(timeframe, pattern_name)` | Morning Star, Engulfing, Pin Bar, etc. |
| `candlestick_direction(timeframe, direction)` | Last N candles bullish/bearish |
| `candlestick_body_size(timeframe, threshold)` | Body > threshold × ATR |

### 2.7 Time Conditions

| Condition | Parameters |
|-----------|------------|
| `time_between(start_utc, end_utc)` | e.g., 06:00-16:00 UTC |
| `time_session(session_name)` | Asia, London, NY, overlap |
| `time_since_plan_start(minutes)` | Plan has been active > N min |
| `time_before_calendar_event(minutes)` | N min before major event |
| `no_calendar_event_within(minutes)` | Quiet window |

### 2.8 Position State Conditions (for management contingencies)

Only evaluable if trade is open:

| Condition | Parameters |
|-----------|------------|
| `profit_pips_above(threshold)` | Position in profit > N pips |
| `profit_pips_below(threshold)` | Position in loss < N pips (negative) |
| `profit_percent_above(threshold)` | Percentage of initial risk |
| `mfe_reached(pips)` | Max Favorable Excursion hit X pips |
| `mae_reached(pips)` | Max Adverse Excursion hit X pips |
| `duration_exceeds(minutes)` | Trade open > N min |
| `profit_retraced_from_peak(pips)` | Gave back N pips from MFE |

### 2.9 Macro / Cross-Market Conditions

Using existing market_context_fetcher:

| Condition | Parameters |
|-----------|------------|
| `dxy_above(value)` | DXY > threshold |
| `dxy_below(value)` | DXY < threshold |
| `dxy_direction(direction)` | Rising or falling |
| `vix_above(value)` | Risk-off confirmation |
| `correlated_move(symbol, direction)` | e.g., Silver moves same direction |

### 2.10 Advanced Pattern Conditions (NOT v1, for v2+)

Complex patterns we DON'T support in v1 but should plan for:

- Fair Value Gap (FVG) formation and retest
- Order Block (OB) touch and rejection
- Break of Structure (BoS)
- Change of Character (ChoCH)
- Liquidity sweep patterns
- Wyckoff accumulation/distribution phases
- Head & Shoulders, Cup & Handle, Double Top/Bottom

---

## 3 — Action Types Catalog

What Snow can do when conditions fire:

### 3.1 Entry Actions

| Action | Parameters |
|--------|------------|
| `execute_market_buy` | volume, sl, tp |
| `execute_market_sell` | volume, sl, tp |

(v1: Snow only executes market orders. Pending orders still via broker — Floki can use either path.)

### 3.2 Management Actions

| Action | Parameters |
|--------|------------|
| `adjust_sl(new_price)` | Move stop loss to new level |
| `adjust_tp(new_price)` | Move take profit |
| `trail_sl(trail_pips)` | Activate trailing stop |
| `move_sl_to_breakeven(offset_pips)` | Breakeven + offset |
| `move_sl_to_price(price)` | Absolute SL placement |

### 3.3 Exit Actions

| Action | Parameters |
|--------|------------|
| `close_full()` | Close entire position at market |
| `close_partial(percent)` | Close X% of position (25, 50, 75) |
| `close_at_price(price)` | Effectively a limit order |

### 3.4 Meta Actions

| Action | Parameters |
|--------|------------|
| `cancel_plan()` | Snow kills this plan without executing |
| `alert_floki(message)` | Notify but don't execute (for ambiguous situations) |
| `escalate_to_floki()` | Ask Floki for fresh decision before executing |

---

## 4 — Schema Proposal v1

### 4.1 Top-level structure

```yaml
plan:
  id: "PLAN-20260423-001"  # Snow generates
  created_by: "floki"
  created_at: "2026-04-23T17:30:00Z"
  expires_at: "2026-04-23T21:30:00Z"  # Optional TTL
  status: "pending" | "entered" | "closed" | "cancelled" | "expired"
  
  analysis:  # Free text from Floki — audit trail
    thesis: "Gold rallied to H1 resistance after DXY softened..."
    key_levels: [4735, 4720, 4707]
    confidence: 72
    regime_assumed: "TRENDING_BEARISH"
  
  entry:
    direction: "SELL"  # or "BUY"
    volume: 0.02
    conditions:  # ALL must be true (AND)
      - price_touches(4730.0, tolerance=0.3)
      - rsi_above(timeframe="H1", threshold=70)
      - macd_histogram_below(timeframe="H1", threshold=0)
      - time_between(start="06:00", end="20:00")
    initial_sl: 4740.0
    initial_tp: 4710.0
  
  management:  # Contingencies that modify the trade
    - name: "lock_profit_at_sr"
      priority: 7
      conditions:
        - price_below(4720.0)  # Reached intermediate support
      action:
        type: "move_sl_to_price"
        price: 4727.0  # Lock 3 pips profit
      fires: "once"  # or "every_time"
    
    - name: "trail_on_strong_momentum"
      priority: 5
      conditions:
        - mfe_reached(pips=50)
        - adx_above(timeframe="H1", threshold=30)
      action:
        type: "trail_sl"
        trail_pips: 15
      fires: "once"
  
  exit:  # Contingencies that close the trade
    - name: "resistance_rejection"
      priority: 9  # High priority, closes trade
      conditions:
        - price_above(4733.0)
        - next_candle_closes_below(4730.0, timeframe="M1")
      action:
        type: "close_full"
      fires: "once"
    
    - name: "partial_at_target"
      priority: 8
      conditions:
        - price_below(4715.0)
      action:
        type: "close_partial"
        percent: 50
      fires: "once"
    
    - name: "time_exit"
      priority: 3
      conditions:
        - time_since_plan_start(minutes=240)
        - profit_pips_below(10)
      action:
        type: "close_full"
      fires: "once"
  
  emergency:  # Snow-level safeguards
    max_loss_pips: 150  # Force close if exceeded
    max_duration_minutes: 480  # Auto close after 8h
    on_broker_error: "alert_floki"
```

### 4.2 Priority resolution (Opcão C + Opcão A confirmed by Hermano)

When multiple contingencies fire simultaneously:

**Default priority (by action type):**
1. `close_full` = 100 (highest)
2. `close_partial` = 80
3. `adjust_sl` / `adjust_tp` / `trail_sl` = 50
4. `move_sl_to_breakeven` = 40

**Override priority:** If Floki specifies `priority: N` (1-10), multiply by 10 and add to default.

**Example:**
- Contingency A: `close_full` with priority=5 → effective = 100 + 50 = 150
- Contingency B: `adjust_sl` with priority=9 → effective = 50 + 90 = 140
- Winner: A (close).

**Tie-breaker:** First defined in plan wins.

### 4.3 Fires semantics

- `fires: "once"` — contingency executes once then deactivates (most common)
- `fires: "every_time"` — re-evaluates forever (e.g., trailing SL every 5s check)

### 4.4 Condition evaluation

All conditions in a contingency's `conditions` array must be TRUE simultaneously (AND logic v1).

Future v2: OR logic via groups:
```yaml
conditions_or:
  - [condition_a, condition_b]  # This group AND
  - [condition_c]               # OR this group
```

---

## 5 — Snow Agent Architecture

### 5.1 Process model

```
snow.py (NEW Python agent)
  ├── Main loop (5s interval)
  │   ├── Load active plans from SQLite
  │   ├── Fetch live market data (M1 bars, tick, indicators)
  │   ├── For each plan:
  │   │   ├── If status=pending: evaluate entry conditions
  │   │   ├── If status=entered: evaluate management + exit contingencies
  │   │   └── If expired: mark expired, notify Floki
  │   ├── Process fired contingencies by priority
  │   ├── Execute actions (market orders via existing executor.py)
  │   └── Update plan state in SQLite
  ├── Plan CRUD API (for Floki's submit_plan_to_snow tool)
  ├── Dashboard API (expose active plans, recent triggers)
  └── Audit log (every condition evaluation, every action)
```

### 5.2 Integration with existing system

- **Market data:** Snow reads from `central_brain.py` output (already computed indicators)
- **Execution:** Snow calls `executor.py::execute_trade()` for market orders
- **MT5:** Via existing `ea_bridge.py` / direct MT5 API
- **State:** Snow's own SQLite tables + reads `positions` for active trade refs
- **Logging:** Uses existing `logger.py`
- **Dashboard:** New `/api/snow-plans` endpoint + Trade Room card

### 5.3 Data model (SQLite)

New tables:

```sql
CREATE TABLE snow_plans (
    id TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    trade_ticket INTEGER,  -- Set when entry fires
    entered_at TEXT,
    closed_at TEXT,
    outcome_pips REAL,
    outcome_dollars REAL
);

CREATE TABLE snow_triggers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    contingency_name TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    action_type TEXT NOT NULL,
    action_params TEXT,  -- JSON
    execution_status TEXT,
    execution_result TEXT,
    FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
);

CREATE TABLE snow_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    contingency_name TEXT,
    conditions_json TEXT,  -- Which conditions were checked
    result_json TEXT,      -- Which were true/false
    fired BOOLEAN,
    FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
);
```

### 5.4 Floki's new tool

Replace `execute_trade` (market) with:

```python
def submit_plan_to_snow(
    thesis: str,  # Free text analysis
    direction: str,  # BUY or SELL
    volume: float,
    entry_conditions: list,  # List of condition dicts
    initial_sl: float,
    initial_tp: float,
    management_contingencies: list = None,
    exit_contingencies: list = None,
    expires_at: str = None,
) -> dict:
    """
    Submit a contingency plan to Snow for execution.
    Returns plan_id and validation result.
    """
```

Floki also keeps:
- `close_trade(ticket, reason_for_direct_action)` — direct close (reason mandatory)
- `adjust_trade(ticket, new_sl, new_tp, reason_for_direct_action)` — direct adjust (reason mandatory)
- `submit_plan_to_snow_close(ticket, exit_contingencies)` — preferred: let Snow watch
- `submit_plan_to_snow_management(ticket, management_contingencies)` — preferred: let Snow manage

---

## 6 — Known Edge Cases

Catalog of scenarios that need explicit handling:

### 6.1 Condition evaluation edge cases

1. **Stale data:** Indicator computed at time T, plan evaluated at T+5s. Acceptable? (Yes for M1+, no for tick-level conditions — Snow v1 doesn't support tick-level)
2. **Bar boundary:** "Next M1 candle closes below X" — which M1 is "next"? Define: the first M1 bar that closes AFTER the triggering condition fires.
3. **Missing indicators:** If regime_detector fails, condition `regime_matches(X)` is undefined. Default: evaluate as FALSE (safe).
4. **Symbol data gap:** XAUUSD has occasional data gaps (weekend crossings). Plans should survive gaps; evaluation resumes when data available.

### 6.2 Execution edge cases

1. **Entry condition fires, but current spread is too wide.** Snow logs and retries for 60s (mirrors existing `wait_for_acceptable_spread`). If still wide, alert_floki.
2. **Entry condition fires, but max positions reached.** Snow cannot execute. Alert Floki, cancel plan.
3. **Exit condition fires, but position already closed (e.g., SL hit meanwhile).** Snow detects via ticket status, marks plan closed, no action.
4. **Two plans target opposite directions.** FLO-85 safety gate still applies — Snow respects it.

### 6.3 Priority edge cases

1. **Two contingencies fire same 5s cycle with equal priority.** First-defined wins (stable ordering).
2. **Close contingency fires, but partial_close already reduced position.** Snow closes what remains.
3. **Adjust SL contingency fires after SL already hit.** Impossible (position closed, plan marked closed first).

### 6.4 Plan lifecycle edge cases

1. **Plan expires before entry fires.** Snow marks expired, notifies Floki. No execution.
2. **Entry fires but broker rejects.** Snow retries 3x over 30s, then cancels plan and alerts.
3. **Plan submitted with invalid condition reference.** Schema validator rejects at submission time.
4. **Floki submits plan, then immediately cancels.** Snow honors cancel (lifecycle: pending → cancelled).

---

## 7 — Testing Strategy

### 7.1 Unit tests (per condition evaluator)

Every condition type (§2) needs unit tests:
- True case (condition should fire)
- False case (condition should not fire)
- Boundary case (exactly at threshold)
- Data-missing case (should return False safely)

### 7.2 Integration tests (per contingency pattern)

Test full contingency lifecycle:
- Plan submitted → conditions fire → action executed → state updated
- Plan submitted → expires without firing → marked expired
- Plan with multiple contingencies → priority resolution correct

### 7.3 Shadow mode (pre-cutover)

Phase 3 of implementation:
- Snow runs parallel to existing system
- Floki submits plans AS WELL AS making direct executions
- Log what Snow WOULD have done vs what actually happened
- Compare outcomes for 1-2 weeks
- Dashboard shows "shadow vs actual" comparison

### 7.4 Stress tests

- 10 simultaneous plans, each with 5 contingencies
- Evaluate latency (5s budget)
- Memory stability over 24h run
- Broker disconnect recovery

---

## 8 — Prompt Engineering (Floki)

Floki needs new prompt guidance for plan-first thinking. Draft sections to add:

### 8.1 New plan-first principles

```
When you identify a trade opportunity, you DO NOT execute immediately.
Instead, you submit a PLAN to Snow with:
1. Entry conditions (when should trade open)
2. Management contingencies (how should trade be modified during its life)
3. Exit contingencies (what scenarios should close it)

Snow is a 5-second executor. It will watch markets and fire when conditions match.

You are the SCENARIO ARCHITECT. Think: "What if price does X? What if it does Y?"
Write a plan for each scenario you can reasonably anticipate.
```

### 8.2 Contingency design heuristics

```
A good plan has 2-5 exit contingencies covering:
- Rejection scenario (price fails at anticipated resistance/support)
- Target scenario (price reaches profit zone)
- Time scenario (price stalls too long)
- Invalidation scenario (macro context changes)
- Volatility scenario (ATR explodes)

Don't write 20 contingencies. Write the 5 that capture 80% of scenarios.
```

### 8.3 When to use direct action

```
PREFER: submit_plan_to_snow for all entries
PREFER: submit_plan_to_snow_close for planned exits
PREFER: submit_plan_to_snow_management for SL trails

USE DIRECT ACTION ONLY IF:
- Breaking news requires immediate close (news too volatile for plan)
- Snow plan is clearly wrong given new information (cancel plan + direct close)
- Emergency system issue

When using direct action, you MUST fill reason_for_direct_action with:
- What changed since the plan was written
- Why the plan is no longer valid
- What outcome you expect from direct action
This is for audit. If you can't justify it in writing, use a plan.
```

---

## 9 — Dashboard Visualization

### 9.1 Trade Room: New "Snow Plans" panel

Shows:
- Active plans (pending, entered)
- For each plan: progress bar of entry conditions met (3/5, etc.)
- Contingencies: listed with status (waiting, armed, fired)
- Next evaluation time

### 9.2 Per-plan detail view

- Full plan JSON (collapsed)
- Timeline: created → conditions evaluated → fires → executions
- Comparison: expected vs actual outcome

### 9.3 History / Audit trail

- All plans ever created
- Which fired vs expired vs cancelled
- Outcome analysis (did Snow's firing beat Floki's hypothetical direct action?)

---

## 10 — Rollout Plan

### Phase 1 — RFC (next session)

Output: detailed RFC document covering:
- Schema finalized
- State machine diagram
- Every component API
- Complete test plan
- Dashboard wireframes

### Phase 2 — Implementation (Claude Code)

Order:
1. Schema + validator (standalone, unit-testable)
2. Condition evaluator (takes market state + condition → bool)
3. Snow core loop (load plans, evaluate, execute)
4. Floki tool integration (submit_plan_to_snow)
5. Dashboard visualization
6. Audit trail queries

Rules 14, 15, 18 enforced throughout.

### Phase 3 — Shadow mode

- Deploy Snow reading plans but NOT executing
- Floki instructed to submit plans alongside market orders
- 1-2 weeks comparing Snow-hypothetical vs actual outcomes
- Fine-tune condition thresholds, edge cases

### Phase 4 — Cutover

- Remove market execute from Floki tools
- Snow becomes only path for market orders
- Week-long intensive monitoring
- Rollback plan: if critical issue, re-enable market execute as emergency tool

---

## 11 — Success Metrics (for Phase 4+)

Measure after 2 weeks post-cutover:

| Metric | Baseline (pre-Snow) | Target |
|--------|---------------------|--------|
| SL adjustments per trade | ~3-9 (B-1 pattern) | <2 |
| SELL WR | 8-14% | >25% (if mechanism is right) |
| Pending-path $ share | 5× market | Maintained or better |
| Avg trade duration | ~40 min | Stable or +20% (less premature exits) |
| Direct action usage | N/A | <20% of executions |
| Audit trail completeness | Partial | 100% |

---

## 12 — References

### 12.1 Institutional trading systems
- Fidelity Conditional Orders documentation
- Charles Schwab Advanced Order Types guide
- Trading Technologies (TT) TT OCO, TT Bracket documentation
- Alpaca Markets OCO/OTO API docs
- NautilusTrader event-driven engine (open source)

### 12.2 Trading concepts
- ICT/Smart Money Concepts framework (Fair Value Gaps, Order Blocks, BoS, ChoCH)
- TradingView alert operator reference
- Institutional pre-trade checklists (Trade That Swing, Bulls on Wall Street)
- Break-and-Retest patterns (Scarface methodology, Capital.com reference)
- Confluence trading (XS, TradeFundrr)
- Multi-timeframe analysis (paperswithbacktest, TrendRider)

### 12.3 Exit management
- QuantifiedStrategies exit research (backtests show simple > complex)
- ATAS partial close methodology
- Mind Math Money 9 exit strategies framework
- LuxAlgo trailing stop research

---

**End of research document v1. Ready for Phase 1 RFC.**
