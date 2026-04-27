"""Snow DB layer — SQLite persistence for plans, triggers, and evaluations.

Phase 2 scope (RFC §4 + §7.6 + §9): DDL + CRUD only. No MT5, no executor,
no evaluator, no main-loop wiring. Those land in later phases.

Tables (RFC §4.1):
  * snow_plans        — one row per plan, lifecycle + outcome
  * snow_triggers     — append-only audit log of every contingency firing
  * snow_evaluations  — bounded state-change log (NOT raw polling)

Concurrency model (RFC §4.3):
  * New connection per call (mirrors db_writer.py).
  * WAL inherited from db_writer._get_connection().
  * PRAGMA foreign_keys=ON is enabled locally for every snow connection —
    SQLite disables FK enforcement by default, and Snow depends on the FK
    from snow_triggers/snow_evaluations to snow_plans.

Atomicity (RFC §7.6, invariant I6):
  `record_trigger_and_transition()` writes one snow_triggers row AND
  updates the owning snow_plans row inside a single BEGIN / COMMIT, so
  crash-or-exception between the two cannot leave the plan in a FIRED
  state with no audit row, or vice versa.

Timestamp rule (CLAUDE.md Rule 22):
  All timestamps written by THIS module (entered_at, closed_at,
  last_evaluated_at, fired_at, evaluated_at) go through `tz_utils.utc_iso()`.
  Plan-owned timestamps (created_at, expires_at) come pre-formatted from
  the validated Plan object.

Test hook:
  `_connect()` is the single choke-point. Tests monkeypatch it to open
  an ephemeral on-disk SQLite file (tmp_path). `:memory:` is deliberately
  NOT used — each new connection to `:memory:` is a separate DB and
  would give tests phantom empty reads under the new-conn-per-call model.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Optional

import db_writer
from tz_utils import utc_iso

from snow.schema import Plan, PlanStatus


# =============================================================================
# Connection
# =============================================================================

def _connect() -> sqlite3.Connection:
    """Open a fresh SQLite connection for Snow use.

    Wraps `db_writer._get_connection()` so Snow inherits WAL + the 5 s
    busy-timeout, then:
      * enables FK enforcement (off by default in SQLite);
      * sets `row_factory = sqlite3.Row` so query results are dict-like.

    DEPENDENCY NOTE: this is a thin wrapper around `db_writer._get_connection()`.
    Any change to that function — path resolution, timeout, journal mode,
    pragmas — automatically propagates to Snow. If a future edit to
    db_writer drops WAL or narrows the timeout, expect WAL-dependent tests
    (concurrent insert) and I7 partial-unique tests to break first. Treat
    the wrapper as the single choke-point; do not reach around it.

    Tests monkeypatch this function to point at a tmp_path .db file.
    """
    conn = db_writer._get_connection()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# DDL
# =============================================================================

_DDL_STATEMENTS: tuple[str, ...] = (
    # --- snow_plans ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS snow_plans (
        id                TEXT PRIMARY KEY,
        schema_version    INTEGER NOT NULL,
        created_by        TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        expires_at        TEXT,
        status            TEXT NOT NULL,
        plan_json         TEXT NOT NULL,
        trade_ticket      INTEGER,
        entered_at        TEXT,
        closed_at         TEXT,
        outcome_pips      REAL,
        outcome_usd       REAL,
        last_evaluated_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snow_plans_status ON snow_plans(status)",
    # Partial index — only live-ish plans need expires_at lookup
    """
    CREATE INDEX IF NOT EXISTS idx_snow_plans_expires
        ON snow_plans(expires_at)
        WHERE status IN ('pending', 'active', 'triggered')
    """,
    # I7 — at most ONE live plan per broker ticket
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_snow_plans_live_ticket
        ON snow_plans(trade_ticket)
        WHERE trade_ticket IS NOT NULL
          AND status IN ('triggered', 'active', 'closing')
    """,

    # --- snow_triggers ------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS snow_triggers (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id           TEXT NOT NULL,
        contingency_name  TEXT NOT NULL,
        contingency_kind  TEXT NOT NULL,
        fired_at          TEXT NOT NULL,
        action_type       TEXT NOT NULL,
        action_params     TEXT,
        execution_status  TEXT NOT NULL,
        execution_result  TEXT,
        cycle_duration_ms INTEGER,
        FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snow_triggers_plan_fired
        ON snow_triggers(plan_id, fired_at DESC)
    """,

    # --- snow_evaluations ---------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS snow_evaluations (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        plan_id             TEXT NOT NULL,
        contingency_name    TEXT NOT NULL,
        evaluated_at        TEXT NOT NULL,
        event               TEXT NOT NULL,
        conditions_snapshot TEXT,
        FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snow_evaluations_plan_time
        ON snow_evaluations(plan_id, evaluated_at DESC)
    """,

    # --- snow_execution_quality (FLO-365) -----------------------------------
    # One row per dispatched FireEvent. PK is the snow_triggers row that
    # records the same dispatch — they're 1:1, so we share IDs. Slippage,
    # latency, and tick-snapshot fields are NULL for non-fill paths
    # (modify/adjust dispatches) so a single table covers all action types.
    """
    CREATE TABLE IF NOT EXISTS snow_execution_quality (
        id              INTEGER PRIMARY KEY,
        plan_id         TEXT NOT NULL,
        action_type     TEXT NOT NULL,
        fired_at        TEXT,
        executed_at     TEXT NOT NULL,
        latency_ms      INTEGER,
        plan_volume     REAL,
        plan_price      REAL,
        actual_volume   REAL,
        actual_price    REAL,
        slippage_pips   REAL,
        bid_at_fire     REAL,
        ask_at_fire     REAL,
        mid_at_fire     REAL,
        status          TEXT NOT NULL,
        ticket          INTEGER,
        attempts        INTEGER,
        error_message   TEXT,
        FOREIGN KEY (id)      REFERENCES snow_triggers(id),
        FOREIGN KEY (plan_id) REFERENCES snow_plans(id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snow_exec_quality_plan
        ON snow_execution_quality(plan_id, executed_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snow_exec_quality_action
        ON snow_execution_quality(action_type, executed_at DESC)
    """,
)


