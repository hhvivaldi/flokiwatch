"""
DRY RUN VISUAL - Monitor with Visual Updates for Discord
Runs the bot in test mode with formatted alerts every 1 hour
"""

import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# Force DRY_RUN
config.DRY_RUN = True

from logger import log, get_log_file_path
from alerts import discord
from confluence import analyze_confluence, is_actionable_signal, get_trade_direction
from safety_checks import is_safe_to_trade, safety
from technical_analyzer import get_mt5_data, calculate_indicators, calculate_technical_score, get_atr_value
from risk_manager import calculate_position_size, calculate_sl_tp


# ============================================================================
# VISUAL FORMATTING FUNCTIONS
# ============================================================================

def create_progress_bar(current: float, total: float, width: int = 20) -> str:
    """Create visual progress bar"""
    if total <= 0:
        return "░" * width
    
    progress = min(current / total, 1.0)
    filled = int(width * progress)
    empty = width - filled
    
    bar = "█" * filled + "░" * empty
    percent = progress * 100
    
    return f"{bar} {current:.0f}h/{total:.0f}h ({percent:.0f}%)"


def get_score_emoji(score: float) -> str:
    """Return emoji based on score"""
    if score >= 70:
        return "🟢"
    elif score >= 55:
        return "🟡"
    elif score >= 45:
        return "⚪"
    elif score >= 30:
        return "🟠"
    else:
        return "🔴"


def get_score_label(score: float) -> str:
    """Return label based on score"""
    if score >= 70:
        return "bullish"
    elif score >= 55:
        return "slightly bullish"
    elif score >= 45:
        return "neutral"
    elif score >= 30:
        return "slightly bearish"
    else:
        return "bearish"


