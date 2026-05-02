"""FLO-382 — diagnostic instrumentation helpers.

Three additive event emitters for the 24h pilot study:

  * snow.plan.recipe_pulled   — Recipe Book adoption telemetry (D1)
  * snow.trade.scratch_pattern — BE-scratch + close attribution (D2)
  * snow.trade.volume_audit    — planned vs actual sizing (D3)

All three emit single-line structured INFO logs. Each emitter is
wrapped in its own try/except — failure inside diagnostic code is
caught, logged WARN, and never propagates back to production paths
(submit_plan_to_snow, runtime_reconcile, outcome.backfill).

Mapping policy
--------------
D1 setup_type → category mapping is data-driven from the recipe
book itself. `categories_for_setup_type(s)` returns the set of
recipe categories whose setup_type_alignment list contains `s`.
This means the mapping updates automatically as the recipe book
grows, with no hand-coded table to maintain.

D2 close_reason classification is a deterministic 5-bucket map:
  - broker_sl  | DEAL_REASON_SL
  - broker_tp  | DEAL_REASON_TP
  - manual_mt5 | DEAL_REASON_CLIENT
  - snow_close | DEAL_REASON_EXPERT + matching snow_triggers row
  - expert_unattributed | DEAL_REASON_EXPERT + no match
The unattributed bucket collapses Floki adjust_trade closes,
monitor.py BE/drawdown closes, safety_checks closes, and EA
Bridge closes — distinguishing them deterministically requires
audit infra outside FLO-382's scope. Acceptance threshold: if
the 24h pilot shows expert_unattributed > 30% of closes, follow
up FLO-383 to add finer attribution.

D3 BE detection relies on the close-deal's `sl` field (MT5 deal
attribute = position SL at deal moment). If `abs(close_deal.sl
- entry_price) < 1 pip`, BE was locked at close time. This catches
Snow `move_sl_to_breakeven`, Floki `adjust_trade` to entry, and
monitor.py `modify_sl` to entry symmetrically.
"""
from __future__ import annotations

from typing import Any, Optional

from logger import log
from tz_utils import utc_iso


# Pip distance below which we treat close_deal.sl == entry_price
# as "BE was locked." 1 pip on XAUUSD = 0.1 price units.
_BE_TOLERANCE_PIPS = 1.0


def categories_for_setup_type(setup_type: str) -> list[str]:
    """Return the sorted list of Recipe Book categories that contain
    at least one recipe whose setup_type_alignment includes
    `setup_type`. Empty list if setup_type is unknown to the book
    or the book itself is unavailable.
    """
    try:
        from snow.recipe_book import get_recipes_by_category
        all_recipes = get_recipes_by_category(category=None)
        if not all_recipes.get("success"):
            return []
        cats: set[str] = set()
        for r in all_recipes.get("recipes", []):
            if setup_type in (r.get("setup_type_alignment") or []):
                cat = r.get("category")
                if cat:
                    cats.add(cat)
        return sorted(cats)
    except Exception:
        return []


# FLO-395 E2: indicator-family taxonomy for entry-conditions vocabulary
# diversity metric. Each Snow primitive type maps to one analytical
# family. `count of distinct families in entry.conditions` is the
# success metric for FLO-395 Phase 1 (target: 0.84 → 2.0+ over 7d).
#
# The taxonomy is intentionally coarse — splits on analytical role
# (oscillator vs trend vs structural), not on indicator identity. A
# plan with rsi+stochastic+macd_histogram counts as ONE family
# (oscillator) because it's the same analytical signal repeated; a
# plan with rsi+ema_relation+price_at_sr_zone counts as THREE families
# (oscillator + trend + structural) because the analytical surface is
# genuinely diverse.
_PRIMITIVE_FAMILY: dict[str, str] = {
    # oscillator family
    "rsi": "oscillator",
    "macd_histogram": "oscillator",
    "stochastic": "oscillator",
    # trend family
    "ema_relation": "trend",
    # structural family (price-vs-level)
    "price_above": "structural",
    "price_below": "structural",
    "price_at_sr_zone": "structural",
    "price_at_fibonacci": "structural",
    "price_at_pivot": "structural",
    "price_crossed_level": "structural",
    # volatility family
    "atr": "volatility",
    "bollinger_position": "volatility",
    # pattern / divergence family (transition signals)
    "indicator_divergence": "pattern",
    "indicator_crossover": "pattern",
    "indicator_was": "pattern",
    # time family (gating, not directional)
    "time_between": "time",
    "duration_exceeds": "time",
}


