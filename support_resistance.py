"""
SUPPORT & RESISTANCE — Zone Detection Module
Detects S/R zones using fractal swing highs/lows + cluster merge.
Stateless: recalculates from OHLCV data each call.

Dual-layer: H1 (intraday) + H4 (structural).
Integration: context modifier for Cérebro Central (confidence, SL/TP, scenarios).
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class SRZone:
    """A Support/Resistance zone."""
    price_low: float          # Lower boundary of zone
    price_high: float         # Upper boundary of zone
    midpoint: float           # Zone center
    zone_type: str            # "SUPPORT", "RESISTANCE", "FLIP"
    touches: int              # Number of times price touched and reversed
    timeframe: str            # "H1" or "H4"
    last_touch_bar: int       # Bar index of most recent touch (relative to end of data)
    strength: str             # "weak" (1-2), "moderate" (3), "strong" (4+)
    swing_points: List[float] = field(default_factory=list)  # Raw swing prices in this cluster
    confluence: List[str] = field(default_factory=list)       # Multi-TF origin e.g. ["H1", "H4"] or ["H4", "D1"]


@dataclass
class SRContext:
    """S/R context for a given price and direction."""
    nearest_support: Optional[SRZone] = None
    nearest_resistance: Optional[SRZone] = None
    dist_to_support_pips: float = 9999.0
    dist_to_resistance_pips: float = 9999.0
    confidence_adjustment: float = 0.0
    sl_adjustment_pips: float = 0.0
    tp_adjustment_pips: float = 0.0
    confirmations: List[str] = field(default_factory=list)
    alerts: List[str] = field(default_factory=list)
    all_zones: List[SRZone] = field(default_factory=list)
    description: str = ""


# ============================================================================
# CONSTANTS (defaults — overridden by config.py in live/backtest)
# ============================================================================

PIP_SIZE = 0.1  # XAU/USD: 1 pip = $0.1


# ============================================================================
# FRACTAL SWING DETECTION
# ============================================================================

def _find_swing_highs(highs: np.ndarray, order: int = 2) -> List[Tuple[int, float]]:
    """
    Find swing highs using fractal logic.
    A bar is a swing high if its high is greater than the `order` bars on each side.

    Args:
        highs: Array of high prices
        order: Number of bars on each side to compare (2 = 5-bar fractal)

    Returns:
        List of (index, price) tuples
    """
    swings = []
    for i in range(order, len(highs) - order):
        is_swing = True
        for j in range(1, order + 1):
            if highs[i] <= highs[i - j] or highs[i] <= highs[i + j]:
                is_swing = False
                break
        if is_swing:
            swings.append((i, float(highs[i])))
    return swings


def _find_swing_lows(lows: np.ndarray, order: int = 2) -> List[Tuple[int, float]]:
    """
    Find swing lows using fractal logic.
    A bar is a swing low if its low is less than the `order` bars on each side.
    """
    swings = []
    for i in range(order, len(lows) - order):
        is_swing = True
        for j in range(1, order + 1):
            if lows[i] >= lows[i - j] or lows[i] >= lows[i + j]:
                is_swing = False
                break
        if is_swing:
            swings.append((i, float(lows[i])))
    return swings


# ============================================================================
# CLUSTER MERGE
# ============================================================================

def _cluster_swings(swings: List[Tuple[int, float]], merge_pips: float = 80.0) -> List[dict]:
    """
    Cluster nearby swing points into zones.

    Args:
        swings: List of (bar_index, price) tuples
        merge_pips: Maximum distance in pips to merge two swings

    Returns:
        List of cluster dicts with keys: prices, indices, low, high, midpoint
    """
    if not swings:
        return []

    merge_dist = merge_pips * PIP_SIZE
    sorted_swings = sorted(swings, key=lambda x: x[1])

    clusters = []
    current_cluster = {
        'prices': [sorted_swings[0][1]],
        'indices': [sorted_swings[0][0]],
    }

    for i in range(1, len(sorted_swings)):
        price = sorted_swings[i][1]
        idx = sorted_swings[i][0]

        # Check distance to cluster midpoint
        cluster_mid = np.mean(current_cluster['prices'])
        if abs(price - cluster_mid) <= merge_dist:
            current_cluster['prices'].append(price)
            current_cluster['indices'].append(idx)
        else:
            # Finalize current cluster
            clusters.append(current_cluster)
            current_cluster = {
                'prices': [price],
                'indices': [idx],
            }

    clusters.append(current_cluster)

    # Compute boundaries
    for c in clusters:
        c['low'] = min(c['prices'])
        c['high'] = max(c['prices'])
        c['midpoint'] = np.mean(c['prices'])

    return clusters


# ============================================================================
# TOUCH COUNTING & ZONE CLASSIFICATION
# ============================================================================

def _count_touches(cluster: dict, highs: np.ndarray, lows: np.ndarray,
                   closes: np.ndarray, opens: np.ndarray,
                   tolerance_pips: float = 30.0) -> Tuple[int, int, str, int, int]:
    """
    Count how many times price touched a zone and classify it.

    A "touch" is when price enters the zone (within tolerance) and the candle
    shows a reversal (close moves away from the zone).

    Returns:
        Tuple: (total_touches, support_touches, resistance_touches,
                zone_type, last_touch_bar_from_end)
    """
    zone_low = cluster['low'] - tolerance_pips * PIP_SIZE
    zone_high = cluster['high'] + tolerance_pips * PIP_SIZE
    n_bars = len(highs)

    support_touches = 0
    resistance_touches = 0
    last_touch_bar = -1

    for i in range(n_bars):
        # Check if candle reached the zone
        candle_entered_zone = (lows[i] <= zone_high and highs[i] >= zone_low)
        if not candle_entered_zone:
            continue

        # Determine if it was a support or resistance touch
        body_direction = closes[i] - opens[i]

        # Support touch: price dipped into zone from above and bounced up
        if lows[i] <= zone_high and lows[i] >= zone_low - tolerance_pips * PIP_SIZE:
            if body_direction > 0:  # Bullish candle (bounced up)
                support_touches += 1
                last_touch_bar = i

        # Resistance touch: price reached zone from below and rejected down
        if highs[i] >= zone_low and highs[i] <= zone_high + tolerance_pips * PIP_SIZE:
            if body_direction < 0:  # Bearish candle (rejected down)
                resistance_touches += 1
                last_touch_bar = i

    total = support_touches + resistance_touches

    # Classify zone type
    if support_touches > 0 and resistance_touches > 0:
        zone_type = "FLIP"
    elif support_touches >= resistance_touches:
        zone_type = "SUPPORT"
    else:
        zone_type = "RESISTANCE"

    # Last touch as bars from end
    last_touch_from_end = (n_bars - 1 - last_touch_bar) if last_touch_bar >= 0 else 9999

    return total, support_touches, resistance_touches, zone_type, last_touch_from_end


# ============================================================================
# MAIN DETECTION
# ============================================================================

def detect_zones(df: pd.DataFrame, timeframe: str = "H1",
                 merge_pips: float = 80.0, max_age_bars: int = 200,
                 min_touches: int = 2, lookback: int = 200,
                 fractal_order: int = 2,
                 touch_tolerance_pips: float = 30.0) -> List[SRZone]:
    """
    Detect S/R zones from OHLCV data.

    Args:
        df: DataFrame with 'high', 'low', 'close', 'open' columns
        timeframe: "H1" or "H4" (for labeling)
        merge_pips: Distance in pips to merge nearby swings
        max_age_bars: Discard zones not touched in this many bars
        min_touches: Minimum touches to qualify as a zone
        lookback: Number of bars to analyze
        fractal_order: Fractal order (2 = 5-bar pattern)
        touch_tolerance_pips: Tolerance for touch detection

    Returns:
        List of SRZone objects, sorted by midpoint price
    """
    if df is None or len(df) < lookback // 2:
        return []

    # Use last `lookback` bars
    df_slice = df.tail(lookback).copy()
    n = len(df_slice)

    highs = df_slice['high'].values.astype(float)
    lows = df_slice['low'].values.astype(float)
    closes = df_slice['close'].values.astype(float)
    opens = df_slice['open'].values.astype(float)

    # Find swing points
    swing_highs = _find_swing_highs(highs, order=fractal_order)
    swing_lows = _find_swing_lows(lows, order=fractal_order)

    # Combine all swings
    all_swings = swing_highs + swing_lows

    if not all_swings:
        return []

    # Cluster
    clusters = _cluster_swings(all_swings, merge_pips=merge_pips)

    # Build zones
    zones = []
    for cluster in clusters:
        total, sup_t, res_t, zone_type, last_touch_from_end = _count_touches(
            cluster, highs, lows, closes, opens,
            tolerance_pips=touch_tolerance_pips
        )

        # Filter: minimum touches
        if total < min_touches:
            continue

        # Filter: max age (zone must have been touched recently)
        if last_touch_from_end > max_age_bars:
            continue

        # Strength classification
        if total >= 4:
            strength = "strong"
        elif total >= 3:
            strength = "moderate"
        else:
            strength = "weak"

        zone = SRZone(
            price_low=round(cluster['low'], 2),
            price_high=round(cluster['high'], 2),
            midpoint=round(cluster['midpoint'], 2),
            zone_type=zone_type,
            touches=total,
            timeframe=timeframe,
            last_touch_bar=last_touch_from_end,
            strength=strength,
            swing_points=[round(p, 2) for p in cluster['prices']],
        )
        zones.append(zone)

    # Sort by midpoint
    zones.sort(key=lambda z: z.midpoint)

    return zones


def _merge_lower_into_higher(lower_zones: List[SRZone], higher_zones: List[SRZone],
                              merge_pips: float) -> List[SRZone]:
    """
    Merge lower-timeframe zones into higher-timeframe zones.
    Higher TF absorbs overlapping lower TF zones, adds touches, and tracks confluence.

    Returns:
        List of lower-TF zones that were NOT absorbed (survivors).
    """
    if not higher_zones or not lower_zones:
        return lower_zones

    merge_dist = merge_pips * PIP_SIZE
    survivors = []

    for lz in lower_zones:
        absorbed = False
        for hz in higher_zones:
            if abs(lz.midpoint - hz.midpoint) <= merge_dist:
                # Higher TF absorbs lower TF zone — accumulate touches for multi-TF confirmation
                hz.touches += lz.touches
                # Track confluence: add lower TF label if not already present
                if lz.timeframe not in hz.confluence:
                    hz.confluence.append(lz.timeframe)
                # Also propagate any existing confluence from the lower zone
                for tf in lz.confluence:
                    if tf not in hz.confluence:
                        hz.confluence.append(tf)
                # Upgrade strength
                if hz.touches >= 4:
                    hz.strength = "strong"
                elif hz.touches >= 3:
                    hz.strength = "moderate"
                absorbed = True
                break
        if not absorbed:
            survivors.append(lz)

    return survivors


def _upgrade_confluence_strength(zones: List[SRZone]) -> None:
    """Upgrade strength for zones with multi-TF confluence."""
    for z in zones:
        if len(z.confluence) >= 2:
            # +1 strength tier for 2-TF confluence
            if z.strength == "weak":
                z.strength = "moderate"
            elif z.strength == "moderate":
                z.strength = "strong"
        # 3-TF confluence (H1+H4+D1) is always strong
        if len(z.confluence) >= 3:
            z.strength = "strong"


def detect_zones_triple(df_h1: pd.DataFrame, df_h4: pd.DataFrame,
                        df_d1: pd.DataFrame = None,
                        merge_pips: float = 80.0,
                        merge_pips_d1: float = 150.0,
                        max_age_bars: int = 500,
                        min_touches: int = 2,
                        lookback_h1: int = 200,
                        lookback_h4: int = 540,
                        lookback_d1: int = 130) -> List[SRZone]:
    """
    Detect S/R zones from H1, H4, and D1 data with multi-timeframe confluence.
    Bottom-up merge: H1 → H4 → D1 (higher TF absorbs lower).

    Args:
        df_h1: H1 OHLCV DataFrame
        df_h4: H4 OHLCV DataFrame
        df_d1: D1 OHLCV DataFrame (optional — falls back to H1+H4 only)
        merge_pips: Merge radius for H1/H4 zones
        merge_pips_d1: Merge radius for D1 zones (wider)
        max_age_bars: Max bars since last touch
        min_touches: Minimum touches to qualify
        lookback_h1: H1 bars to analyze
        lookback_h4: H4 bars to analyze
        lookback_d1: D1 bars to analyze

    Returns:
        Combined list of SRZone objects, sorted by midpoint
    """
    # Detect independently on each timeframe
    h1_zones = detect_zones(df_h1, timeframe="H1", merge_pips=merge_pips,
                            max_age_bars=max_age_bars, min_touches=min_touches,
                            lookback=lookback_h1)
    h4_zones = detect_zones(df_h4, timeframe="H4", merge_pips=merge_pips,
                            max_age_bars=max_age_bars, min_touches=min_touches,
                            lookback=lookback_h4)

    d1_zones = []
    if df_d1 is not None and len(df_d1) > 30:
        d1_zones = detect_zones(df_d1, timeframe="D1", merge_pips=merge_pips_d1,
                                max_age_bars=max_age_bars, min_touches=min_touches,
                                lookback=lookback_d1)

    # Bottom-up merge: H1 → H4
    surviving_h1 = _merge_lower_into_higher(h1_zones, h4_zones, merge_pips)

    # H4 → D1
    surviving_h4 = _merge_lower_into_higher(h4_zones, d1_zones, merge_pips_d1)

    # Remaining H1 zones that weren't absorbed by H4 — try merging into D1
    surviving_h1 = _merge_lower_into_higher(surviving_h1, d1_zones, merge_pips_d1)

    # Ensure each zone's own TF is in confluence if it absorbed others
    for z in d1_zones + surviving_h4 + surviving_h1:
        if z.confluence and z.timeframe not in z.confluence:
            z.confluence.insert(0, z.timeframe)
        # Sort confluence by hierarchy
        tf_order = {"D1": 0, "H4": 1, "H1": 2}
        z.confluence.sort(key=lambda t: tf_order.get(t, 9))

    # Upgrade strength for confluent zones
    _upgrade_confluence_strength(d1_zones)
    _upgrade_confluence_strength(surviving_h4)
    _upgrade_confluence_strength(surviving_h1)

    # Combine all
    combined = d1_zones + surviving_h4 + surviving_h1
    combined.sort(key=lambda z: z.midpoint)
    return combined


def detect_zones_dual(df_h1: pd.DataFrame, df_h4: pd.DataFrame,
                      merge_pips: float = 80.0, max_age_bars: int = 200,
                      min_touches: int = 2) -> List[SRZone]:
    """
    Backward-compatible wrapper: detect S/R zones from H1 and H4 data.
    Delegates to detect_zones_triple() with df_d1=None.
    """
    return detect_zones_triple(
        df_h1, df_h4, df_d1=None,
        merge_pips=merge_pips, max_age_bars=max_age_bars,
        min_touches=min_touches,
    )


# ============================================================================
# S/R CONTEXT FOR BRAIN
# ============================================================================

def get_sr_context(zones: List[SRZone], current_price: float, atr: float,
                   direction: Optional[str] = None,
                   confidence_penalty_max: float = 10.0,
                   confidence_bonus_max: float = 10.0,
                   penalty_proximity_atr: float = 0.5,
                   penalty_min_touches: int = 4) -> SRContext:
    """
    Analyze S/R context for the current price and trade direction.

    Args:
        zones: List of detected S/R zones
        current_price: Current price
        atr: Current ATR value
        direction: "BUY", "SELL", or None (for HOLD analysis)
        confidence_penalty_max: Maximum confidence penalty for trading into S/R
        confidence_bonus_max: Maximum confidence bonus for bouncing off S/R
        penalty_proximity_atr: Penalty fires only within this fraction of ATR
        penalty_min_touches: Minimum zone touches for penalty to fire

    Returns:
        SRContext with adjustments, confirmations, alerts
    """
    ctx = SRContext(all_zones=zones)

    if not zones or atr <= 0:
        ctx.description = "No S/R zones detected"
        return ctx

    # Find nearest support (below price) and resistance (above price)
    for zone in zones:
        if zone.midpoint < current_price:
            dist = (current_price - zone.midpoint) / PIP_SIZE
            if dist < ctx.dist_to_support_pips:
                ctx.dist_to_support_pips = dist
                ctx.nearest_support = zone
        elif zone.midpoint > current_price:
            dist = (zone.midpoint - current_price) / PIP_SIZE
            if dist < ctx.dist_to_resistance_pips:
                ctx.dist_to_resistance_pips = dist
                ctx.nearest_resistance = zone

    # ATR in pips for distance comparison
    atr_pips = atr / PIP_SIZE

    # Build description
    desc_parts = []
    if ctx.nearest_support:
        s = ctx.nearest_support
        desc_parts.append(f"Support: {s.midpoint:.2f} ({s.touches}T, {s.timeframe}, {ctx.dist_to_support_pips:.0f} pips below)")
    if ctx.nearest_resistance:
        r = ctx.nearest_resistance
        desc_parts.append(f"Resistance: {r.midpoint:.2f} ({r.touches}T, {r.timeframe}, {ctx.dist_to_resistance_pips:.0f} pips above)")
    ctx.description = " | ".join(desc_parts) if desc_parts else "No nearby S/R zones"

    if direction is None:
        return ctx

    direction = direction.upper()

    # ================================================================
    # CONFIDENCE ADJUSTMENTS
    # ================================================================

    # BUY near strong resistance → penalty
    if direction == "BUY" and ctx.nearest_resistance:
        r = ctx.nearest_resistance
        penalty_range = atr_pips * penalty_proximity_atr
        if ctx.dist_to_resistance_pips <= penalty_range and r.touches >= penalty_min_touches:
            # Scale penalty by zone strength and proximity
            proximity_factor = 1.0 - (ctx.dist_to_resistance_pips / penalty_range)  # 1.0 = at zone, 0.0 = at range edge
            strength_factor = min(r.touches / 5.0, 1.0)  # 5+ touches = max
            penalty = confidence_penalty_max * proximity_factor * strength_factor
            penalty = round(min(penalty, confidence_penalty_max), 1)
            if penalty > 0:
                ctx.confidence_adjustment -= penalty
                ctx.alerts.append(
                    f"BUY into {r.timeframe} resistance at {r.midpoint:.2f} "
                    f"({r.touches} touches, {ctx.dist_to_resistance_pips:.0f} pips away) "
                    f"— confidence -{penalty:.0f}"
                )

    # SELL near strong support → penalty
    if direction == "SELL" and ctx.nearest_support:
        s = ctx.nearest_support
        penalty_range = atr_pips * penalty_proximity_atr
        if ctx.dist_to_support_pips <= penalty_range and s.touches >= penalty_min_touches:
            proximity_factor = 1.0 - (ctx.dist_to_support_pips / penalty_range)
            strength_factor = min(s.touches / 5.0, 1.0)
            penalty = confidence_penalty_max * proximity_factor * strength_factor
            penalty = round(min(penalty, confidence_penalty_max), 1)
            if penalty > 0:
                ctx.confidence_adjustment -= penalty
                ctx.alerts.append(
                    f"SELL into {s.timeframe} support at {s.midpoint:.2f} "
                    f"({s.touches} touches, {ctx.dist_to_support_pips:.0f} pips away) "
                    f"— confidence -{penalty:.0f}"
                )

    # BUY bouncing off support → bonus
    if direction == "BUY" and ctx.nearest_support:
        s = ctx.nearest_support
        # "Bouncing off" = price is within 0.5×ATR above support and support has touches
        if ctx.dist_to_support_pips <= atr_pips * 0.5 and s.touches >= 2:
            strength_factor = min(s.touches / 4.0, 1.0)
            bonus = confidence_bonus_max * strength_factor
            bonus = round(min(bonus, confidence_bonus_max), 1)
            if bonus > 0:
                ctx.confidence_adjustment += bonus
                ctx.confirmations.append(
                    f"BUY near {s.timeframe} support at {s.midpoint:.2f} "
                    f"({s.touches} touches) — confidence +{bonus:.0f}"
                )

    # SELL rejected at resistance → bonus
    if direction == "SELL" and ctx.nearest_resistance:
        r = ctx.nearest_resistance
        if ctx.dist_to_resistance_pips <= atr_pips * 0.5 and r.touches >= 2:
            strength_factor = min(r.touches / 4.0, 1.0)
            bonus = confidence_bonus_max * strength_factor
            bonus = round(min(bonus, confidence_bonus_max), 1)
            if bonus > 0:
                ctx.confidence_adjustment += bonus
                ctx.confirmations.append(
                    f"SELL near {r.timeframe} resistance at {r.midpoint:.2f} "
                    f"({r.touches} touches) — confidence +{bonus:.0f}"
                )

    # Breakout confirmations
    if direction == "BUY" and ctx.nearest_resistance:
        r = ctx.nearest_resistance
        if current_price > r.price_high and r.touches >= 3:
            ctx.confirmations.append(
                f"Breakout above {r.timeframe} resistance at {r.midpoint:.2f} ({r.touches} touches)"
            )

    if direction == "SELL" and ctx.nearest_support:
        s = ctx.nearest_support
        if current_price < s.price_low and s.touches >= 3:
            ctx.confirmations.append(
                f"Breakout below {s.timeframe} support at {s.midpoint:.2f} ({s.touches} touches)"
            )

    return ctx


# ============================================================================
# SL/TP ADJUSTMENT
# ============================================================================

def adjust_sl_tp_for_sr(entry_price: float, stop_loss: float, take_profit: float,
                        direction: str, atr: float, zones: List[SRZone],
                        sl_adjust_enabled: bool = True,
                        tp_adjust_enabled: bool = True,
                        min_zone_touches: int = 3) -> Tuple[float, float, str]:
    """
    Adjust SL and TP based on nearby S/R zones.

    - SL: If a strong zone exists just beyond ATR-based SL, extend SL past the zone.
    - TP: If a strong zone exists between entry and ATR-based TP, pull TP before the zone.

    Args:
        entry_price: Trade entry price
        stop_loss: ATR-based stop loss
        take_profit: ATR-based take profit
        direction: "BUY" or "SELL"
        atr: Current ATR value
        zones: List of S/R zones
        sl_adjust_enabled: Whether to adjust SL
        tp_adjust_enabled: Whether to adjust TP
        min_zone_touches: Minimum touches for a zone to trigger adjustment

    Returns:
        Tuple: (adjusted_sl, adjusted_tp, description)
    """
    adj_sl = stop_loss
    adj_tp = take_profit
    desc_parts = []
    max_sl_dist = 1.5 * atr  # Only adjust for zones within 1.5×ATR of the ATR-based level

    direction = direction.upper()

    if not zones:
        return adj_sl, adj_tp, ""

    if direction == "BUY":
        # SL is below entry. Check if a support zone is just beyond SL.
        if sl_adjust_enabled:
            for zone in zones:
                if zone.touches < min_zone_touches:
                    continue
                # Zone is below SL but within 1.5×ATR of SL
                if zone.price_low < stop_loss and (stop_loss - zone.price_low) <= max_sl_dist:
                    # Extend SL past the zone (give zone room to hold)
                    new_sl = zone.price_low - 10 * PIP_SIZE  # 10 pips past zone
                    if new_sl < adj_sl:
                        adj_sl = round(new_sl, 2)
                        desc_parts.append(
                            f"SL extended past {zone.timeframe} support at {zone.midpoint:.2f} "
                            f"({zone.touches}T) → SL {adj_sl:.2f}"
                        )
                        break  # Only adjust for nearest relevant zone

        # TP is above entry. Check if a resistance zone is between entry and TP.
        if tp_adjust_enabled:
            for zone in reversed(zones):  # Check from highest to lowest
                if zone.touches < min_zone_touches:
                    continue
                # Zone is between entry and TP
                if entry_price < zone.price_low < take_profit:
                    # Pull TP to just before the zone
                    new_tp = zone.price_low - 5 * PIP_SIZE  # 5 pips before zone
                    if new_tp > entry_price + atr * 0.5:  # Don't pull TP too close
                        adj_tp = round(new_tp, 2)
                        desc_parts.append(
                            f"TP pulled before {zone.timeframe} resistance at {zone.midpoint:.2f} "
                            f"({zone.touches}T) → TP {adj_tp:.2f}"
                        )
                        break

    elif direction == "SELL":
        # SL is above entry. Check if a resistance zone is just beyond SL.
        if sl_adjust_enabled:
            for zone in reversed(zones):
                if zone.touches < min_zone_touches:
                    continue
                # Zone is above SL but within 1.5×ATR of SL
                if zone.price_high > stop_loss and (zone.price_high - stop_loss) <= max_sl_dist:
                    new_sl = zone.price_high + 10 * PIP_SIZE
                    if new_sl > adj_sl:
                        adj_sl = round(new_sl, 2)
                        desc_parts.append(
                            f"SL extended past {zone.timeframe} resistance at {zone.midpoint:.2f} "
                            f"({zone.touches}T) → SL {adj_sl:.2f}"
                        )
                        break

        # TP is below entry. Check if a support zone is between entry and TP.
        if tp_adjust_enabled:
            for zone in zones:
                if zone.touches < min_zone_touches:
                    continue
                # Zone is between TP and entry
                if take_profit < zone.price_high < entry_price:
                    new_tp = zone.price_high + 5 * PIP_SIZE
                    if new_tp < entry_price - atr * 0.5:  # Don't pull TP too close
                        adj_tp = round(new_tp, 2)
                        desc_parts.append(
                            f"TP pulled before {zone.timeframe} support at {zone.midpoint:.2f} "
                            f"({zone.touches}T) → TP {adj_tp:.2f}"
                        )
                        break

    description = " | ".join(desc_parts)
    return adj_sl, adj_tp, description


# ============================================================================
# SCENARIO CHECK (for central_brain)
# ============================================================================

def is_near_strong_zone(zones: List[SRZone], current_price: float,
                        atr: float, min_touches: int = 4) -> Tuple[bool, Optional[SRZone]]:
    """
    Check if price is within 0.5×ATR of a strong H4 or D1 zone.
    Used by _identify_scenario() for zona_sr_forte.
    D1 zones are prioritized over H4 (more significant structural levels).

    Returns:
        Tuple: (is_near, zone_or_None)
    """
    threshold = atr * 0.5

    # Check D1 first (most significant), then H4
    for tf in ("D1", "H4"):
        for zone in zones:
            if zone.touches < min_touches:
                continue
            if zone.timeframe != tf:
                continue

            dist = abs(current_price - zone.midpoint)
            if dist <= threshold:
                return True, zone

    return False, None


# ============================================================================
# UTILITY: FORMAT FOR EXPLANATION
# ============================================================================

def format_zones_for_explanation(zones: List[SRZone], current_price: float,
                                max_zones: int = 3) -> str:
    """Format nearest zones for brain explanation text."""
    if not zones:
        return "  No S/R zones detected"

    above = [z for z in zones if z.midpoint > current_price]
    below = [z for z in zones if z.midpoint <= current_price]

    # Nearest above (sorted ascending)
    above_nearest = sorted(above, key=lambda z: z.midpoint)[:max_zones]
    # Nearest below (sorted descending)
    below_nearest = sorted(below, key=lambda z: -z.midpoint)[:max_zones]

    lines = []
    if above_nearest:
        lines.append("  Resistance above:")
        for z in above_nearest:
            dist = (z.midpoint - current_price) / PIP_SIZE
            lines.append(
                f"    {z.midpoint:.2f} ({z.zone_type}, {z.touches}T, {z.timeframe}, "
                f"{z.strength}, {dist:.0f} pips)"
            )
    if below_nearest:
        lines.append("  Support below:")
        for z in below_nearest:
            dist = (current_price - z.midpoint) / PIP_SIZE
            lines.append(
                f"    {z.midpoint:.2f} ({z.zone_type}, {z.touches}T, {z.timeframe}, "
                f"{z.strength}, {dist:.0f} pips)"
            )

    return "\n".join(lines)


# ============================================================================
# TEST
# ============================================================================

def test_support_resistance():
    """Basic test with synthetic data."""
    print("=" * 60)
    print("SUPPORT & RESISTANCE TEST")
    print("=" * 60)

    # Create synthetic H1 data with clear S/R levels
    np.random.seed(42)
    n = 200
    base = 2900.0
    prices = np.zeros(n)
    prices[0] = base

    # Create a range-bound market between 2880 and 2920
    for i in range(1, n):
        prices[i] = prices[i - 1] + np.random.randn() * 3
        # Bounce off support ~2880
        if prices[i] < 2880:
            prices[i] = 2880 + abs(np.random.randn()) * 2
        # Bounce off resistance ~2920
        if prices[i] > 2920:
            prices[i] = 2920 - abs(np.random.randn()) * 2

    df = pd.DataFrame({
        'open': prices - np.random.rand(n) * 2,
        'high': prices + np.random.rand(n) * 5,
        'low': prices - np.random.rand(n) * 5,
        'close': prices,
        'volume': np.random.randint(1000, 5000, n),
    })

    print(f"\nData: {n} bars, price range {df['low'].min():.2f} - {df['high'].max():.2f}")

    # Detect zones
    zones = detect_zones(df, timeframe="H1")
    print(f"\nDetected {len(zones)} zones:")
    for z in zones:
        print(f"  {z.zone_type} @ {z.midpoint:.2f} ({z.price_low:.2f}-{z.price_high:.2f}) "
              f"| {z.touches}T | {z.strength} | last touch {z.last_touch_bar} bars ago")

    # Get context
    current = float(df['close'].iloc[-1])
    atr = float((df['high'] - df['low']).tail(14).mean())
    ctx = get_sr_context(zones, current, atr, direction="BUY")
    print(f"\nContext for BUY @ {current:.2f} (ATR={atr:.2f}):")
    print(f"  {ctx.description}")
    print(f"  Confidence adjustment: {ctx.confidence_adjustment:+.1f}")
    for c in ctx.confirmations:
        print(f"  + {c}")
    for a in ctx.alerts:
        print(f"  ! {a}")

    print(f"\n{format_zones_for_explanation(zones, current)}")
    print("\nTest complete!")


if __name__ == "__main__":
    test_support_resistance()
