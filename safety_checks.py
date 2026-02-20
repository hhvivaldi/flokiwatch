"""
SAFETY CHECKS - Safety Validations
Checks conditions before executing trades
"""

from datetime import datetime, timedelta
from typing import Tuple, List, Optional
import config


class SafetyChecker:
    """Safety checks manager"""
    
    def __init__(self):
        self.consecutive_losses = 0
        self.pause_until: Optional[datetime] = None
        self.daily_loss = 0.0
        self.daily_trades = 0
        self.last_reset_date = datetime.now().date()
        # Anti-overtrading: last trade per direction
        self.last_trade_time: dict = {'BUY': None, 'SELL': None}
        self.last_close_type: dict = {'BUY': None, 'SELL': None}  # "trailing", "sl", "tp", None
    
    def reset_daily_stats(self):
        """Reset daily statistics"""
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.daily_loss = 0.0
            self.daily_trades = 0
            self.last_reset_date = today
    
    def record_trade_result(self, profit: float):
        """Record trade result"""
        self.reset_daily_stats()
        self.daily_trades += 1
        
        if profit < 0:
            self.consecutive_losses += 1
            self.daily_loss += abs(profit)
        else:
            self.consecutive_losses = 0
    
    def check_all(
        self,
        account_balance: float,
        open_positions: int,
        mt5_connected: bool,
        has_high_impact_news: bool = False,
        trade_direction: str = None,
        open_positions_list: list = None
    ) -> Tuple[bool, List[str]]:
        """
        Execute all safety checks.
        
        Args:
            account_balance: Current account balance
            open_positions: Number of open positions
            mt5_connected: Whether MT5 is connected
            has_high_impact_news: Whether high-impact news is upcoming
            trade_direction: Proposed trade direction ("BUY" or "SELL")
            open_positions_list: List of PositionInfo (for smart pyramid check)
        
        Returns:
            Tuple: (is_safe, list_of_reasons_if_not_safe)
        """
        self.reset_daily_stats()
        
        reasons = []
        
        # 1. MT5 connected
        if not mt5_connected:
            reasons.append("MT5 is not connected")
        
        # 2. Market open + buffer before close
        is_open, market_reason, _ = self.is_market_open()
        if not is_open:
            reasons.append(market_reason)
        elif self.is_in_close_buffer():
            reasons.append(f"{getattr(config, 'MARKET_CLOSE_BUFFER_MINUTES', 60)} min buffer before close — no new positions")
        elif self.is_in_open_buffer():
            reasons.append(f"{getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)} min buffer after open — no new positions")
        
        # 4. Consecutive losses
        if self.consecutive_losses >= config.MAX_CONSECUTIVE_LOSSES:
            reasons.append(f"Reached {self.consecutive_losses} consecutive losses")
            # Activate pause
            if self.pause_until is None:
                self.pause_until = datetime.now() + timedelta(hours=config.PAUSE_AFTER_LOSSES_HOURS)
        
        # 5. Active pause
        if self.pause_until and datetime.now() < self.pause_until:
            reasons.append(f"Bot paused until {self.pause_until.strftime('%Y-%m-%d %H:%M')}")
        elif self.pause_until and datetime.now() >= self.pause_until:
            # Pause expired
            self.pause_until = None
            self.consecutive_losses = 0
        
        # 6. Maximum positions
        if open_positions >= config.MAX_POSITIONS:
            reasons.append(f"Maximum positions reached ({open_positions}/{config.MAX_POSITIONS})")
        
        # 7. Maximum daily loss
        daily_loss_percent = (self.daily_loss / account_balance) * 100 if account_balance > 0 else 0
        if daily_loss_percent >= config.MAX_DAILY_LOSS:
            reasons.append(f"Maximum daily loss reached ({daily_loss_percent:.1f}%)")
        
        # 8. High-impact news
        if has_high_impact_news:
            reasons.append("High-impact news in the next 2 hours")
        
        # 9. Anti-overtrading: check time since last trade in same direction
        if trade_direction:
            overtrading_ok, overtrading_reason = self.check_overtrading(trade_direction)
            if not overtrading_ok:
                reasons.append(overtrading_reason)
        
        # 10. Smart Pyramid: check if position already exists in same direction
        if trade_direction and open_positions_list:
            pyramid_ok, pyramid_reason = self.check_pyramid_allowed(trade_direction, open_positions_list)
            if not pyramid_ok:
                reasons.append(pyramid_reason)
        
        is_safe = len(reasons) == 0
        return is_safe, reasons
    
    def check_pyramid_allowed(self, direction: str, positions_list: list) -> Tuple[bool, str]:
        """
        Smart Pyramid: allows 2nd position in same direction ONLY if 1st is in profit >= PYRAMID_MIN_PROFIT_PERCENT.
        
        Args:
            direction: "BUY" or "SELL"
            positions_list: List of PositionInfo with open positions
        
        Returns:
            Tuple: (allowed, reason)
        """
        direction = direction.upper()
        min_profit_pct = getattr(config, 'PYRAMID_MIN_PROFIT_PERCENT', 0.3)
        
        # Find positions in same direction
        same_dir_positions = [p for p in positions_list if p.direction == direction]
        
        if not same_dir_positions:
            return True, ""  # No position in same direction → allow
        
        # Check if ALL positions in same direction have sufficient profit
        for pos in same_dir_positions:
            if pos.open_price <= 0:
                continue
            
            # Calculate profit % based on direction
            if direction == "BUY":
                profit_pct = ((pos.current_price - pos.open_price) / pos.open_price) * 100
            else:  # SELL
                profit_pct = ((pos.open_price - pos.current_price) / pos.open_price) * 100
            
            if profit_pct < min_profit_pct:
                return False, (
                    f"Smart Pyramid: {direction} #{pos.ticket} open with insufficient profit "
                    f"({profit_pct:+.2f}% < {min_profit_pct}%) for reinforcement"
                )
        
        # All positions in same direction have sufficient profit → allow pyramid
        return True, ""
    
    def check_overtrading(self, direction: str) -> Tuple[bool, str]:
        """
        Check if enough time has passed since last trade in same direction.
        Dynamic cooldown: trailing close → 30 min, SL close → 45 min, default → 45 min.
        """
        direction = direction.upper()
        if direction not in self.last_trade_time:
            return True, ""
        
        last_time = self.last_trade_time.get(direction)
        if last_time is None:
            return True, ""
        
        # Dynamic cooldown based on close type
        close_type = self.last_close_type.get(direction)
        if close_type == "trailing":
            min_minutes = getattr(config, 'MIN_MINUTES_AFTER_TRAILING', 30)
        elif close_type == "sl":
            min_minutes = getattr(config, 'MIN_MINUTES_AFTER_SL', 45)
        else:
            min_minutes = getattr(config, 'MIN_MINUTES_BETWEEN_TRADES', 45)
        
        elapsed = (datetime.now() - last_time).total_seconds() / 60
        
        if elapsed < min_minutes:
            remaining = int(min_minutes - elapsed)
            return False, f"Wait {remaining}min before new {direction} (anti-overtrading)"
        
        return True, ""
    
    def record_trade_opened(self, direction: str):
        """Record that a trade was opened (for anti-overtrading)"""
        direction = direction.upper()
        self.last_trade_time[direction] = datetime.now()
    
    def record_close_type(self, direction: str, close_type: str):
        """
        Record the close type of the last trade (for dynamic cooldown).
        
        Args:
            direction: "BUY" or "SELL"
            close_type: "trailing", "sl", "tp", or None
        """
        direction = direction.upper()
        self.last_close_type[direction] = close_type
        # Update last_trade_time to the close moment (cooldown counts from here)
        self.last_trade_time[direction] = datetime.now()
    
    def is_market_open(self, now_utc: Optional[datetime] = None) -> Tuple[bool, str, Optional[datetime]]:
        """
        Check if the gold market is open.
        
        XAU/USD operates: Sunday 22:00 UTC → Friday 21:00 UTC
        Daily pause: 21:00-22:00 UTC (Mon-Thu)
        
        Args:
            now_utc: Datetime UTC (default: datetime.utcnow())
        
        Returns:
            Tuple: (is_open, reason_if_closed, next_open_utc)
        """
        if now_utc is None:
            now_utc = datetime.utcnow()
        
        weekday = now_utc.weekday()  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
        hour = now_utc.hour
        minute = now_utc.minute
        
        close_hour = getattr(config, 'MARKET_DAILY_CLOSE_HOUR', 21)
        open_hour = getattr(config, 'MARKET_DAILY_OPEN_HOUR', 22)
        
        # Weekend: Friday 21:00 → Sunday 22:00
        # Friday after 21:00
        if weekday == 4 and hour >= close_hour:
            # Next open: Sunday 22:00
            days_until_sunday = 2
            next_open = now_utc.replace(hour=open_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
            return False, "Weekend — market closed (Friday after 21:00 UTC)", next_open
        
        # Saturday
        if weekday == 5:
            days_until_sunday = 1
            next_open = now_utc.replace(hour=open_hour, minute=0, second=0, microsecond=0) + timedelta(days=days_until_sunday)
            return False, "Weekend — market closed (Saturday)", next_open
        
        # Sunday before 22:00
        if weekday == 6 and hour < open_hour:
            next_open = now_utc.replace(hour=open_hour, minute=0, second=0, microsecond=0)
            return False, "Weekend — market closed (Sunday before 22:00 UTC)", next_open
        
        # Daily pause: 21:00-22:00 UTC (Mon-Thu)
        # Sunday after 22:00 has no pause (just opened)
        if weekday in [0, 1, 2, 3] and hour >= close_hour and hour < open_hour:
            next_open = now_utc.replace(hour=open_hour, minute=0, second=0, microsecond=0)
            return False, f"Daily pause — market closed {close_hour}:00-{open_hour}:00 UTC", next_open
        
        return True, "", None
    
    def is_in_close_buffer(self, now_utc: Optional[datetime] = None) -> bool:
        """
        Check if we are within the buffer before close (no new positions).
        The monitor continues managing existing positions.
        
        Returns:
            True if we are in the last N minutes before daily/weekly close.
        """
        if now_utc is None:
            now_utc = datetime.utcnow()
        
        buffer_min = getattr(config, 'MARKET_CLOSE_BUFFER_MINUTES', 5)
        close_hour = getattr(config, 'MARKET_DAILY_CLOSE_HOUR', 21)
        weekday = now_utc.weekday()
        hour = now_utc.hour
        minute = now_utc.minute
        
        # Buffer before daily close (21:00 UTC) — applies Mon-Fri
        if weekday in [0, 1, 2, 3, 4]:
            close_time_minutes = close_hour * 60
            current_minutes = hour * 60 + minute
            if 0 <= (close_time_minutes - current_minutes) <= buffer_min:
                return True
        
        return False
    
    def is_in_open_buffer(self, now_utc: Optional[datetime] = None) -> bool:
        """
        Check if we are within the buffer after open (no new positions).
        Avoids session open gaps (Asian/London).
        
        Returns:
            True if we are in the first N minutes after daily open.
        """
        if now_utc is None:
            now_utc = datetime.utcnow()
        
        buffer_min = getattr(config, 'MARKET_OPEN_BUFFER_MINUTES', 60)
        open_hour = getattr(config, 'MARKET_DAILY_OPEN_HOUR', 22)
        weekday = now_utc.weekday()
        hour = now_utc.hour
        minute = now_utc.minute
        
        # Buffer after daily open (22:00 UTC) — applies Mon-Fri
        # Sunday 22:00 = weekday 6, hour 22+
        if weekday in [0, 1, 2, 3, 6]:
            open_time_minutes = open_hour * 60
            current_minutes = hour * 60 + minute
            elapsed = current_minutes - open_time_minutes
            if 0 <= elapsed <= buffer_min:
                return True
        
        return False
    
    def check_trading_hours(self) -> Tuple[bool, str]:
        """Backward compat wrapper — uses is_market_open()"""
        is_open, reason, _ = self.is_market_open()
        return is_open, reason
    
    def check_friday_close(self) -> Tuple[bool, str]:
        """Backward compat wrapper — uses is_market_open()"""
        is_open, reason, _ = self.is_market_open()
        return is_open, reason
    
    def check_spread(self, current_spread: float, max_spread: float = 5.0) -> Tuple[bool, str]:
        """Check if spread is acceptable"""
        if current_spread > max_spread:
            return False, f"Spread too high ({current_spread:.1f} > {max_spread})"
        return True, ""
    
    def get_pause_info(self) -> Optional[dict]:
        """Return info about active pause"""
        if self.pause_until and datetime.now() < self.pause_until:
            return {
                'paused': True,
                'until': self.pause_until,
                'reason': f'{self.consecutive_losses} consecutive losses',
                'remaining': self.pause_until - datetime.now()
            }
        return None
    
    def force_pause(self, hours: int, reason: str):
        """Force manual pause"""
        self.pause_until = datetime.now() + timedelta(hours=hours)
        return {
            'paused': True,
            'until': self.pause_until,
            'reason': reason
        }
    
    def clear_pause(self):
        """Clear pause"""
        self.pause_until = None
        self.consecutive_losses = 0


# Global instance
safety = SafetyChecker()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def is_safe_to_trade(
    account_balance: float,
    open_positions: int,
    mt5_connected: bool,
    has_high_impact_news: bool = False,
    trade_direction: str = None,
    open_positions_list: list = None
) -> Tuple[bool, List[str]]:
    """Wrapper for complete check"""
    return safety.check_all(
        account_balance=account_balance,
        open_positions=open_positions,
        mt5_connected=mt5_connected,
        has_high_impact_news=has_high_impact_news,
        trade_direction=trade_direction,
        open_positions_list=open_positions_list
    )


def record_trade_opened(direction: str):
    """Record that a trade was opened (for anti-overtrading)"""
    safety.record_trade_opened(direction)


def record_close_type(direction: str, close_type: str):
    """Record close type of last trade (for dynamic cooldown)"""
    safety.record_close_type(direction, close_type)


def record_trade_result(profit: float):
    """Record trade result"""
    safety.record_trade_result(profit)


def is_market_open(now_utc: Optional[datetime] = None) -> Tuple[bool, str, Optional[datetime]]:
    """Wrapper for market open check"""
    return safety.is_market_open(now_utc)


def is_in_close_buffer(now_utc: Optional[datetime] = None) -> bool:
    """Wrapper for close buffer check"""
    return safety.is_in_close_buffer(now_utc)


def get_safety_status() -> dict:
    """Return current safety status"""
    safety.reset_daily_stats()
    
    market_open, market_reason, next_open = safety.is_market_open()
    
    return {
        'consecutive_losses': safety.consecutive_losses,
        'daily_loss': safety.daily_loss,
        'daily_trades': safety.daily_trades,
        'pause_info': safety.get_pause_info(),
        'is_market_open': market_open,
        'market_reason': market_reason,
        'next_open': next_open,
        'is_trading_hours': market_open,
        'is_friday_ok': market_open
    }


# ============================================================================
# TEST
# ============================================================================

def test_safety_checks():
    """Test safety checks"""
    print("=" * 60)
    print("🧪 SAFETY CHECKS TEST")
    print("=" * 60)
    
    # Reset
    safety.consecutive_losses = 0
    safety.pause_until = None
    safety.daily_loss = 0
    
    # Test 1: All OK
    print("\n📊 Test 1: Normal conditions")
    is_safe, reasons = safety.check_all(
        account_balance=1000,
        open_positions=1,
        mt5_connected=True,
        has_high_impact_news=False
    )
    print(f"   Safe: {is_safe}")
    if reasons:
        print(f"   Reasons: {reasons}")
    
    # Test 2: MT5 disconnected
    print("\n📊 Test 2: MT5 disconnected")
    is_safe, reasons = safety.check_all(
        account_balance=1000,
        open_positions=1,
        mt5_connected=False,
        has_high_impact_news=False
    )
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    
    # Test 3: Too many positions
    print("\n📊 Test 3: Maximum positions")
    is_safe, reasons = safety.check_all(
        account_balance=1000,
        open_positions=3,
        mt5_connected=True,
        has_high_impact_news=False
    )
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    
    # Test 4: Consecutive losses
    print("\n📊 Test 4: After 3 consecutive losses")
    safety.record_trade_result(-20)
    safety.record_trade_result(-20)
    safety.record_trade_result(-20)
    
    is_safe, reasons = safety.check_all(
        account_balance=1000,
        open_positions=0,
        mt5_connected=True,
        has_high_impact_news=False
    )
    print(f"   Consecutive losses: {safety.consecutive_losses}")
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    
    pause_info = safety.get_pause_info()
    if pause_info:
        print(f"   Pause until: {pause_info['until']}")
    
    # Test 5: High impact news
    print("\n📊 Test 5: High-impact news")
    safety.clear_pause()
    is_safe, reasons = safety.check_all(
        account_balance=1000,
        open_positions=0,
        mt5_connected=True,
        has_high_impact_news=True
    )
    print(f"   Safe: {is_safe}")
    print(f"   Reasons: {reasons}")
    
    # General status
    print("\n📊 General Status:")
    status = get_safety_status()
    for key, value in status.items():
        print(f"   {key}: {value}")


if __name__ == "__main__":
    test_safety_checks()