def _entry_vocabulary_diversity(plan: Optional[Any]) -> tuple[int, int, list[str]]:
    """FLO-395 E2: count distinct primitive types and analytical
    families in plan.entry.conditions.

    Returns (n_distinct_types, n_distinct_families, families_sorted).
    Returns (0, 0, []) if plan is None or has no parseable entry block.
    """
    try:
        if plan is None:
            return (0, 0, [])
        entry = getattr(plan, "entry", None)
        if entry is None and isinstance(plan, dict):
            entry = plan.get("entry")
        if entry is None:
            return (0, 0, [])
        if isinstance(entry, dict):
            conditions = entry.get("conditions", [])
        else:
            conditions = getattr(entry, "conditions", None) or []
        if not conditions:
            return (0, 0, [])
        types: set[str] = set()
        families: set[str] = set()
        for c in conditions:
            ct = (
                c.get("type") if isinstance(c, dict)
                else getattr(c, "type", None)
            )
            if not ct:
                continue
            types.add(str(ct))
            fam = _PRIMITIVE_FAMILY.get(str(ct))
            if fam:
                families.add(fam)
        return (len(types), len(families), sorted(families))
    except Exception:
        return (0, 0, [])


def emit_recipe_pulled(
    plan_id: str,
    recipe_pulls: list[dict[str, Any]],
    final_setup_type: Optional[str],
    plan: Optional[Any] = None,
) -> None:
    """Emit snow.plan.recipe_pulled diagnostic for FLO-382 D1 + FLO-395 E2.

    Args:
      plan_id: ID of the plan that was just submitted.
      recipe_pulls: list of {ts, category, count} dicts captured
        from the AgentTools.get_snow_recipe_book buffer for the
        cycle that submitted this plan.
      final_setup_type: setup_type from the submitted plan's
        analysis block. May be None on schema_version < 3 plans.
      plan: parsed Plan model (or plan dict) — used for FLO-395 E2
        entry-vocabulary-diversity computation. Optional for backwards
        compatibility; when None, diversity fields emit as 0 / [].
    """
    try:
        n = len(recipe_pulls)
        cats_pulled = sorted({
            rp.get("category") for rp in recipe_pulls
            if rp.get("category") is not None
        })
        # Closed-status derivation
        if n == 0:
            match_status = "no_pull"
        elif final_setup_type is None:
            match_status = "no_setup_type"
        else:
            valid_cats = set(categories_for_setup_type(final_setup_type))
            if not valid_cats:
                match_status = "unknown_setup_type"
            elif any(c in valid_cats for c in cats_pulled):
                match_status = "matched"
            else:
                match_status = "mismatched"

        cats_repr = "[" + ",".join(cats_pulled) + "]"

        # FLO-395 E2: vocabulary diversity in entry.conditions.
        n_types, n_families, families = _entry_vocabulary_diversity(plan)
        families_repr = "[" + ",".join(families) + "]"

        log.info(
            f"snow.plan.recipe_pulled "
            f"plan_id={plan_id} cycle_emit_ts={utc_iso()} "
            f"recipe_pulls_count={n} "
            f"recipe_categories_pulled={cats_repr} "
            f"final_setup_type={final_setup_type or 'null'} "
            f"match_status={match_status} "
            f"entry_distinct_primitive_types={n_types} "
            f"entry_distinct_families={n_families} "
            f"entry_families={families_repr}"
        )
    except Exception as e:  # never fail the production caller
        try:
            log.warning(f"snow.plan.recipe_pulled emit failed: {e}")
        except Exception:
            pass


