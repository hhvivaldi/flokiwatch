"""
LOGGER - Logging System
Records all bot activities
"""

import os
import logging
from datetime import datetime
from typing import Optional
import config


class TradingLogger:
    """Custom logger for the trading bot"""
    
    def __init__(self, name: str = "TradingBot"):
        self.name = name
        self.log_dir = config.LOG_DIR
        
        # Create logs directory
        os.makedirs(self.log_dir, exist_ok=True)
        
        # Configure logger
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, config.LOG_LEVEL))
        
        # Avoid handler duplication
        if not self.logger.handlers:
            # File handler
            log_file = os.path.join(
                self.log_dir,
                f"trading_bot_{datetime.now().strftime('%Y-%m-%d')}.log"
            )
            file_handler = logging.FileHandler(log_file, encoding='utf-8')
            file_handler.setLevel(logging.DEBUG)
            
            # Console handler
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            
            # Format
            formatter = logging.Formatter(
                '%(asctime)s | %(levelname)-8s | %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )
            file_handler.setFormatter(formatter)
            console_handler.setFormatter(formatter)
            
            self.logger.addHandler(file_handler)
            self.logger.addHandler(console_handler)
    
    def info(self, message: str):
        """Info log"""
        self.logger.info(message)
    
    def debug(self, message: str):
        """Debug log"""
        self.logger.debug(message)
    
    def warning(self, message: str):
        """Warning log"""
        self.logger.warning(message)
    
    def error(self, message: str):
        """Error log"""
        self.logger.error(message)
    
    def critical(self, message: str):
        """Critical log"""
        self.logger.critical(message)
    
    def success(self, message: str):
        """Success log (uses INFO with prefix)"""
        self.logger.info(f"SUCCESS | {message}")
    
    def trade(self, message: str):
        """Trade log (uses INFO with prefix)"""
        self.logger.info(f"TRADE | {message}")
    
    def analysis(self, tech: float, news: float, ml: float, final: float):
        """Complete analysis log"""
        self.logger.info(
            f"ANALYSIS | Tech:{tech:.1f} News:{news:.1f} ML:{ml:.1f} Final:{final:.1f}"
        )
    
    def decision(self, decision: str, confidence: str, score: float):
        """Decision log"""
        self.logger.info(
            f"DECISION | {decision} (confidence: {confidence}) Score:{score:.1f}"
        )
    
    def order(self, action: str, ticket: int, lot: float, price: float, sl: float, tp: float):
        """Order log"""
        self.logger.info(
            f"ORDER | {action} | Ticket:{ticket} Lot:{lot} Price:{price:.2f} SL:{sl:.2f} TP:{tp:.2f}"
        )
    
    def position_update(self, ticket: int, action: str, details: str):
        """Position update log"""
        self.logger.info(f"POSITION | Ticket:{ticket} | {action} | {details}")
    
    def safety_block(self, reason: str):
        """Safety block log"""
        self.logger.warning(f"SAFETY BLOCK | {reason}")
    
    def mt5_status(self, connected: bool, message: str = ""):
        """MT5 status log"""
        status = "CONNECTED" if connected else "DISCONNECTED"
        self.logger.info(f"MT5 | {status} | {message}")


# Global instance
log = TradingLogger()


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_log_file_path() -> str:
    """Return current log file path"""
    return os.path.join(
        config.LOG_DIR,
        f"trading_bot_{datetime.now().strftime('%Y-%m-%d')}.log"
    )


def read_recent_logs(lines: int = 50) -> list:
    """Read the last N lines of the log"""
    log_file = get_log_file_path()
    
    if not os.path.exists(log_file):
        return []
    
    with open(log_file, 'r', encoding='utf-8') as f:
        all_lines = f.readlines()
        return all_lines[-lines:]


def count_today_trades() -> dict:
    """Count today's trades based on logs"""
    log_file = get_log_file_path()
    
    if not os.path.exists(log_file):
        return {'total': 0, 'buys': 0, 'sells': 0}
    
    buys = 0
    sells = 0
    
    with open(log_file, 'r', encoding='utf-8') as f:
        for line in f:
            if 'ORDER | BUY' in line:
                buys += 1
            elif 'ORDER | SELL' in line:
                sells += 1
    
    return {
        'total': buys + sells,
        'buys': buys,
        'sells': sells
    }


# ============================================================================
# TEST
# ============================================================================

def test_logger():
    """Test the logging system"""
    print("=" * 60)
    print("🧪 LOGGING SYSTEM TEST")
    print("=" * 60)
    
    log.info("Bot started")
    log.analysis(tech=75.5, news=68.2, ml=62.0, final=70.3)
    log.decision("BUY", "high", 70.3)
    log.order("BUY", 12345, 0.02, 2650.50, 2635.00, 2680.00)
    log.success("Order executed successfully")
    log.position_update(12345, "TP1_HIT", "50% closed, SL moved to breakeven")
    log.safety_block("Asian session active")
    log.warning("High spread detected")
    log.error("Failed to connect MT5")
    log.mt5_status(True, "Connected to ICMarkets server")
    
    print(f"\n📁 Log saved to: {get_log_file_path()}")
    
    print("\n📋 Last log lines:")
    for line in read_recent_logs(10):
        print(f"   {line.strip()}")


if __name__ == "__main__":
    test_logger()
