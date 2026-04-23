# FLO-347 — Snow Implementation RFC v1

**Status:** **FROZEN** (approved by Hermano, 2026-04-23 Opção A integral). Editorial edits applied: Phase 4.5 gate, §13.4 caveat, §4.1.1 schema_version cross-ref.
**Author:** DEV
**Date:** 2026-04-23
**Companion:** `FLO-347_Snow_Research_v1.md` (CTO-authored research & schema reference)
**Dependency:** **FLO-348** (thread-safety hardening: executor_lock + mt5_lock + call-site audit) ships BEFORE Phase 2 implementation. All §5/§7 references assume FLO-348 landed.
**Tickets opened alongside:** FLO-349 (Simba coexistence audit, P2), FLO-350 (dashboard history view, P2), FLO-351 (dashboard auth audit, P2, parallel).

---

## 0 — What this RFC is and is not

**IS:** concrete implementation proposal grounded in the FlokiWatch codebase. Maps research-doc concepts to specific files, functions, schemas, locks, and dataflows. Flags uncertainties and proposes alternatives where tradeoffs exist.

**IS NOT:** production code. No function bodies beyond illustrative signatures. No prompt text. No dashboard HTML. Those land in Phase 2 implementation PRs.

**Pip convention:** 1 pip = 0.1 price units (per `capture.py:19` `PIP_SIZE = 0.1`). Used consistently wherever pips appear below.

**TZ convention:** all internal timestamps are true UTC via `tz_utils.utc_iso()`; MT5 epoch values are converted at the boundary via `executor._mt5_server_offset()`. Confirmed Session A.

---

## 1 — Architectural Overview

### 1.1 Component diagram

```
┌───────────────────── main.py (existing) ──────────────────────┐
│                                                               │
│  XAUUSDBot.start()                                            │
│    ├── spawn Sage thread (existing, unchanged)                │
│    ├── spawn Echo thread (existing, unchanged)                │
│    ├── spawn Luna thread (existing, unchanged)                │
│    ├── spawn Rex Monitor thread (existing, unchanged)         │
│    ├── spawn Simba AgentMonitor.check() (existing, 30s)       │
│    ├── spawn Snow loop  ◄── NEW, daemon thread, 5s            │
│    └── main loop (Floki cycle, position mgmt)                 │
│                                                               │
└───────────────────────────────────────────────────────────────┘
                  │
                  │ shared in-process state:
                  │   - executor (MT5Executor singleton, FLO-348 locked)
                  │   - _last_agent_data (cached semantic snapshot)
                  │   - history.db (SQLite WAL)
                  ▼
┌──────────────────── snow/ (new package) ─────────────────────┐
│                                                              │
│  snow_loop.py                                                │
│    └── SnowLoop.tick() every 5s:                             │
│          1. load active plans from db                        │
│          2. refresh live_data (direct MT5)                   │
│          3. evaluate each plan's contingencies               │
│          4. resolve priorities                               │
│          5. execute fired actions via executor (locked)      │
│          6. persist state changes                            │
│                                                              │
│  schema.py          — plan/contingency/condition dataclasses │
│  validator.py       — submit-time plan validation            │
│  live_data.py       — fresh MT5 ticks + M1 bars + indicators │
│  semantic_cache.py  — adapter over bot._last_agent_data      │
│  evaluator/         — one module per condition primitive     │
│    price.py                                                  │
│    indicator.py                                              │
│    position.py                                               │
│    time_.py                                                  │
│    structural.py                                             │
│  actions.py         — action wrappers → executor             │
│  priority.py        — priority resolution algorithm          │
│  db.py              — snow_plans/triggers/evaluations CRUD   │
│  recovery.py        — startup crash-recovery protocol        │
│  tests/             — pytest, ~60 unit + 10 integration      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
                  │
                  │ reads/writes
                  ▼
┌──────────── history.db (existing SQLite, WAL) ──────────────┐
│                                                             │
│  (existing tables: trades, agent_proactive_analyses, ...)   │
│                                                             │
│  snow_plans        ◄── NEW                                  │
│  snow_triggers     ◄── NEW                                  │
│  snow_evaluations  ◄── NEW (bounded, state-change only)     │
│                                                             │
└─────────────────────────────────────────────────────────────┘

Floki side (existing ai_agent.py):
  - new tool: submit_plan_to_snow(plan_dict)
  - existing tools retained: close_trade / adjust_trade / execute_trade
    (all require reason_for_direct_action when used bypassing Snow)
```

### 1.2 Data flow for a typical plan

```
[T+0]       Floki cycle produces plan_dict
            → submit_plan_to_snow(plan_dict)
            → validator.validate(plan_dict)         # synchronous, ≤100ms
            → snow.db.insert_plan(row)              # status=PENDING
            → return {"plan_id": "PLAN-...", "validated": true}

[T+5s .. T+Δ]   Snow loop ticks every 5s:
                → load plans WHERE status IN ('PENDING', 'ACTIVE', 'TRIGGERED', 'CLOSING')
                → live_data.refresh()
                → for plan P:
                    for contingency C in P.contingencies:
                      if C.state == ARMED:
                        result = evaluator.evaluate(C.conditions, ctx)
                        if all true → C.state = FIRED, enqueue_action

[T+Δ]       Priority resolver orders fired actions
            → for each: acquire executor_lock, call executor, release
            → update plan/contingency state in DB
            → write snow_triggers row
```

### 1.3 Who owns what

| Concern | Owner | Notes |
|---|---|---|
| Plan creation | Floki (via tool) | Snow never creates plans |
| Plan validation | Snow (`validator.py`) | Synchronous at submit time |
| Plan state (lifecycle) | Snow | Floki reads via DB or API |
| Condition evaluation | Snow | Uses live + semantic data |
| Live data fetch | Snow (`live_data.py`) | Direct MT5; own pytest |
| Semantic data | Floki's cycle | Snow reads `bot._last_agent_data` |
| Action execution | Snow → `executor` | Via executor_lock (FLO-348) |
| Position management | Snow (plans) + monitor.py (fallback) | No conflict; monitor.py is EA-down safety |
| Trade/deal records | existing `db_writer.py` | Unchanged; Snow inserts additional rows to snow_* tables |

### 1.4 What Snow does NOT do in v1