def _classify_close_reason(
    plan_id: str,
    close_deals: list[Any],
    close_time_iso: Optional[str],
) -> tuple[str, Optional[int]]:
    """Return (close_reason_label, raw_deal_reason_int).

    Deterministic 5-bucket classifier — see module docstring.
    """
    try:
        from mt5_safe import mt5
        DEAL_REASON_SL = int(getattr(mt5, "DEAL_REASON_SL", 4))
        DEAL_REASON_TP = int(getattr(mt5, "DEAL_REASON_TP", 3))
        DEAL_REASON_CLIENT = int(getattr(mt5, "DEAL_REASON_CLIENT", 5))
        DEAL_REASON_EXPERT = int(getattr(mt5, "DEAL_REASON_EXPERT", 2))
    except Exception:
        # MT5 unavailable in test/import path. Fall back to canonical
        # numeric values documented in MT5 5.0.45.
        DEAL_REASON_SL, DEAL_REASON_TP = 4, 3
        DEAL_REASON_CLIENT, DEAL_REASON_EXPERT = 5, 2

    if not close_deals:
        return "no_close_deal", None

    # Use the last (newest) close deal's reason when partials chain.
    raw_reason: Optional[int] = None
    for d in close_deals:
        r = getattr(d, "reason", None)
        if r is not None:
            raw_reason = int(r)

    if raw_reason is None:
        return "unknown_deal_reason", None

    if raw_reason == DEAL_REASON_SL:
        return "broker_sl", raw_reason
    if raw_reason == DEAL_REASON_TP:
        return "broker_tp", raw_reason
    if raw_reason == DEAL_REASON_CLIENT:
        return "manual_mt5", raw_reason
    if raw_reason != DEAL_REASON_EXPERT:
        return f"raw_{raw_reason}", raw_reason

    # DEAL_REASON_EXPERT: try to attribute to a Snow-dispatched close
    # via snow_triggers. Match by plan_id + executed_at within ±5s of
    # close_time_iso. action_type ∈ {close_full, close_partial}.
    try:
        from snow import db as snow_db
        triggers = snow_db.list_triggers(plan_id=plan_id, limit=20)
    except Exception:
        triggers = []

    if not close_time_iso or not triggers:
        return "expert_unattributed", raw_reason

    try:
        import datetime as _dt
        close_dt = _dt.datetime.fromisoformat(
            close_time_iso.replace("Z", "+00:00")
        )
    except Exception:
        return "expert_unattributed", raw_reason

    for t in triggers:
        action_type = (t.get("action_type") or "").strip()
        if action_type not in ("close_full", "close_partial"):
            continue
        executed_at = t.get("executed_at")
        if not executed_at:
            continue
        try:
            t_dt = _dt.datetime.fromisoformat(
                str(executed_at).replace("Z", "+00:00")
            )
        except Exception:
            continue
        if abs((t_dt - close_dt).total_seconds()) <= 5.0:
            return "snow_close", raw_reason

    return "expert_unattributed", raw_reason


