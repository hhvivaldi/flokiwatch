"""FLO-455 Phase 1 — ICT Smart Money Concepts zones for the MT5 chart.

Detects UNMITIGATED Order Blocks (OB), Fair Value Gaps (FVG), and liquidity
Sweeps on H1 via the `smartmoneyconcepts` package, and writes `ict_zones.json`
to MQL5\\Files\\ for ICTZoneDrawer.mq5 (Phase 2) to draw — the same bridge
pattern as sr_zones.json.

ADDITIVE: does NOT touch the existing S/R zone system. H1 only. Zero AI cost
(pure Python). The package is BETA — outputs must be validated visually on the
MT5 chart before being trusted (per the ticket).

JSON schema (per FLO-455):
    {"timestamp": <iso>, "zones": [
        {"type":"OB"|"FVG","direction":"bullish"|"bearish","timeframe":"H1",
         "top":float,"bottom":float,"status":"unmitigated","candle_time":<iso>},
        {"type":"SWEEP","direction":"high"|"low","timeframe":"H1",
         "level":float,"candle_time":<iso>}]}
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from typing import Any, Dict, List, Optional

from logger import log
from tz_utils import utc_iso
import config

# Import is BETA + prints a star-the-repo banner to stdout — suppress that one-
# time noise so it never lands in the trade logs.
try:
    import pandas as pd
    with contextlib.redirect_stdout(io.StringIO()):
        from smartmoneyconcepts import smc
    _SMC_OK = True
except Exception as _imp_err:  # pragma: no cover
    _SMC_OK = False
    log.warning(f"ICT_ZONES: smartmoneyconcepts unavailable ({type(_imp_err).__name__}); "
                f"zone detection disabled")

_SWING_LENGTH = 50      # smc.swing_highs_lows lookback (gold H1)
_MAX_BARS = 300         # H1 history fed to smc
_MAX_ZONES_PER_TYPE = 25  # cap clutter on the chart
_BROKER_OFFSET_H = 3    # MT5 broker server time = UTC+3 (project memory)


def _iso(ts) -> Optional[str]:
    """A pandas Timestamp (UTC) -> ISO-8601 'Z' string."""
    try:
        t = pd.Timestamp(ts)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t.isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def build_ict_zones_payload(ohlc, timeframe: str = "H1") -> Dict[str, Any]:
    """Pure-ish: take an OHLC DataFrame (lowercase open/high/low/close[/volume],
    UTC DatetimeIndex) and return the FLO-455 JSON payload of UNMITIGATED zones.
    Fail-soft per sub-detector — one failing function never blocks the rest."""
    zones: List[Dict[str, Any]] = []
    payload = {"timestamp": utc_iso(), "zones": zones}
    if not _SMC_OK or ohlc is None or len(ohlc) < (_SWING_LENGTH + 5):
        return payload

    try:
        swings = smc.swing_highs_lows(ohlc, swing_length=_SWING_LENGTH)
    except Exception as e:
        log.warning(f"ICT_ZONES swing_highs_lows failed: {type(e).__name__}: {e}")
        return payload

    # ---- Fair Value Gaps (unmitigated) ----
    try:
        fvg = smc.fvg(ohlc)
        sel = fvg[fvg["FVG"].notna() & fvg["MitigatedIndex"].isna()].tail(_MAX_ZONES_PER_TYPE)
        for ts, row in sel.iterrows():
            zones.append({
                "type": "FVG",
                "direction": "bullish" if row["FVG"] == 1 else "bearish",
                "timeframe": timeframe,
                "top": round(float(row["Top"]), 2),
                "bottom": round(float(row["Bottom"]), 2),
                "status": "unmitigated",
                "candle_time": _iso(ts),
            })
    except Exception as e:
        log.warning(f"ICT_ZONES fvg failed: {type(e).__name__}: {e}")

    # ---- Order Blocks (unmitigated) ----
    try:
        ob = smc.ob(ohlc, swings)
        if "MitigatedIndex" in ob.columns:
            sel = ob[ob["OB"].notna() & ob["MitigatedIndex"].isna()]
        else:
            sel = ob[ob["OB"].notna()]
        for ts, row in sel.tail(_MAX_ZONES_PER_TYPE).iterrows():
            zones.append({
                "type": "OB",
                "direction": "bullish" if row["OB"] == 1 else "bearish",
                "timeframe": timeframe,
                "top": round(float(row["Top"]), 2),
                "bottom": round(float(row["Bottom"]), 2),
                "status": "unmitigated",
                "candle_time": _iso(ts),
            })
    except Exception as e:
        log.warning(f"ICT_ZONES ob failed: {type(e).__name__}: {e}")

    # ---- Liquidity sweeps ----
    # Liquidity: 1 = buy-side (resting above the highs), -1 = sell-side (below the
    # lows). Drawn as a level line; 'high'/'low' is the side the liquidity sits on.
    try:
        liq = smc.liquidity(ohlc, swings)
        sel = liq[liq["Liquidity"].notna()].tail(_MAX_ZONES_PER_TYPE)
        for ts, row in sel.iterrows():
            zones.append({
                "type": "SWEEP",
                "direction": "high" if row["Liquidity"] == 1 else "low",
                "timeframe": timeframe,
                "level": round(float(row["Level"]), 2),
                "candle_time": _iso(ts),
            })
    except Exception as e:
        log.warning(f"ICT_ZONES liquidity failed: {type(e).__name__}: {e}")

    return payload


def _fetch_h1_ohlc(bars: int = _MAX_BARS):
    """H1 OHLC from MT5 via the thread-safe proxy (Rule 23), UTC-indexed."""
    from mt5_safe import mt5, mt5_lock
    with mt5_lock:
        rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_H1, 0, bars)
    if rates is None or len(rates) < (_SWING_LENGTH + 5):
        return None
    df = pd.DataFrame(rates)
    df["utc"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=_BROKER_OFFSET_H)
    df = df.rename(columns={"tick_volume": "volume"}).set_index("utc")
    keep = [c for c in ("open", "high", "low", "close", "volume") if c in df.columns]
    return df[keep]


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
    """Brain-cycle entry point: fetch H1, detect zones, write ict_zones.json.
    Fail-soft — never raises; returns the payload (or None if disabled)."""
    if not _SMC_OK:
        return None
    path = path or getattr(config, "ICT_ZONES_JSON_PATH", None)
    if not path:
        return None
    try:
        df = _fetch_h1_ohlc()
        payload = (build_ict_zones_payload(df, "H1") if df is not None
                   else {"timestamp": utc_iso(), "zones": []})
        _write_json(payload, path)
        n = len(payload["zones"])
        _ob = sum(1 for z in payload["zones"] if z["type"] == "OB")
        _fvg = sum(1 for z in payload["zones"] if z["type"] == "FVG")
        _sw = sum(1 for z in payload["zones"] if z["type"] == "SWEEP")
        log.info(f"ICT_ZONES | H1 | wrote {n} zones (OB={_ob} FVG={_fvg} SWEEP={_sw}) "
                 f"-> {os.path.basename(path)}")
        return payload
    except Exception as e:  # pragma: no cover
        log.warning(f"ICT_ZONES build_and_write failed: {type(e).__name__}: {e}")
        return None
