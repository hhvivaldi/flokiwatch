"""
VOLATILITY GUARD - Free Fall / Spike Protection
Detects extreme movements in M5 candles and reports status to the Central Brain.

Stateless: always recalculates from the last 20 M5 candles (~100 min).
No file persistence — if bot restarts, recalculates automatically.

"Block first, ask later" logic (2-candle confirm/cancel):
  1. Candle 1 >= 1.8% → immediate EXTREME (total block)
  2. Candle 2 (next) decides:
     - Cancel A: body2% < 0.5% → NORMAL (normalized)
     - Cancel B: opposite direction and body2% >= 1.0% → NORMAL (strong reversal)
     - Confirm A: same direction and body2% >= 1.0% → COOLING 90 min (real cascade)
     - Ambiguous (none of the above) → COOLING 30 min

Status:
  EXTREME      → M5 candle with >1.8% movement, no next candle yet (total block)
  COOLING_DOWN → Extreme confirmed or ambiguous (strong signals only)
  NORMAL       → No recent extreme events, or extreme cancelled
"""

from datetime import datetime, timezone
from typing import Dict, Optional

import config
from logger import log


def _parse_candle_time(row) -> datetime:
    """Extract and normalize timestamp from a DataFrame candle."""
    candle_time = row['time'] if 'time' in row.index else row.name
    
    if hasattr(candle_time, 'to_pydatetime'):
        candle_time = candle_time.to_pydatetime()
    
    if hasattr(candle_time, 'tzinfo') and candle_time.tzinfo is not None:
        candle_time = candle_time.replace(tzinfo=None)
    
    return candle_time


def _candle_body_percent(row) -> float:
    """Calculate candle body movement %."""
    open_price = row['open']
    if open_price <= 0:
        return 0.0
    return abs(row['close'] - open_price) / open_price * 100


def _candle_direction(row) -> str:
    """Return candle direction: DOWN or UP."""
    return "DOWN" if row['close'] < row['open'] else "UP"


