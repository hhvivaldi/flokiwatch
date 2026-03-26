import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Tuple

import config
from logger import log


def _get_signal_file_path() -> str:
    return getattr(
        config,
        "BRAIN_SIGNAL_JSON_PATH",
        os.path.join(os.path.dirname(config.SR_ZONES_JSON_PATH), "brain_signal.json"),
    )


def get_ea_management_params(sl_pips: float, volatility_status: Any) -> Tuple[float, float, float, float]:
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
        # FLO-116: EA becomes pure executor. Floki manages positions via
        # adjust_trade (direct MT5 API) and close_trade. Set BE/trailing
        # triggers to 9999 so the EA never activates them autonomously.
        # Keep max_drawdown as emergency backstop only.
        return (
            9999.0,
            9999.0,
            9999.0,
            float(getattr(config, "FLOKI_MAX_DRAWDOWN_PIPS", 800)),
        )

    return (
        float(getattr(config, "EA_TIGHT_BREAKEVEN_TRIGGER_PIPS", getattr(config, "BREAKEVEN_TRIGGER_PIPS", sl_pips_f * 0.5))),
        float(getattr(config, "EA_TIGHT_TRAILING_TRIGGER_PIPS", getattr(config, "TRAILING_TRIGGER_PIPS", sl_pips_f * 0.7))),
        float(getattr(config, "EA_TIGHT_TRAILING_DISTANCE_PIPS", getattr(config, "TRAILING_DISTANCE_PIPS", sl_pips_f * 0.7))),
        float(getattr(config, "EA_TIGHT_MAX_DRAWDOWN_PIPS", config.MAX_POSITION_DRAWDOWN_PIPS)),
    )


def get_scheduled_minutes(requested_minutes: Any, has_open_position: bool) -> int:
    try:
        minutes = int(requested_minutes)
    except Exception:
        minutes = 5

    if minutes < 2:
        minutes = 2
    if minutes > 120:
        minutes = 120

    if has_open_position:
        max_minutes = int(getattr(config, "FLOKI_MAX_CHECK_WITH_POSITION", 10) or 10)
        if minutes > max_minutes:
            minutes = max_minutes

    return minutes


def get_fallback_minutes(has_open_position: bool) -> int:
    if has_open_position:
        return int(getattr(config, "FLOKI_FALLBACK_CHECK_WITH_POSITION", 3) or 3)
    return 5


def write_floki_heartbeat() -> bool:
    try:
        signal_path = os.path.abspath(_get_signal_file_path())
        payload: Dict[str, Any] = {}

        if os.path.exists(signal_path):
            try:
                with open(signal_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing, dict):
                    payload = dict(existing)
            except Exception:
                payload = {}

        payload["floki_heartbeat"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

        parent = os.path.dirname(signal_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        tmp_path = signal_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, signal_path)
        return True
    except Exception as e:
        log.debug(f"FLOKI_PM | heartbeat write failed: {e}")
        return False
