"""
RISK MANAGER - Risk Management
Calculates lot size, stop loss, take profit
"""

from dataclasses import dataclass
from typing import Tuple, Optional
import config


@dataclass
class PositionSize:
    """Position size calculation result"""
    lot_size: float
    risk_amount: float
    stop_loss_pips: float
    potential_loss: float
    potential_profit_tp1: float
    potential_profit_tp2: float


@dataclass
class StopLevels:
    """SL and TP levels"""
    entry_price: float
    stop_loss: float
    take_profit_1: float
    take_profit_2: float
    sl_pips: float
    tp1_pips: float
    tp2_pips: float
    risk_reward_1: float
    risk_reward_2: float
    # FLO-299 #22: clamp-visibility fields. When auto-sizing clamps SL to
    # [MIN_SL_PIPS, MAX_SL_PIPS], consumers (Floki, logs, audit) see the
    # raw vs capped values and whether a clamp occurred.
    sl_pips_raw: Optional[float] = None
    sl_pips_clamped_from: Optional[float] = None
    sl_clamped: bool = False
    sl_clamp_reason: Optional[str] = None


def calculate_position_size(
    account_balance: float,
    risk_percent: float,
    stop_loss_pips: float
) -> PositionSize:
    """
    Calculate position size based on risk.
    
    Args:
        account_balance: Account balance in USD
        risk_percent: Risk percentage (e.g.: 2.0 for 2%)
        stop_loss_pips: SL distance in pips
    
    Returns:
        PositionSize with lot_size and details
    """
    # Calculate risk amount
    risk_amount = account_balance * (risk_percent / 100)
    
    # Pip value per lot (XAU/USD: $10 per pip for 1 lot)
    pip_value = config.PIP_VALUE_PER_LOT
    
    # Calculate lot size
    if stop_loss_pips > 0:
        lot_size = risk_amount / (stop_loss_pips * pip_value)
    else:
        lot_size = config.MIN_LOT_SIZE
    
    # Round to lot step (0.01)
    lot_size = round(lot_size / config.LOT_STEP) * config.LOT_STEP
    
    # Apply limits
    lot_size = max(config.MIN_LOT_SIZE, min(lot_size, config.MAX_LOT_SIZE))
    
    # Calculate potentials
    actual_pip_value = lot_size * pip_value
    potential_loss = stop_loss_pips * actual_pip_value
    
    # TP1 = 2x ATR, TP2 = 3x ATR (assumindo SL = 1.5x ATR)
    tp1_pips = stop_loss_pips * (config.TAKE_PROFIT_1_ATR_MULT / config.STOP_LOSS_ATR_MULT)
    tp2_pips = stop_loss_pips * (config.TAKE_PROFIT_2_ATR_MULT / config.STOP_LOSS_ATR_MULT)
    
    potential_profit_tp1 = tp1_pips * actual_pip_value
    potential_profit_tp2 = tp2_pips * actual_pip_value
    
    return PositionSize(
        lot_size=lot_size,
        risk_amount=risk_amount,
        stop_loss_pips=stop_loss_pips,
        potential_loss=potential_loss,
        potential_profit_tp1=potential_profit_tp1,
        potential_profit_tp2=potential_profit_tp2
    )


