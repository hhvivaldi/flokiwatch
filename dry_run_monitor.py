"""
DRY RUN MONITOR - Monitors and generates DRY_RUN report
Runs the bot in test mode and collects statistics
With visual updates for Discord every 1 hour
"""

import os
import sys
import time
import json
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# Force DRY_RUN
config.DRY_RUN = True

from logger import log, get_log_file_path, read_recent_logs
from alerts import discord
from confluence import analyze_confluence, is_actionable_signal, get_trade_direction
from safety_checks import is_safe_to_trade, safety, get_safety_status
from technical_analyzer import get_mt5_data, calculate_indicators, calculate_technical_score, get_atr_value
from risk_manager import calculate_position_size, calculate_sl_tp
from executor import executor, get_account_balance
import traceback


class DryRunStats:
    """Collect DRY_RUN statistics"""
    
    def __init__(self):
        self.start_time = datetime.now()
        self.cycles = 0
        self.signals = defaultdict(int)  # BUY, SELL, HOLD counts
        self.trades_simulated = 0
        self.trades_blocked = 0
        self.block_reasons = defaultdict(int)
        self.discord_sent = 0
        self.discord_failed = 0
        self.errors = []
        self.warnings = []
        self.crashes = 0
        self.mt5_reconnects = 0
        self.last_scores = {}
        
    def record_cycle(self):
        self.cycles += 1
        
    def record_signal(self, decision: str):
        self.signals[decision] += 1
        
    def record_trade_simulated(self):
        self.trades_simulated += 1
        
    def record_trade_blocked(self, reason: str):
        self.trades_blocked += 1
        self.block_reasons[reason] += 1
        
    def record_discord_sent(self, success: bool):
        if success:
            self.discord_sent += 1
        else:
            self.discord_failed += 1
            
    def record_error(self, error: str):
        self.errors.append(f"{datetime.now().isoformat()}: {error}")
        
    def record_warning(self, warning: str):
        self.warnings.append(f"{datetime.now().isoformat()}: {warning}")
        
    def get_runtime(self) -> timedelta:
        return datetime.now() - self.start_time
    
    def get_runtime_str(self) -> str:
        runtime = self.get_runtime()
        hours = runtime.total_seconds() / 3600
        return f"{hours:.1f}h"
    
    def generate_report(self) -> str:
        """Generate formatted report"""
        runtime = self.get_runtime()
        hours = runtime.total_seconds() / 3600
        
        total_signals = sum(self.signals.values())
        buy_signals = self.signals.get('STRONG_BUY', 0) + self.signals.get('BUY', 0)
        sell_signals = self.signals.get('STRONG_SELL', 0) + self.signals.get('SELL', 0)
        hold_signals = self.signals.get('HOLD', 0)
        
        discord_total = self.discord_sent + self.discord_failed
        discord_status = "✅ OK" if self.discord_failed == 0 else f"⚠️ {self.discord_failed} failures"
        
        errors_status = "✅ Clean" if len(self.errors) == 0 else f"❌ {len(self.errors)} errors"
        
        uptime = 100.0 if self.crashes == 0 else ((self.cycles - self.crashes) / self.cycles * 100)
        
        report = f"""
📊 DRY_RUN REPORT ({self.get_runtime_str()})
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⏱️ Running time: {hours:.1f}h
🔄 Cycles completed: {self.cycles}

📊 SIGNALS DETECTED:
├─ BUY signals: {buy_signals} (STRONG: {self.signals.get('STRONG_BUY', 0)}, BUY: {self.signals.get('BUY', 0)})
├─ SELL signals: {sell_signals} (STRONG: {self.signals.get('STRONG_SELL', 0)}, SELL: {self.signals.get('SELL', 0)})
├─ HOLD (neutral): {hold_signals}
└─ Total: {total_signals}

 SIMULATED TRADES:
├─ Executed: {self.trades_simulated}
├─ Blocked: {self.trades_blocked}
"""
        
        if self.block_reasons:
            report += " Block reasons:\n"
            for reason, count in self.block_reasons.items():
                report += f"│  └─ {reason}: {count}\n"
        
        report += f"""
 DISCORD:
├─ Alerts sent: {self.discord_sent}
├─ Alerts failed: {self.discord_failed}
└─ Status: {discord_status}

🐛 ERRORS:
├─ Critical: {len(self.errors)}
├─ Warnings: {len(self.warnings)}
└─ Logs: {errors_status}

💻 STABILITY:
├─ Crashes: {self.crashes}
├─ MT5 reconnections: {self.mt5_reconnects}
└─ Uptime: {uptime:.1f}%
"""
        
        if self.last_scores:
            report += f"""
📈 LAST SCORE:
├─ Tech: {self.last_scores.get('tech', 0):.1f}
├─ News: {self.last_scores.get('news', 0):.1f}
├─ ML: {self.last_scores.get('ml', 0):.1f}
└─ Final: {self.last_scores.get('final', 0):.1f}
"""
        
        return report
    
    def save_report(self, filename: str = "dry_run_report.txt"):
        """Save report to file"""
        report = self.generate_report()
        with open(filename, 'w', encoding='utf-8') as f:
            f.write(report)
        return filename