def init_snow_tables() -> None:
    """Create Snow tables + indexes if they don't exist. Idempotent.

    Called once from main.py startup AFTER `db_writer.init_db()` (Phase 4
    wiring). Safe to call repeatedly — every statement is `IF NOT EXISTS`,
    and additive ALTER TABLE migrations are wrapped in a duplicate-column
    guard so re-running on an already-migrated DB is a no-op.
    """
    conn = _connect()
    try:
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        _apply_additive_migrations(conn)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Additive migrations — null-safe ALTER TABLE
# =============================================================================
#
# SQLite supports ADD COLUMN; older code reading a migrated DB simply
# ignores the new column. The wrapper below catches the
# "duplicate column name" OperationalError so init_snow_tables() stays
# idempotent across restarts. Down-migrations are NOT supported here —
# revert path is "restore from data/history.db.backup-pre-phase8b" or
# accept the additive column as a benign artefact (NULL-safe for v1).

# (column_name, ddl_fragment) — checked in order on every init.
_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    # FLO-359 Phase 8b commit 1: state cache column for stateful primitives.
    # NULL for v1 plans and for any v2 plan that has not yet flushed state.
    ("state_cache_json", "ALTER TABLE snow_plans ADD COLUMN state_cache_json TEXT"),
)


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Run idempotent ALTER TABLE statements. Each ADD COLUMN is wrapped
    in an `OperationalError` guard so re-running is a no-op."""
    for col_name, ddl in _ADDITIVE_COLUMNS:
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            # Two SQLite phrasings: "duplicate column name: X" / "column X already exists"
            if "duplicate column" in msg or "already exists" in msg:
                continue
            raise


# =============================================================================
# Plans
# =============================================================================

# Status groups used in reload queries (RFC §3.5, §5)
_LIVE_PLAN_STATUSES: tuple[str, ...] = (
    PlanStatus.PENDING.value,
    PlanStatus.TRIGGERED.value,
    PlanStatus.ACTIVE.value,
    PlanStatus.CLOSING.value,
)


def generate_plan_id(date: Optional[str] = None) -> str:
    """Generate a new plan_id of the form `PLAN-YYYYMMDD-NNN`.

    NNN is a daily monotonic counter — the highest existing NNN for
    `date` plus 1, or 001 if none exist yet. `date` defaults to the
    current UTC calendar day via `tz_utils.trading_day_utc()` (stripped
    to YYYYMMDD).

    Concurrency: two simultaneous callers COULD race and receive the same
    NNN. The caller (submit_plan_to_snow tool) must treat that as a
    collision and retry — SQLite's PRIMARY KEY enforces uniqueness, so
    the loser sees `IntegrityError` on insert. This is acceptable at
    Floki's submission cadence (a handful per day).
    """
    from tz_utils import trading_day_utc
    date = date or trading_day_utc().replace("-", "")
    prefix = f"PLAN-{date}-"
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT id FROM snow_plans WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
            (prefix + "%",),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        next_n = 1
    else:
        try:
            next_n = int(str(row["id"]).rsplit("-", 1)[-1]) + 1
        except (ValueError, IndexError):
            # Malformed existing id — fall back to fresh sequence. Logged
            # by caller if it actually hits the DB layer somehow.
            next_n = 1
    return f"{prefix}{next_n:03d}"


def insert_plan(plan: Plan) -> None:
    """Persist a validated Plan at status=PENDING (or whatever the Plan
    carries — validator sets PENDING by default).

    The JSON blob is authoritative; the columns are denormalised for
    indexing (status, expires_at, trade_ticket). On insert, `schema_version`
    is extracted explicitly into the column rather than re-parsed later
    from plan_json — keeps the index-lookup path zero-parse.
    """
    plan_json = plan.model_dump_json()
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO snow_plans (
                id, schema_version, created_by, created_at, expires_at,
                status, plan_json, trade_ticket, entered_at, closed_at,
                outcome_pips, outcome_usd, last_evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (
                plan.id,
                plan.schema_version,
                plan.created_by,
                plan.created_at,
                plan.expires_at,
                plan.status.value,
                plan_json,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_plan(plan_id: str) -> Optional[dict[str, Any]]:
    """Return the full snow_plans row as a dict, or None if not found."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM snow_plans WHERE id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_plan_as_model(plan_id: str) -> Optional[Plan]:
    """Return the plan re-hydrated as a Pydantic Plan, or None."""
    row = get_plan(plan_id)
    if row is None:
        return None
    return Plan.model_validate_json(row["plan_json"])


