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
        self.recent_prices: List[Tuple[float, float]] = []
        self.session_last_trigger_date: Dict[str, str] = {}

    def check(self) -> None:
        """Run Agent monitor checks (called every ~60 seconds)."""
        try:
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

            try:
                price_str = f"{tick_mid:.2f}" if isinstance(tick_mid, (int, float)) else "n/a"
                log.debug(
                    "AGENT_MONITOR | tick | "
                    f"price={price_str} | "
                    f"active_trade={trade_summary} | "
                    f"dist_SL={dist_sl_str} | dist_TP={dist_tp_str} | "
                    f"conditions={conditions_status} | "
                    f"upcoming_high={upcoming_high}"
                )
            except Exception:
                pass

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

    def _get_active_trade(self) -> Optional[Dict[str, Any]]:
        try:
            from db_writer import get_active_trade_from_proactive

            return get_active_trade_from_proactive()
        except Exception:
            return None

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

    def _check_trade_at_risk(self) -> None:
        from db_writer import get_active_trade_from_proactive

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
        self._fire_fast_decision(
            "BREAKOUT_5M",
            {"move": float(move), "signed_move": float(signed_move), "window_seconds": 300},
        )

    def _check_session_change(self) -> None:
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        london_key = "london"
        if now.hour == 8 and 0 <= now.minute < 5:
            if self.session_last_trigger_date.get(london_key) != today:
                self.session_last_trigger_date[london_key] = today
                log.info("MONITOR | London session opening")
                self._fire_fast_decision("SESSION_OPEN_LONDON", {"time_utc": now.isoformat()})

        ny_key = "ny"
        if now.hour == 13 and 0 <= now.minute < 5:
            if self.session_last_trigger_date.get(ny_key) != today:
                self.session_last_trigger_date[ny_key] = today
                log.info("MONITOR | NY session opening")
                self._fire_fast_decision("SESSION_OPEN_NY", {"time_utc": now.isoformat()})

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

        for cond in conditions:
            if not isinstance(cond, dict):
                continue

            ctype = str(cond.get("type") or "").strip().lower()
            level = cond.get("level")
            desc = str(cond.get("description") or "").strip()

            try:
                level_f = float(level)
            except Exception:
                continue

            fired = False
            if ctype == "price_touch":
                fired = abs(price_used - level_f) < 2.0
            elif ctype == "price_break":
                cross_dir = str(cond.get("direction") or "").strip().lower()
                if self.last_price_used is not None:
                    if cross_dir == "below":
                        fired = self.last_price_used >= level_f and price_used < level_f
                    elif cross_dir == "above":
                        fired = self.last_price_used <= level_f and price_used > level_f

            if fired:
                key = self._spam_key(direction, cond)
                if not self._can_fire(key, cooldown_seconds=300):
                    continue

                label = desc or f"{ctype} @ {level_f}"
                log.info(f"MONITOR | Entry condition met — {direction} {ctype} @ {level_f} | {label}")
                self._fire_fast_decision(
                    "ENTRY_CONDITION_MET",
                    {
                        "direction": direction,
                        "condition_type": ctype,
                        "level": level_f,
                        "price": price_used,
                        "description": label,
                    },
                )

        self.last_price_used = price_used
