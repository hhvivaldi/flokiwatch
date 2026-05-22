"""FLO-455 Phase 1 — ICT Smart Money Concepts zones for the MT5 chart.

Detects H1 Fair Value Gaps and liquidity Sweeps and writes `ict_zones.json` to
MQL5\\Files\\ for ICTZoneDrawer.mq5 (Phase 2) to draw — the same bridge pattern
as sr_zones.json.

DATA SOURCE (FLO-455 follow-up, CEO 2026-05-22): reuses Floki's OWN detectors
`agent_tools._scan_fvgs` / `_scan_sweeps` (FLO-438) — they work correctly on
gold ($4500+). The external `smartmoneyconcepts` package returned 0 zones (not
calibrated for gold-scale prices), so it was dropped. _scan_fvgs already returns
only UNFILLED (unmitigated) FVGs.

NOTE: Order Blocks are NOT produced — Floki's tools detect FVGs + sweeps only.
OB can be added later if/when an OB detector exists.

ADDITIVE: does NOT touch the existing S/R zone system. H1 only. Zero AI cost.

JSON schema (per FLO-455):
    {"timestamp": <iso>, "zones": [
        {"type":"FVG","direction":"bullish"|"bearish","timeframe":"H1",
         "top":float,"bottom":float,"status":"unmitigated","candle_time":<iso>},
        {"type":"SWEEP","direction":"high"|"low","timeframe":"H1",
         "level":float,"candle_time":<iso>}]}
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from logger import log
from tz_utils import utc_iso
import config

_TIMEFRAME = "H1"
_MAX_PER_TYPE = 15
_MAX_SWEEPS = 5   # FLO-455 follow-up: cap drawn sweeps to the 5 nearest current price


def _map_fvg(f: Dict[str, Any], tf: str) -> Dict[str, Any]:
    return {
        "type": "FVG",
        "direction": f.get("direction"),          # "bullish" / "bearish"
        "timeframe": tf,
        "top": f.get("top"),
        "bottom": f.get("bottom"),
        "status": "unmitigated",                  # _scan_fvgs returns unfilled only
        "candle_time": f.get("formed_at_iso"),
    }


def _map_sweep(s: Dict[str, Any], tf: str) -> Dict[str, Any]:
    # BSL = buy-side liquidity (resting above the highs) -> "high";
    # SSL = sell-side (below the lows) -> "low".
    return {
        "type": "SWEEP",
        "direction": "high" if s.get("direction") == "BSL" else "low",
        "timeframe": tf,
        "level": s.get("level"),
        "candle_time": s.get("sweep_candle_time_iso"),
    }


def build_ict_zones_payload(fvgs: Optional[List[dict]], sweeps: Optional[List[dict]],
                            timeframe: str = _TIMEFRAME, current_price: Optional[float] = None,
                            max_sweeps: int = _MAX_SWEEPS) -> Dict[str, Any]:
    """Pure mapping: FLO-438 scanner output -> FLO-455 ict_zones.json payload.

    Sweeps are decluttered (FLO-455 follow-up — 11 markers was chart noise): keep
    only the `max_sweeps` NEAREST to `current_price`. FVGs are kept as-is."""
    zones: List[Dict[str, Any]] = []
    for f in (fvgs or []):
        if f.get("top") is not None and f.get("bottom") is not None:
            zones.append(_map_fvg(f, timeframe))

    sw = [s for s in (sweeps or []) if s.get("level") is not None]
    if current_price is not None:
        sw.sort(key=lambda s: abs(float(s["level"]) - current_price))  # nearest first
    sw = sw[:max_sweeps]
    for s in sw:
        zones.append(_map_sweep(s, timeframe))
    return {"timestamp": utc_iso(), "zones": zones}


def _current_price() -> Optional[float]:
    """Mid price from MT5 (for the nearest-sweep filter). None on failure."""
    try:
        from mt5_safe import mt5, mt5_lock
        with mt5_lock:
            t = mt5.symbol_info_tick("XAUUSD")
        if t and t.bid and t.ask:
            return (float(t.bid) + float(t.ask)) / 2.0
    except Exception:
        pass
    return None


def _scan_h1():
    """Run Floki's FLO-438 detectors on H1. Lazy import of agent_tools avoids any
    import cycle (central_brain -> ict_zones -> agent_tools). Returns (fvgs, sweeps)."""
    from agent_tools import _scan_fvgs, _scan_sweeps, _mt5_tf
    tf_const = _mt5_tf(_TIMEFRAME)
    if tf_const is None:
        return [], []
    fvgs = _scan_fvgs(_TIMEFRAME, tf_const, max_results=_MAX_PER_TYPE)
    sweeps = _scan_sweeps(_TIMEFRAME, tf_const, max_results=_MAX_PER_TYPE)
    return fvgs, sweeps


def _write_json(payload: Dict[str, Any], path: str) -> None:
    """Atomic write (temp + os.replace), mirroring the project's state-JSON convention."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def build_and_write_ict_zones(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Brain-cycle entry point: detect H1 FVGs + sweeps via Floki's detectors and
    write ict_zones.json. Fail-soft — never raises; returns the payload (or None)."""
    path = path or getattr(config, "ICT_ZONES_JSON_PATH", None)
    if not path:
        return None
    try:
        fvgs, sweeps = _scan_h1()
        payload = build_ict_zones_payload(fvgs, sweeps, _TIMEFRAME,
                                          current_price=_current_price())
        _write_json(payload, path)
        _f = sum(1 for z in payload["zones"] if z["type"] == "FVG")
        _s = sum(1 for z in payload["zones"] if z["type"] == "SWEEP")
        log.info(f"ICT_ZONES | H1 | wrote {len(payload['zones'])} zones "
                 f"(FVG={_f} SWEEP={_s}) -> {os.path.basename(path)}")
        return payload
    except Exception as e:  # pragma: no cover
        log.warning(f"ICT_ZONES build_and_write failed: {type(e).__name__}: {e}")
        return None