def list_plans_by_status(
    statuses: Iterable[str], limit: int = 200
) -> list[dict[str, Any]]:
    """Return plans whose `status` is in `statuses` (lower-case strings).

    Used by the dashboard/API surface. Ordered by `created_at DESC` so the
    newest plans show first.
    """
    statuses = tuple(statuses)
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    conn = _connect()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM snow_plans
             WHERE status IN ({placeholders})
             ORDER BY created_at DESC
             LIMIT ?
            """,
            (*statuses, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_active_plans() -> list[dict[str, Any]]:
    """Return all plans the Snow loop must evaluate this tick.

    "Active" here = any non-terminal status. Terminal statuses (closed,
    expired, failed, canceled) are skipped. RFC §3.5 recovery uses the
    same filter.
    """
    return list_plans_by_status(_LIVE_PLAN_STATUSES, limit=10_000)


def update_plan_status(plan_id: str, new_status: str) -> None:
    """Transition a plan's status column. Does NOT record a trigger —
    callers that also need an audit row should use
    `record_trigger_and_transition()` instead."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE snow_plans SET status = ? WHERE id = ?",
            (new_status, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_plan_trade_ticket(plan_id: str, ticket: int) -> None:
    """Attach a broker ticket to the plan (entry just fired). Stamps
    `entered_at` with the current UTC time."""
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE snow_plans
               SET trade_ticket = ?, entered_at = ?
             WHERE id = ?
            """,
            (ticket, utc_iso(), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_plan_outcome(
    plan_id: str,
    outcome_pips: float,
    outcome_usd: float,
    new_status: str = PlanStatus.CLOSED.value,
) -> None:
    """Record final P&L and close the plan. Stamps `closed_at` with the
    current UTC time."""
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE snow_plans
               SET outcome_pips = ?,
                   outcome_usd  = ?,
                   closed_at    = ?,
                   status       = ?
             WHERE id = ?
            """,
            (outcome_pips, outcome_usd, utc_iso(), new_status, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def mark_plan_terminal(
    plan_id: str,
    new_status: str,
    *,
    closed_at: Optional[str] = None,
) -> None:
    """FLO-374: transition a plan to a terminal status (CLOSED /
    EXPIRED / CANCELLED / FAILED) AND stamp `closed_at` with the
    current UTC time, atomically.

    Use this instead of `update_plan_status` whenever the new status
    is terminal. The `closed_at` column is the canonical "when did
    this plan end" timestamp consumed by reporting / dashboards /
    `outcome_pips` / `outcome_usd` joins. Pre-FLO-374 the recovery
    sweep and several other terminal transitions stamped only
    `status` and left `closed_at` NULL — the dashboard duration
    column read NULL and several downstream queries had to fall back
    to `last_evaluated_at` heuristics.

    `update_plan_outcome` already stamps `closed_at`; this helper
    exists so callers that DON'T have outcome figures yet (recovery
    runs before backfill_outcome; cancel_plan never has outcome)
    can still close the audit gap.

    FLO-379: optional `closed_at` lets callers stamp the broker-side
    close time (from MT5 deal history) instead of the detection
    moment. Audit accuracy beats convention — queries asking "what
    closed at 13:11Z" expect broker time, not when Snow noticed.
    When omitted, behavior is unchanged (`utc_iso()` now). The
    `COALESCE` protection still applies: a previously-stamped
    `closed_at` always wins.
    """
    if new_status not in {
        PlanStatus.CLOSED.value,
        PlanStatus.EXPIRED.value,
        PlanStatus.CANCELLED.value,
        PlanStatus.FAILED.value,
    }:
        # Defensive: keep this helper scoped to terminal states. A
        # caller mistakenly passing PlanStatus.ACTIVE would silently
        # corrupt `closed_at` on a still-live plan otherwise.
        raise ValueError(
            f"mark_plan_terminal: {new_status!r} is not a terminal "
            f"status. Use update_plan_status for non-terminal "
            f"transitions."
        )
    stamp = closed_at if closed_at else utc_iso()
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE snow_plans
               SET status    = ?,
                   closed_at = COALESCE(closed_at, ?)
             WHERE id = ?
            """,
            (new_status, stamp, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_plan_outcome_columns_only(
    plan_id: str,
    outcome_pips: float,
    outcome_usd: float,
) -> None:
    """FLO-353 — backfill `outcome_pips` / `outcome_usd` WITHOUT changing
    `status` or `closed_at`. Caller has already transitioned the plan;
    this just fills observability columns. Distinct from
    `update_plan_outcome` which is a one-shot close + outcome stamp."""
    conn = _connect()
    try:
        conn.execute(
            """
            UPDATE snow_plans
               SET outcome_pips = ?,
                   outcome_usd  = ?
             WHERE id = ?
            """,
            (outcome_pips, outcome_usd, plan_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_plan_last_evaluated(plan_id: str) -> None:
    """Bookkeeping — stamps last_evaluated_at with the current UTC time.
    Called by the Snow loop at the end of each tick per plan (Phase 4)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE snow_plans SET last_evaluated_at = ? WHERE id = ?",
            (utc_iso(), plan_id),
        )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Triggers (audit log — every contingency firing)
# =============================================================================

def record_trigger(
    plan_id: str,
    contingency_name: str,
    contingency_kind: str,
    action_type: str,
    execution_status: str,
    action_params: Optional[dict[str, Any]] = None,
    execution_result: Optional[dict[str, Any]] = None,
    cycle_duration_ms: Optional[int] = None,
) -> int:
    """Append a snow_triggers row. Returns the new row's auto-increment id.

    `action_params` and `execution_result` are serialised as JSON so the
    row stays append-only and self-describing.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO snow_triggers (
                plan_id, contingency_name, contingency_kind, fired_at,
                action_type, action_params, execution_status,
                execution_result, cycle_duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                contingency_name,
                contingency_kind,
                utc_iso(),
                action_type,
                json.dumps(action_params) if action_params is not None else None,
                execution_status,
                json.dumps(execution_result) if execution_result is not None else None,
                cycle_duration_ms,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def has_contingency_fired_successfully(
    plan_id: str, contingency_name: str
) -> bool:
    """FLO-373: Return True if the named contingency has already fired
    successfully on this plan (LIVE path) OR has at least one
    `*_would_fire` evaluation row (DRY_RUN path). Used by snow_loop to
    enforce `fires: once` across ticks AND across bot restarts.

    LIVE source of truth: snow_triggers row with execution_status =
    'success'. DRY_RUN source of truth: snow_evaluations row with
    event in {entry_would_fire, management_would_fire, exit_would_fire}.

    The two-table check exists because LIVE dispatch writes only to
    snow_triggers and DRY_RUN simulation writes only to
    snow_evaluations — and a single fires:once contract should hold
    in either mode.

    Failed dispatches (retry_exhausted / timeout / error) do NOT block
    re-evaluation: a contingency that tried-and-failed remains armed.
    """
    conn = _connect()
    try:
        # LIVE path
        r = conn.execute(
            "SELECT 1 FROM snow_triggers "
            " WHERE plan_id = ? AND contingency_name = ? "
            "   AND execution_status = 'success' "
            " LIMIT 1",
            (plan_id, contingency_name),
        ).fetchone()
        if r is not None:
            return True
        # DRY_RUN path
        r = conn.execute(
            "SELECT 1 FROM snow_evaluations "
            " WHERE plan_id = ? AND contingency_name = ? "
            "   AND event IN ('entry_would_fire', "
            "                 'management_would_fire', "
            "                 'exit_would_fire') "
            " LIMIT 1",
            (plan_id, contingency_name),
        ).fetchone()
        return r is not None
    finally:
        conn.close()


def list_triggers(
    plan_id: Optional[str] = None, limit: int = 100
) -> list[dict[str, Any]]:
    """Return recent trigger rows, newest first. If `plan_id` is given,
    filters to that plan; otherwise global."""
    conn = _connect()
    try:
        # Secondary order on id DESC because fired_at resolution is seconds
        # (utc_iso drops microseconds) — triggers fired in the same second
        # would otherwise tie-break non-deterministically, confusing callers
        # that want "most recent first".
        if plan_id is None:
            rows = conn.execute(
                "SELECT * FROM snow_triggers "
                "ORDER BY fired_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM snow_triggers
                 WHERE plan_id = ?
                 ORDER BY fired_at DESC, id DESC
                 LIMIT ?
                """,
                (plan_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# =============================================================================
# Evaluations (state-change log — NOT raw polling; RFC §4.2)
# =============================================================================

def record_evaluation(
    plan_id: str,
    contingency_name: str,
    event: str,
    conditions_snapshot: Optional[dict[str, Any]] = None,
) -> int:
    """Append a snow_evaluations row. `event` must be one of
    {'armed', 'all_true_first_time', 'fired', 'deactivated', 'guard_blocked'}.

    Returns the new row's auto-increment id.
    """
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO snow_evaluations (
                plan_id, contingency_name, evaluated_at, event,
                conditions_snapshot
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                contingency_name,
                utc_iso(),
                event,
                json.dumps(conditions_snapshot)
                    if conditions_snapshot is not None
                    else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# =============================================================================
# Atomic compound operations (RFC §7.6, invariant I6)
# =============================================================================

def record_trigger_and_transition(
    plan_id: str,
    *,
    contingency_name: str,
    contingency_kind: str,
    action_type: str,
    execution_status: str,
    new_plan_status: str,
    action_params: Optional[dict[str, Any]] = None,
    execution_result: Optional[dict[str, Any]] = None,
    cycle_duration_ms: Optional[int] = None,
    trade_ticket: Optional[int] = None,
) -> int:
    """Append a snow_triggers row AND transition the plan's status in a
    single transaction.

    Either both writes succeed and the transaction commits, or an
    exception in either statement rolls back both. Callers never see the
    plan advanced to (e.g.) ACTIVE with no matching trigger row, or a
    trigger row orphaned by a failed status update.

    If `trade_ticket` is provided (non-None), the plan's `trade_ticket`
    AND `entered_at` columns are set in the same transaction — this is
    the normal path when an entry contingency fires.

    Returns the new trigger row's id.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            """
            INSERT INTO snow_triggers (
                plan_id, contingency_name, contingency_kind, fired_at,
                action_type, action_params, execution_status,
                execution_result, cycle_duration_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                contingency_name,
                contingency_kind,
                utc_iso(),
                action_type,
                json.dumps(action_params) if action_params is not None else None,
                execution_status,
                json.dumps(execution_result) if execution_result is not None else None,
                cycle_duration_ms,
            ),
        )
        trigger_id = cur.lastrowid

        if trade_ticket is not None:
            conn.execute(
                """
                UPDATE snow_plans
                   SET status = ?, trade_ticket = ?, entered_at = ?
                 WHERE id = ?
                """,
                (new_plan_status, trade_ticket, utc_iso(), plan_id),
            )
        else:
            conn.execute(
                "UPDATE snow_plans SET status = ? WHERE id = ?",
                (new_plan_status, plan_id),
            )
        conn.commit()
        return trigger_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =============================================================================
# Execution quality (FLO-365)
# =============================================================================

def insert_execution_quality(
    *,
    trigger_id: int,
    plan_id: str,
    action_type: str,
    fired_at: Optional[str],
    executed_at: str,
    latency_ms: Optional[int],
    plan_volume: Optional[float],
    plan_price: Optional[float],
    actual_volume: Optional[float],
    actual_price: Optional[float],
    slippage_pips: Optional[float],
    bid_at_fire: Optional[float],
    ask_at_fire: Optional[float],
    mid_at_fire: Optional[float],
    status: str,
    ticket: Optional[int],
    attempts: Optional[int],
    error_message: Optional[str],
) -> None:
    """Append a snow_execution_quality row keyed to a snow_triggers id.

    Best-effort: the caller has already recorded the trigger and (if a
    fill) updated the plan; a failure here must not propagate up and
    abort the dispatch. Logs and swallows.
    """
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO snow_execution_quality (
                id, plan_id, action_type, fired_at, executed_at,
                latency_ms, plan_volume, plan_price,
                actual_volume, actual_price, slippage_pips,
                bid_at_fire, ask_at_fire, mid_at_fire,
                status, ticket, attempts, error_message
            ) VALUES (?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?)
            """,
            (
                int(trigger_id), plan_id, action_type, fired_at, executed_at,
                latency_ms, plan_volume, plan_price,
                actual_volume, actual_price, slippage_pips,
                bid_at_fire, ask_at_fire, mid_at_fire,
                status, ticket, attempts, error_message,
            ),
        )
        conn.commit()
    except Exception as e:
        # Best-effort observability: don't let an FK / disk hiccup abort
        # the dispatch. Logged once via the project logger; the missing
        # row will surface to operators when an aggregate query comes up
        # short.
        try:
            from logger import log as _log
            _log.warning(
                f"snow.db.insert_execution_quality_failed trigger_id={trigger_id} "
                f"plan={plan_id} action={action_type}: {type(e).__name__}: {e}"
            )
        except Exception:
            pass
    finally:
        conn.close()
