"""FLO-403 Phase 2 — Trade Manager Agent tools.

Six tools, each scoped to deliver only the ALLOWED categories from
the CEO directive (FLO-403 §B.1). Two are NEW lean wrappers
(get_management_indicators, get_regime_stability_flag) that scope
existing AgentTools methods to the directive — the underlying methods
over-deliver (full multi-TF indicator suite, FLO-139 7-state regime
classification with confidence + 5-pillar breakdown), so wrapping
locks the contract at the data layer rather than relying on prompt
discipline.

The NEVER list (charts, S/R zones, Fibonacci, Pivots, Recipe Book,
Luna brief, Rex debate, Research Manager verdict, ML prediction,
trade history, journal, lessons, reflexions, plan-authoring tools)
is enforced BY CONSTRUCTION — those AgentTools methods are not
exposed on this class. The LLM cannot call what isn't in the tool
roster sent in the API request.

See data/_design/FLO-403_Phase2_Trade_Manager_Design.md §4 for the
full design + rationale.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


_REGIME_CACHE_PATH = os.path.join("data", "tm_regime_at_open.json")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_regime_cache() -> Dict[str, str]:
    """Read ticket → regime_at_open map. Missing or malformed file
    is a silent no-op (returns empty dict)."""
    try:
        if os.path.exists(_REGIME_CACHE_PATH):
            with open(_REGIME_CACHE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
    except Exception:
        pass
    return {}


def _write_regime_cache(cache: Dict[str, str]) -> None:
    """Atomic write — temp + os.replace."""
    try:
        os.makedirs(os.path.dirname(_REGIME_CACHE_PATH), exist_ok=True)
        tmp = _REGIME_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _REGIME_CACHE_PATH)
    except Exception:
        pass  # best-effort; missing cache → regime_changed=False (conservative)


class TradeManagerTools:
    """Tool surface for the Trade Manager Agent.

    Construction injects the live executor (mt5_safe-wrapped under
    FLO-348) and an AgentTools instance for read-only passthroughs.
    The TM does NOT inherit AgentTools — it composes — so the
    `not exposed` contract is enforced by the public surface of THIS
    class, not by inheritance discipline.
    """

    def __init__(self, *, executor, agent_tools=None, bot=None):
        """Init is intentionally CHEAP — no AgentTools construction.

        The lazy `_floki_tools` property builds AgentTools the first
        time it's needed (matching main.py's per-cycle construction
        pattern: bot + executor + safety_checks_module + risk_manager_module).
        Tests pass `agent_tools=<mock>` which short-circuits the
        property; production passes `bot=<TradingBot instance>`.
        """
        self._executor = executor
        self._bot = bot
        self._cached_floki_tools = agent_tools  # may be a stub or None

    @property
    def _floki_tools(self):
        """Composition: lazy-construct AgentTools on first access. Per
        Floki's per-cycle pattern (main.py:4764) we wire bot + executor
        + safety + risk. If the test path injected an `agent_tools`
        stub at __init__, this short-circuits and returns it directly."""
        if self._cached_floki_tools is None:
            from agent_tools import AgentTools
            import safety_checks
            import risk_manager
            self._cached_floki_tools = AgentTools(
                self._bot,
                executor=self._executor,
                safety_checks_module=safety_checks,
                risk_manager_module=risk_manager,
            )
        return self._cached_floki_tools

    # =========================================================================
    # 1. Inventory — passthrough (lean fields are already what we need)
    # =========================================================================

    def get_open_positions(self) -> Dict[str, Any]:
        """{positions: [...], count: int} — same shape AgentTools returns,
        which already includes `managed_by` and `comment`."""
        return self._floki_tools.get_open_positions()

    # =========================================================================
    # 2. Position state — Snow-side MFE/MAE/age + lean plan reference
    # =========================================================================

    def get_position_state(self, ticket: int) -> Dict[str, Any]:
        """Lean Snow-side state for one ticket. Returns:

            {ticket, mfe_pips, mae_pips, age_minutes, managed_by,
             plan_id, plan_thesis, contingencies_remaining}

        contingencies_remaining is a list of NAMES only — TM doesn't
        need the conditions/actions sub-tree. The full plan dict stays
        in Snow's storage and is never exposed to the prompt.

        Failure mode: any error in any sub-fetch returns the field as
        None rather than crashing the whole call. The TM's prompt
        builder treats None as 'n/a'.
        """
        out: Dict[str, Any] = {
            "ticket": int(ticket) if ticket is not None else None,
            "mfe_pips": None,
            "mae_pips": None,
            "age_minutes": None,
            "managed_by": "floki",
            "plan_id": None,
            "plan_thesis": None,
            "contingencies_remaining": [],
        }
        # --- MT5-side: position lookup for managed_by + age ---
        try:
            positions = self._executor.get_open_positions() or []
            for p in positions:
                if int(getattr(p, "ticket", 0) or 0) != int(ticket):
                    continue
                comment = str(getattr(p, "comment", "") or "")
                out["managed_by"] = (
                    "snow" if comment.startswith("snow:") else "floki"
                )
                # age_minutes from open_time if available
                ot = getattr(p, "open_time", None)
                if ot is not None:
                    try:
                        if isinstance(ot, (int, float)):
                            opened_at = datetime.fromtimestamp(
                                float(ot), tz=timezone.utc,
                            )
                        else:
                            s = str(ot).replace("Z", "+00:00")
                            opened_at = datetime.fromisoformat(s)
                            if opened_at.tzinfo is None:
                                opened_at = opened_at.replace(tzinfo=timezone.utc)
                        delta = datetime.now(timezone.utc) - opened_at
                        out["age_minutes"] = int(delta.total_seconds() / 60)
                    except Exception:
                        pass
                break
        except Exception:
            pass

        # --- Snow-side: plan + contingencies ---
        try:
            from snow import db as snow_db
            plan_id = None
            for p_row in snow_db.list_plans_by_status(("active",), limit=200):
                if p_row.get("trade_ticket") == int(ticket):
                    plan_id = p_row.get("id")
                    break
            if plan_id:
                out["plan_id"] = plan_id
                try:
                    plan = snow_db.get_plan_as_model(plan_id)
                    if plan is not None:
                        out["plan_thesis"] = (
                            plan.analysis.thesis if plan.analysis else None
                        )
                        # Names of management + exit contingencies that
                        # haven't fired yet (state != FIRED).
                        remaining: List[str] = []
                        for c in (list(plan.management) + list(plan.exit)):
                            if getattr(c, "state", None) != "fired":
                                remaining.append(c.name)
                        out["contingencies_remaining"] = remaining
                except Exception:
                    pass
        except Exception:
            pass

        # --- Snow state_cache: MFE/MAE pips (best-effort) ---
        try:
            from snow import state_cache  # may not be available in all builds
            cache = state_cache.read_position_cache(int(ticket))
            if isinstance(cache, dict):
                out["mfe_pips"] = _safe_float(cache.get("mfe_pips"))
                out["mae_pips"] = _safe_float(cache.get("mae_pips"))
        except Exception:
            pass

        return out

    # =========================================================================
    # 3. Management indicators — NEW lean wrapper (drops D1/H4/H1)
    # =========================================================================

    def get_management_indicators(self) -> Dict[str, Any]:
        """ONLY the indicators a HOLD/ADJUST/CLOSE decision needs.

        Returns:
            {M5: {RSI, Stoch_K, Stoch_D, ATR, recent_closes: [last 10]},
             M15: {RSI, Stoch_K, Stoch_D, MACD_histogram, ATR}}

        DROPS D1/H4/H1 keys from the underlying get_indicators output.
        Per FLO-403 §B.1 — multi-TF analytical breadth (D1/H4 EMAs,
        higher-TF divergence) is plan-authoring vocabulary, not
        management vocabulary.

        recent_closes: last 10 M5 close prices, oldest-first. Empty
        list if the multi-TF indicators dict doesn't carry them.
        """
        out: Dict[str, Any] = {"M5": {}, "M15": {}}

        for tf in ("M5", "M15"):
            try:
                ind = self._floki_tools.get_indicators(timeframe=tf)
                if not isinstance(ind, dict):
                    continue
                slot = out[tf]
                slot["RSI"] = _safe_float(ind.get("rsi"))
                # Stochastic shape varies; tolerate dict-or-flat.
                stoch = ind.get("stochastic") or ind.get("stoch") or {}
                if isinstance(stoch, dict):
                    slot["Stoch_K"] = _safe_float(stoch.get("k"))
                    slot["Stoch_D"] = _safe_float(stoch.get("d"))
                else:
                    slot["Stoch_K"] = None
                    slot["Stoch_D"] = None
                slot["ATR"] = _safe_float(ind.get("atr"))
                if tf == "M15":
                    macd = ind.get("macd") or {}
                    if isinstance(macd, dict):
                        slot["MACD_histogram"] = _safe_float(macd.get("histogram"))
                    else:
                        slot["MACD_histogram"] = None
                if tf == "M5":
                    closes = ind.get("recent_closes") or []
                    if isinstance(closes, list):
                        slot["recent_closes"] = [
                            _safe_float(c) for c in closes[-10:]
                        ]
                    else:
                        slot["recent_closes"] = []
            except Exception:
                continue
        return out

    # =========================================================================
    # 4. Regime stability flag — NEW reductive wrapper
    # =========================================================================

    def get_regime_stability_flag(self, ticket: int) -> Dict[str, Any]:
        """{regime_changed: bool, current: <regime>, at_open: <regime>}

        ONLY the bool reaches the prompt; current / at_open are carried
        for telemetry. The regime-at-open is cached on first sighting
        per ticket (file-based, atomic writes) — bot restart preserves
        it. Per-ticket cache invalidation happens when get_open_positions
        no longer includes the ticket (cleared by daemon on a stale-pass).

        Conservative on missing data: if either fetch fails, returns
        regime_changed=False (assume stable, don't trigger TM action
        on a phantom regime flip).
        """
        out: Dict[str, Any] = {
            "regime_changed": False,
            "current": None,
            "at_open": None,
        }
        try:
            regime_data = self._floki_tools.get_market_regime()
            current = (
                regime_data.get("regime") if isinstance(regime_data, dict)
                else None
            )
        except Exception:
            current = None
        if not current:
            return out
        out["current"] = current

        cache = _read_regime_cache()
        key = str(int(ticket)) if ticket is not None else None
        if key is None:
            return out

        if key in cache:
            out["at_open"] = cache[key]
            out["regime_changed"] = (cache[key] != current)
        else:
            # First sighting — stamp current as at_open. NB: this is
            # "regime at first TM look" not strictly "regime at trade
            # open"; the TM is invoked early after PENDING_FILL so the
            # gap is minimal. Documented caveat in the design.
            cache[key] = current
            _write_regime_cache(cache)
            out["at_open"] = current

        return out

    def clear_regime_cache_for_ticket(self, ticket: int) -> None:
        """Remove a stale ticket entry from the regime cache. Daemon
        calls this when a ticket leaves the open-positions inventory."""
        cache = _read_regime_cache()
        key = str(int(ticket)) if ticket is not None else None
        if key and key in cache:
            del cache[key]
            _write_regime_cache(cache)

    # =========================================================================
    # 5. Echo CRITICAL — filter to severity + since-trade-open
    # =========================================================================

    def get_echo_critical(self, since_iso: Optional[str] = None) -> List[dict]:
        """Filter underlying echo_alerts to severity=CRITICAL since the
        trade-open timestamp. Empty list usually.

        since_iso: ISO-8601 UTC timestamp; alerts with `time` < since
        are dropped. None → no time filter (fail-open on a missing
        anchor — the LLM gets all CRITICALs which is still cheap).
        """
        try:
            data = self._floki_tools.get_echo_alerts()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        alerts = data.get("alerts") or data.get("events") or []
        if not isinstance(alerts, list):
            return []
        out: List[dict] = []
        for a in alerts:
            if not isinstance(a, dict):
                continue
            sev = str(a.get("severity") or "").upper()
            if sev != "CRITICAL":
                continue
            # Time filter
            if since_iso:
                t = str(a.get("time") or a.get("timestamp") or "")
                if t and t < since_iso:
                    continue
            out.append({
                "time": a.get("time") or a.get("timestamp"),
                "severity": "CRITICAL",
                "headline": a.get("headline") or a.get("summary"),
                "source": a.get("source"),
            })
        return out

    # =========================================================================
    # 6. Execute paths — caller-aware Phase 1 guard (Q10.1 Option A)
    # =========================================================================
    # TM passes caller_role="trade_manager" to AgentTools.close_trade /
    # adjust_trade so the Phase 1 Snow ownership guard recognizes the
    # authorized executor and lets the call through. Floki callers use
    # the default ("floki") and remain blocked on snow:* positions.
    #
    # In shadow mode (TRADE_MANAGER_ENABLED=False — default) these are
    # never invoked by the daemon — TradeManager._dispatch logs the
    # decision instead.

    def close_trade(self, ticket: int, reason: str) -> Dict[str, Any]:
        """Close a position. `reason` is a TM-side rationale string
        carried into the result for telemetry. Bypasses the Phase 1
        Snow ownership guard via caller_role='trade_manager'."""
        result = self._floki_tools.close_trade(
            int(ticket), caller_role="trade_manager",
        )
        if isinstance(result, dict):
            result["tm_reason"] = str(reason or "")[:200]
        return result

    def adjust_trade(
        self, ticket: int, new_sl: float, new_tp: float, reason: str,
    ) -> Dict[str, Any]:
        """SL/TP adjustment. Same caller_role bypass as close_trade."""
        result = self._floki_tools.adjust_trade(
            int(ticket),
            new_sl=float(new_sl),
            new_tp=float(new_tp),
            caller_role="trade_manager",
        )
        if isinstance(result, dict):
            result["tm_reason"] = str(reason or "")[:200]
        return result


__all__ = ["TradeManagerTools"]