def calculate_sl_tp(
    entry_price: float,
    direction: str,
    atr_value: float
) -> StopLevels:
    """
    Calculate SL and TP levels based on ATR.
    
    Args:
        entry_price: Entry price
        direction: "BUY" or "SELL"
        atr_value: ATR value (Average True Range)
    
    Returns:
        StopLevels with all levels
    """
    # Calculate distances based on ATR
    sl_distance = atr_value * config.STOP_LOSS_ATR_MULT
    
    # Convert to pips and apply limits (XAU/USD: 1 pip = 0.1)
    pip_size = 0.1
    sl_pips_raw = sl_distance / pip_size
    
    # Apply MIN/MAX SL limits in pips
    min_sl = getattr(config, 'MIN_SL_PIPS', 100)
    max_sl = getattr(config, 'MAX_SL_PIPS', 200)
    sl_pips_capped = max(min_sl, min(sl_pips_raw, max_sl))

    # FLO-299 #22: track clamp status so downstream consumers (StopLevels
    # return value, logs) can surface the fact to Floki.
    _sl_clamped = (sl_pips_capped != sl_pips_raw)
    if _sl_clamped:
        if sl_pips_raw < min_sl:
            _sl_clamp_reason = f"sl_below_min ({sl_pips_raw:.1f} < {min_sl})"
        else:
            _sl_clamp_reason = f"sl_above_max ({sl_pips_raw:.1f} > {max_sl})"
    else:
        _sl_clamp_reason = None

    # Recalculate distance with capped SL
    sl_distance = sl_pips_capped * pip_size
    
    # TP proportional to SL (maintains ATR ratio)
    tp1_ratio = config.TAKE_PROFIT_1_ATR_MULT / config.STOP_LOSS_ATR_MULT
    tp2_ratio = config.TAKE_PROFIT_2_ATR_MULT / config.STOP_LOSS_ATR_MULT
    tp1_distance = sl_distance * tp1_ratio
    tp2_distance = sl_distance * tp2_ratio
    
    if direction.upper() == "BUY":
        stop_loss = entry_price - sl_distance
        take_profit_1 = entry_price + tp1_distance
        take_profit_2 = entry_price + tp2_distance
    else:  # SELL
        stop_loss = entry_price + sl_distance
        take_profit_1 = entry_price - tp1_distance
        take_profit_2 = entry_price - tp2_distance
    
    # Calculate pips (XAU/USD: 1 pip = 0.1)
    pip_size = 0.1
    sl_pips = abs(entry_price - stop_loss) / pip_size
    tp1_pips = abs(take_profit_1 - entry_price) / pip_size
    tp2_pips = abs(take_profit_2 - entry_price) / pip_size
    
    # Risk/Reward ratios
    risk_reward_1 = tp1_pips / sl_pips if sl_pips > 0 else 0
    risk_reward_2 = tp2_pips / sl_pips if sl_pips > 0 else 0
    
    return StopLevels(
        entry_price=round(entry_price, 2),
        stop_loss=round(stop_loss, 2),
        take_profit_1=round(take_profit_1, 2),
        take_profit_2=round(take_profit_2, 2),
        sl_pips=round(sl_pips, 1),
        tp1_pips=round(tp1_pips, 1),
        tp2_pips=round(tp2_pips, 1),
        risk_reward_1=round(risk_reward_1, 2),
        risk_reward_2=round(risk_reward_2, 2),
        # FLO-299 #22: surface auto-sizing SL clamp.
        sl_pips_raw=round(sl_pips_raw, 1),
        sl_pips_clamped_from=round(sl_pips_raw, 1) if _sl_clamped else None,
        sl_clamped=_sl_clamped,
        sl_clamp_reason=_sl_clamp_reason,
    )


def calculate_breakeven_sl(
    entry_price: float,
    direction: str,
    spread_pips: float = 2.0
) -> float:
    """
    Calculate SL at breakeven (+ spread).
    
    Args:
        entry_price: Entry price
        direction: "BUY" or "SELL"
        spread_pips: Spread in pips to ensure real breakeven
    
    Returns:
        New SL price at breakeven
    """
    pip_size = 0.1
    spread_value = spread_pips * pip_size
    
    if direction.upper() == "BUY":
        # For BUY, SL goes slightly above entry
        return round(entry_price + spread_value, 2)
    else:
        # For SELL, SL goes slightly below entry
        return round(entry_price - spread_value, 2)


def calculate_trailing_stop(
    current_price: float,
    direction: str,
    current_sl: float,
    trailing_distance_pips: float = 15.0
) -> Optional[float]:
    """
    Calculate new SL for trailing stop.
    
    Args:
        current_price: Current price
        direction: "BUY" or "SELL"
        current_sl: Current SL
        trailing_distance_pips: Trailing distance in pips
    
    Returns:
        New SL if it should move, None if not
    """
    pip_size = 0.1
    trailing_distance = trailing_distance_pips * pip_size
    
    if direction.upper() == "BUY":
        # For BUY, SL rises when price rises
        new_sl = current_price - trailing_distance
        if new_sl > current_sl:
            return round(new_sl, 2)
    else:
        # For SELL, SL drops when price drops
        new_sl = current_price + trailing_distance
        if new_sl < current_sl:
            return round(new_sl, 2)
    
    return None


