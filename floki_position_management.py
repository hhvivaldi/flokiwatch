import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import config
from logger import log


def _signal_file_path() -> str:
    return getattr(
        config,
        "BRAIN_SIGNAL_JSON_PATH",
        os.path.join(os.path.dirname(config.SR_ZONES_JSON_PATH), "brain_signal.json"),
    )


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> bool:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        if os.path.exists(path):
            os.remove(path)
        os.rename(tmp_path, path)
        return True
    except Exception as e:
        log.debug(f"FLOKI_PM | atomic write failed: {e}")
        return False


def compute_ea_position_params(sl_pips: float, volatility_status: Optional[str]) -> Tuple[float, float, float, float]:
    sl_pips_f = float(sl_pips)
    vol_status = str(volatility_status or "").strip().upper()

    if vol_status == "COOLING_DOWN":
        return (
            float(config.COOLING_BREAKEVEN_TRIGGER_PIPS),
            float(config.COOLING_TRAILING_TRIGGER_PIPS),
            float(config.COOLING_TRAILING_DISTANCE_PIPS),
            float(config.MAX_POSITION_DRAWDOWN_PIPS),
        )

    if bool(getattr(config, "FLOKI_MANAGES_POSITION", False)):
        return (
            float(sl_pips_f * getattr(config, "FLOKI_BREAKEVEN_ATR_MULT", 0.8)),
            float(getattr(config, "FLOKI_TRAILING_TRIGGER_PIPS", 500)),
            float(getattr(config, "FLOKI_TRAILING_DISTANCE_PIPS", 300)),
            float(getattr(config, "FLOKI_MAX_DRAWDOWN_PIPS", config.MAX_POSITION_DRAWDOWN_PIPS)),
        )

    return (
        float(sl_pips_f * getattr(config, "BREAKEVEN_ATR_MULT", 0.5)),
        float(sl_pips_f * getattr(config, "TRAILING_ATR_MULT", 0.7)),
        float(sl_pips_f * getattr(config, "TRAILING_DISTANCE_ATR_MULT", 0.7)),
        float(getattr(config, "EA_TIGHT_MAX_DRAWDOWN_PIPS", config.MAX_POSITION_DRAWDOWN_PIPS)),
    )


def cap_next_check_minutes(requested_minutes: Any, has_open_position: bool) -> int:
    try:
        minutes = int(requested_minutes)
    except Exception:
        minutes = 5

    if minutes < 2:
        minutes = 2
    if minutes > 120:
        minutes = 120

    if has_open_position:
        cap = int(getattr(config, "FLOKI_MAX_CHECK_WITH_POSITION", 10) or 10)
        if minutes > cap:
            return cap
    return minutes


def fallback_next_check_minutes(has_open_position: bool) -> int:
    if has_open_position:
        return int(getattr(config, "FLOKI_FALLBACK_CHECK_WITH_POSITION", 3) or 3)
    return 5


def write_floki_heartbeat() -> bool:
    try:
        file_path = _signal_file_path()
        payload: Dict[str, Any] = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    payload = dict(existing)
            except Exception:
                payload = {}

        payload["floki_heartbeat"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        return _write_json_atomic(file_path, payload)
    except Exception as e:
        log.debug(f"FLOKI_PM | heartbeat write failed: {e}")
        return False


def write_next_check_payload(next_path: str, requested_minutes: int) -> bool:
    minutes = int(requested_minutes)
    next_at = datetime.utcnow() + timedelta(minutes=minutes)
    payload = {
        "next_check_at": next_at.isoformat(timespec="seconds") + "Z",
        "requested_minutes": minutes,
    }
    return _write_json_atomic(next_path, payload)
