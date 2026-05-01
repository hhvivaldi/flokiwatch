"""FLO-403 Phase 2 — Trade Manager Agent daemon.

Cheap LLM (Qwen 3.6-Plus by default) that supervises OPEN trades.
Reactive, single-call-per-cycle: gather context server-side via 6
tools (no in-LLM tool loop), then ONE chat completion that returns
strict decision JSON.

Decision space: HOLD_TRADE | ADJUST_TRADE | CLOSE_TRADE | NO_OP.
NO authoring, NO opening positions, NO calls to anything outside
TradeManagerTools.

Lifecycle:
  - Instantiated once per bot lifetime (alongside AIAgent).
  - main.py routes SIMBA_WAKE / SIMBA_WATCH / PENDING_FILL /
    TM_CHECK / TM_HEARTBEAT events here via run_cycle().
  - Concurrency: per-instance RLock; second event during an
    in-flight cycle returns {"reason": "tm_cycle_in_progress"} —
    same lossy-by-design pattern as Floki's proactive_lock.

Failure modes (all → NO_OP, no Floki fallback):
  - LLM timeout / provider down
  - Response parse failure
  - Tool fetch raises (best-effort fields populated as None)
The deterministic safety net (monitor.py BE/trail/drawdown,
Snow exit + management contingencies, runtime_reconcile) covers
everything the TM might miss.

Shadow vs production:
  - TRADE_MANAGER_ENABLED=False (default) — daemon runs the LLM
    cycle, logs the decision, but does NOT dispatch to broker.
  - TRADE_MANAGER_ENABLED=True — decisions execute via
    TradeManagerTools.close_trade / adjust_trade.

See data/_design/FLO-403_Phase2_Trade_Manager_Design.md §3 for
the full design.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from logger import log
from trade_manager_prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    parse_decision_json,
)
from trade_manager_tools import TradeManagerTools


# Single-call timeout — Qwen p50 ~85s today; 120s gives slack
# without blocking Snow's 5s tick on a stuck request.
_LLM_TIMEOUT_SECONDS = 120


def _utc_iso(ts: Optional[float] = None) -> str:
    from datetime import datetime, timezone
    dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TradeManager:
    """Daemon class. Stateless across cycles except for the cycle lock
    and the trade-open-time map (used to bound the get_echo_critical
    `since_iso` filter to per-ticket open time)."""

    def __init__(
        self,
        *,
        executor,
        model: str,
        api_base: str,
        api_key: str,
        shadow_mode: bool,
        bot=None,
        agent_tools=None,
    ):
        self._executor = executor
        self._bot = bot
        self._model = model
        self._api_base = api_base
        self._api_key = api_key
        self._shadow = bool(shadow_mode)
        self._tools = TradeManagerTools(
            executor=executor, agent_tools=agent_tools, bot=bot,
        )
        self._cycle_lock = threading.RLock()
        # ticket → ISO-8601 UTC trade-open time, used for the
        # get_echo_critical since_iso filter. Best-effort; missing
        # entries fall back to no time filter.
        self._open_time_map: Dict[int, str] = {}
        self._client = None  # lazy — built on first run_cycle

        log.info(
            f"TRADE_MANAGER | initialized | model={model} | "
            f"shadow={'YES' if shadow_mode else 'NO (PRODUCTION)'} | "
            f"endpoint={api_base}"
        )

    # =========================================================================
    # Public entry point — called by main.py trigger router
    # =========================================================================

    def run_cycle(
        self, trigger_type: str, trigger_data: Optional[dict] = None,
    ) -> Dict[str, Any]:
        """Single TM cycle.

        Returns:
          {success: bool, decision: <DECISION>, reason: <str>,
           executed: bool, latency_ms: int, ...}

        Concurrency: holds `_cycle_lock` for the duration. A second
        call during an in-flight cycle returns immediately with
        reason="tm_cycle_in_progress" — same shape as Floki's
        proactive_lock. Lossy by design; heartbeat catches up.
        """
        if not self._cycle_lock.acquire(blocking=False):
            return {
                "success": False,
                "reason": "tm_cycle_in_progress",
                "decision": "NO_OP",
                "executed": False,
            }
        t0 = time.time()
        try:
            # --- 1. Gather context (server-side, no LLM yet) ---
            ctx = self._gather_context(trigger_type, trigger_data or {})
            # FLO-418: pass through to LLM if EITHER a position is open
            # OR Snow has an awaiting opposing-decision. Pre-FLO-418 we
            # short-circuited on no-position, but TM now also decides
            # on opposing plans (cancel / override) which can fire
            # AFTER a position has just closed.
            if not ctx.get("position") and not ctx.get("awaiting_decisions"):
                latency_ms = int((time.time() - t0) * 1000)
                log.info(
                    f"TM_CYCLE | trigger={trigger_type} | NO_OP | "
                    f"reason=no_open_position | {latency_ms}ms"
                )
                return {
                    "success": True,
                    "decision": "NO_OP",
                    "reason": "no_open_position",
                    "executed": False,
                    "latency_ms": latency_ms,
                }

            # --- 2. Build prompts ---
            user_msg = build_user_prompt(ctx)

            # --- 3. LLM call ---
            try:
                raw = self._call_llm(SYSTEM_PROMPT, user_msg)
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                log.warning(
                    f"TM_CYCLE | trigger={trigger_type} | LLM_FAILED | "
                    f"{type(e).__name__}: {e} | {latency_ms}ms — defaulting NO_OP"
                )
                return {
                    "success": False,
                    "reason": f"llm_failed:{type(e).__name__}",
                    "decision": "NO_OP",
                    "executed": False,
                    "latency_ms": latency_ms,
                }

            # --- 4. Parse strict decision JSON ---
            decision = parse_decision_json(raw)
            if decision is None:
                latency_ms = int((time.time() - t0) * 1000)
                log.warning(
                    f"TM_CYCLE | trigger={trigger_type} | PARSE_FAILED | "
                    f"raw[:200]={raw[:200]!r} | {latency_ms}ms — defaulting NO_OP"
                )
                return {
                    "success": False,
                    "reason": "parse_failed",
                    "decision": "NO_OP",
                    "executed": False,
                    "latency_ms": latency_ms,
                }

            # --- 5. Dispatch (or shadow-log) ---
            executed = self._dispatch(decision, ctx)
            latency_ms = int((time.time() - t0) * 1000)
            log.info(
                f"TM_CYCLE | trigger={trigger_type} | "
                f"decision={decision['decision']} | "
                f"reason={decision.get('reason', '')[:80]!r} | "
                f"shadow={self._shadow} | executed={executed} | {latency_ms}ms"
            )

            # FLO-419 follow-up: persist decision to agent_events so the
            # Trade Room UI surfaces TM activity. Author "QWEN_TM"
            # distinguishes TM cycles from Floki's FLOKI_DECISION rows;
            # the existing _build_trade_room_messages reads agent_events
            # and forwards the `author` field to the frontend without
            # change, so this is the only wire-up needed.
            self._persist_decision_event(
                decision=decision,
                ctx=ctx,
                trigger_type=trigger_type,
                executed=executed,
                latency_ms=latency_ms,
            )

            return {
                "success": True,
                **decision,
                "executed": executed,
                "latency_ms": latency_ms,
            }
        finally:
            self._cycle_lock.release()

    # =========================================================================
    # Internals
    # =========================================================================

    def _persist_decision_event(
        self,
        *,
        decision: Dict[str, Any],
        ctx: Dict[str, Any],
        trigger_type: str,
        executed: bool,
        latency_ms: int,
    ) -> None:
        """Write the TM cycle's decision to agent_events so the Trade
        Room dashboard surfaces it. Never throws — logs and swallows."""
        try:
            decision_type = str(decision.get("decision") or "NO_OP").upper()
            reason = str(decision.get("reason") or "")[:200]

            pos = ctx.get("position") or {}
            ticket = pos.get("ticket")

            # Human-readable summary line. The Trade Room renders
            # `content` directly; keep it scannable.
            ticket_tag = f"#{ticket}" if ticket else "no-position"
            shadow_tag = " [SHADOW]" if self._shadow else ""
            exec_tag = "" if executed or decision_type == "NO_OP" else " (not executed)"
            content = (
                f"{decision_type} {ticket_tag}{shadow_tag}{exec_tag}: {reason}"
            )

            payload = {
                "decision": decision_type,
                "reason": reason,
                "executed": bool(executed),
                "shadow": bool(self._shadow),
                "latency_ms": int(latency_ms),
                "trigger_type": trigger_type,
                "ticket": ticket,
                "new_sl": decision.get("new_sl"),
                "new_tp": decision.get("new_tp"),
                "plan_id": decision.get("plan_id"),
                "current_sl": pos.get("sl"),
                "current_tp": pos.get("tp"),
                "unrealised_pips": pos.get("unrealised_pips"),
                "unrealised_usd": pos.get("current_pnl"),
            }

            from db_writer import record_agent_event
            record_agent_event(
                event_type=f"TM_{decision_type}",
                content=content,
                payload=payload,
                author="QWEN_TM",
            )
        except Exception as e:
            log.warning(f"TM: persist_decision_event failed: {e}")

    def _gather_context(
        self, trigger_type: str, trigger_data: dict,
    ) -> Dict[str, Any]:
        """Six tool calls + per-ticket selection. No LLM. Failure on
        any sub-fetch yields a None field — the prompt builder treats
        None as 'n/a'."""
        ctx: Dict[str, Any] = {
            "position": None,
            "plan": {},
            "market": {},
            "trigger": {"type": trigger_type, "data": trigger_data},
            "awaiting_decisions": [],
        }

        # FLO-418: gather any plans Snow is holding pending Floki/TM
        # decision (opposing position detected). Always populated —
        # TM may have NO open position but still need to decide on
        # an awaiting plan.
        try:
            from snow.db import list_plans_with_awaiting_decision
            ctx["awaiting_decisions"] = list_plans_with_awaiting_decision()
        except Exception as e:
            log.debug(
                f"TM_GATHER | list_plans_with_awaiting_decision failed: {e}"
            )
            ctx["awaiting_decisions"] = []

        # 1. Inventory — pick first managed position. Multi-position
        # case is bounded (max 3 per CLAUDE.md); for v1 the TM cycles
        # one position per call. The trigger router fires per-ticket
        # for TM_CHECK so this naturally serializes.
        try:
            inv = self._tools.get_open_positions() or {}
            positions = inv.get("positions") or []
        except Exception as e:
            log.debug(f"TM_GATHER | get_open_positions failed: {e}")
            positions = []
        if not positions:
            # No open position. If there ARE awaiting decisions, ctx
            # already carries them — run_cycle will pass through to
            # the LLM only if awaiting_decisions is non-empty.
            return ctx

        # Prefer the trigger's ticket if provided; otherwise first.
        target_ticket = None
        if isinstance(trigger_data, dict):
            tt = trigger_data.get("ticket")
            if tt is not None:
                try:
                    target_ticket = int(tt)
                except Exception:
                    target_ticket = None
        chosen = None
        if target_ticket is not None:
            for p in positions:
                if int(p.get("ticket") or 0) == target_ticket:
                    chosen = p
                    break
        if chosen is None:
            chosen = positions[0]
        ctx["position"] = dict(chosen)
        ticket = int(chosen.get("ticket") or 0)

        # Stamp open time on first sighting (used for echo since-filter).
        if ticket and ticket not in self._open_time_map:
            self._open_time_map[ticket] = _utc_iso()

        # 2. Position state (lean Snow-side view)
        try:
            state = self._tools.get_position_state(ticket)
            if isinstance(state, dict):
                ctx["position"]["mfe_pips"] = state.get("mfe_pips")
                ctx["position"]["mae_pips"] = state.get("mae_pips")
                if state.get("age_minutes") is not None:
                    ctx["position"]["age_minutes"] = state.get("age_minutes")
                ctx["plan"] = {
                    "plan_id": state.get("plan_id"),
                    "thesis": state.get("plan_thesis"),
                    "contingencies_remaining": state.get(
                        "contingencies_remaining", []
                    ),
                }
        except Exception as e:
            log.debug(f"TM_GATHER | get_position_state failed: {e}")

        # 3. Management indicators (M5 + M15 only)
        try:
            ctx["market"]["indicators"] = self._tools.get_management_indicators()
        except Exception as e:
            log.debug(f"TM_GATHER | get_management_indicators failed: {e}")
            ctx["market"]["indicators"] = {"M5": {}, "M15": {}}

        # 4. Regime stability flag
        try:
            regime = self._tools.get_regime_stability_flag(ticket)
            ctx["market"]["regime_changed"] = bool(regime.get("regime_changed"))
        except Exception as e:
            log.debug(f"TM_GATHER | get_regime_stability_flag failed: {e}")
            ctx["market"]["regime_changed"] = False

        # 5. Echo CRITICAL since trade open
        since_iso = self._open_time_map.get(ticket)
        try:
            ctx["market"]["echo_critical_since_open"] = (
                self._tools.get_echo_critical(since_iso=since_iso)
            )
        except Exception as e:
            log.debug(f"TM_GATHER | get_echo_critical failed: {e}")
            ctx["market"]["echo_critical_since_open"] = []

        # current_price — real MT5 tick. Critical for ADJUST/CLOSE
        # decisions: TM must know where price is relative to entry/SL/TP.
        # Use the close-side price (bid for BUY, ask for SELL) — that's
        # the price the position would actually close at, and what
        # MFE/MAE/PnL are computed against. Fallback to entry on tick
        # failure (broker disconnect, weekend) — keeps prompt parseable;
        # downstream prompt notes "approx" when fallback fires.
        try:
            _prices = self._executor.get_current_price()  # (bid, ask) or None
            if _prices is not None:
                bid, ask = _prices
                _dir = str(chosen.get("direction") or "").upper().strip()
                ctx["market"]["current_price"] = bid if _dir == "BUY" else ask
                ctx["market"]["current_price_source"] = "mt5_tick"
            else:
                ctx["market"]["current_price"] = float(chosen.get("entry") or 0)
                ctx["market"]["current_price_source"] = "entry_fallback"
        except Exception as e:
            log.debug(f"TM_GATHER | current_price fetch failed: {e}")
            try:
                ctx["market"]["current_price"] = float(chosen.get("entry") or 0)
            except Exception:
                ctx["market"]["current_price"] = 0.0
            ctx["market"]["current_price_source"] = "entry_fallback"
        # unrealised_pips — derive from current_pnl + volume if useful.
        # v1 leaves it None and the LLM works with current_pnl USD.
        ctx["position"]["unrealised_pips"] = None

        return ctx

    def _call_llm(self, system_msg: str, user_msg: str) -> str:
        """Single chat completion. JSON-mode hint via response_format
        works on Qwen + Kimi + Gemini OpenAI-compat endpoints. Lazy
        client init."""
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._api_base,
                timeout=_LLM_TIMEOUT_SECONDS,
            )
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
            max_tokens=300,
        )
        msg = resp.choices[0].message
        return getattr(msg, "content", "") or ""

    def _dispatch(self, decision: dict, ctx: dict) -> bool:
        """Return True iff the decision actually executed against the
        broker. Shadow mode never executes."""
        d = decision["decision"]
        if d == "NO_OP" or d == "HOLD_TRADE":
            return False
        # CLOSE_TRADE / ADJUST_TRADE / CANCEL_PLAN / OVERRIDE_OPPOSING_BLOCK
        # are all execute-class.
        if self._shadow:
            log.info(
                f"TRADE_MANAGER_SHADOW | would_{d} | "
                f"reason={decision.get('reason', '')!r} | "
                f"NOT executed (TRADE_MANAGER_ENABLED=False)"
            )
            return False

        # FLO-418: opposing-decision branches don't need the open
        # position's ticket — they target a specific awaiting plan_id.
        if d in ("CANCEL_PLAN", "OVERRIDE_OPPOSING_BLOCK"):
            plan_id = str(decision.get("plan_id") or "").strip()
            if not plan_id:
                log.warning(
                    f"TRADE_MANAGER_EXECUTE | {d} | missing plan_id; "
                    f"NOT executed"
                )
                return False
            try:
                if d == "CANCEL_PLAN":
                    result = self._tools.cancel_plan(
                        plan_id, reason=decision.get("reason", ""),
                    )
                else:
                    result = self._tools.override_opposing_block(
                        plan_id, reason=decision.get("reason", ""),
                    )
                ok = bool(isinstance(result, dict) and result.get("success"))
                log.info(
                    f"TRADE_MANAGER_EXECUTE | {d} | plan_id={plan_id} | "
                    f"success={ok} | result={result}"
                )
                return ok
            except Exception as e:
                log.error(
                    f"TRADE_MANAGER_EXECUTE | {d} | plan_id={plan_id} | "
                    f"raised {type(e).__name__}: {e}"
                )
                return False

        # CLOSE_TRADE / ADJUST_TRADE need an open position ticket.
        ticket = int((ctx.get("position") or {}).get("ticket") or 0)
        if not ticket:
            return False
        try:
            if d == "CLOSE_TRADE":
                result = self._tools.close_trade(
                    ticket, reason=decision.get("reason", ""),
                )
            else:  # ADJUST_TRADE
                result = self._tools.adjust_trade(
                    ticket,
                    new_sl=decision["new_sl"],
                    new_tp=decision["new_tp"],
                    reason=decision.get("reason", ""),
                )
            ok = bool(isinstance(result, dict) and result.get("success"))
            log.info(
                f"TRADE_MANAGER_EXECUTE | {d} | ticket={ticket} | "
                f"success={ok} | result={result}"
            )
            return ok
        except Exception as e:
            log.error(
                f"TRADE_MANAGER_EXECUTE | {d} | ticket={ticket} | "
                f"raised {type(e).__name__}: {e}"
            )
            return False


# =============================================================================
# Module-level singleton — same pattern as ai_agent.initialize_agent
# =============================================================================

_tm_instance: Optional["TradeManager"] = None


def initialize_trade_manager(executor, bot=None) -> bool:
    """Initialize the global TradeManager singleton from config. Returns
    True iff the daemon is constructed; False if config disables it
    or instantiation raises.

    `bot` is the TradingBot reference threaded into the per-cycle
    AgentTools construction (matches Floki's pattern at main.py:4764).
    Optional in tests; production passes self.

    Called by main.py at startup, alongside initialize_agent. A failed
    TM init logs a warning and the bot continues; trigger routing
    falls through to the not-initialized debug log."""
    global _tm_instance
    try:
        import config as _cfg
        _tm_instance = TradeManager(
            executor=executor,
            bot=bot,
            model=_cfg.TRADE_MANAGER_MODEL,
            api_base=_cfg.TRADE_MANAGER_API_BASE,
            api_key=_cfg.TRADE_MANAGER_API_KEY,
            shadow_mode=not bool(_cfg.TRADE_MANAGER_ENABLED),
        )
        return True
    except Exception as e:
        log.warning(f"TRADE_MANAGER | initialize_trade_manager failed: {e}")
        _tm_instance = None
        return False


def get_trade_manager() -> Optional["TradeManager"]:
    """Return the singleton or None if not initialized. Trigger router
    at main.py uses this to dispatch."""
    return _tm_instance


__all__ = ["TradeManager", "initialize_trade_manager", "get_trade_manager"]