def validate_risk_params(
    account_balance: float,
    lot_size: float,
    stop_loss_pips: float
) -> Tuple[bool, str]:
    """
    Validate if risk parameters are within limits.
    
    Returns:
        Tuple: (is_valid, message)
    """
    # Calculate actual risk
    pip_value = lot_size * config.PIP_VALUE_PER_LOT
    potential_loss = stop_loss_pips * pip_value
    risk_percent = (potential_loss / account_balance) * 100
    
    # Check limits
    if risk_percent > config.RISK_PER_TRADE * 1.5:  # 50% margin
        return False, f"Risk too high: {risk_percent:.1f}% (max: {config.RISK_PER_TRADE}%)"
    
    if lot_size < config.MIN_LOT_SIZE:
        return False, f"Lot size too small: {lot_size} (min: {config.MIN_LOT_SIZE})"
    
    if lot_size > config.MAX_LOT_SIZE:
        return False, f"Lot size too large: {lot_size} (max: {config.MAX_LOT_SIZE})"
    
    if stop_loss_pips < 5:
        return False, f"SL too tight: {stop_loss_pips} pips (min: 5)"
    
    if stop_loss_pips > 50:
        return False, f"SL too wide: {stop_loss_pips} pips (max: 50)"
    
    return True, "OK"


# ============================================================================
# TESTS
# ============================================================================

def test_risk_manager():
    """Test the risk manager with examples"""
    print("=" * 60)
    print("🧪 RISK MANAGER TEST")
    print("=" * 60)
    
    # Example 1: Position size calculation
    print("\n📊 Example 1: Position Size")
    print(f"   Capital: $1000, Risk: 2%, SL: 15 pips")
    
    pos = calculate_position_size(
        account_balance=1000,
        risk_percent=2.0,
        stop_loss_pips=15
    )
    
    print(f"   Lot Size: {pos.lot_size}")
    print(f"   Risk in $: ${pos.risk_amount:.2f}")
    print(f"   Potential loss: ${pos.potential_loss:.2f}")
    print(f"   TP1 profit: ${pos.potential_profit_tp1:.2f}")
    print(f"   TP2 profit: ${pos.potential_profit_tp2:.2f}")
    
    # Example 2: SL/TP calculation
    print("\n📊 Example 2: SL/TP (BUY)")
    print(f"   Entry: 2650.00, ATR: 10.0")
    
    levels = calculate_sl_tp(
        entry_price=2650.00,
        direction="BUY",
        atr_value=10.0
    )
    
    print(f"   Stop Loss: {levels.stop_loss} ({levels.sl_pips} pips)")
    print(f"   Take Profit 1: {levels.take_profit_1} ({levels.tp1_pips} pips)")
    print(f"   Take Profit 2: {levels.take_profit_2} ({levels.tp2_pips} pips)")
    print(f"   Risk/Reward 1: 1:{levels.risk_reward_1}")
    print(f"   Risk/Reward 2: 1:{levels.risk_reward_2}")
    
    # Example 3: SL/TP calculation (SELL)
    print("\n📊 Example 3: SL/TP (SELL)")
    print(f"   Entry: 2650.00, ATR: 10.0")
    
    levels = calculate_sl_tp(
        entry_price=2650.00,
        direction="SELL",
        atr_value=10.0
    )
    
    print(f"   Stop Loss: {levels.stop_loss} ({levels.sl_pips} pips)")
    print(f"   Take Profit 1: {levels.take_profit_1} ({levels.tp1_pips} pips)")
    print(f"   Take Profit 2: {levels.take_profit_2} ({levels.tp2_pips} pips)")
    
    # Example 4: Breakeven
    print("\n📊 Example 4: Breakeven SL")
    be_sl = calculate_breakeven_sl(2650.00, "BUY", spread_pips=2.0)
    print(f"   BUY entry 2650.00 → Breakeven SL: {be_sl}")
    
    # Example 5: Trailing Stop
    print("\n📊 Example 5: Trailing Stop")
    new_sl = calculate_trailing_stop(
        current_price=2665.00,
        direction="BUY",
        current_sl=2635.00,
        trailing_distance_pips=15.0
    )
    print(f"   Current price: 2665.00, Current SL: 2635.00")
    print(f"   New SL (trailing 15 pips): {new_sl}")


if __name__ == "__main__":
    test_risk_manager()
