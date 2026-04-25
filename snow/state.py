"""Snow per-condition state cache — FLO-359 Phase 8b commit 2.

In-memory + DB persistence layer for state-bearing condition primitives
(indicator_crossover, indicator_was, price_crossed_level — primitive
classes land in commits 3-5). The infrastructure ships first so each
piece can be reviewed and reverted independently.

Three layers (FLO-356 RFC §2.1)
-------------------------------
- Per-tick:    `PerConditionStateCache` — dict, in-memory only.
- Per-plan:    `snow_plans.state_cache_json` — JSON list, one row per plan.
- Restart:     `rehydrate_from_db()` — populates the cache at bot start
               from the JSON column and drops rows whose `last_seen_at`
               is older than `STALE_STATE_THRESHOLD_MINUTES`.

Key convention (RFC §2.2 example)
---------------------------------
- `contingency_name = "_entry"` for entry-block conditions.
- `contingency_name = Contingency.name` (verbatim) for management/exit.
- Validator already forbids duplicate names within a plan (see
  `snow.validator._check_contingency_names_unique`), so the triple
  `(plan_id, contingency_name, condition_index)` is unique by
  construction. The cache leans on that rule rather than re-deriving
  it.

Persistence shape (RFC §2.2)
----------------------------
`snow_plans.state_cache_json` holds a JSON list per plan:

    [
      {"contingency_name": "_entry", "condition_index": 1,
       "cond_type": "indicator_crossover", "prev_value": 49.6,
       "prev_above_threshold": false, "bar_history": [],
       "bar_history_max_n": 0, "prev_bar_close_at": null,
       "last_seen_at": "2026-04-25T14:32:11Z"},
      ...
    ]

`plan_id` is the parent column's primary key — it is NOT serialised
inside each row to keep blobs compact.

Thread safety
-------------
Each `PerConditionStateCache` instance owns its own `threading.RLock`.
Snow's main loop is single-threaded; the lock is defence in depth +
isolates test instances from any future multi-threaded callers, in
the same spirit as `mt5_lock` / `executor_lock` (FLO-348).

Production callers use the module-level singleton `state_cache`.
Tests construct fresh `PerConditionStateCache()` instances directly
to maintain isolation.

Performance budget (RFC §8.2)
-----------------------------
- Per-tick cost: <10 ms total across all stateful conditions.
- Per-flush cost: <50 ms for 100 plans × 8 conditions.

A spec-test (`TestPerformance`) asserts the per-flush bound under a
realistic plan count to catch a serialisation regression before any
loop integration ships in commit 3+.
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from logger import log
from tz_utils import utc_iso


# Default stale-state cutoff per RFC §11.1. Restart-recovery drops any
# row whose `last_seen_at` is older than this threshold; the affected
# condition cold-starts on its next evaluation. 15 min covers planned
# maintenance + a typical crash restart without trusting state across
# long outages.
STALE_STATE_THRESHOLD_MINUTES: int = 15


# =============================================================================
# Per-condition row
# =============================================================================

@dataclass
class ConditionStateRow:
    """One row per (plan_id, contingency_name, condition_index).

    Carries every field any of the three commit-3-5 stateful primitives
    needs. Each primitive uses a subset:

      * indicator_crossover  → prev_value, prev_above_threshold
      * indicator_was        → bar_history, bar_history_max_n,
                               prev_bar_close_at
      * price_crossed_level  → prev_above_threshold (latch)

    `prev_bar_close_at` (ISO-8601 UTC-Z timestamp of the previously-
    appended bar's close) is the bar-dedupe key. Bar IDs aren't a
    first-class concept in the data layer; the close timestamp is, and
    is unique per (TF, bar) — so we use it directly rather than
    inventing a parallel id concept (advisor decision, RFC §3.2).
    """
    plan_id: str
    contingency_name: str
    condition_index: int
    cond_type: str
    prev_value: Optional[float] = None
    prev_above_threshold: Optional[bool] = None
    bar_history: list[float] = field(default_factory=list)
    bar_history_max_n: int = 0
    prev_bar_close_at: Optional[str] = None
    last_seen_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable shape. `plan_id` is dropped — it lives in
        the parent snow_plans row's primary key."""
        d = asdict(self)
        d.pop("plan_id", None)
        return d

    @classmethod
    def from_dict(cls, plan_id: str, d: dict[str, Any]) -> "ConditionStateRow":
        """Lenient deserialiser — missing optional fields default to
        the dataclass value, so a future v3 schema with extra fields
        round-trips through a v2 reader without crashing (the v2 reader
        just ignores the new keys).
        """
        return cls(
            plan_id=plan_id,
            contingency_name=d["contingency_name"],
            condition_index=int(d["condition_index"]),
            cond_type=str(d["cond_type"]),
            prev_value=d.get("prev_value"),
            prev_above_threshold=d.get("prev_above_threshold"),
            bar_history=list(d.get("bar_history", [])),
            bar_history_max_n=int(d.get("bar_history_max_n", 0)),
            prev_bar_close_at=d.get("prev_bar_close_at"),
            last_seen_at=str(d.get("last_seen_at", "")),
        )


# =============================================================================
# Cache
# =============================================================================

class PerConditionStateCache:
    """Dict-keyed per-condition state with per-instance RLock and
    per-plan dirty tracking. Flush writes one UPDATE per dirty plan
    (the column holds a JSON list of all that plan's rows) — never
    partial; in-memory IS the truth between flushes.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, int], ConditionStateRow] = {}
        # Per-plan dirty tracking. Per-row tracking would be wasted
        # complexity given the per-plan write granularity.
        self._dirty_plans: set[str] = set()
        self._lock = threading.RLock()

    # ----- inspection -----

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def get(
        self,
        plan_id: str,
        contingency_name: str,
        condition_index: int,
    ) -> Optional[ConditionStateRow]:
        with self._lock:
            return self._rows.get((plan_id, contingency_name, condition_index))

    def rows_for_plan(self, plan_id: str) -> list[ConditionStateRow]:
        with self._lock:
            return [r for r in self._rows.values() if r.plan_id == plan_id]

    def is_dirty(self, plan_id: str) -> bool:
        with self._lock:
            return plan_id in self._dirty_plans

    # ----- mutation -----

    def get_or_create(
        self,
        plan_id: str,
        contingency_name: str,
        condition_index: int,
        cond_type: str,
        bar_history_max_n: int = 0,
    ) -> ConditionStateRow:
        """Return the existing row for the key, or allocate a fresh
        one. Allocation marks the plan dirty so the next flush
        persists the cold-start row."""
        with self._lock:
            key = (plan_id, contingency_name, condition_index)
            row = self._rows.get(key)
            if row is None:
                row = ConditionStateRow(
                    plan_id=plan_id,
                    contingency_name=contingency_name,
                    condition_index=condition_index,
                    cond_type=cond_type,
                    bar_history_max_n=bar_history_max_n,
                    last_seen_at=utc_iso(),
                )
                self._rows[key] = row
                self._dirty_plans.add(plan_id)
            return row

    def mark_updated(self, plan_id: str, *, stamp: bool = True) -> None:
        """Mark `plan_id` dirty so the next flush picks it up.
        If `stamp=True`, also refresh `last_seen_at` on every row
        belonging to the plan — call this after an evaluator mutates
        a row's prev_value / bar_history / prev_above_threshold."""
        with self._lock:
            self._dirty_plans.add(plan_id)
            if stamp:
                now = utc_iso()
                for (pid, _, _), row in self._rows.items():
                    if pid == plan_id:
                        row.last_seen_at = now

    def forget_plan(self, plan_id: str) -> int:
        """Drop every row for `plan_id`. Call on plan transition to a
        terminal status (commit 3+ wires this into the lifecycle).
        Returns the number of rows removed.
        """
        with self._lock:
            keys = [k for k in self._rows if k[0] == plan_id]
            for k in keys:
                del self._rows[k]
            self._dirty_plans.discard(plan_id)
            return len(keys)

    def clear(self) -> None:
        """Reset the cache to empty. Used by tests + atomic rehydrate."""
        with self._lock:
            self._rows.clear()
            self._dirty_plans.clear()

    # ----- persistence -----

    def flush_to_db(
        self, plan_ids: Optional[Iterable[str]] = None
    ) -> int:
        """Serialise the cache for `plan_ids` (or all dirty plans if
        None) to `snow_plans.state_cache_json`. One UPDATE per plan;
        each plan's blob contains the FULL current row-set for that
        plan. Returns the number of plans written.

        Snapshot semantics: rows for the targeted plans are read out
        under the lock into per-plan blobs, the lock is released, then
        the DB write runs. If a plan transitions or a row mutates
        between snapshot and commit, the next flush will pick up the
        change (per-plan dirty bit was set on the mutation).
        """
        from snow.db import _connect  # late import — avoids cycle

        with self._lock:
            if plan_ids is None:
                targets = sorted(self._dirty_plans)
            else:
                targets = sorted(set(plan_ids))
            if not targets:
                return 0
            blobs: list[tuple[str, str]] = []
            for pid in targets:
                rows = [
                    r.to_dict() for r in self._rows.values()
                    if r.plan_id == pid
                ]
                blobs.append(
                    (json.dumps(rows, separators=(",", ":")), pid)
                )
            # Clear dirty inside the snapshot lock. A mutation arriving
            # during the DB write below will re-mark the plan; if the
            # write itself fails, in-memory state is still authoritative
            # and the next mutation will re-flag the plan. Discarding
            # AFTER the write would create a race in any multi-threaded
            # caller (mark-then-discard losing the dirty bit).
            for pid in targets:
                self._dirty_plans.discard(pid)

        conn = _connect()
        try:
            conn.executemany(
                "UPDATE snow_plans SET state_cache_json = ? WHERE id = ?",
                blobs,
            )
            conn.commit()
        finally:
            conn.close()
        return len(targets)

    def rehydrate_from_db(
        self,
        stale_threshold_minutes: int = STALE_STATE_THRESHOLD_MINUTES,
    ) -> int:
        """Atomic clear + reload from `snow_plans.state_cache_json` for
        every plan in a live status. Stale rows (last_seen_at older than
        the threshold) are dropped. Corrupt JSON or unparseable rows are
        logged + skipped, NOT raised — restart should never crash on
        bad disk state. Returns the number of rows loaded.
        """
        from snow.db import _connect, _LIVE_PLAN_STATUSES  # late import

        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(
            minutes=stale_threshold_minutes
        )

        conn = _connect()
        try:
            placeholders = ",".join("?" * len(_LIVE_PLAN_STATUSES))
            rows = conn.execute(
                f"SELECT id, state_cache_json FROM snow_plans "
                f"WHERE status IN ({placeholders})",
                _LIVE_PLAN_STATUSES,
            ).fetchall()
        finally:
            conn.close()

        loaded: list[ConditionStateRow] = []
        for r in rows:
            plan_id = r[0]
            blob = r[1]
            if not blob:
                continue
            try:
                parsed = json.loads(blob)
            except (json.JSONDecodeError, TypeError) as e:
                log.warning(
                    f"snow.state: plan_id={plan_id} corrupt "
                    f"state_cache_json — dropping ({type(e).__name__})"
                )
                continue
            if not isinstance(parsed, list):
                log.warning(
                    f"snow.state: plan_id={plan_id} state_cache_json "
                    f"is not a JSON list — dropping"
                )
                continue
            for entry in parsed:
                if not isinstance(entry, dict):
                    log.warning(
                        f"snow.state: plan_id={plan_id} non-dict "
                        f"state row — dropping"
                    )
                    continue
                try:
                    cs = ConditionStateRow.from_dict(plan_id, entry)
                except (KeyError, TypeError, ValueError) as e:
                    log.warning(
                        f"snow.state: plan_id={plan_id} unparseable "
                        f"state row — dropping ({type(e).__name__})"
                    )
                    continue
                ls = _parse_iso_z(cs.last_seen_at)
                if ls is not None and ls < cutoff:
                    continue
                loaded.append(cs)

        with self._lock:
            self._rows.clear()
            self._dirty_plans.clear()
            for cs in loaded:
                self._rows[
                    (cs.plan_id, cs.contingency_name, cs.condition_index)
                ] = cs
        return len(loaded)


# =============================================================================
# Helpers
# =============================================================================

def _parse_iso_z(ts: str) -> Optional[_dt.datetime]:
    """Parse an ISO-8601 UTC-Z timestamp; return None on any failure."""
    if not ts or not ts.endswith("Z"):
        return None
    try:
        return _dt.datetime.fromisoformat(ts[:-1]).replace(
            tzinfo=_dt.timezone.utc
        )
    except ValueError:
        return None


# =============================================================================
# Module-level singleton + convenience wrappers
# =============================================================================
#
# Production callers (loop integration in commit 3+, main.py startup
# call) reach for `state_cache` / `rehydrate_state_cache()` /
# `flush_state_cache()`. Tests construct their own
# `PerConditionStateCache()` instance directly to keep test isolation
# clean — DO NOT use the singleton in tests.

state_cache = PerConditionStateCache()


def rehydrate_state_cache(
    stale_threshold_minutes: int = STALE_STATE_THRESHOLD_MINUTES,
) -> int:
    """Production helper. Called once on bot start AFTER
    `snow.db.init_snow_tables()`."""
    return state_cache.rehydrate_from_db(stale_threshold_minutes)


def flush_state_cache(
    plan_ids: Optional[Iterable[str]] = None,
) -> int:
    """Production helper. Loop integration (commit 3+) calls this on
    its flush cadence (every 60 ticks per RFC §2.5)."""
    return state_cache.flush_to_db(plan_ids)
