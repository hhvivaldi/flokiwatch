import json
import os
import time
import threading
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple

from logger import log


class AgentMonitor:
    def __init__(self, bot=None):
        self.bot = bot
        self.entry_conditions: Optional[Dict[str, Any]] = None
        self.entry_conditions_timestamp: Optional[str] = None
        self.last_trigger_times: Dict[str, float] = {}
        self.last_price_used: Optional[float] = None
        self.last_trade_pnl_points: Optional[float] = None
        self.max_profit_seen_points_by_ticket: Dict[int, float] = {}
        self._last_drawdown_track_log_ts_by_ticket: Dict[int, float] = {}
        self.recent_prices: List[Tuple[float, float]] = []
        self.session_last_trigger_date: Dict[str, str] = {}
        self._simba_template_idx: int = 0
        self._last_simba_summary_ts: float = 0.0
        self._last_simba_eval_ts: float = 0.0
        self._simba_5m_high: Optional[float] = None
        self._simba_5m_low: Optional[float] = None
        self._simba_5m_first_price: Optional[float] = None
        self._last_stale_db_active_trade_log_ts: float = 0.0
        self._condition_velocity: Dict[str, Dict[str, float]] = {}  # FLO-66: {key: {"prev_value": X, "prev_time": ts}}

    def check(self) -> None:
        """Run Agent monitor checks (called every ~60 seconds)."""
        try:
            # Reconcile cached per-ticket state against live MT5 positions (source of truth)
            try:
                from executor import executor

                live_positions = executor.get_open_positions() or []
                live_tickets = set()
                for p in live_positions:
                    try:
                        t = getattr(p, "ticket", None)
                        if t is not None:
                            live_tickets.add(int(t))
                    except Exception:
                        continue

                if live_tickets:
                    try:
                        stale = [t for t in list(self.max_profit_seen_points_by_ticket.keys()) if int(t) not in live_tickets]
                        for t in stale:
                            self.max_profit_seen_points_by_ticket.pop(t, None)
                    except Exception:
                        pass
                    try:
                        stale = [t for t in list(self._last_drawdown_track_log_ts_by_ticket.keys()) if int(t) not in live_tickets]
                        for t in stale:
                            self._last_drawdown_track_log_ts_by_ticket.pop(t, None)
                    except Exception:
                        pass
            except Exception:
                pass

            tick_mid = None
            try:
                tick_mid = self._mid_price()
            except Exception:
                tick_mid = None

            trade_summary = "none"
            dist_sl_str = "n/a"
            dist_tp_str = "n/a"
            try:
                trade = self._get_active_trade()
                if trade:
                    trade_summary = self._format_trade_summary(trade)
                    dist_sl, dist_tp = self._compute_trade_distances(trade)
                    if dist_sl is not None:
                        dist_sl_str = f"{dist_sl:.1f}"
                    if dist_tp is not None:
                        dist_tp_str = f"{dist_tp:.1f}"
            except Exception:
                pass

            conditions_status = "none"
            try:
                latest = self._load_latest_entry_conditions()
                if latest and isinstance(latest.get("entry_conditions"), dict):
                    conditions_status = "present"
            except Exception:
                pass

            upcoming_high = "none"
            try:
                upcoming_high = self._get_upcoming_high_event_summary()
            except Exception:
                upcoming_high = "none"

            _ = trade_summary
            _ = dist_sl_str
            _ = dist_tp_str
            _ = conditions_status
            _ = upcoming_high

            try:
                self._check_trade_at_risk()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | trade-risk error (ignored): {e}")

            try:
                self._check_calendar_events()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | calendar error (ignored): {e}")

            try:
                self._check_breakout()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | breakout error (ignored): {e}")

            try:
                self._check_session_change()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | session error (ignored): {e}")

            try:
                self._check_profit_drawdown()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | profit-drawdown error (ignored): {e}")

            try:
                self._check_simba_wake_conditions()
            except Exception as e:
                log.debug(f"AGENT_MONITOR | simba error (ignored): {e}")

            try:
                latest = self._load_latest_entry_conditions()
                if not latest:
                    return

                entry_conditions = latest.get("entry_conditions")
                if not isinstance(entry_conditions, dict):
                    return

                self.entry_conditions = entry_conditions
                self.entry_conditions_timestamp = latest.get("timestamp")

                if self._is_expired(self.entry_conditions_timestamp, entry_conditions):
                    return

                self._check_entry_conditions(entry_conditions)
            except Exception as e:
                log.debug(f"AGENT_MONITOR | entry-conditions error (ignored): {e}")
        except Exception as e:
            log.debug(f"AGENT_MONITOR | check error (ignored): {e}")

    def _wake_conditions_path(self) -> str:
        import os

        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_wake_conditions.json")

    def _load_wake_conditions(self) -> Dict[str, Any]:
        import json
        import os
        import time
        from datetime import datetime

        path = self._wake_conditions_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_wake_conditions(self, payload: Dict[str, Any]) -> bool:
        import json
        import os

        path = self._wake_conditions_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _safe_float(self, x: Any) -> Optional[float]:
        try:
            if x is None:
                return None
            return float(x)
        except Exception:
            return None

    def _next_simba_template(self, templates: List[str]) -> str:
        if not templates:
            return ""
        try:
            idx = int(self._simba_template_idx) % len(templates)
        except Exception:
            idx = 0
        self._simba_template_idx += 1
        return templates[idx]

    def _format_condition_for_message(self, cond: Dict[str, Any], idx: int, scanner_data: Dict[str, Any]) -> Dict[str, Any]:
        ctype = str(cond.get("type") or "").strip()
        desc = str(cond.get("description") or "").strip()

        current_price = self._safe_float(scanner_data.get("current_price"))
        indicators = scanner_data.get("indicators") if isinstance(scanner_data.get("indicators"), dict) else {}
        current_rsi = self._safe_float(scanner_data.get("current_rsi") if scanner_data.get("current_rsi") is not None else indicators.get("rsi"))
        current_adx = self._safe_float(scanner_data.get("current_adx") if scanner_data.get("current_adx") is not None else indicators.get("adx"))
        current_volume = self._safe_float(scanner_data.get("current_volume") or scanner_data.get("volume"))
        h1_volume = self._safe_float(scanner_data.get("h1_volume") or scanner_data.get("last_h1_tick_volume"))

        threshold = None
        current_value = None
        level = None

        if ctype in ("price_above", "price_below"):
            threshold = self._safe_float(cond.get("level") if cond.get("level") is not None else cond.get("value"))
            current_value = current_price
            level = threshold
        elif ctype == "h1_volume_above":
            threshold = self._safe_float(cond.get("threshold") if cond.get("threshold") is not None else cond.get("value"))
            current_value = h1_volume
        elif ctype in ("indicator_above", "indicator_below"):
            threshold = self._safe_float(cond.get("threshold") if cond.get("threshold") is not None else cond.get("value"))
            ind = str(cond.get("indicator") or "").strip().lower()
            current_value = self._safe_float(indicators.get(ind)) if ind else None
        elif ctype in ("rsi_above", "rsi_below"):
            threshold = self._safe_float(cond.get("value") if cond.get("value") is not None else cond.get("threshold"))
            current_value = current_rsi
        elif ctype == "volume_above":
            threshold = self._safe_float(cond.get("value") if cond.get("value") is not None else cond.get("threshold"))
            current_value = current_volume
        elif ctype == "adx_above":
            threshold = self._safe_float(cond.get("value") if cond.get("value") is not None else cond.get("threshold"))
            current_value = current_adx

        return {
            "n": idx,
            "type": ctype,
            "condition_description": desc or ctype,
            "threshold": threshold,
            "level": level,
            "current_value": current_value,
            "current_price": current_price,
        }

    def _check_simba_wake_conditions(self) -> None:
        now_ts = time.time()
        try:
            if (now_ts - float(self._last_simba_eval_ts or 0.0)) < 30.0:
                return
        except Exception:
            pass
        self._last_simba_eval_ts = now_ts

        wake_conditions = self._load_wake_conditions()
        bot = getattr(self, "bot", None)
        if bot is None:
            return

        conditions = wake_conditions.get("conditions") if isinstance(wake_conditions, dict) else None
        if not isinstance(conditions, list):
            conditions = []

        conditions_fingerprint = None
        try:
            import json
            import hashlib

            canon = json.dumps(conditions, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
            conditions_fingerprint = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
        except Exception:
            conditions_fingerprint = None

        dp = getattr(bot, "_last_agent_data", None)
        if not isinstance(dp, dict) or not dp:
            return

        price_mid = None
        try:
            cp = dp.get("current_price")
            if isinstance(cp, dict):
                b = self._safe_float(cp.get("bid"))
                a = self._safe_float(cp.get("ask"))
                if b is not None and a is not None:
                    price_mid = (b + a) / 2.0
                elif b is not None:
                    price_mid = b
                elif a is not None:
                    price_mid = a
            elif cp is not None:
                price_mid = self._safe_float(cp)
        except Exception:
            price_mid = None

        if price_mid is None:
            try:
                price_mid = self._safe_float(dp.get("price"))
            except Exception:
                price_mid = None

        if isinstance(price_mid, (int, float)):
            try:
                if self._simba_5m_high is None or float(price_mid) > float(self._simba_5m_high):
                    self._simba_5m_high = float(price_mid)
                if self._simba_5m_low is None or float(price_mid) < float(self._simba_5m_low):
                    self._simba_5m_low = float(price_mid)
                if self._simba_5m_first_price is None:
                    self._simba_5m_first_price = float(price_mid)
            except Exception:
                pass

        in_cooldown = False
        cooldown_minutes = 30  # Reverted from 5 (FLO-204 will implement proper FIRED flag)
        mins_remaining = None
        next_eligible_iso = None
        try:
            from datetime import timezone, timedelta

            cw = wake_conditions if isinstance(wake_conditions, dict) else {}
            try:
                _DEFAULT_COOLDOWN = 30  # Reverted from 5 (caused wake loops — FLO-204 is proper fix)
                _persisted = int(cw.get("cooldown_minutes") or _DEFAULT_COOLDOWN)
                cooldown_minutes = min(_persisted, _DEFAULT_COOLDOWN)  # Clamp to configured max
            except Exception:
                cooldown_minutes = 5

            last_wake_at = cw.get("last_wake_at")
            last_fp = str(cw.get("cooldown_fingerprint") or "").strip() or None
            if last_wake_at and cooldown_minutes > 0 and conditions_fingerprint and last_fp == conditions_fingerprint:
                lw = datetime.fromisoformat(str(last_wake_at).replace("Z", "+00:00"))
                if lw.tzinfo is None:
                    lw = lw.replace(tzinfo=timezone.utc)
                now_utc = datetime.now(timezone.utc)
                elapsed = (now_utc - lw).total_seconds() / 60.0
                if elapsed < float(cooldown_minutes):
                    in_cooldown = True
                    mins_remaining = int(max(0, round(float(cooldown_minutes) - elapsed)))
                    next_eligible = lw + timedelta(minutes=int(cooldown_minutes))
                    next_eligible_iso = next_eligible.isoformat()
        except Exception:
            in_cooldown = False

        if price_mid is None:
            try:
                tick = dp.get("tick")
                if isinstance(tick, dict):
                    price_mid = self._safe_float(tick.get("mid"))
            except Exception:
                price_mid = None

        if price_mid is None:
            try:
                from executor import executor

                px = executor.get_current_price()
                if px and isinstance(px, (list, tuple)) and len(px) >= 2:
                    b = self._safe_float(px[0])
                    a = self._safe_float(px[1])
                    if b is not None and a is not None:
                        price_mid = (b + a) / 2.0
            except Exception:
                price_mid = None

        scanner_data: Dict[str, Any] = {
            "current_price": price_mid,
            "indicators": dp.get("indicators"),
            "patterns": dp.get("patterns"),
            "volume": dp.get("volume") or dp.get("tick_volume") or dp.get("last_h1_volume"),
        }
        try:
            candles = dp.get("candles")
            if isinstance(candles, dict):
                h1_candles = candles.get("H1")
                if isinstance(h1_candles, list) and h1_candles:
                    last_h1 = h1_candles[-1]
                    if isinstance(last_h1, dict):
                        scanner_data["last_h1_tick_volume"] = int(last_h1.get("volume", last_h1.get("tick_volume", 0)) or 0)
        except Exception:
            pass

        # ------------------------------------------------------------
        # WATCH CONDITIONS (open-trade protection): agent_watch_conditions.json
        # ------------------------------------------------------------
        watch_trigger = None
        watch_ticket = None
        watch_reason = None
        watch_cond_type = None
        watch_payload = None
        watch_active = False
        try:
            from executor import executor

            positions = []
            try:
                positions = executor.get_open_positions() or []
            except Exception:
                positions = []

            watch_store = {}
            try:
                watch_store = self._load_watch_conditions()
            except Exception:
                watch_store = {}

            try:
                if isinstance(watch_store, dict) and positions:
                    pos_ticket_set = set()
                    for p in positions:
                        try:
                            t = p.get("ticket") if isinstance(p, dict) else getattr(p, "ticket", None)
                            if t is not None:
                                pos_ticket_set.add(str(int(t)))
                        except Exception:
                            continue
                    for k, payload in watch_store.items():
                        if str(k) not in pos_ticket_set:
                            continue
                        conds = payload.get("conditions") if isinstance(payload, dict) else None
                        if isinstance(conds, list) and conds:
                            watch_active = True
                            break
            except Exception:
                watch_active = False

            if isinstance(watch_store, dict) and positions:
                pos_by_ticket = {}
                for p in positions:
                    try:
                        t = p.get("ticket") if isinstance(p, dict) else getattr(p, "ticket", None)
                        if t is not None:
                            pos_by_ticket[int(t)] = p
                    except Exception:
                        continue

                for ticket_str, payload in list(watch_store.items()):
                    try:
                        t = int(ticket_str)
                    except Exception:
                        continue
                    pos = pos_by_ticket.get(t)
                    if pos is None:
                        continue

                    conds = payload.get("conditions") if isinstance(payload, dict) else None
                    if not isinstance(conds, list) or not conds:
                        continue

                    trig = self._evaluate_watch_conditions_for_position(pos, conds, scanner_data)
                    if trig:
                        watch_trigger = trig
                        watch_ticket = t
                        watch_payload = payload if isinstance(payload, dict) else None
                        watch_cond_type = str(trig.get("type") or "").strip()
                        watch_reason = str(trig.get("description") or "").strip() or watch_cond_type
                        break
        except Exception:
            watch_trigger = None

        if watch_trigger is not None and watch_ticket is not None:
            try:
                from db_writer import record_agent_event

                msg = self._next_simba_template(
                    [
                        "Boss, trade alert! {reason}. Floki should take a look.",
                        "Yo Boss — trade alert: {reason}. Might wanna peek at this.",
                    ]
                ).format(reason=str(watch_reason or "watch condition triggered")[:240])

                record_agent_event(
                    "SIMBA_CHECK",
                    msg,
                    payload={
                        "watch": {
                            "ticket": watch_ticket,
                            "trigger": watch_trigger,
                            "store": watch_payload,
                        },
                        "price": self._safe_float(scanner_data.get("current_price")),
                    },
                    author="SIMBA",
                )
            except Exception:
                pass

            # Clear watch conditions for this ticket after trigger
            try:
                store2 = self._load_watch_conditions()
                if isinstance(store2, dict):
                    store2.pop(str(watch_ticket), None)
                    self._save_watch_conditions(store2)
            except Exception:
                pass

            try:
                df = getattr(bot, "_last_df", None)
                snapshot_time_iso = datetime.utcnow().isoformat()
                trigger_data = {
                    "ticket": int(watch_ticket),
                    "watch_condition": watch_trigger,
                    "watch_reason": watch_reason,
                    "watch_type": watch_cond_type,
                }
                _ = df
                _ = snapshot_time_iso
                try:
                    self._fire_proactive_out_of_cycle("SIMBA_WATCH", dict(trigger_data))
                except Exception as e:
                    log.debug(f"AGENT_MONITOR | simba watch fire failed (ignored): {e}")
            except Exception as e:
                try:
                    log.debug(f"AGENT_MONITOR | simba watch call failed (ignored): {e}")
                except Exception:
                    pass

        expired = False
        elapsed_min = None
        try:
            max_sleep = wake_conditions.get("max_sleep_minutes")
            max_sleep_i = int(max_sleep) if max_sleep is not None else 0
        except Exception:
            max_sleep_i = 0

        try:
            started_at = wake_conditions.get("sleep_started_at")
            if started_at and max_sleep_i > 0:
                from datetime import timezone

                st = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
                if st.tzinfo is None:
                    st = st.replace(tzinfo=timezone.utc)
                elapsed_min = (datetime.now(timezone.utc) - st).total_seconds() / 60.0
                expired = elapsed_min >= float(max_sleep_i)
        except Exception:
            expired = False

        # FLO-149 Fix 1: Skip max_sleep wake if Floki's timer is due within 2 minutes
        if expired:
            try:
                _nc_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "agent_next_check.json")
                if os.path.exists(_nc_path):
                    with open(_nc_path, "r", encoding="utf-8") as _ncf:
                        _nc = json.load(_ncf)
                    nc_iso = _nc.get("next_check_at") if isinstance(_nc, dict) else None
                    if nc_iso:
                        from datetime import timezone as _tz
                        nc_dt = datetime.fromisoformat(str(nc_iso).replace("Z", "+00:00"))
                        if nc_dt.tzinfo is None:
                            nc_dt = nc_dt.replace(tzinfo=_tz.utc)
                        secs_until = (nc_dt - datetime.now(_tz.utc)).total_seconds()
                        if 0 < secs_until < 120:
                            log.info(f"SIMBA | max_sleep expired but Floki check due in {int(secs_until)}s — skipping wake")
                            expired = False
            except Exception:
                pass

        triggered_ids: List[str] = []
        checked_count = 0
        met_count = 0
        if not expired:
            try:
                triggered_ids, checked_count, met_count = self._evaluate_wake_conditions(
                    conditions,
                    scanner_data,
                    wake_conditions,
                )
            except Exception:
                triggered_ids = []
                checked_count = 0
                met_count = 0

        # FLO-66: Collect velocity data from evaluated conditions
        velocity_data = {}
        try:
            for i, c in enumerate(conditions or [], start=1):
                if not isinstance(c, dict):
                    continue
                vel = c.pop("_velocity", None)
                cur = c.pop("_current", None)
                if vel is None:
                    continue
                cid = str(c.get("id") or f"c{i}").strip() or f"c{i}"
                velocity_data[cid] = {
                    "current": cur,
                    "velocity": vel.get("status"),
                    "eta_cycles": vel.get("eta_cycles"),
                    "velocity_per_min": vel.get("velocity_per_min"),
                }
        except Exception:
            pass

        simba_result: Dict[str, Any] = {
            "decision": "WAKE" if triggered_ids else "SLEEP",
            "triggered": triggered_ids,
            "checked_count": int(checked_count),
            "met_count": int(met_count),
            "summary": "python_eval",
            "model": "python",
            "latency_ms": 0,
            "velocity_data": velocity_data,
        }

        raw_wake = bool(expired or triggered_ids)
        decision = "WAKE" if (raw_wake and not in_cooldown) else "SLEEP"

        any_orders_active = bool(conditions) or bool(watch_active)

        simba_state_decision = "MONITORING"
        try:
            if decision == "WAKE":
                simba_state_decision = "ALERT"
            elif bool(conditions) or bool(watch_active):
                simba_state_decision = "WATCHING"
            else:
                simba_state_decision = "MONITORING"
        except Exception:
            simba_state_decision = "MONITORING"

        summary_emitted = False
        try:
            from db_writer import record_agent_event

            price_f = self._safe_float(scanner_data.get("current_price"))
            price_str = f"{price_f:.2f}" if isinstance(price_f, (int, float)) else "n/a"

            emit_summary = False
            try:
                if decision == "WAKE" or raw_wake or in_cooldown:
                    emit_summary = False
                else:
                    emit_summary = (now_ts - float(self._last_simba_summary_ts or 0.0)) >= 300.0
            except Exception:
                emit_summary = False

            nearest_levels = []
            try:
                if isinstance(price_f, (int, float)) and isinstance(conditions, list) and conditions:
                    for i, c in enumerate(conditions, start=1):
                        if not isinstance(c, dict):
                            continue
                        level = self._safe_float(c.get("level"))
                        if level is None:
                            continue
                        cid = str(c.get("id") or f"c{i}").strip() or f"c{i}"
                        distance = abs(float(price_f) - float(level))
                        nearest_levels.append({"id": cid, "level": level, "distance": distance, "type": c.get("type")})
                    nearest_levels.sort(key=lambda x: x.get("distance") if isinstance(x, dict) else 1e18)
                    nearest_levels = nearest_levels[:3]
            except Exception:
                nearest_levels = []

            if decision == "WAKE" and expired:
                hours = 0.0
                try:
                    if elapsed_min is not None:
                        hours = float(elapsed_min) / 60.0
                except Exception:
                    hours = 0.0

                msg = self._next_simba_template(
                    [
                        "Boss, it's been {hours}h since Floki last looked. Nothing triggered but figured he should take a fresh look anyway.",
                        "Yo Boss, {hours}h of silence. No triggers hit but dragging Floki out of bed for a check.",
                    ]
                ).format(hours=f"{hours:.1f}")

                record_agent_event(
                    "SIMBA_CHECK",
                    msg,
                    payload={
                        "simba": simba_result,
                        "expired": True,
                        "elapsed_min": elapsed_min,
                        "price": price_f,
                        "range_low": self._simba_5m_low,
                        "range_high": self._simba_5m_high,
                        "nearest_levels": nearest_levels,
                        "cooldown": in_cooldown,
                        "cooldown_minutes": cooldown_minutes,
                        "cooldown_fingerprint": conditions_fingerprint,
                    },
                    author="SIMBA",
                )
                summary_emitted = True
            elif decision == "WAKE" and triggered_ids:
                first = None
                try:
                    by_id = {}
                    for i, c in enumerate(conditions, start=1):
                        if not isinstance(c, dict):
                            continue
                        cid = str(c.get("id") or f"c{i}").strip() or f"c{i}"
                        by_id[cid] = (i, c)
                    t0 = triggered_ids[0]
                    if t0 in by_id:
                        n, cond = by_id[t0]
                        first = self._format_condition_for_message(cond, n, scanner_data)
                except Exception:
                    first = None

                if first and first.get("type") in ("price_above", "price_below") and first.get("level") is not None and first.get("current_price") is not None:
                    msg = self._next_simba_template(
                        [
                            "Boss, heads up! Price just punched through {level} — sitting at {current_price} now. That's your condition #{n}. Floki, you're up!",
                        ]
                    ).format(
                        level=f"{float(first.get('level')):.2f}",
                        current_price=f"{float(first.get('current_price')):.2f}",
                        n=int(first.get("n") or 1),
                    )
                else:
                    msg = self._next_simba_template(
                        [
                            "Oi Boss! {condition_description} just triggered ({current_value} vs your {threshold}). Time to wake the big guy.",
                        ]
                    ).format(
                        condition_description=str((first or {}).get("condition_description") or "a condition"),
                        current_value=str((first or {}).get("current_value") if (first or {}).get("current_value") is not None else "n/a"),
                        threshold=str((first or {}).get("threshold") if (first or {}).get("threshold") is not None else "n/a"),
                    )

                record_agent_event(
                    "SIMBA_CHECK",
                    msg,
                    payload={
                        "simba": simba_result,
                        "expired": False,
                        "triggered": triggered_ids,
                        "price": price_f,
                        "range_low": self._simba_5m_low,
                        "range_high": self._simba_5m_high,
                        "nearest_levels": nearest_levels,
                        "cooldown": in_cooldown,
                        "cooldown_minutes": cooldown_minutes,
                        "cooldown_fingerprint": conditions_fingerprint,
                    },
                    author="SIMBA",
                )
                summary_emitted = True
            elif raw_wake and in_cooldown:
                next_hhmm = ""
                try:
                    if next_eligible_iso:
                        next_hhmm = str(next_eligible_iso)[11:16]
                except Exception:
                    next_hhmm = ""

                msg = (
                    f"Cooldown active ({mins_remaining} min remaining). Conditions unchanged since last wake. "
                    f"Next wake eligible at {next_hhmm}."
                ).strip()

                # FLO-149 Fix 2: cooldown → log only, not feed
                log.info(f"SIMBA_COOLDOWN | {msg} | price={price_str}")
                summary_emitted = True
            else:
                if emit_summary:
                    checked = 0
                    met = 0
                    try:
                        checked = int(simba_result.get("checked_count")) if isinstance(simba_result, dict) else len(conditions)
                    except Exception:
                        checked = len(conditions)
                    try:
                        met = int(simba_result.get("met_count")) if isinstance(simba_result, dict) else 0
                    except Exception:
                        met = 0

                    watch_checked = 0
                    watch_met = 1 if (watch_trigger is not None) else 0
                    try:
                        watch_store = self._load_watch_conditions()
                        if isinstance(watch_store, dict):
                            for _, payload in watch_store.items():
                                conds = payload.get("conditions") if isinstance(payload, dict) else None
                                if isinstance(conds, list):
                                    watch_checked += len(conds)
                    except Exception:
                        watch_checked = 0

                    try:
                        checked = int(checked) + int(watch_checked)
                        met = int(met) + int(watch_met)
                    except Exception:
                        pass

                    rng_low = self._simba_5m_low
                    rng_high = self._simba_5m_high
                    rng_txt = ""
                    try:
                        if isinstance(rng_low, (int, float)) and isinstance(rng_high, (int, float)):
                            rng_txt = f"{float(rng_low):.2f}-{float(rng_high):.2f}"
                    except Exception:
                        rng_txt = ""

                    trend = "steady"
                    try:
                        first_px = self._simba_5m_first_price
                        if isinstance(first_px, (int, float)) and isinstance(price_f, (int, float)):
                            delta = float(price_f) - float(first_px)
                            if abs(delta) < 0.01:
                                trend = "steady"
                            elif delta > 0:
                                trend = "up"
                            else:
                                trend = "down"
                    except Exception:
                        trend = "steady"

                    any_orders_or_watch_active = bool(any_orders_active) or bool(watch_active)

                    if not any_orders_or_watch_active:
                        msg = self._next_simba_template(
                            [
                                "No watch orders from Floki. Price at {price}. Just keeping an eye on things.",
                                "Boss left me no watch list. {price} right now — I'm still on patrol.",
                            ]
                        ).format(price=price_str)
                    else:
                        near_txt = ""
                        try:
                            if nearest_levels:
                                parts = []
                                for x in nearest_levels:
                                    lvl = x.get("level")
                                    dist = x.get("distance")
                                    if isinstance(lvl, (int, float)) and isinstance(dist, (int, float)):
                                        parts.append(f"{float(lvl):.2f} ({float(dist):.1f} away)")
                                if parts:
                                    near_txt = "; nearest: " + ", ".join(parts)
                        except Exception:
                            near_txt = ""

                        msg = self._next_simba_template(
                            [
                                "5-min check-in: price {price} (range {range_txt}). {met}/{checked} conditions met. Trend {trend}{near}.",
                                "Quick patrol report: {price} (last 5m {range_txt}). Hits: {met}/{checked}. Drift {trend}{near}.",
                            ]
                        ).format(
                            price=price_str,
                            range_txt=(rng_txt or "n/a"),
                            met=int(met),
                            checked=int(checked),
                            trend=str(trend),
                            near=str(near_txt),
                        )

                    # FLO-149 Fix 2: patrol → log only, not feed
                    log.info(f"SIMBA_5MIN_SUMMARY | {msg}")
                    summary_emitted = True
                    try:
                        self._last_simba_summary_ts = float(now_ts)
                        self._simba_5m_high = float(price_f) if isinstance(price_f, (int, float)) else None
                        self._simba_5m_low = float(price_f) if isinstance(price_f, (int, float)) else None
                        self._simba_5m_first_price = float(price_f) if isinstance(price_f, (int, float)) else None
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            price_f = self._safe_float(scanner_data.get("current_price"))
            checked = 0
            met = 0
            try:
                checked = int(simba_result.get("checked_count")) if isinstance(simba_result, dict) else len(conditions)
            except Exception:
                checked = len(conditions)
            try:
                met = int(simba_result.get("met_count")) if isinstance(simba_result, dict) else (len(triggered_ids) if decision == "WAKE" else 0)
            except Exception:
                met = 0

            summary = None
            try:
                if isinstance(simba_result, dict):
                    summary = simba_result.get("summary")
            except Exception:
                summary = None

            if decision == "WAKE":
                summary_txt = "ALERT"
            elif not any_orders_active:
                summary_txt = "No watch orders."
            else:
                summary_txt = "Watching levels."
            if isinstance(summary, str) and summary.strip():
                summary_txt = summary.strip()[:240]

            if not hasattr(bot, "last_analysis") or not isinstance(getattr(bot, "last_analysis", None), dict):
                bot.last_analysis = {}
            bot.last_analysis["simba"] = {
                "decision": simba_state_decision,
                "checked_count": int(checked),
                "met_count": int(met),
                "summary": summary_txt,
                "timestamp": datetime.utcnow().isoformat(),
                "price": price_f,
                "cooldown": bool(in_cooldown),
                "has_conditions": bool(any_orders_active),
                "velocity_data": velocity_data,
            }
        except Exception:
            pass

        if decision != "WAKE":
            return

        try:
            wake_conditions["sleep_started_at"] = datetime.utcnow().isoformat()
            wake_conditions["last_wake_at"] = datetime.utcnow().isoformat()
            if conditions_fingerprint:
                wake_conditions["cooldown_fingerprint"] = conditions_fingerprint

            try:
                wake_conditions["cooldown_minutes"] = int(wake_conditions.get("cooldown_minutes") or 30)
            except Exception:
                wake_conditions["cooldown_minutes"] = 30

            # FLO-184: conditions preserved after wake. Fingerprint cooldown
            # (lines 340-354) prevents re-trigger for cooldown_minutes.
            self._save_wake_conditions(wake_conditions)
        except Exception:
            pass

        try:
            df = getattr(bot, "_last_df", None)
            snapshot_time_iso = datetime.utcnow().isoformat()
            trigger_data = {
                "expired": bool(expired),
                "triggered": triggered_ids,
                "simba": simba_result,
            }
            _ = df
            _ = snapshot_time_iso

            try:
                self._fire_proactive_out_of_cycle("SIMBA_WAKE", dict(trigger_data))
            except Exception as e:
                log.debug(f"AGENT_MONITOR | simba wake fire failed (ignored): {e}")

            # FLO-78: Discord card for Simba wake
            try:
                from discord_cards import build_simba_wake_card, send_built_card
                # Find first triggered condition details
                _first_cond = None
                for _ci, _c in enumerate(conditions or [], start=1):
                    _cid = str(_c.get("id") or f"c{_ci}").strip() or f"c{_ci}"
                    if _cid in triggered_ids:
                        _first_cond = _c
                        break
                if _first_cond:
                    _vel_info = velocity_data.get(str(_first_cond.get("id") or ""), {})
                    _grp = _first_cond.get("group")
                    _grp_str = f"Group {_grp}" if _grp else None
                    send_built_card(build_simba_wake_card(
                        condition_type=_first_cond.get("type", "unknown"),
                        threshold=_first_cond.get("value") or _first_cond.get("level") or _first_cond.get("threshold"),
                        current=_vel_info.get("current"),
                        velocity=_vel_info.get("velocity"),
                        group_info=_grp_str,
                    ))
            except Exception:
                pass
        except Exception as e:
            try:
                log.debug(f"AGENT_MONITOR | simba wake call failed (ignored): {e}")
            except Exception:
                pass

    # FLO-66: Approach velocity tracking
    def _calc_velocity(self, cond_key: str, current_value: float, threshold: float, is_above: bool) -> Dict[str, Any]:
        """
        Calculate approach velocity and ETA for a condition.
        Returns: {"velocity_per_min": float, "status": "STABLE"|"APPROACHING"|"RAPID", "eta_cycles": int|None}
        """
        import time as _time

        now = _time.time()
        prev = self._condition_velocity.get(cond_key)
        self._condition_velocity[cond_key] = {"prev_value": current_value, "prev_time": now}

        if prev is None:
            return {"velocity_per_min": 0, "status": "STABLE", "eta_cycles": None}

        elapsed_min = (now - prev["prev_time"]) / 60.0
        if elapsed_min < 0.1:  # Less than 6 seconds — skip
            return {"velocity_per_min": 0, "status": "STABLE", "eta_cycles": None}

        delta = current_value - prev["prev_value"]
        velocity = delta / elapsed_min

        # Distance to threshold
        if is_above:
            distance = threshold - current_value  # positive = not yet reached
        else:
            distance = current_value - threshold  # positive = not yet reached

        # Already met or velocity moving away
        if distance <= 0 or (is_above and velocity <= 0) or (not is_above and velocity >= 0):
            return {"velocity_per_min": round(velocity, 3), "status": "STABLE", "eta_cycles": None}

        # ETA in cycles (each cycle ~ 1 min for Simba checks)
        approach_speed = abs(velocity)
        if approach_speed < 0.001:
            return {"velocity_per_min": round(velocity, 3), "status": "STABLE", "eta_cycles": None}

        eta_minutes = distance / approach_speed
        eta_cycles = int(round(eta_minutes))

        if eta_cycles <= 3:
            status = "RAPID"
        elif eta_cycles <= 10:
            status = "APPROACHING"
        else:
            status = "STABLE"

        return {"velocity_per_min": round(velocity, 3), "status": status, "eta_cycles": eta_cycles}

    def _velocity_log_suffix(self, vel: Dict[str, Any]) -> str:
        """Format velocity info for log line."""
        v = vel.get("velocity_per_min", 0)
        status = vel.get("status", "STABLE")
        eta = vel.get("eta_cycles")
        if status == "STABLE":
            return ""
        sign = "+" if v > 0 else ""
        eta_str = f" (~{eta} cycles)" if eta is not None else ""
        return f" | velocity {sign}{v:.2f}/min {status}{eta_str}"

    def _evaluate_wake_conditions(
        self,
        conditions: List[Dict[str, Any]],
        scanner_data: Dict[str, Any],
        wake_conditions: Dict[str, Any],
    ) -> Tuple[List[str], int, int]:
        triggered: List[str] = []
        checked = 0
        met = 0
        met_ids: set = set()  # FLO-67: track which conditions are met by cid

        price = self._safe_float(scanner_data.get("current_price"))
        indicators = scanner_data.get("indicators") if isinstance(scanner_data.get("indicators"), dict) else {}
        patterns = scanner_data.get("patterns")
        volume = self._safe_float(scanner_data.get("volume"))
        last_h1_vol = self._safe_float(scanner_data.get("last_h1_tick_volume"))

        tol_default = None
        try:
            tol_default = float(wake_conditions.get("price_touch_tolerance") or 1.0)
        except Exception:
            tol_default = 1.0

        ctx_shared = {
            "current_price": price,
            "price_touch_default_tolerance": tol_default,
        }

        for idx, c in enumerate(conditions or [], start=1):
            if not isinstance(c, dict):
                continue

            ctype = str(c.get("type") or "").strip()
            cid = str(c.get("id") or "").strip() or f"c{idx}"
            if not ctype:
                continue

            checked += 1

            try:
                if ctype == "price_above":
                    level = self._safe_float(c.get("level"))
                    if level is None or price is None:
                        continue
                    vel = self._calc_velocity(f"price_above_{cid}", float(price), float(level), True)
                    c["_velocity"] = vel
                    c["_current"] = price
                    if float(price) > float(level):
                        met += 1
                        met_ids.add(cid)

                elif ctype == "price_below":
                    level = self._safe_float(c.get("level"))
                    if level is None or price is None:
                        continue
                    vel = self._calc_velocity(f"price_below_{cid}", float(price), float(level), False)
                    c["_velocity"] = vel
                    c["_current"] = price
                    if float(price) < float(level):
                        met += 1
                        met_ids.add(cid)

                elif ctype == "price_touch":
                    ok, _ = self._is_simba_condition_met(c, ctx_shared, source="wake")
                    if ok:
                        met += 1
                        met_ids.add(cid)

                elif ctype == "h1_volume_above":
                    thr = self._safe_float(c.get("threshold"))
                    if thr is None:
                        continue
                    v = last_h1_vol if last_h1_vol is not None else volume
                    if v is None:
                        continue
                    if float(v) > float(thr):
                        met += 1
                        met_ids.add(cid)

                elif ctype == "scanner_pattern":
                    target = str(c.get("pattern") or "").strip().lower()
                    if not target:
                        continue

                    found = False
                    if isinstance(patterns, list):
                        for p in patterns:
                            try:
                                name = str((p or {}).get("name") if isinstance(p, dict) else p).strip().lower()
                                if name and target in name:
                                    found = True
                                    break
                            except Exception:
                                continue
                    elif isinstance(patterns, dict):
                        try:
                            for k in patterns.keys():
                                if target in str(k).lower():
                                    found = True
                                    break
                        except Exception:
                            found = False

                    if found:
                        met += 1
                        met_ids.add(cid)

                elif ctype in ("indicator_above", "indicator_below"):
                    ind = str(c.get("indicator") or "").strip().lower()
                    thr = self._safe_float(c.get("threshold"))
                    if not ind or thr is None:
                        continue

                    cur = None
                    try:
                        raw = indicators.get(ind)
                        if isinstance(raw, dict):
                            cur = self._safe_float(raw.get("value"))
                        else:
                            cur = self._safe_float(raw)
                    except Exception:
                        cur = None
                    if cur is None:
                        continue

                    is_above = ctype == "indicator_above"
                    vel = self._calc_velocity(f"ind_{ind}_{cid}", float(cur), float(thr), is_above)
                    c["_velocity"] = vel
                    c["_current"] = cur

                    if is_above and float(cur) > float(thr):
                        met += 1
                        met_ids.add(cid)
                    elif not is_above and float(cur) < float(thr):
                        met += 1
                        met_ids.add(cid)

                elif ctype in ("rsi_above", "rsi_below"):
                    thr = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                    cur_rsi = self._safe_float(indicators.get("rsi"))
                    if cur_rsi is None or thr is None:
                        continue
                    is_met = (cur_rsi > thr) if ctype == "rsi_above" else (cur_rsi < thr)
                    is_above = ctype == "rsi_above"
                    vel = self._calc_velocity(f"rsi_{cid}", cur_rsi, thr, is_above)
                    c["_velocity"] = vel
                    c["_current"] = cur_rsi
                    status = "MET" if is_met else "NOT MET"
                    try:
                        log.info(f"SIMBA_CHECK | RSI H1: {cur_rsi:.1f} → {ctype}({thr:.0f}): {status}{self._velocity_log_suffix(vel)}")
                    except Exception:
                        pass
                    if is_met:
                        met += 1
                        met_ids.add(cid)

                elif ctype == "volume_above":
                    thr = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                    cur_vol = last_h1_vol if last_h1_vol is not None else volume
                    if cur_vol is None or thr is None:
                        continue
                    is_met = float(cur_vol) > float(thr)
                    vel = self._calc_velocity(f"vol_{cid}", float(cur_vol), float(thr), True)
                    c["_velocity"] = vel
                    c["_current"] = cur_vol
                    status = "MET" if is_met else "NOT MET"
                    try:
                        log.info(f"SIMBA_CHECK | Volume H1: {cur_vol:.0f} → volume_above({thr:.0f}): {status}{self._velocity_log_suffix(vel)}")
                    except Exception:
                        pass
                    if is_met:
                        met += 1
                        met_ids.add(cid)

                elif ctype == "adx_above":
                    thr = self._safe_float(c.get("value") if c.get("value") is not None else c.get("threshold"))
                    cur_adx = self._safe_float(indicators.get("adx"))
                    if cur_adx is None or thr is None:
                        continue
                    is_met = float(cur_adx) > float(thr)
                    vel = self._calc_velocity(f"adx_{cid}", float(cur_adx), float(thr), True)
                    c["_velocity"] = vel
                    c["_current"] = cur_adx
                    status = "MET" if is_met else "NOT MET"
                    try:
                        log.info(f"SIMBA_CHECK | ADX H1: {cur_adx:.1f} → adx_above({thr:.0f}): {status}{self._velocity_log_suffix(vel)}")
                    except Exception:
                        pass
                    if is_met:
                        met += 1
                        met_ids.add(cid)

                else:
                    try:
                        log.warning(f"SIMBA | unrecognized wake condition type: {ctype} (id={cid})")
                    except Exception:
                        pass
            except Exception as e:
                try:
                    log.debug(f"SIMBA | condition eval error (ignored) | id={cid} type={ctype}: {e}")
                except Exception:
                    pass
                continue

        # FLO-67: Composite AND groups
        # Group conditions by "group" field. Conditions without group = standalone (OR).
        # Within a group: ALL must be met (AND). Between groups/standalone: OR.
        groups: Dict[str, List[str]] = {}  # group_id -> [cid, ...]
        standalone: List[str] = []

        for idx, c in enumerate(conditions or [], start=1):
            if not isinstance(c, dict):
                continue
            cid = str(c.get("id") or f"c{idx}").strip() or f"c{idx}"
            group = c.get("group")
            if group is not None and str(group).strip():
                g = str(group).strip()
                if g not in groups:
                    groups[g] = []
                groups[g].append(cid)
            else:
                standalone.append(cid)

        # Standalone conditions: any met = triggered (backwards compatible OR)
        for cid in standalone:
            if cid in met_ids:
                triggered.append(cid)

        # Grouped conditions: ALL in group must be met
        for g, cids in groups.items():
            all_met = all(cid in met_ids for cid in cids)
            if all_met:
                triggered.extend(cids)
                try:
                    log.info(f"SIMBA_CHECK | Group {g}: ALL {len(cids)} conditions met (AND)")
                except Exception:
                    pass
            else:
                met_in_group = sum(1 for cid in cids if cid in met_ids)
                try:
                    log.info(f"SIMBA_CHECK | Group {g}: {met_in_group}/{len(cids)} met (AND — not triggered)")
                except Exception:
                    pass

        return triggered, checked, met

    def _watch_conditions_path(self) -> str:
        import os

        base_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "agent_watch_conditions.json")

    def _load_watch_conditions(self) -> dict:
        import json
        import os

        path = self._watch_conditions_path()
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_watch_conditions(self, payload: dict) -> bool:
        import json
        import os

        path = self._watch_conditions_path()
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if os.path.exists(path):
                os.remove(path)
            os.rename(tmp, path)
            return True
        except Exception:
            return False

    def _evaluate_watch_conditions_for_position(
        self,
        pos: Any,
        conditions: List[Dict[str, Any]],
        scanner_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        # `pos` may be PositionInfo or dict depending on executor.
        current_price = None
        pnl_dollars = None
        try:
            if isinstance(pos, dict):
                current_price = self._safe_float(pos.get("current_price"))
                pnl_dollars = self._safe_float(pos.get("profit"))
            else:
                current_price = self._safe_float(getattr(pos, "current_price", None))
                pnl_dollars = self._safe_float(getattr(pos, "profit", None))
        except Exception:
            current_price = None
            pnl_dollars = None

        if current_price is None:
            current_price = self._safe_float(scanner_data.get("current_price"))

        macro = {}
        try:
            bot = getattr(self, "bot", None)
            dp = getattr(bot, "_last_agent_data", None) if bot is not None else None
            macro = dp.get("macro_data") if isinstance(dp, dict) else {}
            if not isinstance(macro, dict):
                macro = {}
        except Exception:
            macro = {}

        # FLO-172: inject indicators so indicator_threshold can read RSI/MACD/ADX
        indicators = scanner_data.get("indicators") if isinstance(scanner_data.get("indicators"), dict) else {}

        ctx = {
            "current_price": current_price,
            "pnl_dollars": pnl_dollars,
            "macro": macro,
            "indicators": indicators,
        }
        for c in conditions or []:
            if not isinstance(c, dict):
                continue
            ok, _ = self._is_simba_condition_met(c, ctx, source="watch")
            if ok:
                return c
        return None

    def _is_simba_condition_met(self, cond: Dict[str, Any], ctx: Dict[str, Any], source: str) -> Tuple[bool, Optional[str]]:
        ctype = str(cond.get("type") or "").strip()
        if not ctype:
            return False, None

        try:
            if ctype == "price_touch":
                lvl = self._safe_float(cond.get("level"))
                price = self._safe_float(ctx.get("current_price"))
                if lvl is None or price is None:
                    return False, None
                tol = 5.0
                try:
                    if cond.get("tolerance") is not None:
                        tol = float(cond.get("tolerance"))
                    elif ctx.get("price_touch_default_tolerance") is not None:
                        tol = float(ctx.get("price_touch_default_tolerance"))
                except Exception:
                    tol = 5.0
                return abs(float(price) - float(lvl)) <= float(tol), None

            if ctype == "pnl_threshold":
                thr = self._safe_float(cond.get("value"))
                pnl = self._safe_float(ctx.get("pnl_dollars"))
                if thr is None or pnl is None:
                    return False, None
                return (
                    (thr < 0 and float(pnl) <= float(thr))
                    or (thr > 0 and float(pnl) >= float(thr))
                    or thr == 0
                ), None

            # FLO-172: pnl_below — wake when profit drops below a positive threshold
            if ctype == "pnl_below":
                thr = self._safe_float(cond.get("value"))
                pnl = self._safe_float(ctx.get("pnl_dollars"))
                if thr is None or pnl is None:
                    return False, None
                return float(pnl) < float(thr), None

            if ctype == "indicator_threshold":
                ind = str(cond.get("indicator") or "").strip().lower()
                direction = str(cond.get("direction") or "").strip().lower()
                lvl = self._safe_float(cond.get("level"))
                if lvl is None or direction not in ("above", "below"):
                    return False, None

                # FLO-172: dispatch by indicator type
                cur = None
                if ind == "vix":
                    macro = ctx.get("macro") if isinstance(ctx.get("macro"), dict) else {}
                    cur = self._safe_float(macro.get("vix"))
                else:
                    indicators = ctx.get("indicators") if isinstance(ctx.get("indicators"), dict) else {}
                    # Map friendly names to nested dict paths
                    _IND_MAP = {
                        "rsi": ("rsi", "value"),
                        "macd_histogram": ("macd", "histogram"),
                        "macd_hist": ("macd", "histogram"),
                        "adx": ("adx", "value"),
                    }
                    mapping = _IND_MAP.get(ind)
                    if mapping:
                        block = indicators.get(mapping[0])
                        if isinstance(block, dict):
                            cur = self._safe_float(block.get(mapping[1]))
                        else:
                            cur = self._safe_float(block)
                    else:
                        # Generic fallback: try indicators[ind] as dict or raw value
                        raw = indicators.get(ind)
                        if isinstance(raw, dict):
                            cur = self._safe_float(raw.get("value"))
                        else:
                            cur = self._safe_float(raw)

                if cur is None:
                    return False, None
                ok = (direction == "above" and float(cur) >= float(lvl)) or (direction == "below" and float(cur) <= float(lvl))
                return ok, None

            if ctype in ("price_above", "price_below", "h1_volume_above", "scanner_pattern", "indicator_above", "indicator_below"):
                # These are wake-only types (handled in _evaluate_wake_conditions)
                return False, None

            try:
                log.warning(f"SIMBA | unrecognized {source} condition type: {ctype}")
            except Exception:
                pass
            return False, "unrecognized"
        except Exception as e:
            try:
                log.debug(f"SIMBA | condition eval error (ignored) | source={source} type={ctype}: {e}")
            except Exception:
                pass
            return False, "error"

    def _get_active_trade(self) -> Optional[Dict[str, Any]]:
        # MT5 is the source of truth. DB is advisory only.
        try:
            from executor import executor

            live_positions = executor.get_open_positions() or []
        except Exception:
            live_positions = []

        if not live_positions:
            # Ignore stale DB-only "active trade" when MT5 has none.
            try:
                from db_writer import get_active_trade_from_proactive

                stale = get_active_trade_from_proactive()
                if stale:
                    now_ts = time.time()
                    if (now_ts - float(self._last_stale_db_active_trade_log_ts or 0.0)) > 900.0:
                        self._last_stale_db_active_trade_log_ts = now_ts
                        log.debug(
                            "AGENT_MONITOR | stale DB active_trade ignored (MT5 has 0 positions)"
                        )
            except Exception:
                pass
            return None

        # Build from the first live position
        pos = live_positions[0]
        try:
            ticket = getattr(pos, "ticket", None)
            direction = getattr(pos, "direction", None)
            open_price = getattr(pos, "open_price", None)
            sl = getattr(pos, "sl", None)
            tp = getattr(pos, "tp", None)
            out = {
                "timestamp": datetime.utcnow().isoformat(),
                "decision": "OPEN_BUY" if str(direction).upper() == "BUY" else "OPEN_SELL",
                "entry": open_price,
                "sl": sl,
                "tp": tp,
                "ticket": ticket,
            }
        except Exception:
            out = None

        # If DB has a newer open record, keep its timestamp (but never override MT5 existence)
        try:
            from db_writer import get_active_trade_from_proactive

            db_trade = get_active_trade_from_proactive()
            if isinstance(out, dict) and isinstance(db_trade, dict):
                if db_trade.get("timestamp"):
                    out["timestamp"] = db_trade.get("timestamp")
        except Exception:
            pass

        return out

    def _trade_direction_from_decision(self, decision: str) -> str:
        if decision == "OPEN_BUY":
            return "BUY"
        if decision == "OPEN_SELL":
            return "SELL"
        return ""

    def _format_trade_summary(self, trade: Dict[str, Any]) -> str:
        decision = str(trade.get("decision") or "")
        direction = self._trade_direction_from_decision(decision)
        entry = trade.get("entry")
        try:
            entry_f = float(entry) if entry is not None else None
        except Exception:
            entry_f = None

        if direction and entry_f is not None:
            return f"{direction}@{entry_f:.1f}"
        if direction:
            return direction
        return "none"

    def _compute_trade_distances(self, trade: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        decision = str(trade.get("decision") or "")
        direction = self._trade_direction_from_decision(decision)
        if direction not in ("BUY", "SELL"):
            return None, None

        price_used = self._price_used(direction)
        if price_used is None:
            return None, None

        sl = trade.get("sl")
        tp = trade.get("tp")
        try:
            sl_f = float(sl) if sl is not None else None
        except Exception:
            sl_f = None
        try:
            tp_f = float(tp) if tp is not None else None
        except Exception:
            tp_f = None

        dist_sl = abs(price_used - sl_f) if sl_f is not None else None
        dist_tp = abs(tp_f - price_used) if tp_f is not None else None
        return dist_sl, dist_tp

    def _get_upcoming_high_event_summary(self) -> str:
        from economic_calendar import get_upcoming_events

        events = get_upcoming_events(max_events=3)
        if not isinstance(events, list):
            return "none"

        best = None
        for ev in events:
            if not isinstance(ev, dict):
                continue
            importance = str(ev.get("importance") or "").upper()
            if importance != "HIGH":
                continue
            minutes_until = ev.get("minutes_until")
            try:
                minutes_until_f = float(minutes_until)
            except Exception:
                continue
            if minutes_until_f < 0:
                continue
            if best is None or minutes_until_f < best[0]:
                best = (minutes_until_f, str(ev.get("name") or "?"))

        if not best:
            return "none"

        minutes_until_f, name = best
        return f"{int(minutes_until_f)}m:{name}"

    def _load_latest_entry_conditions(self) -> Optional[Dict[str, Any]]:
        try:
            from db_writer import get_latest_proactive_entry_conditions

            return get_latest_proactive_entry_conditions()
        except Exception:
            return None

    def _mid_price(self) -> Optional[float]:
        try:
            from executor import executor

            prices = executor.get_current_price()
            if not prices:
                return None

            bid, ask = prices
            return float((float(bid) + float(ask)) / 2.0)
        except Exception:
            return None

    def _is_expired(self, timestamp_str: Optional[str], entry_conditions: Dict[str, Any]) -> bool:
        try:
            validity_minutes = entry_conditions.get("validity_minutes")
            if validity_minutes is None:
                return False

            ts = None
            if timestamp_str:
                try:
                    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                except Exception:
                    ts = None

            if ts is None:
                return False

            age_seconds = (datetime.utcnow() - ts.replace(tzinfo=None)).total_seconds()
            return age_seconds > float(validity_minutes) * 60.0
        except Exception:
            return False

    def _price_used(self, direction: str) -> Optional[float]:
        try:
            from executor import executor

            prices = executor.get_current_price()
            if not prices:
                return None

            bid, ask = prices
            if str(direction).upper() == "BUY":
                return float(ask)
            if str(direction).upper() == "SELL":
                return float(bid)

            return float((bid + ask) / 2.0)
        except Exception:
            return None

    def _spam_key(self, direction: str, cond: Dict[str, Any]) -> str:
        ctype = str(cond.get("type") or "").strip().lower()
        level = cond.get("level")
        cross_dir = str(cond.get("direction") or "").strip().lower()
        return f"{direction}:{ctype}:{level}:{cross_dir}"

    def _can_fire(self, key: str, cooldown_seconds: int = 300) -> bool:
        now = time.time()
        last = self.last_trigger_times.get(key)
        if last is not None and (now - last) < cooldown_seconds:
            return False
        self.last_trigger_times[key] = now
        return True

    def _fire_fast_decision(self, trigger_type: str, trigger_data: Dict[str, Any]) -> None:
        try:
            if self.bot is None:
                return
            if not hasattr(self.bot, "agent_fast_decide"):
                return
            t = threading.Thread(
                target=self.bot.agent_fast_decide,
                args=(trigger_type, trigger_data),
                daemon=True,
            )
            t.start()
        except Exception as e:
            log.debug(f"AGENT_MONITOR | fast_decision error (ignored): {e}")

    def _fire_proactive_out_of_cycle(self, trigger_type: str, trigger_data: Dict[str, Any]) -> None:
        try:
            if self.bot is None:
                return
            if not hasattr(self.bot, "agent_proactive_out_of_cycle"):
                return
            t = threading.Thread(
                target=self.bot.agent_proactive_out_of_cycle,
                args=(trigger_type, trigger_data),
                daemon=True,
            )
            t.start()
        except Exception as e:
            log.debug(f"AGENT_MONITOR | proactive_out_of_cycle error (ignored): {e}")

    def _check_trade_at_risk(self) -> None:
        from db_writer import get_active_trade_from_proactive

        try:
            from executor import executor

            live_positions = executor.get_open_positions()
            if not live_positions:
                self.last_trade_pnl_points = None
                return
        except Exception:
            pass

        trade = get_active_trade_from_proactive()
        if not trade:
            self.last_trade_pnl_points = None
            return

        decision = str(trade.get("decision") or "")
        direction = "BUY" if decision == "OPEN_BUY" else "SELL" if decision == "OPEN_SELL" else ""
        if direction not in ("BUY", "SELL"):
            return

        price_used = self._price_used(direction)
        if price_used is None:
            return

        entry = trade.get("entry")
        sl = trade.get("sl")
        tp = trade.get("tp")

        try:
            sl_f = float(sl) if sl is not None else None
            tp_f = float(tp) if tp is not None else None
            entry_f = float(entry) if entry is not None else None
        except Exception:
            return

        if sl_f is not None:
            dist_to_sl = abs(price_used - sl_f)
            if dist_to_sl < 5.0:
                key = "trade_risk_sl"
                if self._can_fire(key, cooldown_seconds=300):
                    log.info(f"MONITOR | Trade at risk — SL {dist_to_sl:.1f} points away")
                    self._fire_fast_decision(
                        "TRADE_RISK_SL",
                        {"direction": direction, "dist_to_sl": dist_to_sl, "price": price_used, "sl": sl_f},
                    )

        if tp_f is not None:
            dist_to_tp = abs(tp_f - price_used)
            if dist_to_tp < 5.0:
                key = "trade_risk_tp"
                if self._can_fire(key, cooldown_seconds=300):
                    log.info(f"MONITOR | Trade near TP — {dist_to_tp:.1f} points away")
                    self._fire_fast_decision(
                        "TRADE_RISK_TP",
                        {"direction": direction, "dist_to_tp": dist_to_tp, "price": price_used, "tp": tp_f},
                    )

        if entry_f is not None:
            pnl_points = (price_used - entry_f) if direction == "BUY" else (entry_f - price_used)
            if self.last_trade_pnl_points is not None:
                if self.last_trade_pnl_points > 0 and pnl_points < 0:
                    key = "trade_pnl_flip"
                    if self._can_fire(key, cooldown_seconds=300):
                        log.info("MONITOR | P&L flipped negative")
                        self._fire_fast_decision(
                            "TRADE_RISK_PNL_FLIP",
                            {
                                "direction": direction,
                                "pnl_points": pnl_points,
                                "prev_pnl_points": self.last_trade_pnl_points,
                                "price": price_used,
                                "entry": entry_f,
                            },
                        )
            self.last_trade_pnl_points = pnl_points

    def _check_calendar_events(self) -> None:
        from economic_calendar import get_upcoming_events

        events = get_upcoming_events(max_events=3)
        if not isinstance(events, list):
            return

        for ev in events:
            if not isinstance(ev, dict):
                continue

            importance = str(ev.get("importance") or "").upper()
            if importance != "HIGH":
                continue

            minutes_until = ev.get("minutes_until")
            try:
                minutes_until_f = float(minutes_until)
            except Exception:
                continue

            if minutes_until_f < 0 or minutes_until_f >= 15:
                continue

            name = str(ev.get("name") or "?")
            t = str(ev.get("time") or "?")
            key = f"calendar_high:{name}:{t}"
            if not self._can_fire(key, cooldown_seconds=600):
                continue

            log.info(f"MONITOR | HIGH impact event in {int(minutes_until_f)} minutes: {name}")
            self._fire_fast_decision(
                "CALENDAR_HIGH_IMPACT",
                {"minutes_until": minutes_until_f, "name": name, "time": t},
            )

    def _check_breakout(self) -> None:
        price = self._mid_price()
        if price is None:
            return

        now_ts = time.time()
        self.recent_prices.append((now_ts, price))

        cutoff = now_ts - 300.0
        self.recent_prices = [(ts, p) for (ts, p) in self.recent_prices if ts >= cutoff]
        if len(self.recent_prices) < 2:
            return

        prices = [p for (_, p) in self.recent_prices]
        move = max(prices) - min(prices)
        if move <= 15.0:
            return

        key = "breakout"
        if not self._can_fire(key, cooldown_seconds=300):
            return

        first_price = self.recent_prices[0][1]
        last_price = self.recent_prices[-1][1]
        signed_move = last_price - first_price
        sign = "+" if signed_move >= 0 else "-"
        log.info(f"MONITOR | Breakout detected — price moved {sign}{abs(move):.1f} points in 5 minutes")
        return

    def _check_session_change(self) -> None:
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        london_key = "london"
        if now.hour == 8 and 0 <= now.minute < 5:
            if self.session_last_trigger_date.get(london_key) != today:
                self.session_last_trigger_date[london_key] = today
                log.info("MONITOR | London session opening")
                return

        ny_key = "ny"
        if now.hour == 13 and 0 <= now.minute < 5:
            if self.session_last_trigger_date.get(ny_key) != today:
                self.session_last_trigger_date[ny_key] = today
                log.info("MONITOR | NY session opening")
                return

    def _check_profit_drawdown(self) -> None:
        try:
            from executor import executor

            live_positions = executor.get_open_positions()
            if not live_positions:
                self.max_profit_seen_points_by_ticket = {}
                return
        except Exception:
            return

        for pos in live_positions:
            if not isinstance(pos, dict):
                continue

            ticket = pos.get("ticket")
            if ticket is None:
                continue
            try:
                ticket_i = int(ticket)
            except Exception:
                continue

            direction = str(pos.get("direction") or pos.get("type") or "").upper()
            if direction not in ("BUY", "SELL"):
                continue

            entry = pos.get("price_open")
            if entry is None:
                entry = pos.get("open_price")
            if entry is None:
                entry = pos.get("entry")
            try:
                entry_f = float(entry)
            except Exception:
                continue

            price_used = self._price_used(direction)
            if price_used is None:
                continue

            current_profit_points = (price_used - entry_f) if direction == "BUY" else (entry_f - price_used)
            prev_peak = self.max_profit_seen_points_by_ticket.get(ticket_i)
            if prev_peak is None or current_profit_points > prev_peak:
                self.max_profit_seen_points_by_ticket[ticket_i] = float(current_profit_points)

            try:
                now_ts = time.time()
                last_log_ts = self._last_drawdown_track_log_ts_by_ticket.get(ticket_i) or 0
                if (now_ts - float(last_log_ts)) >= 60.0:
                    self._last_drawdown_track_log_ts_by_ticket[ticket_i] = now_ts
                    peak_for_log = self.max_profit_seen_points_by_ticket.get(ticket_i, current_profit_points)
                    log.info(
                        "DRAWDOWN_TRACK | "
                        f"ticket=#{ticket_i} | peak={float(peak_for_log):.1f} | current={float(current_profit_points):.1f}"
                    )
            except Exception:
                pass

            if prev_peak is None or current_profit_points > prev_peak:
                continue

            peak = float(prev_peak)

            try:
                now_ts = time.time()
                last_log_ts = self._last_drawdown_track_log_ts_by_ticket.get(ticket_i) or 0
                if (now_ts - float(last_log_ts)) >= 60.0:
                    self._last_drawdown_track_log_ts_by_ticket[ticket_i] = now_ts
                    log.info(
                        "DRAWDOWN_TRACK | "
                        f"ticket=#{ticket_i} | peak={peak:.1f} | current={current_profit_points:.1f}"
                    )
            except Exception:
                pass

            if peak <= 3.0:
                continue

            if current_profit_points >= (peak * 0.5):
                continue

            key = f"profit_drawdown:{ticket_i}"
            if not self._can_fire(key, cooldown_seconds=300):
                continue

            log.info(
                "MONITOR | Profit drawdown — "
                f"ticket={ticket_i} {direction} peak=+{peak:.1f} now=+{current_profit_points:.1f} points"
            )

            self._fire_fast_decision(
                "PROFIT_DRAWDOWN",
                {
                    "ticket": ticket_i,
                    "direction": direction,
                    "max_profit_points": float(peak),
                    "current_profit_points": float(current_profit_points),
                    "peak_to_now_ratio": float(current_profit_points / peak) if peak else None,
                    "message": (
                        f"Your {direction} was at +{peak:.1f} points profit and has dropped to "
                        f"+{current_profit_points:.1f}. Do you want to protect profits?"
                    ),
                },
            )

    def _check_entry_conditions(self, entry_conditions: Dict[str, Any]) -> None:
        direction = str(entry_conditions.get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            return

        price_used = self._price_used(direction)
        if price_used is None:
            return

        conditions = entry_conditions.get("conditions") or []
        if not isinstance(conditions, list):
            return

        indicator_ctx: Dict[str, Any] = {}
        try:
            if self.bot is not None:
                indicator_ctx = getattr(self.bot, "last_analysis", None) or {}
        except Exception:
            indicator_ctx = {}

        indicators = {}
        try:
            if isinstance(indicator_ctx, dict):
                indicators = indicator_ctx.get("indicators") or {}
        except Exception:
            indicators = {}

        rsi_value = None
        try:
            rsi_obj = indicators.get("rsi") or {}
            rsi_value = float(rsi_obj.get("value"))
        except Exception:
            rsi_value = None

        volume_ratio = None
        try:
            vol_obj = indicators.get("volume") or {}
            volume_ratio = float(vol_obj.get("ratio"))
        except Exception:
            volume_ratio = None

        all_met = True
        primary = None
        for cond in conditions:
            if not isinstance(cond, dict):
                continue

            ctype = str(cond.get("type") or "").strip().lower()
            if primary is None and ctype in ("price_touch", "price_break"):
                primary = cond

            if ctype == "price_touch":
                level = cond.get("level")
                try:
                    level_f = float(level)
                except Exception:
                    all_met = False
                    break
                if abs(price_used - level_f) >= 2.0:
                    all_met = False
                    break

            elif ctype == "price_break":
                level = cond.get("level")
                cross_dir = str(cond.get("direction") or "").strip().lower()
                try:
                    level_f = float(level)
                except Exception:
                    all_met = False
                    break
                if self.last_price_used is None:
                    all_met = False
                    break
                if cross_dir == "below":
                    if not (self.last_price_used >= level_f and price_used < level_f):
                        all_met = False
                        break
                elif cross_dir == "above":
                    if not (self.last_price_used <= level_f and price_used > level_f):
                        all_met = False
                        break
                else:
                    all_met = False
                    break

            elif ctype == "volume_confirmation":
                thr = cond.get("threshold")
                try:
                    thr_f = float(thr)
                except Exception:
                    all_met = False
                    break
                if volume_ratio is None:
                    all_met = False
                    break
                if float(volume_ratio) < thr_f:
                    all_met = False
                    break

            elif ctype == "rsi_confirmation":
                level = cond.get("level")
                cross_dir = str(cond.get("direction") or "").strip().lower()
                try:
                    level_f = float(level)
                except Exception:
                    all_met = False
                    break
                if rsi_value is None:
                    all_met = False
                    break
                if cross_dir == "below":
                    if not (float(rsi_value) < level_f):
                        all_met = False
                        break
                elif cross_dir == "above":
                    if not (float(rsi_value) > level_f):
                        all_met = False
                        break
                else:
                    all_met = False
                    break

            else:
                all_met = False
                break

        if all_met and primary is not None:
            key = self._spam_key(direction, primary)
            if self._can_fire(key, cooldown_seconds=300):
                ctype = str(primary.get("type") or "").strip().lower()
                lvl = primary.get("level")
                try:
                    lvl_f = float(lvl) if lvl is not None else None
                except Exception:
                    lvl_f = None

                desc = str(primary.get("description") or "").strip()
                label = desc or (f"{ctype} @ {lvl_f}" if lvl_f is not None else ctype)
                log.info(f"MONITOR | Entry condition met — {direction} {ctype} @ {lvl_f} | {label}")
                return

        self.last_price_used = price_used
