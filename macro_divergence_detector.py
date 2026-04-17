"""
FLO-293 Part 3: Macro cross-asset divergence detector.

Pure function. Given MT5 rates for UST10Y_M6 M15, DXY_M6 M15, and XAUUSD M5,
detects when a cross-asset move has occurred but XAU has not caught up yet,
producing a short-term directional bias signal.

Signals (one-of, S24 wins ties):
  yields_surge_xau_lag  bond futures dropped >=0.15% in 60 min AND
                        XAU has NOT dropped >=0.10% in 60 min  -> BEARISH
  dxy_drop_xau_lag      DXY dropped >=0.15% in 30 min AND
                        XAU has NOT risen >=0.10% in 60 min    -> BULLISH

Confidence values below are STATIC v1 calibrations from the FLO-293 Part 3
backtest (S24 n=58 90d-dir-match=88.6% / 180d=71.2%; S25 n=347 90d=67.9% /
180d=65.3%). 180d values are shipped as the more-honest out-of-sample number.

TODO FLO-XXX: Recalibrate these confidence values by 2026-07-17 (90d from
author time), OR immediately after any major macro regime change such as
resolution of the Iran/oil crisis that dominated the calibration window.
At author time: Luna environment was DANGER, risk_level 9.
"""
from typing import Any, Dict, Optional
import numpy as np

# Thresholds - locked from the FLO-293 Part 3 backtest grid
UST_60M_RET_THR = 0.0015   # bond price must move >=0.15% over 60 min (~2-3bp yield)
DXY_30M_RET_THR = 0.0015   # DXY must move >=0.15% over 30 min
XAU_60M_LAG_THR = 0.001    # XAU must NOT have moved >=0.10% over 60 min

# Freshness window - stale M15 data suppresses the signal
STALE_M15_SECONDS = 1800   # 30 min

# Calibrated confidence (integer 0-100); see TODO above for refresh cadence
CONF_YIELDS_SURGE_XAU_LAG = 71   # from 180d dir-match 71.2% on n=157 fires
CONF_DXY_DROP_XAU_LAG = 65       # from 180d dir-match 65.3% on n=377 fires


def _trailing_return(closes: np.ndarray, lookback_bars: int) -> Optional[float]:
    """(close[-1] - close[-1-lookback]) / close[-1-lookback], or None if insufficient/invalid."""
    if closes is None or len(closes) < lookback_bars + 1:
        return None
    base = closes[-1 - lookback_bars]
    if base == 0 or not np.isfinite(base):
        return None
    return float((closes[-1] - base) / base)


def _closes(rates: Any) -> Optional[np.ndarray]:
    """Extract close-price array from either an MT5 structured array or a list of dict-like rows."""
    if rates is None or len(rates) == 0:
        return None
    try:
        return np.asarray([float(r["close"]) for r in rates], dtype=float)
    except Exception:
        return None


def _latest_ts(rates: Any) -> Optional[int]:
    """Return the latest bar's open-time as unix seconds."""
    if rates is None or len(rates) == 0:
        return None
    try:
        return int(rates[-1]["time"])
    except Exception:
        return None


def detect_macro_divergence(
    ust_m15_rates: Optional[Any],
    dxy_m15_rates: Optional[Any],
    xau_m5_rates: Optional[Any],
    now_utc: float,
) -> Optional[Dict[str, Any]]:
    """
    Returns the active macro divergence signal, or None if no signal fires.

    Args:
        ust_m15_rates: MT5 rates for UST10Y_M6 M15 (>=5 bars), or None.
        dxy_m15_rates: MT5 rates for DXY_M6 M15 (>=3 bars), or None.
        xau_m5_rates:  MT5 rates for XAUUSD M5 (>=13 bars).
        now_utc:       Current unix timestamp in seconds (UTC).

    Returns:
        None, or a dict with keys:
          signal      str ("yields_surge_xau_lag" | "dxy_drop_xau_lag")
          bias        str ("BEARISH" | "BULLISH")
          confidence  int 0-100
          age_min     int  (minutes since the driving M15 bar's open time)
          detail      str  (human-readable numbers for evidence/logging)
    """
    xau_closes = _closes(xau_m5_rates)
    if xau_closes is None or len(xau_closes) < 13:
        return None
    xau_ret_60m = _trailing_return(xau_closes, 12)  # 12 M5 bars = 60 min
    if xau_ret_60m is None:
        return None

    # S24 first (higher confidence)
    ust_closes = _closes(ust_m15_rates)
    ust_ts = _latest_ts(ust_m15_rates)
    if ust_closes is not None and ust_ts is not None and len(ust_closes) >= 5:
        age_sec = now_utc - ust_ts
        if 0 <= age_sec <= STALE_M15_SECONDS:
            ust_ret_60m = _trailing_return(ust_closes, 4)  # 4 M15 bars = 60 min
            if (ust_ret_60m is not None
                    and ust_ret_60m < -UST_60M_RET_THR
                    and xau_ret_60m > -XAU_60M_LAG_THR):
                return {
                    "signal": "yields_surge_xau_lag",
                    "bias": "BEARISH",
                    "confidence": CONF_YIELDS_SURGE_XAU_LAG,
                    "age_min": int(age_sec // 60),
                    "detail": (
                        f"UST10Y_M6 {ust_ret_60m * 100:+.2f}%/60m; "
                        f"XAU {xau_ret_60m * 100:+.2f}%/60m"
                    ),
                }

    # S25
    dxy_closes = _closes(dxy_m15_rates)
    dxy_ts = _latest_ts(dxy_m15_rates)
    if dxy_closes is not None and dxy_ts is not None and len(dxy_closes) >= 3:
        age_sec = now_utc - dxy_ts
        if 0 <= age_sec <= STALE_M15_SECONDS:
            dxy_ret_30m = _trailing_return(dxy_closes, 2)  # 2 M15 bars = 30 min
            if (dxy_ret_30m is not None
                    and dxy_ret_30m < -DXY_30M_RET_THR
                    and xau_ret_60m < XAU_60M_LAG_THR):
                return {
                    "signal": "dxy_drop_xau_lag",
                    "bias": "BULLISH",
                    "confidence": CONF_DXY_DROP_XAU_LAG,
                    "age_min": int(age_sec // 60),
                    "detail": (
                        f"DXY_M6 {dxy_ret_30m * 100:+.2f}%/30m; "
                        f"XAU {xau_ret_60m * 100:+.2f}%/60m"
                    ),
                }

    return None
