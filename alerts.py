"""
ALERTS - Discord Alert System
Sends notifications to Discord via Webhook
"""

import requests
import json
from datetime import datetime
from typing import Optional
import config
from logger import log


class DiscordAlert:
    """Discord alert manager"""
    
    def __init__(self, webhook_url: str = None):
        self.webhook_url = webhook_url or config.DISCORD_WEBHOOK_URL
        self.bot_name = config.DISCORD_BOT_NAME
        self.enabled = bool(self.webhook_url)
    
    def send(
        self,
        message: str,
        alert_type: str = "info",
        title: Optional[str] = None
    ) -> bool:
        """
        Send alert to Discord.
        
        Args:
            message: Message text
            alert_type: info, success, warning, error
            title: Optional title
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            log.debug(f"[DISCORD DISABLED] {message}")
            return False
        
        # Emojis by type
        emojis = {
            'info': 'ℹ️',
            'success': '✅',
            'warning': '⚠️',
            'error': '❌',
            'trade': '📈',
            'profit': '💰',
            'loss': '🔴',
            'alert': '🔔'
        }
        
        emoji = emojis.get(alert_type, 'ℹ️')
        
        # Format message
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        if title:
            full_message = f"{emoji} **{title}**\n{message}\n`{timestamp}`"
        else:
            full_message = f"{emoji} {message}\n`{timestamp}`"
        
        # Payload
        payload = {
            "content": full_message,
            "username": self.bot_name
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            
            if response.status_code in [200, 204]:
                return True
            else:
                log.warning(f"[DISCORD ERROR] Status: {response.status_code}")
                return False
                
        except Exception as e:
            log.warning(f"[DISCORD ERROR] {e}")
            return False
    
    def send_embed(
        self,
        title: str,
        description: str,
        color: int = 0x00ff00,
        fields: list = None
    ) -> bool:
        """
        Send formatted embed message.
        
        Args:
            title: Embed title
            description: Description
            color: Color in hex (0x00ff00 = green)
            fields: List of dicts with name, value, inline
        
        Returns:
            True if sent successfully
        """
        if not self.enabled:
            log.debug(f"[DISCORD DISABLED] {title}: {description}")
            return False
        
        embed = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
            "footer": {"text": self.bot_name}
        }
        
        if fields:
            embed["fields"] = fields
        
        payload = {
            "username": self.bot_name,
            "embeds": [embed]
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10
            )
            if response.status_code in [200, 204]:
                return True
            else:
                log.warning(f"[DISCORD ERROR] Embed status: {response.status_code}")
                return False
        except Exception as e:
            log.warning(f"[DISCORD ERROR] {e}")
            return False


# Global instance
discord = DiscordAlert()


# ============================================================================
# SPECIFIC ALERT FUNCTIONS
# ============================================================================

def alert_bot_started(mode: str = "LIVE"):
    """Alert: Bot started"""
    discord.send_embed(
        title="🤖 Bot Started",
        description=f"XAU/USD Trading Bot is online and running.",
        color=0x00ff00,  # Green
        fields=[
            {"name": "Mode", "value": mode, "inline": True},
            {"name": "Symbol", "value": config.SYMBOL, "inline": True},
            {"name": "Timeframe", "value": config.TIMEFRAME, "inline": True}
        ]
    )


def alert_bot_stopped(reason: str = "Manual"):
    """Alert: Bot stopped"""
    discord.send_embed(
        title="🛑 Bot Stopped",
        description=f"Trading Bot has been shut down.",
        color=0xff0000,  # Red
        fields=[
            {"name": "Reason", "value": reason, "inline": False}
        ]
    )


def alert_signal_detected(
    decision: str,
    final_score: float,
    tech_score: float,
    news_score: float,
    ml_score: float,
    confidence: str,
    brain_summary: str = ""
):
    """Alert: Signal detected"""
    # Color based on decision
    if "BUY" in decision:
        color = 0x00ff00  # Green
        emoji = "🟢"
    elif "SELL" in decision:
        color = 0xff0000  # Red
        emoji = "🔴"
    else:
        color = 0xffff00  # Yellow
        emoji = "🟡"
    
    fields = [
        {"name": "📊 Technical", "value": f"{tech_score:.1f}", "inline": True},
        {"name": "📰 News", "value": f"{news_score:.1f}", "inline": True},
        {"name": "🤖 ML", "value": f"{ml_score:.1f}", "inline": True}
    ]
    
    # Add brain summary if available
    if brain_summary:
        fields.append({"name": "🧠 Central Brain", "value": brain_summary, "inline": False})
    
    discord.send_embed(
        title=f"{emoji} Signal: {decision}",
        description=f"Final Score: **{final_score:.1f}/100** | Confidence: **{confidence}**",
        color=color,
        fields=fields
    )


def alert_trade_executed(
    direction: str,
    ticket: int,
    lot_size: float,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    is_dry_run: bool = False
):
    """Alert: Trade executed"""
    prefix = "🧪 [TEST] " if is_dry_run else ""
    color = 0x00ff00 if direction == "BUY" else 0xff0000
    
    # Calculate pips
    pip_size = 0.1
    sl_pips = abs(entry_price - stop_loss) / pip_size
    tp_pips = abs(take_profit - entry_price) / pip_size
    
    discord.send_embed(
        title=f"{prefix}✅ {direction} Order Executed",
        description=f"Trade opened automatically by the bot.",
        color=color,
        fields=[
            {"name": "Ticket", "value": str(ticket), "inline": True},
            {"name": "Lot", "value": str(lot_size), "inline": True},
            {"name": "Entry", "value": f"{entry_price:.2f}", "inline": True},
            {"name": "Stop Loss", "value": f"{stop_loss:.2f} (-{sl_pips:.0f} pips)", "inline": True},
            {"name": "Take Profit", "value": f"{take_profit:.2f} (+{tp_pips:.0f} pips)", "inline": True}
        ]
    )


def alert_trade_closed(
    ticket: int,
    direction: str,
    profit: float,
    profit_percent: float,
    reason: str,
    pending: bool = False,
    outcome: str = None
):
    """Alert: Trade closed"""
    if pending and outcome:
        # P&L not yet confirmed — show outcome only
        if outcome == "WIN":
            emoji = "💰"
            color = 0x00ff00
            status = "WIN"
        elif outcome == "LOSS":
            emoji = "🔴"
            color = 0xff0000
            status = "LOSS"
        else:
            emoji = "⚪"
            color = 0x95a5a6
            status = "BE"
        pnl_value = f"{status} — Awaiting confirmation"
    else:
        if profit >= 0:
            emoji = "💰"
            color = 0x00ff00
            status = "PROFIT"
        else:
            emoji = "🔴"
            color = 0xff0000
            status = "LOSS"
        pnl_value = f"${profit:+.2f} ({profit_percent:+.1f}%)"
    
    discord.send_embed(
        title=f"{emoji} Trade Closed - {status}",
        description=f"Ticket #{ticket} has been closed.",
        color=color,
        fields=[
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "P&L", "value": pnl_value, "inline": True},
            {"name": "Reason", "value": reason, "inline": True}
        ]
    )


def alert_trade_resolved(ticket: int, direction: str, profit: float, profit_percent: float, reason: str):
    """Alert: Pending trade resolved with real P&L from MT5"""
    if profit >= 0:
        emoji = "✅"
        color = 0x00ff00
    else:
        emoji = "✅"
        color = 0xff0000
    
    discord.send_embed(
        title=f"{emoji} Trade Resolved",
        description=f"Ticket #{ticket} — real P&L confirmed by MT5.",
        color=color,
        fields=[
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "P&L", "value": f"${profit:+.2f} ({profit_percent:+.1f}%)", "inline": True},
            {"name": "Reason", "value": reason, "inline": True}
        ]
    )


def alert_breakeven(
    ticket: int,
    old_sl: float,
    new_sl: float,
    profit_pips: float
):
    """Alert: Breakeven activated"""
    discord.send_embed(
        title="🛡️ Breakeven Activated",
        description=f"SL moved to entry — zero risk.",
        color=0x00bfff,
        fields=[
            {"name": "Ticket", "value": str(ticket), "inline": True},
            {"name": "Current Profit", "value": f"+{profit_pips:.0f} pips", "inline": True},
            {"name": "SL", "value": f"{old_sl:.2f} → {new_sl:.2f}", "inline": True}
        ]
    )


def alert_sl_hit(ticket: int, loss: float, loss_percent: float):
    """Alert: Stop Loss hit"""
    discord.send_embed(
        title="🔴 Stop Loss Hit",
        description=f"Position closed automatically.",
        color=0xff0000,
        fields=[
            {"name": "Ticket", "value": str(ticket), "inline": True},
            {"name": "Loss", "value": f"${loss:.2f} ({loss_percent:.1f}%)", "inline": True}
        ]
    )


def alert_safety_block(decision: str, score: float, reason: str):
    """Alert: Trade blocked by safety check"""
    discord.send_embed(
        title="⛔ Signal Blocked",
        description=f"Trade {decision} (Score: {score:.1f}) was blocked.",
        color=0xffff00,
        fields=[
            {"name": "Reason", "value": reason, "inline": False}
        ]
    )


def alert_m5_reversal_block(direction: str, move_pct: float, description: str):
    """Alert: Trade blocked by M5 reversal detection"""
    discord.send_embed(
        title="🔄 M5 Reversal — Entry Blocked",
        description=f"{direction} signal blocked: M5 price contradicts direction.",
        color=0xff6600,
        fields=[
            {"name": "M5 Move (30 min)", "value": f"{move_pct:+.2f}%", "inline": True},
            {"name": "Detail", "value": description, "inline": False},
        ]
    )


def alert_error(error_type: str, message: str):
    """Alert: Critical error"""
    discord.send_embed(
        title=f"⚠️ ERROR: {error_type}",
        description=message,
        color=0xff0000
    )


def alert_daily_summary(
    trades_total: int,
    wins: int,
    losses: int,
    pnl: float,
    pnl_percent: float,
    current_balance: float,
    gpt_stats: dict = None
):
    """Alert: Daily summary"""
    win_rate = (wins / trades_total * 100) if trades_total > 0 else 0
    
    if pnl >= 0:
        color = 0x00ff00
        emoji = "📈"
    else:
        color = 0xff0000
        emoji = "📉"
    
    fields = [
        {"name": "Trades", "value": str(trades_total), "inline": True},
        {"name": "Wins", "value": str(wins), "inline": True},
        {"name": "Losses", "value": str(losses), "inline": True},
        {"name": "Win Rate", "value": f"{win_rate:.1f}%", "inline": True},
        {"name": "P&L", "value": f"${pnl:+.2f} ({pnl_percent:+.1f}%)", "inline": True},
        {"name": "Balance", "value": f"${current_balance:.2f}", "inline": True},
    ]
    
    if gpt_stats:
        total_gpt = gpt_stats.get("confirm", 0) + gpt_stats.get("boost", 0) + gpt_stats.get("reduce", 0)
        gpt_value = (
            f"CONFIRM: {gpt_stats.get('confirm', 0)} | "
            f"BOOST: {gpt_stats.get('boost', 0)} | "
            f"REDUCE: {gpt_stats.get('reduce', 0)}\n"
            f"Total: {total_gpt} analyses (cache: {gpt_stats.get('from_cache', 0)})"
        )
        fields.append({"name": "🤖 GPT Validator", "value": gpt_value, "inline": False})
    
    discord.send_embed(
        title=f"{emoji} Daily Summary",
        description=f"Trading bot performance over the last 24h.",
        color=color,
        fields=fields
    )


def alert_pause_trading(reason: str, resume_time: str):
    """Alert: Trading paused"""
    discord.send_embed(
        title="🛑 Trading Paused",
        description=f"Bot paused operations automatically.",
        color=0xffff00,
        fields=[
            {"name": "Reason", "value": reason, "inline": False},
            {"name": "Resumes at", "value": resume_time, "inline": False}
        ]
    )


def alert_trailing_stop(ticket: int, old_sl: float, new_sl: float):
    """Alert: Trailing stop activated"""
    discord.send(
        f"📊 **Trailing Stop Activated**\nTicket: {ticket}\nSL: {old_sl:.2f} → {new_sl:.2f}",
        alert_type="info"
    )


def alert_heartbeat_full(
    current_price: float,
    final_score: float,
    confidence: float,
    scenario: str,
    dominant_pillar: str,
    volatility_status: str,
    calendar_info: str = "",
    gpt_info: str = ""
):
    """Alert: Full heartbeat (scenario changed or score shifted significantly)"""
    fields = [
        {"name": "💰 Current Price", "value": f"{current_price:.2f}", "inline": True},
        {"name": "📊 Score", "value": f"{final_score:.1f}/100", "inline": True},
        {"name": "🎯 Confidence", "value": f"{confidence:.1f}%", "inline": True},
        {"name": "🧠 Scenario", "value": scenario, "inline": False},
        {"name": "📌 Dominant Pillar", "value": dominant_pillar, "inline": True},
        {"name": "⚡ Volatility", "value": volatility_status, "inline": True},
    ]
    
    if calendar_info:
        fields.append({"name": "📅 Calendar", "value": calendar_info, "inline": False})
    
    if gpt_info:
        fields.append({"name": "🤖 GPT Validator", "value": gpt_info, "inline": False})
    
    discord.send_embed(
        title="💤 Heartbeat — HOLD",
        description="Bot active and analyzing. No conditions for trade.",
        color=0x7289DA,
        fields=fields
    )


def alert_market_closed(reason: str, next_open: str):
    """Alert: Market closed"""
    discord.send_embed(
        title="🌙 Market Closed",
        description=reason,
        color=0x95a5a6,  # Gray
        fields=[
            {"name": "Next open", "value": next_open, "inline": False}
        ]
    )


def alert_market_open():
    """Alert: Market opened"""
    discord.send_embed(
        title="☀️ Market Open",
        description="Gold market has reopened. Bot active and analyzing.",
        color=0x2ecc71  # Green
    )


def alert_heartbeat_short():
    """Alert: Short heartbeat (no significant changes)"""
    timestamp = datetime.now().strftime("%H:%M")
    discord.send(
        f"🔄 Analysis at {timestamp} — HOLD maintained, no significant changes.",
        alert_type="info"
    )


def alert_spread_delay(spread: float, max_spread: float, retry_count: int):
    """Alert: Trade delayed due to high spread (first occurrence only)"""
    discord.send_embed(
        title="⏳ Entry Delayed — High Spread",
        description=f"Spread too high: **{spread:.1f} pips** (max: {max_spread:.1f})\nRetrying every 30s for up to 5 minutes...",
        color=0xf39c12,  # Orange
        fields=[
            {"name": "Retry", "value": f"#{retry_count}", "inline": True},
            {"name": "Max Retries", "value": "10", "inline": True}
        ]
    )


def alert_spread_skip(direction: str, spread: float, final_score: float):
    """Alert: Trade skipped after spread timeout"""
    discord.send_embed(
        title="⛔ Trade Skipped — Spread Timeout",
        description=f"Spread did not normalize after 5 minutes.\n**{direction}** signal (score {final_score:.0f}) was not executed.",
        color=0xe74c3c,  # Red
        fields=[
            {"name": "Final Spread", "value": f"{spread:.1f} pips", "inline": True},
            {"name": "Reason", "value": "Rollover / Low liquidity / News spike", "inline": True}
        ]
    )


# ============================================================================
# TEST
# ============================================================================

def test_alerts():
    """Test alert sending"""
    print("=" * 60)
    print("🧪 DISCORD ALERTS TEST")
    print("=" * 60)
    
    # Simple test
    print("\n1. Sending simple alert...")
    result = discord.send("Bot connection test", alert_type="info")
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")
    
    # Embed test
    print("\n2. Sending embed...")
    result = discord.send_embed(
        title="🧪 Embed Test",
        description="This is a test of the alert system.",
        color=0x00ff00,
        fields=[
            {"name": "Field 1", "value": "Value 1", "inline": True},
            {"name": "Field 2", "value": "Value 2", "inline": True}
        ]
    )
    print(f"   Result: {'✅ OK' if result else '❌ Failed'}")
    
    print("\n✅ Test complete!")


if __name__ == "__main__":
    test_alerts()