def get_volatility_status() -> Dict:
    """
    Analyze last 20 M5 candles to detect extreme movements.
    Uses 2-candle logic: blocks on candle 1, confirms/cancels on candle 2.
    
    Returns:
        Dict with:
            status: "EXTREME" | "COOLING_DOWN" | "NORMAL"
            last_extreme_candle: dict with extreme candle data (or None)
            minutes_since_extreme: minutes since last extreme (or None)
            extreme_percent: % movement of extreme candle (or 0)
            cooling_reason: "confirmed" | "ambiguous" | None
            description: descriptive text of status
    """
    try:
        from technical_analyzer import get_mt5_data
        
        df = get_mt5_data(timeframe="M5", bars=20)
        
        if df is None or len(df) < 2:
            log.warning("Volatility Guard: no M5 data — assuming NORMAL")
            return _build_result("NORMAL", description="No M5 data available")
        
        threshold = config.EXTREME_CANDLE_THRESHOLD_PERCENT
        now = datetime.now()
        
        # Search for most recent extreme candle (from newest to oldest)
        last_extreme_idx = None
        
        for i in range(len(df) - 1, -1, -1):
            body_pct = _candle_body_percent(df.iloc[i])
            if body_pct >= threshold:
                last_extreme_idx = i
                break
        
        # No extreme in last 20 candles
        if last_extreme_idx is None:
            return _build_result("NORMAL", description="No extreme candles in last 20 M5 candles")
        
        # Build extreme candle info
        extreme_row = df.iloc[last_extreme_idx]
        extreme_time = _parse_candle_time(extreme_row)
        minutes_since = (now - extreme_time).total_seconds() / 60
        pct = round(_candle_body_percent(extreme_row), 2)
        direction = _candle_direction(extreme_row)
        
        extreme_info = {
            "time": extreme_time,
            "open": float(extreme_row['open']),
            "close": float(extreme_row['close']),
            "high": float(extreme_row['high']),
            "low": float(extreme_row['low']),
            "move_percent": pct,
            "direction": direction,
            "minutes_ago": round(minutes_since, 1),
        }
        
        # ============================================================
        # CASE 1: Extreme candle is the last candle (no next candle)
        # → immediate EXTREME (total block)
        # ============================================================
        is_last_candle = (last_extreme_idx == len(df) - 1)
        
        if is_last_candle:
            desc = (
                f"EXTREME: M5 candle {direction} of {pct:.2f}% "
                f"({minutes_since:.0f} min ago) — TOTAL BLOCK"
            )
            log.warning(f"Volatility Guard: {desc}")
            return _build_result(
                "EXTREME",
                last_extreme_candle=extreme_info,
                minutes_since_extreme=minutes_since,
                extreme_percent=pct,
                description=desc,
            )
        
        # ============================================================
        # CASE 2: Next candle exists → evaluate confirm/cancel
        # ============================================================
        next_row = df.iloc[last_extreme_idx + 1]
        next_body_pct = _candle_body_percent(next_row)
        next_direction = _candle_direction(next_row)
        same_direction = (next_direction == direction)
        opposite_direction = (next_direction != direction)
        
        cancel_threshold = config.EXTREME_CANCEL_THRESHOLD_PERCENT
        confirm_threshold = config.EXTREME_CONFIRM_THRESHOLD_PERCENT
        
        # --- Cancel A: normalized (body2 < 0.5%) ---
        if next_body_pct < cancel_threshold:
            desc = (
                f"CANCELLED: Extreme candle {direction} {pct:.2f}% "
                f"{minutes_since:.0f} min ago, but next candle normalized "
                f"({next_body_pct:.2f}% < {cancel_threshold}%) — NORMAL"
            )
            log.info(f"Volatility Guard: {desc}")
            return _build_result("NORMAL", description=desc)
        
        # --- Cancel B: strong reversal (opposite and >= 1.0%) ---
        if opposite_direction and next_body_pct >= confirm_threshold:
            desc = (
                f"CANCELLED: Extreme candle {direction} {pct:.2f}% "
                f"{minutes_since:.0f} min ago, but next candle reversed strongly "
                f"({next_direction} {next_body_pct:.2f}%) — NORMAL"
            )
            log.info(f"Volatility Guard: {desc}")
            return _build_result("NORMAL", description=desc)
        
        # --- Confirm A: cascade (same direction and >= 1.0%) ---
        if same_direction and next_body_pct >= confirm_threshold:
            cooling_minutes = config.COOLING_CONFIRMED_MINUTES
            if minutes_since <= cooling_minutes:
                desc = (
                    f"CONFIRMED: Cascade {direction} — candle 1: {pct:.2f}%, "
                    f"candle 2: {next_body_pct:.2f}% same dir "
                    f"({minutes_since:.0f} min ago) — COOLING {cooling_minutes} min"
                )
                log.warning(f"Volatility Guard: {desc}")
                return _build_result(
                    "COOLING_DOWN",
                    last_extreme_candle=extreme_info,
                    minutes_since_extreme=minutes_since,
                    extreme_percent=pct,
                    cooling_reason="confirmed",
                    description=desc,
                )
            else:
                return _build_result(
                    "NORMAL",
                    description=f"Confirmed cascade {minutes_since:.0f} min ago (> {cooling_minutes} min) — expired"
                )
        
        # --- Ambiguous: neither cancelled nor confirmed → short COOLING ---
        cooling_minutes = config.COOLING_AMBIGUOUS_MINUTES
        if minutes_since <= cooling_minutes:
            desc = (
                f"AMBIGUOUS: Extreme candle {direction} {pct:.2f}% "
                f"{minutes_since:.0f} min ago, next candle {next_direction} {next_body_pct:.2f}% "
                f"— COOLING {cooling_minutes} min (conservative)"
            )
            log.info(f"Volatility Guard: {desc}")
            return _build_result(
                "COOLING_DOWN",
                last_extreme_candle=extreme_info,
                minutes_since_extreme=minutes_since,
                extreme_percent=pct,
                cooling_reason="ambiguous",
                description=desc,
            )
        else:
            return _build_result(
                "NORMAL",
                description=f"Ambiguous extreme {minutes_since:.0f} min ago (> {cooling_minutes} min) — expired"
            )
    
    except Exception as e:
        log.warning(f"Volatility Guard error: {e} — assuming NORMAL")
        return _build_result("NORMAL", description=f"Error: {e} — neutral mode")


def _build_result(
    status: str,
    last_extreme_candle: Optional[Dict] = None,
    minutes_since_extreme: Optional[float] = None,
    extreme_percent: float = 0.0,
    cooling_reason: Optional[str] = None,
    description: str = "",
) -> Dict:
    """Build standardized result dict."""
    return {
        "status": status,
        "last_extreme_candle": last_extreme_candle,
        "minutes_since_extreme": minutes_since_extreme,
        "extreme_percent": extreme_percent,
        "cooling_reason": cooling_reason,
        "description": description,
    }