def _compute_mfe_pips(
    symbol: str,
    open_time_iso: str,
    close_time_iso: str,
    open_price: float,
    direction_sign: int,
    pip_size: float,
) -> tuple[Optional[float], str]:
    """Compute Maximum Favorable Excursion in pips between open and
    close. Returns (mfe_pips, status) where status is one of:
      - "ok"
      - "copy_rates_range_failed"
      - "empty_candle_range"
      - "computation_failed"
    Diagnostic-only — failures never propagate to caller.
    """
    try:
        import datetime as _dt
        import time as _time
        from mt5_safe import mt5
        # FLO-96 fix (2026-05-02 audit): copy_rates_range expects broker-local
        # naive datetimes. Parsing the ISO UTC and stripping tzinfo passed
        # naive UTC to MT5, which interpreted it as broker-time → bars from
        # ~3h before the trade actually happened (wrong window for MFE).
        # Convert UTC -> broker-stored unix -> naive via live tick offset
        # (same pattern as mfe_backfill._utc_to_broker_naive +
        # tick_pressure._broker_now).
        open_dt_utc = _dt.datetime.fromisoformat(
            open_time_iso.replace("Z", "+00:00")
        )
        close_dt_utc = _dt.datetime.fromisoformat(
            close_time_iso.replace("Z", "+00:00")
        )
        try:
            _t = mt5.symbol_info_tick("XAUUSD")
            _server_offset_s = (int(_t.time) - int(_time.time())) if (_t and _t.time) else 10800
        except Exception:
            _server_offset_s = 10800
        # Plausibility band: real broker offset is ~+3h. When market is
        # closed, tick.time is the last tick of the prior session and
        # `tick.time - now()` becomes wildly negative (saw -9.8h in the
        # 2026-05-02 audit). Fall back to the cached default.
        if not (7200 <= _server_offset_s <= 14400):
            _server_offset_s = 10800
        open_dt = _dt.datetime.fromtimestamp(int(open_dt_utc.timestamp()) + _server_offset_s)
        close_dt = _dt.datetime.fromtimestamp(int(close_dt_utc.timestamp()) + _server_offset_s)
        # Single attempt, no retry. Cost of MFE failure = null field;
        # cost of retry-induced latency = blocking close detection.
        rates = mt5.copy_rates_range(symbol, mt5.TIMEFRAME_M1, open_dt, close_dt)
    except Exception:
        return None, "copy_rates_range_failed"

    if rates is None or len(rates) == 0:
        return None, "empty_candle_range"

    try:
        if direction_sign == 1:  # BUY: max favorable = highest high
            extreme = max(float(r["high"]) for r in rates)
            mfe = (extreme - open_price) / pip_size
        else:  # SELL: max favorable = lowest low
            extreme = min(float(r["low"]) for r in rates)
            mfe = (open_price - extreme) / pip_size
        return float(mfe), "ok"
    except Exception:
        return None, "computation_failed"


