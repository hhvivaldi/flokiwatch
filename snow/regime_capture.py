"""FLO-422 Step 5 — trigger-time regime snapshot capture.

Wired into `snow_loop._dispatch_fires` for entry-fire events. Captures
the volatility regime at the moment Snow's entry conditions actually
fire and writes it alongside the author-time snapshot persisted by
`agent_tools._maybe_persist_author_regime_snapshot` at submit time.

Strict invariants (CEO PR1 directive):
  - NEVER raises into the caller. Top-level entry point swallows every
    exception class. The snow_loop dispatch already wraps each fire in
    its own try/except; this module adds belt-and-suspenders.
  - NEVER blocks order execution. The hook is invoked AFTER
    `execute_action` returns successfully (see snow_loop.py `else`
    branch). A snapshot failure is invisible to the trade path.
  - NO behavioral change. Writes JSON columns and emits one log line.
    No code path reads these values yet — this is observability only,
    feeding the FLO-425 dataset accumulation through the FLO-424 window.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from logger import log

# Mirrors agent_tools._FLO422_LIFECYCLE_SETUPS. Kept local to avoid an
# import dependency on agent_tools at module load (lazy-imported below
# for the fetch helpers, where the cost is paid only on actual fires).
_LIFECYCLE_SETUPS = (
    "breakout_range",
    "continuation_momentum",
    "pullback_trend",
    "structural_bounce",
)


def maybe_capture_trigger_snapshot(
    plan_id: str,
    contingency_name: str,
    plan_list_order: int,
) -> None:
    """Trigger-time snapshot capture hook.

    Called once per FireEvent from `snow_loop._dispatch_fires` after a
    successful action dispatch. No-op for non-entry fires (management,
    exit). No-op for non-lifecycle setup_types. Fail-soft on every path.

    Args:
        plan_id:           the plan whose entry just fired.
        contingency_name:  FireEvent.contingency_name; "_entry" for entries.
        plan_list_order:   FireEvent.plan_list_order; -1 for entries
                           (per snow.priority.FireEvent convention).
    """
    try:
        # Entry-only filter. Snow loop convention: entry has plan_list_order
        # == -1 AND contingency_name == "_entry". Either alone is sufficient;
        # we check both for explicit clarity.
        if plan_list_order != -1 or contingency_name != "_entry":
            return

        plan_row = _load_plan_row(plan_id)
        if plan_row is None:
            return  # plan deleted or unreadable — nothing to attach to

        setup_type = plan_row["setup_type"]
        direction = plan_row["direction"]
        entry_price = plan_row["entry_price"]

        if setup_type not in _LIFECYCLE_SETUPS:
            return  # symmetric with author-side gating
        if direction not in ("BUY", "SELL"):
            return

        snap_ts = datetime.now(timezone.utc)

        # Reuse the author-side fetch helpers — single source of truth for
        # MT5 + analyses access. Lazy-imported to keep snow.regime_capture
        # importable without pulling agent_tools at module-load time.
        from agent_tools import _flo422_fetch_m5_candles, _flo422_fetch_analyses

        m5_candles = _flo422_fetch_m5_candles(snap_ts, n=30)
        analyses_24h = _flo422_fetch_analyses(snap_ts, minutes_back=24 * 60)
        cutoff_4h = (snap_ts.replace(tzinfo=None) if snap_ts.tzinfo else snap_ts) \
            - timedelta(minutes=240)
        cutoff_4h_iso = cutoff_4h.isoformat()[:19]
        analyses_4h = [a for a in analyses_24h
                       if a.get("timestamp") and a["timestamp"] >= cutoff_4h_iso]

        # current_price: prefer the most recent M5 close; fall back to
        # entry_price (which is the literal trigger level, a reasonable
        # proxy at the moment a level-relative trigger fires).
        if m5_candles:
            current_price = float(m5_candles[-1].get("close") or entry_price or 0.0)
        elif entry_price is not None:
            current_price = float(entry_price)
        else:
            return  # no price reference at all — skip silently

        from breakout_regime import compute_regime_snapshot, compute_drift

        trigger_snapshot = compute_regime_snapshot(
            ts=snap_ts,
            direction=direction,
            setup_type=setup_type,
            breakout_level=float(entry_price) if entry_price is not None else None,
            current_price=current_price,
            candles_m5=m5_candles,
            analyses_4h=analyses_4h,
            analyses_24h=analyses_24h,
            stage="trigger",
        )

        # Drift compute is conditional on the author snapshot existing.
        # If the author snapshot is missing (e.g. the plan was authored
        # before FLO-422 Step 3 shipped, or the author-side capture
        # failed), the trigger snapshot still persists with drift=null.
        author_snapshot: Optional[Dict[str, Any]] = None
        author_json = plan_row["author_regime_snapshot_json"]
        if author_json:
            try:
                author_snapshot = json.loads(author_json)
            except (TypeError, ValueError):
                author_snapshot = None

        drift: Optional[Dict[str, Any]] = None
        author_to_trigger_minutes: Optional[float] = None
        drift_class = "no_author_snapshot"
        if author_snapshot is not None:
            try:
                drift = compute_drift(author_snapshot, trigger_snapshot)
                delta_s = drift.get("delta_seconds_author_to_trigger")
                if delta_s is not None:
                    author_to_trigger_minutes = round(delta_s / 60.0, 1)
                drift_class = drift.get("drift_assessment", "unknown")
            except Exception as e:
                drift = None
                drift_class = f"drift_compute_failed:{type(e).__name__}"

        _persist_trigger_snapshot(plan_id, trigger_snapshot, drift)

        def _fmt_pct(v):
            return f"{v:+.2f}%" if isinstance(v, (int, float)) else "None"

        warn_str = ",".join(trigger_snapshot.get("computation_warnings", []))
        log.info(
            f"BREAKOUT_REGIME_SNAPSHOT | plan={plan_id} | snapshot_version=1 | "
            f"stage=trigger | setup={setup_type} | dir={direction} | "
            f"author_to_trigger_minutes={author_to_trigger_minutes} | "
            f"drift_class={drift_class} | "
            f"impulse_total={trigger_snapshot.get('impulse_total_60m')} | "
            f"bb_width_4h={_fmt_pct(trigger_snapshot.get('bb_width_4h_pct'))} | "
            f"atr_4h={_fmt_pct(trigger_snapshot.get('atr_4h_pct'))} | "
            f"breakout_age_bars={trigger_snapshot.get('breakout_age_bars')} | "
            f"warnings=[{warn_str}]"
        )
    except Exception as e:
        try:
            log.warning(
                f"FLO-422 trigger snapshot failed for {plan_id}: "
                f"{type(e).__name__}: {e}"
            )
        except Exception:
            pass


def _load_plan_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """Pull `plan_json` + `author_regime_snapshot_json` from snow_plans
    and decode the fields the trigger snapshot needs. Returns None on
    any failure or missing row."""
    try:
        import config as _cfg
        db_path = getattr(_cfg, "HISTORY_DB_PATH", "data/history.db")
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT plan_json, author_regime_snapshot_json "
                "FROM snow_plans WHERE id = ?",
                (plan_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None or not row[0]:
            return None
        plan = json.loads(row[0])
        analysis = plan.get("analysis") or {}
        entry = plan.get("entry") or {}
        return {
            "setup_type": analysis.get("setup_type"),
            "direction": entry.get("direction"),
            "entry_price": entry.get("entry_price"),
            "author_regime_snapshot_json": row[1],
        }
    except Exception:
        return None


def _persist_trigger_snapshot(
    plan_id: str,
    trigger_snapshot: Dict[str, Any],
    drift: Optional[Dict[str, Any]],
) -> None:
    """UPDATE snow_plans.trigger_regime_snapshot_json (and
    regime_drift_json when available) in a single transaction.
    Fail-soft."""
    try:
        import config as _cfg
        db_path = getattr(_cfg, "HISTORY_DB_PATH", "data/history.db")
        conn = sqlite3.connect(db_path)
        try:
            if drift is not None:
                conn.execute(
                    "UPDATE snow_plans "
                    "SET trigger_regime_snapshot_json = ?, "
                    "    regime_drift_json = ? "
                    "WHERE id = ?",
                    (
                        json.dumps(trigger_snapshot, default=str),
                        json.dumps(drift, default=str),
                        plan_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE snow_plans "
                    "SET trigger_regime_snapshot_json = ? "
                    "WHERE id = ?",
                    (json.dumps(trigger_snapshot, default=str), plan_id),
                )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        try:
            log.warning(f"FLO-422 trigger persist failed for {plan_id}: {e}")
        except Exception:
            pass
