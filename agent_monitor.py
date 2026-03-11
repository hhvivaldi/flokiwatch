import time
from datetime import datetime
from typing import Any, Dict, Optional, List

from logger import log


class AgentMonitor:
    def __init__(self):
        self.entry_conditions: Optional[Dict[str, Any]] = None
        self.entry_conditions_timestamp: Optional[str] = None
        self.last_trigger_times: Dict[str, float] = {}
        self.last_price_used: Optional[float] = None

    def check(self) -> None:
        """Run Agent monitor checks (called every ~60 seconds)."""
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
            log.debug(f"AGENT_MONITOR | check error (ignored): {e}")

    def _load_latest_entry_conditions(self) -> Optional[Dict[str, Any]]:
        try:
            from db_writer import get_latest_proactive_entry_conditions

            return get_latest_proactive_entry_conditions()
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

    def _can_fire(self, key: str) -> bool:
        now = time.time()
        last = self.last_trigger_times.get(key)
        if last is not None and (now - last) < 300:
            return False
        self.last_trigger_times[key] = now
        return True

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
                if not self._can_fire(key):
                    continue

                label = desc or f"{ctype} @ {level_f}"
                log.info(f"MONITOR | Entry condition met — {direction} {ctype} @ {level_f} | {label}")

        self.last_price_used = price_used
