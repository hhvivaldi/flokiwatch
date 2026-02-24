"""
POSITION MONITOR - Position Management
Automatically manages open positions (breakeven, trailing stop, etc)
"""

from datetime import datetime, timedelta
from typing import List, Optional, Tuple
import config
from logger import log
from alerts import (
    alert_breakeven, alert_trade_closed, alert_trailing_stop,
    alert_sl_hit, discord
)
from executor import (
    executor, PositionInfo, get_positions,
    close_position, modify_sl, get_deal_history
)
from risk_manager import calculate_breakeven_sl, calculate_trailing_stop


class PositionMonitor:
    """Monitor and manage open positions"""
    
    def __init__(self):
        # Track positions that already hit breakeven
        self.breakeven_hit_tickets = set()
        # Track last trailing SL
        self.trailing_sl = {}
        # Track known positions (ticket → PositionInfo)
        self.known_positions = {}
        # Original SL in pips (captured first time we see the position)
        # Used for trailing triggers — does NOT change after breakeven
        self.original_sl_pips = {}
        # Flag: first cycle (populate without detecting closures)
        self._initialized = False
        # Volatility Guard status (updated by main.py each cycle)
        self.volatility_status = "NORMAL"
    
    def set_volatility_status(self, status: str):
        """Update volatility status (EXTREME, COOLING_DOWN, NORMAL)"""
        self.volatility_status = status
    
    def monitor_all_positions(self) -> List[dict]:
        """
        Monitor all open positions and take necessary actions.
        
        Returns:
            List of actions taken
        """
        actions = []
        positions = get_positions()
        current_tickets = {pos.ticket for pos in positions}
        
        # Detect positions closed by broker (SL/TP hit)
        if self._initialized:
            broker_actions = self._check_broker_closures(current_tickets)
            actions.extend(broker_actions)
        else:
            # First cycle: populate known_positions without detecting closures
            self._initialized = True
            if positions:
                log.info(f"   Monitor: {len(positions)} position(s) detected at startup")
        
        # Update known_positions with current positions
        self.known_positions = {pos.ticket: pos for pos in positions}
        
        # Capture original SL first time we see each position
        for pos in positions:
            if pos.ticket not in self.original_sl_pips:
                sl_dist = abs(pos.open_price - pos.sl) / 0.1  # pips
                self.original_sl_pips[pos.ticket] = sl_dist
                be_trig = sl_dist * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
                tr_trig = sl_dist * getattr(config, 'TRAILING_ATR_MULT', 0.7)
                tr_dist = sl_dist * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7)
                log.info(f"   Monitor: #{pos.ticket} {pos.direction} @ {pos.open_price:.2f} | SL original={pos.sl:.2f} ({sl_dist:.0f} pips)")
                log.info(f"   Monitor: #{pos.ticket} Triggers: BE={be_trig:.0f} pips, Trail={tr_trig:.0f} pips, Dist={tr_dist:.0f} pips")
        
        # Check if EA bridge is handling position management
        ea_handles_trailing = False
        if getattr(config, 'USE_EA_BRIDGE', False):
            try:
                from ea_bridge import is_ea_online
                stale_threshold = getattr(config, 'EA_STALE_THRESHOLD_SECONDS', 60)
                ea_handles_trailing = is_ea_online(stale_threshold)
            except Exception:
                pass
        
        for pos in positions:
            # Skip breakeven/trailing if EA is handling it
            if not ea_handles_trailing:
                # 1. Check breakeven (move SL to entry)
                action = self._check_breakeven(pos)
                if action:
                    actions.append(action)
                
                # 2. Check trailing stop
                action = self._check_trailing_stop(pos)
                if action:
                    actions.append(action)
            
            # 3. Check max time (Python still handles this as safety net)
            action = self._check_max_time(pos)
            if action:
                actions.append(action)
            
            # 4. Check excessive drawdown (Python still handles as safety net)
            action = self._check_max_drawdown(pos)
            if action:
                actions.append(action)
        
        # Status log per position (visibility every 30s cycle)
        for pos in positions:
            sl_orig = self._get_original_sl_pips(pos)
            has_be = pos.ticket in self.breakeven_hit_tickets
            
            if not has_be:
                # Has not yet hit breakeven
                if self.volatility_status == "COOLING_DOWN":
                    be_trigger = config.COOLING_BREAKEVEN_TRIGGER_PIPS
                elif sl_orig > 0:
                    be_trigger = sl_orig * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
                else:
                    be_trigger = config.BREAKEVEN_TRIGGER_PIPS
                remaining = be_trigger - pos.profit_pips
                next_phase = f"BE at {be_trigger:.0f} pips ({remaining:.0f} remaining)"
            else:
                # Already has breakeven, next is trailing
                if self.volatility_status == "COOLING_DOWN":
                    tr_trigger = config.COOLING_TRAILING_TRIGGER_PIPS
                elif sl_orig > 0:
                    tr_trigger = sl_orig * getattr(config, 'TRAILING_ATR_MULT', 0.7)
                else:
                    tr_trigger = config.TRAILING_TRIGGER_PIPS
                if pos.profit_pips < tr_trigger:
                    remaining = tr_trigger - pos.profit_pips
                    next_phase = f"BE✓ | Trail at {tr_trigger:.0f} pips ({remaining:.0f} remaining)"
                else:
                    next_phase = f"BE✓ | Trail ACTIVE"
            
            log.info(
                f"   Monitor: #{pos.ticket} {pos.direction} | "
                f"P&L: {pos.profit_pips:+.0f} pips (${pos.profit:+.2f}) | "
                f"SL: {pos.sl:.2f} | {next_phase}"
            )
        
        return actions
    
    def _get_original_sl_pips(self, pos: PositionInfo) -> float:
        """Return original SL in pips (captured at open, does not change after breakeven)"""
        return self.original_sl_pips.get(pos.ticket, 0)
    
    def _check_breakeven(self, pos: PositionInfo) -> Optional[dict]:
        """Phase 1: Move SL to breakeven after profit reaches dynamic trigger"""
        # Already hit breakeven?
        if pos.ticket in self.breakeven_hit_tickets:
            return None
        
        # Aggressive trailing during cooling
        if self.volatility_status == "COOLING_DOWN":
            trigger = config.COOLING_BREAKEVEN_TRIGGER_PIPS
        else:
            # Dynamic: original SL × BREAKEVEN_ATR_MULT (fallback: fixed pips)
            sl_pips = self._get_original_sl_pips(pos)
            if sl_pips > 0:
                trigger = sl_pips * getattr(config, 'BREAKEVEN_ATR_MULT', 0.7)
            else:
                trigger = config.BREAKEVEN_TRIGGER_PIPS
        
        # Check if profit reached trigger
        if pos.profit_pips < trigger:
            return None
        
        # Breakeven reached! Move SL to entry (zero risk)
        breakeven_sl = calculate_breakeven_sl(pos.open_price, pos.direction)
        result = modify_sl(pos.ticket, breakeven_sl)
        
        if result.success:
            self.breakeven_hit_tickets.add(pos.ticket)
            self.trailing_sl[pos.ticket] = breakeven_sl
            
            # Confirm that trailing triggers did NOT change after breakeven
            sl_orig = self._get_original_sl_pips(pos)
            tr_trig = sl_orig * getattr(config, 'TRAILING_ATR_MULT', 0.7)
            tr_dist = sl_orig * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7)
            log.position_update(
                pos.ticket, "BREAKEVEN",
                f"Profit: {pos.profit_pips:.0f} pips → SL moved to {breakeven_sl:.2f} (entry) | "
                f"Original SL={sl_orig:.0f} pips (unchanged) | Trail trigger={tr_trig:.0f} pips, dist={tr_dist:.0f} pips"
            )
            alert_breakeven(pos.ticket, pos.sl, breakeven_sl, pos.profit_pips)
            
            return {
                'action': 'BREAKEVEN',
                'ticket': pos.ticket,
                'new_sl': breakeven_sl,
                'profit_pips': pos.profit_pips
            }
        
        return None
    
    def _check_trailing_stop(self, pos: PositionInfo) -> Optional[dict]:
        """Phase 2: Trailing stop after +TRAILING_TRIGGER_PIPS profit"""
        # Only apply trailing after breakeven
        if pos.ticket not in self.breakeven_hit_tickets:
            return None
        
        # Aggressive trailing during cooling
        if self.volatility_status == "COOLING_DOWN":
            trigger = config.COOLING_TRAILING_TRIGGER_PIPS
            distance = config.COOLING_TRAILING_DISTANCE_PIPS
        else:
            # Dynamic: original SL × ATR mults (fallback: fixed pips)
            sl_pips = self._get_original_sl_pips(pos)
            if sl_pips > 0:
                trigger = sl_pips * getattr(config, 'TRAILING_ATR_MULT', 0.7)
                distance = sl_pips * getattr(config, 'TRAILING_DISTANCE_ATR_MULT', 0.7)
            else:
                trigger = config.TRAILING_TRIGGER_PIPS
                distance = config.TRAILING_DISTANCE_PIPS
        
        # Only activate trailing after trigger
        if pos.profit_pips < trigger:
            return None
        
        current_sl = self.trailing_sl.get(pos.ticket, pos.sl)
        
        # Calculate new SL with trailing
        new_sl = calculate_trailing_stop(
            current_price=pos.current_price,
            direction=pos.direction,
            current_sl=current_sl,
            trailing_distance_pips=distance
        )
        
        if new_sl is None:
            return None
        
        # Move SL
        result = modify_sl(pos.ticket, new_sl)
        
        if result.success:
            old_sl = current_sl
            self.trailing_sl[pos.ticket] = new_sl
            
            log.position_update(pos.ticket, "TRAILING_STOP", f"SL: {old_sl:.2f} → {new_sl:.2f}")
            alert_trailing_stop(pos.ticket, old_sl, new_sl)
            
            return {
                'action': 'TRAILING_STOP',
                'ticket': pos.ticket,
                'old_sl': old_sl,
                'new_sl': new_sl
            }
        
        return None
    
    def _check_max_time(self, pos: PositionInfo) -> Optional[dict]:
        """Check maximum open position time"""
        time_open = datetime.now() - pos.open_time
        max_time = timedelta(hours=config.MAX_POSITION_HOURS)
        
        if time_open < max_time:
            return None
        
        # Check minimum profit
        if pos.profit_pips >= config.MAX_POSITION_MIN_PROFIT_PIPS:
            return None  # Has enough profit, keep
        
        # Close by timeout
        log.position_update(pos.ticket, "TIMEOUT_CLOSE", f"Open for {time_open}")
        
        result = close_position(pos.ticket)
        
        if result.success:
            # Calculate P&L
            account_info = executor.get_account_info()
            balance = account_info['balance'] if account_info else config.CAPITAL_INICIAL
            profit_percent = (pos.profit / balance) * 100
            
            alert_trade_closed(
                ticket=pos.ticket,
                direction=pos.direction,
                profit=pos.profit,
                profit_percent=profit_percent,
                reason=f"Timeout ({config.MAX_POSITION_HOURS}h)"
            )
            
            # Clean up tracking
            self._cleanup_position(pos.ticket)
            
            return {
                'action': 'TIMEOUT_CLOSE',
                'ticket': pos.ticket,
                'profit': pos.profit,
                'time_open': str(time_open)
            }
        
        return None
    
    def _check_max_drawdown(self, pos: PositionInfo) -> Optional[dict]:
        """Check excessive drawdown"""
        # Only check if loss exceeds limit
        if pos.profit_pips >= -config.MAX_POSITION_DRAWDOWN_PIPS:
            return None
        
        # Excessive drawdown - close
        log.position_update(
            pos.ticket, "DRAWDOWN_CLOSE",
            f"Loss: {pos.profit_pips:.1f} pips > {config.MAX_POSITION_DRAWDOWN_PIPS}"
        )
        
        result = close_position(pos.ticket)
        
        if result.success:
            account_info = executor.get_account_info()
            balance = account_info['balance'] if account_info else config.CAPITAL_INICIAL
            profit_percent = (pos.profit / balance) * 100
            
            alert_trade_closed(
                ticket=pos.ticket,
                direction=pos.direction,
                profit=pos.profit,
                profit_percent=profit_percent,
                reason="Excessive drawdown"
            )
            
            self._cleanup_position(pos.ticket)
            
            return {
                'action': 'DRAWDOWN_CLOSE',
                'ticket': pos.ticket,
                'profit': pos.profit,
                'profit_pips': pos.profit_pips
            }
        
        return None
    
    def _check_broker_closures(self, current_tickets: set) -> List[dict]:
        """Detect positions closed by broker (SL/TP hit)"""
        actions = []
        
        for ticket, pos in self.known_positions.items():
            if ticket in current_tickets:
                continue
            
            # Position disappeared — was closed by broker
            log.info(f"   Monitor: Position #{ticket} disappeared — checking history...")
            
            deal = get_deal_history(ticket, open_price=pos.open_price, tp_price=pos.tp, sl_price=pos.sl)
            
            if deal:
                # We have close details
                is_pending = deal.get('pending', False)
                profit = deal['profit'] if not is_pending else 0
                reason = deal['reason']
                direction = deal['direction']
                close_price = deal['close_price']
                outcome = deal.get('outcome')
                
                account_info = executor.get_account_info()
                balance = account_info['balance'] if account_info else config.CAPITAL_INICIAL
                profit_percent = (profit / balance) * 100 if not is_pending else 0
                
                if is_pending:
                    log.position_update(
                        ticket, "BROKER_CLOSE",
                        f"Closed by broker: {reason} | Price: {close_price:.2f} | "
                        f"P&L: PENDING ({outcome}) — awaiting MT5 deal confirmation"
                    )
                else:
                    log.position_update(
                        ticket, "BROKER_CLOSE",
                        f"Closed by broker: {reason} | Price: {close_price:.2f} | P&L: ${profit:+.2f}"
                    )
                
                alert_trade_closed(
                    ticket=ticket,
                    direction=direction,
                    profit=profit,
                    profit_percent=profit_percent,
                    reason=reason,
                    pending=is_pending,
                    outcome=outcome
                )
                
                # Determine close_type for dynamic cooldown
                had_trailing = ticket in self.trailing_sl
                if reason == "Take Profit":
                    close_type = "tp"
                elif had_trailing:
                    close_type = "trailing"
                elif reason == "Stop Loss":
                    close_type = "sl"
                else:
                    close_type = "sl"  # conservative default
                
                # Use original direction from position (not N3-derived) to prevent inversion
                orig_direction = pos.direction if is_pending else direction
                
                actions.append({
                    'action': 'BROKER_CLOSE',
                    'ticket': ticket,
                    'volume': deal.get('volume'),
                    'open_price': pos.open_price,
                    'close_price': close_price,
                    'profit': profit,
                    'reason': reason,
                    'close_time': deal.get('close_time').isoformat() if deal.get('close_time') else None,
                    'close_type': close_type,
                    'direction': orig_direction,
                    'estimated': deal.get('estimated', False),
                    'pending': is_pending,
                    'outcome': outcome,
                    'orig_tp': pos.tp,
                    'orig_sl': pos.sl,
                })
            else:
                # No details — notify anyway
                log.position_update(
                    ticket, "BROKER_CLOSE",
                    f"Position closed (no details in history)"
                )
                
                alert_trade_closed(
                    ticket=ticket,
                    direction=pos.direction,
                    profit=0,
                    profit_percent=0,
                    reason="Closed by broker (details unavailable)"
                )
                
                actions.append({
                    'action': 'BROKER_CLOSE',
                    'ticket': ticket,
                    'volume': pos.volume,
                    'open_price': pos.open_price,
                    'close_price': None,
                    'profit': 0,
                    'reason': 'unknown',
                    'close_time': None,
                    'close_type': 'sl',  # conservative default
                    'direction': pos.direction,
                })
            
            # Clean up tracking
            self._cleanup_position(ticket)
        
        return actions
    
    def _cleanup_position(self, ticket: int):
        """Clean up tracking for closed position"""
        self.breakeven_hit_tickets.discard(ticket)
        self.trailing_sl.pop(ticket, None)
        self.original_sl_pips.pop(ticket, None)
    
    def get_positions_summary(self) -> dict:
        """Return summary of open positions"""
        positions = get_positions()
        
        if not positions:
            return {
                'count': 0,
                'total_profit': 0,
                'positions': []
            }
        
        total_profit = sum(p.profit for p in positions)
        
        return {
            'count': len(positions),
            'total_profit': total_profit,
            'positions': [
                {
                    'ticket': p.ticket,
                    'direction': p.direction,
                    'volume': p.volume,
                    'profit': p.profit,
                    'profit_pips': p.profit_pips,
                    'open_time': p.open_time.isoformat()
                }
                for p in positions
            ]
        }
    
    def close_all_positions(self, reason: str = "Manual") -> List[dict]:
        """Close all open positions"""
        positions = get_positions()
        results = []
        
        for pos in positions:
            result = close_position(pos.ticket)
            
            if result.success:
                account_info = executor.get_account_info()
                balance = account_info['balance'] if account_info else config.CAPITAL_INICIAL
                profit_percent = (pos.profit / balance) * 100
                
                alert_trade_closed(
                    ticket=pos.ticket,
                    direction=pos.direction,
                    profit=pos.profit,
                    profit_percent=profit_percent,
                    reason=reason
                )
                
                self._cleanup_position(pos.ticket)
                
                results.append({
                    'ticket': pos.ticket,
                    'success': True,
                    'profit': pos.profit
                })
            else:
                results.append({
                    'ticket': pos.ticket,
                    'success': False,
                    'error': result.error_message
                })
        
        return results


# Global instance
monitor = PositionMonitor()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def monitor_positions() -> List[dict]:
    """Monitor all positions"""
    return monitor.monitor_all_positions()


def get_positions_summary() -> dict:
    """Return positions summary"""
    return monitor.get_positions_summary()


def close_all_positions(reason: str = "Manual") -> List[dict]:
    """Close all positions"""
    return monitor.close_all_positions(reason)


# ============================================================================
# TEST
# ============================================================================

def test_monitor():
    """Test the position monitor"""
    print("=" * 60)
    print("🧪 POSITION MONITOR TEST")
    print("=" * 60)
    
    # Force DRY RUN
    executor.dry_run = True
    
    print("\n📊 Test 1: Get positions summary")
    summary = get_positions_summary()
    print(f"   Open positions: {summary['count']}")
    print(f"   Total profit: ${summary['total_profit']:.2f}")
    
    print("\n📊 Test 2: Monitor positions")
    actions = monitor_positions()
    print(f"   Actions taken: {len(actions)}")
    for action in actions:
        print(f"   - {action}")
    
    print("\n✅ Tests complete!")


if __name__ == "__main__":
    test_monitor()