# Global statistics instance
stats = DryRunStats()


def run_analysis_cycle():
    """Execute one analysis cycle"""
    global stats
    
    try:
        # 1. Get data
        df = get_mt5_data()
        
        if df is None or len(df) < 50:
            stats.record_warning("Insufficient data")
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
            stats.record_warning(f"News error: {e}")
        
        # 5. Score ML
        try:
            from ml_predictor import get_ml_score
            ml_score, ml_prob = get_ml_score(df)
        except Exception as e:
            ml_score, ml_prob = 50, 0.5
            stats.record_warning(f"ML error: {e}")
        
        # 6. Confluence
        result = analyze_confluence(tech_score, news_score, ml_score, ml_prob)
        
        # Save scores
        stats.last_scores = {
            'tech': tech_score,
            'news': news_score,
            'ml': ml_score,
            'final': result.final_score
        }
        
        # Record signal
        stats.record_signal(result.decision)
        
        log.analysis(tech_score, news_score, ml_score, result.final_score)
        log.decision(result.decision, result.confidence, result.final_score)
        
        # 7. Check if actionable
        if not is_actionable_signal(result.decision):
            return
        
        direction = get_trade_direction(result.decision)
        
        # 8. Safety Checks
        account_balance = config.CAPITAL_INICIAL
        
        is_safe, reasons = is_safe_to_trade(
            account_balance=account_balance,
            open_positions=0,
            mt5_connected=True,
            has_high_impact_news=False
        )
        
        if not is_safe:
            for reason in reasons:
                stats.record_trade_blocked(reason)
            log.safety_block("; ".join(reasons))
            return
        
        # 9. Trade would be executed!
        stats.record_trade_simulated()
        
        atr = get_atr_value(df)
        entry_price = df['close'].iloc[-1]
        levels = calculate_sl_tp(entry_price, direction, atr)
        pos_size = calculate_position_size(account_balance, config.RISK_PER_TRADE, levels.sl_pips)
        
        log.trade(f"[DRY RUN] {direction} | Lot:{pos_size.lot_size} Entry:{entry_price:.2f} SL:{levels.stop_loss:.2f} TP:{levels.take_profit_1:.2f}")
        
        # Send Discord alert
        from alerts import alert_signal_detected
        result_discord = discord.send(
            f"🧪 [DRY RUN] Signal {result.decision} detected!\nScore: {result.final_score:.1f}\nDirection: {direction}",
            alert_type="trade"
        )
        stats.record_discord_sent(result_discord)
        
    except Exception as e:
        stats.record_error(str(e))
        log.error(f"Cycle error: {e}")