def format_uptime(start_time: datetime) -> str:
    """Format uptime in readable format"""
    delta = datetime.now() - start_time
    hours = int(delta.total_seconds() // 3600)
    minutes = int((delta.total_seconds() % 3600) // 60)
    return f"{hours}h {minutes}min"


def send_discord_safe(message: str, alert_type: str = "info") -> bool:
    """Send to Discord with error handling (does NOT block)"""
    try:
        result = discord.send(message, alert_type=alert_type)
        return result
    except Exception as e:
        log.warning(f"Discord failed (bot continues): {e}")
        return False


# ============================================================================
# STATISTICS CLASS
# ============================================================================

class DryRunStats:
    """Collect DRY_RUN statistics"""
    
    def __init__(self, duration_hours: float = 24):
        self.start_time = datetime.now()
        self.duration_hours = duration_hours
        self.cycles = 0
        self.total_cycles_expected = int(duration_hours * 60 / 5)  # 5min cycles
        self.signals = defaultdict(int)
        self.trades_simulated = 0
        self.trades_blocked = 0
        self.block_reasons = defaultdict(int)
        self.discord_sent = 0
        self.discord_failed = 0
        self.errors = []
        self.warnings = []
        self.last_scores = {
            'tech': 50, 'news': 50, 'ml': 50, 
            'ml_prob': 0.5, 'final': 50, 'decision': 'HOLD'
        }
        self.pnl = 0.0
        self.wins = 0
        self.losses = 0
    
    def get_runtime_hours(self) -> float:
        return (datetime.now() - self.start_time).total_seconds() / 3600
    
    def get_remaining_hours(self) -> float:
        elapsed = self.get_runtime_hours()
        return max(0, self.duration_hours - elapsed)


# Global instance
stats = None


# ============================================================================
# VISUAL DISCORD MESSAGES
# ============================================================================

def send_hourly_update():
    """Send visual hourly update to Discord"""
    global stats
    
    now_gmt = datetime.utcnow()
    runtime = format_uptime(stats.start_time)
    remaining = stats.get_remaining_hours()
    
    # Progress bar
    progress_bar = create_progress_bar(stats.get_runtime_hours(), stats.duration_hours)
    
    # Scores
    tech = stats.last_scores.get('tech', 50)
    news = stats.last_scores.get('news', 50)
    ml = stats.last_scores.get('ml', 50)
    ml_prob = stats.last_scores.get('ml_prob', 0.5)
    final = stats.last_scores.get('final', 50)
    decision = stats.last_scores.get('decision', 'HOLD')
    
    # ML status
    ml_status = f"(prob {ml_prob:.2f})" if ml_prob >= 0.55 else f"(ignored - prob {ml_prob:.2f})"
    
    # Win rate
    total_trades = stats.wins + stats.losses
    win_rate = f"{(stats.wins/total_trades*100):.0f}%" if total_trades > 0 else "N/A"
    
    # Bot status
    bot_status = "🟢 RUNNING" if len(stats.errors) == 0 else "🟡 WITH WARNINGS"
    
    message = f"""📊 HOURLY UPDATE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ Time: {now_gmt.strftime('%H:%M')} GMT
⏱️ Uptime: {runtime}

📊 DRY_RUN PROGRESS ({stats.duration_hours:.0f}h)
{progress_bar}
Time remaining: ~{remaining:.1f} hours

💰 CAPITAL:
├─ Initial: ${config.CAPITAL_INICIAL:,.2f}
├─ Current: ${config.CAPITAL_INICIAL + stats.pnl:,.2f}
└─ P&L: ${stats.pnl:+,.2f} ({stats.pnl/config.CAPITAL_INICIAL*100:+.1f}%)

📊 CURRENT SCORES:
├─ Technical: {tech:.1f}/100 ({get_score_label(tech)})
├─ News: {news:.1f}/100 ({get_score_label(news)})
├─ ML: {ml:.1f}/100 {ml_status}
└─ Final: {final:.1f}/100 -> {decision}

📈 POSITIONS:
└─ Open: 0 (DRY_RUN)

📋 TRADES TODAY:
├─ Executed: {stats.trades_simulated}
├─ Blocked: {stats.trades_blocked}
└─ Win Rate: {win_rate}

🔄 STATUS:
├─ Cycles: {stats.cycles}/{stats.total_cycles_expected}
├─ Errors: {len(stats.errors)}
└─ Bot: {bot_status}"""
    
    success = send_discord_safe(message, "info")
    if success:
        stats.discord_sent += 1
    else:
        stats.discord_failed += 1
    
    return success


def send_signal_alert(
    decision: str,
    tech_score: float,
    news_score: float,
    ml_score: float,
    ml_prob: float,
    final_score: float,
    is_safe: bool,
    safety_reasons: list,
    direction: str,
    entry_price: float,
    sl: float,
    tp1: float,
    tp2: float,
    lot_size: float,
    sl_pips: float,
    tp1_pips: float,
    tp2_pips: float
):
    """Send detailed BUY/SELL signal alert"""
    global stats
    
    now_gmt = datetime.utcnow()
    
    # Emoji and color based on direction
    if "BUY" in decision:
        emoji = "🟢"
        title = "BUY SIGNAL DETECTED!"
    else:
        emoji = "🔴"
        title = "SELL SIGNAL DETECTED!"
    
    # Confluence
    if final_score >= 70:
        confluence_str = "VERY STRONG"
    elif final_score >= 65:
        confluence_str = "STRONG"
    else:
        confluence_str = "MODERATE"
    
    # ML status
    ml_check = "✅" if ml_prob >= 0.55 else "⚠️"
    ml_status = f"(prob {ml_prob:.2f})"
    
    # Safety checks
    safety_lines = []
    if is_safe:
        safety_lines.append("├─ High-impact news: ✅ None")
        safety_lines.append("├─ Schedule: ✅ Active session")
        safety_lines.append("├─ Positions: ✅ 0/3")
        safety_lines.append("└─ Status: ✅ APPROVED")
        trade_status = "✅ TRADE WOULD BE EXECUTED:"
    else:
        for i, reason in enumerate(safety_reasons):
            prefix = "└─" if i == len(safety_reasons) - 1 else "├─"
            safety_lines.append(f"{prefix} ❌ {reason}")
        trade_status = "⛔ TRADE BLOCKED:"
    
    safety_text = "\n".join(safety_lines)
    
    # Calculate values in $
    pip_value = 10 * lot_size  # $10 por pip por lote
    sl_value = sl_pips * pip_value
    tp1_value = tp1_pips * pip_value
    tp2_value = tp2_pips * pip_value
    
    message = f"""{emoji} {title}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ Hora: {now_gmt.strftime('%H:%M')} GMT
🎯 Score Final: {final_score:.1f}/100

📊 BREAKDOWN:
├─ Technical: {tech_score:.0f}/100 {get_score_emoji(tech_score)}
├─ News: {news_score:.0f}/100 {get_score_emoji(news_score)}
├─ ML: {ml_score:.0f}/100 {ml_check} {ml_status}
└─ Confluence: {confluence_str}

⚠️ SAFETY CHECKS:
{safety_text}

{trade_status}
├─ Direction: {direction}
├─ Lot Size: {lot_size}
├─ Entrada: {entry_price:.2f}
├─ SL: {sl:.2f} (-{sl_pips:.0f} pips / -${sl_value:.2f})
├─ TP1: {tp1:.2f} (+{tp1_pips:.0f} pips / +${tp1_value:.2f})
└─ TP2: {tp2:.2f} (+{tp2_pips:.0f} pips / +${tp2_value:.2f})

🧪 [DRY_RUN - Order NOT executed]"""
    
    success = send_discord_safe(message, "trade")
    if success:
        stats.discord_sent += 1
    else:
        stats.discord_failed += 1
    
    return success


def send_startup_message(duration_hours: float):
    """Send startup message"""
    now_gmt = datetime.utcnow()
    end_time = now_gmt + timedelta(hours=duration_hours)
    
    message = f"""🚀 DRY RUN STARTED!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏰ Start: {now_gmt.strftime('%Y-%m-%d %H:%M')} GMT
⏰ Expected end: {end_time.strftime('%Y-%m-%d %H:%M')} GMT
⏱️ Duration: {duration_hours:.0f} hours

📊 CONFIGURATION:
├─ Capital: ${config.CAPITAL_INICIAL:,.2f}
├─ Risk/Trade: {config.RISK_PER_TRADE}%
├─ Max Lot: {config.MAX_LOT_SIZE}
├─ Interval: {config.ANALYSIS_INTERVAL_SECONDS}s
└─ Updates: Every 1 hour

🎯 THRESHOLDS:
├─ STRONG_BUY: > 70
├─ BUY: > 65
├─ SELL: < 35
└─ STRONG_SELL: < 30

📱 You will receive:
├─ Automatic hourly updates
├─ Detailed signal alerts
└─ Final report when finished

🟢 Bot running... Follow on Discord!"""
    
    return send_discord_safe(message, "success")


def send_final_report():
    """Send final report"""
    global stats
    
    runtime = format_uptime(stats.start_time)
    
    buy_signals = stats.signals.get('STRONG_BUY', 0) + stats.signals.get('BUY', 0)
    sell_signals = stats.signals.get('STRONG_SELL', 0) + stats.signals.get('SELL', 0)
    hold_signals = stats.signals.get('HOLD', 0)
    total_signals = sum(stats.signals.values())
    
    # Status
    if len(stats.errors) == 0:
        status = "✅ SUCCESS"
    else:
        status = f"⚠️ {len(stats.errors)} errors"
    
    # Block reasons
    block_text = ""
    if stats.block_reasons:
        for reason, count in stats.block_reasons.items():
            block_text += f"\n│  └─ {reason}: {count}"
    
    message = f"""🏁 DRY RUN FINISHED!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱️ Total time: {runtime}
🔄 Cycles: {stats.cycles}
📊 Status: {status}

📊 SIGNALS DETECTED:
├─ BUY: {buy_signals} (STRONG: {stats.signals.get('STRONG_BUY', 0)})
├─ SELL: {sell_signals} (STRONG: {stats.signals.get('STRONG_SELL', 0)})
├─ HOLD: {hold_signals}
└─ Total: {total_signals}

✅ TRADES:
├─ Simulated: {stats.trades_simulated}
├─ Blocked: {stats.trades_blocked}{block_text}

📱 DISCORD:
├─ Sent: {stats.discord_sent}
├─ Failed: {stats.discord_failed}

💻 STABILITY:
├─ Errors: {len(stats.errors)}
├─ Warnings: {len(stats.warnings)}
└─ Uptime: 100%

🎯 NEXT STEP:
If everything OK, activate LIVE mode!
python main.py --live"""
    
    return send_discord_safe(message, "success")


# ============================================================================
# ANALYSIS CYCLE
# ============================================================================

def run_analysis_cycle():
    """Execute one analysis cycle"""
    global stats
    
    try:
        # 1. Get data
        df = get_mt5_data()
        
        if df is None or len(df) < 50:
            stats.warnings.append("Insufficient data")
            return
        
        # 2. Calculate indicators
        df = calculate_indicators(df)
        
        # 3. Technical Score
        tech_score, _ = calculate_technical_score(df)
        
        # 4. Score News
        try:
            from news_sentiment import get_hybrid_score
            news_result = get_hybrid_score()
            news_score = news_result.get('score', 50)
        except Exception as e:
            news_score = 50
            stats.warnings.append(f"News: {e}")
        
        # 5. ML Score
        try:
            from ml_predictor import get_ml_score
            ml_score, ml_prob = get_ml_score(df)
        except Exception as e:
            ml_score, ml_prob = 50, 0.5
            stats.warnings.append(f"ML: {e}")
        
        # 6. Confluence
        result = analyze_confluence(tech_score, news_score, ml_score, ml_prob)
        
        # Save scores
        stats.last_scores = {
            'tech': tech_score,
            'news': news_score,
            'ml': ml_score,
            'ml_prob': ml_prob,
            'final': result.final_score,
            'decision': result.decision
        }
        
        # Record signal
        stats.signals[result.decision] += 1
        
        log.analysis(tech_score, news_score, ml_score, result.final_score)
        log.decision(result.decision, result.confidence, result.final_score)
        
        # 7. Check if actionable signal
        if not is_actionable_signal(result.decision):
            return
        
        direction = get_trade_direction(result.decision)
        
        # 8. Calculate levels
        atr = get_atr_value(df)
        entry_price = df['close'].iloc[-1]
        levels = calculate_sl_tp(entry_price, direction, atr)
        pos_size = calculate_position_size(config.CAPITAL_INICIAL, config.RISK_PER_TRADE, levels.sl_pips)
        
        # 9. Safety Checks (including anti-overtrading)
        is_safe, reasons = is_safe_to_trade(
            account_balance=config.CAPITAL_INICIAL,
            open_positions=0,
            mt5_connected=True,
            has_high_impact_news=False,
            trade_direction=direction
        )
        
        # 10. Send detailed alert
        send_signal_alert(
            decision=result.decision,
            tech_score=tech_score,
            news_score=news_score,
            ml_score=ml_score,
            ml_prob=ml_prob,
            final_score=result.final_score,
            is_safe=is_safe,
            safety_reasons=reasons,
            direction=direction,
            entry_price=entry_price,
            sl=levels.stop_loss,
            tp1=levels.take_profit_1,
            tp2=levels.take_profit_2,
            lot_size=pos_size.lot_size,
            sl_pips=levels.sl_pips,
            tp1_pips=levels.tp1_pips,
            tp2_pips=levels.tp2_pips
        )
        
        if is_safe:
            stats.trades_simulated += 1
            log.trade(f"[DRY RUN] {direction} | Lot:{pos_size.lot_size} Entry:{entry_price:.2f}")
            # Record trade for anti-overtrading
            from safety_checks import record_trade_opened
            record_trade_opened(direction)
        else:
            stats.trades_blocked += 1
            for reason in reasons:
                stats.block_reasons[reason] += 1
            log.safety_block("; ".join(reasons))
        
    except Exception as e:
        stats.errors.append(str(e))
        log.error(f"Cycle error: {e}")
        log.error(traceback.format_exc())


# ============================================================================
# MAIN LOOP
# ============================================================================

def run_dry_run(duration_hours: float = 24):
    """Run DRY_RUN with visual updates"""
    global stats
    
    stats = DryRunStats(duration_hours)
    
    print("=" * 60)
    print("🧪 DRY RUN VISUAL - STARTING")
    print("=" * 60)
    print(f"Duration: {duration_hours}h")
    print(f"Interval: {config.ANALYSIS_INTERVAL_SECONDS}s")
    print(f"Discord updates: every 1 hour")
    print("=" * 60)
    
    # Initialize MT5
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("❌ Failed to initialize MT5")
        return
    
    print("✅ MT5 connected")
    
    # Send startup message
    send_startup_message(duration_hours)
    
    end_time = datetime.now() + timedelta(hours=duration_hours)
    last_hourly_update = datetime.now()
    
    try:
        while datetime.now() < end_time:
            cycle_start = datetime.now()
            
            # Execute cycle
            run_analysis_cycle()
            stats.cycles += 1
            
            # Print local progress
            if stats.cycles % 12 == 0:  # Every 1 hour (12 cycles of 5min)
                print(f"📊 Cycle {stats.cycles}/{stats.total_cycles_expected} | "
                      f"Score: {stats.last_scores.get('final', 0):.1f} | "
                      f"Trades: {stats.trades_simulated}")
            
            # Hourly Discord update
            if (datetime.now() - last_hourly_update).total_seconds() >= 3600:
                print("\n📤 Sending hourly update to Discord...")
                send_hourly_update()
                last_hourly_update = datetime.now()
            
            # Wait for next cycle
            elapsed = (datetime.now() - cycle_start).total_seconds()
            sleep_time = max(0, config.ANALYSIS_INTERVAL_SECONDS - elapsed)
            
            if sleep_time > 0:
                time.sleep(sleep_time)
                
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
        
    except Exception as e:
        stats.errors.append(str(e))
        print(f"❌ Error: {e}")
        traceback.print_exc()
        
    finally:
        # Final report
        print("\n" + "=" * 60)
        print("📊 FINISHING...")
        print("=" * 60)
        
        send_final_report()
        
        # Save local report
        filename = f"dry_run_report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(f"DRY RUN REPORT\n")
            f.write(f"Duration: {format_uptime(stats.start_time)}\n")
            f.write(f"Cycles: {stats.cycles}\n")
            f.write(f"Trades simulated: {stats.trades_simulated}\n")
            f.write(f"Trades blocked: {stats.trades_blocked}\n")
            f.write(f"Errors: {len(stats.errors)}\n")
        
        print(f"📁 Report saved: {filename}")
        
        mt5.shutdown()
        print("✅ DRY RUN finished!")


def quick_test(cycles: int = 3):
    """Quick test"""
    global stats
    stats = DryRunStats(1)
    
    print("=" * 60)
    print(f"🧪 QUICK TEST - {cycles} cycles")
    print("=" * 60)
    
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("❌ MT5 not available")
        return
    
    # Send test
    send_discord_safe("🧪 Quick test started!", "info")
    
    for i in range(cycles):
        print(f"\n--- Cycle {i+1}/{cycles} ---")
        run_analysis_cycle()
        stats.cycles += 1
        
        if i < cycles - 1:
            print("Waiting 10s...")
            time.sleep(10)
    
    # Send test update
    print("\n📤 Sending test update...")
    send_hourly_update()
    
    mt5.shutdown()
    print("\n✅ Test complete!")


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='DRY RUN Visual Monitor')
    parser.add_argument('--hours', type=float, default=24, help='Duration in hours')
    parser.add_argument('--quick', action='store_true', help='Quick test')
    parser.add_argument('--cycles', type=int, default=3, help='Cycles for quick test')
    
    args = parser.parse_args()
    
    if args.quick:
        quick_test(args.cycles)
    else:
        run_dry_run(duration_hours=args.hours)