def emit_scratch_and_volume_audit(
    plan_id: str,
    ticket: int,
    plan_row: dict[str, Any],
    in_deal: Any,
    close_deals: list[Any],
    open_price: float,
    vw_close_price: float,
    direction_sign: int,
    outcome_pips: float,
    pip_size: float,
    close_time_iso: Optional[str],
) -> None:
    """Emit snow.trade.scratch_pattern + snow.trade.volume_audit
    diagnostics for FLO-382 D2 + D3.

    Called by snow.outcome.backfill_outcome on success. All inputs
    come from data already collected in that path — this function
    adds zero MT5 calls except the optional MFE candle query.
    """
    try:
        # ---- D2: scratch pattern ----
        close_reason, raw_reason = _classify_close_reason(
            plan_id, close_deals, close_time_iso,
        )
        # SL at close moment — last close deal's `sl` attribute.
        sl_at_close: Optional[float] = None
        for d in close_deals:
            s = getattr(d, "sl", None)
            if s is not None:
                try:
                    sl_at_close = float(s)
                except Exception:
                    pass
        if sl_at_close is not None and sl_at_close > 0:
            sl_distance_pips = (sl_at_close - open_price) * direction_sign / pip_size
            be_was_locked = abs(sl_at_close - open_price) < (
                _BE_TOLERANCE_PIPS * pip_size
            )
        else:
            sl_distance_pips = None
            be_was_locked = False

        # Time-to-close
        try:
            import datetime as _dt
            in_time = getattr(in_deal, "time", None)
            close_times = [getattr(d, "time", None) for d in close_deals]
            close_times = [t for t in close_times if t is not None]
            if in_time is not None and close_times:
                tt_seconds = float(max(close_times) - in_time)
            else:
                tt_seconds = None
        except Exception:
            tt_seconds = None

        # MFE — single best-effort attempt. Anchor open time on the
        # MT5 entry-deal epoch (definitionally trade-open moment),
        # NOT on plan_row.entered_at which is the bot's clock at
        # broker-confirmation receipt and can drift by seconds.
        try:
            symbol = plan_row.get("symbol") or "XAUUSD"
        except Exception:
            symbol = "XAUUSD"

        in_deal_time = getattr(in_deal, "time", None)
        if in_deal_time and close_time_iso:
            try:
                import datetime as _dt
                open_time_iso = _dt.datetime.utcfromtimestamp(
                    int(in_deal_time)
                ).isoformat() + "Z"
            except Exception:
                open_time_iso = None
            if open_time_iso:
                mfe_pips, mfe_status = _compute_mfe_pips(
                    symbol=symbol,
                    open_time_iso=open_time_iso,
                    close_time_iso=close_time_iso,
                    open_price=open_price,
                    direction_sign=direction_sign,
                    pip_size=pip_size,
                )
            else:
                mfe_pips, mfe_status = None, "missing_timestamps"
        else:
            mfe_pips, mfe_status = None, "missing_timestamps"

        log.info(
            f"snow.trade.scratch_pattern "
            f"plan_id={plan_id} ticket={int(ticket)} "
            f"entry_price={open_price:.5f} "
            f"close_price={vw_close_price:.5f} "
            f"pip_distance_from_entry={outcome_pips:.2f} "
            f"close_reason={close_reason} "
            f"raw_deal_reason={raw_reason if raw_reason is not None else 'null'} "
            f"be_was_locked={str(be_was_locked).lower()} "
            f"sl_at_close_pips_from_entry="
            f"{('null' if sl_distance_pips is None else f'{sl_distance_pips:.2f}')} "
            f"mfe_during_trade="
            f"{('null' if mfe_pips is None else f'{mfe_pips:.2f}')} "
            f"mfe_query_status={mfe_status} "
            f"time_to_close_seconds="
            f"{('null' if tt_seconds is None else f'{tt_seconds:.0f}')}"
        )
    except Exception as e:
        try:
            log.warning(f"snow.trade.scratch_pattern emit failed: {e}")
        except Exception:
            pass

    try:
        # ---- D3: volume audit ----
        # Planned volume from plan_json.entry.volume
        planned_volume: Optional[float] = None
        try:
            import json as _json
            plan_json = plan_row.get("plan_json")
            if plan_json:
                pj = _json.loads(plan_json) if isinstance(plan_json, str) else plan_json
                planned_volume = float(pj.get("entry", {}).get("volume"))
        except Exception:
            planned_volume = None

        # Actual volume = IN deal volume (entry deal). Partial closes
        # don't change the entry volume.
        try:
            actual_volume = float(getattr(in_deal, "volume", 0.0))
        except Exception:
            actual_volume = None

        if planned_volume is not None and actual_volume is not None:
            mismatch = abs(planned_volume - actual_volume) > 0.001
        else:
            mismatch = None

        # Account balance at open: best-effort from plan_row meta
        # (optional). May be null on plans submitted before FLO-382's
        # capture path landed.
        balance = None
        try:
            import json as _json
            plan_json = plan_row.get("plan_json")
            if plan_json:
                pj = _json.loads(plan_json) if isinstance(plan_json, str) else plan_json
                # Optional meta block — not part of the validated schema,
                # captured by submit_plan_to_snow if available.
                meta = pj.get("meta") or {}
                if isinstance(meta, dict):
                    b = meta.get("account_balance_at_open")
                    if b is not None:
                        balance = float(b)
        except Exception:
            balance = None

        log.info(
            f"snow.trade.volume_audit "
            f"plan_id={plan_id} ticket={int(ticket)} "
            f"planned_volume="
            f"{('null' if planned_volume is None else f'{planned_volume:.2f}')} "
            f"actual_volume="
            f"{('null' if actual_volume is None else f'{actual_volume:.2f}')} "
            f"mismatch="
            f"{('null' if mismatch is None else str(mismatch).lower())} "
            f"account_balance_at_open="
            f"{('null' if balance is None else f'{balance:.2f}')}"
        )
    except Exception as e:
        try:
            log.warning(f"snow.trade.volume_audit emit failed: {e}")
        except Exception:
            pass