def run_dry_run(duration_hours: float = 24, report_interval_minutes: int = 60):
    """
    Run DRY_RUN for specified duration.
    
    Args:
        duration_hours: Duration in hours
        report_interval_minutes: Interval for generating partial reports
    """
    global stats
    
    print("=" * 60)
    print("🧪 DRY RUN MONITOR - STARTING")
    print("=" * 60)
    print(f"Planned duration: {duration_hours}h")
    print(f"Analysis interval: {config.ANALYSIS_INTERVAL_SECONDS}s")
    print(f"Report every: {report_interval_minutes}min")
    print("=" * 60)
    
    # Initialize MT5
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("❌ Failed to initialize MT5")
        return
    
    print("✅ MT5 connected")
    
    # Initial alert
    discord.send("🧪 DRY RUN started! Monitoring for {:.0f}h...".format(duration_hours), alert_type="info")
    
    end_time = datetime.now() + timedelta(hours=duration_hours)
    last_report_time = datetime.now()
    
    try:
        while datetime.now() < end_time:
            cycle_start = datetime.now()
            
            # Execute cycle
            run_analysis_cycle()
            stats.record_cycle()
            
            # Generate partial report
            if (datetime.now() - last_report_time).total_seconds() >= report_interval_minutes * 60:
                report = stats.generate_report()
                print(report)
                stats.save_report()
                last_report_time = datetime.now()
                
                # Send summary to Discord
                discord.send(
                    f"📊 DRY RUN Update ({stats.get_runtime_str()})\n"
                    f"Cycles: {stats.cycles}\n"
                    f"Simulated trades: {stats.trades_simulated}\n"
                    f"Blocked: {stats.trades_blocked}",
                    alert_type="info"
                )
            
            # Wait for next cycle
            elapsed = (datetime.now() - cycle_start).total_seconds()
            sleep_time = max(0, config.ANALYSIS_INTERVAL_SECONDS - elapsed)
            
            if sleep_time > 0:
                time.sleep(sleep_time)
                
    except KeyboardInterrupt:
        print("\n⚠️ Interrupted by user")
        
    except Exception as e:
        stats.record_error(str(e))
        stats.crashes += 1
        print(f"❌ Error: {e}")
        
    finally:
        # Final report
        print("\n" + "=" * 60)
        print("📊 FINAL REPORT")
        print("=" * 60)
        
        report = stats.generate_report()
        print(report)
        
        filename = stats.save_report(f"dry_run_report_{datetime.now().strftime('%Y%m%d_%H%M')}.txt")
        print(f"\n📁 Report saved to: {filename}")
        
        # Send final report to Discord
        discord.send(report, alert_type="info")
        
        mt5.shutdown()
        print("\n✅ DRY RUN finished!")


def quick_test(cycles: int = 5):
    """Quick test with few cycles"""
    global stats
    stats = DryRunStats()
    
    print("=" * 60)
    print(f"🧪 QUICK TEST - {cycles} cycles")
    print("=" * 60)
    
    import MetaTrader5 as mt5
    if not mt5.initialize():
        print("❌ MT5 not available")
        return
    
    for i in range(cycles):
        print(f"\n--- Cycle {i+1}/{cycles} ---")
        run_analysis_cycle()
        stats.record_cycle()
        
        if i < cycles - 1:
            print("Waiting 10s...")
            time.sleep(10)
    
    mt5.shutdown()
    
    print("\n" + stats.generate_report())


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='DRY RUN Monitor')
    parser.add_argument('--hours', type=float, default=24, help='Duration in hours')
    parser.add_argument('--quick', action='store_true', help='Quick test (5 cycles)')
    parser.add_argument('--cycles', type=int, default=5, help='Number of cycles for quick test')
    
    args = parser.parse_args()
    
    if args.quick:
        quick_test(args.cycles)
    else:
        run_dry_run(duration_hours=args.hours)
