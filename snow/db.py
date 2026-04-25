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