- Does not create plans autonomously (no Snow-initiated trades)
- Does not submit pending orders to MT5 (Floki's existing `place_pending_order` unchanged)
- Does not modify Floki's prompt at runtime
- Does not make decisions under ambiguity — if conditions unresolvable, fail-safe FALSE + alert
- Does not support OR logic in conditions (v2)
- Does not support tick-level conditions (v2)
- Does not replace Simba (coexists; Simba re-evaluates triggers from a different data source)

---

## 2 — Schema Specification

### 2.1 Python representation — Pydantic v2

**Decision:** Pydantic v2 (not dataclasses, not raw JSON).

**Rationale:**
- Validation for free (type + value constraints)
- JSON schema export for dashboard / future API clients
- Discriminated unions handle the condition/action type zoo cleanly
- Already a transitive dep via OpenAI SDK — zero new runtime cost

**Alternative considered:** `dataclasses` + manual validation. Rejected: ~300 LoC of hand-rolled validators for the 15 primitives, repeated for actions, error-prone.

**Alternative considered:** raw JSON with jsonschema. Rejected: no static typing in evaluator code, worse editor support, and we end up maintaining a JSON schema AND a Python parser.

### 2.2 Top-level `Plan` model

```python
# snow/schema.py

from enum import Enum
from typing import Literal, Optional, Union
from pydantic import BaseModel, Field, field_validator

SCHEMA_VERSION = 1

class PlanStatus(str, Enum):
    PENDING   = "pending"
    TRIGGERED = "triggered"
    ACTIVE    = "active"
    CLOSING   = "closing"
    CLOSED    = "closed"
    CANCELLED = "cancelled"
    EXPIRED   = "expired"
    FAILED    = "failed"

class ContingencyState(str, Enum):
    ARMED        = "armed"
    FIRED        = "fired"
    FAILED       = "failed"
    DEACTIVATED  = "deactivated"   # fires=once and already fired

class Plan(BaseModel):
    schema_version: int = SCHEMA_VERSION
    id:            str = Field(pattern=r"^PLAN-\d{8}-\d{3}$")
    created_by:    Literal["floki"] = "floki"
    created_at:    str                 # ISO-8601 UTC with Z
    expires_at:    Optional[str] = None
    status:        PlanStatus

    analysis: "PlanAnalysis"
    entry:    "EntryBlock"
    management: list["Contingency"] = Field(default_factory=list, max_length=10)
    exit:       list["Contingency"] = Field(default_factory=list, max_length=10)
    emergency:  "EmergencyBlock"

    trade_ticket: Optional[int] = None  # set on TRIGGERED→ACTIVE transition
    entered_at:   Optional[str] = None
    closed_at:    Optional[str] = None
    outcome_pips: Optional[float] = None
    outcome_usd:  Optional[float] = None

    @field_validator("expires_at", "created_at", "entered_at", "closed_at")
    def _utc_suffix(cls, v):
        if v and not v.endswith("Z"):
            raise ValueError("all timestamps must end with 'Z'")
        return v
```

### 2.3 Entry, analysis, emergency blocks

```python
class PlanAnalysis(BaseModel):
    thesis:          str              = Field(max_length=2000)
    key_levels:      list[float]      = Field(default_factory=list, max_length=10)
    confidence:      int              = Field(ge=0, le=100)
    regime_assumed:  Optional[str]    = None

class EntryBlock(BaseModel):
    direction:    Literal["BUY", "SELL"]
    volume:       float = Field(gt=0, le=2.0)
    conditions:   list["Condition"]          = Field(min_length=1, max_length=8)
    initial_sl:   float
    initial_tp:   float
    reason_for_direct_action: Optional[str] = None   # only populated if Floki bypasses Snow for entry

class EmergencyBlock(BaseModel):
    max_loss_pips:        float                = Field(default=150, gt=0, le=1000)
    max_duration_minutes: int                  = Field(default=480, gt=0, le=10080)
    on_broker_error:      Literal["alert_floki", "close_full", "cancel_plan"] = "alert_floki"
```

### 2.4 `Contingency` model

```python
class ContingencyFires(str, Enum):
    ONCE       = "once"
    EVERY_TIME = "every_time"

class Contingency(BaseModel):
    name:            str = Field(max_length=40)
    priority:        int = Field(default=5, ge=1, le=10)   # Floki override 1-10, default 5
    conditions:      list["Condition"]    = Field(min_length=1, max_length=8)
    action:          "Action"
    fires:           ContingencyFires     = ContingencyFires.ONCE
    state:           ContingencyState     = ContingencyState.ARMED
    fired_at:        Optional[str]        = None
    evaluated_count: int                  = 0

    # opt-in guards (CTO decision CK-4: enforced in evaluator, NOT in executor)
    guards: Optional["ContingencyGuards"] = None

class ContingencyGuards(BaseModel):
    only_if_tighter_sl:     bool = False
    cooldown_seconds:       int  = 0
    min_mfe_pips_required:  Optional[float] = None
    max_adjustments_total:  Optional[int] = None
```

### 2.5 Condition primitives (14 for v1)

Discriminated by `type` field. All return bool from `.evaluate(ctx)`.

| # | Type | Source | Purpose | Parameters |
|---|---|---|---|---|
| 1 | `price_above` | live | Price > level | `level: float` |
| 2 | `price_below` | live | Price < level | `level: float` |
| 3 | `rsi` | live | RSI on TF vs threshold | `tf`, `op ∈ {above,below}`, `threshold` |
| 4 | `macd_histogram` | live | MACD hist on TF | `tf`, `op`, `threshold` |
| 5 | `ema_relation` | live (M1/M5) or semantic (H1+) | EMA structure | `tf`, `period`, `relation ∈ {price_above, price_below, aligned_bull, aligned_bear}` |
| 6 | `atr` | live | ATR magnitude vs baseline | `tf`, `op`, `multiplier`, `baseline_pips` |
| 7 | `price_at_sr_zone` | semantic | Near strong S/R | `zone_type ∈ {support, resistance, any}`, `tolerance_pips` |
| 8 | `price_at_fibonacci` | semantic | At Fib level | `level ∈ {0.382, 0.5, 0.618, 0.786}` |
| 9 | `profit_pips` | live (ticket) | Position P/L in pips | `op`, `threshold` |
| 10 | `mfe_reached` | live (snow-owned tracker) | Max favorable ≥ pips | `pips` |
| 11 | `mae_reached` | live (snow-owned tracker) | Max adverse ≥ pips | `pips` |
| 12 | `profit_retraced_from_peak` | live | Gave back pips from MFE | `pips` |
| 13 | `duration_exceeds` | clock | Trade age > N min | `minutes` |
| 14 | `time_between` | clock | UTC time window | `start_utc: HH:MM`, `end_utc: HH:MM` |

**Explicitly NOT in v1** (deferred to v2, rationale in §6.6):
- `price_crosses_above` / `price_crosses_below` — stateful (requires prior-tick price memory); punting the state-cache persistence question to v2
- All conditions in research §2.2.4 (ADX/DI), §2.3 (volume), §2.5 (MTF composites), §2.6 (candlestick), §2.9 (DXY/VIX), §2.10 (FVG/OB/BoS/etc.)

```python
class _BaseCondition(BaseModel):
    # All concrete condition classes extend this.
    pass

class PriceAbove(_BaseCondition):
    type: Literal["price_above"] = "price_above"
    level: float

class PriceBelow(_BaseCondition):
    type: Literal["price_below"] = "price_below"
    level: float

class RSI(_BaseCondition):
    type: Literal["rsi"] = "rsi"
    tf:   Literal["M1","M5","M15","H1","H4","D1"]
    op:   Literal["above","below"]
    threshold: float = Field(ge=0, le=100)

# ... etc for the 16 primitives

Condition = Union[
    PriceAbove, PriceBelow,
    RSI, MACDHistogram, EMARelation, ATR,
    PriceAtSRZone, PriceAtFibonacci,
    ProfitPips, MFEReached, MAEReached, ProfitRetracedFromPeak,
    DurationExceeds, TimeBetween,
]
# Pydantic v2 discriminated union on 'type'. 14 primitives.
```

**v2 backlog** (explicitly out of v1 scope per CTO): MTF confluence composites, volume conditions, ADX / DI conditions, candlestick patterns, Bollinger, stochastic, macro (DXY/VIX), advanced patterns (FVG/OB/BoS), OR-logic groups.

### 2.6 Action primitives

| Action | Executor call | Notes |
|---|---|---|
| `execute_market` | `executor.execute_trade(direction, vol, sl, tp, comment="snow_entry")` | Entry only |
| `adjust_sl` | `executor.modify_position(ticket, new_sl=x)` | Guard at plan level via `only_if_tighter_sl` |
| `adjust_tp` | `executor.modify_position(ticket, new_tp=x)` | |
| `move_sl_to_breakeven` | `executor.modify_position(ticket, new_sl=entry + offset)` | `offset_pips` param |
| `move_sl_to_price` | same as `adjust_sl` with explicit price | |
| `trail_sl` | repeated `adjust_sl` with distance from current | fires=every_time typically |
| `close_full` | `executor.close_position(ticket)` | No volume specified |
| `close_partial` | `executor.close_position(ticket, volume=frac*current)` | frac ∈ (0,1) |
| `cancel_plan` | snow-internal state transition; no MT5 call | |
| `alert_floki` | `alerts.alert_error("Snow plan message", severity="info")` | No MT5 call |
| `escalate_to_floki` | same as `alert_floki` + sets escalation flag on plan | |

```python
class ActionCloseFull(BaseModel):
    type: Literal["close_full"] = "close_full"

class ActionMoveSL(BaseModel):
    type: Literal["move_sl_to_price"] = "move_sl_to_price"
    price: float

class ActionMoveSLBE(BaseModel):
    type: Literal["move_sl_to_breakeven"] = "move_sl_to_breakeven"
    offset_pips: float = 0

# ... etc

Action = Union[ActionExecuteMarket, ActionAdjustSL, ActionAdjustTP,
               ActionMoveSLBE, ActionMoveSL, ActionTrailSL,
               ActionCloseFull, ActionClosePartial,
               ActionCancelPlan, ActionAlertFloki, ActionEscalateToFloki]
```

### 2.7 Schema versioning

- `schema_version: int` field on `Plan`. Default `SCHEMA_VERSION = 1`.
- Validator rejects plans whose `schema_version` > known version.
- Upgrade path (v2): migration script reads snow_plans.plan_json, transforms, writes back under schema_version=2. Snow loop handles both versions via factory.
- Breaking changes always go through a new version. Non-breaking additions (new conditions, new fields with defaults) do NOT require version bump.

### 2.8 Example complete plan (illustrative)

Compact example matching research §4.1 but in v1 schema form:

```json
{
  "schema_version": 1,
  "id": "PLAN-20260423-001",
  "created_by": "floki",
  "created_at": "2026-04-23T17:30:00Z",
  "expires_at": "2026-04-23T21:30:00Z",
  "status": "pending",
  "analysis": {
    "thesis": "Gold at H1 Fib 61.8% retracement with bearish regime + DXY tailwind",
    "key_levels": [4735.0, 4720.0, 4707.0],
    "confidence": 72,
    "regime_assumed": "TRENDING_BEARISH"
  },
  "entry": {
    "direction": "SELL",
    "volume": 0.02,
    "conditions": [
      {"type": "price_above", "level": 4730.0},
      {"type": "rsi", "tf": "H1", "op": "above", "threshold": 70},
      {"type": "time_between", "start_utc": "06:00", "end_utc": "20:00"}
    ],
    "initial_sl": 4740.0,
    "initial_tp": 4710.0
  },
  "management": [
    {
      "name": "lock_10_at_support",
      "priority": 7,
      "conditions": [{"type": "price_below", "level": 4720.0}],
      "action": {"type": "move_sl_to_price", "price": 4727.0},
      "fires": "once",
      "guards": {"only_if_tighter_sl": true, "cooldown_seconds": 60}
    }
  ],
  "exit": [
    {
      "name": "rejection_exit",
      "priority": 9,
      "conditions": [{"type": "price_above", "level": 4733.0}],
      "action": {"type": "close_full"},
      "fires": "once"
    },
    {
      "name": "time_stop",
      "priority": 3,
      "conditions": [
        {"type": "duration_exceeds", "minutes": 240},
        {"type": "profit_pips", "op": "below", "threshold": 10}
      ],
      "action": {"type": "close_full"},
      "fires": "once"
    }
  ],
  "emergency": {
    "max_loss_pips": 150,
    "max_duration_minutes": 480,
    "on_broker_error": "alert_floki"
  }
}
```

---

## 3 — State Machine (Plan Lifecycle)

### 3.1 Plan-level states

```
        ┌──────────────────────────────────────────────────────────┐
        │                                                          │
        │                   ┌─────────────┐                        │
        │                   │  PENDING    │ ──submit+validate───── │  entry
        │                   │  (entry     │                        │
        │                   │  watching)  │                        │
        │                   └──┬────┬─────┘                        │
        │                      │    │                              │
        │   entry fires        │    │  floki cancels / TTL hits    │
        │                      ▼    ▼                              │
        │              ┌─────────┐  ┌───────────┐                  │
        │              │TRIGGERED│  │CANCELLED  │                  │
        │              │(broker  │  │ /EXPIRED  │(terminal)        │
        │              │calling) │  └───────────┘                  │
        │              └────┬────┘                                 │
        │    broker success │ broker reject × 3 retries            │
        │                   ▼                                      │
        │              ┌────────┐        ┌────────┐                │
        │              │ ACTIVE │◄──────►│FAILED  │ (broker lost    │
        │              │(mgmt + │        │(terminal) position)     │
        │              │ exit)  │        └────────┘                │
        │              └───┬────┘                                  │
        │                  │                                       │
        │ exit fires       │    position closed externally (SL/TP/ │
        │                  │    monitor.py/Floki direct close)     │
        │                  ▼                                       │
        │              ┌─────────┐                                 │
        │              │CLOSING  │  transient; ≤1 cycle            │
        │              └────┬────┘                                 │
        │                   ▼                                      │
        │              ┌─────────┐                                 │
        │              │ CLOSED  │ (terminal)                      │
        │              └─────────┘                                 │
        └──────────────────────────────────────────────────────────┘
```

### 3.2 Transition table

| From | To | Trigger | DB write |
|---|---|---|---|
| (none) | PENDING | `submit_plan_to_snow` returns OK | INSERT snow_plans |
| PENDING | TRIGGERED | entry contingency fires | UPDATE status, snow_triggers row |
| PENDING | CANCELLED | Floki calls `cancel_plan(plan_id)` | UPDATE |
| PENDING | EXPIRED | `expires_at < now` during tick | UPDATE |
| TRIGGERED | ACTIVE | `execute_trade` returns ticket | UPDATE (trade_ticket, entered_at) |
| TRIGGERED | FAILED | 3× executor retries fail | UPDATE + alert |
| ACTIVE | CLOSING | exit contingency fires | UPDATE + snow_triggers |
| ACTIVE | CLOSED | monitor detects position gone (not via Snow) | UPDATE (outcome fields populated from MT5 deal history) |
| ACTIVE | FAILED | MT5 reports ticket disappeared mid-cycle AND no deal record within 60s | UPDATE + alert |
| CLOSING | CLOSED | close_position returns success | UPDATE (outcome_pips, outcome_usd from deal) |

### 3.3 Contingency-level states (within a plan)

```
ARMED ─── conditions eval every tick ───┐
  │                                     │
  │  conditions ALL true                │
  ▼                                     │
FIRED ── action executes ──► success ──►│ fires=every_time → back to ARMED
  │                                     │ fires=once       → DEACTIVATED
  │  action fails after retries         │
  ▼                                     │
FAILED (terminal for this contingency; plan continues) ◄──────────┘
```

### 3.4 Invariants

| # | Invariant | Where enforced |
|---|---|---|
| I1 | A plan has exactly one `trade_ticket` (or none before entry) | `recovery.py` startup check; state transition guards |
| I2 | `TRIGGERED` is transient; must resolve to ACTIVE or FAILED within 60s | Snow loop watchdog |
| I3 | `CLOSING` is transient; must resolve to CLOSED within 60s | Snow loop watchdog |
| I4 | Emergency `max_loss_pips` is evaluated every tick on ACTIVE plans regardless of contingency state | hardcoded check in loop |
| I5 | Effective priority ∈ [7, 228] (see §8.1); no overflow possible | schema validator + enum-like base registry |
| I6 | When Snow executes action, plan row updated in same DB transaction | sqlite3 transaction within lock |
| I7 | At most ONE plan can own any given `trade_ticket` at a time | UNIQUE index on snow_plans.trade_ticket WHERE status IN ('TRIGGERED','ACTIVE','CLOSING') |

### 3.5 Crash recovery

On Snow startup (or after restart), `recovery.reconcile()`:

1. Load all plans with `status IN ('PENDING','TRIGGERED','ACTIVE','CLOSING')`.
2. For each:
   - **PENDING**: if `expires_at < now` → EXPIRED. Else: resume.
   - **TRIGGERED**: query MT5 `history_orders_get(from_dt=created_at)` for matching comment/magic. If a position found → ACTIVE + link ticket. If no position found AND `created_at + 60s < now` → FAILED. Else: wait one more cycle.
   - **ACTIVE**: query `executor.get_open_positions()` by ticket. If found → resume evaluation. If not found → query `mt5.history_deals_get(position=ticket)` for close deal. If close deal found → mark CLOSED with outcome from deal. Else → FAILED (position lost; alert).
   - **CLOSING**: same as ACTIVE, but bias toward CLOSED if position gone.

Recovery runs BEFORE the first tick of the loop. Plans in unexpected states (e.g., `FIRED` on a contingency with no corresponding DB trigger row) are flagged via alert but not auto-fixed — CTO intervenes.

---

## 4 — Storage Design

### 4.1 Tables

```sql
-- 4.1.1 snow_plans: one row per plan; lifecycle + outcome
CREATE TABLE IF NOT EXISTS snow_plans (
    id               TEXT PRIMARY KEY,         -- "PLAN-YYYYMMDD-NNN"
    schema_version   INTEGER NOT NULL,         -- see §14.1 item 6 (dual-version coexistence during v1→v2 migration)
    created_by       TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT,
    status           TEXT NOT NULL,            -- see PlanStatus enum
    plan_json        TEXT NOT NULL,            -- serialized Pydantic model
    trade_ticket     INTEGER,                  -- NULL before entry, FK-ish to trades.ticket
    entered_at       TEXT,
    closed_at        TEXT,
    outcome_pips     REAL,
    outcome_usd      REAL,
    last_evaluated_at TEXT                     -- bookkeeping
);

CREATE INDEX IF NOT EXISTS idx_snow_plans_status
    ON snow_plans(status);

CREATE INDEX IF NOT EXISTS idx_snow_plans_expires
    ON snow_plans(expires_at)
    WHERE status IN ('pending', 'active', 'triggered');

CREATE UNIQUE INDEX IF NOT EXISTS idx_snow_plans_live_ticket
    ON snow_plans(trade_ticket)
    WHERE trade_ticket IS NOT NULL
      AND status IN ('triggered', 'active', 'closing');   -- enforces I7


-- 4.1.2 snow_triggers: append-only audit log of every contingency firing
CREATE TABLE IF NOT EXISTS snow_triggers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id           TEXT NOT NULL,
    contingency_name  TEXT NOT NULL,
    contingency_kind  TEXT NOT NULL,           -- 'entry' | 'management' | 'exit' | 'emergency'
    fired_at          TEXT NOT NULL,
    action_type       TEXT NOT NULL,
    action_params     TEXT,                    -- JSON
    execution_status  TEXT NOT NULL,           -- 'success' | 'failed' | 'retrying' | 'skipped_guard'
    execution_result  TEXT,                    -- JSON: error message, new sl/tp, etc.
    cycle_duration_ms INTEGER,                 -- eval → executor return
    FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
);

CREATE INDEX IF NOT EXISTS idx_snow_triggers_plan_fired
    ON snow_triggers(plan_id, fired_at DESC);


-- 4.1.3 snow_evaluations: BOUNDED state-change log (NOT raw polling)
CREATE TABLE IF NOT EXISTS snow_evaluations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id             TEXT NOT NULL,
    contingency_name    TEXT NOT NULL,
    evaluated_at        TEXT NOT NULL,
    event               TEXT NOT NULL,         -- 'armed' | 'all_true_first_time' | 'fired' | 'deactivated' | 'guard_blocked'
    conditions_snapshot TEXT,                  -- JSON: per-condition bool result (for the event)
    FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
);

CREATE INDEX IF NOT EXISTS idx_snow_evaluations_plan_time
    ON snow_evaluations(plan_id, evaluated_at DESC);
```

### 4.2 Evaluation-log sizing

Research §5.3 proposed writing snow_evaluations on every cycle. Back-of-envelope: 10 plans × 5 contingencies × 1 eval per 5s = ~173k rows/day. **Rejected as written.**

**Decision:** `snow_evaluations` stores **state changes only** (ARMED, guard-blocked, FIRED, DEACTIVATED), not raw polling. Expected volume: ≤100 rows/day.

**Retention:** keep 30 days via daily pruning job (scheduled from main.py startup, mirrors `agent_monitor` once-per-day patterns).

### 4.3 Concurrency and access layer

- All access via `snow/db.py` which uses `db_writer._get_connection()` (WAL + 5s timeout, already in codebase).
- New connection per call; close in `try/finally`. No persistent connection pool.
- Read-heavy workload (plan reload every 5s) is handled by WAL readers without blocking writers.
- Writes are small (≤200 bytes JSON) and infrequent (triggers on state change).

### 4.4 Schema migration

Run via `snow/db.init_snow_tables()` called from main.py startup AFTER `db_writer.init_db()`. Idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

---

## 5 — Process Model

### 5.1 Thread model

Snow runs as a single daemon thread spawned from `XAUUSDBot.start()` during initialization, following the Sage/Echo/Luna/Rex-Monitor pattern:

```python
# main.py — added inside start() near existing spawns:
from snow import snow_loop
self._snow_thread = threading.Thread(
    target=snow_loop.run_forever,
    args=(self,),                    # pass bot so snow can read _last_agent_data
    name="SnowLoop",
    daemon=True,
)
self._snow_thread.start()
```

`snow_loop.run_forever(bot)` is a `while self.running:` loop that:
1. Sleeps 5 s (interruptible via `self.running` check after shorter sub-sleeps)
2. Acquires `_snow_lock.acquire(blocking=False)`; if held, skip this cycle (prevents overlap if cycle > 5s)
3. Runs `tick(bot)` → catch & log all exceptions (never lets thread die)
4. Releases lock

### 5.2 Dependencies on FLO-348 (blocking)

FLO-347 Phase 2 implementation **cannot start** until FLO-348 lands:

| Lock | Scope | Owner | Used by |
|---|---|---|---|
| `executor_lock` | module-level `threading.RLock` in `executor.py` | FLO-348 | `execute_trade`, `modify_position`, `close_position`, `close_pending_order`; called from Floki (main.py), monitor.py, AND snow/actions.py |
| `mt5_lock` | module-level `threading.RLock` wrapping every `mt5.*` call | FLO-348 | `executor.py`, `deal_resolver.py`, `agent_data_builder.py`, `snow/live_data.py`, anywhere else `mt5.*` is called |
| `_snow_lock` | instance-level on `SnowLoop` | Snow | own loop only |

`mt5_lock` is RLock (re-entrant): single-thread call chains into MT5 helpers work. But the same THREAD can re-enter; a different thread blocks until release. Expected contention: ≤5 ms per mt5 call (most are ≤ tens of ms), so Snow's 5 s cycle has ample headroom.

### 5.3 Interaction with main.py's existing loop

**main.py's cycle** (Floki + monitor):
- Sleeps in 1 s chunks totaling ~300 s between full analysis cycles
- During sleep: checks `agent_monitor.check()` every 30 s, `positions` every 1-2 s
- Makes executor calls: during `execute_trade`, during `_monitor_cycle`, during `adjust_trade`, etc.

**Snow's cycle** (5 s):
- Independent of main loop
- Shares `bot._last_agent_data` (read-only from Snow's side)
- Shares `executor` singleton (writes via executor_lock)

**No changes required in main.py** beyond the thread spawn. The `bot` reference passed to Snow is the orchestrator; Snow reads state via `bot._last_agent_data`, `bot.running`, `bot.executor`.

### 5.4 Failure isolation

- Snow thread crash: logged + alerted; daemon=True so doesn't prevent shutdown. Auto-restart on next `main.start()` (i.e., requires process restart — no in-process resurrection in v1).
- MT5 disconnect: `live_data.py` catches, returns None, evaluator treats missing data as False. Plans stay in PENDING/ACTIVE; no actions fire until data returns. Alert emitted after 2 min of continuous MT5 outage.
- DB write failure: logged + retried with backoff (5×, 0.5 s → 5 s). Persistent failure → alert. Plan state in-memory until DB catches up; lost on process restart.

### 5.5 Startup sequence

```
main.start():
  1. init MT5
  2. db_writer.init_db()                       (existing)
  3. snow.db.init_snow_tables()                (new, idempotent)
  4. snow.recovery.reconcile()                 (new; see §3.5)
  5. spawn Sage / Echo / Luna / Rex Monitor    (existing)
  6. spawn Snow thread                          (new)
  7. main loop
```

Reconciliation runs BEFORE the Snow thread starts so the first tick sees a consistent state.

### 5.6 Shutdown

`self.running = False` causes both main loop and Snow loop to exit at next sleep boundary. Snow's ARMED contingencies are left as-is (persisted in DB). No in-flight action should be killed mid-execution because `executor_lock` guarantees atomicity at the call-site.

---

## 6 — Condition Evaluator Architecture

### 6.1 Live vs semantic classification

| Primitive | Data source | Refresh cadence | Notes |
|---|---|---|---|
| `price_above/below/crosses_*` | **LIVE** — `mt5.symbol_info_tick()` | 5 s | Bid for SELL-side conditions, ask for BUY, mid otherwise |
| `rsi`, `macd_histogram` | **LIVE** — Snow recomputes from M1 bars | 5 s on M1-M5; 60 s on H1+ (cached within snow) | See §6.3 |
| `ema_relation` (M1, M5) | LIVE | 5 s | |
| `ema_relation` (H1, H4, D1) | SEMANTIC — `bot._last_agent_data.indicators` | Floki cycle (≤5 min) | Acceptable staleness |
| `atr` | LIVE on M1; SEMANTIC H1+ | 5 s / Floki cycle | |
| `price_at_sr_zone` | SEMANTIC — `bot._last_agent_data.sr_zones` | Floki cycle | S/R changes slowly |
| `price_at_fibonacci` | SEMANTIC — `bot._last_agent_data.h1_fib_levels` | Floki cycle | |
| `profit_pips`, `mfe_reached`, `mae_reached`, `profit_retraced_from_peak` | LIVE — per-plan tracker | 5 s | Snow owns MFE/MAE per-ticket in-memory; seeded on ACTIVE transition |
| `duration_exceeds` | CLOCK — `utc_now() - plan.entered_at` | 5 s | |
| `time_between` | CLOCK | 5 s | |

**Rationale:** price and position-state conditions fundamentally need fresh data (5 s M1 ticks cover the fast domain). Semantic conditions (regime, S/R zones, H4+ indicators) change on multi-minute timescales, so reusing Floki's cycle cache is correct and cheap.

### 6.2 `LiveData` class

```python
# snow/live_data.py

class LiveData:
    def __init__(self, bot):
        self._bot = bot
        self._last_tick = None
        self._m1_bars = []          # last N=120 M1 bars (2h)
        self._indicator_cache = {}  # {(tf, indicator, period): (value, computed_at)}

    def refresh(self) -> None:
        """Called once per Snow tick. Populates tick + M1 bars."""
        with mt5_lock:                       # FLO-348
            self._last_tick = mt5.symbol_info_tick("XAUUSD")
            self._m1_bars = self._fetch_m1_bars(count=120)
        self._indicator_cache.clear()        # force recompute this tick

    def price(self, side="mid") -> float:
        # side ∈ {"bid","ask","mid"}
        ...

    def rsi(self, tf: str, period: int = 14) -> Optional[float]:
        # cached within this tick
        ...

    def macd_histogram(self, tf: str) -> Optional[float]:
        ...
```

For H1+ indicators, `LiveData` first checks `bot._last_agent_data.indicators` (semantic cache); only computes locally if missing.

### 6.3 Indicator recompute budget

- RSI, MACD, ATR on M1: pure-Python (numpy optional) over 120 bars = ~1–2 ms each
- EMAs on M1/M5: same order of magnitude
- Full Snow tick budget target: **≤250 ms** for indicator refresh + evaluation across 10 active plans
- Hot path: `refresh()` is called once per tick, not once per plan. Indicator values cached for the tick.

Measured via Session-A proxy: `flo341_b_authoritative_rerun.py` computed MFE for 26 tickets with `copy_rates_range` calls — total wall-clock ≤10 s including Python + MT5 IO. Projected Snow tick: ≤1 s well within the 5 s window.

### 6.4 Evaluator API

```python
# snow/evaluator.py

class EvalContext:
    live_data:      LiveData
    semantic_cache: SemanticCache
    plan:           Plan
    ticket:         Optional[int]   # only for position-state conditions

def evaluate_conditions(conditions: list[Condition], ctx: EvalContext) -> tuple[bool, dict]:
    """
    Returns (all_true, per_condition_result).
    Short-circuits on first False.
    Any condition returning None (data missing) treated as False.
    """
    results = {}
    all_true = True
    for c in conditions:
        result = _dispatch(c, ctx)   # calls c's .evaluate(ctx)
        results[c.model_fields_set] = result
        if result is not True:       # False or None
            all_true = False
            break
    return all_true, results
```

### 6.5 Missing-data handling

Fail-safe rule: condition evaluates to **False** (not True) if any required data is missing. Exceptions logged with `snow.evaluator.missing_data` tag. Plans stay in their current state until data returns.

Edge case: if `profit_pips` needs position state but the position has just been closed externally, evaluator returns False and the state transition code (§3) marks the plan CLOSED in the same tick.

### 6.6 Stateful conditions — deferred to v2

**Decision: all stateful primitives are OUT of v1 scope.**

Research §2.1 lists `price_crosses_above` and `price_crosses_below`. Both require memory of the previous tick's price relative to the level. After a Snow process restart, a plan with a cross-condition has zero memory of prior state; the condition silently returns False forever until price moves back through the level a second time. That is either a correctness bug or the primitive drops to v2.

**Option (i) — drop crosses from v1 entirely.** Keep only `price_above` / `price_below`. Floki can synthesize cross semantics by watching the condition flip across ticks externally (but this belongs in the prompt, not the evaluator). Simpler, consistent with "punt stateful to v2" stance on OR logic.

**Option (ii) — add `state_cache_json` column + in-memory cache with DB flush on state changes.** ~40 lines of plumbing, but Snow gains genuine stateful conditions.

**Option (iii) — seed state on startup from the current tick ("assume the current side was the prior side").** Zero persistence cost; false-negative on the restart tick only. Good-enough approximation.

**RFC recommendation:** **Option (i).** The 14 primitives listed in §2.5 don't include crosses. Ship v1 clean; revisit with v2. If Floki needs "price reached X and then reversed" semantics, express it as two contingencies: one arms at `price_above(X)` (fires=once), a later one at `price_below(X)` (fires=once with cooldown). Two triggers wire up a cross pattern.

**Implication for §4.1 schema:** no `state_cache_json` column needed in v1. Added in v2 migration if/when we enable stateful primitives.

### 6.7 Priority evaluation within a tick

All contingencies across all active plans are evaluated. Fires are queued with their computed priority (see §8). After evaluation, the priority resolver orders the queue and executes sequentially. One plan can fire multiple contingencies in a single tick; resolver handles priority correctly.

---

## 7 — Action Executor Design

### 7.1 Reuse vs new path

**Decision:** Snow reuses `executor.py`'s existing methods. No new path.

**Rationale:**
- 14 months of production hardening (FLO-282, FLO-291, FLO-338 C.1/C.2/B ghost guards)
- Signal-id gate, phantom detection, deal-history reconciliation
- `record_trade_open` / `record_trade_close` integration in `db_writer.py`
- MT5 connection reuse

Snow's wrapper (`snow/actions.py`) adds:
- `executor_lock` acquisition (FLO-348)
- Plan-level context injection (`comment="snow:PLAN-..."` so trades are traceable)
- Per-contingency guard enforcement (§7.3)
- Snow-specific retry/backoff (3×, 2 s → 8 s)
- Atomic plan-state update in the same transaction as the executor result

### 7.2 Action wrapper signature

```python
# snow/actions.py

def execute_action(plan: Plan, contingency: Contingency, ctx: EvalContext) -> ActionResult:
    """
    Dispatches contingency.action to the right executor call.
    Acquires executor_lock; releases on return.
    Records snow_triggers row with full audit trail.
    Updates plan/contingency state atomically.
    """
    action = contingency.action
    guard_result = _check_guards(plan, contingency, ctx)
    if not guard_result.ok:
        _record_trigger(plan, contingency, "skipped_guard", guard_result.reason)
        return ActionResult.skipped(guard_result.reason)

    with executor_lock:               # FLO-348
        result = _dispatch(action, plan, ctx)

    _record_trigger(plan, contingency, result.status, result.details)
    _update_plan_state_after_action(plan, contingency, result)
    return result
```

### 7.3 Guard-not-at-executor rationale (CTO decision CK-4)

`agent_tools.adjust_trade` had its SL-widening guard and rate limit REMOVED on 2026-04-02 (FLO-200: "Floki has full autonomy"). The executor-layer is deliberately permissive. Re-adding guards at `executor.modify_position` would:
- Silently change Floki's direct-action semantics (Floki currently relies on no guard)
- Couple two independent user agents' behavior
- Make individual trade-level policy impossible (some plans want SL tightening only, some want either direction)

**Snow enforces its guards IN THE CONDITION EVALUATOR / ACTION WRAPPER.** Per-contingency opt-in via `Contingency.guards`:

| Guard | Enforcement point | Effect if triggered |
|---|---|---|
| `only_if_tighter_sl` | action wrapper (before executor call) | skipped_guard event logged; plan continues |
| `cooldown_seconds` | action wrapper (check last fire time for same contingency) | skipped_guard, retry next tick |
| `min_mfe_pips_required` | condition evaluator | contingency remains ARMED |
| `max_adjustments_total` | action wrapper (count snow_triggers rows for this ticket) | contingency → DEACTIVATED |

Default: no guards (`guards=None`). Matches project autonomy posture.

### 7.4 Retry policy

- On MT5 error during execute_trade/modify/close: log, wait 2 s, retry (2nd). Wait 4 s, retry (3rd).
- After 3 failures: mark plan FAILED (entry) or contingency FAILED (management/exit). Alert.
- Exception: "no position found" (ticket already closed) → mark plan CLOSED with outcome fetched from history; not retryable.

### 7.5 Race conditions and idempotence

- **Snow + Floki both adjust SL in same second:** `executor_lock` serializes. Second arrival observes the first's SL and may decide its own adjust is now redundant (the guard `only_if_tighter_sl` filters this naturally).
- **Snow fires close, but position closed externally (e.g., MT5 TP hit):** `close_position` returns "no position" error. Snow treats as success (outcome already achieved) and transitions plan → CLOSED using MT5 deal history for outcome fields.
- **Two Snow contingencies fire same tick with same action against same ticket:** priority resolver serializes. Second action re-reads live position state; often becomes no-op via guard or becomes irrelevant (plan is CLOSING).

### 7.6 Atomic state update

Snow's action execution + state persistence is wrapped:

```python
with snow_db_connection() as conn:
    conn.execute("BEGIN")
    try:
        # 1. call executor (outside conn transaction but inside executor_lock)
        with executor_lock:
            exec_result = executor.<method>(...)
        # 2. now persist in the same conn transaction
        conn.execute("INSERT INTO snow_triggers ...", (...))
        conn.execute("UPDATE snow_plans SET ... WHERE id = ?", (...))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        # MT5 state may be ahead of DB state; next reconcile() cycle repairs
```

If executor succeeds but DB write fails, the next `recovery.reconcile()` run detects the ticket-in-MT5-without-matching-plan-state and resyncs. Not perfectly atomic (there is no distributed transaction), but the recovery path catches any divergence within 5 s.

---

## 8 — Priority Resolution Algorithm

### 8.1 Formula (option-b-refined, category-gap doubling)

```
effective_priority = base + min(base - 1, override * 10)
```

Where `override` = `Contingency.priority` ∈ [1, 10], default 5. The `min(base - 1, override * 10)` clip ensures override CANNOT push a contingency into the next category.

**Rule:** `base_{n+1} ≥ 2 × base_n` (category-gap doubling). This guarantees strict ordering: maxed override in category N cannot exceed min-override in category N+1.

**Bases (v1, anchored to powers of 2 to satisfy doubling):**

| Action type | `base` | effective range [min, max] |
|---|---|---|
| `close_full` | **128** | [138, 228] |
| `close_partial` | **64** | [74, 127] |
| `cancel_plan` | **32** | [42, 63] |
| `adjust_sl` / `adjust_tp` / `move_sl_to_price` / `trail_sl` | **16** | [26, 31] |
| `move_sl_to_breakeven` | **8** | [15, 15] (base-1=7 clip saturates at override≥1) |
| `alert_floki` / `escalate_to_floki` | **4** | [7, 7] |
| `execute_market` | (N/A — entry only, no priority conflict) | — |

Why powers of 2 and not CTO's proposed (100/50/40/25/12/6):
- CTO's proposal did not strictly satisfy the doubling rule at `close_partial (50)` vs `2×cancel_plan (80)` and `cancel_plan (40)` vs `2×adjust (50)`.
- Under CTO's bases + stated formula, `close_partial` min (60) < `cancel_plan` max (79), violating the "close > cancel" invariant.
- Powers of 2 (128, 64, 32, 16, 8, 4) strictly satisfy the rule and preserve CTO's core intent ("override nuances within category, cannot cross categories").
- Override expressiveness is preserved for the categories where it matters (close_full: 10 distinct values across [138, 228]; close_partial: 10 across [74, 127]; cancel_plan: 4 across [42, 63] due to base=32 clipping above override=3).

**Worked examples:**

| Contingency | base | override | `min(base-1, override*10)` | effective |
|---|---|---|---|---|
| close_full default | 128 | 5 | min(127, 50) = 50 | 178 |
| close_full min-priority | 128 | 1 | min(127, 10) = 10 | 138 |
| close_full max-priority | 128 | 10 | min(127, 100) = 100 | 228 |
| close_partial max-priority | 64 | 10 | min(63, 100) = 63 | 127 |
| cancel_plan max-priority | 32 | 10 | min(31, 100) = 31 | 63 |
| adjust_sl max-priority | 16 | 10 | min(15, 100) = 15 | 31 |
| move_sl_to_breakeven default | 8 | 5 | min(7, 50) = 7 | 15 |
| alert_floki default | 4 | 5 | min(3, 50) = 3 | 7 |

Strict category ordering holds: `close_full min (138) > close_partial max (127)`; `close_partial min (74) > cancel_plan max (63)`; etc. Minimum inter-category gap is 11 points (close_full ↔ close_partial boundary).

### 8.2 Why option-b-refined (not option-a or option-c)

Option-b-refined (CTO choice) binds override boost to `min(base-1, override*10)`. Combined with the doubling rule, this provides:

- **Strict category ordering** by construction. No possibility of override flipping categories.
- **Mechanism-enforced, not prompt-enforced.** Floki cannot accidentally (or cleverly) use priority=10 on `adjust_sl` to pre-empt a close. The schema + formula refuse that outcome.
- **Override expressiveness preserved** where it counts. `close_full` retains 10 distinct override values across [138, 228]; `close_partial` retains 10 across [74, 127].

**Domain bounds:** effective priority ∈ [7, 228]. No overflow possible on any reasonable int.

**Option-a (widen bases, keep linear) rejected** because `close_full` maxed-out still ties or under-runs `close_partial` under naïve re-anchoring; fixing it requires bases that deviate so far from the original that override expressiveness becomes vestigial.

**Option-c (accept the violation, use prompt guidance) rejected** as CTO noted: "mechanism should enforce invariant, not prompt."

### 8.3 Tie-breaking

If two contingencies fire the same tick with identical effective_priority:

**Order of tie-break resolution:**
1. First-defined wins within the SAME PLAN (stable: plan's `management`/`exit` list order, management before exit, entry before all).
2. Earliest `created_at` wins ACROSS plans (older plan has priority).
3. Lexicographic plan_id (deterministic fallback).

All three deterministic — no randomness. Ties are rare because override permits 10 distinct values within category and plan-list ordering further stratifies; a genuine tie requires same-category + same-override + same-list-position + same-creation-timestamp.

### 8.4 Cross-plan interference

Two plans can have contingencies against the same ticket (e.g., Floki submits a `submit_plan_to_snow_management` plan to take over managing an existing ticket). Priority resolver orders globally across all fired contingencies from all plans, then execution serialises under `executor_lock`. This is safe but has a subtle interaction with I7 (unique trade_ticket constraint on active plans) — see §11.3 for the "ownership transfer" edge case.

### 8.5 Priority edge cases (CTO §8 mandate)

1. **Identical priority + same cycle + same plan:** first-defined in plan's contingency list wins. Tests: `snow/tests/priority_test.py::test_tie_break_same_plan`.
2. **Identical priority + same cycle + different plans:** older plan (by `created_at`) wins. Tests: `test_tie_break_cross_plan`.
3. **Priority bounds:** validator rejects `priority < 1` or `priority > 10` via Pydantic `Field(ge=1, le=10)`. Effective-priority range [7, 228] cannot overflow.
4. **Emergency blocks:** `emergency.max_loss_pips` check is NOT a contingency; it's a loop-level invariant evaluated every tick regardless. Effective "priority" = ∞ (always executes before any normal contingency). Implementation: checked BEFORE the contingency-evaluation phase.
5. **`fires=once` already-fired contingency:** skipped entirely; not re-entered into priority queue.
6. **Intra-category ordering (e.g., close_full vs close_partial with max overrides):** STRICT — `close_full` min (138) > `close_partial` max (127). A partial-close with override=10 can never beat an override-1 full-close. This is the intended category-gap guarantee.

Tests in §12 exercise all 6 category boundaries: `close_full↔close_partial`, `close_partial↔cancel_plan`, `cancel_plan↔adjust_*`, `adjust_*↔move_sl_to_breakeven`, `move_sl_to_breakeven↔alert_floki`, and emergency-override-all.

---

## 9 — Floki Integration

### 9.1 New tool — `submit_plan_to_snow`

Signature (lives in `agent_tools.py` alongside existing tools):

```python
def submit_plan_to_snow(
    self,
    plan: dict,          # Pydantic-parseable Plan dict (see §2)
) -> dict:
    """
    Submit a contingency plan to Snow for monitoring + execution.

    Returns:
      { "success": bool,
        "plan_id": "PLAN-YYYYMMDD-NNN" | None,
        "validation_errors": list[str] | None }
    """
```

Behaviour:
1. Validate `plan` dict via `snow.validator.validate_plan(plan)` — Pydantic parse + business-rule checks (valid level values within current ±2% of price, SL on correct side of entry, etc.).
2. On validation failure: return `{"success": False, "plan_id": None, "validation_errors": [...]}`. Floki sees errors; can revise + retry.
3. On success: generate plan_id `PLAN-{YYYYMMDD}-{NNN}` (daily counter), INSERT row into `snow_plans` with `status="pending"`, return `{"success": True, "plan_id": "PLAN-...", "validation_errors": None}`.
4. Total latency target: ≤ 200 ms (synchronous; Floki's cycle waits for the result).

Synchronous because Floki needs the plan_id to reference it in subsequent tool calls (`cancel_plan`, `get_plan_status`).

### 9.2 Supporting tools

```python
def cancel_plan(self, plan_id: str, reason: str) -> dict:
    """Transition PENDING plan to CANCELLED. Refuse if plan is ACTIVE."""

def get_plan_status(self, plan_id: str) -> dict:
    """Return full plan row from snow_plans (for Floki self-reference)."""

def list_active_plans(self, ticket: int | None = None) -> list[dict]:
    """Return plans in status IN ('PENDING','TRIGGERED','ACTIVE','CLOSING')."""
```

`cancel_plan` requires `reason` (non-empty string) for audit trail. Write to `snow_triggers` with `contingency_name="_user_cancel"`, `action_type="cancel_plan"`, `execution_result=reason`.

### 9.3 Retained tools — now with mandatory `reason_for_direct_action`

Existing Floki tools stay (Non-negotiable #7) but gain a mandatory audit field:

```python
def close_trade(self, ticket: int, reason_for_direct_action: str) -> dict:
    """Direct close. reason_for_direct_action MUST be non-empty; otherwise refuse."""

def adjust_trade(self, ticket: int, new_sl: float, new_tp: float,
                 reason_for_direct_action: str) -> dict:
    """Direct SL/TP adjust. Same requirement."""

def execute_trade(self, direction: str, lot_size: float, stop_loss: float,
                  take_profit: float, reason_for_direct_action: str, **kwargs) -> dict:
    """Direct market entry. Same requirement."""
```

Enforcement: `agent_tools._log_tool` writes `reason_for_direct_action` to `floki_decision_flags.source_table` (existing column) with prefix `direct_action|`. Dashboard can query/filter. If Floki passes empty string, tool returns `{"success": False, "reason": "reason_for_direct_action required when bypassing Snow; use submit_plan_to_snow for default path"}`.

**This is a breaking change to three Floki tools.** Rule 14 applies to the implementation PR. All existing callers inside `ai_agent.py`/`agent_tools.py` tests must pass a `reason_for_direct_action` string.

### 9.4 Prompt scaffolding

Drafted for `agent_prompts.py` — not final text; prose-level only. Three new blocks added to Floki's system prompt:

**Block 1 — plan-first thinking** (research §8.1 paraphrased):

> You DO NOT execute trades immediately. When you identify an opportunity, submit a PLAN to Snow with: (1) entry conditions — when the trade should open, (2) management contingencies — how the trade adapts during its life, (3) exit contingencies — what scenarios close it. Snow watches markets every 5 seconds and fires when conditions match. You are the scenario architect.

**Block 2 — contingency design heuristics** (research §8.2):

> A good plan has 2-5 exit contingencies covering: rejection (price fails at anticipated resistance), target (price reaches profit zone), time (price stalls too long), invalidation (macro context changes), volatility (ATR explodes). Don't write 20 contingencies. Write the 5 that capture 80% of scenarios.

**Block 3 — when to bypass Snow** (research §8.3 + CTO audit-trail mandate):

> PREFER `submit_plan_to_snow` for all entries.
> USE DIRECT ACTION ONLY IF: (1) breaking news demands immediate close (news too fast for plan), (2) Snow plan is clearly wrong given new info (cancel plan + direct close), (3) emergency system issue. When using direct action, `reason_for_direct_action` MUST explain what changed and why the plan path failed. If you can't justify it in writing, use a plan.

**Block 4 — guards are opt-in**:

> Per-contingency `guards` are optional. Default (no guards) means Snow executes the action whenever conditions fire. Opt into guards when you explicitly want: `only_if_tighter_sl` (never loosen SL), `cooldown_seconds` (throttle repeated fires), `min_mfe_pips_required` (only fire after minimum favorable move), `max_adjustments_total` (cap total adjusts per ticket).

### 9.5 Migration — what changes in `ai_agent.py`

- Add 4 new tool registrations (`submit_plan_to_snow`, `cancel_plan`, `get_plan_status`, `list_active_plans`).
- Add 3 modified tool signatures (existing `close_trade`, `adjust_trade`, `execute_trade` with `reason_for_direct_action`).
- Add prompt blocks 1-4 to Floki's system prompt.
- No changes to Rex/Sage/Echo/Luna — they don't submit trades.
- No changes to `rex_validator.py`'s Bull/Bear debate — they still produce direction signals; Floki translates those into plans.

### 9.6 Backward compatibility

During shadow mode (§13) Floki uses BOTH new tools AND existing direct-action tools. Post-cutover, `execute_trade` (direct market) is removed from the tool registry but the implementation remains for emergency/debug use. `close_trade` and `adjust_trade` stay permanently (research §3.4 meta-actions require them).

---

## 10 — Dashboard Integration

### 10.1 New Trade Room card (not extension)

Per CTO: new card, not an extension of an existing panel. Matches existing card patterns in `dashboard/templates/trade_room.html`.

**Card: "Snow Plans"** — placed alongside existing "Trades Today", "Position", "Floki Decisions" cards.

**Layout sketch:**

```
┌─────────── Snow Plans ───────────┐
│ Active (2)  │  Recent (last 24h)   │
│─────────────────────────────────── │
│                                    │
│ ▸ PLAN-20260423-007  [PENDING]    │
│   SELL 0.02 @ 4730  thesis...      │
│   Entry conditions: 2/3 met   ███░ │
│   Expires in: 3h 42m               │
│                                    │
│ ▸ PLAN-20260423-005  [ACTIVE]      │
│   ticket #1608729539  BUY 0.02     │
│   Current P/L: +$7.40 (+37p)       │
│   Next contingency: trail@MFE50    │
│   2 exit + 1 management armed      │
│                                    │
│ [Recent Triggers]                  │
│ 14:32  PLAN-006: lock_profit fired │
│         SL 4725 → 4727.30          │
│ 14:18  PLAN-005: entered @ 4728    │
│                                    │
└────────────────────────────────────┘
```

Click through to per-plan detail page (`/snow/plans/{id}`) with: full plan JSON (collapsed), state transitions, conditions timeline, actual vs expected outcome.

### 10.2 Backend — new FastAPI endpoints in `dashboard/server.py`

```python
@app.get("/api/snow/plans")
async def list_plans(status: str = "active", limit: int = 50) -> list[dict]:
    """status ∈ {active, all, pending, closed} — server filters."""

@app.get("/api/snow/plans/{plan_id}")
async def get_plan(plan_id: str) -> dict:
    """Full plan + recent triggers for this plan."""

@app.get("/api/snow/triggers")
async def list_triggers(hours: int = 24, limit: int = 100) -> list[dict]:
    """Recent snow_triggers rows, paginated."""
```

Each endpoint reads from `history.db` via `snow.db` helpers (same connection pattern as existing endpoints). No new websockets in v1 — dashboard polls every 5 s via existing state refresh.

### 10.3 Frontend

`dashboard/static/app.js` — add `updateSnowPlansCard(data)` function called on each state refresh. Reuses existing time-formatting helpers from `tz.js` (`displayTime()`, `displayAge()`) to avoid duplicate TZ logic (per CLAUDE.md Rule 22).

No new CSS framework; extends existing `style.css` card class. Condition-progress bar is a basic `<progress>` element styled to match.

### 10.4 Audit & history view

Out of v1 scope — v2 adds:
- All plans ever created (filterable by status / outcome)
- Outcome analysis: Snow's firing vs Floki's hypothetical direct action, with pip / $ deltas
- Shadow-mode parity report (only used during shadow phase, then hidden)

v1 provides the API endpoints that make this view possible; the UI is punted.

---

## 11 — Edge Case Catalog (extends research §6)

### 11.1 `executor_lock` contention

**Scenario:** Floki mid-cycle holds `executor_lock` while calling `executor.modify_position`; Snow's exit contingency fires at the same moment.

**Resolution:** Snow blocks on `executor_lock.acquire(timeout=3.0)`.
- Lock is scoped narrowly (per §7.1: "held ONLY during the MT5 call itself") so typical hold time is 100–500 ms.
- 3-second Snow timeout is ~10× expected hold. Reaching timeout implies pathological executor behaviour (MT5 hang) — Snow then logs + alerts + marks contingency `FAILED` and moves on.

**Test:** `snow/tests/executor_lock_contention_test.py` mocks a slow executor and verifies Snow respects the timeout + fails gracefully.

### 11.2 Plan expires while TRIGGERED (broker call in flight)

**Scenario:** Plan's `expires_at < now` triggers during the same cycle that Snow is calling `executor.execute_trade`. Race.

**Resolution:** Expiry check happens BEFORE `execute_trade`. Once a plan transitions to TRIGGERED, expiry is IGNORED until TRIGGERED resolves (either to ACTIVE or FAILED). Expiry field is preserved; if the trade succeeds and the plan enters ACTIVE status after `expires_at`, the plan enters a soft-expired state: existing contingencies continue firing, but no NEW entry is possible (the trade is already in the market). The operator-visible plan status shows `ACTIVE` with a `expired_but_holding` flag.

**Test:** `snow/tests/lifecycle_test.py::test_triggered_survives_expiry`.

### 11.3 Two plans try to own the same ticket (I7 violation)

**Scenario:** Floki submits plan P1 with entry conditions that fire. Then submits plan P2 with `trade_ticket=<P1's ticket>` intending to take over management.

**Resolution:** Validator rejects P2 at submission time with error "a live plan already owns this ticket; use `cancel_plan(P1)` first OR use `transfer_plan(P1.id, new_contingencies=...)`". The `transfer_plan` tool is v2 — v1 just rejects the second submission.

I7 index: `UNIQUE(trade_ticket) WHERE status IN ('triggered','active','closing')`. The SQLite UNIQUE WHERE ensures only one live plan per ticket.

**Test:** `snow/tests/validator_test.py::test_reject_duplicate_ticket_ownership`.

### 11.4 Plan in FIRED state with no matching `snow_triggers` row

**Scenario:** Action executed, but DB write for `snow_triggers` failed (§7.6 atomic update race).

**Resolution:** `recovery.reconcile()` detects this on startup: contingency in FIRED state but no trigger row. Reads MT5 deal history for the `trade_ticket`, infers the action type from the deal (close vs modify), synthesizes a trigger row with `execution_status="recovered"`. Alert emitted for manual review.

If reconciliation cannot decide (multiple deals, ambiguous match), plan is marked `status="active"` but contingency `state="FAILED"` with alert. Human intervention required.

**Test:** `snow/tests/recovery_test.py::test_missing_trigger_row_infers_from_mt5`.

### 11.5 Emergency `max_loss_pips` during contingency evaluation

**Scenario:** Mid-tick, emergency threshold crossed (e.g., gap-down blows past SL). Contingencies still have yet to fire.

**Resolution:** Emergency check runs FIRST in each tick, before any contingency evaluation. If `max_loss_pips` triggered: immediately close full via `executor.close_position`, mark plan `status="closed"`, record `snow_triggers` row with `contingency_name="_emergency_max_loss"`. No other contingencies evaluate that tick.

Rationale: emergency is a ceiling, not a preference. Running it first costs ~50 ms per tick but prevents pathological compound-adjustment scenarios.

**Test:** `snow/tests/emergency_test.py::test_max_loss_preempts_contingencies`.

### 11.6 Wide spread at entry

**Scenario:** Entry conditions fire, but current spread exceeds `config.MAX_SPREAD_PIPS` (existing guard in `executor.execute_trade`).

**Resolution:** Snow respects the existing executor guard. Response on `executor.execute_trade` will indicate spread-too-wide. Snow retries every 5 s for up to `config.SPREAD_RETRY_INTERVAL_SECONDS × 12` = 60 s (matches existing `wait_for_acceptable_spread` behaviour in main.py). After 60 s: mark plan `FAILED`, alert Floki.

**Decision:** do NOT extend retry indefinitely. A 60 s tolerance preserves Floki's original thesis; beyond that the setup is likely stale.

**Test:** `snow/tests/wide_spread_test.py`.

### 11.7 MT5 disconnect mid-tick

**Scenario:** Snow refreshes `live_data`, but MT5 API returns None or raises.

**Resolution:** `live_data.refresh()` catches; subsequent primitives see `live_data.price() is None` and evaluate their conditions as False (fail-safe, §6.5). Plans stay in current state. Alert emitted after 2 min of continuous MT5 outage (matches existing `is_ea_online` debounce).

Already-in-flight MT5 calls under `executor_lock` may succeed or fail per MT5's own timeout; those are handled by §11.1 / §7.4.

**Test:** `snow/tests/mt5_outage_test.py` mocks disconnect and verifies no-fire behavior.

### 11.8 Floki submits plan referencing stale price

**Scenario:** Floki's analysis ran 4 minutes ago at price 4720. By the time Floki calls `submit_plan_to_snow`, price is 4735 and Floki's entry condition `price_above(4730)` already TRUE. Does Snow fire immediately on next tick?

**Resolution:** **Yes, by design.** If entry conditions are true at the first tick after submission, Snow fires. This is NOT a bug; it's the intended behavior of an event-driven executor. Floki's prompt (§9.4 Block 2) will emphasize: "conditions should be forward-looking from the moment of submission." If Floki wants to express "fire ONLY on a future crossing", they use two sequential contingencies per §6.6.

**Validator-level warning (not rejection):** if entry conditions evaluate true at submission time using `bot._last_agent_data` snapshot, include a warning in the `submit_plan_to_snow` response: `"warning": "entry conditions already satisfied at submission; Snow will fire on first tick"`. Floki can choose to proceed or cancel.

**Test:** `snow/tests/validator_test.py::test_warn_immediate_entry`.

### 11.9 Position closed externally (SL hit outside Snow)

**Scenario:** Plan is ACTIVE, MT5 hits the broker-side SL. Snow didn't fire; the EA or MT5 closed the position.

**Resolution:** Next Snow tick detects ticket missing from `executor.get_open_positions()`. Snow queries `mt5.history_deals_get(position=ticket)` for the close deal, reads exit price + profit, marks plan `CLOSED` with outcome populated. Writes `snow_triggers` row with `contingency_name="_external_close"`, `action_type="noop"`, `execution_status="external"`.

Not a bug — this is the clean path when broker-side SL/TP fires. Tracked separately from Snow-side closes for analytics.

**Test:** `snow/tests/external_close_test.py`.

### 11.10 Contingency guard blocks repeatedly (never fires)

**Scenario:** `cooldown_seconds=60` and conditions stay true for 5 min. Each tick evaluates true but guard blocks.

**Resolution:** After N failed fires (default 12 = 1 min worth of attempts), Snow logs `"guard_suspended"` event and SKIPS evaluation of this contingency for remainder of plan lifetime. Plan stays ACTIVE; other contingencies continue. Operator-visible state transition logged.

Rationale: if a guard is perpetually true, either the plan's cooldown is misconfigured or the conditions are wrong. Surfacing this prevents log spam and informs post-mortem.

**Test:** `snow/tests/guard_test.py::test_guard_suspension_after_repeated_block`.

---

## 12 — Testing Strategy

### 12.1 Framework

**pytest scoped to `snow/tests/` only** (per CTO CK-3). No impact on existing standalone-script tests.

- `requirements-dev.txt` adds `pytest>=7.4`, `pytest-asyncio>=0.21` (if needed for any async edges), `freezegun>=1.3` (clock mocking for time-based conditions).
- Invocation: `python -m pytest snow/tests -v` from repo root. CI integration deferred to v2 (no CI pipeline in v1 scope).

### 12.2 Test categories

**Unit tests (per primitive evaluator) — ~56 tests:**
- 14 primitives × 4 cases (true / false / boundary / data-missing) = 56.
- Each test constructs an `EvalContext` with mocked `live_data` + `semantic_cache`, calls the primitive, asserts bool.
- File layout: one test file per primitive-family (e.g., `price_test.py`, `rsi_test.py`, `position_state_test.py`).

**Unit tests (priority resolution) — ~8 tests:**
- 6 category-boundary tests (one per adjacent pair)
- 1 tie-break test
- 1 emergency-preempts test

**Unit tests (validator) — ~10 tests:**
- 6 reject-cases (malformed condition, missing field, out-of-range override, etc.)
- 4 accept-edge-cases (min/max values, empty management, empty exit, 10 contingencies)

**Unit tests (state machine) — ~6 tests:**
- Transition matrix coverage (§3.2 transitions)

**Integration tests — ~10 scenarios:**
- `test_entry_then_exit_full_lifecycle` — plan submit → entry fires → management trails → exit fires → CLOSED
- `test_cancel_before_entry`
- `test_expire_before_entry`
- `test_emergency_max_loss_overrides`
- `test_two_contingencies_fire_same_tick_priority_resolves`
- `test_executor_lock_contention` (from §11.1)
- `test_mt5_outage_keeps_plan_active` (§11.7)
- `test_recovery_from_crashed_mid_execution` (§3.5)
- `test_guard_suspension_after_repeated_block` (§11.10)
- `test_external_close_path` (§11.9)

**Stress tests — 3 scenarios:**
- 10 plans × 5 contingencies × continuous 1-hour run: measure tick latency, CPU, memory
- MT5 API latency injection: 100 ms per call; verify ≤ 5 s cycle budget holds
- SQLite concurrency: 10 concurrent writes, verify WAL doesn't deadlock

### 12.3 Test infrastructure

- `snow/tests/fixtures/` — reusable `Plan` dicts for known scenarios
- `snow/tests/conftest.py` — pytest fixtures for mocked `live_data` + `executor` + in-memory sqlite
- MT5 mocking: a `FakeMT5` class replicates `mt5.symbol_info_tick` + `mt5.copy_rates_range` + `mt5.history_deals_get` surface used by Snow. Bar data loaded from CSV fixtures representing known market scenarios.

### 12.4 Gating

Each implementation phase (§15) requires:
- 100% of its own unit tests passing
- 0 regressions in prior phases' tests
- Manual smoke-test dry-run with a single dummy plan before PR merge

---

## 13 — Shadow Mode Design

### 13.1 Three candidate approaches

**Approach 1 — Parallel decision log (log-intent, don't-execute).** Snow loop runs in full; when an action WOULD fire, it writes a `snow_triggers` row with `execution_status="shadow_intent"` and skips the executor call. Floki continues to direct-execute. Comparison: did Snow's would-be action match Floki's actual action?

**Approach 2 — Staggered A/B (half trades go to Snow, half to direct).** Rejected per CTO (1-day noise, statistical power insufficient for 2-week comparison).

**Approach 3 — Floki tags trades "Snow-managed".** Floki explicitly chooses per-trade which path. Flagged trades go through Snow end-to-end; unflagged go through direct actions. Compare outcomes between tagged and untagged populations.

### 13.2 RFC recommendation: Approach 3 (with Approach 1 as a passive layer)

CTO flagged Approach 3 as preferred. Combined with Approach 1 as a passive layer, this gives:

- **Primary signal (Approach 3):** Floki submits SOME plans real-fires, SOME trades direct-fires. Outcomes directly comparable within the shadow window.
- **Secondary signal (Approach 1):** Even for direct-fired trades, Snow submits "what it would have done" (shadow_intent rows). This gives a cheap Nth-trade-counterfactual without Floki changing behavior.

Rationale:
- Approach 3 alone gives real outcomes but relies on Floki choosing to experiment. Floki's choices may be biased (easy trades → plans; hard trades → direct).
- Approach 1 alone gives coverage but the "would have" comparison is counterfactual and noisier.
- Together: real-outcome data + full-coverage counterfactual, interpretable separately.

### 13.3 Shadow-mode implementation details

**Snow tool-level flag:** `submit_plan_to_snow(plan, shadow_mode=False)` default. When `shadow_mode=True`, Snow accepts the plan but never calls the real executor — actions fire as `shadow_intent` rows only.

During shadow phase:
- Floki's system prompt (temporarily) includes: "When you want to submit a plan, occasionally submit it WITH the flag `shadow_mode=true` so Snow observes without acting. Continue using direct actions normally."
- Default shadow-mode target: 30-50 % of entries go through `shadow_mode=True` plans; 30-50 % through `shadow_mode=False` (real plans); balance direct-action.

**For direct-action trades (Approach 1 passive layer):**
- At the moment Floki calls `execute_trade` directly, `agent_tools.py` ALSO calls `snow.shadow_log_hypothesis()` which creates a synthetic Plan based on the trade params + a default SL/TP contingency set. This plan is persisted but never runs through the evaluator — it's just a "what Snow would have done" baseline.

### 13.4 Success criteria for cutover gate

After 14 consecutive days of operation (CTO guidance: "1-2 weeks minimum"):

| Metric | Threshold | Measurement |
|---|---|---|
| **Safety — zero ghost/phantom trades from Snow path** | 0 | Cross-check `snow_plans` trade_ticket vs MT5 deal history for orphans |
| **Safety — zero executor deadlocks** | 0 | Log grep for `executor_lock` timeout events; must be 0 production hits |
| **Correctness — condition evaluator accuracy** | ≥ 99% | Sample 100 evaluator runs; manually verify against live MT5 state at the timestamp |
| **Performance — tick latency p95** | ≤ 1000 ms | Histogram of `tick_duration_ms` metric |
| **Outcome parity — Snow-executed vs direct-executed $/trade** | Snow ≥ direct − $1 | Compare median $ per trade across the two populations (50+ trades each). **First-order signal with wide CI; tighten in v1.5 with accumulated volume.** |
| **Outcome quality — SL-adjustment churn per trade** | ≤ 2.0 | Count `snow_triggers` rows per Snow plan vs ticket 1605010600's baseline of 9 adjusts |
| **Floki preference signal** | Floki opts in to >50% of trades via plans | Ratio of Floki-initiated `submit_plan_to_snow(shadow_mode=False)` vs direct calls |

At least **5 of 7** must be met before cutover, with **all 2 safety criteria** non-negotiable.

### 13.5 Cutover procedure

When gate passes:
1. Merge cutover PR that removes `execute_trade` (direct market) from Floki's tool registry.
2. Update prompt to remove the "shadow_mode=True" guidance; all plans now real.
3. Retain emergency `close_trade` and `adjust_trade` direct-action paths (research §3.4); they always required `reason_for_direct_action`.
4. Monitor for 7 days post-cutover with daily dashboard review.
5. If critical issue, rollback = re-add `execute_trade` to tool registry and temporarily reduce Snow plan coverage.

### 13.6 What I DON'T know yet about shadow mode

- Whether Floki will self-select "easy" trades into plans → biasing results. Mitigation: require at least 3 shadow plans per day minimum via prompt.
- Whether Snow's would-be-action matches Floki's actual-action closely enough that the comparison is meaningful. If Snow and Floki mostly agree, shadow mode tells us little; if they disagree significantly, interpretability gets murky.
- Whether 14 days gives enough volume. Current rate: ~15-20 trades/day. 14 days × 15 trades = ~210 trades; split 3 ways gives ~70 per cell. Statistically marginal.

These go into §14 risks.

---

## 14 — Risks, Unknowns, Open Questions

### 14.1 Known unknowns — things I cannot answer without experimentation

1. **MT5 Python API thread-safety under sustained concurrent load.** Docs are silent; `executor.py`'s offset-cache hack (TTL to avoid `symbol_info_tick` hammering) suggests prior concern. FLO-348's `mt5_lock` covers worst case, but we don't know if there are rarer race conditions deeper in the MT5 Python binding. **Mitigation:** stress test (§12.2); live monitoring during shadow.
2. **Stale semantic-cache fidelity.** Floki's cycle cadence varies 30 s to 5 min depending on activity. A condition like `regime_matches(TRENDING_BEARISH)` may reference a regime computed 4 min ago — possibly wrong now. **Mitigation:** flag `semantic_stale_seconds` on each evaluation; if > 180 s, emit warning in `snow_evaluations`.
3. **Snow's indicator recompute drift vs central_brain's.** If Snow's RSI computation produces 62.4 and Floki saw 61.9 in their last cycle, contingencies written against "RSI > 62" will fire at different moments. Close enough for most conditions but not all. **Mitigation:** reuse central_brain's exact algorithms; port formulas byte-for-byte.
4. **Floki prompt adoption rate.** Will Floki actually use plans consistently, or revert to direct-action under pressure? Research §8.3 addresses this in prompt; empirical test is shadow mode.
5. **Cancel-plan race.** Floki calls `cancel_plan(P)` just as Snow's tick is processing P's entry contingency. Is the cancellation observed before entry fires? **Mitigation:** `cancel_plan` writes to DB under `_snow_lock` (Snow's own instance lock); Snow reads status fresh each tick. Narrow but non-zero race window.
6. **Schema evolution path.** v1 plans stored as JSON in `snow_plans.plan_json`. Adding a field in v2 requires migration. **Mitigation:** `schema_version` column allows dual-version coexistence during transition.

### 14.2 Things I explicitly don't know from outside the codebase

1. Whether the MT5 terminal's `history_deals_get` behaves identically from multiple concurrent callers (used by executor + Snow + monitor). Have not tested.
2. Whether `dashboard/server.py` has auth (beyond default) — implications for Snow endpoints. If exposed publicly, `/api/snow/plans` reveals trading strategy.
3. Whether there are any hidden global state assumptions in `agent_data_builder.build_data_package()` that break if Snow reads `bot._last_agent_data` while Floki is mid-rebuilding it. **Short-term mitigation:** read with a snapshot copy; long-term: add a `_last_agent_data_lock` if contention becomes real.

### 14.3 Answers to CTO's §14 questions (from initial RFC request)

**Q1: Existing threading/subprocess patterns Snow should follow or avoid?**
- **Follow:** `threading.Thread(daemon=True)` + instance-level `threading.Lock` for mutual exclusion (pattern: Sage/Echo/Luna/Rex Monitor spawn; `_proactive_lock` / `_fast_decision_lock` non-blocking acquire).
- **Avoid:** `asyncio` (only used in `ai_agent.py` for OpenAI streaming; not a codebase-wide pattern); `subprocess.Popen` (used for `deal_resolver` one-shots, overkill for Snow's continuous loop).
- **Caveat:** `_mt5_offset_cache_ex` in `executor.py:15-29` is a TTL-cache to avoid hammering `symbol_info_tick`; Snow reuses it (calls `_mt5_server_offset()` which honors the same cache).

**Q2: Pain points with `executor.py` influencing Snow's integration?**
- No internal thread lock on public methods. **Fixed by FLO-348** (CK-1).
- Broker offset cache is TTL-based with 1 h validity. Fine for Snow's 5 s cadence — Snow just reads through it.
- `execute_trade` has complex branching for EA vs direct path (FLO-282, FLO-291, FLO-338 C.1/C.2/B ghost guards). All that logic is encapsulated inside `execute_trade` and is thread-safe once FLO-348 wraps the entry point.
- `modify_position` / `close_position` are simpler and inherit the same lock.

**Q3: MT5 API quirks Snow must handle?**
- Naive datetime passed to `copy_rates_range` is interpreted as **Lisbon local (UTC+2 during DST)**, not UTC or broker. Session-A finding. Snow's `live_data.py` must compensate using `now + broker_shift` when querying recent bars.
- `tick.time` / `deal.time` / `rates['time']` all use broker-wall-clock-as-epoch; subtract `broker_offset` (3 h) to get true UTC.
- `history_deals_get(position=ticket)` and `history_orders_get(ticket=ticket)` return `TradeDeal` / `TradeOrder` tuples with broker-time epochs.
- Disconnects are rare but do happen during broker maintenance windows. `live_data.refresh()` catches gracefully.
- Symbol data gap on weekend crossings — known issue; evaluator skips cleanly.

**Q4: Prior architectural work that explored similar territory?**
- FLO-211 Rex Monitor (proactive 30-min scan, deterministic classifier): a precedent for daemon-thread passive agent, no LLM, no execution. Snow extends this pattern to execution + 5 s cadence + plans.
- FLO-261 (forward-simulation shadow): a simpler "what if" computation layer; similar in spirit to Approach 1 shadow mode. Architectural patterns inform §13.
- FLO-338 ghost-trade ghost guards: the reconciliation + dual-writer pattern (C.1 / C.2 / B) is a model for how Snow's post-action DB updates coexist with multiple writers.
- No abandoned branches or cancelled tickets explored Snow-equivalent territory that I found. This is greenfield.

### 14.4 Flagged for CTO as NOT-IN-SCOPE (separate tickets)

1. **FLO-348** (prerequisite): `executor_lock` + `mt5_lock` + call-site audit + regression tests. 1-2 sessions.
2. **FLO-349** (proposed; post-Snow): Simba coexistence audit. After 2-4 weeks of Snow production data, evaluate whether Simba's entry-condition watch remains useful or should be deprecated for plan-submitted paths.
3. **FLO-350** (proposed; post-Snow): dashboard "Audit & history" view (§10.4 punt). Becomes relevant once months of plan data accumulate.
4. **v2 scope:** OR logic (`conditions_or` groups), stateful primitives (crosses), MTF composites, volume conditions, advanced patterns (FVG/OB), tick-level conditions, plan-transfer tool.

---

## 15 — File Structure and Implementation Plan

### 15.1 Directory layout

```
snow/                               ◄── NEW package
├── __init__.py
├── schema.py                       ◄── Pydantic Plan/Contingency/Condition/Action
├── validator.py                    ◄── Business-rule checks beyond Pydantic
├── snow_loop.py                    ◄── SnowLoop + run_forever entry point
├── live_data.py                    ◄── LiveData class; fresh MT5 + indicators
├── semantic_cache.py               ◄── Adapter over bot._last_agent_data
├── evaluator/
│   ├── __init__.py
│   ├── dispatch.py                 ◄── Type → evaluator fn mapping
│   ├── price.py                    ◄── price_above, price_below
│   ├── indicator.py                ◄── rsi, macd, ema_relation, atr
│   ├── structural.py               ◄── price_at_sr_zone, price_at_fibonacci
│   ├── position.py                 ◄── profit_pips, mfe, mae, retrace, duration
│   └── time_.py                    ◄── time_between
├── actions.py                      ◄── Action → executor dispatch + guards
├── priority.py                     ◄── effective_priority formula + resolver
├── db.py                           ◄── CRUD for snow_plans/triggers/evaluations
├── recovery.py                     ◄── Startup reconciliation
├── shadow.py                       ◄── Shadow mode intents + logging
└── tests/
    ├── __init__.py
    ├── conftest.py                 ◄── Pytest fixtures (FakeMT5, FakeBot, etc.)
    ├── fixtures/
    │   ├── plans/                  ◄── Sample JSON plans
    │   └── bars/                   ◄── CSV M1 bar data for scenarios
    ├── schema_test.py
    ├── validator_test.py
    ├── evaluator/                  ◄── Per-primitive unit tests
    │   ├── price_test.py
    │   ├── indicator_test.py
    │   ├── structural_test.py
    │   ├── position_test.py
    │   └── time_test.py
    ├── priority_test.py
    ├── lifecycle_test.py
    ├── actions_test.py
    ├── recovery_test.py
    ├── integration/
    │   ├── full_lifecycle_test.py
    │   ├── emergency_test.py
    │   ├── executor_lock_contention_test.py
    │   ├── external_close_test.py
    │   ├── guard_test.py
    │   ├── mt5_outage_test.py
    │   └── wide_spread_test.py
    └── stress/
        ├── ten_plan_throughput_test.py
        ├── mt5_latency_injection_test.py
        └── sqlite_concurrency_test.py

Files OUTSIDE snow/ that change:
  main.py                           ◄── Thread spawn + reconcile call
  agent_tools.py                    ◄── New tools + reason_for_direct_action
  agent_prompts.py                  ◄── Block 1-4 added to Floki prompt
  dashboard/server.py               ◄── 3 new /api/snow/* endpoints
  dashboard/templates/trade_room.html  ◄── New Snow card
  dashboard/static/app.js           ◄── updateSnowPlansCard fn
  dashboard/static/style.css        ◄── (minimal; extend card class)
  requirements.txt                  ◄── Add pydantic (may already be transitive)
  requirements-dev.txt              ◄── Add pytest, pytest-asyncio, freezegun

Files ASSUMED UNTOUCHED by Snow:
  executor.py (modified only by FLO-348)
  central_brain.py
  monitor.py
  agent_monitor.py (Simba — coexists; see §9)
  rex_validator.py
  sage_auditor.py
  echo_sentinel.py
  luna_analyst.py
  db_writer.py (Snow uses its own snow/db.py module, separate tables)
```

### 15.2 Task dependency graph

```
Phase 0: FLO-348 (prerequisite, separate ticket)
  → executor_lock + mt5_lock + regression tests
  → BLOCKS all Phase 1+ below

Phase 1: Schema + validator
  ├── snow/schema.py
  ├── snow/validator.py
  └── snow/tests/schema_test.py + validator_test.py
  DELIVERABLE: pytest green; no integration

Phase 2: DB layer
  ├── snow/db.py (snow_plans/triggers/evaluations tables + CRUD)
  └── snow/tests/db_test.py (in-memory SQLite)
  DEPENDS ON: Phase 1
  DELIVERABLE: pytest green; schema can round-trip through DB

Phase 3: Evaluator primitives
  ├── snow/live_data.py (mocked MT5 initially)
  ├── snow/semantic_cache.py (mocked bot)
  ├── snow/evaluator/ (all 14 primitives)
  └── snow/tests/evaluator/*
  DEPENDS ON: Phase 1
  DELIVERABLE: 56+ unit tests green

Phase 4: Snow main loop (DRY RUN)
  ├── snow/snow_loop.py
  ├── snow/priority.py
  ├── snow/actions.py (STUB — logs but doesn't execute)
  ├── snow/recovery.py
  └── snow/tests/lifecycle_test.py + priority_test.py
  DEPENDS ON: Phase 2, 3
  DELIVERABLE: loop runs, state transitions work, NO MT5 execution

Phase 4.5: DRY RUN telemetry gate (48h production demo validation)
  ├── Deploy Phase 4 build on DEMO account with Floki submitting real plans
  ├── Snow evaluates + logs intended actions to snow_triggers (execution_status="dry_run_intent")
  ├── NO executor calls; zero live money at risk
  ├── 48-hour continuous run; observe:
  │     - tick latency distribution (p50, p95, p99)
  │     - plan lifecycle correctness (transitions, expiries, recovery)
  │     - condition evaluator stability (no crashes, no stuck plans)
  │     - MT5 API behavior under Snow's cadence
  │     - memory stability (no leak over 48h)
  │     - DB growth rate (snow_triggers / snow_evaluations row counts)
  └── Go/no-go review before Phase 5 begins
  DEPENDS ON: Phase 4
  DELIVERABLE: signed-off telemetry report + go/no-go call

Phase 5: Executor integration
  ├── snow/actions.py (REAL executor calls under executor_lock)
  └── snow/tests/actions_test.py + executor_lock_contention_test.py
  DEPENDS ON: Phase 4, FLO-348
  DELIVERABLE: Snow can execute against DRY_RUN MT5 executor

Phase 6: Floki tool integration
  ├── agent_tools.py (new + modified tools)
  ├── agent_prompts.py (prompt blocks)
  └── integration test with real Floki cycle
  DEPENDS ON: Phase 5
  DELIVERABLE: Floki can submit a plan; Snow evaluates + executes

Phase 7: Dashboard
  ├── dashboard/server.py (3 endpoints)
  ├── dashboard/templates/trade_room.html (Snow card)
  └── dashboard/static/app.js (updateSnowPlansCard)
  DEPENDS ON: Phase 6
  DELIVERABLE: UI shows live plans; no behavior change

Phase 8: Shadow mode
  ├── snow/shadow.py (intent logging)
  ├── agent_prompts.py (shadow-mode prompt addendum)
  └── 14-day observation period + metrics review
  DEPENDS ON: Phase 7
  DELIVERABLE: shadow-mode signals collected; cutover gate decision

Phase 9: Cutover
  └── Remove execute_trade (direct) from Floki tools; monitor 7 days
  DEPENDS ON: Phase 8 gate met
  DELIVERABLE: Snow is the default entry path
```

### 15.3 Rule-14 / Rule-18 checklist per phase

| Phase | Files touched | Trade-critical? | Rule-18 skill | Rule-14 review? |
|---|---|---|---|---|
| 0 (FLO-348) | executor.py | YES | senior-backend + senior-architect | YES |
| 1 | snow/schema.py, validator.py | no | senior-backend | standard |
| 2 | snow/db.py | no | database-designer | standard |
| 3 | snow/evaluator/*, live_data.py | mostly no (indicators only) | senior-backend + senior-ml-engineer | standard |
| 4 | snow/snow_loop.py | YES (loop touches plan execution) | senior-architect + agent-designer | YES |
| 4.5 | no code; DEMO deployment only | YES (production-facing observation) | senior-architect | gate review by CTO |
| 5 | snow/actions.py | YES (calls executor) | senior-backend + senior-architect | YES |
| 6 | agent_tools.py + agent_prompts.py | YES (Floki tool changes) | senior-prompt-engineer + senior-backend | YES |
| 7 | dashboard/* | no | senior-frontend (via /distinctive-frontend) + senior-backend | standard |
| 8 | snow/shadow.py + prompt | YES (production observation) | senior-architect | YES |
| 9 | agent_tools.py cutover | YES | senior-backend + senior-architect | YES |

---

## 16 — Timeline Estimate (phase-level, no calendar dates)

Estimates are focused-hour ranges assuming a single-contributor implementation with standard iteration cycles. No calendar dates per CTO guidance.

| Phase | Focused hours (low) | Focused hours (high) | Notes |
|---|---|---|---|
| 0 — FLO-348 (prerequisite) | 4 | 8 | executor_lock + mt5_lock + call-site audit + regression tests |
| 1 — Schema + validator | 6 | 10 | 14 primitives × validation; Pydantic discriminated unions take care |
| 2 — DB layer | 2 | 4 | 3 tables + CRUD; small |
| 3 — Evaluator primitives | 10 | 16 | 14 primitives + LiveData indicator recompute + 56 unit tests |
| 4 — Snow main loop (DRY RUN) | 8 | 14 | state machine + priority resolver + recovery |
| 4.5 — DRY RUN telemetry gate | 1 | 2 (code) + 48h demo observation | No new code; deployment + telemetry collection; CTO go/no-go |
| 5 — Executor integration | 4 | 8 | thin wrapper under executor_lock; biggest risk is Rule 14 review cycles |
| 6 — Floki tool integration | 6 | 10 | new tools + prompt changes + first integration test |
| 7 — Dashboard | 6 | 12 | backend endpoints + frontend card + styling |
| 8 — Shadow mode | 4 | 8 (code) + 14 calendar days observation | code minimal; observation is the real cost |
| 9 — Cutover | 2 | 4 | remove tool from registry + monitor |
| **Total (code only, excluding gates)** | **53** | **96** | Implementation effort |

**Observation windows (calendar time, non-code):**
- Phase 4.5: 48 h demo validation
- Phase 8: 14 calendar days shadow mode
- Phase 9: 7 calendar days post-cutover monitoring

**Session-level framing** (at ~4-6 focused hours per session):
- Phases 0-3: **4-6 sessions** (foundation)
- Phases 4-4.5: **2-3 sessions** (loop + 48h gate)
- Phases 5-7: **3-4 sessions** (executor + integration + UI)
- Phases 8-9: **2 sessions** (code) + **21 calendar days observation**

Total from FLO-348 start to Snow cutover: roughly **12-16 code sessions + 48 h demo + 14 days shadow + 7 days post-cutover**. Cadence set by Hermano — "segue o fluxo, vamos vendo."

---

## End of RFC v1

**Status:** §1-§16 complete. Awaiting final CTO review. On approval: FLO-348 ticket opens; Phase 1 implementation begins.

**Summary of CTO-facing decision points resolved in this RFC:**
- Pydantic v2 schema (§2.1)
- 14 condition primitives for v1 (§2.5)
- Plan state machine + invariants (§3)
- SQLite WAL + 3 tables, state-change-only evaluations (§4)
- Daemon thread spawned from main.py, blocked by FLO-348 (§5)
- Live + semantic data split per primitive (§6)
- Reuse executor.py under executor_lock; guards in Snow layer only (§7)
- Priority formula `base + min(base-1, override*10)` with power-of-2 bases (§8, revised per CTO)
- submit_plan_to_snow + audit-trail on direct actions (§9)
- Trade Room card + 3 API endpoints (§10)
- 10 edge cases with tests (§11)
- pytest scoped to `snow/tests/` (§12)
- Shadow mode = Approach 3 (Floki tags) + Approach 1 (passive intent log) (§13)
- Risks & unknowns explicit (§14)
- File structure + 10-phase task graph including Phase 4.5 DRY RUN gate (§15)
- 53-96 focused hours implementation + 48 h demo + 14-day shadow + 7-day post-cutover (§16)


