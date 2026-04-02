"""
SAFETY CHECKS - Safety Validations
Checks conditions before executing trades
"""

from datetime import datetime, timedelta
from typing import Tuple, List, Optional
import json
import logging
import os
import config

log = logging.getLogger(__name__)

SAFETY_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "safety_state.json")


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
        self._load_state()
    
    # -----------------------------------------------------------------
    # Persistence (FLO-93)
    # -----------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist safety state to disk (atomic write)."""
        try:
            os.makedirs(os.path.dirname(SAFETY_STATE_FILE), exist_ok=True)
            payload = {
                "pause_until": self.pause_until.isoformat() if self.pause_until else None,
                "daily_loss": self.daily_loss,
                "daily_trades": self.daily_trades,
                "consecutive_losses": self.consecutive_losses,
                "last_reset_date": str(self.last_reset_date),
                "last_trade_time": {
                    k: v.isoformat() if v else None
                    for k, v in self.last_trade_time.items()
                },
                "last_close_type": dict(self.last_close_type),
                "saved_at": datetime.now().isoformat(),
            }
            tmp_path = SAFETY_STATE_FILE + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, SAFETY_STATE_FILE)
        except Exception as e:
            log.warning(f"SAFETY | Failed to save state: {e}")

    def _load_state(self) -> None:
        """Restore safety state from disk on startup."""
        try:
            if not os.path.exists(SAFETY_STATE_FILE):
                return
            with open(SAFETY_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                log.warning("SAFETY | State file corrupt (not a dict) — fresh start")
                return

            # --- pause_until: survives restart AND daily reset ---
            pu = data.get("pause_until")
            if pu:
                try:
                    pause_dt = datetime.fromisoformat(pu)
                    if pause_dt > datetime.now():
                        self.pause_until = pause_dt
                        log.info(f"SAFETY | Restored pause_until={pause_dt.strftime('%Y-%m-%d %H:%M')}")
                    else:
                        log.info(f"SAFETY | Stored pause expired ({pu}) — cleared")
                except (ValueError, TypeError):
                    pass

            # --- daily counters: only restore if same day ---
            saved_date_str = data.get("last_reset_date")
            today = datetime.now().date()
            same_day = False
            if saved_date_str:
                try:
                    saved_date = datetime.strptime(saved_date_str, "%Y-%m-%d").date()
                    same_day = saved_date == today
                except (ValueError, TypeError):
                    pass

            if same_day:
                self.daily_loss = float(data.get("daily_loss", 0.0))
                self.daily_trades = int(data.get("daily_trades", 0))
                self.consecutive_losses = int(data.get("consecutive_losses", 0))
                self.last_reset_date = today

                # Restore anti-overtrading timestamps
                for direction in ("BUY", "SELL"):
                    ltt = (data.get("last_trade_time") or {}).get(direction)
                    if ltt:
                        try:
                            self.last_trade_time[direction] = datetime.fromisoformat(ltt)
                        except (ValueError, TypeError):
                            pass
                    lct = (data.get("last_close_type") or {}).get(direction)
                    if lct:
                        self.last_close_type[direction] = lct

                log.info(
                    f"SAFETY | Restored state: losses={self.consecutive_losses}, "
                    f"daily_loss=${self.daily_loss:.2f}, trades={self.daily_trades}"
                )
            else:
                # New day — only consecutive_losses resets, pause_until already handled above
                log.info(f"SAFETY | New day ({today} vs saved {saved_date_str}) — daily counters reset")

        except json.JSONDecodeError:
            log.warning("SAFETY | State file corrupt (bad JSON) — fresh start")
        except Exception as e:
            log.warning(f"SAFETY | Failed to load state: {e} — fresh start")

    def reset_daily_stats(self):
        """Reset daily statistics"""
        today = datetime.now().date()
        if today != self.last_reset_date:
            self.daily_loss = 0.0
            self.daily_trades = 0
            self.last_reset_date = today
            self._save_state()
    
    def record_trade_result(self, profit: float):
        """Record trade result"""
        self.reset_daily_stats()
        self.daily_trades += 1

        if profit < 0:
            self.consecutive_losses += 1
            self.daily_loss += abs(profit)
        else:
            self.consecutive_losses = 0
        self._save_state()
    
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
        
        # FLO-118: Removed checks 4-10 (consecutive losses pause, active pause,
        # max positions, max daily loss, high-impact news, anti-overtrading,
        # smart pyramid). Floki manages his own risk. Sage advises via session
        # memory — Floki reads and decides for himself.

        # FLO-85: Hard gate — no opposing positions allowed
        # Use `is not None` instead of truthiness — empty list [] means "no positions" (safe),
        # but None means "fetch failed" (must block)
        if trade_direction and open_positions_list is not None:
            opposing_ok, opposing_reason = self.check_no_opposing_position(trade_direction, open_positions_list)
            if not opposing_ok:
                reasons.append(opposing_reason)
        elif trade_direction and open_positions_list is None:
            reasons.append("opposing_check_skipped: position fetch failed")

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
    
    def check_no_opposing_position(self, direction: str, positions_list: list) -> Tuple[bool, str]:
        """
        Hard gate: block trade if an open position exists in the OPPOSITE direction.
        FlokiWatch is a directional system — no opposing positions allowed (FLO-85).
        """
        direction = direction.upper()
        opposite = "SELL" if direction == "BUY" else "BUY"

        for pos in positions_list:
            if pos.direction == opposite:
                return False, (
                    f"Opposing position already open (ticket #{pos.ticket} is {opposite}) — "
                    f"cannot open {direction} while {opposite} is active"
                )

        return True, ""

    def check_overtrading(self, direction: str) -> Tuple[bool, str]:
        """FLO-200: Anti-overtrading cooldown REMOVED — Floki has full autonomy.
        Was: 30-45 min cooldown after trade close. Now: always passes."""
        return True, ""
    
    def record_trade_opened(self, direction: str):
        """Record that a trade was opened (for anti-overtrading)"""
        direction = direction.upper()
        self.last_trade_time[direction] = datetime.now()
        self._save_state()
    
    def record_close_type(self, direction: str, close_type: str, pnl: Optional[float] = None):
        """
        Record the close type of the last trade (for dynamic cooldown).

        Args:
            direction: "BUY" or "SELL"
            close_type: "trailing", "sl", "tp", "breakeven", or None
            pnl: P&L of the closed trade (used to detect breakeven closes)
        """
        direction = direction.upper()
        # FLO-116: If P&L is near zero (-$2 to +$2), classify as breakeven
        # regardless of close_type — EA-killed trades shouldn't trigger long cooldowns
        if pnl is not None and -2.0 <= float(pnl) <= 2.0:
            close_type = "breakeven"
        self.last_close_type[direction] = close_type
        # Update last_trade_time to the close moment (cooldown counts from here)
        self.last_trade_time[direction] = datetime.now()
        self._save_state()
    
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
        self._save_state()
        return {
            'paused': True,
            'until': self.pause_until,
            'reason': reason
        }
    
    def clear_pause(self):
        """Clear pause"""
        self.pause_until = None
        self.consecutive_losses = 0
        self._save_state()


# Global instance
safety = SafetyChecker()


# ============================================================================
# CONVENIENCE FUNCTIONS
# ============================================================================

def is_bot_paused() -> bool:
    """FLO-92: Check if bot is paused (consecutive losses or daily loss limit).
    Used to kill all agent cycles when paused — zero API spend."""
    safety.reset_daily_stats()
    if safety.pause_until and datetime.now() < safety.pause_until:
        return True
    if safety.daily_loss > 0:
        try:
            from mt5_interface import get_account_info
            info = get_account_info()
            balance = info.get("balance", 0) if isinstance(info, dict) else 0
        except Exception:
            balance = 0
        if balance > 0:
            daily_loss_pct = (safety.daily_loss / balance) * 100
            if daily_loss_pct >= config.MAX_DAILY_LOSS:
                return True
    return False


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


def record_close_type(direction: str, close_type: str, pnl: float = None):
    """Record close type of last trade (for dynamic cooldown)"""
    safety.record_close_type(direction, close_type, pnl=pnl)


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
