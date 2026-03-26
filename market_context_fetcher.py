"""
FLO-122: Standalone market context fetcher.

Reads 20 correlated MT5 instruments and returns organized data.
Used by both agent_tools.get_market_context() and state_writer.write_state().
"""

import time
from datetime import datetime
from typing import Any, Dict, Optional

from logger import log


MARKET_CONTEXT_SYMBOLS = {
    "metals": ["XAGUSD", "XPTUSD", "XPDUSD"],
    "forex": ["EURUSD", "USDJPY", "USDCHF", "AUDUSD", "USDCNH", "GBPUSD"],
    "indices": ["US500"],
    "energy": ["XTIUSD"],
    "crypto": ["BTCUSD"],
    "futures": ["DXY_M6", "VIX_J6", "UST10Y_M6"],
}

FUTURES_LABELS = {
    "DXY_M6": "Dollar Index",
    "VIX_J6": "VIX Fear Gauge",
    "UST10Y_M6": "10Y Bond Price (up=yields down)",
}

# Module-level cache
_cache: Optional[Dict[str, Any]] = None
_cache_ts: float = 0
_CACHE_TTL = 60  # seconds


def _infer_session(utc_hour: int) -> str:
    h = utc_hour % 24
    if 0 <= h <= 6:
        return "ASIAN"
    if 7 <= h <= 12:
        return "LONDON"
    if 13 <= h <= 20:
        return "NY"
    return "OFF"


def fetch_market_context(force: bool = False) -> Dict[str, Any]:
    """Fetch all correlated instruments from MT5. Cached for 60s.

    Args:
        force: bypass cache and fetch fresh data

    Returns:
        Dict with metals, forex, indices, energy, crypto, futures, session.
        Returns empty dict on MT5 failure.
    """
    global _cache, _cache_ts

    if not force and _cache and (time.time() - _cache_ts) < _CACHE_TTL:
        return _cache

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return _cache or {}

    result: Dict[str, Any] = {}

    for category, symbols in MARKET_CONTEXT_SYMBOLS.items():
        cat_data: Dict[str, Any] = {}
        for sym in symbols:
            try:
                mt5.symbol_select(sym, True)
                tick = mt5.symbol_info_tick(sym)
                if tick and tick.bid > 0:
                    info = mt5.symbol_info(sym)
                    prev_close = getattr(info, "session_close", 0) if info else 0
                    change_pct = None
                    if prev_close and prev_close > 0:
                        change_pct = round(((tick.bid - prev_close) / prev_close) * 100, 2)
                    entry: Dict[str, Any] = {
                        "bid": round(tick.bid, 5 if tick.bid < 10 else 2),
                        "change_pct": change_pct,
                    }
                    # Day range
                    if info:
                        d_hi = getattr(info, "bidhigh", 0) or 0
                        d_lo = getattr(info, "bidlow", 0) or 0
                        if d_hi > d_lo > 0:
                            decimals = 5 if d_hi < 10 else 2
                            entry["day_high"] = round(d_hi, decimals)
                            entry["day_low"] = round(d_lo, decimals)
                            entry["position_in_range"] = round(
                                (tick.bid - d_lo) / (d_hi - d_lo), 2
                            )
                    # Label for futures
                    label = FUTURES_LABELS.get(sym)
                    if label:
                        entry["label"] = label
                    cat_data[sym] = entry
                else:
                    cat_data[sym] = None
            except Exception:
                cat_data[sym] = None
        result[category] = cat_data

    # Derived: gold/silver ratio
    try:
        gold_tick = mt5.symbol_info_tick("XAUUSD")
        silver = (result.get("metals") or {}).get("XAGUSD")
        if gold_tick and gold_tick.bid > 0 and silver and silver.get("bid"):
            result["metals"]["gold_silver_ratio"] = round(gold_tick.bid / silver["bid"], 1)
    except Exception:
        pass

    # Derived: dollar strength
    try:
        forex = result.get("forex") or {}
        eur = (forex.get("EURUSD") or {}).get("change_pct")
        jpy = (forex.get("USDJPY") or {}).get("change_pct")
        chf = (forex.get("USDCHF") or {}).get("change_pct")
        if eur is not None and jpy is not None and chf is not None:
            strong_signals = (1 if eur < 0 else 0) + (1 if jpy > 0 else 0) + (1 if chf > 0 else 0)
            if strong_signals >= 2:
                result["forex"]["dollar_strength"] = "strong"
            elif strong_signals <= 1:
                result["forex"]["dollar_strength"] = "weak"
            else:
                result["forex"]["dollar_strength"] = "mixed"
    except Exception:
        pass

    # Session context
    try:
        utc_hour = datetime.utcnow().hour
        result["session"] = {
            "name": _infer_session(utc_hour),
            "utc_hour": utc_hour,
        }
    except Exception:
        pass

    _cache = result
    _cache_ts = time.time()
    return result
