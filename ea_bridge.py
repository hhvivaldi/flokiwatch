"""
EA BRIDGE - Python ↔ MT5 EA Communication
Handles JSON file I/O between Python Brain and FlokiBridge EA
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

import MetaTrader5 as mt5
import config
from logger import log


@dataclass
class EAPosition:
    """Position info from EA status"""
    ticket: int
    direction: str
    volume: float
    open_price: float
    current_price: float
    sl: float
    tp: float
    profit: float
    profit_pips: float
    open_time: datetime
    phase: str  # OPEN, BREAKEVEN, TRAILING
    breakeven_hit: bool
    trailing_active: bool
    max_profit_pips: float


@dataclass
class EAStatus:
    """Full EA status"""
    version: int
    timestamp: datetime
    last_signal_id: str
    last_signal_result: str
    balance: float
    equity: float
    margin: float
    free_margin: float
    positions: List[EAPosition]
    closed_today: List[Dict]
    spread_pips: float
    last_error: Optional[str]
    is_stale: bool  # True if status file is older than threshold


def get_signal_file_path() -> str:
    """Get path to brain_signal.json"""
    return getattr(config, 'BRAIN_SIGNAL_JSON_PATH', 
                   os.path.join(os.path.dirname(config.SR_ZONES_JSON_PATH), 'brain_signal.json'))


def get_status_file_path() -> str:
    """Get path to ea_status.json"""
    return getattr(config, 'EA_STATUS_JSON_PATH',
                   os.path.join(os.path.dirname(config.SR_ZONES_JSON_PATH), 'ea_status.json'))


def clear_stale_signal(max_age_hours: float = 4.0) -> bool:
    """
    Clear brain_signal.json if it contains a stale signal.
    
    Should be called on Python startup to prevent old signals from reaching the EA.
    The EA already rejects signals >10 minutes old, but this prevents unnecessary
    file reads and log noise.
    
    Args:
        max_age_hours: Maximum age of signal before deletion (default 4 hours)
    
    Returns:
        True if file was deleted, False otherwise
    """
    try:
        file_path = get_signal_file_path()
        
        if not os.path.exists(file_path):
            return False
        
        # Check file modification time first (quick check)
        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
        age_hours = (datetime.now() - file_mtime).total_seconds() / 3600
        
        if age_hours <= max_age_hours:
            return False  # File is fresh enough
        
        # File is old - read and log the stale signal ID before deleting
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            signal_id = data.get('signal_id', 'unknown')
            signal_type = data.get('signal', 'unknown')
            log.info(f"EA Bridge: Clearing stale signal file (age: {age_hours:.1f}h) - ID: {signal_id}, Type: {signal_type}")
        except Exception:
            log.info(f"EA Bridge: Clearing stale signal file (age: {age_hours:.1f}h) - could not read contents")
        
        os.remove(file_path)
        return True
        
    except Exception as e:
        log.warning(f"EA Bridge: Failed to clear stale signal - {e}")
        return False


def write_signal(
    signal: str,
    sl: float,
    tp: float,
    lot_size: float,
    confidence: float,
    breakeven_trigger_pips: float,
    trailing_trigger_pips: float,
    trailing_distance_pips: float,
    max_drawdown_pips: float = 1000,
    comment: str = ""
) -> bool:
    """
    Write signal to brain_signal.json for EA to read.
    
    Args:
        signal: BUY, SELL, HOLD, or CLOSE
        sl: Stop loss price
        tp: Take profit price
        lot_size: Position size
        confidence: Brain confidence (0-100)
        breakeven_trigger_pips: Pips profit to move SL to entry
        trailing_trigger_pips: Pips profit to activate trailing
        trailing_distance_pips: Trailing distance behind price
        max_drawdown_pips: Emergency close threshold
        comment: Order comment
    
    Returns:
        True if successful
    """
    try:
        # Use MT5 server time to avoid timezone mismatch with EA
        # The EA compares signal timestamp against TimeCurrent() which is server time
        server_time = None
        try:
            if mt5.initialize():
                tick = mt5.symbol_info_tick(config.SYMBOL)
                if tick and tick.time:
                    server_time = datetime.fromtimestamp(tick.time)
        except Exception:
            pass
        
        # Fallback to local time if MT5 not available
        now = server_time if server_time else datetime.now()
        signal_id = now.strftime("%Y%m%d%H%M%S")
        
        payload = {
            "version": 1,
            "timestamp": now.strftime("%Y.%m.%d %H:%M:%S"),
            "signal_id": signal_id,
            "signal": signal.upper(),
            "sl": round(sl, 2),
            "tp": round(tp, 2),
            "lot_size": round(lot_size, 2),
            "confidence": round(confidence, 1),
            "magic": config.MAGIC_NUMBER,
            "comment": comment or f"Brain-{signal}-{int(confidence)}",
            "breakeven_trigger_pips": round(breakeven_trigger_pips, 0),
            "trailing_trigger_pips": round(trailing_trigger_pips, 0),
            "trailing_distance_pips": round(trailing_distance_pips, 0),
            "max_drawdown_pips": round(max_drawdown_pips, 0)
        }
        
        file_path = get_signal_file_path()
        
        # Write to temp file first, then rename (atomic)
        temp_path = file_path + ".tmp"
        with open(temp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        
        # Rename (atomic on most systems)
        if os.path.exists(file_path):
            os.remove(file_path)
        os.rename(temp_path, file_path)
        
        log.info(f"EA Bridge: Signal written - {signal} | SL:{sl:.2f} TP:{tp:.2f} Lot:{lot_size}")
        return True
        
    except Exception as e:
        log.error(f"EA Bridge: Failed to write signal - {e}")
        return False


def read_ea_status(stale_threshold_seconds: int = 60) -> Optional[EAStatus]:
    """
    Read ea_status.json from EA.
    
    Args:
        stale_threshold_seconds: If file is older than this, mark as stale
    
    Returns:
        EAStatus object, or None if file doesn't exist or can't be read
    """
    try:
        file_path = get_status_file_path()
        
        if not os.path.exists(file_path):
            return None
        
        # Check file age
        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
        age_seconds = (datetime.now() - file_mtime).total_seconds()
        is_stale = age_seconds > stale_threshold_seconds
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Parse positions
        positions = []
        for pos_data in data.get('positions', []):
            try:
                open_time_str = pos_data.get('open_time', '')
                # Handle MT5 format: "2026.02.24 15:30:00"
                if '.' in open_time_str and ' ' in open_time_str:
                    open_time = datetime.strptime(open_time_str, "%Y.%m.%d %H:%M:%S")
                else:
                    open_time = datetime.fromisoformat(open_time_str) if open_time_str else datetime.now()
                
                positions.append(EAPosition(
                    ticket=int(pos_data.get('ticket', 0)),
                    direction=pos_data.get('direction', ''),
                    volume=float(pos_data.get('volume', 0)),
                    open_price=float(pos_data.get('open_price', 0)),
                    current_price=float(pos_data.get('current_price', 0)),
                    sl=float(pos_data.get('sl', 0)),
                    tp=float(pos_data.get('tp', 0)),
                    profit=float(pos_data.get('profit', 0)),
                    profit_pips=float(pos_data.get('profit_pips', 0)),
                    open_time=open_time,
                    phase=pos_data.get('phase', 'OPEN'),
                    breakeven_hit=pos_data.get('breakeven_hit', False),
                    trailing_active=pos_data.get('trailing_active', False),
                    max_profit_pips=float(pos_data.get('max_profit_pips', 0))
                ))
            except Exception as e:
                log.warning(f"EA Bridge: Failed to parse position - {e}")
        
        # Parse timestamp
        ts_str = data.get('timestamp', '')
        if '.' in ts_str and ' ' in ts_str:
            timestamp = datetime.strptime(ts_str, "%Y.%m.%d %H:%M:%S")
        else:
            timestamp = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
        
        account = data.get('account', {})
        
        return EAStatus(
            version=data.get('version', 1),
            timestamp=timestamp,
            last_signal_id=data.get('last_signal_id', ''),
            last_signal_result=data.get('last_signal_result', ''),
            balance=float(account.get('balance', 0)),
            equity=float(account.get('equity', 0)),
            margin=float(account.get('margin', 0)),
            free_margin=float(account.get('free_margin', 0)),
            positions=positions,
            closed_today=data.get('closed_today', []),
            spread_pips=float(data.get('spread_pips', 0)),
            last_error=data.get('last_error'),
            is_stale=is_stale
        )
        
    except Exception as e:
        log.warning(f"EA Bridge: Failed to read status - {e}")
        return None


def is_ea_online(stale_threshold_seconds: int = 60) -> bool:
    """
    Check if EA is online (status file exists and is fresh).
    
    Args:
        stale_threshold_seconds: Max age of status file to consider EA online
    
    Returns:
        True if EA is online
    """
    status = read_ea_status(stale_threshold_seconds)
    if status is None:
        return False
    return not status.is_stale


def get_positions_from_ea() -> List[EAPosition]:
    """
    Get open positions from EA status.
    Returns empty list if EA is offline or no positions.
    """
    status = read_ea_status()
    if status is None:
        return []
    return status.positions


def get_ea_spread() -> Optional[float]:
    """Get current spread from EA status (in pips)."""
    status = read_ea_status()
    if status is None:
        return None
    return status.spread_pips


def get_ea_account_info() -> Optional[Dict]:
    """Get account info from EA status."""
    status = read_ea_status()
    if status is None:
        return None
    return {
        'balance': status.balance,
        'equity': status.equity,
        'margin': status.margin,
        'free_margin': status.free_margin
    }


# ============================================================================
# TEST
# ============================================================================

def test_ea_bridge():
    """Test the EA bridge"""
    print("=" * 60)
    print("🧪 EA BRIDGE TEST")
    print("=" * 60)
    
    print(f"\nSignal file: {get_signal_file_path()}")
    print(f"Status file: {get_status_file_path()}")
    
    print("\n📊 Test 1: Check EA online status")
    online = is_ea_online()
    print(f"   EA online: {online}")
    
    print("\n📊 Test 2: Read EA status")
    status = read_ea_status()
    if status:
        print(f"   Timestamp: {status.timestamp}")
        print(f"   Balance: ${status.balance:.2f}")
        print(f"   Positions: {len(status.positions)}")
        print(f"   Spread: {status.spread_pips:.1f} pips")
        print(f"   Stale: {status.is_stale}")
        print(f"   Last signal: {status.last_signal_id}")
        print(f"   Last result: {status.last_signal_result}")
    else:
        print("   Status not available")
    
    print("\n📊 Test 3: Write HOLD signal")
    success = write_signal(
        signal="HOLD",
        sl=0,
        tp=0,
        lot_size=0,
        confidence=50,
        breakeven_trigger_pips=100,
        trailing_trigger_pips=150,
        trailing_distance_pips=100
    )
    print(f"   Write success: {success}")
    
    print("\n✅ Tests complete!")


if __name__ == "__main__":
    test_ea_bridge()
